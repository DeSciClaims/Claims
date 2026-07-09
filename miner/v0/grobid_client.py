from __future__ import annotations

import hashlib
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import requests


logger = logging.getLogger(__name__)


class GrobidClient:
    def __init__(
        self,
        base_url: str,
        cache_dir: Path,
        timeout_s: int = 120,
        retries: int = 3,
        retry_wait_s: int = 2,
    ) -> None:
        self.base_url = base_url
        self.cache_dir = cache_dir
        self.timeout_s = timeout_s
        self.retries = retries
        self.retry_wait_s = retry_wait_s
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, pdf_path: Path) -> Path:
        key = hashlib.sha256(str(pdf_path.resolve()).encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.tei.xml"

    def process_pdf(self, pdf_path: Path, use_cache: bool = True) -> str:
        cache_path = self._cache_path(pdf_path)
        if use_cache and cache_path.exists():
            return cache_path.read_text(encoding="utf-8")

        endpoint = urllib.parse.urljoin(self.base_url, "api/processFulltextDocument")
        for attempt in range(1, self.retries + 1):
            try:
                with pdf_path.open("rb") as handle:
                    response = requests.post(
                        endpoint,
                        files={
                            "input": (pdf_path.name, handle, "application/pdf"),
                            "consolidateHeader": (None, "1"),
                        },
                        timeout=self.timeout_s,
                    )
                if response.status_code in (200, 204) and response.text.strip():
                    cache_path.write_text(response.text, encoding="utf-8")
                    return response.text
                logger.warning(
                    "GROBID failed for %s with status %s",
                    pdf_path.name,
                    response.status_code,
                )
            except requests.RequestException as exc:
                logger.warning("GROBID request error for %s: %s", pdf_path.name, exc)
            if attempt < self.retries:
                time.sleep(self.retry_wait_s)
        raise RuntimeError(f"Failed to process {pdf_path} with GROBID")


def maybe_load_cached_tei(cache_dir: Path, pdf_path: Path) -> Optional[str]:
    key = hashlib.sha256(str(pdf_path.resolve()).encode("utf-8")).hexdigest()
    tei_path = cache_dir / f"{key}.tei.xml"
    if tei_path.exists():
        return tei_path.read_text(encoding="utf-8")
    return None

