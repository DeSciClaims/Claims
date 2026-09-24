from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from typing import Any

from neurons.tasks import download_pdf

from .config import AgentV1Config
from .ingest import document_source_payload, ingest_pdf
from .provider import (
    dspy_model_id,
    normalize_provider,
    provider_api_base,
    provider_api_key_env,
)

_SPACE = re.compile(r"\s+")


def review_consensus_assignment(payload: dict[str, Any]) -> dict[str, Any]:
    cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    if not cases:
        raise ValueError("consensus assignment contains no cases")
    config = AgentV1Config.from_env()
    provider, model, api_base, api_key_env = _consensus_lm_settings(config)
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise RuntimeError(f"{api_key_env} is required for model-backed consensus review")
    try:
        import dspy
    except ImportError as exc:  # pragma: no cover - deployment dependency guard.
        raise RuntimeError("dspy is required for model-backed consensus review") from exc

    lm = dspy.LM(
        model=dspy_model_id(model, provider=provider, api_base=api_base),
        api_key=api_key,
        api_base=api_base,
        temperature=0.0,
        max_tokens=int(os.getenv("SUBNET_CLAIMS_CONSENSUS_MAX_TOKENS", "8192")),
        timeout=int(os.getenv("SUBNET_CLAIMS_CONSENSUS_TIMEOUT", str(config.timeout_seconds))),
        cache=False,
    )

    class ConsensusReviewSignature(dspy.Signature):
        """Review every case independently against its supplied evidence and return strict JSON.

        Select exactly one listed option for every item by checking the supplied source payload.
        Prefer source-faithful, directly supported claims. Reject unsupported numerical,
        directional, population, intervention, and outcome changes. Return 1-4 verbatim source
        quotes for every choice. Each quote must be copied exactly from the text of a supplied
        source span, apart from whitespace. Do not use the same choice mechanically across cases.
        """

        assignment_json: str = dspy.InputField()
        responses_json: str = dspy.OutputField(
            desc=(
                "JSON array with one object per item: item_id, selected_option, confidence, rationale. "
                "Each object must also contain evidence_items, an array of 1-4 objects with "
                "evidence_id, paper_id, quote, page (when known), and local_span_id. Use only "
                "options listed for that item."
            )
        )

    predictor = dspy.Predict(ConsensusReviewSignature)
    responses: list[dict[str, Any]] = []
    for source_document, source_cases in _cases_by_source_document(cases):
        source_payload = _extract_source_payload(source_document, config)
        request_context = {
            "schema": str(payload.get("schema") or "claims_consensus_assignment_v1"),
            "round_id": str(payload.get("round_id") or ""),
            "source_document": source_document,
            "source_payload": source_payload,
        }
        responses.extend(
            _review_case_batch(
                dspy_module=dspy,
                lm=lm,
                predictor=predictor,
                request_context=request_context,
                cases=source_cases,
                source_payload=source_payload,
                paper_id=str(source_document.get("paper_id") or ""),
            )
        )
    response_by_item = {response["item_id"]: response for response in responses}
    return {
        "schema": "claims_miner_consensus_round_response_v1",
        "round_id": str(payload.get("round_id") or ""),
        "responses": [response_by_item[str(case.get("item_id") or "")] for case in cases],
    }


def _review_case_batch(
    *,
    dspy_module: Any,
    lm: Any,
    predictor: Any,
    request_context: dict[str, Any],
    cases: list[dict[str, Any]],
    source_payload: dict[str, Any],
    paper_id: str,
    retry_feedback: str = "",
) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for _attempt in range(2):
        request = {**request_context, "cases": cases}
        if retry_feedback:
            request["retry_feedback"] = retry_feedback
            request["retry_instruction"] = (
                "Correct every listed validation error. Return all requested item IDs exactly once, "
                "choose only a listed option, and copy each evidence quote verbatim from a source span."
            )
        try:
            if hasattr(dspy_module, "context"):
                with dspy_module.context(lm=lm):
                    result = predictor(assignment_json=json.dumps(request, ensure_ascii=False))
            else:  # pragma: no cover - older DSPy compatibility.
                dspy_module.configure(lm=lm)
                result = predictor(assignment_json=json.dumps(request, ensure_ascii=False))
            return _parse_responses(
                getattr(result, "responses_json", result),
                cases,
                source_payload=source_payload,
                paper_id=paper_id,
            )
        except Exception as exc:  # noqa: BLE001 - provider and parser failures are retried uniformly.
            last_error = exc
            retry_feedback = str(exc)

    if len(cases) > 1:
        midpoint = len(cases) // 2
        return _review_case_batch(
            dspy_module=dspy_module,
            lm=lm,
            predictor=predictor,
            request_context=request_context,
            cases=cases[:midpoint],
            source_payload=source_payload,
            paper_id=paper_id,
            retry_feedback=str(last_error or ""),
        ) + _review_case_batch(
            dspy_module=dspy_module,
            lm=lm,
            predictor=predictor,
            request_context=request_context,
            cases=cases[midpoint:],
            source_payload=source_payload,
            paper_id=paper_id,
            retry_feedback=str(last_error or ""),
        )

    abstention = _abstention_response(
        cases[0] if cases else {},
        source_payload=source_payload,
        paper_id=paper_id,
        reason=str(last_error or "model review failed"),
    )
    if abstention is not None:
        return [abstention]
    item_id = str(cases[0].get("item_id") or "") if cases else ""
    raise RuntimeError(
        f"consensus reviewer failed to return a complete valid response for item {item_id}: {last_error}"
    )


