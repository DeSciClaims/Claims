"""Provider-free file access checks; search hits never count as full inspection."""
import json
from pathlib import Path

from .literals import LiteralDocument
from .store import sha


def access_audit(root):
    root=Path(root)
    manifest=json.loads((root/'input_manifest.json').read_text())
    catalog=json.loads((root/'catalog.json').read_text())
    event_file=root/'tool_events.jsonl'
    events=[json.loads(line) for line in event_file.read_text().splitlines()] if event_file.is_file() else []
    ranges={}
    for event in events:
        if event['tool']!='read_file':continue
        args=event['arguments'];result=event['result']
        ranges.setdefault(args['path'],[]).append((args['offset'],args['offset']+len(result['text'])))
    def covers(name,start,end):
        cursor=start
        for left,right in sorted(ranges.get(name,[])):
            if left>cursor:break
            cursor=max(cursor,right)
            if cursor>=end:return True
        return False
    def whole(name):return covers(name,0,len((root/name).read_text()))
    documents={}
    def node(name,pointer):
        if not (root/name).is_file():return False
        if name not in documents:documents[name]=LiteralDocument((root/name).read_bytes())
        span=documents[name].spans.get(pointer)
        return span is not None and covers(name,*span)
    anchors=[]
    for i,item in enumerate(catalog['source']):
        if (whole(item['file']) or node('source/evidence.json',f'/anchors/{i}')
            or node('request.json',f'/payload/authoritative_evidence/anchors/{i}')):
            anchors.append(item['anchor_id'])
    components=[]
    for item in catalog['candidate']:
        pointer=item.get('original_pointer')
        if pointer is None:continue
        escaped=pointer.replace('~','~0').replace('/','~1')
        if (whole(item['file']) or node('candidate/components.json','/'+escaped)
            or node('request.json','/payload/components/'+escaped)):components.append(pointer)
    modified=[name for name,digest in manifest['files'].items()
              if not (root/name).is_file() or sha((root/name).read_bytes())!=digest]
    expected_components=[x['original_pointer'] for x in catalog['candidate'] if 'original_pointer' in x]
    return {'modified_inputs':modified,'source_anchors':len(catalog['source']),
            'fully_read_source_anchors':len(anchors),
            'unread_source_anchor_ids':[x['anchor_id'] for x in catalog['source'] if x['anchor_id'] not in set(anchors)],
            'matching_components':len(expected_components),'fully_read_matching_components':len(components),
            'unread_matching_components':sorted(set(expected_components)-set(components)),
            'instructions_fully_read':whole('instructions.txt'),
            'task_fully_read':whole('task.json'),
            'schema_fully_read':whole('output_schema.json'),
            'proves_scientific_understanding':False}
