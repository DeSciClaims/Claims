"""Guide arithmetic, sampling, original bytes, strict contracts and durable recovery."""

from fractions import Fraction
from pathlib import Path
import json
import pytest
from validator.scientific_result_v1.source_tasks.sampling import (
    frame,
    rank,
    ordered,
    table_draw,
    seed_commitment,
    audit_buckets,
    PROTOCOL,
)
from validator.scientific_result_v1.source_tasks.scoring import quality
from validator.scientific_result_v1.source_tasks.literals import LiteralDocument
from validator.scientific_result_v1.source_tasks.store import (
    Store,
    Conflict,
    Pending,
    sha,
)
from validator.scientific_result_v1.source_tasks.contracts import FractionValue
from validator.scientific_result_v1.source_tasks.packages import Package
from validator.scientific_result_v1.source_tasks.packets import Index

SCOPE = (PROTOCOL, "r1", "final", "p1")
SECRET = b"x" * 32


@pytest.mark.parametrize(
    "c,t,p,ac,at,expected",
    [
        (1, 1, 1, True, True, Fraction(1)),
        (Fraction(5, 6), 1, 1, True, True, Fraction(47, 54)),
        (1, 1, Fraction(3, 4), True, True, Fraction(3, 4)),
        (1, 1, Fraction(1, 2), True, True, Fraction(1, 2)),
        (Fraction(1, 2), 1, Fraction(1, 2), True, True, Fraction(11, 36)),
        (Fraction(5, 6), None, 1, True, False, Fraction(5, 6)),
        (None, 1, 1, False, True, Fraction(1)),
        (None, None, 1, False, False, None),
        (None, 1, 1, True, True, None),
    ],
)
def test_guide_exact_arithmetic(c, t, p, ac, at, expected):
    assert (
        quality(
            gate=1,
            content=c,
            archive=t,
            audit=p,
            content_available=ac,
            archive_available=at,
        )
        == expected
    )


def test_hidden_seed_codec_and_pipeline_independent_audit():
    assert frame("a", "é") == b"\0\0\0\0\0\0\0\1a\0\0\0\0\0\0\0\2\xc3\xa9"
    assert len(rank(SECRET, SCOPE, "P_BUCKET", "s1")) == 32
    assert audit_buckets(["s1", "s2", "UNANCHORED"], SECRET, SCOPE) == ordered(
        ["s1", "s2"], SECRET, SCOPE, "P_BUCKET"
    ) + ["UNANCHORED"]
    assert seed_commitment(SECRET, SCOPE, "1" * 64, "2" * 64) != seed_commitment(
        SECRET, SCOPE, "3" * 64, "2" * 64
    )
    with pytest.raises(ValueError):
        rank(b"bad", SCOPE, "P_BUCKET", "s1")


def test_table_round_robin_census_and_inclusion():
    pop = {"t1": ["t1b1", "t1b2", "t1b3"], "t2": ["t2b1"], "t3": ["t3b1", "t3b2"]}
    selected, diagnostics = table_draw(pop, 4, SECRET, SCOPE, "T_TABLE", "T_BLOCK")
    assert len(selected) == 4 and len(set(selected)) == 4
    assert all(any(b in selected for b in blocks) for blocks in pop.values())
    assert all(d["table_inclusion"] == 1 for d in diagnostics.values())
    selected, diagnostics = table_draw(pop, 2, SECRET, SCOPE, "T_TABLE", "T_BLOCK")
    assert len(diagnostics) == 2 and all(
        d["table_inclusion"] == Fraction(2, 3) for d in diagnostics.values()
    )


def test_literal_values_missing_null_numeric_precision_and_pointers():
    raw = b'{"a/b":{"x~y":1.2300e+02},"null":null,"false":false}'
    doc = LiteralDocument(raw)
    assert doc.literal("/a~1b/x~0y") == "1.2300e+02"
    assert doc.literal("/null") == "null"
    assert doc.resolve("/false") is False
    for invalid in [b'{"a":1,"a":2}', b'{"a":NaN}', b"{} {}", b'"\\ud800"']:
        with pytest.raises((ValueError, UnicodeError)):
            LiteralDocument(invalid)


def test_reduced_unit_fractions_reject_nonreduced_and_booleans():
    for val in [
        dict(numerator=2, denominator=4),
        dict(numerator=True, denominator=1),
        dict(numerator=2, denominator=1),
    ]:
        with pytest.raises(ValueError):
            FractionValue(**val)


def test_durable_dispatch_restart_cas_and_immutable_attempts(tmp_path):
    store = Store(tmp_path)
    request = store.put({"a": 1})
    answer = store.put({"ok": True})
    n, cached = store.reserve("one", request.sha256, now=100)
    assert n == 1 and cached is None
    with pytest.raises(Pending):
        Store(tmp_path).reserve("one", request.sha256, now=101)
    n, _ = store.reserve("one", request.sha256, now=221)
    assert n == 2
    store.finish("one", n, response=answer.sha256)
    assert Store(tmp_path).reserve("one", request.sha256, now=222) == (
        None,
        answer.sha256,
    )
    with pytest.raises(Conflict):
        store.finish("one", n, error="late timeout")
    assert store.advance("paper", None, answer.sha256) == answer.sha256
    with pytest.raises(Conflict):
        store.advance("paper", None, request.sha256)
    assert store.generation("g", "binding", activated_at=100) == (100, 1900)
    assert Store(tmp_path).generation("g", "binding", activated_at=200) == (100, 1900)


@pytest.fixture
def generated(tmp_path):
    # Equivalent to the guide's fixture generator without requiring a local tmp guide.
    schema = json.loads(
        (
            Path(__file__).parents[2]
            / "validator/scientific_result_v1/source_tasks/extraction_schema.json"
        ).read_text()
    )
    original = next(x for x in schema["examples"] if x["paper"]["id"] == "P005")
    text = next(
        x["content"]
        for x in schema["x-table_extraction"]["example_csv_files"]
        if x["extraction_prefix"] == "P005.E001"
    )
    path = tmp_path / "package"
    path.mkdir()
    (path / "output.json").write_text(json.dumps(original))
    (path / "P005.E001.T001.csv").write_text(text, newline="")
    return path, original, text


