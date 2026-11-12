import gzip
import json

from competition.intake_lock import CompetitionBackend


def test_publication_uses_gzip_without_changing_signed_receipt(monkeypatch):
    sent = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, *_):
            return b'{"status":"published"}'

    def open_request(request, timeout):
        sent["request"] = request
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    receipt = {"bundle": {"final_pair_scores": [{"score": 0.5}] * 1000},
               "bundle_sha256": "a" * 64, "signature_hex": "b" * 128}
    backend = CompetitionBackend(base_url="https://backend.example", controller_token="secret")
    assert backend.publish_round("round_1", receipt)["status"] == "published"
    request = sent["request"]
    assert request.get_header("Content-encoding") == "gzip"
    assert json.loads(gzip.decompress(request.data)) == receipt
    assert len(request.data) < len(json.dumps(receipt).encode())
