from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from competition.production_monitor import check_production_health
from neurons import competition_production_health


RECEIPT = "a" * 64
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


class Reader:
    def __init__(self, active=True):
        self.active = active

    def read_active(self):
        return (SimpleNamespace(round_id="week", receipt_sha256=RECEIPT)
                if self.active else None)


class Backend:
    def __init__(self, **changes):
        self.changes = changes

    def production_health(self, *, network, receipt_sha256):
        assert (network, receipt_sha256) == ("testnet", RECEIPT)
        return {
            "network": network, "receipt_sha256": receipt_sha256,
            "total": 3, "queued": 1, "leased": 1,
            "expired_leases": 0, "succeeded": 1, "terminal_failures": 0,
            "oldest_queued_at": "2026-11-16T11:45:00+00:00",
            **self.changes,
        }


def test_health_reports_active_queue_without_exposing_results():
    report = check_production_health(
        network="testnet", reader=Reader(), backend=Backend(), now=NOW,
        max_queued_seconds=1800,
    )
    assert report["status"] == "healthy"
    assert report["oldest_queued_age_seconds"] == 900
    assert report["alerts"] == []
    assert "result_private" not in report
    assert check_production_health(
        network="testnet", reader=Reader(False), backend=Backend(), now=NOW,
    )["status"] == "inactive"
    missing = check_production_health(
        network="testnet", reader=Reader(False), backend=Backend(), now=NOW,
        required_active_at=datetime(2026, 11, 16, 9, tzinfo=UTC),
    )
    assert missing["alerts"] == ["missing_active_release"]


def test_health_alerts_on_faults_expired_leases_and_stale_queue():
    report = check_production_health(
        network="testnet", reader=Reader(), backend=Backend(
            total=4, expired_leases=1, terminal_failures=1,
            oldest_queued_at="2026-11-16T10:00:00+00:00",
        ), now=NOW, max_queued_seconds=3600,
    )
    assert report["status"] == "alert"
    assert report["alerts"] == ["terminal_failures", "expired_leases", "queue_age"]


@pytest.mark.parametrize("changes", [
    {"receipt_sha256": "b" * 64}, {"queued": -1},
    {"total": 5}, {"expired_leases": 2},
    {"oldest_queued_at": None},
])
def test_health_rejects_mismatched_backend_summary(changes):
    with pytest.raises(ValueError):
        check_production_health(
            network="testnet", reader=Reader(), backend=Backend(**changes), now=NOW,
        )


def test_cli_uses_results_token_and_nonzero_alert_exit(monkeypatch, capsys):
    monkeypatch.setenv("CLAIMS_PRODUCTION_RESULTS_TOKEN", "reader-only")
    monkeypatch.setenv("CLAIMS_PRODUCTION_START_AT", "2026-11-16T09:00:00+00:00")
    captured = {}

    def backend(*, base_url, controller_token):
        captured.update(base_url=base_url, token=controller_token)
        return object()

    monkeypatch.setattr(competition_production_health, "CompetitionBackend", backend)
    monkeypatch.setattr(competition_production_health, "ProductionReleaseReader",
                        lambda root: object())
    monkeypatch.setattr(competition_production_health, "check_production_health",
                        lambda **kwargs: {"status": "alert", "alerts": ["terminal_failures"]})
    assert competition_production_health.main([
        "--network", "testnet", "--backend-url", "https://api.example.org",
        "--production-root", "/private/production",
    ]) == 2
    assert captured == {"base_url": "https://api.example.org", "token": "reader-only"}
    assert '"terminal_failures"' in capsys.readouterr().out
