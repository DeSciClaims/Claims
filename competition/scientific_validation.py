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
