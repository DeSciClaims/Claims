from __future__ import annotations

from datetime import datetime, timezone

from neurons.memory_monitor import MemorySample, ValidatorMemorySampler


def test_memory_sampler_summarizes_run_and_stage_interval() -> None:
    sampler = ValidatorMemorySampler(interval_seconds=1.0)
    sampler._samples = [
        MemorySample(timestamp=100.0, process_tree_rss_bytes=100 * 1024 * 1024, system_available_bytes=900 * 1024 * 1024, process_count=2),
        MemorySample(timestamp=101.0, process_tree_rss_bytes=250 * 1024 * 1024, system_available_bytes=700 * 1024 * 1024, process_count=5),
        MemorySample(timestamp=102.0, process_tree_rss_bytes=180 * 1024 * 1024, system_available_bytes=750 * 1024 * 1024, process_count=3),
    ]

    summary = sampler.summary()
    assert summary["process_tree_rss_start_mb"] == 100.0
    assert summary["process_tree_rss_end_mb"] == 180.0
    assert summary["process_tree_rss_peak_mb"] == 250.0
    assert summary["system_available_min_mb"] == 700.0
    assert summary["process_count_peak"] == 5

    start = datetime.fromtimestamp(100.5, timezone.utc).isoformat()
    end = datetime.fromtimestamp(101.5, timezone.utc).isoformat()
    stage = sampler.interval_summary(start, end)
    assert stage["sample_count"] == 1
    assert stage["process_tree_rss_peak_mb"] == 250.0
