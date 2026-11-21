"""Lossless judge transport; the rich immutable audit packet remains unchanged."""
from .contracts import AssertionPacket

ENCODING = 'claims_property_columns_v3'


def judge_packet(packet):
    """Remove repeated property-key/reference syntax, never scientific assertions."""
    value = packet.model_dump(mode='json')
    value['assertion_encoding'] = ENCODING
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
    name_counts=Counter(tuple(name for name,_ in node['properties']) for node in nodes)
    sets={names:'N'+str(i) for i,(names,count) in enumerate(name_counts.items()) if count>1}
    for node in nodes:
        names=tuple(name for name,_ in node['properties'])
        if names in sets:
            node['property_names_ref']=sets[names]
            node['properties']=[literal for _,literal in node['properties']]
    value['property_name_sets']={key:list(names) for names,key in sets.items()}
    return value


def audit_packet(value):
    """Decode for exact round-trip checks and replay; JSON literal strings stay exact."""
    import copy
    value = copy.deepcopy(value)
    encoding=value.pop('assertion_encoding')
    if encoding not in {ENCODING,'claims_property_pairs_v1','claims_property_pairs_v2'}:
        raise ValueError('Unknown scientific property encoding')
    pool=value.pop('literal_pool',{})
    if not isinstance(pool,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in pool.items()):
        raise ValueError('Invalid original-literal pool')
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
            if name in names:
                raise ValueError('Duplicate transported property')
            names.add(name)
            present = literal is not None
            ref = overrides.get(name, {**root, 'pointer': root['pointer'] + '/' + name.replace('~', '~0').replace('/', '~1')}) if present else None
            properties.append(dict(name=name, present=present, value_json=literal, original_ref=ref))
        if not set(overrides) <= names:
            raise ValueError('Unknown property reference override')
        node['properties'] = properties
    return AssertionPacket.model_validate(value, strict=True)


def judge_evidence(evidence):
    """Store each document hash once; original document/anchor identities stay literal."""
    documents = {}
    anchors = []
    for anchor in evidence['anchors']:
        uid, digest = anchor['document_uid'], anchor['source_sha256']
        if uid in documents and documents[uid] != digest:
            raise ValueError('Source document identity has conflicting hashes')
        documents[uid] = digest
        anchors.append({key:value for key,value in anchor.items() if key != 'source_sha256'})
    return {**evidence, 'evidence_encoding':'claims_document_sha_v1',
            'document_source_sha256':documents, 'anchors':anchors}


def audit_evidence(value):
    if value.get('evidence_encoding') != 'claims_document_sha_v1':
        raise ValueError('Unknown source evidence encoding')
    documents = value['document_source_sha256']
    anchors = [{**anchor, 'source_sha256':documents[anchor['document_uid']]} for anchor in value['anchors']]
    return {**{k:v for k,v in value.items() if k not in {'evidence_encoding','document_source_sha256','anchors'}},
            'anchors':anchors}
