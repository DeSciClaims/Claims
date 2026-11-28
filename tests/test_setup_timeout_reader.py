"""Reuse only exact completed immutable receipts; retain all current-head checks."""
import importlib.util,json,hashlib
from pathlib import Path
from types import SimpleNamespace
import pytest
spec=importlib.util.spec_from_file_location('setup_reader',Path(__file__).parents[1]/'scripts/scientific_setup_timeout_reader.py')
reader=importlib.util.module_from_spec(spec);spec.loader.exec_module(reader)


def fixture():
    proof='b'*64;approval=json.dumps({'verified_scientific_audit':{'sha256':proof}}).encode();review=hashlib.sha256(approval).hexdigest()
    receipt=SimpleNamespace(binding=SimpleNamespace(round_id='r',stage='final',paper_id='p',policy_sha256='c'*64,revision=0))
    ref=SimpleNamespace(sha256='a'*64);store=SimpleNamespace(load=lambda ref,kind:receipt,head=lambda key:'a'*64,get_blob=lambda key:approval)
    args=dict(verified={'a'*64:{'paper_id':'p','revision':0}},round_id='r',configuration_sha256='c'*64,
        review_sha256=review,proof_sha256=proof,original_replay=lambda *a,**kw:'original')
    return store,ref,receipt,args


def test_completed_receipt_is_reused_only_with_current_scope_and_authorized_proof():
    store,ref,receipt,args=fixture()
    assert reader.completed_receipt(store,ref,**args) is receipt
    ref.sha256='d'*64
    assert reader.completed_receipt(store,ref,**args)=='original'


@pytest.mark.parametrize('change',['head','policy','revision','paper','proof','approval'])
def test_changed_live_state_or_checkpoint_is_rejected(change):
    store,ref,receipt,args=fixture()
    if change=='head':store.head=lambda key:'d'*64
    elif change=='policy':receipt.binding.policy_sha256='d'*64
    elif change=='revision':receipt.binding.revision=1
    elif change=='paper':receipt.binding.paper_id='different'
    elif change=='proof':args['proof_sha256']='d'*64
    elif change=='approval':store.get_blob=lambda key:b'{}'
    with pytest.raises(ValueError):reader.completed_receipt(store,ref,**args)
