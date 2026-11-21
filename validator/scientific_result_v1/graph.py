"""Pure identity, path and reviewed-graph projections."""

from __future__ import annotations
import json
import re
from collections import defaultdict
from typing import Any


def definitions(payload: dict) -> list[dict]:
    records = [
        obj
        for name in (
            "sources",
            "tables",
            "result_groups",
            "results",
            "shared_context",
            "cross_group_summaries",
        )
        for obj in payload.get(name, [])
    ]
    records += [
        field
        for name in ("results", "shared_context", "cross_group_summaries")
        for record in payload.get(name, [])
        for field in record.get("fields", [])
    ]
    records += payload.get("data_code_availability", {}).get("artifacts", [])
    return records


def objects(payload: dict) -> dict[str, dict]:
    return {obj["id"]: obj for obj in definitions(payload)}


def walk(value: Any):
    if isinstance(value, dict):
        yield value
        for key, item in value.items():
            if key not in {"value", "context"}:
                yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


class GraphProjection:
    """One reviewed graph/SCC/index per immutable package, with bounded ancestry reuse."""
    def __init__(self,payload):
        from collections import OrderedDict
        self.payload=payload
        self.inventory=objects(payload)
        self.science={node['id'] for name in ('results','shared_context','cross_group_summaries') for record in payload.get(name,[]) for node in [record,*record.get('fields',[])]}
        self.forward={k:set() for k in self.science}
        self.reverse={k:set() for k in self.science}
        self.incident=defaultdict(list)
        self.table_for=defaultdict(set)
        self.unresolved=defaultdict(list)
        for i,edge in enumerate(payload.get('relationships',[])):
            a,b=edge['source_id'],edge['target_id']
            self.incident[a].append(edge);self.incident[b].append(edge)
            if edge['relation']=='table_for':self.table_for[b].add(a)
            direction=edge.get('closure_direction')
            if direction is None:
                self.unresolved[a].append(i);self.unresolved[b].append(i)
            if a not in self.science or b not in self.science or direction not in {'source_to_target','target_to_source'}:continue
            if direction=='target_to_source':a,b=b,a
            self.forward[a].add(b);self.reverse[b].add(a)
        for key,node in self.inventory.items():
            parent=node.get('parent_field_id')
            if key in self.science and parent in self.science:
                self.forward[key].add(parent);self.reverse[parent].add(key)
        visited=set();order=[]
        for root in sorted(self.science,key=lambda s:s.encode()):
            if root in visited:continue
            pending=[(root,False)]
            while pending:
                node,done=pending.pop()
                if done:order.append(node);continue
                if node in visited:continue
                visited.add(node);pending.append((node,True))
                pending.extend((child,False) for child in sorted(self.forward[node],key=lambda s:s.encode(),reverse=True) if child not in visited)
        self.component={};self.components=[]
        for root in reversed(order):
            if root in self.component:continue
            cid=len(self.components);members=set();pending=[root]
            while pending:
                node=pending.pop()
                if node in self.component:continue
                self.component[node]=cid;members.add(node)
                pending.extend(self.reverse[node]-self.component.keys())
            self.components.append(members)
        self.terminal=set(range(len(self.components)))
        for node,targets in self.forward.items():
            if any(self.component[t]!=self.component[node] for t in targets):self.terminal.discard(self.component[node])
        self.cache=OrderedDict();self.cached_members=0

    def ancestors(self,cores):
        cores=set(cores)
        components={self.component[c] for c in cores if c in self.component}
        cache_key=next(iter(components)) if len(components)==1 and len(cores)==sum(c in self.component for c in cores) else None
        if cache_key in self.cache:
            self.cache.move_to_end(cache_key);return set(self.cache[cache_key])
        members=set(cores);pending=list(cores)
        while pending:
            for node in self.reverse.get(pending.pop(),set())-members:
                members.add(node);pending.append(node)
        if cache_key is not None and len(members)<=1_000_000:
            while self.cache and (len(self.cache)>=128 or self.cached_members+len(members)>1_000_000):
                _,old=self.cache.popitem(last=False);self.cached_members-=len(old)
            self.cache[cache_key]=frozenset(members);self.cached_members+=len(members)
        return members

    def closure(self,result):
        members=self.ancestors(result['closure']['core_ids']);refs=set(self.table_for[result['id']])
        for node in members:
            for edge in self.incident[node]:
                if edge.get('closure_direction')=='none':refs.update({edge['source_id'],edge['target_id']}-members)
        for node in [result,*(self.inventory[k] for k in members if k in self.inventory)]:
            refs.update(r['target_id'] for r in node.get('references',[]))
            refs.update(r['table_id'] for r in node.get('table_references',[]))
        return members,refs-members


def closure_sets(payload: dict, result: dict, projection=None) -> tuple[set[str], set[str]]:
    return (projection or GraphProjection(payload)).closure(result)


def resolve_field_path(value: Any, path: str) -> Any:
    # Full tokenization; no eval, ignored suffixes, or negative indexes.
    token = re.compile(r'(?:^|\.)([A-Za-z_][A-Za-z0-9_]*)|\[(\d+|"(?:[^"\\]|\\.)*")\]')
    position = 0
    for match in token.finditer(path):
        if match.start() != position:
            raise ValueError("Invalid field path")
        key = match.group(1)
        if key is None:
            raw = match.group(2)
            key = json.loads(raw) if raw.startswith('"') else int(raw)
        value = value[key]
        position = match.end()
    if position != len(path) or not position:
        raise ValueError("Invalid field path")
    return value
