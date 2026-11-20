"""Conservative checks for false extraction placeholders."""

import re


def normalized(text):
    return " ".join(text.split())


def placeholder_issues(payload: dict) -> list[dict]:
    """Missing/qualitative observations are allowed; claimed numeric digitization needs numbers."""

    def has_number(value):
        if isinstance(value, bool):
            return False
        if isinstance(value, (float, int)):
            return True
        if isinstance(value, str):
            return bool(re.search(r"(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?", value))
        if isinstance(value, dict):
            return any(has_number(v) for v in value.values())
        if isinstance(value, list):
            return any(has_number(v) for v in value)
        return False

    issues = []
    for section in ("results", "shared_context", "cross_group_summaries"):
        for record in payload.get(section, []):
            for field in record.get("fields", []):
                value = field.get("value")
                placeholder = isinstance(value, str) and normalized(value).lower() in {
                    "digitized curve",
                    "digitized values",
                    "see figure",
                    "see plot",
                    "see table",
                    "to be extracted",
                    "placeholder",
                    "tbd",
                }
                if placeholder or (
                    "digitiz" in field.get("origin", "").lower()
                    and not has_number(value)
                ):
                    issues.append(
                        {
                            "code": "content-placeholder",
                            "path": field["id"],
                            "message": "A placeholder is not an extracted scientific value. Digitized origin requires actual numerical data; disclose unrecovered graphical content in extraction notes rather than claiming digitization.",
                        }
                    )
    return issues
