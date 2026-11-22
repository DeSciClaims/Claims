"""Read-only checks of terminal components and explicit unresolved judgments."""

from .graph import GraphProjection


def checks(payload,projection=None):
    issues = []
    projection=projection or GraphProjection(payload)
    for result in payload['results']:
        cores=set(result['closure']['core_ids'])
        components={projection.component.get(c) for c in cores}
        if None in components or len(components)!=1:
            issues.append({'code':'V13','path':result['id'],'message':'Multiple cores must share one strongly connected component; use an explicit compound anchor otherwise.'})
            continue
        component=next(iter(components))
        if component not in projection.terminal:
            issues.append({'code':'V13','path':result['id'],'message':'Selected core is not terminal in the explicitly represented local graph.'})
        cycle=len(projection.components[component])>1 or any(c in projection.forward[c] for c in cores)
        if cycle and not result['closure'].get('notes') and result['closure']['status']!='review_required':
            issues.append({'code':'V13','path':result['id'],'message':'Nontrivial cycle requires scientific explanation or review_required status.'})
    reviews = {r["object_id"] for r in payload.get("closure_review", [])}
    for result in payload["results"]:
        if (
            reviews & ({result["id"]} | {f["id"] for f in result["fields"]})
            and result["closure"]["status"] == "closed"
        ):
            issues.append(
                {
                    "code": "V14",
                    "path": result["id"],
                    "message": "Result contains explicitly unresolved objects but is marked closed.",
                }
            )
    return issues
