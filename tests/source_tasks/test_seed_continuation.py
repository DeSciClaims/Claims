"""Original commitment is carried only under explicit, identical-science approval."""
import copy
import pytest
from validator.scientific_result_v1.source_tasks.store import Store,sha,canonical
from validator.scientific_result_v1.source_tasks.sampling import seed_commitment
from validator.scientific_result_v1.source_tasks import seed_continuation as contract


def fixture(tmp_path):
    store=Store(tmp_path/'evidence');scope=(contract.PROTOCOL,'round','final','paper');secret=b's'*32
    def put(v):return store.put(v).model_dump(mode='json')
    old_settings={'implementation_digest':'1'*64,'model_routes':[{'model':'fixed'}],
        'content_weight':7,'archive_weight':2,'source_parser_digest':'2'*64,'execution_policy':{'fixed':True}}
    new_settings={**old_settings,'implementation_digest':'3'*64}
    def config(settings,factory):
        ref=put(settings)
        return {'competition_policy_runtime':{'source_sha256':{'scientific_factory.py':factory,'other.py':'a'*64}},
            'scientific_scoring':{'settings':ref,'settings_sha256':ref['sha256'],
                'first_round_scoring':{'round_id':'round'},'experimental_launch':{'round_id':'round'}},
            'worker_policy':{'dollar_limit_usd':.1}}
    old=put(config(old_settings,'4'*64));new=put(config(new_settings,'5'*64))
    packet=put({'protocol_id':contract.PROTOCOL,'paper_id':'paper','round_id':'round'});frame=put({'protocol_id':contract.PROTOCOL,'paper_id':'paper','source_packet_sha256':packet['sha256']})
    secret_ref=store.put_named('private-seeds/original',secret).model_dump(mode='json')
    original=seed_commitment(secret,scope,packet['sha256'],old['sha256'])
    prepared=put({'source_packet':packet,'source_frame':frame,'seeds_by_stage':{'final':{'secret':secret_ref,'commitment':original}}})
    store.advance('prepared-source:round:paper',None,prepared['sha256'])
    value={'kind':contract.KIND,'scope':dict(zip(('protocol_id','round_id','stage','paper_id'),scope)),
        'prepared_source':prepared,'source_packet':packet,'source_frame':frame,'original_policy':old,
        'current_policy':new,'original_commitment':original,'secret_sha256':sha(secret),'input_pool_sha256':'6'*64,
        'approved_by':'owner-approved','approved_at':1,'reason':'NONSCIENTIFIC_IMPLEMENTATION_TRANSITION','preserve':contract.PRESERVE.copy()}
    kwargs={'scope':scope,'source_packet_sha256':packet['sha256'],'source_frame_sha256':frame['sha256'],
        'current_policy_sha256':new['sha256'],'input_pool_sha256':'6'*64,'secret':secret,'original_commitment':original}
    return store,value,kwargs


def approve(store,value,kwargs):
    ref=store.put(value);store.advance(contract.key(kwargs['scope'],kwargs['current_policy_sha256']),None,ref.sha256)
    return ref


def test_explicit_original_seed_and_commitment_are_preserved(tmp_path):
    store,value,kwargs=fixture(tmp_path)
    with pytest.raises(ValueError,match='no approved continuation'):contract.verify(store,**kwargs)
    before=canonical(value);approve(store,value,kwargs)
    assert contract.verify(store,**kwargs)==value
    assert canonical(value)==before
    assert contract.commitment(kwargs['secret'],kwargs['scope'],kwargs['source_packet_sha256'],value['original_policy']['sha256'])==value['original_commitment']
    assert contract.commitment(kwargs['secret'],kwargs['scope'],kwargs['source_packet_sha256'],kwargs['current_policy_sha256'])!=value['original_commitment']


def test_current_policy_commitments_remain_ordinary(tmp_path):
    store,value,kwargs=fixture(tmp_path)
    kwargs['original_commitment']=seed_commitment(kwargs['secret'],kwargs['scope'],kwargs['source_packet_sha256'],kwargs['current_policy_sha256'])
    assert contract.verify(store,**kwargs) is None


@pytest.mark.parametrize('field,value',[('secret',b'x'*32),('source_packet_sha256','a'*64),
    ('source_frame_sha256','b'*64),('current_policy_sha256','c'*64),('input_pool_sha256','d'*64),
    ('original_commitment','e'*64),('scope',(contract.PROTOCOL,'other','final','paper')),
    ('scope',(contract.PROTOCOL,'round','screen','paper'))])
def test_changed_original_inputs_are_rejected(tmp_path,field,value):
    store,record,kwargs=fixture(tmp_path);approve(store,record,kwargs);kwargs[field]=value
    with pytest.raises((ValueError,KeyError)):contract.verify(store,**kwargs)


@pytest.mark.parametrize('change',['model','weight','parser','execution','other_code','worker_budget'])
def test_scientific_or_other_policy_changes_are_rejected(tmp_path,change):
    store,value,kwargs=fixture(tmp_path)
    new=contract.document(store,value['current_policy']);settings=contract.document(store,new['scientific_scoring']['settings'])
    if change=='model':settings['model_routes'][0]['model']='different'
    elif change=='weight':settings['content_weight']=8
    elif change=='parser':settings['source_parser_digest']='b'*64
    elif change=='execution':settings['execution_policy']={'fixed':False}
    elif change=='other_code':new['competition_policy_runtime']['source_sha256']['other.py']='b'*64
    else:new['worker_policy']['dollar_limit_usd']=1
    ref=store.put(settings).model_dump(mode='json');new['scientific_scoring']['settings']=ref;new['scientific_scoring']['settings_sha256']=ref['sha256']
    value['current_policy']=store.put(new).model_dump(mode='json');kwargs['current_policy_sha256']=value['current_policy']['sha256'];approve(store,value,kwargs)
    with pytest.raises(ValueError,match='changed'):contract.verify(store,**kwargs)


def test_superseded_preparation_and_unapproved_record_changes_are_rejected(tmp_path):
    store,value,kwargs=fixture(tmp_path);approve(store,value,kwargs)
    replacement=store.put({'different':'preparation'})
    store.advance('prepared-source:round:paper',value['prepared_source']['sha256'],replacement.sha256)
    with pytest.raises(ValueError,match='superseded'):contract.verify(store,**kwargs)


@pytest.mark.parametrize('change',['extra','approval','clock','preserve'])
def test_closed_authorization_contract(tmp_path,change):
    store,value,kwargs=fixture(tmp_path)
    if change=='extra':value['skip_checks']=True
    elif change=='approval':value['approved_by']=''
    elif change=='clock':value['approved_at']=-1
    else:value['preserve']['outputs']=False
    approve(store,value,kwargs)
    with pytest.raises(ValueError):contract.verify(store,**kwargs)