def _abstention_response(
    case: dict[str, Any],
    *,
    source_payload: dict[str, Any],
    paper_id: str,
    reason: str,
) -> dict[str, Any] | None:
    options = {str(option) for option in case.get("options", []) if str(option).strip()}
    if "insufficient_information" not in options:
        return None
    span = next(
        (
            item
            for item in source_payload.get("spans", [])
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ),
        None,
    )
    if span is None:
        return None
    quote = str(span.get("text") or "").strip()[:500].strip()
    if not quote:
        return None
    return {
        "item_id": str(case.get("item_id") or ""),
        "selected_option": "insufficient_information",
        "confidence": 0.0,
        "rationale": f"Automated review abstained after invalid model output: {reason[:240]}",
        "evidence_items": [
            {
                "evidence_id": str(span.get("span_id") or "source-span"),
                "paper_id": str(span.get("paper_id") or paper_id),
                "quote": quote,
                "page": span.get("page"),
                "local_span_id": str(span.get("span_id") or ""),
            }
        ],
    }


def _consensus_lm_settings(config: AgentV1Config) -> tuple[str, str, str, str]:
    provider = normalize_provider(
        os.getenv("SUBNET_CLAIMS_CONSENSUS_PROVIDER") or config.provider,
        api_base=os.getenv("SUBNET_CLAIMS_CONSENSUS_API_BASE", ""),
    )
    model = (
        os.getenv("SUBNET_CLAIMS_CONSENSUS_MODEL")
        or config.model
    ).strip()
    configured_api_base = os.getenv("SUBNET_CLAIMS_CONSENSUS_API_BASE")
    if not configured_api_base and provider == config.provider:
        configured_api_base = config.api_base
    api_base = provider_api_base(provider, configured_api_base)
    api_key_env = provider_api_key_env(
        provider,
        os.getenv("SUBNET_CLAIMS_CONSENSUS_API_KEY_ENV"),
    )
    return provider, model, api_base, api_key_env


def _parse_responses(
    raw: Any,
    cases: list[dict[str, Any]],
    *,
    source_payload: dict[str, Any] | None = None,
    paper_id: str = "",
) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:].lstrip()
        parsed = json.loads(text)
    else:
        parsed = raw
    if isinstance(parsed, dict):
        parsed = parsed.get("responses")
    if not isinstance(parsed, list):
        raise ValueError("responses_json must be a JSON array")  # noqa: TRY004
    expected = {
        str(case.get("item_id") or ""): {
            str(option) for option in case.get("options", []) if str(option).strip()
        }
        for case in cases
    }
    normalized: dict[str, dict[str, Any]] = {}
    invalid_reasons: dict[str, str] = {}
    for response in parsed:
        if not isinstance(response, dict):
            continue
        item_id = str(response.get("item_id") or "").strip()
        selected = str(response.get("selected_option") or "").strip()
        if item_id not in expected:
            continue
        if selected not in expected[item_id]:
            invalid_reasons[item_id] = f"selected_option {selected!r} is not listed"
            continue
        if item_id in normalized:
            invalid_reasons[item_id] = "item_id was returned more than once"
            continue
        raw_evidence = response.get("evidence_items")
        if not isinstance(raw_evidence, list):
            raw_evidence = response.get("evidence")
        evidence = [item for item in (raw_evidence or []) if isinstance(item, dict)]
        if source_payload is not None:
            if not 1 <= len(evidence) <= 4:
                invalid_reasons[item_id] = "evidence_items must contain between 1 and 4 entries"
                continue
            valid_evidence = [
                item
                for item in evidence
                if _local_evidence_error([item], source_payload, paper_id=paper_id) is None
            ]
            if not valid_evidence:
                invalid_reasons[item_id] = _local_evidence_error(
                    evidence[:1],
                    source_payload,
                    paper_id=paper_id,
                ) or "no evidence item matches a source span"
                continue
            evidence = valid_evidence
        normalized[item_id] = {
            "item_id": item_id,
            "selected_option": selected,
            "confidence": _normalize_confidence(response.get("confidence")),
            "rationale": str(response.get("rationale") or "").strip(),
            "evidence_items": evidence,
        }
    missing = [item_id for item_id in expected if item_id not in normalized]
    if missing:
        details = [f"{item_id}: {invalid_reasons.get(item_id, 'not returned')}" for item_id in missing]
        raise ValueError(f"consensus response omitted items or returned invalid items: {details}")
    return [normalized[str(case["item_id"])] for case in cases]


