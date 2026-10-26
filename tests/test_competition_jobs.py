from competition.jobs import build_jobs, job_id


def test_job_ids_are_stable_and_unqualified_submissions_are_skipped():
    selected = [
        {"hotkey": "miner1", "package_sha256": "a" * 64, "qualified": True},
        {"hotkey": "miner2", "package_sha256": "b" * 64, "qualified": False},
    ]
    sample = [
        {"stage": "week_one", "paper_id": "paper1"},
        {"stage": "week_one", "paper_id": "paper2"},
    ]
    arguments = {
        "round_id": "week1", "selected": selected, "sample": sample,
        "configuration_sha256": "c" * 64,
    }
    first = build_jobs(**arguments)
    assert first == build_jobs(**arguments)
    assert len(first) == 2
    assert {job["hotkey"] for job in first} == {"miner1"}
    assert first[0]["job_id"] == job_id(
        round_id="week1", hotkey="miner1", package_sha256="a" * 64,
        stage="week_one", paper_id="paper1", configuration_sha256="c" * 64,
    )
    assert first[0]["job_id"] != first[1]["job_id"]