def test_lossless_packet_index_and_gate_separate_from_science(generated):
    path, doc, csv = generated
    package = Package(path, "a")
    assert package.gate.gate_bit == 1
    index = Index(package)
    obj = next(o for o in index.objects if o.object_id == "P005.E001.R001.F001")
    assert next(p.value_json for p in obj.properties if p.name == "value") == "12.0"
    doc["results"][1]["fields"][0]["value"] = 35
    (path / "output.json").write_text(json.dumps(doc))
    assert (
        Package(path, "a").gate.gate_bit == 1
    )  # Wrong science is not structural invalidity.
    (path / "P005.E001.T001.csv").unlink()
    assert Package(path, "a").gate.gate_bit == 0


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("perfect", Fraction(1)),
        ("sharded", Fraction(1)),
        ("wrong_value", Fraction(0)),
        ("wrong_label", Fraction(0)),
        ("wrong_operator", Fraction(0)),
        ("wrong_csv_precision", Fraction(7, 9)),
        ("false_summary", Fraction(0)),
        ("reference_omits_b", Fraction(11, 18)),
        ("shared_storage", Fraction(1)),
        ("missing_csv", Fraction(0)),
        ("stale_closure", Fraction(0)),
        ("unreviewed_link", Fraction(0)),
        ("bad_annotation_range", Fraction(0)),
        ("source_hold", None),
        ("invalid_judge_pointer", None),
    ],
)
def test_full_p005_pool_freezes_exact_scores_and_replays_without_provider(
    generated, tmp_path, variant, expected
):
    import time
    from validator.scientific_result_v1.source_tasks.contracts import (
        SourceDocument,
        SourcePacket,
        SourceAnchor,
        TextLocator,
        TableLocator,
        SourceTable,
        SourceFrame,
        SourceTile,
        SourceGridManifest,
        ComparisonPlan,
        SubmittedPackage,
        DocumentBinding,
        ModelRoute,
    )
    from validator.scientific_result_v1.source_tasks.operations import Operations
    from validator.scientific_result_v1.source_tasks.evaluator import PaperEvaluator
    from validator.scientific_result_v1.source_tasks.store import canonical
    from validator.scientific_result_v1.source_tasks.judging import SCIENCE, ARCHIVE

    path, doc, csv = generated
    store = Store(tmp_path / "evidence")
    source_text = (
        "\n".join(x["text"] for r in doc["results"] for x in r["source_snippets"])
        + "\nMean ± SE; A/B comparison; noncausal descriptive investigation."
    )
    src = store.put_named("sources/source.txt", source_text.encode(), "text/plain")
    packet = SourcePacket(
        protocol_id=PROTOCOL,
        paper_id="P005",
        round_id="r1",
        state="READY",
        documents=[
            SourceDocument(
                document_uid="d1",
                role="MAIN",
                title="Synthetic source",
                version="1",
                url=None,
                required=True,
                acquisition_status="SUPPLIED",
                availability="AVAILABLE",
                file=src,
            )
        ],
        errors=[],
    )
    header = SourceAnchor(
        anchor_id="header",
        document_uid="d1",
        source_sha256=src.sha256,
        locator=TextLocator(kind="text", start=0, stop=len(source_text)),
        exact_text=source_text,
        rendering=None,
    )
    rows = [
        SourceAnchor(
            anchor_id="row" + str(i),
            document_uid="d1",
            source_sha256=src.sha256,
            locator=TableLocator(
                kind="table",
                table_uid="t1",
                first_row=i,
                stop_row=i + 1,
                column_names=[],
            ),
            exact_text=r["source_snippets"][0]["text"],
            rendering=None,
        )
        for i, r in enumerate(doc["results"])
    ]
    from layout_fixture import layout_fixture
    table = SourceTable(
        table_uid="t1",
        document_uid="d1",
        anchor_ids=["header"],
        numerical=True,
        data_row_count=2,
        layout_receipt=layout_fixture(store,packet,["row0","row1"],["outcome","a","b","p"]),
        data_row_anchor_ids=["row0", "row1"],
        logical_column_names=["outcome", "a", "b", "p"],
        header_anchor_ids=["header"],
        note_anchor_ids=[],
    )
    tile = SourceTile(
        tile_id="t1b1",
        document_uid="d1",
        stratum="TABLE",
        eligible_content=True,
        owned_anchor_ids=["row0", "row1"],
        context_anchor_ids=["header"],
        table_uid="t1",
        first_data_row=0,
        stop_data_row=2,
    )
    source_frame = SourceFrame(
        protocol_id=PROTOCOL,
        paper_id="P005",
        source_packet_sha256=sha(canonical(packet)),
        parser_config_sha256="0" * 64,
        revision=0,
        state="READY",
        anchors=[header, *rows],
        tables=[table],
        tiles=[tile],
        excluded_region_anchor_ids=[],
        errors=[],
    )
    grid = SourceGridManifest(
        protocol_id=PROTOCOL,
        round_id="r1",
        stage="final",
        revision=0,
        precommitted_order_sha256=sha(canonical(["P005"])),
        initial_paper_ids=["P005"],
        replacements=[],
    )
    plan = ComparisonPlan(
        protocol_id=PROTOCOL,
        round_id="r1",
        stage="final",
        plan_revision=0,
        source_grid_revision=0,
        reference_fallback_used=False,
        baseline_alias="a",
        challenger_aliases=["b"],
        reference_alias="a",
        source_grid=store.put(grid),
        parent_plan_sha256=None,
        reason="INITIAL",
    )
    packages = []
    bindings = {}
    for alias in ["a", "b"]:
        import copy

        candidate = copy.deepcopy(doc)
        delivered = csv
        if alias == "b":
            if variant == "wrong_value":
                candidate["results"][1]["fields"][0]["value"] = 35
            if variant == "wrong_label":
                candidate["results"][0]["fields"][1]["label"] = "standard_deviation"
            if variant == "wrong_operator":
                candidate["results"][1]["fields"][4]["value_operator"] = "="
            if variant == "wrong_csv_precision":
                delivered = csv.replace("12.0 ± 1.2", "12 ± 1.2")
            if variant == "false_summary":
                candidate["results"][0]["summary"] = (
                    "This descriptive comparison proves causation."
                )
            if variant == "reference_omits_b":
                removed = candidate["results"][1]["id"]
                candidate["results"] = candidate["results"][:1]
                candidate["relationships"] = [
                    r
                    for r in candidate["relationships"]
                    if not r["source_id"].startswith(removed)
                    and not r["target_id"].startswith(removed)
                ]
                candidate["result_groups"][0]["core_result_ids"] = [
                    candidate["results"][0]["id"]
                ]
            if variant == "shared_storage":
                fields = []
                for r in candidate["results"]:
                    for f in list(r["fields"]):
                        if f["label"] == "standard_error":
                            f["context"] = copy.deepcopy(r["context"])
                            fields.append(f)
                            r["fields"].remove(f)
                candidate["shared_context"] = [
                    {
                        "id": "P005.E001.C001",
                        "title": "Shared storage",
                        "fields": fields,
                        "source_snippets": [
                            r["source_snippets"][0] for r in candidate["results"]
                        ],
                    }
                ]
            if variant == "stale_closure":
                candidate["results"][0]["closure"]["member_ids"].remove(
                    "P005.E001.R001.F002"
                )
            if variant == "unreviewed_link":
                del candidate["relationships"][0]["closure_direction"]
            if variant == "bad_annotation_range":
                candidate["tables"][0]["annotations"][1]["rows"] = [3]
        packages.append(
            SubmittedPackage(
                protocol_id=PROTOCOL,
                paper_id="P005",
                round_id="r1",
                output_alias=alias,
                source_packet_sha256=sha(canonical(packet)),
                pipeline_digest=sha(alias.encode()),
                roles=["BASELINE", "BRONZE"] if alias == "a" else ["CHALLENGER"],
                execution_receipt=store.put({"terminal": "completed"}),
                state="COMPLETED",
                extraction_json=store.put_named(
                    alias + "/output.json",
                    json.dumps(candidate).encode(),
                    "application/json",
                ),
                table_csv_files=[]
                if alias == "b" and variant == "missing_csv"
                else [
                    store.put_named(
                        alias + "/P005.E001.T001.csv", delivered.encode(), "text/csv"
                    )
                ],
                errors=[],
            )
        )
        bindings[alias] = [
            DocumentBinding(
                output_alias=alias,
                source_id="P005.E001.S001",
                state="BOUND",
                document_uid="d1",
                evidence_anchor_ids=["header"],
                explanation="Trusted execution input",
            )
        ]

    class Script:
        calls = 0

        def generate(self, *, instructions, payload, output_dir, stage):
            self.calls += 1
            if "source_card_consolidate" in stage:
                return payload["original_source_draft"]
            if "source_card_review" in stage:
                return dict(
                    request_id=payload["request_id"],
                    proposal_kind="CARD_BATCH",
                    proposal_hash=payload["proposal_hash"],
                    outcome="ACCEPT",
                    checked_anchor_ids=payload["checked_anchor_ids"],
                    checked_item_ids=payload["checked_item_ids"],
                    issues=[],
                    corrected_proposal=None,
                    evidence_requests=[],
                    unresolved_reason=None,
                )
            if "source_card" in stage:
                return dict(
                    tile_id="t1b1",
                    outcome="READY",
                    cards=[
                        dict(
                            local_key=str(i),
                            task_kind="CONTENT",
                            scope_kind="SCIENTIFIC_RESULT",
                            requirements=[
                                dict(
                                    requirement_id="marker" + str(i),
                                    statement="Preserve complete comparison " + str(i),
                                    qualifications=[],
                                    evidence_anchor_ids=["row" + str(i)],
                                    encoding_constraints=[],
                                )
                            ],
                            evidence_anchor_ids=["row" + str(i), "header"],
                            source_scope_note="Descriptive two-group comparison",
                        )
                        for i in range(2)
                    ],
                    evidence_requests=[],
                    unresolved_reason=None,
                )
            if "source_match" in stage:
                assert payload["task"]["task_kind"] != "OUTPUT_AUDIT", "Known assertions do not require semantic lookup"
                keys = list(payload["components"])
                if variant=='sharded':
                    requirements=payload['task']['requirements']
                    if not requirements:keys=[]
                    elif requirements[0]['requirement_id'] in {'table-identity','archive-complete'}:keys=[k for k in keys if k.startswith('/tables/')]
                    else:
                        ordinal=int(requirements[0]['requirement_id'][-1])
                        keys=[k for k in keys if k=='/results/'+str(ordinal) or k.startswith('/results/'+str(ordinal)+'/fields/')]
                refs = [
                    dict(kind="json", output_alias=payload["output_alias"], pointer=k)
                    for k in keys
                ]
                return dict(
                    task_id=payload["task"]["task_id"],
                    output_alias=payload["output_alias"],
                    state="MATCHED" if refs else "ABSENT",
                    inspected_component_ids=list(payload["components"]),
                    matches=[
                        dict(requirement_id=r["requirement_id"], output_refs=refs)
                        for r in payload["task"]["requirements"]
                    ],
                    explanation="Scripted complete fixture correspondence",
                )
            task = payload["task"]
            if variant=="source_hold" and payload['packet']['output_alias']=='b':
                return dict(task_id=task['task_id'],packet_id=payload['packet']['packet_id'],outcome='SOURCE_UNRESOLVED',checks=[],evidence_requests=[],target_issue=None,unresolved_reason='Scripted genuine source ambiguity',technical_error=None)
            dims = ARCHIVE if task["task_kind"] == "ARCHIVE" else SCIENCE
            failed = "NUMERIC_FIDELITY" if variant=="invalid_judge_pointer" and payload["packet"]["output_alias"]=="b" else None
            missing = False
            if payload["packet"]["output_alias"] == "b":
                if variant in {
                    "wrong_value",
                    "wrong_label",
                    "wrong_operator",
                    "false_summary",
                }:
                    if task["task_kind"] == "OUTPUT_AUDIT" or (
                        task["task_kind"] == "CONTENT"
                        and task["requirements"][0]["requirement_id"]
                        == (
                            "marker0"
                            if variant in {"wrong_label", "false_summary"}
                            else "marker1"
                        )
                    ):
                        failed = (
                            "CLASSIFICATION"
                            if variant == "wrong_label"
                            else "SCOPE_AND_ORIGIN"
                            if variant == "false_summary"
                            else "NUMERIC_FIDELITY"
                        )
                if variant == "wrong_csv_precision" and task["task_kind"] == "ARCHIVE":
                    failed = "TABLE_FIDELITY"
                if (
                    variant == "reference_omits_b"
                    and task["task_kind"] == "CONTENT"
                    and task["requirements"][0]["requirement_id"] == "marker1"
                ):
                    failed = "REQUIRED_INFORMATION"
                    missing = True
            checks = []
            for d in sorted(dims):
                counter = []
                if d == failed:
                    counter = [
                        dict(
                            code="MISSING_REQUIRED_INFORMATION"
                            if missing
                            else "TABLE_VALUE"
                            if d == "TABLE_FIDELITY"
                            else "VALUE",
                            rule_ids=["V09"],
                            description="Scripted source-grounded fixture defect",
                            evidence_anchor_ids=["header"],
                            output_refs=[]
                            if missing
                            else [
                                dict(
                                    kind="json", output_alias="b", pointer="/nonexistent" if variant=="invalid_judge_pointer" else "/results/0"
                                )
                            ],
                            missing_requirement_id="marker1" if missing else None,
                        )
                    ]
                checks.append(
                    dict(
                        dimension=d,
                        status="FAIL"
                        if d == failed
                        else "NOT_APPLICABLE"
                        if task["task_kind"] == "OUTPUT_AUDIT"
                        and d == "REQUIRED_INFORMATION"
                        else "PASS",
                        evidence_anchor_ids=["header"],
                        counterexamples=counter,
                    )
                )
            return dict(
                task_id=task["task_id"],
                packet_id=payload["packet"]["packet_id"],
                outcome="ASSESSED",
                checks=checks,
                evidence_requests=[],
                target_issue=None,
                unresolved_reason=None,
                technical_error=None,
            )

    runtime = Script()
    routes = {}
    for role, model in [
        ("SOURCE_READER", "primary"),
        ("PRIMARY", "primary"),
        ("SECONDARY", "secondary"),
        ("ARBITER", "arbiter"),
    ]:
        routes[role] = ModelRoute(
            role=role,
            provider="scripted",
            model=model,
            runtime_revision="test1",
            route_sha256="0" * 64,
            supports_strict_json_schema=True,
            supports_images=False,
            context_window_tokens=100000,
            temperature=0,
            top_p=1,
            n=1,
            max_output_tokens=16384,
            request_timeout_seconds=120,
            provider_parameters=store.put({}),
        )

    def budget(text):
        try:node=json.loads(text)
        except ValueError:return len(text.split())
        if variant=='sharded' and isinstance(node,dict) and 'packet_id' in node and 'objects' in node:
            return 1000+4000*sum(o['kind']=='FIELD' for o in node['objects'])
        return len(text.split())

    def ops(offline=False):
        return Operations(
            store=store,
            routes=routes,
            runtimes={r: runtime for r in routes},
            tokenizers={r: budget for r in routes},
            trace_dir=tmp_path / "traces",
            deadline=time.time() + 1800,
            policy_hash="1" * 64,
            offline=offline,
        )

    secret = b"q" * 32
    scope = (PROTOCOL, "r1", "final", "P005")
    kwargs = dict(
        source_packet=packet,
        source_frame=source_frame,
        packages=packages,
        comparison_plan=plan,
        secret=secret,
        commitment=seed_commitment(secret, scope, sha(canonical(packet)), "1" * 64),
        policy_sha256="1" * 64,
        document_bindings=bindings,
    )
    receipt, ref = PaperEvaluator(ops()).evaluate(**kwargs)
    if expected is None:
        assert receipt.state=='HOLD'
        assert all(v.score is None for v in receipt.paper_scores)
        from competition.scientific_scoring import adapt_scientific_receipt
        pending=adapt_scientific_receipt(receipt,ref,store=store,hotkey_by_alias={'a':'reference','b':'challenger'},baseline_alias='a',challenger_aliases=['b'])
        assert not pending.pair_rows and not pending.scores_by_hotkey
        from validator.scientific_result_v1.source_tasks.replay import replay
        assert replay(store,ref)==receipt
        return
    assert receipt.state == "FROZEN"
    values = {s.output_alias: s.score.fraction() for s in receipt.paper_scores}
    assert values == {"a": Fraction(1), "b": expected}
    from competition.scientific_scoring import adapt_scientific_receipt

    result = adapt_scientific_receipt(
        receipt,
        ref,
        store=store,
        hotkey_by_alias={"a": "reference", "b": "challenger"},
        baseline_alias="a",
        challenger_aliases=["b"],
    )
    assert result.pair_rows[0]["challenger_score"] == float(expected)
    assert result.pair_rows[0]["baseline_score"] == 1.0
    from validator.scientific_result_v1.source_tasks.replay import replay
    assert replay(store,ref)==receipt
    calls = runtime.calls
    replay, replay_ref = PaperEvaluator(ops(offline=True)).evaluate(**kwargs)
    assert replay_ref == ref and replay == receipt
    assert runtime.calls == calls
    if variant=='sharded':
        assert any('::shard:' in bit.task_id for vector in receipt.paper_scores for bit in vector.task_bits)
    if variant=='perfect':
        from validator.scientific_result_v1.source_tasks.replay import replay as verify
        from validator.scientific_result_v1.source_tasks.contracts import SampleManifest,JudgeRunReceipt
        manifest=store.load(receipt.sampling_manifest,SampleManifest)
        corruptions=[receipt.model_copy(update={'paper_scores':receipt.paper_scores[:1]}),
            receipt.model_copy(update={'sampling_manifest':store.put(manifest.model_copy(update={'selections':manifest.selections[:-1]}))}),
            receipt.model_copy(update={'judge_runs':[r for r in receipt.judge_runs if store.load(r,JudgeRunReceipt).role!='SECONDARY']}),
            receipt.model_copy(update={'paper_scores':[receipt.paper_scores[0].model_copy(update={'task_bits':[]}),receipt.paper_scores[1]]})]
        for altered in corruptions:
            with pytest.raises(ValueError):verify(store,store.put(altered))
        from competition.scientific_lineage import verify_current_grid
        class Backend:
            scientific_store=store
            def get_round(self,_):return {'configuration_sha256':'1'*64,'rules_public':{'policy':{'validation_protocol':PROTOCOL}}}
            def get_paper_evaluation(self,*_):return {'result_private':{'silver':{'receipt_sha256':ref.sha256,'sampled_receipt':receipt.model_dump(mode='json'),'output_alias_by_hotkey':{'reference':'a','challenger':'b'}}}}
        plan_data={'comparison_plan':store.put(plan),'alias_by_hotkey':{'reference':'a','challenger':'b'},'precommitted_order':['P005']}
        store.advance('scientific-plan:r1:final',None,store.put(plan_data).sha256)
        assert len(verify_current_grid('r1','final',backend=Backend(),pairs=result.pair_rows))==1
        stale=store.put(receipt.model_copy(update={'binding':receipt.binding.model_copy(update={'revision':1})}))
        store.advance_paper_revision('r1','final','P005',ref.sha256,stale.sha256)
        with pytest.raises(ValueError,match='stale'):verify_current_grid('r1','final',backend=Backend(),pairs=result.pair_rows)


