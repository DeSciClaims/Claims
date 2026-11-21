"""Bounded independent DSPy operations with durable request ownership and replay."""

from __future__ import annotations
from .budgets import SOURCE_TOKEN_BUDGETS, STRUCTURED_PACKET_TOKENS
from pathlib import Path
import threading
import time
from .store import canonical, sha, Pending, Exhausted
from .literals import LiteralDocument
from .contracts import FileRef,JudgeRunReceipt,ErrorRecord
import json

ROOT = Path(__file__).parent
GLOBAL_GATE=threading.BoundedSemaphore(16)
SCOPE_OVERRIDE = """EVALUATION POLICY OVERRIDE: only source bytes supplied to all miners are in scope.
Unsupplied supplements and external tables are informational, never a reason for a
coverage failure or common source hold. Supplied supplements remain required.
Do not fetch URLs. This overrides companion acquisition requirements in the role prompt.
SOURCE EVIDENCE POLICY: docling_pdf_derivatives_v1. The controller independently runs
Docling on supplied PDFs. Its retained text, OCR, equations, captions and table
cells are authoritative evidence for this evaluation. Rely on those derivatives;
page images, visual inspection, crop coordinates and image-capable routes are not
required. Do not request an original-page crop or require plot-derived quantities
absent from the captured Docling output. Parser omissions and OCR errors are an
explicit limitation of this policy, not something the judge may invent away.
LOSSLESS ASSERTION TRANSPORT: a packet marked assertion_encoding=claims_property_columns_v3
uses ordered [name, original_json_literal] pairs unless property_names_ref is present.
With property_names_ref=N0, properties is an ordered literal-value vector: zip it
with packet.property_name_sets.N0 to recover exactly the original property pairs.
The name set contains the literal original property names in their original order.
A null pair value means ABSENT; a string containing the JSON literal null means PRESENT
with a null value. All other pair strings preserve the exact original JSON literal,
including numbers, operators, units, nested context and quotes.
A {"literal_ref":"L0"} slot means the exact original JSON literal string in
packet.literal_pool.L0, not missing information or a summary. Expand it unchanged
before interpreting that property's original value. Repeated long literal strings
share one pool entry; their content is neither paraphrased nor rounded.
A property pointer is original_ref.pointer + '/' + JSON-Pointer-escaped name;
property_ref_overrides contains
any exceptions. The output_alias is inherited from original_ref. Every assertion,
missing-property marker, relation, closure, CSV record and sidecar is retained.
These pairs replace repetitive property wrapper keys, not scientific content.
context_headers uses the same pair encoding for explicitly PARTIAL original
identity/scope headers of non-focal objects. These headers provide interpretation
of context-only associations; they are not complete extracted quantities and
cannot fill an unrendered numeric value. objects and relations remain the scoped
assertion ledger. A context-header counterexample must cite a provided property,
not an unseen value.
LOSSLESS SOURCE TRANSPORT: authoritative_evidence with evidence_encoding=claims_document_sha_v1
stores source_sha256 once per document in document_source_sha256[document_uid]. Each
anchor inherits exactly that source hash. Full document_uid, anchor_id, exact_text,
locator and rendering remain unchanged. No source text or source identity is omitted."""


class Hold(RuntimeError):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


class LayoutSubdivisionRequired(Hold):
    """Explicit length truncation requires a smaller owned source scope."""


