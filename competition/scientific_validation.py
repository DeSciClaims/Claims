"""Controller-side scientific gate, independent of miner reports and sources."""

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import tempfile
from typing import Callable

from validator.scientific_result_v1.engine import validate
from validator.scientific_result_v1.package import materialize
from validator.scientific_result_v1.sources import SourceBundle


@dataclass
class TrustedScientificValidator:
    source_bundle_for_job: Callable[[dict], SourceBundle]
    runtime_for_job: Callable[[dict], object]
    report_root: Path
    review_options: dict = field(default_factory=dict)

    def __call__(self, artifact, job, pdf):
        # Report filenames do not use miner-controlled job/paper/hotkey paths.
        token = hashlib.sha256(str(job["job_id"]).encode()).hexdigest()
        self.report_root.mkdir(parents=True, exist_ok=True)
        report_dir = Path(tempfile.mkdtemp(prefix=token + "-", dir=self.report_root))
        try:
            bundle = self.source_bundle_for_job(job)
        except Exception as exc:
            raise RuntimeError("Controller source bundle unavailable") from exc
        pdf_hash = hashlib.sha256(pdf).hexdigest()
        if bundle.main_pdf_sha256 != pdf_hash or bundle.paper_id != job["paper_id"]:
            raise RuntimeError(
                "Controller source bundle differs from the released paper"
            )
        with tempfile.TemporaryDirectory(prefix="claims-scientific-") as work:
            package_dir = Path(work) / "package"
            payload = materialize(artifact, package_dir, paper_id=job["paper_id"])
            try:
                report = validate(
                    payload,
                    package_dir,
                    sources=bundle,
                    runtime=self.runtime_for_job(job),
                    expected_paper_id=job["paper_id"],
                    expected_pdf_sha256=pdf_hash,
                    scope="trusted",
                    report_dir=report_dir,
                    **self.review_options,
                )
            except Exception as exc:
                raise RuntimeError("Trusted validator could not execute") from exc
        report["validation_report_sha256"] = hashlib.sha256(
            (report_dir / "validation_report.json").read_bytes()
        ).hexdigest()
        return report


@dataclass
class TrustedScientificDeliveryValidator:
    """Worker collection checks only; scientific scoring waits for the common pool."""
    source_bundle_for_job: Callable[[dict], SourceBundle]
    report_root: Path
    read_source_file: Callable | None = None

    def worker_inputs(self,job):
        bundle=self.source_bundle_for_job(job)
        if self.read_source_file is None or not hasattr(bundle,"state"):
            raise RuntimeError("Scientific source packet reader is required before execution")
        documents=[];files={};total=0
        for i,document in enumerate(bundle.documents):
            if document.file is None:continue
            body=self.read_source_file(document.file);total+=len(body)
            suffix={"application/pdf":"pdf","text/plain":"txt","text/html":"html","text/csv":"csv"}.get(document.file.media_type)
            if suffix is None:raise RuntimeError("Unsupported supplied source media type")
            name="paper.pdf" if document.role=="MAIN" else "supplied-source-S"+str(i+1).zfill(3)+"."+suffix
            files[name]=body
            documents.append({"source_id":"S"+str(i+1).zfill(3),"document_uid":document.document_uid,"role":document.role,
                "path":name,"sha256":document.file.sha256,"byte_length":len(body),"media_type":document.file.media_type})
        if total>210_000_000:raise RuntimeError("Common scientific input bundle exceeds profile")
        return {"manifest":{"source_scope_policy":"supplied_sources_only","sources":documents},"files":files}

    def __call__(self, artifact, job, pdf):
        if not isinstance(artifact,dict) or artifact.get('schema_version')!='claims_scientific_package_v2':
            raise ValueError('Source-task submissions require original JSON bytes in the v2 transport')
        from validator.scientific_result_v1.source_tasks.packages import Package
        from validator.scientific_result_v1.source_tasks.store import canonical,sha
        bundle=self.source_bundle_for_job(job)
        if hasattr(bundle,"main_pdf_sha256"):
            main_hash=bundle.main_pdf_sha256;bundle_hash=bundle.sha256
        else:
            mains=[d for d in bundle.documents if d.role=="MAIN" and d.file is not None]
            if bundle.state!="READY" or len(mains)!=1:raise RuntimeError("Controller source packet is not ready")
            main_hash=mains[0].file.sha256;bundle_hash=sha(canonical(bundle))
        if bundle.paper_id!=job['paper_id'] or main_hash!=hashlib.sha256(pdf).hexdigest():
            raise RuntimeError('Controller source differs from released input')
        with tempfile.TemporaryDirectory(prefix='claims-delivery-') as work:
            directory=Path(work)/'package'
            materialize(artifact,directory,paper_id=job['paper_id'],max_bytes=210_000_000)
            original=directory/'extraction.json'
            original.rename(directory/'output.json')
            package=Package(directory,'delivery',paper_id=job['paper_id'])
            report={'schema_version':'claims_scientific_delivery_v1','validation_protocol':'claims-source-task-v1',
                    'validation_phase':'mechanical_delivery','delivery_accepted':bool(package.gate.gate_bit),
                    'authoritative':True,'execution_status':'completed','gate':package.gate.model_dump(mode='json'),
                    'source_bundle_sha256':bundle_hash,'package_sha256':sha(canonical({k:sha(v) for k,v in package.files.items()}))}
        self.report_root.mkdir(parents=True,exist_ok=True)
        token=hashlib.sha256(str(job['job_id']).encode()).hexdigest()
        path=self.report_root/(token+'-'+sha(canonical(report))+'.json')
        path.write_bytes(canonical(report));report['validation_report_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        return report