def test_original_json_transport_preserves_numeric_lexemes(generated,tmp_path):
    from validator.scientific_result_v1.package import envelope,materialize
    path,doc,_=generated
    original=(path/'output.json').read_bytes().replace(b'12.0',b'1.200e1',1)
    (path/'output.json').write_bytes(original)
    packet=envelope(doc,path,original_json=path/'output.json')
    target=tmp_path/'round-trip';materialize(packet,target,paper_id='P005')
    assert (target/'extraction.json').read_bytes()==original
    assert LiteralDocument(original).literal('/results/0/fields/0/value')=='1.200e1'


def test_worker_bundle_supplies_only_declared_source_bytes_and_schema():
    from competition.worker_bundle import build_worker_bundle
    from validator.scientific_result_v1.source_tasks.packages import SCHEMA_HASH
    pdf=b'%PDF-main';supplement=b'%PDF-supplement';package=b'wheel bytes'
    manifest={'source_scope_policy':'supplied_sources_only','sources':[
        {'source_id':'S001','role':'MAIN','path':'paper.pdf','sha256':sha(pdf),'byte_length':len(pdf),'media_type':'application/pdf'},
        {'source_id':'S002','role':'SUPPLEMENT','path':'supplied-source-S002.pdf','sha256':sha(supplement),'byte_length':len(supplement),'media_type':'application/pdf'}]}
    job={'job_id':'a'*64,'paper_id':'P005','package_sha256':sha(package),'expected_pdf_sha256':sha(pdf),
        'worker_contract':{'package_filename':'miner-1.0-py3-none-any.whl','entrypoint':'miner:main',
            'request':{'paper':{'paper_id':'P005'},'pdf_path':'paper.pdf','output_schema':'scientific_result_v1.1'}},
        'scientific_inputs':{'manifest':manifest,'files':{'paper.pdf':pdf,'supplied-source-S002.pdf':supplement}}}
    bundle=build_worker_bundle(job=job,package=package,pdf=pdf)
    assert bundle['supplied-source-S002.pdf']==supplement
    assert sha(bundle['claims_extraction.schema.json'])==SCHEMA_HASH
    inputs=json.loads(bundle['input.json'])
    assert inputs['scientific_input_sha256']['supplied-source-S002.pdf']==sha(supplement)
    del job['scientific_inputs']
    bundle=build_worker_bundle(job=job,package=package,pdf=pdf)
    assert 'supplied-source-S002.pdf' not in bundle
    assert len(json.loads(bundle['sources.json'])['sources'])==1


