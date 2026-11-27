"""Strict owner-approved continuation of an original sampling commitment.

Builtin-only contract shared byte-for-byte with the backend. A continuation
preserves the original commitment and seed; it never manufactures a replacement.
"""
import copy
import hashlib
import json
import math
import re

KIND='claims_seed_policy_continuation_v1'
PROTOCOL='claims-source-task-v1'
FIELDS={'kind','scope','prepared_source','source_packet','source_frame','original_policy',
    'current_policy','original_commitment','secret_sha256','input_pool_sha256',
    'approved_by','approved_at','reason','preserve'}
PRESERVE={'seed':True,'source':True,'outputs':True,'deadlines':True}

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()

def sha(raw):return hashlib.sha256(raw).hexdigest()

def key(scope,policy_sha256):return 'seed-continuation:'+scope[1]+':'+sha(canonical([*scope,policy_sha256]))

def commitment(secret,scope,source_sha256,policy_sha256):
    if len(secret)!=32:raise ValueError('Sampling seed must retain its original 32 bytes')
    parts=[*scope,source_sha256,policy_sha256]
    raw=b''.join(len(p.encode()).to_bytes(8,'big')+p.encode() for p in parts)
    return sha(raw+secret)

def read(store,ref):
    if (not isinstance(ref,dict) or set(ref)!={'sha256','relative_path','byte_length','media_type'}
        or not re.fullmatch('[0-9a-f]{64}',str(ref['sha256']))
        or type(ref['byte_length']) is not int or ref['byte_length']<0):
        raise ValueError('Invalid continuation evidence reference')
    raw=store.get_blob(ref['sha256'])
    if sha(raw)!=ref['sha256'] or len(raw)!=ref['byte_length']:
        raise ValueError('Continuation evidence hash/length mismatch')
    return raw

def document(store,ref):return json.loads(read(store,ref))

def equivalent_settings(store,old_ref,new_ref,round_id):
    old=document(store,old_ref);new=document(store,new_ref)
    if old_ref['sha256']==new_ref['sha256']:raise ValueError('Continuation requires distinct original/current policies')
    for config in (old,new):
        spec=config['scientific_scoring']
        if (spec['first_round_scoring']['round_id']!=round_id
            or spec['experimental_launch']['round_id']!=round_id
            or spec['settings']['sha256']!=spec['settings_sha256']):
            raise ValueError('Continuation requires exact round-bound scientific policies')
    a=document(store,old['scientific_scoring']['settings'])
    b=document(store,new['scientific_scoring']['settings'])
    for settings in (a,b):
        digest=settings.pop('implementation_digest',None)
        if not re.fullmatch('[0-9a-f]{64}',str(digest)):raise ValueError('Missing scientific implementation identity')
    if canonical(a)!=canonical(b):raise ValueError('Scientific settings changed; seed carry-forward is forbidden')
    x=copy.deepcopy(old);y=copy.deepcopy(new)
    for config in (x,y):
        files=config['competition_policy_runtime']['source_sha256']
        if not re.fullmatch('[0-9a-f]{64}',str(files.get('scientific_factory.py'))):
            raise ValueError('Missing scientific factory identity')
        files['scientific_factory.py']='OWNER_APPROVED_IMPLEMENTATION_TRANSITION'
        spec=config['scientific_scoring'];spec['settings']=None;spec['settings_sha256']=None
    if canonical(x)!=canonical(y):raise ValueError('Policy changed outside the approved implementation identity fields')

def validate_record(store,ref,*,scope,source_packet_sha256,source_frame_sha256,
                    current_policy_sha256,input_pool_sha256,secret,original_commitment):
    value=document(store,ref)
    names=('protocol_id','round_id','stage','paper_id')
    if (set(value)!=FIELDS or value['kind']!=KIND or tuple(value['scope'].get(k) for k in names)!=tuple(scope)
        or set(value['scope'])!=set(names) or scope[0]!=PROTOCOL
        or scope[2] not in {'prescreen','screen','final','week_one'}
        or value['source_packet']['sha256']!=source_packet_sha256
        or value['source_frame']['sha256']!=source_frame_sha256
        or value['current_policy']['sha256']!=current_policy_sha256
        or value['input_pool_sha256']!=input_pool_sha256
        or value['original_commitment']!=original_commitment
        or value['secret_sha256']!=sha(secret) or len(secret)!=32
        or value['reason']!='NONSCIENTIFIC_IMPLEMENTATION_TRANSITION'
        or canonical(value['preserve'])!=canonical(PRESERVE)
        or not isinstance(value['approved_by'],str) or not 1<=len(value['approved_by'])<=128
        or type(value['approved_at']) not in {int,float} or not math.isfinite(value['approved_at']) or value['approved_at']<=0):
        raise ValueError('Seed continuation scope, authorization or original inputs changed')
    prepared=document(store,value['prepared_source'])
    if store.head('prepared-source:'+scope[1]+':'+scope[3])!=value['prepared_source']['sha256']:
        raise ValueError('Seed continuation uses a superseded preparation')
    seed=prepared['seeds_by_stage'][scope[2]]
    if (prepared['source_packet']!=value['source_packet'] or prepared['source_frame']!=value['source_frame']
        or seed['secret']['sha256']!=value['secret_sha256'] or seed['commitment']!=original_commitment):
        raise ValueError('Seed continuation differs from the original committed preparation')
    equivalent_settings(store,value['original_policy'],value['current_policy'],scope[1])
    if commitment(secret,scope,source_packet_sha256,value['original_policy']['sha256'])!=original_commitment:
        raise ValueError('Original sampling commitment does not verify')
    packet=document(store,value['source_packet']);frame=document(store,value['source_frame'])
    if ((packet.get('protocol_id'),packet.get('round_id'),packet.get('paper_id'))!=(scope[0],scope[1],scope[3])
        or (frame.get('protocol_id'),frame.get('paper_id'),frame.get('source_packet_sha256'))
           !=(scope[0],scope[3],source_packet_sha256)):
        raise ValueError('Continuation source artifacts belong to another scope')
    return value

def verify(store,*,scope,source_packet_sha256,source_frame_sha256,current_policy_sha256,
           input_pool_sha256,secret,original_commitment):
    if commitment(secret,scope,source_packet_sha256,current_policy_sha256)==original_commitment:
        return None
    digest=store.head(key(scope,current_policy_sha256))
    if digest is None:raise ValueError('Seed commitment mismatch; no approved continuation')
    raw=store.get_blob(digest)
    ref={'sha256':digest,'relative_path':'blobs/'+digest,'byte_length':len(raw),'media_type':'application/json'}
    return validate_record(store,ref,scope=scope,source_packet_sha256=source_packet_sha256,
        source_frame_sha256=source_frame_sha256,current_policy_sha256=current_policy_sha256,
        input_pool_sha256=input_pool_sha256,secret=secret,original_commitment=original_commitment)
