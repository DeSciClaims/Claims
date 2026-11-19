from dataclasses import replace
from types import SimpleNamespace

import pytest

from competition.production_checkpoints import ProductionExecutionCheckpoints
from competition.production_runner import run_production_paper
from competition.production_worker_service import ProductionWorkerCoordinator
from test_competition_production_runner import _active, _lease, PDF, TOKEN, Keys, Execution, Logs


POLICY = {'inference_base_url': 'https://gateway.example/v1', 'allowed_models': ['example/model'],
          'published_token_ceiling': 1000, 'dollar_limit_usd': 1.0,
          'lease_seconds': 600, 'max_inference_output_tokens': 8192}


class Backend:
    def __init__(self):
        self.saved = []

    def complete_production_job(self, job_id, **kw):
        self.saved.append(kw['result'])
        return {'completed': True}


def coordinator(tmp_path, keys=None):
    return ProductionWorkerCoordinator(
        network='testnet', worker_id='worker', production_start_at='2026-01-01T00:00:00+00:00',
        backend=Backend(), reader=None, rules_for_release=lambda _: POLICY,
        pdf_fetcher=SimpleNamespace(fetch=lambda *a: PDF), keys=keys or Keys(), execution=Execution(),
        logs=Logs(), checkpoints=ProductionExecutionCheckpoints(tmp_path / 'checkpoints'))


def test_cleanup_failure_is_saved_then_recovered_without_model_replay_or_double_billing(tmp_path):
    class BrokenCleanup(Keys):
        def revoke(self, key):
            raise RuntimeError('private gateway message')
    worker = coordinator(tmp_path, BrokenCleanup())
    active = _active()
    lease = _lease(active)
    fault = worker._run_claimed(active, POLICY, lease)
    assert fault['status'] == 'infrastructure_fault'
    assert fault['inference_cost_usd'] == 0.12
    assert fault['prompt_and_completion_tokens'] == 350
    assert 'private gateway' not in str(fault)
    assert worker.execution.calls == 1
    saved = worker.checkpoints.find(lease['job_id'])
    assert saved['phase'] == 'accounted'
    path = worker.checkpoints._path(lease['job_id'], 1)
    assert path.stat().st_mode & 0o777 == 0o600
    assert TOKEN not in path.read_text()
    retry = {**lease, 'attempts': 2, 'result_private': {**fault, 'billing_observed': True}}
    healthy_keys = Keys()
    worker = replace(worker, keys=healthy_keys,
                     pdf_fetcher=SimpleNamespace(fetch=lambda *a: pytest.fail('must not refetch PDF')))
    result = worker._run_claimed(active, POLICY, retry)
    assert result['status'] == 'completed'
    assert result['artifact']['paper']['paper_id'] == 'paper-1'
    assert TOKEN.encode() not in worker.logs.receipt[1]
    assert result['inference_cost_usd'] == 0 and result['prompt_and_completion_tokens'] == 0
    assert not healthy_keys.created and healthy_keys.revoked
    assert worker.execution.calls == 1
    assert worker.checkpoints.find(lease['job_id']) is None


def test_unknown_usage_never_replays_paid_work(tmp_path):
    class MissingUsage(Keys):
        def check_budget(self, key):
            raise RuntimeError('usage unavailable')
    worker = coordinator(tmp_path, MissingUsage())
    lease = _lease(_active())
    first = worker._run_claimed(_active(), POLICY, lease)
    assert first['inference_cost_usd'] is None
    assert worker.checkpoints.find(lease['job_id'])['phase'] == 'worker_finished'
    second = worker._run_claimed(_active(), POLICY,
                                 {**lease, 'attempts': 2, 'result_private': first})
    assert second['status'] == 'infrastructure_fault'
    assert second['inference_cost_usd'] is None
    assert worker.execution.calls == 1


def test_operator_retry_keeps_previous_spend_in_remaining_allowance(tmp_path):
    worker = coordinator(tmp_path, Keys(expected_tokens=650, expected_dollars=0.88))
    lease = {**_lease(_active()), 'result_private': {'prior_attempts': [
        {'status': 'infrastructure_fault', 'billing_observed': True,
         'prompt_and_completion_tokens': 350, 'inference_cost_usd': 0.12}]}}
    result = worker._run_claimed(_active(), POLICY, lease)
    assert result['status'] == 'completed'
    assert worker.execution.request['inference']['budget']['remaining_tokens'] == 650
    assert worker.execution.request['inference']['budget']['remaining_dollar_limit_usd'] == 0.88


def test_one_paper_control_failure_does_not_stop_later_admissions(tmp_path, monkeypatch):
    from test_competition_production_worker_service import batch_worker
    worker = batch_worker(size=8)
    worker.backend.jobs = [{**_lease(_active()), 'job_id': f'{i:064x}', 'paper_id': f'paper-{i}'}
                           for i in range(8)]
    worker.backend.complete_production_job = lambda *a, **kw: {'completed': True}
    worker = replace(worker, logs=Logs())
    seen = []
    def run(active, lease, **kw):
        seen.append(lease['paper_id'])
        if lease['paper_id'] == 'paper-0':
            raise RuntimeError('one paper error')
        return {'status': 'completed'}
    monkeypatch.setattr('competition.production_worker_service.run_production_paper', run)
    rows = worker.run_available()
    assert len(rows) == len(seen) == 8
    assert sum(row['status'] == 'infrastructure_fault' for row in rows) == 1
    assert sum(row['status'] == 'completed' for row in rows) == 7


def test_checkpoint_identity_mismatch_cannot_publish_another_papers_artifact(tmp_path):
    worker = coordinator(tmp_path)
    lease = _lease(_active())
    worker.checkpoints.save(lease['job_id'], 1, {'phase': 'accounted', 'paper_id': 'other'}, redact='')
    result = worker._run_claimed(_active(), POLICY, lease)
    assert result['status'] == 'infrastructure_fault' and result['artifact'] is None
    assert worker.execution.calls == 0 and not worker.keys.created


def test_reset_with_unknown_historical_usage_does_not_issue_another_key(tmp_path):
    worker = coordinator(tmp_path)
    lease = {**_lease(_active()), 'result_private': {'prior_attempts': [
        {'status': 'infrastructure_fault', 'billing_observed': True,
         'prompt_and_completion_tokens': None, 'inference_cost_usd': None}]}}
    result = worker._run_claimed(_active(), POLICY, lease)
    assert result['status'] == 'infrastructure_fault'
    assert result['detail'] == 'unmetered_prior_attempt'
    assert worker.execution.calls == 0 and not worker.keys.created