def _cases_by_source_document(
    cases: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    grouped: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for case in cases:
        case_payload = case.get("case") if isinstance(case.get("case"), dict) else {}
        source_document = case_payload.get("source_document")
        if not isinstance(source_document, dict):
            raise ValueError(f"consensus item {case.get('item_id')} has no source_document")  # noqa: TRY004
        paper_id = str(source_document.get("paper_id") or "").strip()
        source_url = str(source_document.get("source_url") or "").strip()
        if not paper_id or not source_url:
            raise ValueError(f"consensus item {case.get('item_id')} has an incomplete source_document")
        key = f"{paper_id}:{source_url}"
        if key not in grouped:
            grouped[key] = (source_document, [])
        grouped[key][1].append(case)
    return list(grouped.values())


def _extract_source_payload(source_document: dict[str, Any], config: AgentV1Config) -> dict[str, Any]:
    source_url = str(source_document.get("source_url") or "")
    paper_id = str(source_document.get("paper_id") or "")
    cache_key = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:24]
    download = download_pdf(
        source_url,
        output_dir=config.cache_dir / "consensus_sources" / cache_key,
        expected_sha256=str(source_document.get("source_sha256") or ""),
    )
    document = ingest_pdf(
        download.path,
        max_chars=None,
        reader=config.pdf_reader,
        grobid_url=config.grobid_url,
        grobid_cache_dir=config.cache_dir / "grobid",
        grobid_timeout_s=config.grobid_timeout_s,
        grobid_retries=config.grobid_retries,
        grobid_retry_wait_s=config.grobid_retry_wait_s,
    )
    document.paper.paper_id = paper_id
    for span in document.spans:
        span.paper_id = paper_id
    return document_source_payload(document, max_chars=None)


def _valid_local_evidence(
    evidence: list[dict[str, Any]],
    source_payload: dict[str, Any],
    *,
    paper_id: str,
) -> bool:
    return _local_evidence_error(evidence, source_payload, paper_id=paper_id) is None


def _local_evidence_error(
    evidence: list[dict[str, Any]],
    source_payload: dict[str, Any],
    *,
    paper_id: str,
) -> str | None:
    if not 1 <= len(evidence) <= 4:
        return "evidence_items must contain between 1 and 4 entries"
    source_spans = [
        _normalize_text(str(span.get("text") or ""))
        for span in source_payload.get("spans", [])
        if isinstance(span, dict)
    ]
    for item in evidence:
        if str(item.get("paper_id") or paper_id) != paper_id:
            return "evidence paper_id does not match the assigned paper"
        quote = _normalize_text(str(item.get("quote") or ""))
        if len(quote) < 8:
            return "evidence quote is shorter than 8 characters"
        if not any(quote in span for span in source_spans):
            return "evidence quote is not a verbatim substring of any source span"
    return None


def _normalize_text(value: str) -> str:
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()


def _normalize_confidence(value: Any) -> float:
    labels = {
        "high": 0.9,
        "medium": 0.6,
        "moderate": 0.6,
        "low": 0.3,
    }
    if isinstance(value, str) and value.strip().lower() in labels:
        return labels[value.strip().lower()]
    try:
        return max(0.0, min(1.0, float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0
