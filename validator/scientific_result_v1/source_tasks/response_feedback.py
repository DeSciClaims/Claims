"""Bounded schema feedback for model-authored responses; never repair an answer."""
import json


def schema_feedback(error,root):
    def resolved(spec):
        while '$ref' in spec and spec['$ref'].startswith('#/'):
            value=root
            for part in spec['$ref'][2:].split('/'):
                value=value[part.replace('~1','/').replace('~0','~')]
            spec=value
        return spec

    def compatible(spec,value):
        spec=resolved(spec)
        if 'const' in spec and value!=spec['const']:return False
        if 'enum' in spec and value not in spec['enum']:return False
        if isinstance(value,dict):
            for name,child in spec.get('properties',{}).items():
                if name not in value:continue
                child=resolved(child)
                if 'const' in child and value[name]!=child['const']:return False
                if name in {'outcome','status','kind','code','proposal_kind'} and 'enum' in child and value[name] not in child['enum']:return False
                if isinstance(value[name],list):
                    if len(value[name])<child.get('minItems',0):return False
                    if 'maxItems' in child and len(value[name])>child['maxItems']:return False
        return True

    def leaves(issue):
        children=issue.context
        if children and issue.validator in {'anyOf','oneOf'}:
            branches=issue.schema[issue.validator]
            selected={i for i,branch in enumerate(branches) if compatible(branch,issue.instance)}
            relevant=[e for e in children if e.schema_path and e.schema_path[0] in selected]
            if relevant:children=relevant
        if children:
            for child in children:yield from leaves(child)
        else:yield issue

    messages=[];seen=set()
    for issue in leaves(error):
        path='/'+'/'.join(str(p).replace('~','~0').replace('/','~1') for p in issue.absolute_path)
        if issue.validator=='enum':
            rendered=json.dumps(issue.instance,ensure_ascii=False)
            message='value '+rendered[:100]+' is outside the allowed set'
            if isinstance(issue.instance,str):
                ancestors=[p for p in issue.validator_value if isinstance(p,str) and issue.instance.startswith(p+'/')]
                if ancestors:message+='; allowed ancestor '+max(ancestors,key=len)+' may be used only if it represents your finding'
        elif issue.validator=='const':message='must copy the exact fixed binding from the request'
        elif issue.validator in {'minItems','maxItems','type','minLength','maxLength'}:
            message=issue.validator+'='+str(issue.validator_value)[:120]
        elif issue.validator in {'anyOf','oneOf'}:message='must match one complete declared response variant'
        else:message=issue.message[:180]
        item=path+': '+message
        if item not in seen:messages.append(item);seen.add(item)
        if len(messages)==5:break
    return ('Response schema validation failed: '+'; '.join(messages))[:1600]
