"""Explicit experimental mainnet activation, never a claim of full qualification."""
from validator.scientific_result_v1.source_tasks.qualification import activation_failures

SCHEMA = 'claims_scientific_experimental_launch_v1'


def pilot_report(assessment):
    return {'pilot': {'eligible_papers': len(assessment['papers']),
                     'distinct_pipeline_digests': len({s['package_sha256'] for s in assessment['scores']})}}


def validate_launch(value, round_id, assessment):
    expected = {'schema_version', 'round_id', 'basis_round_id', 'basis_assessment',
                'unmet_qualification_requirements', 'scientific_accuracy_policy'}
    if (set(value) != expected or value['schema_version'] != SCHEMA or value['round_id'] != round_id
            or value['scientific_accuracy_policy'] != 'unmeasured_without_truth_corpus'
            or assessment.get('kind') != 'claims_testnet_conservative_assessment_v1'
            or assessment.get('round_id') != value['basis_round_id']
            or not value['basis_round_id'].endswith('_testnet')
            or assessment.get('policy', {}).get('coverage_threshold_behavior') != 'advisory'):
        raise ValueError('Experimental launch is not bound to the completed testnet evidence')
    papers = {(p['stage'], p['paper_id']) for p in assessment['papers']}
    hotkeys = {s['hotkey'] for s in assessment['scores']}
    grid = {(s['stage'], s['paper_id'], s['hotkey']) for s in assessment['scores']}
    if (len(papers) < 10 or len(hotkeys) < 3 or len(grid) != len(assessment['scores'])
            or grid != {(stage, pid, hotkey) for stage, pid in papers for hotkey in hotkeys}):
        raise ValueError('Experimental launch requires a completed ten-paper, three-pipeline assessment')
    failures = activation_failures(pilot_report(assessment), truth_corpus_required=False)
    if value['unmet_qualification_requirements'] != failures:
        raise ValueError('Experimental launch must disclose all unmet production requirements')
    return value
