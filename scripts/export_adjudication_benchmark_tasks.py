from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv


CLAIMS_ROOT = Path(__file__).resolve().parents[1]
if str(CLAIMS_ROOT) not in sys.path:
    sys.path.insert(0, str(CLAIMS_ROOT))

from validator.agent_v1.adjudication_models import AdjudicationContextBundle
from validator.agent_v1.comparison_models import BronzeDiffCase, ComparisonCandidate
from validator.agent_v1.file_agent_workflow import build_eligibility_adjudication_task
from validator.agent_v1.record_projection import project_agent_artifact


TABLE_CASES = "claims_dashboard_adjudication_cases"
TABLE_BRONZE = "claims_dashboard_bronze_records"
TABLE_ARTIFACTS = "claims_dashboard_miner_artifacts"


class SupabaseReader:
    def __init__(self, *, url: str, service_role_key: str, timeout_seconds: float = 120.0):
        self.rest_url = f"{url.rstrip('/')}/rest/v1"
        self.headers = {
            "apikey": service_role_key,
            "authorization": f"Bearer {service_role_key}",
        }
        self.timeout_seconds = timeout_seconds

    def select(
        self,
        table: str,
        *,
        filters: dict[str, str],
        columns: str = "*",
        order: str | None = None,
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            query = {
                "select": columns,
                **filters,
                "limit": str(page_size),
                "offset": str(offset),
            }
            if order:
                query["order"] = order
            request = Request(
                f"{self.rest_url}/{table}?{urlencode(query, safe='(),.*')}",
                headers=self.headers,
            )
            with urlopen(request, timeout=self.timeout_seconds) as response:
                page = json.loads(response.read().decode("utf-8"))
            if not isinstance(page, list):
                raise RuntimeError(f"Supabase {table} response was not a list.")
            rows.extend(row for row in page if isinstance(row, dict))
            if len(page) < page_size:
                return rows
            offset += page_size


def main() -> int:
    args = _parse_args()
    load_dotenv(args.backend_env.expanduser().resolve(), override=False)
    reader = SupabaseReader(
        url=_required_env(args.supabase_url_env),
        service_role_key=_required_env(args.service_role_key_env),
        timeout_seconds=args.timeout,
    )
    excluded_papers = _csv(args.exclude_paper_ids)
    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    exported: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for run_id in _csv(args.run_ids):
        if len(exported) >= args.sample_count:
            break
        case_rows = reader.select(
            TABLE_CASES,
            filters={"network": f"eq.{args.network}", "run_id": f"eq.{run_id}"},
            columns=(
                "case_id,run_id,batch_id,paper_id,mismatch_type,candidate_ids,"
                "decision,created_at"
            ),
            order="paper_id.asc,created_at.asc,case_id.asc",
        )
        by_paper: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in case_rows:
            paper_id = str(row.get("paper_id") or "")
            if paper_id and paper_id not in excluded_papers:
                by_paper[paper_id].append(row)

        paper_options = sorted(
            by_paper.items(),
            key=lambda item: (
                -_case_shape_count(item[1], size=2),
                -len(item[1]),
                item[0],
            ),
        )
        for paper_id, paper_cases in paper_options:
            try:
                task, metadata = reconstruct_task(
                    reader,
                    network=args.network,
                    run_id=run_id,
                    paper_id=paper_id,
                    case_rows=paper_cases,
                    cases_per_sample=args.cases_per_sample,
                )
            except Exception as exc:
                failures.append(
                    {
                        "run_id": run_id,
                        "paper_id": paper_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            sample_index = len(exported) + 1
            stage_dir = (
                output_root
                / f"silver_{run_id}_{paper_id}"
                / paper_id
                / "executions"
                / "eligibility_adjudication_negative"
            )
            task_path = stage_dir / "task.json"
            _write_json(task_path, {**task, "judge_role": "negative"})
            exported.append(
                {
                    "sample_id": f"sample_{sample_index:02d}",
                    "run_id": run_id,
                    "paper_id": paper_id,
                    "task_path": str(task_path),
                    **metadata,
                }
            )
            break

    manifest = {
        "schema": "claims_database_adjudication_export_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "network": args.network,
        "requested_sample_count": args.sample_count,
        "cases_per_sample": args.cases_per_sample,
        "samples": exported,
        "failures": failures,
    }
    _write_json(output_root / "export_manifest.json", manifest)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    if len(exported) < args.sample_count:
        raise SystemExit(
            f"Exported only {len(exported)} of {args.sample_count} requested samples."
        )
    return 0


def reconstruct_task(
    reader: SupabaseReader,
    *,
    network: str,
    run_id: str,
    paper_id: str,
    case_rows: list[dict[str, Any]],
    cases_per_sample: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    batch_id = next(
        (str(row.get("batch_id") or "") for row in case_rows if row.get("batch_id")),
        "",
    )
    if not batch_id:
        raise ValueError("comparison cases do not identify a batch")
    bronze_rows = reader.select(
        TABLE_BRONZE,
        filters={
            "network": f"eq.{network}",
            "run_id": f"eq.{run_id}",
            "paper_id": f"eq.{paper_id}",
        },
        columns="bronze_record_id,artifact,source_payload,created_at",
        order="created_at.desc",
    )
    if not bronze_rows:
        bronze_rows = reader.select(
            TABLE_BRONZE,
            filters={"network": f"eq.{network}", "paper_id": f"eq.{paper_id}"},
            columns="bronze_record_id,artifact,source_payload,created_at",
            order="created_at.desc",
        )
    if not bronze_rows:
        raise ValueError("Bronze record is unavailable")
    bronze_row = bronze_rows[0]
    bronze_artifact = _object(bronze_row.get("artifact"))
    if not bronze_artifact:
        raise ValueError("Bronze artifact is empty")

    artifact_rows = reader.select(
        TABLE_ARTIFACTS,
        filters={
            "network": f"eq.{network}",
            "batch_id": f"eq.{batch_id}",
            "paper_id": f"eq.{paper_id}",
        },
        columns="artifact_id,uid,agent_output,source_payload,created_at",
        order="created_at.desc",
    )
    candidates_by_id: dict[str, ComparisonCandidate] = {
        candidate.candidate_id: candidate
        for candidate in project_agent_artifact(bronze_artifact, origin="bronze")
    }
    source_payloads = [_object(bronze_row.get("source_payload"))]
    seen_uids: set[int] = set()
    for artifact_row in artifact_rows:
        uid = artifact_row.get("uid")
        if not isinstance(uid, int) or uid in seen_uids:
            continue
        seen_uids.add(uid)
        artifact = _object(artifact_row.get("agent_output"))
        source_payloads.append(_object(artifact_row.get("source_payload")))
        for candidate in project_agent_artifact(
            artifact,
            origin="miner",
            miner_id=f"uid_{uid}",
        ):
            candidates_by_id[candidate.candidate_id] = candidate

    contexts: list[AdjudicationContextBundle] = []
    unresolved_case_ids: list[str] = []
    for row in case_rows:
        case = _case_from_row(row)
        candidates = [
            candidates_by_id[candidate_id]
            for candidate_id in case.candidate_ids
            if candidate_id in candidates_by_id
        ]
        if len(candidates) != len(case.candidate_ids) or len(candidates) not in {1, 2}:
            unresolved_case_ids.append(case.case_id)
            continue
        contexts.append(
            AdjudicationContextBundle(
                case=case,
                candidates=candidates,
                candidate_order_seed=case.case_id,
            )
        )
    if len(contexts) < cases_per_sample:
        raise ValueError(
            f"only {len(contexts)} resolvable cases; need {cases_per_sample}"
        )
    selected_contexts = _sample_contexts(
        contexts,
        count=cases_per_sample,
        seed=f"{run_id}:{paper_id}",
    )
    source_map = _source_context_map(source_payloads)
    paper_context = _object(bronze_artifact.get("paper"))
    paper_context.setdefault("paper_id", paper_id)
    task, _case_aliases, candidate_aliases = build_eligibility_adjudication_task(
        selected_contexts,
        paper_context=paper_context,
        source_context_by_span_id=source_map,
    )
    missing_span_ids = sorted(
        {
            span_id
            for context in selected_contexts
            for candidate in context.candidates
            for span_id in candidate.source_span_ids
            if span_id not in source_map
        }
    )
    raw = json.dumps(task, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return task, {
        "batch_id": batch_id,
        "bronze_record_id": bronze_row.get("bronze_record_id"),
        "input_bytes": len(raw),
        "case_count": len(selected_contexts),
        "singleton_count": sum(len(context.candidates) == 1 for context in selected_contexts),
        "pair_count": sum(len(context.candidates) == 2 for context in selected_contexts),
        "candidate_count": sum(len(value) for value in candidate_aliases.values()),
        "available_case_count": len(contexts),
        "unresolved_case_count": len(unresolved_case_ids),
        "missing_referenced_span_count": len(missing_span_ids),
    }


def _case_from_row(row: dict[str, Any]) -> BronzeDiffCase:
    decision = _object(row.get("decision"))
    payload = _object(decision.get("case"))
    candidate_ids = _strings(row.get("candidate_ids")) or _strings(payload.get("candidate_ids"))
    if not candidate_ids:
        raise ValueError(f"case {row.get('case_id')} has no candidate IDs")
    original_case_id = str(payload.get("original_case_id") or payload.get("case_id") or row.get("case_id") or "")
    return BronzeDiffCase.model_validate(
        {
            "case_id": original_case_id,
            "paper_id": str(row.get("paper_id") or payload.get("paper_id") or ""),
            "miner_id": str(payload.get("miner_id") or "graph"),
            "mismatch_type": str(row.get("mismatch_type") or payload.get("mismatch_type") or "EXTRA_FROM_MINER"),
            "candidate_ids": candidate_ids,
            "bronze_candidate_id": payload.get("bronze_candidate_id"),
            "miner_candidate_id": payload.get("miner_candidate_id"),
            "question": str(payload.get("question") or "Which candidate satisfies every eligibility gate?"),
            "metadata": _object(payload.get("metadata")),
        }
    )


def _sample_contexts(
    contexts: list[AdjudicationContextBundle],
    *,
    count: int,
    seed: str,
) -> list[AdjudicationContextBundle]:
    ordered = sorted(contexts, key=lambda context: context.case.case_id)
    randomizer = random.Random(hashlib.sha256(seed.encode("utf-8")).hexdigest())
    selected = randomizer.sample(ordered, count)
    return sorted(selected, key=lambda context: context.case.case_id)


def _source_context_map(payloads: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for payload in payloads:
        spans = payload.get("spans") if isinstance(payload.get("spans"), list) else []
        for index, span in enumerate(spans, start=1):
            if not isinstance(span, dict):
                continue
            span_id = str(span.get("span_id") or span.get("id") or f"span_{index}").strip()
            text = str(span.get("text") or span.get("quote") or "").strip()
            if span_id and text:
                result[span_id] = text
            metadata = _object(span.get("metadata"))
            reader_span_id = str(metadata.get("reader_span_id") or "").strip()
            if reader_span_id and text:
                result.setdefault(reader_span_id, text)
    return result


def _case_shape_count(rows: list[dict[str, Any]], *, size: int) -> int:
    return sum(len(_strings(row.get("candidate_ids"))) == size for row in rows)


def _object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required.")
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconstruct adjudication benchmark tasks from production comparison cases."
    )
    parser.add_argument("--backend-env", type=Path, default=CLAIMS_ROOT.parent / "Claims-Backend-Service" / ".env")
    parser.add_argument("--network", default="mainnet")
    parser.add_argument("--run-ids", required=True)
    parser.add_argument("--exclude-paper-ids", default="")
    parser.add_argument("--sample-count", type=int, default=7)
    parser.add_argument("--cases-per-sample", type=int, default=12)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--supabase-url-env", default="SUPABASE_URL")
    parser.add_argument("--service-role-key-env", default="SUPABASE_SERVICE_ROLE_KEY")
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