class Operations:
    def __init__(
        self,
        *,
        store,
        routes,
        runtimes,
        tokenizers,
        trace_dir,
        deadline,
        policy_hash,
        gate=None,
        offline=False,
    ):
        self.store = store
        self.routes = routes
        self.runtimes = runtimes
        self.tokenizers = tokenizers
        self.trace_dir = Path(trace_dir)
        self.deadline = deadline
        self.policy_hash = policy_hash
        self.gate = gate or GLOBAL_GATE
        self.offline = offline
        self.used = []
        self.response_refs = {}
        self.run_refs = []
        self.packet_refs = []
        if set(routes) != {"SOURCE_READER", "PRIMARY", "SECONDARY", "ARBITER"}:
            raise ValueError("Four frozen routes required")
        identities = lambda names: {
            (routes[n].provider, routes[n].model, routes[n].runtime_revision)
            for n in names
        }
        if (
            len(identities(["PRIMARY", "SECONDARY", "ARBITER"])) != 3
            or len(identities(["SOURCE_READER", "SECONDARY", "ARBITER"])) != 3
        ):
            raise ValueError("Independent route identities required")
        if set(tokenizers) != set(routes) or set(runtimes) != set(routes):
            raise ValueError("Pinned tokenizers and runtimes required for all routes")

    def request(
        self,
        role,
        operation,
        data,
        response_type,
        *,
        validate=None,
        source_tokens=None,
        packet_tokens=None,
        source_limit=SOURCE_TOKEN_BUDGETS[0],
    ):
        prompt = (
            (ROOT / "criteria.txt").read_text()
            + "\n"
            + (ROOT / "system.txt").read_text()
            + "\n"
            + (ROOT / (operation + ".txt")).read_text()
            + "\n"
            + SCOPE_OVERRIDE
        )
        if operation == "secondary":
            prompt += "\n" + (ROOT / "primary_verify.txt").read_text()
        prompt += "\nSCHEMA RULES:\n" + json.dumps(json.loads((ROOT/"extraction_schema.json").read_text())["x-validation_checks"], ensure_ascii=False)
        prompt += "\nSTRICT OUTPUT: Return exactly one JSON object matching stage_schema. No Markdown fences, prose, partial objects or ellipses, including after validation_feedback. Echo all supplied binding/inspection IDs exactly; do not shorten lists. A retry must produce the complete corrected response."
        from .response_schema import response_schema
        schema = response_schema(response_type,data)
        route = self.routes[role]
        request = {
            "role": role,
            "operation": operation,
            "route": route.model_dump(mode="json"),
            "policy_sha256": self.policy_hash,
            "instructions": prompt,
            "payload": data,
            "stage_schema": schema,
        }
        images = {}
        def renderings(node):
            if isinstance(node,dict):
                image=node.get("rendering")
                if isinstance(image,dict):
                    ref=FileRef.model_validate(image,strict=True)
                    images[ref.sha256]=(ref,node.get("document_uid","source"),node.get("locator",{}).get("page_index",0)+1)
                for value in node.values():renderings(value)
            elif isinstance(node,list):
                for value in node:renderings(value)
        renderings(data)
        if source_tokens is None:
            source_data=data.get("authoritative_evidence",data.get("anchors",None))
            if source_data is not None:source_tokens=self.tokenizers[role](canonical(source_data).decode())
        if packet_tokens is None and "packet" in data:
            packet_tokens=self.tokenizers[role](canonical(data["packet"]).decode())
        image_tokens=0
        if images:
            if not route.supports_images:raise Hold("EVALUATOR","Frozen route cannot inspect required source images")
            counter=getattr(self.tokenizers[role],"image_tokens",None)
            if counter is None:raise Hold("EVALUATOR","Frozen tokenizer lacks required image accounting")
            image_tokens=sum(counter(self.store.read(ref)) for ref,_,_ in images.values())
            if type(image_tokens)!=int or image_tokens<0:raise ValueError("Invalid image token accounting")
        request_ref = self.store.put(request)
        key = request_ref.sha256
        count = self.tokenizers[role](canonical(request).decode())
        if type(count) != int or count < 0:
            raise ValueError("Invalid pinned tokenizer result")
        if count + image_tokens + route.max_output_tokens > route.context_window_tokens:
            raise Hold("EVALUATOR", "Route context window exceeded")
        if source_tokens is not None and source_tokens + image_tokens > source_limit:
            raise Hold("SOURCE", "Source scope exceeds current evidence bound")
        if packet_tokens is not None and packet_tokens > STRUCTURED_PACKET_TOKENS:
            raise Hold("EVALUATOR", "Complete joint packet exceeds shard bound")
        for _ in range(2):
            if time.time() >= self.deadline:
                raise Hold(
                    "EVALUATOR", "Unchanged scientific generation deadline elapsed"
                )
            try:
                feedback=self.store.last_error(key)
                attempt_request=self.store.put({**request,"validation_feedback":feedback}) if feedback else request_ref
                ordinal, cached = self.store.reserve(key, attempt_request.sha256, timeout=120)
            except (Pending, Exhausted) as exc:
                raise Hold("EVALUATOR", str(exc)) from exc
            if cached is not None:
                result = response_type.model_validate_json(
                    self.store.get_blob(cached), strict=True
                )
                if validate:
                    validate(result)
                self.response_refs[key] = self.store.put(result)
                for attempt in (1,2):
                    head=self.store.head("judge-run:"+key+":"+str(attempt))
                    if head is not None:
                        raw=self.store.get_blob(head)
                        self.run_refs.append(FileRef(relative_path="blobs/"+head,sha256=head,byte_length=len(raw),media_type="application/octet-stream"))
                        self.used.append(JudgeRunReceipt.model_validate_json(raw,strict=True).run_id)
                return result
            if self.offline:
                self.store.finish(
                    key, ordinal, error="Provider disabled; uncached operation"
                )
                raise Hold("EVALUATOR", "Replay requested an uncached model operation")
            runtime = self.runtimes[role]
            payload = json.loads(canonical({**data, "stage_schema": schema}))
            if feedback:payload["validation_feedback"]=feedback
            if images:
                payload["source_images"]=[]
                for digest,(ref,source,page) in images.items():
                    path=self.trace_dir/"source-images"/(digest+".png")
                    path.parent.mkdir(parents=True,exist_ok=True)
                    data_bytes=self.store.read(ref)
                    if path.exists() and sha(path.read_bytes())!=digest:raise Hold("EVALUATOR","Source rendering changed")
                    path.write_bytes(data_bytes)
                    payload["source_images"].append({"path":str(path),"source_id":source,"page":page,"sha256":digest})
            raw_ref = None
            started=time.monotonic()
            try:
                # A distinct provider context for each role and candidate. No earlier verdict to secondary.
                with self.gate:
                    if time.time() >= self.deadline:
                        raise Hold("EVALUATOR", "Deadline expired in queue")
                    answer = runtime.generate(
                        instructions=prompt,
                        payload=payload,
                        output_dir=self.trace_dir / role,
                        stage="source-task-" + operation + "-" + key[:16],
                    )
                raw = canonical(answer)
                LiteralDocument(raw)
                raw_ref = self.store.put_blob(sha(raw), raw)
                result = response_type.model_validate_json(raw, strict=True)
                if validate:
                    validate(result)
                self.store.finish(key, ordinal, response=raw_ref.sha256)
                self.response_refs[key] = raw_ref
                self.record_run(key,ordinal,role,attempt_request,raw_ref,count,started,None)
                return result
            except Exception as exc:
                from .errors import safe_message
                message=safe_message(exc,[getattr(r,'api_key',None) for r in self.runtimes.values()])
                self.store.finish(
                    key,
                    ordinal,
                    response=raw_ref.sha256 if raw_ref else None,
                    error=type(exc).__name__ + ": " + message[:2000],
                )
                self.record_run(key,ordinal,role,attempt_request,raw_ref,count,started,exc)
                from ..runtime import TruncatedStageError
                if operation == 'source_table_layout' and isinstance(exc, TruncatedStageError):
                    raise LayoutSubdivisionRequired('SOURCE', 'Provider explicitly truncated table layout') from exc
                if isinstance(exc, Hold):
                    raise
        raise Hold(
            "MODEL", "Technical response failure after one identical-request retry"
        )


    def record_run(self,key,ordinal,role,request,response,count,started,error):
        usage=getattr(self.runtimes[role],"usage",[])
        if usage and not usage[-1].get('stage','').endswith(key[:16]):
            usage=[]
        last=usage[-1].get("usage",{}) if usage else {}
        raw_response=response
        if usage:
            call=usage[-1].get("call_id")
            path=self.trace_dir/role/(str(call)+".response.txt")
            if path.is_file():raw_response=self.store.put_named("provider-responses/"+str(call)+".txt",path.read_bytes(),"text/plain")
        from .errors import safe_message
        message=safe_message(error,[getattr(r,'api_key',None) for r in self.runtimes.values()]) if error else None
        receipt=JudgeRunReceipt(run_id=key+":"+str(ordinal),role=role,
            model_route_sha256=self.routes[role].route_sha256,request=request,raw_response=raw_response,
            parsed_response=response if error is None else None,attempt=ordinal,
            input_tokens=last.get("prompt_tokens") or count,
            output_tokens=last.get("completion_tokens") or (self.tokenizers[role](self.store.read(response).decode()) if response else 0),
            elapsed_ms=int((time.monotonic()-started)*1000),
            error=ErrorRecord(category="MODEL",code="RESPONSE_INVALID" if response else "PROVIDER_ERROR",stage="VERIFICATION",message=message[:2000],retryable=ordinal==1,output_refs=[]) if error else None)
        usage_record={'run_id':receipt.run_id,'model':self.routes[role].model,'role':role,
            'usage':last,'billing_available':last.get('cost') is not None,
            'provider_request_id':usage[-1].get('provider_request_id') if usage else None,
            'provider':usage[-1].get('provider') if usage else None,
            'tokens_observed':bool(last),'dispatch_outcome':'confirmed' if usage else 'unknown'}
        usage_ref=self.store.put(usage_record)
        self.store.advance('provider-usage:'+receipt.run_id,None,usage_ref.sha256)
        ref=self.store.put(receipt);self.store.advance("judge-run:"+key+":"+str(ordinal),None,ref.sha256)
        self.run_refs.append(ref)
        self.used.append(receipt.run_id)


    def scoped(self):
        from dataclasses import is_dataclass, replace
        runtimes={role:replace(runtime,usage=[]) if is_dataclass(runtime) else runtime
                  for role,runtime in self.runtimes.items()}
        return Operations(store=self.store,routes=self.routes,runtimes=runtimes,tokenizers=self.tokenizers,
            trace_dir=self.trace_dir,deadline=self.deadline,policy_hash=self.policy_hash,gate=self.gate,offline=self.offline)


def retained_usage(store, run_refs):
    """One event per confirmed/unknown provider attempt, independent of cache consumers."""
    result=[];seen=set()
    for ref in run_refs:
        run=store.load(ref,JudgeRunReceipt)
        if run.run_id in seen:continue
        seen.add(run.run_id)
        digest=store.head('provider-usage:'+run.run_id)
        if digest is None:raise ValueError('Retained judge attempt lacks usage provenance')
        usage=json.loads(store.get_blob(digest))
        result.append({'model':usage['model'],'pass_id':run.run_id,'status':'error' if run.error else 'success',
            'cost_kind':'actual' if usage['billing_available'] else 'unavailable',
            'cost_usd':usage['usage'].get('cost'),'prompt_tokens':usage['usage'].get('prompt_tokens'),
            'completion_tokens':usage['usage'].get('completion_tokens'),'dispatch_outcome':usage['dispatch_outcome'],
            'usage_sha256':digest})
    return result
