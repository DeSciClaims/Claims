from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from miner.agent_v1.skillpack import SkillPack


@dataclass(frozen=True)
class RigorToolSpec:
    name: str
    description: str
    func: Callable[..., str]


class RigorToolbox:
    def __init__(self, *, run_dir: Path, skill_pack: SkillPack) -> None:
        self.run_dir = run_dir.resolve()
        self.skill_pack = skill_pack

    def specs(self) -> list[RigorToolSpec]:
        return [
            RigorToolSpec("read_artifact", "Read the Claims agent artifact under review.", self.read_artifact),
            RigorToolSpec("read_source_payload", "Read source spans available to the miner.", self.read_source_payload),
            RigorToolSpec("read_structural_findings", "Read deterministic structural findings.", self.read_structural_findings),
            RigorToolSpec("read_grounding_findings", "Read deterministic grounding findings.", self.read_grounding_findings),
            RigorToolSpec("read_run_file", "Read a UTF-8 file inside the validator run directory.", self.read_run_file),
            RigorToolSpec("write_run_file", "Write a UTF-8 file inside the validator run directory.", self.write_run_file),
            RigorToolSpec("list_run_files", "List files inside the validator run directory.", self.list_run_files),
            RigorToolSpec("search_source_text", "Search source span text for a case-insensitive query.", self.search_source_text),
            RigorToolSpec("read_skill_resource", "Read a mounted validator skill resource by relative path.", self.read_skill_resource),
            RigorToolSpec("read_output_schema", "Read the JSON Schema for rigor_findings.json.", self.read_output_schema),
            RigorToolSpec("submit_rigor_findings", "Validate and write the final rigor_findings.json file.", self.submit_rigor_findings),
        ]

    def read_artifact(self) -> str:
        return self._read_file("agent_output.json")

    def read_source_payload(self) -> str:
        return self._read_file("source_payload.json")

    def read_structural_findings(self) -> str:
        return self._read_file("structural_findings.json")

    def read_grounding_findings(self) -> str:
        return self._read_file("grounding_findings.json")

    def read_run_file(self, path: str) -> str:
        return self._read_file(path)

    def write_run_file(self, path: str, content: str) -> str:
        target = self._safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {target.relative_to(self.run_dir).as_posix()}"

    def list_run_files(self) -> str:
        files = sorted(path.relative_to(self.run_dir).as_posix() for path in self.run_dir.rglob("*") if path.is_file())
        return json.dumps(files, indent=2)

    def search_source_text(self, query: str, limit: int = 20) -> str:
        try:
            payload = json.loads(self.read_source_payload())
        except Exception as exc:
            return self._tool_error("invalid_source_payload", str(exc))
        needle = query.casefold()
        matches = []
        for span in payload.get("spans", []) or []:
            text = str(span.get("text") or "")
            if needle and needle in text.casefold():
                matches.append(
                    {
                        "span_id": span.get("span_id"),
                        "section_name": span.get("section_name"),
                        "page": span.get("page"),
                        "text": text[:1200],
                    }
                )
            if len(matches) >= limit:
                break
        return json.dumps(matches, indent=2, ensure_ascii=False)

    def read_skill_resource(self, path: str) -> str:
        try:
            return self.skill_pack.resource_text(path)
        except FileNotFoundError:
            return self._tool_error("skill_resource_not_found", f"Skill resource not found: {path}.")

    def read_output_schema(self) -> str:
        return self._read_file("rigor_findings_schema.json")

    def submit_rigor_findings(self, candidate_json: str) -> str:
        validation = self._validate_rigor_findings(candidate_json)
        if validation["issue_count"]:
            return json.dumps({"accepted": False, "validation": validation}, indent=2, ensure_ascii=False)
        self.write_run_file("rigor_findings.json", json.dumps(json.loads(candidate_json), indent=2, ensure_ascii=False))
        return json.dumps({"accepted": True, "output_path": "rigor_findings.json"}, indent=2)

    def _validate_rigor_findings(self, candidate_json: str) -> dict:
        issues = []
        try:
            raw = json.loads(candidate_json)
        except Exception as exc:
            return {"issue_count": 1, "issues": [{"path": "$", "code": "invalid_json", "message": str(exc)}]}
        if not isinstance(raw, dict):
            issues.append({"path": "$", "code": "invalid_type", "message": "Output must be a JSON object."})
        elif not isinstance(raw.get("findings"), list):
            issues.append({"path": "$.findings", "code": "invalid_type", "message": "findings must be an array."})
        else:
            for index, finding in enumerate(raw["findings"]):
                if not isinstance(finding, dict):
                    issues.append({"path": f"$.findings[{index}]", "code": "invalid_type", "message": "Finding must be an object."})
                    continue
                for field in ("dimension", "severity", "message"):
                    if not str(finding.get(field) or "").strip():
                        issues.append({"path": f"$.findings[{index}].{field}", "code": "missing_field", "message": "Required field is empty."})
        return {"issue_count": len(issues), "issues": issues}

    def _read_file(self, path: str) -> str:
        try:
            return self._safe_path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._tool_error("run_file_not_found", f"Run file not found: {path}")
        except ValueError as exc:
            return self._tool_error("invalid_run_file_path", str(exc))

    def _safe_path(self, path: str) -> Path:
        candidate = (self.run_dir / path).resolve()
        if candidate != self.run_dir and self.run_dir not in candidate.parents:
            raise ValueError(f"Path escapes run directory: {path}")
        return candidate

    def _tool_error(self, code: str, message: str) -> str:
        return json.dumps({"error": {"code": code, "message": message}}, indent=2, ensure_ascii=False)
