import hashlib

from competition.logs import upload_worker_log


def test_worker_log_is_private_hotkey_scoped_and_redacts_paper_key(tmp_path):
    log = tmp_path / "worker.log"
    log.write_text("[install] done\n[execute] token=sk-bf-secret\n")

    class Store:
        def put_object(self, **kwargs):
            self.record = kwargs

    store = Store()
    receipt = upload_worker_log(
        s3_client=store, bucket="private-competition", log_path=log,
        network="testnet", round_id="week1", hotkey="MinerHotkey",
        job_id="job-1", paper_key="sk-bf-secret",
    )
    assert receipt.object_key == "logs/testnet/week1/MinerHotkey/job-1.txt"
    assert store.record["Bucket"] == "private-competition"
    assert b"sk-bf-secret" not in store.record["Body"]
    assert b"[install] done" in store.record["Body"]
    assert receipt.sha256 == hashlib.sha256(store.record["Body"]).hexdigest()
    assert store.record["Metadata"]["sha256"] == receipt.sha256
