"""Adapt one common scientific receipt into existing paper/pair score interfaces."""

from dataclasses import dataclass
from typing import Mapping, Any
from validator.scientific_result_v1.source_tasks.contracts import (
    SampledSilverReceipt,
    PublicScoreComponents,
    PairPublicDetails,
    CompetitionPairRow,
)
from validator.scientific_result_v1.source_tasks.sampling import PROTOCOL


@dataclass(frozen=True)
class ScientificPaperEvaluation:
    pipeline: SampledSilverReceipt
    receipt_sha256: str
    scores_by_hotkey: Mapping[str, float]
    pair_rows: tuple[dict[str, Any], ...]
    judge_usage_events: tuple[dict[str, Any], ...] = ()
    resources: dict[str, Any] | None = None
    validation_protocol: str = PROTOCOL
    output_alias_by_hotkey: Mapping[str,str] | None = None


def adapt_scientific_receipt(
    receipt,
    receipt_ref,
    *,
    store,
    hotkey_by_alias,
    baseline_alias,
    challenger_aliases,
    usage=(),
):
    if not isinstance(receipt, SampledSilverReceipt):
        raise ValueError("Wrong protocol receipt")
    if store.load(receipt_ref, SampledSilverReceipt) != receipt:
        raise ValueError("Scientific receipt hash mismatch")
    if len(set(hotkey_by_alias.values())) != len(hotkey_by_alias):
        raise ValueError("Duplicate scientific hotkey mapping")
    if receipt.state != "FROZEN":
        return ScientificPaperEvaluation(
            receipt, receipt_ref.sha256, {}, (), tuple(usage), output_alias_by_hotkey={v:k for k,v in hotkey_by_alias.items()}
        )
    vectors = {v.output_alias: v for v in receipt.paper_scores}
    expected = {baseline_alias, *challenger_aliases}
    if set(vectors) != expected or not expected <= hotkey_by_alias.keys():
        raise ValueError("Incomplete scientific score grid")
    refs = {}
    scores = {}
    for alias, vector in vectors.items():
        if vector.state not in {"SCORED", "PIPELINE_FAILURE"} or vector.score is None:
            raise ValueError("Frozen receipt contains unmeasured score")
        if vector.binding != receipt.binding:
            raise ValueError("Scientific score binding mismatch")
        public = PublicScoreComponents(
            paper_id=receipt.binding.paper_id,
            output_alias=alias,
            state=vector.state,
            gate=vector.gate,
            content=vector.content,
            archive=vector.archive,
            output_audit=vector.output_audit,
            score=vector.score,
            content_available=vector.content_available,
            archive_available=vector.archive_available,
        )
        refs[alias] = store.put(public)
        scores[hotkey_by_alias[alias]] = float(vector.score.fraction())
    rows = []
    for alias in challenger_aliases:
        details = PairPublicDetails(
            protocol_id=PROTOCOL,
            silver_receipt_sha256=receipt_ref.sha256,
            revision=receipt.binding.revision,
            evaluation_status="SCORED",
            challenger_components=refs[alias],
            baseline_components=refs[baseline_alias],
            exclusion_receipt=None,
        )
        rows.append(
            CompetitionPairRow(
                paper_id=receipt.binding.paper_id,
                challenger_hotkey=hotkey_by_alias[alias],
                baseline_hotkey=hotkey_by_alias[baseline_alias],
                challenger_score=scores[hotkey_by_alias[alias]],
                baseline_score=scores[hotkey_by_alias[baseline_alias]],
                infrastructure_fault=False,
                details_public=details,
            ).model_dump(mode="json")
        )
    return ScientificPaperEvaluation(
        receipt, receipt_ref.sha256, scores, tuple(rows), tuple(usage), output_alias_by_hotkey={v:k for k,v in hotkey_by_alias.items()}
    )


def scientific_copy_diagnostics(*,round_id,stage,entries,paper_ids,backend):
    """Equality is a diagnostic only; never inspect claims or veto similar science."""
    from .publication import canonical_sha256
    from validator.scientific_result_v1.source_tasks.contracts import SubmittedPackage
    from validator.scientific_result_v1.source_tasks.store import sha,canonical
    import tempfile
    from validator.scientific_result_v1.source_tasks.remote import RemoteStore
    baseline=next(e for e in entries if e['role'] in {'incumbent','reference'})
    challengers=[e for e in entries if e['role']=='challenger']
    evidence=[]
    with tempfile.TemporaryDirectory(prefix='claims-scientific-copy-') as work:
        store=RemoteStore(work,backend)
        for paper_id in paper_ids:
            frozen=backend.get_paper_evaluation(round_id,stage,paper_id)
            silver=frozen['result_private']['silver']
            receipt=SampledSilverReceipt.model_validate_json(canonical(silver['sampled_receipt']),strict=True)
            if receipt.state!='FROZEN' or sha(canonical(receipt))!=silver['receipt_sha256']:
                raise ValueError('Scientific copy diagnostic has stale/corrupt receipt')
            if (receipt.binding.round_id,receipt.binding.stage,receipt.binding.paper_id)!=(round_id,stage,paper_id):raise ValueError('Scientific copy scope mismatch')
            packages=[store.load(f,SubmittedPackage) for f in receipt.input_packages]
            if any(p.source_packet_sha256!=receipt.binding.source_packet_sha256 for p in packages):raise ValueError('Scientific pipelines did not share source packet')
            evidence.append({'paper_id':paper_id,'silver_receipt_sha256':silver['receipt_sha256'],'source_packet_sha256':receipt.binding.source_packet_sha256})
    return {e['hotkey']:{'schema_version':'claims_source_copy_diagnostic_v1','is_copy':False,
        'semantic_copy_check':'not_applicable','executable_package_hash_equal':e['package_sha256']==baseline['package_sha256'],
        'source_packet_hash_equal':True,'evidence_sha256':canonical_sha256(evidence)} for e in challengers}
