"""Check fixed route parameters, strict output format and image delivery.

This capability check does not establish scientific accuracy or permit activation.
"""
from pathlib import Path
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import secrets
from PIL import Image
from validator.scientific_result_v1.source_tasks.runtime import SourceTaskRuntime
from validator.scientific_result_v1.source_tasks.configuration import MODELS


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshots',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--reasoning-effort',default='none')
    parser.add_argument('--model',action='append')
    parser.add_argument('--text-only',action='store_true',help='Check captured text instead of image capability')
    args=parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file)
    key=os.environ.get('OPENROUTER_API_KEY')
    if not key:raise ValueError('OPENROUTER_API_KEY is required')
    root=args.output_dir.resolve();root.mkdir(parents=True,exist_ok=True)
    schema={'type':'object','properties':{'color':{'type':'string','enum':['red','green','blue']}},'required':['color'],'additionalProperties':False}
    def run(model):
        name=model.replace('/','--');snapshot=json.loads((args.snapshots/(name+'.json')).read_text())
        snapshot=snapshot.get('data',snapshot)
        preferences={'require_parameters':True,'only':sorted({e['provider_name'] for e in snapshot['endpoints'] if 'structured_outputs' in e.get('supported_parameters',[])})}
        color=secrets.choice(['red','green','blue'])
        if args.text_only:
            evidence={'source_text':'The swatch color is '+color+'.'}
            instructions='Report the color stated in the supplied source text. Return one strict JSON object matching stage_schema.'
        else:
            path=root/(name+'.png');Image.new('RGB',(32,32),color).save(path)
            evidence={'source_images':[{'path':str(path),'source_id':'capability-preflight','page':1}]}
            instructions='Report the color of the supplied image. Return one strict JSON object matching stage_schema.'
        runtime=SourceTaskRuntime(model=model,api_key=key,provider_preferences=preferences,reasoning_effort=args.reasoning_effort)
        try:
            answer=runtime.generate(instructions=instructions,
                payload={'stage_schema':schema,**evidence},
                output_dir=root/'runs'/name,stage='image-and-format-preflight')
            report={'model':model,'mode':'text' if args.text_only else 'image','max_output_tokens':runtime.max_tokens,'passed':answer=={'color':color},'expected':color,'answer':answer,'usage':runtime.usage}
        except Exception as exc:
            report={'model':model,'passed':False,'error_type':type(exc).__name__,'error_message':str(exc).replace(key,'[REDACTED]')[:2000],'usage':runtime.usage}
        (root/(name+'.json')).write_text(json.dumps(report,indent=2))
        return {'model':model,'passed':report['passed']}
    with ThreadPoolExecutor(max_workers=3) as executor:
        from validator.scientific_result_v1.source_tasks.runner import APPROVED_MODELS
        models=set(args.model or MODELS.values())
        if not models<=APPROVED_MODELS:raise ValueError('Unapproved preflight model')
        reports=list(executor.map(run,sorted(models)))
    print(json.dumps(reports))
    if not all(r['passed'] for r in reports):raise SystemExit(1)

if __name__=='__main__':main()
