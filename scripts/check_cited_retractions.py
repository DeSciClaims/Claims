"""Optional lookup: does an extracted paper cite work that has a retraction notice?

Run this after extraction, on a machine with network access. It is not part of a
miner or validator pipeline, and it must not be called from the sandboxed
competition miner, which has no network. It changes no claim, score or artifact.

It reads only `paper.doi` or `paper.pmid` from artifact.json and asks the free
SaneCite API which of the paper's references carry a notice (retraction,
expression of concern or reinstatement). Notice data comes from the Retraction
Watch database via Crossref. By default it only prints. With --write it adds one
file, cited_retractions.json, beside artifact.json.

A listed reference is not a finding against the paper: the citing paper may have
been published before the notice (see `timing`) or may cite the work to criticize
it. Where SaneCite's census has read a citation, the entry also carries its
`reading`. Those readings are by AI, pending human review.

Usage:
  python scripts/check_cited_retractions.py path/to/artifact.json
  python scripts/check_cited_retractions.py path/to/run_folder --write
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://sanecite.com/api/check"
OUTPUT_NAME = "cited_retractions.json"
DELAY_SECONDS = 3.1  # The API allows 20 calls a minute.
TIMEOUT_SECONDS = 30
MAX_RATE_LIMIT_WAIT = 90


def identifiers(paper: dict) -> list[str]:
    """The paper's IDs, best first."""
    values = (str(paper.get("doi") or "").strip(), str(paper.get("pmid") or "").strip())
    return [value for value in values if value]


def fetch(identifier: str, api: str, sleep=time.sleep) -> dict | None:
    """The API's answer for one ID, or None if it doesn't know the ID."""
    url = f"{api}?{urllib.parse.urlencode({'q': identifier})}"
    request = urllib.request.Request(url, headers={"User-Agent": "claims-check-cited-retractions"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                answer = json.load(response)
            return answer if answer.get("ok") else None
        except urllib.error.HTTPError as err:
            if err.code == 429:
                if attempt == 1:
                    break
                try:
                    wait = int(err.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 60
                sleep(min(max(wait, 1), MAX_RATE_LIMIT_WAIT))
                continue
            if 400 <= err.code < 500:
                return None
            raise
    raise RuntimeError("still rate limited after waiting")


def check(paper: dict, api: str = API, sleep=time.sleep) -> dict | None:
    """The record for one paper, or None if the API knows none of its IDs."""
    for identifier in identifiers(paper):
        answer = fetch(identifier, api, sleep)
        if answer is None:
            continue
        return {
            "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "queried": identifier,
            "api": api,
            "data": answer.get("data"),
            "refs_total": answer.get("refs_total"),
            "refs_checked": answer.get("refs_checked"),
            "paper_own_notice": (answer.get("paper") or {}).get("own"),
            "flagged": answer.get("flagged") or [],
        }
    return None


def artifact_paths(targets: list[str]) -> list[Path]:
    paths: list[Path] = []
    for target in map(Path, targets):
        paths.extend(sorted(target.rglob("artifact.json")) if target.is_dir() else [target])
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("targets", nargs="+", help="artifact.json files, or folders to search for them")
    parser.add_argument("--write", action="store_true", help=f"Write {OUTPUT_NAME} beside each artifact.json")
    parser.add_argument("--api", default=API, help=argparse.SUPPRESS)
    parser.add_argument("--delay", type=float, default=DELAY_SECONDS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    counts = {"checked": 0, "with_flags": 0, "unknown": 0, "errors": 0}
    for index, path in enumerate(artifact_paths(args.targets)):
        if index:
            time.sleep(args.delay)
        try:
            paper = json.loads(path.read_text(encoding="utf-8"))["paper"]
            record = check(paper, args.api)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as err:
            counts["errors"] += 1
            print(f"{path.parent}: {err}", file=sys.stderr)
            continue
        if record is None:
            counts["unknown"] += 1
            print(f"{path.parent}: not found")
            continue
        counts["checked"] += 1
        counts["with_flags"] += bool(record["flagged"])
        print(f"{path.parent}: {len(record['flagged'])} flagged of {record['refs_checked']} references checked")
        if args.write:
            output = path.parent / OUTPUT_NAME
            output.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        else:
            for reference in record["flagged"]:
                print(f"  {reference.get('status')}: {reference.get('doi') or reference.get('pmid')}")

    print(json.dumps(counts))
    return 1 if counts["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
