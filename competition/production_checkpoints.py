"""Private atomic snapshots of completed production work, before key cleanup."""
import json
import re
from .production_handoff import FilesystemProductionReleaseStore


class ProductionExecutionCheckpoints(FilesystemProductionReleaseStore):
    max_bytes = 16_000_000

    def _path(self, job_id, attempt):
        if not re.fullmatch(r'[0-9a-f]{64}', job_id) or type(attempt) is not int or attempt not in {1, 2}:
            raise ValueError('invalid production checkpoint identity')
        return self.root / f'{job_id}-attempt-{attempt}.json'

    def save(self, job_id, attempt, record, *, redact):
        data = json.dumps(record, separators=(',', ':'), allow_nan=False)
        if redact:
            data = data.replace(redact, '[REDACTED_PAPER_KEY]')
        content = data.encode()
        if len(content) > self.max_bytes:
            raise ValueError('production checkpoint exceeds output limit')
        self._write_atomic(self._path(job_id, attempt), content)

    def load(self, job_id, attempt):
        path = self._path(job_id, attempt)
        if path.is_symlink() or path.stat().st_size > self.max_bytes:
            raise ValueError('invalid production checkpoint file')
        return json.loads(path.read_bytes())

    def remove(self, job_id, attempt):
        self._path(job_id, attempt).unlink(missing_ok=True)

    def find(self, job_id):
        candidates = []
        for attempt in (1, 2):
            path = self._path(job_id, attempt)
            if path.exists():
                candidates.append((path.stat().st_mtime_ns, self.load(job_id, attempt)))
        return max(candidates, key=lambda pair: pair[0])[1] if candidates else None


def checkpoint_matches(record, active, lease, network):
    return isinstance(record, dict) and all(record.get(field) == value for field, value in {
        'schema_version': 'claims_production_checkpoint_v1', 'network': network,
        'job_id': lease['job_id'], 'paper_id': lease['paper_id'],
        'source_sha256': lease['source_sha256'], 'round_id': active.round_id,
        'receipt_sha256': active.receipt_sha256, 'package_sha256': active.package_sha256,
    }.items())


def usage_already_recorded(lease, key_id):
    if not key_id:
        return False
    prior = lease.get('result_private') or {}
    if not isinstance(prior, dict):
        return False
    return any(isinstance(row, dict) and row.get('usage_virtual_key_id') == key_id
               and type(row.get('inference_cost_usd')) in {int, float}
               for row in [prior, *prior.get('prior_attempts', [])])
