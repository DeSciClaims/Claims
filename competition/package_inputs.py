"""Resolve one leased job to its sealed package in trusted controller compute."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class PackageBackend(Protocol):
    def get_round(self, round_id: str) -> Mapping[str, Any]: ...
    def list_stages(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_submissions(self, round_id: str) -> list[dict[str, Any]]: ...


class SealedStorage(Protocol):
    def package_for_submission(self, upload: Mapping[str, Any]) -> bytes: ...


@dataclass(frozen=True)
class CompetitionPackageResolver:
    backend: PackageBackend
    storage: SealedStorage
    baseline_upload: Mapping[str, Any]
    reference_upload: Mapping[str, Any] | None = None

    def _reference_upload_for_round(self, round_id: str) -> Mapping[str, Any]:
        round_record = self.backend.get_round(round_id)
        if not round_record:
            raise ValueError("reference round does not exist")
        upload = (self.baseline_upload if round_record.get("incumbent_hotkey") is None
                  else self.reference_upload)
        if (upload is None
            or upload.get("package_sha256") != round_record.get("reference_package_sha256")
            or not upload.get("package_object_key")):
            raise ValueError("reference upload differs from frozen round")
        return upload

    def reference_manifest_for_round(self, round_id: str) -> dict[str, Any]:
        manifest = self._reference_upload_for_round(round_id).get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError("reference package has no valid manifest")
        return dict(manifest)

    def reference_package_for_round(self, round_id: str) -> bytes:
        package = self.storage.package_for_submission(self._reference_upload_for_round(round_id))
        if not isinstance(package, bytes):
            raise ValueError("sealed reference read did not return bytes")
        return package

    def _upload_for_job(self, job: Mapping[str, Any]) -> Mapping[str, Any]:
        round_id = str(job["round_id"])
        stage = str(job["stage"])
        hotkey = str(job["hotkey"])
        package_sha256 = str(job["package_sha256"])
        round_record = self.backend.get_round(round_id)
        if not round_record:
            raise ValueError("job round does not exist")
        stage_rows = [row for row in self.backend.list_stages(round_id)
                      if row["stage"] == stage]
        if len(stage_rows) != 1:
            raise ValueError("job stage is not frozen")
        entries = [row for row in stage_rows[0]["entries"]
                   if row["hotkey"] == hotkey and row["package_sha256"] == package_sha256]
        if len(entries) != 1:
            raise ValueError("job package differs from selected stage entry")
        role = entries[0]["role"]
        if role in {"reference", "incumbent"}:
            if role == "reference" and round_record.get("incumbent_hotkey") is not None:
                if not any(row["stage"] == "rerun" for row in self.backend.list_stages(round_id)):
                    raise ValueError("reference job has no frozen failure rerun")
                upload = self._reference_upload_for_round(round_id)
                expected_hash = round_record["reference_package_sha256"]
            else:
                upload = self.baseline_upload
                expected_hash = round_record["baseline_package_sha256"]
            if (upload.get("hotkey") != hotkey
                or upload.get("package_sha256") != package_sha256
                or expected_hash != package_sha256
                or (role == "incumbent" and round_record.get("incumbent_hotkey") != hotkey)
                or (role == "reference" and round_record.get("incumbent_hotkey") is None
                    and round_record.get("reference_package_sha256") != package_sha256)):
                raise ValueError("baseline upload differs from frozen round")
        elif role == "challenger":
            uploads = [row for row in self.backend.list_submissions(round_id)
                       if row["hotkey"] == hotkey
                       and row["package_sha256"] == package_sha256
                       and row["status"] in {"accepted", "evaluating", "finished"}]
            if len(uploads) != 1:
                raise ValueError("selected challenger has no unique sealed upload")
            upload = uploads[0]
        else:
            raise ValueError("job has an unknown stage role")
        return upload

    def manifest_for_job(self, job: Mapping[str, Any]) -> dict[str, Any]:
        manifest = self._upload_for_job(job).get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError("selected package has no valid manifest")
        return dict(manifest)

    def __call__(self, job: Mapping[str, Any]) -> bytes:
        upload = self._upload_for_job(job)
        package = self.storage.package_for_submission(upload)
        if not isinstance(package, bytes):
            raise ValueError("sealed package read did not return bytes")
        return package
