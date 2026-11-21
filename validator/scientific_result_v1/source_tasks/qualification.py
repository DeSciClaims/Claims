"""Qualification metrics and activation checks; never manufactures scientific truth."""
import math
import json
from .packages import SCHEMA_PATH


def wilson(successes,total):
    if total==0:return None
    z=1.959963984540054;p=successes/total;den=1+z*z/total
    center=(p+z*z/(2*total))/den;radius=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/den
    return [max(0.0,center-radius),min(1.0,center+radius)]


def accuracy(cases,admissible_pairs):
    ids=[c['case_id'] for c in cases]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate truth case')
    if any(type(c['positive']) is not bool or c['outcome'] not in {'PASS','FAIL','HOLD'} for c in cases):raise ValueError('Invalid truth/outcome')
    byid={c['case_id']:c for c in cases}
    positives=[c for c in cases if c['positive']];negatives=[c for c in cases if not c['positive']]
    counts={'positive':len(positives),'negative':len(negatives),
        'negative_pass':sum(c['outcome']=='PASS' for c in negatives),
        'positive_fail_or_hold':sum(c['outcome']!='PASS' for c in positives),
        'hold':sum(c['outcome']=='HOLD' for c in cases),
        'admissible_pairs':len(admissible_pairs),'admissible_both_pass':sum(all(byid[k]['outcome']=='PASS' for k in pair) for pair in admissible_pairs)}
    def metric(name,n,d):return {'name':name,'numerator':n,'denominator':d,'wilson_95':wilson(n,d)}
    metrics=[metric('negative_pass_rate',counts['negative_pass'],len(negatives)),
             metric('positive_fail_or_hold_rate',counts['positive_fail_or_hold'],len(positives)),
             metric('hold_rate',counts['hold'],len(cases)),
             metric('admissible_pair_both_pass_rate',counts['admissible_both_pass'],len(admissible_pairs))]
    schema=json.loads(SCHEMA_PATH.read_text());labels=set(schema['x-controlled_vocabularies']['object_labels'])
    relations={r['code'] for r in schema['x-controlled_vocabularies']['relationship_labels']}
    annotation_kinds={'footnote','unit','statistical_convention','panel','merged_cell','missing_value','structural_blank','transcription','other'}
    covered=lambda name,code,truth:sum(code in c.get(name,[]) and c['positive']==truth for c in cases)
    coverage=all(covered('labels',l,True)>=2 and covered('labels',l,False)>=2 for l in labels) and all(covered('relations',r,True)>=2 and covered('relations',r,False)>=2 for r in relations)
    coverage=coverage and annotation_kinds<={a for c in cases for a in c.get('annotation_kinds',[])}
    material=sum(not c['positive'] and c.get('material_relation_or_grouping_error',False) for c in cases)
    ready=(len(positives)>=200 and len(negatives)>=200 and len(admissible_pairs)>=30 and material>=30 and coverage
        and counts['negative_pass']*50<=len(negatives) and counts['positive_fail_or_hold']*20<=len(positives)
        and counts['hold']*20<=len(cases) and counts['admissible_both_pass']*20>=19*len(admissible_pairs))
    return {'counts':counts,'metrics':metrics,'coverage_complete':coverage,'qualified':ready}


def activation_failures(report, *, truth_corpus_required=False):
    """Require engineering/pilot evidence. The owner waived the unavailable truth corpus.

    A waiver never establishes judge accuracy; retain that status in the frozen policy.
    """
    failures=[]
    if set(report.get('passed_conformance_cases',[]))!={f'E{i:02d}' for i in range(1,51)}:failures.append('E01–E50 conformance incomplete')
    if truth_corpus_required and report.get('scientific_accuracy',{}).get('qualified') is not True:failures.append('Scientific qualification thresholds not met')
    pilot=report.get('pilot',{});papers=pilot.get('eligible_papers',0)
    if papers<50 or pilot.get('distinct_pipeline_digests',0)<3:failures.append('50-paper, three-pipeline operational pilot incomplete')
    if any(pilot.get('category_counts',{}).get(k,0)<10 for k in ('multi_table','supplementary','equation_or_complex_context','scanned_or_continued')):failures.append('Pilot source-category coverage incomplete')
    if pilot.get('initial_generations_within_1800_seconds',0)*20<19*papers or papers==0:failures.append('95% post-seal deadline target not met')
    if any(pilot.get(k)!=0 for k in ('silent_truncations','inconsistent_grids','prohibited_penalties','raw_output_mutations')):failures.append('Pilot integrity checks incomplete or failed')
    for key in ('offline_grid_replay_passed','correction_and_failure_recovery_passed','publication_lineage_passed','p95_cost_plus_20_percent_funded'):
        if report.get(key) is not True:failures.append(key)
    if report.get('sampling_diagnostic',{}).get('complete_paired_papers',0)!=10 or report.get('sampling_diagnostic',{}).get('resamples')!=2000:failures.append('Ten-paper P census/resampling diagnostic incomplete')
    return failures
