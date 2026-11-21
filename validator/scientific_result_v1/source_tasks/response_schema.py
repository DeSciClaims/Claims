"""Request-specific constraints for strict provider generation, without repairing decisions."""
import copy


def _nonnull(prop):
    variants=prop.get('anyOf')
    if variants:
        options=[v for v in variants if v.get('type')!='null']
        return options[0] if len(options)==1 else {'anyOf':options}
    return prop


def response_schema(response_type,data):
    schema=copy.deepcopy(response_type.model_json_schema())
    props=schema.get('properties',{})
    nested=data.get('request',{})
    bindings={key:data.get(key,nested.get(key)) for key in ('request_id','proposal_kind','proposal_hash','tile_id','table_uid','chunk_id','output_alias')}
    task=data.get('task',{});packet=data.get('packet',{})
    if task:bindings['task_id']=task.get('task_id')
    if packet:
        bindings['packet_id']=packet.get('packet_id');bindings['output_alias']=packet.get('output_alias')
    for key,value in bindings.items():
        if value is not None and key in props:props[key]['const']=value
    defs=schema.get('$defs',{})
    if response_type.__name__=='MatchResponse':
        ids=data.get('inspection_component_ids',list(data.get('components',{})))
        props['inspected_component_ids']['const']=ids
        required=data.get('required_requirement_ids',[r['requirement_id'] for r in task.get('requirements',[])])
        # A keyed object enforces exactly one provider answer per requirement,
        # without unsupported array-uniqueness grammar or duplicate-ID slots.
        props['matches']={'type':'object','properties':{key:{'type':'array','items':{'$ref':'#/$defs/JsonRef'}} for key in required},'required':required,'additionalProperties':False}
        defs['RequirementMatch']['properties']['requirement_id']['enum']=required
        defs['RequirementMatch']['properties']['output_refs']['items']={'$ref':'#/$defs/JsonRef'}
        if 'JsonRef' in defs and data.get('output_alias') is not None:defs['JsonRef']['properties']['output_alias']['const']=data['output_alias']
        if task.get('scope_kind')=='SOURCE_CONTEXT' and 'JsonRef' in defs:
            nodes=[*data.get('components',{}).values(),*data.get('context_headers',{}).values()]
            pointers=sorted({p['original_ref']['pointer'] for node in nodes for p in node.get('properties',[]) if p.get('present') and p.get('original_ref')})
            if pointers:defs['JsonRef']['properties']['pointer']['enum']=pointers
            else:
                for specification in props['matches']['properties'].values():specification['maxItems']=0
        if data.get('scan_scope')=='TABLE_IDENTITY_ONLY':
            defs['JsonRef']['properties']['pointer']['enum']=sorted(data['components'])
        return schema
    if response_type.__name__!='JudgeResponse':return schema
    from .transport import audit_packet
    case=audit_packet(packet)
    roots={o.original_ref.pointer for o in case.objects}|{e.original_ref.pointer for e in case.relations}
    # A counterexample can cite a shown property root and describe its precise nested
    # discrepancy. It never needs to invent a new pointer into an unseen value.
    pointers=roots|{p.original_ref.pointer for o in [*case.objects,*case.relations,*case.context_headers] for p in o.properties if p.present}
    if pointers:defs['JsonRef']['properties']['pointer']['enum']=sorted(pointers)
    defs['JsonRef']['properties']['output_alias']['const']=case.output_alias
    csv=defs.get('CsvRef')
    if csv and case.csv_blocks:
        csv['properties']['output_alias']['const']=case.output_alias
        csv['properties']['filename']['enum']=sorted({b.filename for b in case.csv_blocks})
        csv['properties']['row']['enum']=sorted({0}|{row for b in case.csv_blocks for row in range(b.first_row,b.first_row+len(b.rows))})
        csv['properties']['column']['enum']=list(range(max(len(b.header) for b in case.csv_blocks)))
    else:defs['Counterexample']['properties']['output_refs']['items']={'$ref':'#/$defs/JsonRef'}
    required=list(data['required_dimensions'])
    for branch in defs['DimensionCheck'].get('anyOf',[defs['DimensionCheck']]):
        branch['properties']['dimension']['enum']=required
    anchors=[a['anchor_id'] for a in data['authoritative_evidence']['anchors']]
    for name in ('Counterexample','DimensionCheck'):
        for branch in defs[name].get('anyOf',[defs[name]]):
            branch['properties']['evidence_anchor_ids']['items']['enum']=anchors
    reqs=[r['requirement_id'] for r in task.get('requirements',[])]
    missing=defs['Counterexample']['properties']['missing_requirement_id']
    missing['enum']=[None,*reqs]
    # Expose the existing witness requirement to the provider, too.
    counter=defs['Counterexample'];counter_branches=[]
    for use_output in (True,False):
        branch=copy.deepcopy(counter)
        branch['properties']['output_refs']['minItems' if use_output else 'maxItems']=1 if use_output else 0
        if not use_output:branch['properties']['missing_requirement_id']={**_nonnull(missing),'enum':reqs}
        if use_output or reqs:counter_branches.append(branch)
    defs['Counterexample']={'anyOf':counter_branches}
    base=copy.deepcopy(schema);base.pop('$defs',None)
    branches=[]
    for outcome in ('ASSESSED','NEED_EVIDENCE','TARGET_DEFECT','SOURCE_UNRESOLVED','TECHNICAL_FAILURE'):
        branch=copy.deepcopy(base);p=branch['properties'];p['outcome']={'type':'string','const':outcome}
        p['checks'].update(minItems=len(required) if outcome=='ASSESSED' else 0,maxItems=len(required) if outcome=='ASSESSED' else 0)
        p['evidence_requests'].update(**({'minItems':1} if outcome=='NEED_EVIDENCE' else {'maxItems':0}))
        for key,active in [('target_issue','TARGET_DEFECT'),('unresolved_reason','SOURCE_UNRESOLVED'),('technical_error','TECHNICAL_FAILURE')]:
            p[key]=_nonnull(p[key]) if outcome==active else {'type':'null','const':None}
        branches.append(branch)
    return {**base,'$defs':defs,'anyOf':branches}
