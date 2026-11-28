"""Closed, log-bound owner review of historical worker setup timeouts.

Builtin-only contract shared with the API. Original jobs and scientific scores
are never rewritten; only the whole-entry build audit consumes this review.
"""
import copy,hashlib,json,re,math
KIND='claims_setup_timeout_review_v1'
FIELDS={'kind','round_id','stage','configuration_sha256','approved_by','approved_at','preserve_paper_scores','verified_scientific_audit','records'}

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def read(store,ref):
    if (not isinstance(ref,dict) or set(ref)!={'relative_path','sha256','byte_length','media_type'}
        or not re.fullmatch('[0-9a-f]{64}',str(ref['sha256'])) or type(ref['byte_length']) is not int or ref['byte_length']<0):raise ValueError('Invalid setup review reference')
    raw=store.get_blob(ref['sha256'])
    if sha(raw)!=ref['sha256'] or len(raw)!=ref['byte_length']:raise ValueError('Setup review evidence hash/length changed')
    return raw

def verified_timeout(raw):
    text=raw.decode('utf-8','strict')
    venv=re.findall(r'^\[venv\] exit=0 elapsed=([0-9.]+)s$',text,re.M)
    install=re.findall(r'^\[install\] timeout after ([0-9.]+)s$',text,re.M)
    starts=re.findall(r'^\[install\] start: (.+)$',text,re.M)
    if (len(venv)!=1 or len(install)!=1 or len(starts)!=1 or '[execute]' in text
        or re.search(r'^ERROR:|^Traceback \(most recent call last\):|^\[install\] exit=',text,re.M)
        or not starts[0].endswith('.whl') or ' -m pip install --no-index --find-links ' not in starts[0]
        or 'Installing collected packages:' not in text
        or not text.rstrip().endswith('[install] timeout after '+install[0]+'s')):
        raise ValueError('Log does not establish an offline-wheel installation timeout')
    if float(venv[0])+float(install[0])<599:raise ValueError('Setup timeout does not match the original ten-minute budget')
    return True

def validate(store,ref,record,jobs):
    value=json.loads(read(store,ref));policy=record['rules_public']['policy']
    if (set(value)!=FIELDS or value['kind']!=KIND or value['round_id']!=record['round_id']
        or value['stage']!='final' or value['configuration_sha256']!=record['configuration_sha256']
        or value['preserve_paper_scores'] is not True or policy.get('validation_protocol')!='claims-source-task-v1'
        or policy.get('winner_selection')!='silver_tiebreak' or not isinstance(value['approved_by'],str)
        or not 1<=len(value['approved_by'])<=128 or type(value['approved_at']) not in {int,float}
        or not math.isfinite(value['approved_at']) or value['approved_at']<=0
        or not isinstance(value['records'],list) or not value['records']):raise ValueError('Invalid setup timeout review scope or authorization')
    audit=json.loads(read(store,value['verified_scientific_audit']))
    if audit.get('round_id')!=record['round_id'] or audit.get('stage')!='final' or audit.get('schema_version')!='claims_source_code_audit_v1':raise ValueError('Setup review has a foreign scientific audit')
    by_id={j['job_id']:j for j in jobs};approved={}
    for item in value['records']:
        if set(item)!={'job_id','hotkey','paper_id','package_sha256','log_object_key','original_result','log'}:raise ValueError('Unknown setup-review record fields')
        job=by_id.get(item['job_id'])
        if (job is None or item['job_id'] in approved or job['round_id']!=value['round_id'] or job['stage']!='final'
            or any(job.get(k)!=item[k] for k in ('hotkey','paper_id','package_sha256','log_object_key'))
            or job.get('status')!='completed' or canonical(job['result_private'])!=canonical(item['original_result'])):raise ValueError('Reviewed installation differs from its original sealed job')
        result=job['result_private']
        if (result.get('status')!='build_failed' or result.get('detail')!='package install failed'
            or result.get('artifact') is not None or result.get('prompt_and_completion_tokens')!=0
            or result.get('denied_egress_packets')!=0 or result.get('inference_cost_usd')!=0):raise ValueError('Reviewed job executed or differs from a setup failure')
        verified_timeout(read(store,item['log']));approved[item['job_id']]=item
    return value,approved

def apply(evidence,*,store,ref,record,jobs):
    value,approved=validate(store,ref,record,jobs)
    result=copy.deepcopy(evidence)
    for package in result['packages']:
        own=sorted(k for k,v in approved.items() if v['hotkey']==package['hotkey'])
        original=package['build_passed'];genuine=package['status_counts'].get('build_failed',0)-len(own)
        if genuine<0:raise ValueError('Setup review exceeds original build-failure count')
        package.update(original_build_passed=original,reviewed_setup_timeout_job_ids=own,
            setup_timeout_count=len(own),genuine_build_failure_count=genuine,
            build_passed=bool(package['source_fingerprint'] and package['completed_job_count']>0 and genuine==0))
    result['setup_timeout_review']=ref
    return result
