"""Hermes implementation of the scientific operation runtime, with retained replay."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from .contracts import FileRef
from .file_judge import FileJudge
from .store import canonical,sha


class FileAgentRuntime:
    def __init__(self,*,store,route,harness,python,api_key,reasoning_effort=None,diagnostic_policy=None):
        self.store=store;self.route=route;self.harness=harness;self.python=python
        self.api_key=api_key;self.reasoning_effort=reasoning_effort
        self.model=route.model;self.max_tokens=route.max_output_tokens;self.usage=[];self.deadline=None;self.diagnostic_policy=diagnostic_policy

    def scoped(self):
        child=copy.copy(self);child.usage=[];return child

    def set_deadline(self,deadline):self.deadline=deadline

    def generate(self,*,request,instructions,payload,output_dir,stage):
        if request['harness']!=self.harness or request['route']!=self.route.model_dump(mode='json'):
            raise ValueError('File operation differs from its frozen runtime')
        attempted={**request}
        if payload.get('validation_feedback'):attempted['validation_feedback']=payload['validation_feedback']
        timeout=min(self.harness['stage_timeout_seconds'],max(0,self.deadline-time.time())) if self.deadline else self.harness['stage_timeout_seconds']
        judge=FileJudge(python=self.python,model=self.model,max_turns=self.harness['max_turns'],
                        max_tokens=self.max_tokens,timeout=timeout,diagnostic_policy=self.diagnostic_policy)
        call_id=uuid.uuid4().hex;output_dir=Path(output_dir);output_dir.mkdir(parents=True,exist_ok=True)
        started=time.monotonic()
        try:
            answer,root=judge.run(attempted,output_dir/'file-workspaces',api_key=self.api_key,
                                  context_window_tokens=self.route.context_window_tokens,reasoning_effort=self.reasoning_effort)
            return answer
        finally:
            root=getattr(judge,'last_workspace',None)
            observed={};trace=None
            if root is not None:
                usage_path=root/'provider_usage.json'
                if usage_path.is_file():observed=json.loads(usage_path.read_text())
                trace=retain_trace(self.store,root,attempted)
                if (root/'output.json').is_file():
                    (output_dir/(call_id+'.response.txt')).write_bytes((root/'output.json').read_bytes())
            self.usage.append({'stage':stage,'call_id':call_id,'model':self.model,
                'provider':'openrouter','provider_request_id':None,'file_agent_trace':trace.model_dump(mode='json') if trace else None,
                'usage':{k:observed[k] for k in ('prompt_tokens','completion_tokens','cost') if observed.get(k) is not None},
                'elapsed_seconds':time.monotonic()-started})


def retain_trace(store,root,request):
    """Chunk large conversations without losing bytes or relying on VM-local paths."""
    manifest=json.loads((root/'input_manifest.json').read_text())
    names={*manifest['files'],'input_manifest.json','output.json','tool_events.jsonl',
           'result_manifest.json','access_audit.json','conversation.json','conversation_rounds.json','usage.json',
           'provider_calls.jsonl','provider_usage.json','stdout.log','stderr.log',
           'runtime_context.json','hermes-home/config.yaml','model_submission.json','submission_receipt.json'}
    files=[]
    for name in sorted(names):
        path=root/name
        if not path.is_file():continue
        raw=path.read_bytes()
        parts=[store.put_named('file-agent/'+name+f'.part-{i}',raw[i:i+1000000],'application/octet-stream')
               for i in range(0,len(raw),1000000)]
        files.append({'path':name,'sha256':sha(raw),'byte_length':len(raw),'parts':parts})
    return store.put({'kind':'claims_file_agent_trace_v1','request_sha256':sha(canonical(request)),
                      'harness':request['harness'],'files':files})


def replay_trace(store,ref,request,parsed=None):
    from .file_judge import validate_inspection
    from .runtime import StrictAdapter
    trace=json.loads(store.read(ref))
    if (trace['kind']!='claims_file_agent_trace_v1' or trace['request_sha256']!=sha(canonical(request))
        or trace['harness']!=request['harness']):raise ValueError('File trace request/harness changed')
    with tempfile.TemporaryDirectory(prefix='claims-file-replay-') as work:
        root=Path(work);seen=set()
        for file in trace['files']:
            name=file['path'];path=root/name
            if Path(name).is_absolute() or '..' in Path(name).parts or name in seen:
                raise ValueError('Unsafe or duplicate file trace path')
            seen.add(name);raw=b''.join(store.read(FileRef.model_validate(p,strict=True)) for p in file['parts'])
            if len(raw)!=file['byte_length'] or sha(raw)!=file['sha256']:raise ValueError('File trace bytes changed')
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        if json.loads((root/'request.json').read_text())!=request:raise ValueError('File workspace changed its request')
        if parsed is not None:
            import jsonschema
            wire=json.loads((root/'output.json').read_text())
            jsonschema.Draft202012Validator(request['stage_schema']).validate(wire)
            decoded=StrictAdapter(request['stage_schema']).parse(None,json.dumps(wire))
            if canonical(decoded)!=store.read(parsed):raise ValueError('File verdict changed after submission')
            validate_inspection(root,request)
            if request['harness'].get('version',0)>=8:
                from .file_judge import attest_submission
                original=json.loads((root/'model_submission.json').read_text())
                reconstructed,added=attest_submission(root,request,original)
                if reconstructed!=wire or json.loads((root/'submission_receipt.json').read_text())!={'controller_fields':added}:
                    raise ValueError('Read-attested submission metadata changed')
            if request['harness'].get('version',0)>=4:
                observed=json.loads((root/'runtime_context.json').read_text())
                if (observed['context_window_tokens']!=request['route']['context_window_tokens']
                    or observed['reserved_output_tokens']!=request['route']['max_output_tokens']
                    or not 0<observed['compression_threshold_tokens']<observed['context_window_tokens']-observed['reserved_output_tokens']):
                    raise ValueError('File context management differs from its frozen capacity')
            events=[json.loads(line) for line in (root/'provider_calls.jsonl').read_text().splitlines()]
            if not events:raise ValueError('Successful file judgment has no native provider audit')
            from .harness import TOOLS
            for event in events:
                if (event['model']!=request['route']['model'] or event['max_tokens']!=request['route']['max_output_tokens']
                    or event['temperature']!=(None if request.get('diagnostic_model_policy') else 0) or event['provider']!={'require_parameters':True,'allow_fallbacks':True}
                    or {t['function']['name'] for t in event['tools']}-set(TOOLS)):
                    raise ValueError('File judge native call differs from frozen model/tools/provider policy')
                if request['harness'].get('version',0)>=4 and event.get('api_base')!='https://openrouter.ai/api/v1':
                    raise ValueError('File judge native endpoint differs from its frozen provider')
                version=request['harness'].get('version',0)
                if version>=5 and event['tools'] and event.get('tool_choice')!=('auto' if version>=6 else 'required'):
                    raise ValueError('File judge tool selection differs from its frozen policy')
                if version>=6:
                    from .file_judge import submission_schema
                    for tool in event['tools']:
                        if tool['function']['name']=='claims_submit_output' and tool['function']['parameters']!=submission_schema(request['stage_schema'],request['operation'] if version>=8 else None):
                            raise ValueError('Native submission schema differs from its retained stage contract')
        return {'files':len(seen),'request_sha256':trace['request_sha256']}


def replay_run_trace(store,run,request):
    if not request.get('harness'):return
    head=store.head('provider-usage:'+run.run_id)
    if head is None:raise ValueError('File judge lacks retained usage/trace')
    usage=json.loads(store.get_blob(head))
    if not usage.get('file_agent_trace'):raise ValueError('File judge lost its workspace trace')
    return replay_trace(store,FileRef.model_validate(usage['file_agent_trace'],strict=True),request,run.parsed_response)
