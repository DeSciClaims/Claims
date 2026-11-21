"""Deterministic ownership groups for P; reusable context never merges all consumers."""
from .store import canonical


def assertion_groups(index, owned_refs):
    def physical(pointer):
        objects=[o for o in index.objects if pointer==o.original_ref.pointer or pointer.startswith(o.original_ref.pointer+'/')]
        if not objects:return pointer
        pointer=max(objects,key=lambda o:len(o.original_ref.pointer)).original_ref.pointer
        return pointer.split('/fields/',1)[0]
    by_id={o.object_id:physical(o.original_ref.pointer) for o in index.objects}
    groups={}
    for ref in owned_refs:
        if ref.kind!='json':key=('csv',ref.filename)
        else:
            edge=next((e for e in index.relations if ref.pointer==e.original_ref.pointer or ref.pointer.startswith(e.original_ref.pointer+'/')),None)
            if edge:
                owners=sorted({by_id[k] for k in (edge.source_id,edge.target_id) if k in by_id})
                results=[p for p in owners if p.startswith('/results/')]
                if len(results)==1:key=('record',results[0])
                elif results:key=('joint-relation',*results)
                else:key=('relation',*owners)
            else:key=('record',physical(ref.pointer))
        groups.setdefault(key,[]).append(ref)
    return [sorted(refs,key=canonical) for key,refs in sorted(groups.items(),key=lambda kv:canonical(kv[0]))]
