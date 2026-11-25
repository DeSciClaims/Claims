"""Complete scientific vectors, mandatory secondary checks and arbitration (§5)."""

from .contracts import JudgeResponse, ScientificVerdict
from .transport import judge_packet, judge_evidence
from .budgets import SOURCE_TOKEN_BUDGETS
from .store import sha, canonical
from .sampling import selected_for_pass_audit
from .operations import Hold
from .errors import hold_record

SCIENCE = {
    "NUMERIC_FIDELITY",
    "CLASSIFICATION",
    "SCIENTIFIC_ATTACHMENT",
    "SCOPE_AND_ORIGIN",
    "REQUIRED_INFORMATION",
    "REQUIRED_ENCODING",
    "GROUPING_CONFORMANCE",
    "PROVENANCE",
}
ARCHIVE = {"TABLE_FIDELITY", "TABLE_INTERPRETATION", "TABLE_LINK_CONFORMANCE"}


def validate_judgment(response, task, packet, anchors, resolve):
    if response.task_id != task.task_id or response.packet_id != packet.packet_id:
        raise ValueError("Wrong task/packet echo")
    if response.outcome=='TARGET_DEFECT' and task.task_kind!='CONTENT':
        raise ValueError('TARGET_DEFECT is reserved for a defective generated CONTENT card. Candidate errors require ASSESSED failures; missing source evidence requires NEED_EVIDENCE or SOURCE_UNRESOLVED.')
    for request in response.evidence_requests:
        if not set(request.anchor_ids)<=anchors:raise ValueError("Evidence request cites an unavailable anchor")
    if response.outcome != "ASSESSED":
        return
    if not packet.packet_complete or packet.omitted_due_to_limit_refs:
        raise ValueError("Incomplete packet cannot be assessed")
    expected = ARCHIVE if task.task_kind == "ARCHIVE" else SCIENCE
    dims = [c.dimension for c in response.checks]
    if len(dims) != len(expected) or set(dims) != expected:
        raise ValueError("Wrong scientific dimension vector: checks must contain each required dimension exactly once: " + repr(sorted(expected)))
    requirements = {r.requirement_id for r in task.requirements}
    for check in response.checks:
        if not set(check.evidence_anchor_ids) <= anchors:
            raise ValueError("Unknown source witness")
        if (
            task.task_kind == "OUTPUT_AUDIT"
            and check.dimension == "REQUIRED_INFORMATION"
            and check.status != "NOT_APPLICABLE"
        ):
            raise ValueError("Output audit cannot penalize missing content")
        if check.status == "PASS" and not check.evidence_anchor_ids:
            raise ValueError("A pass needs inspected source evidence")
        for counter in check.counterexamples:
            if not set(counter.evidence_anchor_ids) <= anchors:
                raise ValueError("Unknown counterexample anchor")
            for ref in counter.output_refs:
                resolve(ref)
                if ref.kind=='json':
                    roots=[o.original_ref.pointer for o in packet.objects]+[r.original_ref.pointer for r in packet.relations]
                    properties=[p.original_ref.pointer for o in packet.objects for p in o.properties if p.present]
                    properties += [p.original_ref.pointer for r in packet.relations for p in r.properties if p.present]
                    properties += [p.original_ref.pointer for o in packet.context_headers for p in o.properties if p.present]
                    if ref.pointer not in roots and not any(ref.pointer==p or ref.pointer.startswith(p+'/') for p in properties):
                        raise ValueError('Counterexample cites original information outside the rendered scientific case')
                elif not any(b.filename==ref.filename and (ref.row==0 or b.first_row<=ref.row<b.first_row+len(b.rows)) for b in packet.csv_blocks):
                    raise ValueError('Counterexample cites a CSV record outside the selected complete block')
            if counter.code in {'SOURCE_SEPARATE','MUST_TOGETHER'}:
                import json
                witnesses={o.object_id:o for o in packet.objects if o.kind=='RESULT' and any(
                    r.kind=='json' and (r.pointer==o.original_ref.pointer or r.pointer.startswith(o.original_ref.pointer+'/')) for r in counter.output_refs)}
                if len(witnesses)<2 or 'V10' not in counter.rule_ids:
                    raise ValueError('Family failure requires a concrete two-result witness and its governing family rule')
                groups={json.loads(p.value_json) for o in witnesses.values() for p in o.properties if p.name=='group_id' and p.present}
                if counter.code=='SOURCE_SEPARATE' and len(groups)!=1:raise ValueError('SOURCE_SEPARATE witness is not merged in the submitted family')
                if counter.code=='MUST_TOGETHER' and len(groups)<2:raise ValueError('MUST_TOGETHER witness is not separated in the submitted families')
            if counter.missing_requirement_id is not None:
                if (
                    counter.missing_requirement_id not in requirements
                    or not packet.match.coverage.exhaustive_fallback_completed
                ):
                    raise ValueError(
                        "Omission requires complete lookup and actual requirement"
                    )
            elif not counter.output_refs:
                raise ValueError("Failure needs original erroneous assertion")


