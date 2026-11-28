"""Copy accepted weight shares from local heartbeat state to the backend display.

Reads no wallet and makes no chain calls. Run periodically with the existing
controller token. Failed uploads are safe to repeat and never submit weights.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request


def report(state_path: Path, backend_url: str, token: str) -> dict:
    state = json.loads(state_path.read_text())
    saved = state.get('submitted_weights')
    if not saved:
        return {'status': 'no_submitted_weights'}
    if (saved['submitted_at'] != state['last_submitted_at']
        or saved['submitted_block'] != state['last_submitted_block']
        or saved['publication_sha256'] != state['latest_competition_receipt_sha256']):
        raise ValueError('submitted weight snapshot differs from accepted heartbeat state')
    round_id = saved['round_id']
    payload = {key: value for key, value in saved.items() if key != 'round_id'}
    request = urllib.request.Request(
        backend_url.rstrip('/') + '/internal/competition/rounds/'
        + urllib.parse.quote(round_id, safe='') + '/submitted-weights',
        data=json.dumps(payload).encode(), method='POST',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        json.load(response)
    return {'status': 'reported', 'round_id': round_id,
            'submitted_block': saved['submitted_block']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-path', type=Path, required=True)
    parser.add_argument('--backend-url', required=True)
    args = parser.parse_args()
    token = os.environ.get('CLAIMS_COMPETITION_CONTROLLER_TOKEN', '')
    if not token:
        raise ValueError('controller authentication is required')
    print(json.dumps(report(args.state_path, args.backend_url, token)))


if __name__ == '__main__':
    main()
