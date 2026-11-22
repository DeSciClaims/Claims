import pytest
from competition import pilot_controller_factory as factory
from competition.publication import canonical_sha256


def test_scientific_controller_does_not_require_legacy_judge_panels(monkeypatch):
    configuration = {"scientific_scoring": {"protocol_id": "claims-source-task-v1"}}
    digest = canonical_sha256(configuration)
    monkeypatch.setattr(factory, "_round_file", lambda _: {
        "configuration": configuration, "configuration_sha256": digest})
    def legacy(*args, **kwargs):
        raise AssertionError("Scientific controller entered legacy judge validation")
    monkeypatch.setattr(factory, "validate_provisional_judge_routes", legacy)
    with pytest.raises(ValueError, match="sealed baseline"):
        factory.create_controller_inputs({"round_id": "test", "network": "testnet",
                                          "configuration_sha256": digest}, object())


def test_legacy_controller_still_validates_its_judge_panels(monkeypatch):
    monkeypatch.setattr(factory, "_round_file", lambda _: {"configuration": {}})
    def legacy(*args, **kwargs):
        raise ValueError("legacy routes checked")
    monkeypatch.setattr(factory, "validate_provisional_judge_routes", legacy)
    with pytest.raises(ValueError, match="legacy routes checked"):
        factory.create_controller_inputs({"round_id": "test", "network": "testnet"}, object())
