"""Keep review-only appeals independent of competition publication.

The backend freezes an immutable snapshot of already resolved appeals for the
signed receipt. Pending and later appeals remain in the normal public ledger.
This adapter leaves the frozen competition scoring implementation unchanged.
"""
from competition.intake_lock import CompetitionBackend


class NonBlockingAppealsBackend(CompetitionBackend):
    def list_duplicate_disputes(self, round_id: str) -> list[dict]:
        snapshot = self.runtime_request(round_id, '/duplicate-disputes/publication-snapshot', {})
        rows = snapshot.get('disputes')
        if (snapshot.get('schema_version') != 'claims_duplicate_appeal_snapshot_v1'
            or snapshot.get('round_id') != round_id or not isinstance(rows, list)
            or any(row.get('status') not in {'upheld', 'overturned'} for row in rows)):
            raise ValueError('backend returned an invalid duplicate appeal snapshot')
        return rows
