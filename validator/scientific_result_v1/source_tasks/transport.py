"""Lossless judge transport; the rich immutable audit packet remains unchanged."""
import json
import re
from .contracts import AssertionPacket

ENCODING = 'claims_property_columns_v5'
V4 = 'claims_property_columns_v4'


def _factor_references(value):
    """Intern repeated exact output refs; their literal pointers remain in the pool."""
    from collections import Counter
    counts=Counter();originals={}
    def walk(node):
        if isinstance(node,dict):
            if node.get('kind') in {'json','csv'}:
                key=json.dumps(node,sort_keys=True,separators=(',',':'))
                counts[key]+=1;originals[key]=node
            else:
                for child in node.values():walk(child)
        elif isinstance(node,list):
            for child in node:walk(child)
    walk(value)
    selected={key:'J'+str(i) for i,(key,count) in enumerate(counts.items())
        if count*(len(key)-23)>len(key)+15}
    if not selected:return
    def encode(node):
        if isinstance(node,dict):
            if node.get('kind') in {'json','csv'}:
                key=json.dumps(node,sort_keys=True,separators=(',',':'))
                if key in selected:return {'output_ref':selected[key]}
                return node
            return {k:encode(v) for k,v in node.items()}
        if isinstance(node,list):return [encode(v) for v in node]
        return node
    encoded=encode(value)
    value.clear();value.update(encoded)
    value['output_reference_pool']={ref:originals[key] for key,ref in selected.items()}


def _factor_structure(value):
    """Keep original properties once; derive only exactly identical view fields."""
    originals={}
    for node in value['objects']+value.get('context_headers',[]):
        properties={p['name']:p for p in node['properties'] if p['present']}
        originals[node['original_ref']['pointer']]=properties
        prop=properties.get('id')
        if prop is not None and json.loads(prop['value_json'])==node['object_id']:
            del node['object_id'];node['object_id_from_property']=True
    for node in value['relations']:
        properties={p['name']:p for p in node['properties'] if p['present']}
        derived=[]
        for name in ('source_id','target_id','relation','closure_direction'):
            prop=properties.get(name)
            if prop is not None and json.loads(prop['value_json'])==node[name]:
                del node[name];derived.append(name)
        if derived:node['headers_from_properties']=derived
    for closure in value['closures']:
        pointer=closure['original_ref']['pointer']
        prop=originals.get(pointer.rsplit('/closure',1)[0],{}).get('closure')
        if prop is None:continue
        native=json.loads(prop['value_json'])
        derived={}
        for name,key in (('submitted_core_ids','core_ids'),('submitted_member_ids','member_ids'),('submitted_reference_ids','reference_ids')):
            if isinstance(native,dict) and closure[name]==native.get(key):
                derived[name]=key
        equal=[]
        for name,submitted in (('recomputed_member_ids','submitted_member_ids'),('recomputed_reference_ids','submitted_reference_ids')):
            if closure[name]==closure[submitted]:
                del closure[name];equal.append(name)
        for name in derived:del closure[name]
        if derived:closure['submitted_from_original_closure']=derived
        if equal:closure['computed_sets_equal_submitted']=equal


def _reference_alias(value,alias,*,restore=False):
    if isinstance(value,dict):
        if value.get('kind') in {'json','csv'}:
            if restore:
                if 'output_alias' not in value:value['output_alias']=alias
            elif value.get('output_alias')==alias:del value['output_alias']
        for child in value.values():_reference_alias(child,alias,restore=restore)
    elif isinstance(value,list):
        for child in value:_reference_alias(child,alias,restore=restore)


