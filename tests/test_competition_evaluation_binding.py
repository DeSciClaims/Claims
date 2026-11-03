import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from competition.evaluation_binding import prepare_evaluation
from competition.pilot_worker_factory import _round_file
from competition.publication import publication_bundle, canonical_sha256
from competition.round_setup import _canonical_bytes


def fixture(tmp_path,monkeypatch):
    monkeypatch.setenv('CLAIMS_COMPETITION_ROUNDS_DIR',str(tmp_path))
    monkeypatch.setenv('CLAIMS_COMPETITION_PRODUCTION_ROOT',str(tmp_path/'production'))
    monkeypatch.setenv('CLAIMS_COMPETITION_PUBLISHER_HOTKEY','signer')
    row=dict(round_id='b',network='testnet',deadline_at='2026-11-03T12:00:00+00:00',
        rules_sha256='a'*64,configuration_sha256='b'*64,reference_package_sha256='c'*64,
        rules_public={'policy':{'baseline_selection':'previous_completed_round'}})
    private={'round_id':'b','configuration_sha256':'b'*64,'baseline_pending':True,
        'baseline_upload':{'hotkey':'reference','team_id':'claims','package_sha256':'c'*64},
        'reference_upload':None,'incumbent_hotkey':None}
    (tmp_path/'b.json').write_bytes(_canonical_bytes(private))
    bound=[]
    backend=SimpleNamespace(evaluation_gate=lambda _:{'ready':True,'predecessor':None},
        bind_evaluation=lambda rid,binding:bound.append(binding) or binding)
    monkeypatch.setattr('competition.evaluation_binding.ProductionReleaseReader',lambda _:SimpleNamespace(read_active=lambda:None))
    return row,backend,bound


def test_first_reference_binds_once_without_altering_intake_file(tmp_path,monkeypatch):
    row,backend,bound=fixture(tmp_path,monkeypatch)
    original=(tmp_path/'b.json').read_bytes()
    assert prepare_evaluation(row,backend)['status']=='evaluation_ready'
    private=_round_file('b')
    assert private['baseline_pending'] is False
    assert private['binding']==bound[0]
    assert (tmp_path/'b.json').read_bytes()==original
    row['evaluation_binding']=bound[0]
    assert prepare_evaluation(row,backend)['status']=='evaluation_ready'
    assert len(bound)==1
    assert (tmp_path/'b.evaluation.json').stat().st_mode & 0o777 == 0o600


def test_binding_retry_reuses_private_receipt_after_backend_failure(tmp_path,monkeypatch):
    row,backend,bound=fixture(tmp_path,monkeypatch)
    real=backend.bind_evaluation
    backend.bind_evaluation=lambda *a:(_ for _ in ()).throw(ConnectionError('backend offline'))
    with pytest.raises(ConnectionError):prepare_evaluation(row,backend)
    first=(tmp_path/'b.evaluation.json').read_bytes()
    backend.bind_evaluation=real
    assert prepare_evaluation(row,backend)['status']=='evaluation_ready'
    assert (tmp_path/'b.evaluation.json').read_bytes()==first


def test_queued_or_failed_predecessor_does_not_resolve_a_new_baseline(tmp_path,monkeypatch):
    row,backend,bound=fixture(tmp_path,monkeypatch)
    backend.evaluation_gate=lambda _:{'ready':False}
    assert prepare_evaluation(row,backend)['status']=='waiting_for_preceding_round'
    backend.evaluation_gate=lambda _:{'ready':True,'previous_round':{'round_id':'a','status':'failed','decision':'failure'}}
    assert prepare_evaluation(row,backend)['status']=='waiting_for_preceding_failure_review'
    assert not bound and not (tmp_path/'b.evaluation.json').exists()


def test_signed_publication_includes_binding_evidence():
    from competition.publication import FROZEN_FIELDS
    row={key:'frozen' for key in FROZEN_FIELDS}
    binding={'rules_sha256':row['rules_sha256'],'baseline_package_sha256':'actual-winner'}
    row['evaluation_binding']=binding
    bundle=publication_bundle(row,results={'decision':'winner'},weights={'vector':[]})
    assert bundle['evaluation_binding']==binding
    altered=copy.deepcopy(bundle);altered['evaluation_binding']['baseline_package_sha256']='other'
    assert canonical_sha256(altered)!=canonical_sha256(bundle)


def test_previous_winner_can_activate_after_this_rounds_submission_deadline(tmp_path,monkeypatch):
    from competition.handoff_controller import activate_published_round
    from competition.production_handoff import FilesystemProductionReleaseStore
    from test_competition_production_handoff import _fixture, PACKAGE, PACKAGE_SHA, MANIFEST
    row,backend,bound=fixture(tmp_path,monkeypatch)
    # Use the real reader after the helper's empty-reader setup.
    from competition.production_reader import ProductionReleaseReader
    monkeypatch.setattr('competition.evaluation_binding.ProductionReleaseReader',ProductionReleaseReader)
    signer,prior,stage,receipt=_fixture(accelerated=True)
    monkeypatch.setenv('CLAIMS_COMPETITION_PUBLISHER_HOTKEY',signer.ss58_address)
    upload=dict(hotkey='winner',team_id='new-team',package_sha256=PACKAGE_SHA,
        package_object_key='sealed/winner.whl',package_size_bytes=len(PACKAGE),manifest=MANIFEST,status='accepted')
    backend.base_url='https://backend.example'
    backend.get_round=lambda _:prior
    backend.list_stages=lambda _:[stage]
    backend.list_submissions=lambda _:[upload]
    backend.evaluation_gate=lambda _:{'ready':True,'predecessor':{'round_id':prior['round_id']}}
    now=datetime(2026,11,2,15,tzinfo=UTC)
    release=FilesystemProductionReleaseStore(tmp_path/'production')
    activate_published_round(prior['round_id'],backend=backend,
        publication_for_round=lambda _:receipt,sealed_packages=SimpleNamespace(package_for_submission=lambda _:PACKAGE),
        releases=release,expected_signer_hotkey=signer.ss58_address,network='testnet',now=now)
    row['deadline_at']='2026-11-02T12:30:00+00:00'
    monkeypatch.setattr('competition.evaluation_binding.fetch_round_publication',lambda **_:receipt)
    assert prepare_evaluation(row,backend,now=now)['status']=='evaluation_ready'
    assert bound[0]['incumbent_hotkey']=='winner'
    assert bound[0]['baseline_package_sha256']==PACKAGE_SHA
    assert bound[0]['baseline_origin']['receipt_sha256']==receipt['bundle_sha256']
    assert _round_file('b')['baseline_upload']==upload
