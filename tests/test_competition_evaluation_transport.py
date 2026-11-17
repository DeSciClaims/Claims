import gzip
import json

from competition.intake_lock import CompetitionBackend
from neurons.competition_evaluation_transport import CompressedEvaluationTransport


class ControllerBackend(CompressedEvaluationTransport, CompetitionBackend):
    pass


def test_controller_compresses_large_receipts_and_reads_compressed_replay(monkeypatch):
    requests = []
    full = {"input_sha256": "a" * 64, "result_private": {
        "scores": [{"job_id": "one", "score": {"score": 0.5}}], "pairs": [],
        "silver": {"private": "evidence " * 600_000}}}
    compact = {"input_sha256": full["input_sha256"], "carried_forward": None,
               "result_private": {"scores": full["result_private"]["scores"], "pairs": []}}

    class Response:
        def __init__(self, value):
            self.data = gzip.compress(json.dumps(value).encode(), mtime=0)
            self.headers = {"Content-Encoding": "gzip"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return self.data

    def open_request(request, timeout):
        requests.append(request)
        return Response(compact if request.get_method() == "POST" else full)

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    backend = ControllerBackend(base_url="https://backend.example", controller_token="secret")
    assert backend.freeze_paper_evaluation(
        "round-1", "prescreen", "paper-1", input_sha256="a" * 64,
        result_private=full["result_private"],
    ) == compact
    sent = requests[0]
    assert sent.full_url.endswith("/evaluation?compact=true")
    assert sent.get_header("Content-encoding") == "gzip"
    assert sent.get_header("Accept-encoding") == "gzip"
    assert json.loads(gzip.decompress(sent.data))["result_private"] == full["result_private"]
    assert len(sent.data) < 1_000_000
    assert backend.get_paper_evaluation("round-1", "prescreen", "paper-1") == full