def test_output_audit_selects_only_known_owned_assertions_without_lookup(generated):
    from types import SimpleNamespace
    from validator.scientific_result_v1.source_tasks.contracts import JsonRef
    path, _, _ = generated
    index = Index(Package(path, "a"))
    store = Store(path.parent / "evidence")
    task = SimpleNamespace(task_kind="OUTPUT_AUDIT", output_alias="a", task_id="audit")
    owned = JsonRef(kind="json", output_alias="a", pointer="/results/1/fields/0")
    match = index.audit_match(task, store, [owned, owned])
    assert match.state == "MATCHED" and match.candidate_refs == [owned]
    assert match.matches == []  # No scientific coverage/omission determination.
    assert json.loads(store.read(match.coverage.inspected_component_ids)) == sorted(index.components)
    assert index.audit_match(task, store, []).state == "ABSENT"
    with pytest.raises(ValueError, match="Cross-pipeline"):
        index.audit_match(task, store, [owned.model_copy(update={"output_alias": "b"})])


def test_archive_maps_data_rows_around_original_csv_section_labels(generated):
    from validator.scientific_result_v1.source_tasks.packets import source_data_rows
    from validator.scientific_result_v1.source_tasks.contracts import (
        EvaluationBinding,TaskCard,MatchResult,SearchCoverage,JsonRef)
    path,doc,content=generated
    rows=content.splitlines()
    (path/'P005.E001.T001.csv').write_text(rows[0]+'\nSection label,,,\n'+'\n'.join(rows[1:])+'\n')
    doc['tables'][0]['row_count']=3
    for result in doc['results']:
        for field in result['fields']:
            for ref in field.get('table_references',[]):ref['row']+=1
    for annotation in doc['tables'][0]['annotations']:
        if 'rows' in annotation:annotation['rows']=[row+1 for row in annotation['rows']]
    (path/'output.json').write_text(json.dumps(doc))
    package=Package(path,'a');assert package.gate.gate_bit==1
    index=Index(package);table=doc['tables'][0];store=Store(path.parent/'row-map-store')
    physical=source_data_rows(table,index.package.csv[table['id']],2)
    assert physical==[2,3]
    index.table_data_rows[table['id']]=physical
    index.table_uid_by_output={table['id']:'source-table'}
    hits=index.source_hits({'anchors':[{'anchor_id':'source-row','document_uid':'doc','exact_text':'Marker A',
        'locator':{'kind':'table','table_uid':'source-table','first_row':0,'stop_row':1,'column_names':[]}}]})
    assert '/results/0/fields/0' in hits
    assert '/results/1/fields/0' not in hits
    binding=EvaluationBinding(protocol_id=PROTOCOL,paper_id='P005',round_id='r',stage='final',
        source_packet_sha256='1'*64,source_frame_sha256='2'*64,input_pool_sha256='3'*64,policy_sha256='4'*64,revision=0)
    task=TaskCard(task_id='archive',task_kind='ARCHIVE',binding=binding,tile_ids=['tile'],output_alias=None,source_bucket_id=None,
        requirements=[{'requirement_id':'archive-row','statement':'Preserve the selected row','qualifications':[],
                       'evidence_anchor_ids':['row-a'],'encoding_constraints':[]}],
        evidence_anchor_ids=['row-a'],source_scope_note='One original source data row')
    ids=sorted(index.components);manifest=store.put(ids)
    match=MatchResult(task_id=task.task_id,output_alias='a',state='MATCHED',matches=[],
        candidate_refs=[JsonRef(kind='json',output_alias='a',pointer='/tables/0')],
        coverage=SearchCoverage(index_sha256=index.sha256,available_component_ids=manifest,inspected_component_ids=manifest,
            available_components=len(ids),inspected_components=len(ids),exhaustive_fallback_completed=True),explanation='Known table')
    packet=index.packet(task,binding,match,'5'*64,[],table_ranges={table['id']:(0,1)})
    assert {block.first_row for block in packet.csv_blocks}=={1,2}
    assert next(block.rows[0][0] for block in packet.csv_blocks if block.first_row==1)=='Section label'
    assert next(block.rows[0][0] for block in packet.csv_blocks if block.first_row==2)=='Marker A (mg/L)'


def test_batch_absence_does_not_mean_global_omission(generated):
    from validator.scientific_result_v1.source_tasks.contracts import MatchResponse,TaskCard,EvaluationBinding
    path,doc,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'batch-store')
    binding=EvaluationBinding(protocol_id=PROTOCOL,paper_id='P005',round_id='r',stage='final',source_packet_sha256='1'*64,
        source_frame_sha256='2'*64,input_pool_sha256='3'*64,policy_sha256='4'*64,revision=0)
    task=TaskCard(task_id='identity',task_kind='ARCHIVE',binding=binding,tile_ids=['tile'],output_alias=None,source_bucket_id=None,
        requirements=[{'requirement_id':'table','statement':'Identify the marker source table','qualifications':[],
            'evidence_anchor_ids':['source-row'],'encoding_constraints':[]}],evidence_anchor_ids=['source-row'],source_scope_note='Table identity')
    class Scanner:
        def __init__(self):self.store=store;self.batches=[]
        def request(self,role,operation,data,response_type,validate,**kwargs):
            assert data['scan_scope']=='THIS_BATCH_ONLY'
            assert data['context_headers_are_partial'] is True
            keys=list(data['components']);self.batches.append(keys)
            refs=[{'kind':'json','output_alias':'a','pointer':'/tables/0'}] if '/tables/0' in keys else []
            response=MatchResponse.model_validate({'task_id':task.task_id,'output_alias':'a','state':'MATCHED' if refs else 'ABSENT',
                'inspected_component_ids':keys,'matches':[{'requirement_id':'table','output_refs':refs}],
                'explanation':'Only these original blocks were inspected in this batch.'},strict=True)
            validate(response);return response
    scanner=Scanner()
    match=index.match(task,scanner)
    assert len(scanner.batches)>1
    assert match.state=='MATCHED'
    assert match.coverage.exhaustive_fallback_completed
    assert set(key for batch in scanner.batches for key in batch)==set(index.components)
    # Relation-only batches get exact original identity/scope headers, not invented prose.
    context=index.match_context(['/relationships/0'])
    assert context
    for header in context.values():
        for prop in header['properties']:
            assert prop['value_json']==index.doc.literal(prop['original_ref']['pointer'])


