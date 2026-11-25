"""Frozen harness selection. Historical policies without a descriptor use DSPy."""
import json
import subprocess
from pathlib import Path

from .store import canonical, sha

TOOLS=('claims_read_file','claims_search_files','claims_submit_output')


def hermes_identity(python):
    # Resolve and hash the installed runtime without importing Hermes or loading
    # a developer profile. Paths are operational details, not policy identities.
    script=r'''
import importlib.util,importlib.metadata,json,pathlib,hashlib,subprocess
spec=importlib.util.find_spec('run_agent')
if spec is None:raise ValueError('Hermes runtime is not installed in this interpreter')
root=pathlib.Path(spec.origin).resolve().parent
names=subprocess.check_output(['git','ls-files','*.py'],cwd=root,text=True).splitlines()
files={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names if (root/name).is_file()}
versions={}
for name in ('openai','httpx','pydantic','jsonschema','tiktoken','PyYAML'):
 try:versions[name]=importlib.metadata.version(name)
 except importlib.metadata.PackageNotFoundError:versions[name]=None
import tiktoken._tiktoken as core
files['<tokenizer_binary>']=hashlib.sha256(pathlib.Path(core.__file__).read_bytes()).hexdigest()
raw=json.dumps({'files':files,'dependencies':versions},sort_keys=True,separators=(',',':')).encode()
print(json.dumps({'source_sha256':hashlib.sha256(raw).hexdigest(),'dependencies':versions}))
'''
    result=subprocess.run([str(python),'-c',script],check=True,capture_output=True,text=True,timeout=30)
    return json.loads(result.stdout)


def freeze_harness(mode, *, python=None):
    if mode=='dspy':return None
    if mode!='hermes-files':raise ValueError('Unknown scientific judge harness')
    if not python:raise ValueError('Hermes interpreter required to freeze its runtime identity')
    return {'mode':'hermes-files','version':9,'runtime_identity':hermes_identity(python),
            'max_turns':80,'max_output_tokens':32768,'stage_timeout_seconds':7200,
            'tools':list(TOOLS),'source_context':'same_page_docling_v1',
            'judge_source_corpus':'all_supplied_docling_anchors_v1',
            'matching':'complete_file_corpus_v2','provider_routing':'openrouter_default',
            'context_management':'frozen_route_capacity_v1','audit_ownership':'complete_assertion_groups_v1',
            'artifact_output':'validated_tool_or_final_json_v1','completion':'validated_artifact_with_feedback_v1','submission':'native_stage_shape_v1','artifact_completion_rounds':2,'validation_feedback':'typed_branch_errors_v1','controller_metadata':'read_attested_inspection_v1','validation_recovery':'identical_feedback_three_fresh_retry_v1'}


def validate_harness(value):
    if value is None:return None
    expected={'mode','version','runtime_identity','max_turns','max_output_tokens','stage_timeout_seconds',
              'tools','source_context','matching','provider_routing'}
    if value.get('version') in {2,3,4,5,6,7,8,9}:expected.add('judge_source_corpus')
    if value.get('version') in {4,5,6,7,8,9}:expected.update({'context_management','audit_ownership','artifact_output'})
    if value.get('version') in {5,6,7,8,9}:expected.add('completion')
    if value.get('version') in {6,7,8,9}:expected.update({'submission','artifact_completion_rounds'})
    if value.get('version') in {7,8,9}:expected.add('validation_feedback')
    if value.get('version') in {8,9}:expected.add('controller_metadata')
    if value.get('version')==9:expected.add('validation_recovery')
    if (set(value)!=expected
        or value['mode']!='hermes-files' or (value['version'],value['max_turns']) not in {(1,40),(2,80),(3,80),(4,80),(5,80),(6,80),(7,80),(8,80),(9,80)}
        or value.get('judge_source_corpus','all_supplied_docling_anchors_v1')!='all_supplied_docling_anchors_v1'
        or value['max_output_tokens']!=32768 or value['stage_timeout_seconds']!=(7200 if value['version']>=4 else 600)
        or value['tools']!=list(TOOLS) or value['source_context']!='same_page_docling_v1'
        or value['matching']!=('complete_file_corpus_v2' if value['version']>=3 else 'complete_file_corpus_v1')
        or value.get('completion','validated_artifact_interrupt_v1')!=('validated_artifact_with_feedback_v1' if value['version']>=6 else 'validated_artifact_interrupt_v1')
        or value.get('submission','native_stage_shape_v1')!='native_stage_shape_v1'
        or value.get('artifact_completion_rounds',2)!=2
        or value.get('validation_feedback','typed_branch_errors_v1')!='typed_branch_errors_v1'
        or value.get('context_management','frozen_route_capacity_v1')!='frozen_route_capacity_v1'
        or value.get('audit_ownership','complete_assertion_groups_v1')!='complete_assertion_groups_v1'
        or value.get('artifact_output','validated_tool_or_final_json_v1')!='validated_tool_or_final_json_v1'
        or value.get('controller_metadata','read_attested_inspection_v1')!='read_attested_inspection_v1'
        or value.get('validation_recovery','identical_feedback_three_fresh_retry_v1')!='identical_feedback_three_fresh_retry_v1'
        or value['provider_routing']!='openrouter_default'):
        raise ValueError('Unsupported frozen file-agent harness contract')
    return value


def same_page_context(frame, evidence):
    """Original Docling neighbors are context; owned scientific targets do not change."""
    selected={a['anchor_id'] for a in evidence['anchors']}
    pages={(a.document_uid,a.locator.page_index) for a in frame.anchors
           if a.anchor_id in selected and a.locator.kind=='page'}
    ids=selected|{a.anchor_id for a in frame.anchors if a.locator.kind=='page'
                  and (a.document_uid,a.locator.page_index) in pages}
    return {'anchors':[a.model_dump(mode='json') for a in frame.anchors if a.anchor_id in ids]}
