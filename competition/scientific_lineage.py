"""Verify the current common scientific generation before ranking or publication."""
from contextlib import contextmanager
import tempfile
import json
import os
from pathlib import Path
from validator.scientific_result_v1.source_tasks.contracts import ComparisonPlan,SourceGridManifest,FileRef
from validator.scientific_result_v1.source_tasks.store import sha,canonical
from validator.scientific_result_v1.source_tasks.replay import replay


def enabled(record):
    return record.get('rules_public',{}).get('policy',{}).get('validation_protocol')=='claims-source-task-v1'


@contextmanager
def evidence_store(backend):
    local=getattr(backend,'scientific_store',None)
    if local is not None:
        yield local
        return
    from validator.scientific_result_v1.source_tasks.remote import RemoteStore
    if not hasattr(backend,'base_url'):
        raise ValueError('Scientific replay requires retained evidence and current generation heads')
    root=os.environ.get('CLAIMS_SCIENTIFIC_EVIDENCE_ROOT')
    if root:
        # Immutable blobs remain hash-checked; logical revision heads stay remote.
        yield RemoteStore(Path(root),backend)
        return
    with tempfile.TemporaryDirectory(prefix='claims-scientific-lineage-') as root:
        yield RemoteStore(root,backend)


def reference(store,digest):
    raw=store.get_blob(digest)
    return FileRef(relative_path='blobs/'+digest,sha256=digest,byte_length=len(raw),media_type='application/octet-stream')


def compatible_plan(store,receipt,current,precommitted_order,*,allow_excluded=False):
    old=store.load(receipt.comparison_plan,ComparisonPlan)
    seen=set();child=current
    grid=store.load(current.source_grid,SourceGridManifest)
    active=set(grid.initial_paper_ids)|{r.replacement_paper_id for r in grid.replacements}
    active-={r.excluded_paper_id for r in grid.replacements}
    if receipt.binding.paper_id not in active and not allow_excluded:raise ValueError('Excluded scientific paper cannot return to the score denominator')
    while child!=old:
        if child.reason!='COMMON_REPLACEMENT' or child.parent_plan_sha256 is None:
            raise ValueError('Scientific score belongs to a superseded baseline or incompatible plan')
        if child.parent_plan_sha256 in seen:raise ValueError('Cyclic scientific plan lineage')
        seen.add(child.parent_plan_sha256)
        parent=store.load(reference(store,child.parent_plan_sha256),ComparisonPlan)
        child.validate_transition_from(parent,child.parent_plan_sha256)
        old_grid=store.load(parent.source_grid,SourceGridManifest)
        new_grid=store.load(child.source_grid,SourceGridManifest)
        new_grid.validate_transition_from(old_grid,precommitted_order,sha(canonical(precommitted_order)))
        child=parent


def verify_current_grid(round_id,stage,*,backend,pairs,round_record=None):
    # Independent publication already owns a verified immutable round snapshot.
    # Use that policy rather than assuming its local backend exposes get_round.
    record=backend.get_round(round_id) if round_record is None else round_record
    if not enabled(record):return []
    from .scientific_scoring import adapt_scientific_receipt,adapt_common_exclusion,adapt_first_round_score
    from .scientific_first_round import configured_policy
    first_round_policy=configured_policy(record.get('rules_public',{}).get('configuration',{}),round_id)
    evidence=[]
    with evidence_store(backend) as store:
        stage_head=store.head('scientific-plan:'+round_id+':'+stage)
        if stage_head is None:raise ValueError('Scientific stage has no current comparison plan')
        plan_data=json.loads(store.get_blob(stage_head))
        current=store.load(FileRef.model_validate(plan_data['comparison_plan'],strict=True),ComparisonPlan)
        aliases=plan_data['alias_by_hotkey'];hotkeys={v:k for k,v in aliases.items()}
        for paper_id in sorted({p['paper_id'] for p in pairs}):
            frozen=backend.get_paper_evaluation(round_id,stage,paper_id)
            if frozen is None:raise ValueError('Scientific paper has no frozen score projection')
            silver=frozen['result_private']['silver']
            digest=silver['receipt_sha256']
            if store.head('paper:'+json.dumps([round_id,stage,paper_id]))!=digest:
                raise ValueError('Scientific score projection is stale; current common revision must be published first')
            ref=reference(store,digest);receipt=replay(store,ref)
            excluded='common_exclusion' in silver
            partial='first_round_score' in silver
            if partial and (first_round_policy is None or excluded):raise ValueError('Unauthorized first-round score projection')
            if (receipt.state!='FROZEN' and not excluded and not partial) or receipt.binding.policy_sha256!=record['configuration_sha256']:
                raise ValueError('Scientific paper is unresolved or uses a different policy')
            compatible_plan(store,receipt,current,plan_data['precommitted_order'],allow_excluded=excluded)
            if silver['sampled_receipt']!=receipt.model_dump(mode='json') or silver['output_alias_by_hotkey']!=aliases:
                raise ValueError('Scientific projection changed the original receipt or alias mapping')
            if excluded:
                exclusion_ref=FileRef.model_validate(silver['common_exclusion'],strict=True)
                grid=store.load(current.source_grid,SourceGridManifest)
                replacement=next((r for r in grid.replacements if r.excluded_paper_id==paper_id),None)
                if replacement is not None and replacement.exclusion_receipt!=exclusion_ref:raise ValueError('Excluded paper has different replacement evidence')
                expected=adapt_common_exclusion(receipt,ref,exclusion_ref,store=store,hotkey_by_alias=hotkeys,
                    baseline_alias=current.baseline_alias,challenger_aliases=current.challenger_aliases)
            elif partial:
                expected=adapt_first_round_score(receipt,ref,FileRef.model_validate(silver['first_round_score'],strict=True),
                    scoring_policy=first_round_policy,store=store,hotkey_by_alias=hotkeys,
                    baseline_alias=current.baseline_alias,challenger_aliases=current.challenger_aliases)
            else:
                expected=adapt_scientific_receipt(receipt,ref,store=store,hotkey_by_alias=hotkeys,
                    baseline_alias=current.baseline_alias,challenger_aliases=current.challenger_aliases)
            rows={r['challenger_hotkey']:r for r in expected.pair_rows}
            actual=[r for r in pairs if r['paper_id']==paper_id]
            if len(rows)!=len(actual):raise ValueError('Scientific pair grid is incomplete')
            for row in actual:
                original=rows[row['challenger_hotkey']]
                if any(row.get(k)!=v for k,v in original.items()):
                    raise ValueError('Scientific pair scores/components differ from exact retained evidence')
            evidence.append({'paper_id':paper_id,'silver_receipt_sha256':digest,'revision':receipt.binding.revision,
                'comparison_plan_sha256':sha(canonical(current))})
    return evidence