def test_relation_only_match_renders_its_original_endpoints(generated):
    from validator.scientific_result_v1.source_tasks.contracts import (
        EvaluationBinding,TaskCard,MatchResult,SearchCoverage)
    path,_,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'relation-case')
    edge=index.relations[0]
    binding=EvaluationBinding(protocol_id=PROTOCOL,paper_id='P005',round_id='r',stage='final',source_packet_sha256='1'*64,
        source_frame_sha256='2'*64,input_pool_sha256='3'*64,policy_sha256='4'*64,revision=0)
    task=TaskCard(task_id='relation-case',task_kind='CONTENT',binding=binding,tile_ids=['tile'],output_alias=None,source_bucket_id=None,
        requirements=[{'requirement_id':'attachment','statement':'Verify the scientific attachment','qualifications':[],
            'evidence_anchor_ids':['source'],'encoding_constraints':[]}],evidence_anchor_ids=['source'],source_scope_note='Reported estimate with its uncertainty')
    manifest=store.put(sorted(index.components))
    match=MatchResult(task_id=task.task_id,output_alias='a',state='MATCHED',matches=[],candidate_refs=[edge.original_ref],
        coverage=SearchCoverage(index_sha256=index.sha256,available_component_ids=manifest,inspected_component_ids=manifest,
            available_components=len(index.components),inspected_components=len(index.components),exhaustive_fallback_completed=True),explanation='Original relationship is a candidate')
    packet=index.packet(task,binding,match,'5'*64,[])
    assert {edge.source_id,edge.target_id}<={o.object_id for o in packet.objects}
    assert edge in packet.relations


def packet_case(index, store, *, kind='CONTENT', refs=None, ranges=None):
    from validator.scientific_result_v1.source_tasks.contracts import EvaluationBinding, TaskCard, MatchResult, SearchCoverage
    binding=EvaluationBinding(protocol_id=PROTOCOL,paper_id='P005',round_id='r',stage='final',source_packet_sha256='1'*64,
        source_frame_sha256='2'*64,input_pool_sha256='3'*64,policy_sha256='4'*64,revision=0)
    task=TaskCard(task_id='case',task_kind=kind,binding=binding,tile_ids=['tile'],output_alias=None,source_bucket_id=None,
        requirements=[dict(requirement_id='target',statement='Preserve the selected finding',qualifications=[],
            evidence_anchor_ids=['source'],encoding_constraints=[])],evidence_anchor_ids=['source'],source_scope_note='Selected finding')
    manifest=store.put(sorted(index.components))
    match=MatchResult(task_id=task.task_id,output_alias='a',state='MATCHED',matches=[],candidate_refs=refs or [index.objects[0].original_ref],
        coverage=SearchCoverage(index_sha256=index.sha256,available_component_ids=manifest,inspected_component_ids=manifest,
            available_components=len(index.components),inspected_components=len(index.components),exhaustive_fallback_completed=True),explanation='Fixture correspondence')
    return task,index.packet(task,binding,match,'5'*64,[],table_ranges=ranges)


def test_shared_member_does_not_activate_every_consumer_closure(generated):
    from validator.scientific_result_v1.graph import closure_sets
    path,doc,_=generated
    left,right=doc['results'];shared=left['fields'][1]['id']
    doc['relationships'].append(dict(source_id=shared,target_id=right['id'],relation='applies_under',
        closure_direction='source_to_target',note='Shared contextual statistic'))
    for result in doc['results']:
        members,boundary=closure_sets(doc,result)
        result['closure']['member_ids']=sorted(members)
        result['closure']['reference_ids']=sorted(boundary)
    (path/'output.json').write_text(json.dumps(doc))
    package=Package(path,'a');assert package.gate.gate_bit==1
    index=Index(package);ref=next(o.original_ref for o in index.objects if o.object_id==left['fields'][0]['id'])
    _,packet=packet_case(index,Store(path.parent/'scoped'),refs=[ref])
    assert {c.result_id for c in packet.closures}=={left['id']}
    assert shared in {o.object_id for o in packet.objects}
    assert right['id'] in {o.object_id for o in packet.objects}  # Original boundary context is retained.
    assert not {f['id'] for f in right['fields']}&{o.object_id for o in packet.objects}


def test_lossless_judge_transport_preserves_literals_absence_edges_and_sidecars(generated):
    from validator.scientific_result_v1.source_tasks.transport import judge_packet,audit_packet
    from validator.scientific_result_v1.source_tasks.store import canonical
    path,doc,_=generated
    raw=(path/'output.json').read_text().replace('"value": 12.0','"value": 12.0000',1)
    (path/'output.json').write_text(raw)
    index=Index(Package(path,'a'));store=Store(path.parent/'transport')
    refs=[o.original_ref for o in index.objects if o.kind in {'RESULT','FIELD'}]
    _,packet=packet_case(index,store,refs=refs)
    wire=judge_packet(packet)
    assert audit_packet(wire)==packet
    assert len(canonical(wire))<len(canonical(packet))*.65
    pairs=lambda node:list(zip(wire['property_name_sets'][node['property_names_ref']],node['properties'])) if 'property_names_ref' in node else node['properties']
    assert any(literal=='12.0000' for node in wire['objects'] for _,literal in pairs(node))
    assert any(literal is None for node in wire['objects'] for _,literal in pairs(node))
    assert any(node['property_ref_overrides'] for node in wire['objects'])  # Root sidecar pointers.
    node=next(o for o in wire['objects'] if 'property_names_ref' in o)
    node['properties'].pop()
    with pytest.raises(ValueError,match='mismatched value count'):audit_packet(wire)


def test_archive_packet_keeps_row_links_without_all_scientific_closures(generated):
    path,doc,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'archive-scope')
    table=next(o for o in index.objects if o.kind=='TABLE')
    _,packet=packet_case(index,store,kind='ARCHIVE',refs=[table.original_ref],ranges={table.object_id:(0,1)})
    assert not packet.closures
    assert doc['results'][0]['fields'][0]['id'] in {o.object_id for o in packet.objects}
    assert doc['results'][1]['fields'][0]['id'] not in {o.object_id for o in packet.objects}
    assert {b.first_row for b in packet.csv_blocks}=={1}


@pytest.mark.parametrize('outcome', ['resolved','ambiguous','duplicate','invalid_pointer'])
def test_table_identity_inspects_only_table_objects_and_arbitrates(generated,outcome):
    import copy
    from validator.scientific_result_v1.source_tasks.contracts import MatchResponse
    from validator.scientific_result_v1.source_tasks.table_matching import resolve_table
    from validator.scientific_result_v1.source_tasks.operations import Hold
    path,doc,csv=generated
    other=copy.deepcopy(doc['tables'][0]);other.update(id='P005.E001.T002',csv_file='P005.E001.T002.csv',title='Different source table')
    doc['tables'].append(other);(path/'P005.E001.T002.csv').write_text(csv);(path/'output.json').write_text(json.dumps(doc))
    index=Index(Package(path,'a'));task,_=packet_case(index,Store(path.parent/'table-identity'))
    task=task.model_copy(update={'requirements':[task.requirements[0].model_copy(update={'requirement_id':'table-identity'})]})
    class Script:
        def __init__(self):self.roles=[]
        def request(self,role,operation,data,response_type,validate,**kwargs):
            assert data['scan_scope']=='TABLE_IDENTITY_ONLY'
            assert set(data['components'])=={'/tables/0','/tables/1'}
            if role!='ARBITER':assert 'independent_table_reviews' not in data
            self.roles.append(role)
            refs=[{'kind':'json','output_alias':'a','pointer':'/tables/'+('1' if role=='SECONDARY' else '0')}]
            state='MATCHED'
            if role=='ARBITER':
                if outcome=='ambiguous':state='AMBIGUOUS'
                if outcome=='duplicate':refs.append({'kind':'json','output_alias':'a','pointer':'/tables/1'})
                if outcome=='invalid_pointer':refs=[{'kind':'json','output_alias':'a','pointer':'/results/0'}]
            response=MatchResponse.model_validate(dict(task_id=task.task_id,output_alias='a',state=state,
                inspected_component_ids=list(data['components']),matches=[dict(requirement_id='table-identity',output_refs=refs)],
                explanation='Original source table identity'))
            validate(response);return response
    script=Script()
    if outcome=='ambiguous':
        with pytest.raises(Hold,match='after independent arbitration'):resolve_table(index,script,task,{'anchors':[]})
    elif outcome=='invalid_pointer':
        with pytest.raises(ValueError,match='TableObject roots'):resolve_table(index,script,task,{'anchors':[]})
    else:
        ids=resolve_table(index,script,task,{'anchors':[]})
        assert ids==({'P005.E001.T001','P005.E001.T002'} if outcome=='duplicate' else {'P005.E001.T001'})
    assert script.roles==['PRIMARY','SECONDARY','ARBITER']


