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
    common_exclusion: Any = None
    first_round_score: Any = None
    score_details_by_hotkey: Mapping[str, Any] | None = None


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


def adapt_common_exclusion(receipt,receipt_ref,exclusion_ref,*,store,hotkey_by_alias,baseline_alias,challenger_aliases,usage=()):
    from validator.scientific_result_v1.source_tasks.contracts import CommonExclusionReceipt
    exclusion=store.load(exclusion_ref,CommonExclusionReceipt)
    expected={baseline_alias,*challenger_aliases}
    if (receipt.state not in {'HOLD','INELIGIBLE'} or exclusion.binding!=receipt.binding
        or exclusion.silver_receipt!=receipt_ref or not exclusion.terminal_bounds_exhausted
        or set(exclusion.applies_to_all_aliases)!=expected
        or {v.output_alias for v in receipt.paper_scores}!=expected
        or any(v.score is not None for v in receipt.paper_scores)):
        raise ValueError('Common exclusion must preserve one unmeasured outcome for every entrant')
    pairs=[]
    for alias in challenger_aliases:
        details=PairPublicDetails(protocol_id='claims-source-task-v1',silver_receipt_sha256=receipt_ref.sha256,
            revision=receipt.binding.revision,evaluation_status='COMMON_EXCLUDED',challenger_components=None,
            baseline_components=None,exclusion_receipt=exclusion_ref)
        pairs.append(CompetitionPairRow(paper_id=receipt.binding.paper_id,challenger_hotkey=hotkey_by_alias[alias],
            baseline_hotkey=hotkey_by_alias[baseline_alias],challenger_score=0.0,baseline_score=0.0,
            infrastructure_fault=True,details_public=details).model_dump(mode='json'))
    return ScientificPaperEvaluation(receipt,receipt_ref.sha256,{},tuple(pairs),tuple(usage),
        output_alias_by_hotkey={v:k for k,v in hotkey_by_alias.items()},common_exclusion=exclusion_ref)


def adapt_first_round_score(receipt, receipt_ref, score_ref, *, scoring_policy,
                           store, hotkey_by_alias, baseline_alias, challenger_aliases, usage=()):
    from .scientific_first_round import verify_score_receipt, score_details, KIND
    from validator.scientific_result_v1.source_tasks.contracts import FractionValue
    if store.load(receipt_ref, SampledSilverReceipt) != receipt:
        raise ValueError("Scientific receipt hash mismatch")
    value = verify_score_receipt(store, score_ref, receipt_ref, scoring_policy)
    expected = {baseline_alias, *challenger_aliases}
    vectors = {s["output_alias"]: s for s in value["scores"]}
    if (set(vectors) != expected or not expected <= hotkey_by_alias.keys()
            or len(set(hotkey_by_alias.values())) != len(hotkey_by_alias)):
        raise ValueError("Incomplete first-round score grid")
    refs = {alias: store.put({"kind": KIND, "paper_id": receipt.binding.paper_id,
            "score_receipt": score_ref.model_dump(mode="json"), **vector})
            for alias, vector in vectors.items()}
    scores = {hotkey_by_alias[alias]: float(FractionValue(**vector["score"]).fraction())
              for alias, vector in vectors.items()}
    pairs = []
    for alias in challenger_aliases:
        details = PairPublicDetails(protocol_id=PROTOCOL, silver_receipt_sha256=receipt_ref.sha256,
            revision=receipt.binding.revision, evaluation_status="SCORED",
            challenger_components=refs[alias], baseline_components=refs[baseline_alias], exclusion_receipt=None)
        pairs.append(CompetitionPairRow(paper_id=receipt.binding.paper_id,
            challenger_hotkey=hotkey_by_alias[alias], baseline_hotkey=hotkey_by_alias[baseline_alias],
            challenger_score=scores[hotkey_by_alias[alias]], baseline_score=scores[hotkey_by_alias[baseline_alias]],
            infrastructure_fault=False, details_public=details).model_dump(mode="json"))
    return ScientificPaperEvaluation(receipt, receipt_ref.sha256, scores, tuple(pairs), tuple(usage),
        output_alias_by_hotkey={v:k for k,v in hotkey_by_alias.items()}, first_round_score=score_ref,
        score_details_by_hotkey={hotkey_by_alias[alias]: score_details(value, score_ref, alias) for alias in vectors})


def scientific_copy_diagnostics(*,round_id,stage,entries,paper_ids,backend):
    """Equality is a diagnostic only; never inspect claims or veto similar science."""
    from .publication import canonical_sha256
    from validator.scientific_result_v1.source_tasks.contracts import SubmittedPackage
    from validator.scientific_result_v1.source_tasks.store import sha,canonical
    from .scientific_lineage import evidence_store
    baseline=next(e for e in entries if e['role'] in {'incumbent','reference'})
    challengers=[e for e in entries if e['role']=='challenger']
    evidence=[]
    with evidence_store(backend) as store:
        for paper_id in paper_ids:
            frozen=backend.get_paper_evaluation(round_id,stage,paper_id)
            silver=frozen['result_private']['silver']
            receipt=SampledSilverReceipt.model_validate_json(canonical(silver['sampled_receipt']),strict=True)
            if (receipt.state!='FROZEN' and 'common_exclusion' not in silver and 'first_round_score' not in silver) or sha(canonical(receipt))!=silver['receipt_sha256']:
                raise ValueError('Scientific copy diagnostic has stale/corrupt receipt')
            if 'first_round_score' in silver:
                from .scientific_first_round import configured_policy, verify_score_receipt
                from validator.scientific_result_v1.source_tasks.contracts import FileRef
                record=backend.get_round(round_id)
                policy=configured_policy(record['rules_public']['configuration'],round_id)
                if policy is None:raise ValueError('First-round scoring is not enabled')
                ref=FileRef(relative_path='blobs/'+silver['receipt_sha256'],sha256=silver['receipt_sha256'],
                    byte_length=len(canonical(receipt)),media_type='application/octet-stream')
                verify_score_receipt(store,FileRef.model_validate(silver['first_round_score'],strict=True),ref,policy)
            if (receipt.binding.round_id,receipt.binding.stage,receipt.binding.paper_id)!=(round_id,stage,paper_id):raise ValueError('Scientific copy scope mismatch')
            packages=[store.load(f,SubmittedPackage) for f in receipt.input_packages]
            if any(p.source_packet_sha256!=receipt.binding.source_packet_sha256 for p in packages):raise ValueError('Scientific pipelines did not share source packet')
            evidence.append({'paper_id':paper_id,'silver_receipt_sha256':silver['receipt_sha256'],'source_packet_sha256':receipt.binding.source_packet_sha256})
    return {e['hotkey']:{'schema_version':'claims_source_copy_diagnostic_v1','is_copy':False,
        'semantic_copy_check':'not_applicable','executable_package_hash_equal':e['package_sha256']==baseline['package_sha256'],
        'source_packet_hash_equal':True,'evidence_sha256':canonical_sha256(evidence)} for e in challengers}
