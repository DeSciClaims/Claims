"""Isolated Hermes process exposing only scoped scientific workspace tools."""
import argparse
import json
import os
from pathlib import Path

from .file_judge import WorkspaceTools, FILE_INSTRUCTIONS, _json,submission_schema,submit_final_json


def execute_tool(handler,arguments,finish=None):
    result=handler(**arguments)
    if finish is not None and (result.get('accepted') is True or result.get('retry_fresh_context') is True):finish()
    return json.dumps(result,ensure_ascii=False)


def run_artifact_agent(agent,prompt,tools,max_rounds=2):
    rounds=[];limit=agent.max_iterations;used=0
    for ordinal in range(max_rounds):
        if used>=limit:break
        # Hermes resets its per-turn budget for a new user message. Preserve the
        # operation's original allowance across completion-feedback turns.
        agent.max_iterations=limit-used
        history=rounds[-1].get('messages',[]) if rounds else None
        if ordinal:
            prompt=('A schema-valid output artifact is still missing. Continue from the original files and inspected evidence. '
                    'Use claims_submit_output with native arguments matching output_schema.json. Do not end with an unfinished plan. '
                    'Use the contract outcome for uncertainty or inability to assess. Validation feedback: '+(getattr(tools,'last_feedback',None) or 'No artifact was submitted.'))
        result=agent.run_conversation(prompt,conversation_history=history);rounds.append(result)
        used+=max(1,result.get('api_calls',0))
        if getattr(tools,'stalled',False):break
        if (tools.root/'output.json').exists() or submit_final_json(result,tools):break
        if result.get('failed') or agent.iteration_budget.remaining<=0:break
    return result,rounds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--max-turns', type=int, required=True)
    parser.add_argument('--max-tokens', type=int, required=True)
    parser.add_argument('--context-window-tokens', type=int)
    parser.add_argument('--reasoning-effort')
    args = parser.parse_args()
    tools = WorkspaceTools(args.workspace)
    diagnostic_policy=json.loads((args.workspace/'request.json').read_text()).get('diagnostic_model_policy')
    if diagnostic_policy is not None:
        from .proprietary_diagnostic import validate
        validate(diagnostic_policy,args.model)
    # Hermes imports can load its checkout's .env. Preserve the explicitly
    # supplied credential instead of silently adopting a developer profile key.
    api_key = os.environ['OPENROUTER_API_KEY']
    from tools.registry import registry
    definitions = [
        ('read_file', 'Read an immutable workspace file, paginated by character offset.',
         {'path': {'type':'string'}, 'offset': {'type':'integer'}, 'limit': {'type':'integer'}}, ['path'], tools.read),
        ('search_files', 'Literal case-insensitive search of retained workspace files; hits are navigation, not proof of absence.',
         {'text': {'type':'string'}, 'prefix': {'type':'string'}, 'offset': {'type':'integer'}, 'limit': {'type':'integer'}}, ['text'], tools.search),
        ('submit_output', 'Submit the scientific response as native function arguments matching output_schema.json; validation feedback is returned for corrections.',
         {'output': {'type':'string'}}, ['output'], tools.submit),
    ]
    names = []
    for name, description, properties, required, handler in definitions:
        # Distinct names cannot accidentally resolve to a global Hermes file tool.
        name = 'claims_'+name; names.append(name)
        def wrapped(arguments, _handler=handler, _finishes=name=='claims_submit_output', **kwargs):
            try:return execute_tool(_handler,{'output':arguments} if _finishes else arguments,(lambda:agent.interrupt()) if _finishes else None)
            except (ValueError, OSError, TypeError) as exc:
                return json.dumps({'error': str(exc)})
        parameters=(submission_schema(tools.schema,json.loads((tools.root/'request.json').read_text())['operation'] if json.loads((tools.root/'request.json').read_text()).get('harness',{}).get('version',0)>=8 else None) if name=='claims_submit_output' else
                    {'type':'object','properties':properties,'required':required,'additionalProperties':False})
        registry.register(name=name, toolset='claims_scientific_files',
                          schema={'name':name, 'description':description,
                                  'parameters':parameters},
                          handler=wrapped, check_fn=lambda: True, max_result_size_chars=40000)
    from run_agent import AIAgent
    os.environ['OPENROUTER_API_KEY'] = api_key
    agent = AIAgent(model=args.model, api_key=api_key,
                    base_url='https://openrouter.ai/api/v1', provider='openrouter',api_mode='chat_completions',
                    enabled_toolsets=['claims_scientific_files'], max_iterations=args.max_turns,
                    max_tokens=args.max_tokens, quiet_mode=True, tool_delay=0,
                    skip_context_files=True, skip_memory=True, load_soul_identity=False,
                    session_db=None, save_trajectories=True, fallback_model=None,
                    request_overrides={'temperature':0},
                    tool_start_callback=lambda *a,**k: None)
    # Use Hermes's own transport switch, rather than changing a streaming API
    # body behind its response consumer. Native usage is captured per response.
    agent._disable_streaming=True
    if args.context_window_tokens is not None:
        compressor=agent.context_compressor
        if compressor.context_length!=args.context_window_tokens or compressor.max_tokens!=args.max_tokens:
            raise ValueError('Hermes context management differs from the frozen route/output reservation')
        _json(args.workspace/'runtime_context.json',{'context_window_tokens':compressor.context_length,
            'reserved_output_tokens':compressor.max_tokens,'compression_threshold_tokens':compressor.threshold_tokens,
            'summary_model':args.model})
    # Fail closed if a Hermes release injects any broader tool into this profile.
    offered = {t['function']['name'] for t in agent.tools}
    if offered != set(names): raise ValueError('Unexpected Hermes tool surface: '+repr(offered))
    if agent.api_key != api_key or agent.base_url.rstrip('/') != 'https://openrouter.ai/api/v1':
        raise ValueError('Hermes changed the explicitly configured provider credentials or endpoint')
    prompt = FILE_INSTRUCTIONS + '\nTool names have a claims_ prefix. Workspace: '+str(args.workspace)
    audit=None
    if args.context_window_tokens:
        from .file_provider import ProviderAudit
        audit=ProviderAudit(args.workspace,model=args.model,context_window=args.context_window_tokens,
                            max_tokens=args.max_tokens,api_key=api_key,reasoning_effort=args.reasoning_effort,diagnostic_policy=diagnostic_policy)
        audit.install()
    try:result,rounds = run_artifact_agent(agent,prompt,tools)
    finally:
        if audit is not None:_json(args.workspace/'provider_usage.json',audit.summary())
    # Keep every returned tool/model message for replay and usage investigation.
    _json(args.workspace/'conversation.json', result)
    _json(args.workspace/'conversation_rounds.json',rounds)
    usage = {k:result.get(k) for k in ('input_tokens','output_tokens','prompt_tokens','completion_tokens',
              'cache_read_tokens','cache_write_tokens','reasoning_tokens','total_tokens','api_calls',
              'actual_cost_usd','estimated_cost_usd','cost_status','cost_source','model','provider','completed')}
    _json(args.workspace/'usage.json', usage)
    if tools.stalled:raise ValueError('Repeated validation failure requires a fresh-context controller retry: '+tools.last_feedback)
    if not (args.workspace/'output.json').is_file():
        from .file_judge import submit_final_json
        if not submit_final_json(result,tools):raise ValueError('Hermes did not submit a valid scientific response')


if __name__ == '__main__': main()
