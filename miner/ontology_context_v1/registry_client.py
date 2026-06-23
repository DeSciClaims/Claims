from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class OntologyCandidateRow:
    ontology_source: str
    ontology_id: str
    ontology_label: str
    alias_text: str
    alias_type: str


class OntologyRegistryClient:
    def __init__(self, *, supabase_url: str, supabase_key: str) -> None:
        self.base_url = supabase_url.rstrip("/") + "/rest/v1"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
            }
        )
        self._term_cache: dict[tuple[str, str], dict[str, Any] | None] = {}

    def select(
        self,
        *,
        table_name: str,
        columns: str,
        filters: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"select": columns}
        if filters:
            params.update(filters)
        if limit is not None:
            params["limit"] = limit
        response = self.session.get(f"{self.base_url}/{table_name}", params=params, timeout=120)
        _raise_for_status(response, action=f"select from `{table_name}`")
        return response.json() or []

    def fetch_alias_candidates_exact(
        self,
        *,
        ontology_source: str,
        normalized_text: str,
        limit: int,
    ) -> list[OntologyCandidateRow]:
        rows = self.select(
            table_name="ontology_current_aliases",
            columns="ontology_source,ontology_id,alias_text,alias_type",
            filters={
                "ontology_source": f"eq.{ontology_source}",
                "normalized_alias_text": f"eq.{normalized_text}",
            },
            limit=limit,
        )
        return self._with_labels(rows)

    def fetch_alias_candidates_fuzzy(
        self,
        *,
        ontology_source: str,
        normalized_text: str,
        limit: int,
    ) -> list[OntologyCandidateRow]:
        pattern = f"*{normalized_text}*"
        rows = self.select(
            table_name="ontology_current_aliases",
            columns="ontology_source,ontology_id,alias_text,alias_type",
            filters={
                "ontology_source": f"eq.{ontology_source}",
                "normalized_alias_text": f"ilike.{pattern}",
            },
            limit=limit,
        )
        return self._with_labels(rows)

    def fetch_term(self, *, ontology_source: str, ontology_id: str) -> dict[str, Any] | None:
        cache_key = (ontology_source, ontology_id)
        if cache_key in self._term_cache:
            return self._term_cache[cache_key]
        rows = self.select(
            table_name="ontology_current_terms",
            columns="ontology_source,ontology_id,label,definition,is_obsolete",
            filters={
                "ontology_source": f"eq.{ontology_source}",
                "ontology_id": f"eq.{ontology_id}",
            },
            limit=1,
        )
        term = rows[0] if rows else None
        self._term_cache[cache_key] = term
        return term

    def _with_labels(self, rows: list[dict[str, Any]]) -> list[OntologyCandidateRow]:
        output: list[OntologyCandidateRow] = []
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            ontology_source = str(row.get("ontology_source") or "").strip()
            ontology_id = str(row.get("ontology_id") or "").strip()
            alias_text = str(row.get("alias_text") or "").strip()
            alias_type = str(row.get("alias_type") or "").strip()
            if not ontology_source or not ontology_id:
                continue
            key = (ontology_source, ontology_id, alias_text)
            if key in seen:
                continue
            seen.add(key)
            term = self.fetch_term(ontology_source=ontology_source, ontology_id=ontology_id)
            ontology_label = str((term or {}).get("label") or alias_text).strip()
            output.append(
                OntologyCandidateRow(
                    ontology_source=ontology_source,
                    ontology_id=ontology_id,
                    ontology_label=ontology_label,
                    alias_text=alias_text,
                    alias_type=alias_type,
                )
            )
        return output


def normalize_ontology_text(text: str) -> str:
    normalized = text.strip().lower()
    normalized = re.sub(r"[_/\\-]+", " ", normalized)
    normalized = re.sub(r"[^\w\s:]", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _raise_for_status(response: requests.Response, *, action: str) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body_preview = (response.text or "").strip()
        if len(body_preview) > 400:
            body_preview = body_preview[:400] + "..."
        raise SystemExit(
            f"Ontology registry request failed during {action}: HTTP {response.status_code}\n"
            f"Response body: {body_preview or '<empty>'}"
        ) from exc