def test_match_rejects_unassigned_candidate_noise(generated):
    from validator.scientific_result_v1.source_tasks.contracts import MatchResponse
    path,_,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'candidate-noise')
    task,_=packet_case(index,store)
    class Script:
        def __init__(self):self.store=store
        def request(self,role,operation,data,response_type,validate,**kwargs):
            response=MatchResponse.model_validate(dict(task_id=task.task_id,output_alias='a',state='MATCHED',
                inspected_component_ids=list(data['components']),matches=[dict(requirement_id='target',output_refs=[])],
                candidate_refs=[index.objects[0].original_ref.model_dump(mode='json')],explanation='Unassigned context'))
            validate(response);return response
    with pytest.raises(ValueError,match='Extra inputs are not permitted'):index.match(task,Script())


def test_source_transport_factors_exact_document_identity_and_hash():
    from validator.scientific_result_v1.source_tasks.transport import judge_evidence,audit_evidence
    evidence={'anchors':[dict(anchor_id='a'+str(i),document_uid='original-document-uid',source_sha256='a'*64,
        locator={'kind':'text','start':i,'stop':i+1},exact_text='Original text '+str(i),rendering=None) for i in range(20)]}
    wire=judge_evidence(evidence)
    assert audit_evidence(wire)==evidence
    assert all(a['document_ref']=='D0' for a in wire['anchors'])
    assert all('source_sha256' not in a and 'document_uid' not in a for a in wire['anchors'])
    assert wire['documents']['D0']['document_uid']=='original-document-uid'
    wire['documents']['D0']['source_sha256']='b'*64
    assert audit_evidence(wire)!=evidence  # Retained-source equality in replay catches this.


def test_context_property_matches_do_not_import_unrelated_measurement_closures(generated):
    from validator.scientific_result_v1.source_tasks.contracts import JsonRef
    path,doc,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'context-focus')
    refs=[JsonRef(kind='json',output_alias='a',pointer='/results/'+str(i)+'/context') for i in range(len(doc['results']))]
    _,packet=packet_case(index,store,refs=refs)
    assert not packet.closures
    assert not any(o.kind=='FIELD' for o in packet.objects)
    assert {r['id'] for r in doc['results']}<={o.object_id for o in packet.objects}
    for obj in packet.objects:
        if obj.kind=='RESULT':assert any(p.name=='context' and p.present for p in obj.properties)
    # A value-level reference still brings the entire local quantitative closure.
    _,numeric=packet_case(index,store,refs=[JsonRef(kind='json',output_alias='a',pointer='/results/0/fields/0/value')])
    assert {c.result_id for c in numeric.closures}=={doc['results'][0]['id']}
    assert {f['id'] for f in doc['results'][0]['fields']}<={o.object_id for o in numeric.objects}


def test_audit_groups_keep_all_assertions_once_and_join_record_relations(generated):
    from collections import Counter
    from validator.scientific_result_v1.source_tasks.audit_groups import assertion_groups
    from validator.scientific_result_v1.source_tasks.store import canonical
    path,doc,_=generated;index=Index(Package(path,'a'))
    owned=[o.original_ref for o in index.objects]+[e.original_ref for e in index.relations]
    groups=assertion_groups(index,owned)
    assert Counter(canonical(r) for refs in groups for r in refs)==Counter(map(canonical,owned))
    assert len(groups)<len(owned)//2
    for i,result in enumerate(doc['results']):
        refs={canonical(o.original_ref) for o in index.objects if o.original_ref.pointer=='/results/'+str(i) or o.original_ref.pointer.startswith('/results/'+str(i)+'/fields/')}
        group=next(g for g in groups if refs&{canonical(r) for r in g})
        assert refs<={canonical(r) for r in group}
        assert any(r.pointer.startswith('/relationships/') for r in group)


def test_context_requirements_share_definitions_without_forcing_a_giant_joint_case(generated):
    from validator.scientific_result_v1.source_tasks.contracts import JsonRef,RequirementMatch
    from validator.scientific_result_v1.source_tasks.sharding import partition,validate_child
    path,_,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'context-shards')
    ref=JsonRef(kind='json',output_alias='a',pointer='/results/0/context')
    task,base=packet_case(index,store,refs=[ref]);reqs=[task.requirements[0].model_copy(update={'requirement_id':'context-'+str(i)}) for i in range(2)]
    task=task.model_copy(update={'scope_kind':'SOURCE_CONTEXT','requirements':reqs})
    match=base.match.model_copy(update={'matches':[RequirementMatch(requirement_id=r.requirement_id,output_refs=[ref]) for r in reqs]})
    packet=index.packet(task,task.binding,match,'5'*64,[])
    def budget(text):
        node=json.loads(text)
        return 1000+20000*len(node['match']['matches'])
    shards=partition(index,task,packet,tokenizers={'test':budget})
    assert len(shards)==2
    assert {r.requirement_id for child,_ in shards for r in child.requirements}=={r.requirement_id for r in reqs}
    for child,rendered in shards:
        validate_child(child,task)
        assert rendered.match.matches[0].output_refs==[ref]
    # Scientific requirements may repeat a FULL joint finding; no quantity,
    # uncertainty, endpoint or closure may be partitioned away to make it fit.
    scientific=task.model_copy(update={'scope_kind':'SCIENTIFIC_RESULT'})
    core=next(o.original_ref for o in index.objects if o.kind=='FIELD')
    match=match.model_copy(update={'candidate_refs':[core],
        'matches':[RequirementMatch(requirement_id=r.requirement_id,output_refs=[core]) for r in reqs]})
    packet=index.packet(scientific,scientific.binding,match,'5'*64,[])
    scientific_shards=partition(index,scientific,packet,tokenizers={'test':budget})
    assert len(scientific_shards)==2 and packet.closures
    for _,rendered in scientific_shards:
        assert rendered.objects==packet.objects
        assert rendered.relations==packet.relations
        assert rendered.closures==packet.closures
        assert rendered.evidence_anchor_ids==packet.evidence_anchor_ids


def test_context_association_headers_are_partial_original_definitions(generated):
    from validator.scientific_result_v1.source_tasks.contracts import JsonRef
    from validator.scientific_result_v1.source_tasks.transport import judge_packet,audit_packet
    path,_,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'context-headers')
    edge=index.relations[0]
    ref=JsonRef(kind='json',output_alias='a',pointer=edge.original_ref.pointer)
    task,base=packet_case(index,store,refs=[ref]);task=task.model_copy(update={'scope_kind':'SOURCE_CONTEXT'})
    packet=index.packet(task,task.binding,base.match,'5'*64,[])
    assert packet.context_headers and edge in packet.relations
    assert not packet.closures
    for header in packet.context_headers:
        original=next(o for o in index.objects if o.object_id==header.object_id)
        assert all(p in original.properties for p in header.properties)
        assert all(p.name!='value' for p in header.properties)
    assert audit_packet(judge_packet(packet))==packet


