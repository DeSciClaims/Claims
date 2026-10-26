import hashlib
from io import BytesIO

import pytest

from competition.hippius import HippiusPrivateStorage


def test_private_storage_checks_package_and_writes_scoped_log():
    package = b"wheel bytes"

    class Client:
        logged = []

        def get_object(self, **kwargs):
            assert kwargs == {"Bucket": "packages", "Key": "sealed/testnet/week2/package.whl"}
            return {"ContentLength": len(package), "Body": BytesIO(package)}

        def put_object(self, **kwargs):
            self.logged.append(kwargs)

    client = Client()
    storage = HippiusPrivateStorage(client, "packages", "logs")
    upload = {"package_object_key": "sealed/testnet/week2/package.whl",
              "package_size_bytes": len(package),
              "package_sha256": hashlib.sha256(package).hexdigest()}
    assert storage.package_for_submission(upload) == package
    key = f"logs/testnet/week2/{'5' * 48}/{'a' * 64}/{'b' * 64}-attempt-1.txt"
    storage.put(key, b"installed\nran\n")
    storage.put(key.replace("attempt-1", "attempt-2"), b"retry\n")
    assert [row["Key"] for row in client.logged] == [key, key.replace("attempt-1", "attempt-2")]
    assert all(row["Bucket"] == "logs" for row in client.logged)
    assert client.logged[0]["Metadata"]["sha256"] == hashlib.sha256(b"installed\nran\n").hexdigest()
    with pytest.raises(ValueError, match="path or size"):
        storage.put("logs/testnet/other.txt", b"bad")
    with pytest.raises(ValueError, match="path or size"):
        storage.put(key.replace("attempt-1", "attempt-3"), b"bad")