def _factor_literal_identifiers(value,packet):
    """Intern repeated administrative IDs inside original JSON literal strings."""
    from collections import Counter
    identifiers={o.object_id for o in packet.objects+packet.context_headers}
    identifiers.update(k for e in packet.relations for k in (e.source_id,e.target_id))
    identifiers.update(k for c in packet.closures for keys in
        (c.submitted_core_ids,c.submitted_member_ids,c.submitted_reference_ids,c.recomputed_member_ids,c.recomputed_reference_ids) for k in keys)
    identifiers={s for s in identifiers if len(s)>=12 and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*',s)}
    if not identifiers:return
    pattern=re.compile('|'.join(re.escape(s) for s in sorted(identifiers,key=lambda x:(-len(x),x))))
    nodes=value['objects']+value['relations']+value.get('context_headers',[])
    literals=[literal for node in nodes for _,literal in node['properties'] if isinstance(literal,str)]
    literals.extend(value['literal_pool'].values())
    counts=Counter(m.group() for literal in literals for m in pattern.finditer(literal))
    selected=sorted(s for s,count in counts.items() if count>=3)
    if not selected:return
    prefix='~'
    while any(re.search(re.escape(prefix)+r'\d+;',literal) for literal in literals):prefix+='~'
    pool={s:str(i) for i,s in enumerate(selected)}
    def encode(literal):
        return pattern.sub(lambda m:prefix+pool[m.group()]+';' if m.group() in pool else m.group(),literal)
    for node in nodes:
        node['properties']=[[name,encode(literal) if isinstance(literal,str) else literal] for name,literal in node['properties']]
    value['literal_pool']={key:encode(literal) for key,literal in value['literal_pool'].items()}
    value['literal_identifier_prefix']=prefix
    value['literal_identifier_pool']={key:identifier for identifier,key in pool.items()}


def judge_packet(packet):
    """Remove repeated property-key/reference syntax, never scientific assertions."""
    value = packet.model_dump(mode='json')
    value['assertion_encoding'] = ENCODING
    _factor_structure(value)
    for node in value['objects'] + value['relations'] + value.get('context_headers', []):
        pairs = []
        overrides = {}
        root = node['original_ref']
        for prop in node['properties']:
            name = prop['name']
            pairs.append([name, prop['value_json']])
            expected = {**root, 'pointer': root['pointer'] + '/' + name.replace('~', '~0').replace('/', '~1')}
            if prop['present'] and prop['original_ref'] != expected:
                overrides[name] = prop['original_ref']
        node['properties'] = pairs
        node['property_ref_overrides'] = overrides
    from collections import Counter
    nodes=value['objects']+value['relations']+value.get('context_headers',[])
    counts=Counter(literal for node in nodes for _,literal in node['properties'] if isinstance(literal,str) and len(literal)>64)
    pool={literal:'L'+str(i) for i,(literal,count) in enumerate(counts.items()) if count>1}
    for node in nodes:
        node['properties']=[[name,{'literal_ref':pool[literal]} if isinstance(literal,str) and literal in pool else literal] for name,literal in node['properties']]
    value['literal_pool']={key:literal for literal,key in pool.items()}
    _factor_literal_identifiers(value,packet)
    name_counts=Counter(tuple(name for name,_ in node['properties']) for node in nodes)
    sets={names:'N'+str(i) for i,(names,count) in enumerate(name_counts.items()) if count>1}
    for node in nodes:
        names=tuple(name for name,_ in node['properties'])
        if names in sets:
            node['property_names_ref']=sets[names]
            node['properties']=[literal for _,literal in node['properties']]
    value['property_name_sets']={key:list(names) for names,key in sets.items()}
    _reference_alias(value,value['output_alias'])
    value['reference_output_alias']=value['output_alias']
    _factor_references(value)
    return value


def audit_packet(value):
    """Decode for exact round-trip checks and replay; JSON literal strings stay exact."""
    import copy
    value = copy.deepcopy(value)
    encoding=value.pop('assertion_encoding')
    if encoding not in {ENCODING,V4,'claims_property_columns_v3','claims_property_pairs_v1','claims_property_pairs_v2'}:
        raise ValueError('Unknown scientific property encoding')
    if encoding==ENCODING:
        pool=value.pop('output_reference_pool',{})
        if not isinstance(pool,dict) or any(not isinstance(k,str) or not isinstance(v,dict) or v.get('kind') not in {'json','csv'} for k,v in pool.items()):
            raise ValueError('Invalid original output-reference pool')
        def expand(node):
            if isinstance(node,dict):
                if 'output_ref' in node:
                    if set(node)!={'output_ref'} or node['output_ref'] not in pool:
                        raise ValueError('Unknown original output reference')
                    return copy.deepcopy(pool[node['output_ref']])
                return {k:expand(v) for k,v in node.items()}
            if isinstance(node,list):return [expand(v) for v in node]
            return node
        value=expand(value)
    if encoding in {ENCODING,V4}:
        alias=value.pop('reference_output_alias')
        if alias!=value['output_alias']:raise ValueError('Transport reference alias differs')
        _reference_alias(value,alias,restore=True)
    pool=value.pop('literal_pool',{})
    if not isinstance(pool,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in pool.items()):
        raise ValueError('Invalid original-literal pool')
    identifiers=value.pop('literal_identifier_pool',{})
    prefix=value.pop('literal_identifier_prefix',None)
    if not isinstance(identifiers,dict):
        raise ValueError('Invalid original-identifier pool')
    if identifiers:
        if (encoding not in {ENCODING,V4} or not isinstance(prefix,str) or not prefix
            or not isinstance(identifiers,dict) or any(not isinstance(k,str) or not k.isdecimal() or not isinstance(v,str) for k,v in identifiers.items())):
            raise ValueError('Invalid original-identifier pool')
        pattern=re.compile(re.escape(prefix)+r'(\d+);')
        def decode_literal(literal):
            def replace(match):
                key=match.group(1)
                if key not in identifiers:raise ValueError('Unknown original identifier reference')
                return identifiers[key]
            return pattern.sub(replace,literal)
    else:
        if prefix is not None:raise ValueError('Identifier prefix has no pool')
        decode_literal=lambda literal:literal
    name_sets=value.pop('property_name_sets',{})
    if not isinstance(name_sets,dict) or any(not isinstance(names,list) or any(not isinstance(n,str) for n in names) or len(names)!=len(set(names)) for names in name_sets.values()):
        raise ValueError('Invalid property-name set')
    for node in value['objects'] + value['relations'] + value.get('context_headers', []):
        if 'property_names_ref' in node:
            key=node.pop('property_names_ref')
            if key not in name_sets or len(node['properties'])!=len(name_sets[key]):
                raise ValueError('Unknown property-name set or mismatched value count')
            node['properties']=list(zip(name_sets[key],node['properties']))
        properties = []
        root = node['original_ref']
        overrides = node.pop('property_ref_overrides')
        names = set()
        for name, literal in node['properties']:
            if isinstance(literal,dict):
                if set(literal)!={'literal_ref'} or literal['literal_ref'] not in pool:
                    raise ValueError('Unknown original literal reference')
                literal=pool[literal['literal_ref']]
            if isinstance(literal,str):literal=decode_literal(literal)
            if name in names:
                raise ValueError('Duplicate transported property')
            names.add(name)
            present = literal is not None
            ref = overrides.get(name, {**root, 'pointer': root['pointer'] + '/' + name.replace('~', '~0').replace('/', '~1')}) if present else None
            properties.append(dict(name=name, present=present, value_json=literal, original_ref=ref))
        if not set(overrides) <= names:
            raise ValueError('Unknown property reference override')
        node['properties'] = properties
    if encoding in {ENCODING,V4}:
        originals={}
        for node in value['objects']+value.get('context_headers',[]):
            props={p['name']:p for p in node['properties'] if p['present']}
            originals[node['original_ref']['pointer']]=props
            if node.pop('object_id_from_property',False):
                if 'object_id' in node:raise ValueError('Ambiguous transported object identity')
                node['object_id']=json.loads(props['id']['value_json'])
        for node in value['relations']:
            props={p['name']:p for p in node['properties'] if p['present']}
            for name in node.pop('headers_from_properties',[]):
                if name not in {'source_id','target_id','relation','closure_direction'} or name in node:
                    raise ValueError('Ambiguous transported relation header')
                node[name]=json.loads(props[name]['value_json'])
        names={'submitted_core_ids':'core_ids','submitted_member_ids':'member_ids','submitted_reference_ids':'reference_ids'}
        computed={'recomputed_member_ids':'submitted_member_ids','recomputed_reference_ids':'submitted_reference_ids'}
        for closure in value['closures']:
            derived=closure.pop('submitted_from_original_closure',{})
            if derived:
                props=originals[closure['original_ref']['pointer'].rsplit('/closure',1)[0]]
                native=json.loads(props['closure']['value_json'])
                for name,key in derived.items():
                    if names.get(name)!=key or name in closure:raise ValueError('Ambiguous transported submitted closure')
                    closure[name]=native[key]
            for name in closure.pop('computed_sets_equal_submitted',[]):
                if name not in computed or name in closure:raise ValueError('Ambiguous transported computed closure')
                closure[name]=list(closure[computed[name]])
    return AssertionPacket.model_validate(value, strict=True)


def judge_evidence(evidence):
    """Store each exact document identity once; keep every original anchor/locator."""
    documents = {}
    anchors = []
    for anchor in evidence['anchors']:
        uid, digest = anchor['document_uid'], anchor['source_sha256']
        if uid in documents and documents[uid] != digest:
            raise ValueError('Source document identity has conflicting hashes')
        documents[uid] = digest
    identities={uid:'D'+str(i) for i,uid in enumerate(documents)}
    for anchor in evidence['anchors']:
        anchors.append({**{key:value for key,value in anchor.items() if key not in {'document_uid','source_sha256'}},
            'document_ref':identities[anchor['document_uid']]})
    return {**evidence,'evidence_encoding':'claims_source_documents_v2',
        'documents':{key:{'document_uid':uid,'source_sha256':documents[uid]} for uid,key in identities.items()},'anchors':anchors}


def audit_evidence(value):
    if value.get('evidence_encoding')=='claims_source_documents_v2':
        documents=value['documents']
        if not isinstance(documents,dict) or any(set(d)!={'document_uid','source_sha256'} for d in documents.values()):
            raise ValueError('Invalid source document identity pool')
        anchors=[]
        for anchor in value['anchors']:
            key=anchor.get('document_ref')
            if key not in documents or 'document_uid' in anchor or 'source_sha256' in anchor:
                raise ValueError('Unknown or ambiguous source document reference')
            anchors.append({**{k:v for k,v in anchor.items() if k!='document_ref'},**documents[key]})
        return {**{k:v for k,v in value.items() if k not in {'evidence_encoding','documents','anchors'}},'anchors':anchors}
    if value.get('evidence_encoding') != 'claims_document_sha_v1':
        raise ValueError('Unknown source evidence encoding')
    documents = value['document_source_sha256']
    anchors = [{**anchor, 'source_sha256':documents[anchor['document_uid']]} for anchor in value['anchors']]
    return {**{k:v for k,v in value.items() if k not in {'evidence_encoding','document_source_sha256','anchors'}},
            'anchors':anchors}