def test_request_bound_judge_schema_rejects_wrong_bindings_dimensions_and_pointers(generated):
    import copy,jsonschema
    from validator.scientific_result_v1.source_tasks.contracts import JudgeResponse
    from validator.scientific_result_v1.source_tasks.response_schema import response_schema
    from validator.scientific_result_v1.source_tasks.transport import judge_packet
    from validator.scientific_result_v1.source_tasks.judging import ARCHIVE
    path,_,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'bound-schema')
    table=next(o for o in index.objects if o.kind=='TABLE')
    task,packet=packet_case(index,store,kind='ARCHIVE',refs=[table.original_ref],ranges={table.object_id:(0,1)})
    dims=sorted(ARCHIVE)
    data=dict(task=task.model_dump(mode='json'),packet=judge_packet(packet),required_dimensions=dims,authoritative_evidence={'anchors':[{'anchor_id':'source'}]})
    schema=response_schema(JudgeResponse,data)
    valid=dict(task_id=task.task_id,packet_id=packet.packet_id,outcome='ASSESSED',
        checks={d:dict(dimension=d,status='PASS',evidence_anchor_ids=['source'],counterexamples=[]) for d in dims},
        evidence_requests=[],target_issue=None,unresolved_reason=None,technical_error=None)
    jsonschema.validate(valid,schema)
    for key in ['task_id','packet_id']:
        bad=copy.deepcopy(valid);bad[key]='wrong'
        with pytest.raises(jsonschema.ValidationError):jsonschema.validate(bad,schema)
    bad=copy.deepcopy(valid);bad['checks'][dims[0]]['dimension']='NUMERIC_FIDELITY'
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(bad,schema)
    bad=copy.deepcopy(valid);bad['outcome']='SOURCE_UNRESOLVED';bad['unresolved_reason']='Uncertain'
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(bad,schema)
    failure=copy.deepcopy(valid);failure['checks'][dims[0]].update(status='FAIL',counterexamples=[dict(code='TABLE_VALUE',rule_ids=['V21'],
        description='Value differs',evidence_anchor_ids=['source'],output_refs=[dict(kind='json',output_alias='a',pointer='/tables/0')],missing_requirement_id=None)])
    jsonschema.validate(failure,schema)
    failure['checks'][dims[0]]['counterexamples'][0]['output_refs'][0]['pointer']='/nonexistent'
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(failure,schema)


def test_family_context_does_not_replace_focal_result_or_import_unrelated_closures(generated):
    from validator.scientific_result_v1.source_tasks.transport import judge_packet,audit_packet
    path,doc,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'family-headers')
    target=next(o for o in index.objects if o.kind=='RESULT')
    _,packet=packet_case(index,store,refs=[target.original_ref])
    assert target in packet.objects
    assert any(c.result_id==target.object_id for c in packet.closures)
    other=doc['results'][1]['id']
    assert other in {h.object_id for h in packet.context_headers}
    assert other not in {o.object_id for o in packet.objects}
    header=next(h for h in packet.context_headers if h.object_id==other)
    assert not any(p.name in {'closure','value','source_snippets','summary'} for p in header.properties)
    assert any(o.kind=='FAMILY' for o in packet.objects)
    assert audit_packet(judge_packet(packet))==packet


def test_pooled_literals_preserve_exact_original_strings_and_reject_unknown_refs(generated):
    import copy
    from validator.scientific_result_v1.source_tasks.transport import judge_packet,audit_packet
    path,_,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'pooled-literals')
    refs=[o.original_ref for o in index.objects if o.kind in {'RESULT','FIELD'}]
    _,packet=packet_case(index,store,refs=refs)
    # A repeated original literal containing exact numeric spelling and whitespace
    # must not be parsed/reserialized, rounded or treated as a missing assertion.
    literal='{"estimate":12.0000, "note":"'+('original evidence '*15)+'"}'
    data=packet.model_dump(mode='json')
    for node in data['objects'][:2]:
        prop=node['properties'][0];prop.update(present=True,value_json=literal)
    from validator.scientific_result_v1.source_tasks.contracts import AssertionPacket
    packet=AssertionPacket.model_validate(data);wire=judge_packet(packet)
    assert literal in wire['literal_pool'].values()
    assert audit_packet(wire)==packet
    damaged=copy.deepcopy(wire);node=damaged['objects'][0]
    if 'property_names_ref' in node:node['properties'][0]={'literal_ref':'not-retained'}
    else:node['properties'][0][1]={'literal_ref':'not-retained'}
    with pytest.raises(ValueError,match='Unknown original literal'):
        audit_packet(damaged)


@pytest.mark.parametrize('failed_batch',[False,True])
def test_parallel_matching_preserves_complete_search_order_and_failed_attempts(generated,failed_batch):
    import math,threading
    from validator.scientific_result_v1.source_tasks.contracts import MatchResponse
    from validator.scientific_result_v1.source_tasks.operations import Hold
    path,_,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'parallel-matching')
    task,_=packet_case(index,store)
    batches=1+math.ceil(max(0,len(index.components)-12)/32)
    wave=min(4,batches)
    assert wave>1

    class State:
        def __init__(self,parallel,fail=False):
            self.parallel=parallel;self.fail=fail;self.lock=threading.Lock()
            self.started=0;self.active=0;self.peak=0;self.seen=[]
            self.barrier=threading.Barrier(wave)

    class Child:
        def __init__(self,state):
            self.state=state;self.store=store;self.used=[];self.run_refs=[]
            self.packet_refs=[];self.response_refs={}
        def request(self,role,operation,data,response_type,validate,**kwargs):
            state=self.state;keys=data['inspection_component_ids']
            with state.lock:
                ordinal=state.started;state.started+=1;state.active+=1
                state.peak=max(state.peak,state.active);state.seen.extend(keys)
            try:
                if state.parallel and ordinal<wave:state.barrier.wait(timeout=5)
                label=keys[0];self.used.append(label);self.run_refs.append(label)
                if state.fail and ordinal==0:raise Hold('EVALUATOR','injected incomplete scan')
                refs=[dict(kind='json',output_alias='a',pointer=key) for key in keys if key=='/tables/0']
                result=MatchResponse.model_validate(dict(task_id=task.task_id,output_alias='a',
                    state='MATCHED' if refs else 'ABSENT',inspected_component_ids=keys,
                    matches=[dict(requirement_id=r.requirement_id,output_refs=refs) for r in task.requirements],
                    explanation='This independent original batch only'))
                validate(result);return result
            finally:
                with state.lock:state.active-=1

    class Parent(Child):
        def scoped(self):return Child(self.state)

    state=State(True,failed_batch);parent=Parent(state)
    if failed_batch:
        with pytest.raises(Hold,match='injected incomplete scan'):index.match(task,parent)
        assert len(parent.run_refs)==batches  # All dispatched ledgers survive failure.
    else:
        parallel=index.match(task,parent);serial_ops=Child(State(False))
        serial=index.match(task,serial_ops)
        assert parallel==serial  # Includes ordered matches and complete manifests.
        assert parent.used==serial_ops.used
        assert parallel.coverage.exhaustive_fallback_completed
    assert wave<=state.peak<=4
    assert len(state.seen)==len(index.components)
    assert set(state.seen)==set(index.components)


def test_property_scoped_context_keeps_matched_metadata_without_outcome_values(generated):
    from validator.scientific_result_v1.source_tasks.contracts import JsonRef
    from validator.scientific_result_v1.source_tasks.transport import judge_packet,audit_packet
    path,_,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'property-context')
    field=next(o for o in index.objects if o.kind=='FIELD' and any(p.name=='value' and p.present for p in o.properties))
    descriptor=next(p for p in field.properties if p.name=='field')
    task,base=packet_case(index,store,refs=[descriptor.original_ref]);task=task.model_copy(update={'scope_kind':'SOURCE_CONTEXT'})
    packet=index.packet(task,task.binding,base.match,'5'*64,[])
    header=next(o for o in packet.context_headers if o.object_id==field.object_id)
    assert descriptor in header.properties
    assert not any(p.name=='value' for p in header.properties)
    assert field.object_id not in {o.object_id for o in packet.objects}
    assert not packet.closures
    assert audit_packet(judge_packet(packet))==packet
    # An actual source-context input quantity remains a full original field.
    value=next(p for p in field.properties if p.name=='value')
    match=base.match.model_copy(update={'candidate_refs':[value.original_ref]})
    quantity=index.packet(task,task.binding,match,'5'*64,[])
    assert field in quantity.objects
    assert not any(o.object_id==field.object_id for o in quantity.context_headers)