class NeedEvidence(Exception):
    def __init__(self,requests):self.requests=requests


def assess(ops, *, task, packet, evidence, secret, scope, resolve, expand=None, source_lookup=None):
    initial=len(ops.used);force_secondary=False
    def passed(r):return r.outcome=="ASSESSED" and not any(c.status=="FAIL" for c in r.checks)
    try:
        for expansion in range(3):
            anchors={a["anchor_id"] for a in evidence["anchors"]}
            data={"task":task.model_dump(mode="json"),"packet":judge_packet(packet),"authoritative_evidence":judge_evidence(evidence),
                  "required_dimensions":sorted(ARCHIVE if task.task_kind=="ARCHIVE" else SCIENCE)}
            if source_lookup is not None:data['source_lookup']=source_lookup
            packet_hash=sha(canonical(packet));fingerprint=sha(canonical(data))
            def validate(response):validate_judgment(response,task,packet,anchors,resolve)
            def call(role,operation,request):
                response=ops.request(role,operation,request,JudgeResponse,validate=validate,
                    packet_tokens=ops.tokenizers[role](canonical(data["packet"]).decode()),
                    source_tokens=ops.tokenizers[role](canonical(data["authoritative_evidence"]).decode()),source_limit=SOURCE_TOKEN_BUDGETS[expansion])
                if response.outcome=="NEED_EVIDENCE":raise NeedEvidence(response.evidence_requests)
                return response
            try:
                primary=call("PRIMARY","primary_verify",data)
                decisive=primary
                if force_secondary or not passed(primary) or selected_for_pass_audit(secret,scope,fingerprint):
                    secondary=call("SECONDARY","secondary",data)
                    vector=lambda r:[(c.dimension,c.status) for c in sorted(r.checks,key=lambda x:x.dimension)]
                    interpretation=lambda r:[(c.dimension,[x.model_dump(mode="json") for x in c.counterexamples]) for c in sorted(r.checks,key=lambda x:x.dimension)]
                    if primary.outcome==secondary.outcome=="ASSESSED" and vector(primary)==vector(secondary) and interpretation(primary)==interpretation(secondary):decisive=secondary
                    else:decisive=call("ARBITER","arbitrate",{**data,"primary":primary.model_dump(mode="json"),"secondary":secondary.model_dump(mode="json")})
                break
            except NeedEvidence as request:
                if expand is None or expansion==2:raise Hold("EVALUATOR","Two evidence expansions exhausted or no authorized evidence available")
                evidence=expand(evidence,request.requests,SOURCE_TOKEN_BUDGETS[1:][expansion])
                force_secondary=True
                packet=packet.model_copy(update={"evidence_anchor_ids":[a["anchor_id"] for a in evidence["anchors"]]})
                packet=packet.model_copy(update={"packet_id":"packet-"+sha(canonical(packet.model_dump(exclude={"packet_id"})))})
                ops.packet_refs.append(ops.store.put(packet))
        if decisive.outcome!="ASSESSED":raise Hold("SOURCE" if decisive.outcome in {"SOURCE_UNRESOLVED","TARGET_DEFECT"} else "EVALUATOR",decisive.outcome+": "+str(decisive.unresolved_reason or decisive.target_issue))
        return ScientificVerdict(task_id=task.task_id,output_alias=packet.output_alias,packet_sha256=sha(canonical(packet)),
            state="PASS" if passed(decisive) else "FAIL",decisive_response_sha256=sha(canonical(decisive)),dimension_checks=decisive.checks,
            judge_run_ids=ops.used[initial:],hold_reasons=[])
    except Hold as exc:
        return ScientificVerdict(task_id=task.task_id,output_alias=packet.output_alias,packet_sha256=sha(canonical(packet)),state="HOLD",
            decisive_response_sha256=None,dimension_checks=[],judge_run_ids=ops.used[initial:],hold_reasons=[hold_record(exc,'VERIFICATION')])
