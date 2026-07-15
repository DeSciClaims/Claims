from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, Field


class SkillResource(BaseModel):
    path: str
    sha256: str
    text: str


class SkillPack(BaseModel):
    name: str
    root_dir: Path
    instructions: str
    metadata: dict[str, object] = Field(default_factory=dict)
    resources: dict[str, SkillResource] = Field(default_factory=dict)
    sha256: str

    def resource_text(self, path: str) -> str:
        try:
            return self.resources[path].text
        except KeyError as exc:
            raise FileNotFoundError(f"Skill resource not found: {path}") from exc

    def render_for_agent(self) -> str:
        lines = [
            f"# Skill: {self.name}",
            "",
            self.instructions.strip(),
            "",
            "## Mounted Skill Resources",
        ]
        for path, resource in sorted(self.resources.items()):
            if path == "SKILL.md":
                continue
            lines.extend(["", f"### {path}", "", resource.text.strip()])
        return "\n".join(lines).strip()

    def manifest(self) -> dict[str, object]:
        return {
            "name": self.name,
            "root_dir": str(self.root_dir),
            "sha256": self.sha256,
            "metadata": self.metadata,
            "resources": {
                path: {"sha256": resource.sha256, "bytes": len(resource.text.encode("utf-8"))}
                for path, resource in sorted(self.resources.items())
            },
        }


def load_skill_pack(root_dir: Path) -> SkillPack:
    resolved = root_dir.resolve()
    skill_md = resolved / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"Skill pack is missing SKILL.md: {skill_md}")
    resources: dict[str, SkillResource] = {}
    digest = hashlib.sha256()
    for path in sorted(item for item in resolved.rglob("*") if item.is_file()):
        if any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(resolved).parts):
            continue
        relative = path.relative_to(resolved).as_posix()
        text = path.read_text(encoding="utf-8")
        resource_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        resources[relative] = SkillResource(path=relative, sha256=resource_hash, text=text)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(resource_hash.encode("utf-8"))
        digest.update(b"\0")
    metadata = _parse_skill_frontmatter(resources["SKILL.md"].text)
    return SkillPack(
        name=str(metadata.get("name") or resolved.name),
        root_dir=resolved,
        instructions=resources["SKILL.md"].text,
        metadata=metadata,
        resources=resources,
        sha256=digest.hexdigest(),
    )


def _parse_skill_frontmatter(markdown: str) -> dict[str, object]:
    if not markdown.startswith("---"):
        return {}
    end = markdown.find("\n---", 3)
    if end < 0:
        return {}
    block = markdown[3:end].strip()
    metadata: dict[str, object] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_key, current_lines
        if current_key is not None:
            metadata[current_key] = "\n".join(current_lines).strip()

    for raw_line in block.splitlines():
        stripped = raw_line.strip()
        if not raw_line.startswith((" ", "\t")) and ":" in raw_line:
            flush()
            key, value = raw_line.split(":", 1)
            current_key = key.strip()
            value = value.strip()
            current_lines = [] if value in {"", "|", ">"} else [value]
        elif current_key is not None:
            current_lines.append(stripped)
    flush()
    return metadata