def test_property_name_templates_roundtrip_and_reject_invalid_templates(generated):
    import copy
    from validator.scientific_result_v1.source_tasks.transport import judge_packet,audit_packet
    path,_,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'property-names')
    _,packet=packet_case(index,store,refs=[o.original_ref for o in index.objects if o.kind in {'RESULT','FIELD'}])
    wire=judge_packet(packet)
    assert wire['property_name_sets'] and audit_packet(wire)==packet
    corrupted=copy.deepcopy(wire)
    node=next(o for o in corrupted['objects'] if 'property_names_ref' in o)
    node['property_names_ref']='not-retained'
    with pytest.raises(ValueError,match='Unknown property-name set'):audit_packet(corrupted)


def test_keyed_matcher_wire_requires_all_requirements_and_preserves_empty_answers():
    from validator.scientific_result_v1.source_tasks.response_schema import response_schema
    from validator.scientific_result_v1.source_tasks.runtime import StrictAdapter
    from validator.scientific_result_v1.source_tasks.contracts import MatchResponse
    import jsonschema
    data={'task':{'task_id':'task','requirements':[{'requirement_id':'r1'},{'requirement_id':'r2'}]},'output_alias':'a','components':{},'inspection_component_ids':[]}
    schema=response_schema(MatchResponse,data);adapter=StrictAdapter(schema)
    answer=dict(task_id='task',output_alias='a',state='ABSENT',inspected_component_ids=[],matches={'r2':[],'r1':[]},explanation='Both requirements absent in this batch')
    jsonschema.validate(answer,schema)
    result=MatchResponse.model_validate(adapter.parse(None,json.dumps(answer)))
    assert [m.requirement_id for m in result.matches]==['r1','r2']
    assert all(m.output_refs==[] for m in result.matches)
    del answer['matches']['r2']
    with pytest.raises(ValueError,match='every requirement exactly once'):adapter.parse(None,json.dumps(answer))


def test_shared_table_context_does_not_join_independent_scientific_requirements(generated):
    from validator.scientific_result_v1.source_tasks.contracts import RequirementMatch
    from validator.scientific_result_v1.source_tasks.sharding import partition
    from validator.scientific_result_v1.source_tasks.transport import audit_packet
    path,_,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'scientific-context-shards')
    results=[o for o in index.objects if o.kind=='RESULT'];table=next(o for o in index.objects if o.kind=='TABLE')
    task,base=packet_case(index,store,refs=[results[0].original_ref,results[1].original_ref,table.original_ref])
    requirements=[task.requirements[0].model_copy(update={'requirement_id':key}) for key in ['first','second']]
    task=task.model_copy(update={'requirements':requirements})
    matches=[RequirementMatch(requirement_id=r.requirement_id,output_refs=[o.original_ref,table.original_ref]) for r,o in zip(requirements,results)]
    match=base.match.model_copy(update={'matches':matches})
    packet=index.packet(task,task.binding,match,'5'*64,[])
    def bound(text):return 32001 if len(audit_packet(json.loads(text)).closures)>1 else 1000
    shards=partition(index,task,packet,tokenizers={'test':bound})
    assert len(shards)==2
    assert {r.requirement_id for child,_ in shards for r in child.requirements}=={'first','second'}
    for child,rendered in shards:
        assert len(rendered.closures)==1
        assert table.original_ref in rendered.match.candidate_refs
        assert any(o.object_id==table.object_id for o in rendered.objects)


def test_one_complete_scientific_requirement_cannot_be_split_into_partial_candidates(generated):
    from validator.scientific_result_v1.source_tasks.contracts import RequirementMatch
    from validator.scientific_result_v1.source_tasks.sharding import partition
    from validator.scientific_result_v1.source_tasks.transport import audit_packet
    from validator.scientific_result_v1.source_tasks.operations import Hold
    path,_,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'joint-source-requirement')
    refs=[o.original_ref for o in index.objects if o.kind=='RESULT']
    task,base=packet_case(index,store,refs=refs)
    match=base.match.model_copy(update={'matches':[RequirementMatch(requirement_id=task.requirements[0].requirement_id,output_refs=refs)]})
    packet=index.packet(task,task.binding,match,'5'*64,[])
    def bound(text):return 32001 if len(audit_packet(json.loads(text)).closures)>1 else 1000
    with pytest.raises(Hold,match='No complete independent partition'):
        partition(index,task,packet,tokenizers={'test':bound})


def test_context_property_can_share_a_component_root_pointer(generated):
    from validator.scientific_result_v1.source_tasks.contracts import MatchResponse
    path,_,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'property-component-overlap')
    present={p.original_ref.pointer for o in index.objects for p in o.properties if p.present}
    pointer=next(p for p in ['/extraction','/extraction/notes'] if p in present and p in index.components)
    task,_=packet_case(index,store);task=task.model_copy(update={'scope_kind':'SOURCE_CONTEXT'})
    class Scanner:
        def __init__(self):self.store=store
        def request(self,role,operation,data,response_type,validate,**kwargs):
            keys=data['inspection_component_ids'];refs=[dict(kind='json',output_alias='a',pointer=pointer)] if pointer in keys else []
            response=MatchResponse.model_validate(dict(task_id=task.task_id,output_alias='a',state='MATCHED' if refs else 'ABSENT',inspected_component_ids=keys,
                matches=[dict(requirement_id=r.requirement_id,output_refs=refs) for r in task.requirements],explanation='Original metadata property is also an index component'))
            validate(response);return response
    match=index.match(task,Scanner())
    assert [r.pointer for r in match.candidate_refs]==[pointer]
    assert match.coverage.exhaustive_fallback_completed


@pytest.mark.parametrize('paper_key',['P005','openalex_w123456789','study-2026_trial'])
def test_v4_transport_factors_only_exact_headers_and_closure_views(generated,paper_key):
    from validator.scientific_result_v1.source_tasks.transport import judge_packet,audit_packet
    path,_,_=generated
    if paper_key!='P005':
        (path/'output.json').write_text((path/'output.json').read_text().replace('P005',paper_key))
        (path/'P005.E001.T001.csv').rename(path/(paper_key+'.E001.T001.csv'))
    index=Index(Package(path,'long-output-alias-'+'a'*64))
    _,packet=packet_case(index,Store(path.parent/'v4-transport'),refs=[o.original_ref for o in index.objects if o.kind=='RESULT'])
    wire=judge_packet(packet)
    assert any(o.get('object_id_from_property') for o in wire['objects'])
    assert any(e.get('headers_from_properties') for e in wire['relations'])
    assert any(c.get('submitted_from_original_closure') for c in wire['closures'])
    assert wire['literal_identifier_pool']
    assert audit_packet(wire)==packet
    # A different recomputation order is evidence, and must remain explicit.
    closure=packet.closures[0]
    assert len(closure.recomputed_member_ids)>1
    changed=packet.model_copy(update={'closures':[closure.model_copy(update={'recomputed_member_ids':list(reversed(closure.recomputed_member_ids))}),*packet.closures[1:]]})
    encoded=judge_packet(changed)
    assert 'recomputed_member_ids' in encoded['closures'][0]
    assert audit_packet(encoded)==changed


def test_v4_identifier_pool_avoids_literal_marker_collision_and_rejects_unknown_ids(generated):
    import copy
    from validator.scientific_result_v1.source_tasks.transport import judge_packet,audit_packet
    path,_,_=generated;index=Index(Package(path,'a'))
    _,packet=packet_case(index,Store(path.parent/'v4-collision'),refs=[o.original_ref for o in index.objects if o.kind=='RESULT'])
    node=packet.objects[0];prop=next(p for p in node.properties if p.present and p.name!='id')
    changed_prop=prop.model_copy(update={'value_json':json.dumps('original marker ~0; and '+node.object_id)})
    changed_node=node.model_copy(update={'properties':[changed_prop if p.name==prop.name else p for p in node.properties]})
    changed=packet.model_copy(update={'objects':[changed_node,*packet.objects[1:]]})
    wire=judge_packet(changed)
    assert wire['literal_identifier_prefix']=='~~'
    assert audit_packet(wire)==changed
    corrupted=copy.deepcopy(wire);corrupted['literal_identifier_pool'].pop(next(iter(corrupted['literal_identifier_pool'])))
    with pytest.raises(ValueError,match='Unknown original identifier reference'):audit_packet(corrupted)
    corrupted=copy.deepcopy(wire);corrupted['literal_identifier_pool']=[]
    with pytest.raises(ValueError,match='Invalid original-identifier pool'):audit_packet(corrupted)
