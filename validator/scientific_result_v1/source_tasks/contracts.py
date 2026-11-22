# Wire contracts from Christian’s Claims Validator Implementation Guide, Appendix B.
from __future__ import annotations

from fractions import Fraction
from math import gcd
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


class Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )


Id = Annotated[str, StringConstraints(min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Pointer = Annotated[str, StringConstraints(pattern=r"^(?:/(?:[^~/]|~[01])*)*$")]
Nonnegative = Annotated[int, Field(ge=0)]
Positive = Annotated[int, Field(gt=0)]
UnitFloat = Annotated[float, Field(ge=0, le=1)]
RuleId = Annotated[str, StringConstraints(pattern=r"^V(?:0[1-9]|[12][0-9]|30)$")]
Protocol = Literal["claims-source-task-v1"]
TaskKind = Literal["CONTENT", "ARCHIVE", "OUTPUT_AUDIT"]
Role = Literal["BRONZE", "BASELINE", "CHALLENGER"]
Bit = Annotated[int, Field(ge=0, le=1)]


def exact_int(value):
    if type(value) is not int:
        raise ValueError("an integer JSON token is required")
    return value


def exact_bool(value):
    if type(value) is not bool:
        raise ValueError("a Boolean JSON token is required")
    return value


class FileRef(Closed):
    relative_path: Id
    sha256: Sha256
    byte_length: Nonnegative
    media_type: Id

    @model_validator(mode="after")
    def safe_path(self):
        p = self.relative_path
        if (
            p.startswith("/")
            or "\\" in p
            or "\x00" in p
            or any(x in ("", ".", "..") for x in p.split("/"))
        ):
            raise ValueError("relative_path must be a normalized safe relative path")
        return self


class JsonRef(Closed):
    kind: Literal["json"]
    output_alias: Id
    pointer: Pointer


class CsvRef(Closed):
    kind: Literal["csv"]
    output_alias: Id
    filename: Id
    row: Nonnegative
    column: Nonnegative

    @model_validator(mode="after")
    def basename(self):
        if (
            "/" in self.filename
            or "\\" in self.filename
            or self.filename in (".", "..")
            or "\x00" in self.filename
        ):
            raise ValueError("filename must be a basename")
        return self


OutputRef = Annotated[JsonRef | CsvRef, Field(discriminator="kind")]


class ErrorRecord(Closed):
    category: Literal["PIPELINE", "EVALUATOR", "SOURCE", "MODEL"]
    code: Id
    stage: Id
    message: Id
    retryable: bool
    output_refs: list[OutputRef]


class SourceDocument(Closed):
    document_uid: Id
    role: Literal["MAIN", "SUPPLEMENT", "REFERENCED_SOURCE"]
    title: Id
    version: Id | None
    url: Id | None
    required: bool
    acquisition_status: Literal["SUPPLIED", "RETRIEVED", "UNAVAILABLE"]
    availability: Literal["AVAILABLE", "EXPECTED_MISSING"]
    file: FileRef | None

    @model_validator(mode="after")
    def availability_matches(self):
        if (self.availability == "AVAILABLE") != (self.file is not None):
            raise ValueError("availability/file mismatch")
        if (self.availability == "EXPECTED_MISSING") != (
            self.acquisition_status == "UNAVAILABLE"
        ):
            raise ValueError("acquisition/availability mismatch")
        return self


class SourcePacket(Closed):
    protocol_id: Protocol
    paper_id: Id
    round_id: Id
    state: Literal["READY", "HOLD"]
    documents: Annotated[list[SourceDocument], Field(min_length=1)]
    errors: list[ErrorRecord]


class SubmittedPackage(Closed):
    protocol_id: Protocol
    paper_id: Id
    round_id: Id
    output_alias: Id
    pipeline_digest: Sha256
    roles: Annotated[list[Role], Field(min_length=1)]
    source_packet_sha256: Sha256
    execution_receipt: FileRef
    state: Literal["COMPLETED", "PACKAGE_FAILURE", "INFRASTRUCTURE_FAILURE"]
    extraction_json: FileRef | None
    table_csv_files: list[FileRef]
    errors: list[ErrorRecord]

    @model_validator(mode="after")
    def completed_has_artifact(self):
        if self.state == "COMPLETED" and self.extraction_json is None:
            raise ValueError("completed package requires extraction_json")
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("duplicate pipeline roles")
        return self


class PageLocator(Closed):
    kind: Literal["page"]
    page_index: Nonnegative
    bbox: tuple[UnitFloat, UnitFloat, UnitFloat, UnitFloat]

    @model_validator(mode="after")
    def valid_bbox(self):
        x0, y0, x1, y1 = self.bbox
        if x0 >= x1 or y0 >= y1:
            raise ValueError("empty or reversed page rectangle")
        return self


class TextLocator(Closed):
    kind: Literal["text"]
    start: Nonnegative
    stop: Positive

    @model_validator(mode="after")
    def nonempty(self):
        if self.stop <= self.start:
            raise ValueError("empty text range")
        return self


class TableLocator(Closed):
    kind: Literal["table"]
    table_uid: Id
    first_row: Nonnegative
    stop_row: Positive
    column_names: list[Id]

    @model_validator(mode="after")
    def nonempty(self):
        if self.stop_row <= self.first_row:
            raise ValueError("empty table range")
        return self


SourceLocator = Annotated[
    PageLocator | TextLocator | TableLocator, Field(discriminator="kind")
]


class SourceAnchor(Closed):
    anchor_id: Id
    document_uid: Id
    source_sha256: Sha256
    locator: SourceLocator
    exact_text: str | None
    rendering: FileRef | None

    @model_validator(mode="after")
    def readable(self):
        if self.exact_text is None and self.rendering is None:
            raise ValueError("anchor needs trusted text or rendering")
        return self


class SourceTable(Closed):
    table_uid: Id
    document_uid: Id
    anchor_ids: Annotated[list[Id], Field(min_length=1)]
    numerical: bool
    data_row_count: Nonnegative
    layout_receipt: FileRef | None
    data_row_anchor_ids: list[Id]
    logical_column_names: list[Id]
    header_anchor_ids: list[Id]
    note_anchor_ids: list[Id]

    @model_validator(mode="after")
    def verified_rows(self):
        if self.data_row_count != len(self.data_row_anchor_ids):
            raise ValueError("source row count must equal verified ordered row anchors")
        if self.numerical and (self.layout_receipt is None or self.data_row_count == 0):
            raise ValueError(
                "numerical source table requires a complete verified layout"
            )
        return self


class SourceTile(Closed):
    tile_id: Id
    document_uid: Id
    stratum: Literal["PROSE_OR_EQUATION", "TABLE"]
    eligible_content: bool
    owned_anchor_ids: Annotated[list[Id], Field(min_length=1)]
    context_anchor_ids: list[Id]
    table_uid: Id | None
    first_data_row: Nonnegative | None
    stop_data_row: Positive | None

    @model_validator(mode="after")
    def table_shape(self):
        coords = (self.table_uid, self.first_data_row, self.stop_data_row)
        if self.stratum == "TABLE":
            if (
                any(x is None for x in coords)
                or self.stop_data_row <= self.first_data_row
            ):
                raise ValueError("TABLE tile needs a nonempty table row range")
        elif any(x is not None for x in coords):
            raise ValueError("prose/equation tile cannot carry table coordinates")
        return self


class SourceFrame(Closed):
    protocol_id: Protocol
    paper_id: Id
    source_packet_sha256: Sha256
    parser_config_sha256: Sha256
    revision: Nonnegative
    state: Literal["READY", "SOURCE_UNRESOLVED"]
    anchors: list[SourceAnchor]
    tables: list[SourceTable]
    tiles: list[SourceTile]
    excluded_region_anchor_ids: list[Id]
    errors: list[ErrorRecord]


class EvaluationBinding(Closed):
    protocol_id: Protocol
    paper_id: Id
    round_id: Id
    stage: Literal["prescreen", "screen", "final", "week_one"]
    source_packet_sha256: Sha256
    source_frame_sha256: Sha256
    input_pool_sha256: Sha256
    policy_sha256: Sha256
    revision: Nonnegative


class DocumentBinding(Closed):
    output_alias: Id
    source_id: Id
    state: Literal["BOUND", "VERIFIED_WRONG", "SOURCE_UNRESOLVED"]
    document_uid: Id | None
    evidence_anchor_ids: list[Id]
    explanation: Id


class EvidenceRequest(Closed):
    request_id: Id
    kind: Literal[
        "ADJACENT_TEXT",
        "DEFINITION",
        "TABLE_HEADERS",
        "TABLE_NOTES",
        "METHOD_CONTEXT",
        "SUPPLEMENT",
        "INVESTIGATION_SCOPE",
    ]
    anchor_ids: list[Id]
    document_uid: Id | None
    query: Id


class SourceRegionSlice(Closed):
    anchor_id: Id
    bbox: tuple[UnitFloat, UnitFloat, UnitFloat, UnitFloat] | None

    @model_validator(mode="after")
    def valid_bbox(self):
        if self.bbox is not None:
            x0, y0, x1, y1 = self.bbox
            if x0 >= x1 or y0 >= y1:
                raise ValueError("empty or reversed source region rectangle")
        return self


class SourceHeaderLevel(Closed):
    text: str
    anchor_ids: list[Id]


class SourceColumnLayout(Closed):
    part_key: Id
    local_key: Id
    role: Literal["DATA", "LABEL", "CONTEXT"]
    header_path: list[SourceHeaderLevel]
    description: Id | None


class SourceTablePart(Closed):
    part_key: Id
    anchor_ids: Annotated[list[Id], Field(min_length=1)]
    panel_label: Id | None
    panel_label_anchor_ids: list[Id]
    continuation_of_part_key: Id | None
    column_keys: Annotated[list[Id], Field(min_length=1)]


class SourceDataRowLayout(Closed):
    part_key: Id
    row_regions: Annotated[list[SourceRegionSlice], Field(min_length=1)]
    row_label_anchor_ids: list[Id]
    context_anchor_ids: list[Id]


class ExcludedTableRegion(Closed):
    kind: Literal[
        "SEPARATOR", "CONTINUATION_HEADER", "CAPTION", "FOOTNOTE", "PANEL_LABEL"
    ]
    anchor_ids: Annotated[list[Id], Field(min_length=1)]
    explanation: Id


class SourceTableLayout(Closed):
    document_uid: Id
    identity_evidence_anchor_ids: Annotated[list[Id], Field(min_length=1)]
    identity_note: Id
    parts: Annotated[list[SourceTablePart], Field(min_length=1)]
    columns: Annotated[list[SourceColumnLayout], Field(min_length=1)]
    rows: list[SourceDataRowLayout]
    header_anchor_ids: list[Id]
    note_anchor_ids: list[Id]
    excluded_regions: list[ExcludedTableRegion]


class SourcePartIdentity(Closed):
    part_key: Id
    source_order: Nonnegative
    anchor_ids: Annotated[list[Id], Field(min_length=1)]


class SourceTableLayoutRequest(Closed):
    request_id: Id
    table_uid: Id
    document_uid: Id
    chunk_id: Id
    requested_scope_anchor_ids: Annotated[list[Id], Field(min_length=1)]
    context_anchor_ids: list[Id]
    part_manifest: Annotated[list[SourcePartIdentity], Field(min_length=1)]
    split_depth: Annotated[int, Field(ge=0, le=8)]


class SourceTableLayoutResponse(Closed):
    request_id: Id
    table_uid: Id
    chunk_id: Id
    requested_scope_anchor_ids: Annotated[list[Id], Field(min_length=1)]
    complete_for_scope: bool
    outcome: Literal["READY", "NEED_EVIDENCE", "SOURCE_UNRESOLVED"]
    inspected_anchor_ids: list[Id]
    layout: SourceTableLayout | None
    evidence_requests: list[EvidenceRequest]
    unresolved_reason: Id | None

    @model_validator(mode="after")
    def legal_route(self):
        if (self.outcome == "READY") != (self.layout is not None):
            raise ValueError("only READY has a complete table layout")
        if (self.outcome == "READY") != self.complete_for_scope:
            raise ValueError("READY requires a complete layout for the requested scope")
        if (self.outcome == "NEED_EVIDENCE") != bool(self.evidence_requests):
            raise ValueError("evidence requests only for NEED_EVIDENCE")
        if (self.outcome == "SOURCE_UNRESOLVED") != (
            self.unresolved_reason is not None
        ):
            raise ValueError("unresolved_reason route mismatch")
        return self


class SourceColumnMember(Closed):
    part_key: Id
    local_key: Id


class SourceLogicalColumn(Closed):
    column_uid: Id
    role: Literal["DATA", "LABEL", "CONTEXT"]
    header_path: list[str]
    source_columns: Annotated[list[SourceColumnMember], Field(min_length=1)]


class SourceTableLayoutReceipt(Closed):
    table_uid: Id
    document_uid: Id
    source_packet_sha256: Sha256
    chunk_requests: Annotated[list[FileRef], Field(min_length=1)]
    chunk_responses: Annotated[list[FileRef], Field(min_length=1)]
    source_reviews: Annotated[list[FileRef], Field(min_length=1)]
    arbiter_responses: list[FileRef]
    ordered_data_row_anchor_ids: Annotated[list[Id], Field(min_length=1)]
    logical_columns: Annotated[list[SourceLogicalColumn], Field(min_length=1)]
    header_anchor_ids: list[Id]
    note_anchor_ids: list[Id]


class EncodingConstraint(Closed):
    rule_id: RuleId
    description: Id


class SourceRequirement(Closed):
    requirement_id: Id
    statement: Id
    analysis_scope: Id = "SOURCE_UNSPECIFIED"
    adjustment_scope: Id = "SOURCE_UNSPECIFIED"
    qualifications: list[Id]
    evidence_anchor_ids: Annotated[list[Id], Field(min_length=1)]
    encoding_constraints: list[EncodingConstraint]


class SourceTaskDraft(Closed):
    scope_kind: Literal["SCIENTIFIC_RESULT", "SOURCE_CONTEXT"]
    local_key: Id
    task_kind: Literal["CONTENT", "ARCHIVE"]
    requirements: Annotated[list[SourceRequirement], Field(min_length=1)]
    evidence_anchor_ids: Annotated[list[Id], Field(min_length=1)]
    source_scope_note: Id


class SourceTaskBatch(Closed):
    tile_id: Id
    outcome: Literal["READY", "NEED_EVIDENCE", "SOURCE_UNRESOLVED", "INVENTORY_DEFECT"]
    cards: list[SourceTaskDraft]
    evidence_requests: list[EvidenceRequest]
    unresolved_reason: Id | None

    @model_validator(mode="after")
    def legal_route(self):
        if self.outcome == "READY":
            if (
                not self.cards
                or self.evidence_requests
                or self.unresolved_reason is not None
            ):
                raise ValueError("READY requires nonempty cards and no error/request")
        elif self.cards:
            raise ValueError("non-READY batch cannot contain authoritative cards")
        if (self.outcome == "NEED_EVIDENCE") != bool(self.evidence_requests):
            raise ValueError("evidence requests only for NEED_EVIDENCE")
        if (self.outcome in ("SOURCE_UNRESOLVED", "INVENTORY_DEFECT")) != (
            self.unresolved_reason is not None
        ):
            raise ValueError("reason required exactly for unresolved/defective source")
        return self


class TaskCard(Closed):
    scope_kind: Literal["SCIENTIFIC_RESULT", "SOURCE_CONTEXT"] = "SCIENTIFIC_RESULT"
    task_id: Id
    task_kind: TaskKind
    binding: EvaluationBinding
    tile_ids: list[Id]
    output_alias: Id | None
    source_bucket_id: Id | None
    requirements: list[SourceRequirement]
    evidence_anchor_ids: list[Id]
    source_scope_note: Id

    @model_validator(mode="after")
    def requirements_match_kind(self):
        if (self.task_kind == "OUTPUT_AUDIT") != (len(self.requirements) == 0):
            raise ValueError("only OUTPUT_AUDIT uses an empty required-information set")
        if self.task_kind == "OUTPUT_AUDIT":
            if self.output_alias is None or self.source_bucket_id is None:
                raise ValueError("OUTPUT_AUDIT requires its pipeline and source bucket")
        elif (
            self.output_alias is not None
            or self.source_bucket_id is not None
            or not self.tile_ids
        ):
            raise ValueError("common source tasks require tiles and no pipeline owner")
        return self


class InventoryRegion(Closed):
    document_uid: Id
    anchor_ids: Annotated[list[Id], Field(min_length=1)]
    kind: Literal["PROSE_OR_EQUATION", "TABLE", "EXCLUDED"]
    has_extractable_science: bool
    table_uid: Id | None
    proposed_table_key: Id | None
    source_label: Id | None
    source_regions: list[SourceRegionSlice]
    reason: Id

    @model_validator(mode="after")
    def discovery_identity(self):
        if self.kind == "TABLE":
            if not self.source_regions or (self.table_uid is None and self.proposed_table_key is None):
                raise ValueError(
                    "TABLE discovery requires regions and either a verified table UID or a new source-local key"
                )
        elif self.proposed_table_key is not None or self.table_uid is not None:
            raise ValueError("only TABLE regions identify proposed source tables")
        return self


class CompanionMention(Closed):
    evidence_anchor_ids: Annotated[list[Id], Field(min_length=1)]
    source_label: Id
    document_uid: Id | None
    kind: Literal["ASSOCIATED_SUPPLEMENT", "EXPLICIT_IN_SCOPE_CITED_SOURCE"]
    source_url: Id | None
    required_for_scope: Annotated[Literal[True], BeforeValidator(exact_bool)]


class InventoryResponse(Closed):
    request_id: Id
    outcome: Literal["READY", "SOURCE_UNRESOLVED"]
    inspected_anchor_ids: list[Id]
    regions: list[InventoryRegion]
    companion_mentions: list[CompanionMention]
    unresolved_reason: Id | None


class InventoryRevision(Closed):
    kind: Literal["INVENTORY"]
    payload: InventoryResponse


class CardBatchRevision(Closed):
    kind: Literal["CARD_BATCH"]
    payload: SourceTaskBatch


class TableLayoutRevision(Closed):
    kind: Literal["TABLE_LAYOUT"]
    payload: SourceTableLayoutResponse


SourceProposal = Annotated[
    InventoryRevision | CardBatchRevision | TableLayoutRevision,
    Field(discriminator="kind"),
]


class SourceReviewIssue(Closed):
    code: Literal[
        "INVENTORY_OMISSION",
        "INVENTORY_FALSE_POSITIVE",
        "WRONG_TABLE_IDENTITY",
        "WRONG_TABLE_LAYOUT",
        "SOURCE_MISSTATEMENT",
        "MISSING_REQUIRED_INFORMATION",
        "INVENTED_ENCODING",
        "WRONG_SCOPE",
        "INCOMPLETE_COVERAGE",
        "COMPANION_MISSING",
    ]
    proposal_pointers: Annotated[list[Pointer], Field(min_length=1)]
    evidence_anchor_ids: Annotated[list[Id], Field(min_length=1)]
    description: Id


class SourceReviewResponse(Closed):
    request_id: Id
    proposal_kind: Literal["INVENTORY", "CARD_BATCH", "TABLE_LAYOUT"]
    proposal_hash: Sha256
    outcome: Literal["ACCEPT", "REVISE", "NEED_EVIDENCE", "SOURCE_UNRESOLVED"]
    checked_anchor_ids: list[Id]
    checked_item_ids: list[Id]
    issues: list[SourceReviewIssue]
    corrected_proposal: SourceProposal | None
    evidence_requests: list[EvidenceRequest]
    unresolved_reason: Id | None

    @model_validator(mode="after")
    def legal_route(self):
        if self.outcome == "REVISE":
            if not self.issues or self.corrected_proposal is None:
                raise ValueError(
                    "REVISE needs concrete source issues and complete correction"
                )
            outcome = self.corrected_proposal.payload.outcome
            valid_correction = outcome == "READY" or (
                self.proposal_kind == "CARD_BATCH" and outcome == "INVENTORY_DEFECT"
            )
            if (
                self.corrected_proposal.kind != self.proposal_kind
                or not valid_correction
            ):
                raise ValueError(
                    "correction must be a complete READY proposal of the same kind"
                )
        elif self.issues or self.corrected_proposal is not None:
            raise ValueError("only REVISE carries issues and a corrected proposal")
        if (self.outcome == "NEED_EVIDENCE") != bool(self.evidence_requests):
            raise ValueError("evidence request route mismatch")
        if (self.outcome == "SOURCE_UNRESOLVED") != (
            self.unresolved_reason is not None
        ):
            raise ValueError("source unresolved route mismatch")
        return self


class RequirementMatch(Closed):
    requirement_id: Id
    output_refs: list[OutputRef]


class SearchCoverage(Closed):
    index_sha256: Sha256
    available_component_ids: FileRef
    inspected_component_ids: FileRef
    available_components: Nonnegative
    inspected_components: Nonnegative
    exhaustive_fallback_completed: bool

    @model_validator(mode="after")
    def bounded(self):
        if self.inspected_components > self.available_components:
            raise ValueError("search count exceeds index size")
        if (
            self.exhaustive_fallback_completed
            and self.inspected_components != self.available_components
        ):
            raise ValueError("exhaustive search did not inspect complete index")
        return self


class IdentityDecision(Closed):
    disposition: Literal["KEEP", "DISTINCT_SOURCE_RESULT", "CONTEXT_ONLY"]
    distinction_dimension: Literal["OUTCOME", "ANALYSIS", "POPULATION", "TIME", "METHOD", "QUANTITY_ROLE"] | None
    separate_source_identity: str | None
    evidence_anchor_ids: list[Id]
    reason: Id

    @model_validator(mode="after")
    def source_supported_exclusion(self):
        if self.disposition=="KEEP":
            if self.distinction_dimension is not None or self.separate_source_identity is not None:
                raise ValueError("KEEP cannot assert an exclusion")
        else:
            if not self.evidence_anchor_ids or not self.separate_source_identity:
                raise ValueError("An exclusion needs a positive source identity and witnesses")
            if (self.disposition=="DISTINCT_SOURCE_RESULT")!=(self.distinction_dimension is not None):
                raise ValueError("Only a distinct source result has a distinction dimension")
        return self


class IdentityReviewResponse(Closed):
    task_id: Id
    output_alias: Id
    inspected_owner_ids: list[Id]
    decisions: dict[Id, dict[Id, IdentityDecision]]


class MatchIdentityReceipt(Closed):
    task_id: Id
    output_alias: Id
    original_match: FileRef
    refined_match: FileRef
    owner_groups: dict[Id, list[OutputRef]]
    authoritative_evidence: FileRef
    judge_run_ids: dict[Literal["SECONDARY", "ARBITER"], list[Id]]


class MatchResult(Closed):
    task_id: Id
    output_alias: Id
    state: Literal["MATCHED", "ABSENT", "AMBIGUOUS", "LOOKUP_UNRESOLVED"]
    matches: list[RequirementMatch]
    candidate_refs: list[OutputRef]
    coverage: SearchCoverage
    explanation: Id
    identity_receipt: FileRef | None = None

    @model_validator(mode="after")
    def absence_requires_search(self):
        if self.state == "ABSENT" and (
            self.candidate_refs or not self.coverage.exhaustive_fallback_completed
        ):
            raise ValueError("ABSENT needs empty candidates and exhaustive fallback")
        if self.state in ("MATCHED", "AMBIGUOUS") and not self.candidate_refs:
            raise ValueError("matched/ambiguous result needs original candidates")
        return self


class MatchResponse(Closed):
    task_id: Id
    output_alias: Id
    state: Literal["MATCHED", "ABSENT", "AMBIGUOUS", "LOOKUP_UNRESOLVED"]
    inspected_component_ids: list[Id]
    matches: list[RequirementMatch]
    explanation: Id


class PropertyAssertion(Closed):
    name: Id
    present: bool
    value_json: str | None
    original_ref: OutputRef | None

    @model_validator(mode="after")
    def missing_is_explicit(self):
        if self.present != (self.original_ref is not None):
            raise ValueError("presence must agree with original_ref")
        if self.present != (self.value_json is not None):
            raise ValueError("present property needs its exact raw JSON value")
        return self


class ObjectAssertion(Closed):
    object_id: Id
    kind: Literal[
        "FIELD",
        "RESULT",
        "SHARED_CONTEXT",
        "FAMILY",
        "CROSS_GROUP_SUMMARY",
        "TABLE",
        "SOURCE",
    ]
    original_ref: JsonRef
    properties: list[PropertyAssertion]


class RelationAssertion(Closed):
    original_ref: JsonRef
    source_id: Id
    relation: Literal[
        "uncertainty_of",
        "spread_of",
        "p_value_of",
        "describes",
        "compares",
        "derived_from",
        "contributes_to",
        "applies_under",
        "table_for",
        "other",
    ]
    target_id: Id
    closure_direction: Literal[
        "source_to_target", "target_to_source", "none", "UNREVIEWED"
    ]
    properties: list[PropertyAssertion]


class ClosureView(Closed):
    result_id: Id
    original_ref: JsonRef
    submitted_core_ids: list[Id]
    submitted_member_ids: list[Id]
    submitted_reference_ids: list[Id]
    recomputed_member_ids: list[Id]
    recomputed_reference_ids: list[Id]
    unresolved_edge_refs: list[JsonRef]


class CsvBlock(Closed):
    table_id: Id
    filename: Id
    file_sha256: Sha256
    first_row: Positive
    rows: list[list[str]]
    header: list[str]


class AssertionPacket(Closed):
    packet_id: Id
    task_id: Id
    binding: EvaluationBinding
    output_alias: Id
    submitted_package_sha256: Sha256
    task_card_sha256: Sha256
    match: MatchResult
    objects: list[ObjectAssertion]
    context_headers: list[ObjectAssertion] = Field(default_factory=list)
    relations: list[RelationAssertion]
    closures: list[ClosureView]
    csv_blocks: list[CsvBlock]
    document_bindings: list[DocumentBinding]
    evidence_anchor_ids: list[Id]
    source_bucket_ids: list[Id]
    packet_complete: bool
    omitted_due_to_limit_refs: list[OutputRef]


Dimension = Literal[
    "NUMERIC_FIDELITY",
    "CLASSIFICATION",
    "SCIENTIFIC_ATTACHMENT",
    "SCOPE_AND_ORIGIN",
    "REQUIRED_INFORMATION",
    "REQUIRED_ENCODING",
    "GROUPING_CONFORMANCE",
    "PROVENANCE",
    "TABLE_FIDELITY",
    "TABLE_INTERPRETATION",
    "TABLE_LINK_CONFORMANCE",
]
CounterexampleCode = Literal[
    "VALUE",
    "OPERATOR",
    "UNITS",
    "CLASSIFICATION",
    "MEASURE",
    "UNCERTAINTY_TARGET",
    "TEST_TARGET",
    "CONTRAST_ROLE",
    "COUNT_SCOPE",
    "SYMBOL_SCOPE",
    "ORIGIN",
    "CALCULATION",
    "SCIENTIFIC_LINK",
    "CORE",
    "LOCAL_BOUNDARY",
    "SOURCE_SEPARATE",
    "MUST_TOGETHER",
    "PROVENANCE",
    "MISSING_REQUIRED_INFORMATION",
    "REQUIRED_ENCODING",
    "CONTRADICTORY_ASSERTIONS",
    "UNSUPPORTED_EXTRA",
    "TABLE_VALUE",
    "TABLE_HEADER",
    "TABLE_ANNOTATION",
    "TABLE_LINK",
    "VALUE_STATUS",
]


class Counterexample(Closed):
    code: CounterexampleCode
    rule_ids: Annotated[list[RuleId], Field(min_length=1)]
    description: Id
    evidence_anchor_ids: Annotated[list[Id], Field(min_length=1)]
    output_refs: list[OutputRef]
    missing_requirement_id: Id | None

    @model_validator(mode="after")
    def witness_location(self):
        if not self.output_refs and self.missing_requirement_id is None:
            raise ValueError(
                "counterexample needs original output or missing requirement"
            )
        return self


def dimension_check_schema(schema):
    """Expose existing status/witness constraints to strict provider generation."""
    import copy
    branches=[]
    for status in ('PASS','FAIL','NOT_APPLICABLE'):
        branch=copy.deepcopy(schema)
        branch['properties']['status']={'type':'string','const':status}
        counter=branch['properties']['counterexamples']
        counter['minItems' if status=='FAIL' else 'maxItems']=1 if status=='FAIL' else 0
        if status=='PASS':branch['properties']['evidence_anchor_ids']['minItems']=1
        branches.append(branch)
    schema.clear()
    schema.update(anyOf=branches)


class DimensionCheck(Closed):
    model_config = ConfigDict(json_schema_extra=dimension_check_schema)
    dimension: Dimension
    status: Literal["PASS", "FAIL", "NOT_APPLICABLE"]
    evidence_anchor_ids: list[Id]
    counterexamples: list[Counterexample]

    @model_validator(mode="after")
    def failure_needs_witness(self):
        if (self.status == "FAIL") != bool(self.counterexamples):
            raise ValueError("counterexamples required exactly for FAIL")
        if self.status == "PASS" and not self.evidence_anchor_ids:
            raise ValueError("PASS needs inspected source evidence")
        return self


class TargetIssue(Closed):
    description: Id
    evidence_anchor_ids: Annotated[list[Id], Field(min_length=1)]


class TechnicalError(Closed):
    code: Id
    message: Id
    retryable: bool


class JudgeResponse(Closed):
    task_id: Id
    packet_id: Id
    outcome: Literal[
        "ASSESSED",
        "NEED_EVIDENCE",
        "TARGET_DEFECT",
        "SOURCE_UNRESOLVED",
        "TECHNICAL_FAILURE",
    ]
    checks: list[DimensionCheck]
    evidence_requests: list[EvidenceRequest]
    target_issue: TargetIssue | None
    unresolved_reason: Id | None
    technical_error: TechnicalError | None

    @model_validator(mode="after")
    def legal_route(self):
        if (self.outcome == "ASSESSED") != bool(self.checks):
            raise ValueError("only ASSESSED has nonempty checks")
        if (self.outcome == "NEED_EVIDENCE") != bool(self.evidence_requests):
            raise ValueError("only NEED_EVIDENCE has requests")
        if (self.outcome == "TARGET_DEFECT") != (self.target_issue is not None):
            raise ValueError("target_issue route mismatch")
        if (self.outcome == "SOURCE_UNRESOLVED") != (
            self.unresolved_reason is not None
        ):
            raise ValueError("unresolved_reason route mismatch")
        if (self.outcome == "TECHNICAL_FAILURE") != (self.technical_error is not None):
            raise ValueError("technical_error route mismatch")
        if len({x.dimension for x in self.checks}) != len(self.checks):
            raise ValueError("duplicate dimension")
        return self


class JudgeRunReceipt(Closed):
    run_id: Id
    role: Literal["SOURCE_READER", "PRIMARY", "SECONDARY", "ARBITER"]
    model_route_sha256: Sha256
    request: FileRef
    raw_response: FileRef | None
    parsed_response: FileRef | None
    attempt: Annotated[int, Field(ge=1, le=2)]
    input_tokens: Nonnegative
    output_tokens: Nonnegative
    elapsed_ms: Nonnegative
    error: ErrorRecord | None


class ScientificVerdict(Closed):
    task_id: Id
    output_alias: Id
    packet_sha256: Sha256
    state: Literal["PASS", "FAIL", "HOLD"]
    decisive_response_sha256: Sha256 | None
    dimension_checks: list[DimensionCheck]
    judge_run_ids: list[Id]
    hold_reasons: list[ErrorRecord]

    @model_validator(mode="after")
    def state_consistency(self):
        if self.state == "HOLD":
            if not self.hold_reasons:
                raise ValueError("HOLD requires explicit reasons")
        else:
            if (
                self.hold_reasons
                or self.decisive_response_sha256 is None
                or not self.dimension_checks
            ):
                raise ValueError(
                    "decided verdict requires decisive response and checks"
                )
            expected = (
                "FAIL"
                if any(x.status == "FAIL" for x in self.dimension_checks)
                else "PASS"
            )
            if self.state != expected:
                raise ValueError("verdict differs from dimension checks")
        return self


class FractionValue(Closed):
    numerator: Nonnegative
    denominator: Positive

    @model_validator(mode="after")
    def reduced_unit_fraction(self):
        if (
            self.numerator > self.denominator
            or gcd(self.numerator, self.denominator) != 1
        ):
            raise ValueError("score must be reduced and within [0, 1]")
        return self

    def fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)


class SourceBucket(Closed):
    bucket_id: Id
    output_alias: Id
    kind: Literal["SOURCE_TILE", "UNANCHORED"]
    tile_id: Id | None
    assertion_refs: Annotated[list[OutputRef], Field(min_length=1)]

    @model_validator(mode="after")
    def bucket_location(self):
        if (self.kind == "SOURCE_TILE") != (self.tile_id is not None):
            raise ValueError("source tile required exactly for SOURCE_TILE bucket")
        return self


class SampleSelection(Closed):
    stream: Literal[
        "CONTENT_PROSE", "CONTENT_TABLE", "ARCHIVE", "OUTPUT_AUDIT", "PASS_AUDIT"
    ]
    population_ids: list[Id]
    selected_ids: list[Id]
    output_alias: Id | None
    seed_domain: Id
    selection_trace: FileRef


class RankedItem(Closed):
    item_id: Id
    rank_sha256: Sha256


class SelectionTraceStep(Closed):
    level: Literal[
        "SOURCE_TABLE", "ROW_BLOCK", "PROSE_TILE", "OUTPUT_BUCKET", "PASS_AUDIT"
    ]
    draw_index: Nonnegative
    parent_id: Id | None
    conditional_population_ids: list[Id]
    ranked_items: list[RankedItem]
    requested_count: Nonnegative
    selected_ids: list[Id]


class SelectionTrace(Closed):
    stream: Literal[
        "CONTENT_PROSE", "CONTENT_TABLE", "ARCHIVE", "OUTPUT_AUDIT", "PASS_AUDIT"
    ]
    seed_domain: Id
    steps: list[SelectionTraceStep]


class SampleManifest(Closed):
    binding: EvaluationBinding
    seed_commitment: Sha256
    revealed_seed_hex: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    selections: list[SampleSelection]
    output_buckets: list[SourceBucket]
    content_available: bool
    archive_available: bool


class GateReceipt(Closed):
    output_alias: Id
    state: Literal["PASS", "PIPELINE_FAILURE", "EVALUATOR_HOLD"]
    gate_bit: Bit | None
    errors: list[ErrorRecord]

    @model_validator(mode="after")
    def gate_state(self):
        if (
            self.gate_bit
            != {"PASS": 1, "PIPELINE_FAILURE": 0, "EVALUATOR_HOLD": None}[self.state]
        ):
            raise ValueError("gate bit differs from disposition")
        if self.state != "PASS" and not self.errors:
            raise ValueError("nonpassing gate requires diagnostic errors")
        return self


class TaskBit(Closed):
    task_id: Id
    tile_or_bucket_id: Id
    task_kind: TaskKind
    bit: Bit
    verdict_sha256: Sha256


class PaperScoreVector(Closed):
    binding: EvaluationBinding
    output_alias: Id
    state: Literal["SCORED", "PIPELINE_FAILURE", "HOLD", "INELIGIBLE"]
    gate: Bit | None
    content_prose: FractionValue | None
    content_table: FractionValue | None
    content: FractionValue | None
    archive: FractionValue | None
    output_audit: FractionValue | None
    content_available: bool
    archive_available: bool
    score: FractionValue | None
    skipped_reason: Literal["GATE_FAILED"] | None
    task_bits: list[TaskBit]


class CorrectionRecord(Closed):
    correction_id: Id
    kind: Literal["SOURCE_FRAME", "SOURCE_TASK", "SCIENTIFIC_VERDICT"]
    superseded_receipt_sha256: Sha256
    affected_task_ids: list[Id]
    evidence_anchor_ids: list[Id]
    explanation: Id


class SampledSilverReceipt(Closed):
    binding: EvaluationBinding
    state: Literal["DRAFT", "FROZEN", "HOLD", "INELIGIBLE"]
    source_packet: FileRef
    source_frame: FileRef
    comparison_plan: FileRef
    input_packages: Annotated[list[FileRef], Field(min_length=1)]
    sampling_manifest: FileRef
    task_cards: list[FileRef]
    assertion_packets: list[FileRef]
    judge_runs: list[FileRef]
    deterministic_gates: list[GateReceipt]
    scientific_verdicts: list[FileRef]
    paper_scores: list[PaperScoreVector]
    corrections: list[CorrectionRecord]
    hold_reasons: list[ErrorRecord]


class ProviderParameters(Closed):
    api_base: Id
    tokenizer_id: Id
    tokenizer_sha256: Sha256
    declared_context_window_tokens: Positive
    tools: tuple[()]
    tool_choice: Literal["none"]
    reasoning_effort: Id | None


class ModelRoute(Closed):
    role: Literal["SOURCE_READER", "PRIMARY", "SECONDARY", "ARBITER"]
    provider: Id
    model: Id
    runtime_revision: Id
    route_sha256: Sha256
    supports_strict_json_schema: bool
    supports_images: bool
    context_window_tokens: Positive
    temperature: Annotated[Literal[0], BeforeValidator(exact_int)]
    top_p: Annotated[Literal[1], BeforeValidator(exact_int)]
    n: Annotated[Literal[1], BeforeValidator(exact_int)]
    max_output_tokens: Literal[16384]
    request_timeout_seconds: Literal[120]
    provider_parameters: FileRef


class FrozenSettings(Closed):
    protocol_id: Protocol
    extraction_schema_sha256: Literal[
        "78f5b279dfbf4f2fec5fc83b2219350e43e3fece5c58e94f81fb5d146d10a5e2"
    ]
    integration_base_commit: Literal["0ba3f4bada37fdef54a8845ca6ef1ee2596c023f"]
    implementation_digest: Sha256
    source_parser_digest: Sha256
    normalization_digest: Sha256
    prompt_bundle_digest: Sha256
    renderer_digest: Sha256
    execution_policy: FileRef
    model_routes: Annotated[list[ModelRoute], Field(min_length=4, max_length=4)]
    content_prose_tiles: Literal[3]
    content_table_tiles: Literal[3]
    archive_blocks: Literal[4]
    archive_block_data_rows: Literal[5]
    output_audit_buckets: Literal[4]
    content_weight: Literal[7]
    archive_weight: Literal[2]
    maximum_common_corrections: Literal[2]
    maximum_evidence_expansions: Literal[2]
    technical_retries: Annotated[Literal[1], BeforeValidator(exact_int)]
    source_token_budgets: tuple[Literal[32000], Literal[32000], Literal[32000]]
    structured_packet_tokens_per_shard: Literal[32000]
    max_global_inflight_requests: Literal[16]
    max_case_workers: Literal[4]
    primary_independent_requests_per_batch: Literal[4]
    pass_audit_numerator: Annotated[Literal[1], BeforeValidator(exact_int)]
    pass_audit_denominator: Literal[10]
    initial_lexical_matches: Literal[12]
    exhaustive_scan_batch_size: Literal[32]
    embeddings_enabled: Annotated[Literal[False], BeforeValidator(exact_bool)]
    max_extraction_json_bytes: Literal[10000000]
    max_csv_bytes_each: Literal[50000000]
    max_output_bytes_total: Literal[210000000]
    max_output_files: Literal[2048]
    max_objects: Literal[200000]
    max_relationships: Literal[400000]
    installation_timeout_seconds: Literal[600]
    extraction_timeout_seconds: Literal[1800]
    memory_limit_bytes: Literal[17179869184]
    sealed_evaluation_timeout_seconds: Literal[1800]
    max_table_layout_split_depth: Literal[8]
    replacement_limit_divisor: Literal[20]
    minimum_replacement_limit: Annotated[Literal[1], BeforeValidator(exact_int)]
    winner_selection: Literal["silver_tiebreak"]
    gold_correction_scope: Literal["COMPLETE_INPUT_POOL"]
    output_copy_policy: Literal["DIAGNOSTIC_ONLY"]
    is_copy: Annotated[Literal[False], BeforeValidator(exact_bool)]
    code_audit_checks: tuple[
        Literal["build"], Literal["evaluation_context"], Literal["cap_evasion"]
    ]
    organization_score: Annotated[Literal[False], BeforeValidator(exact_bool)]
    canonical_organization: Annotated[Literal[False], BeforeValidator(exact_bool)]

    @model_validator(mode="after")
    def judge_routes_distinct(self):
        if {x.role for x in self.model_routes} != {
            "SOURCE_READER",
            "PRIMARY",
            "SECONDARY",
            "ARBITER",
        }:
            raise ValueError("one route per required role")
        judges = [x for x in self.model_routes if x.role != "SOURCE_READER"]
        if len({(x.provider, x.model, x.runtime_revision) for x in judges}) != 3:
            raise ValueError(
                "PRIMARY, SECONDARY, ARBITER must be distinct pinned runtimes"
            )
        source_reader = next(x for x in self.model_routes if x.role == "SOURCE_READER")
        source_identity = (
            source_reader.provider,
            source_reader.model,
            source_reader.runtime_revision,
        )
        if any(
            source_identity == (x.provider, x.model, x.runtime_revision)
            for x in judges
            if x.role in ("SECONDARY", "ARBITER")
        ):
            raise ValueError("SOURCE_READER must differ from SECONDARY and ARBITER")
        if not all(x.supports_strict_json_schema for x in self.model_routes):
            raise ValueError("strict JSON Schema capability is mandatory")
        return self


class EvaluateRunManifest(Closed):
    binding: EvaluationBinding
    comparison_plan: FileRef
    source_packet: FileRef
    source_frame: FileRef
    submitted_packages: Annotated[list[FileRef], Field(min_length=1)]
    settings: FileRef
    seed_commitment: Sha256
    paper_seed_file: FileRef


class CommandResult(Closed):
    command: Literal["prepare-source", "evaluate", "replay", "correct", "fixtures"]
    status: Literal["COMPLETED", "CONFIG_INVALID", "HOLD", "INELIGIBLE"]
    binding: EvaluationBinding | None
    artifacts: list[FileRef]
    errors: list[ErrorRecord]


class CorrectionRequest(Closed):
    predecessor: FileRef
    correction: CorrectionRecord
    evidence_files: list[FileRef]
    proposed_artifact: FileRef | None


class PublicScoreComponents(Closed):
    paper_id: Id
    output_alias: Id
    state: Literal["SCORED", "PIPELINE_FAILURE"]
    gate: Bit
    content: FractionValue | None
    archive: FractionValue | None
    output_audit: FractionValue | None
    score: FractionValue
    content_available: bool
    archive_available: bool


class PairPublicDetails(Closed):
    protocol_id: Protocol
    silver_receipt_sha256: Sha256
    revision: Nonnegative
    evaluation_status: Literal["SCORED", "COMMON_EXCLUDED"]
    challenger_components: FileRef | None
    baseline_components: FileRef | None
    exclusion_receipt: FileRef | None

    @model_validator(mode="after")
    def publication_payload(self):
        if self.evaluation_status == "SCORED":
            if (
                self.challenger_components is None
                or self.baseline_components is None
                or self.exclusion_receipt is not None
            ):
                raise ValueError(
                    "SCORED details require the two public component receipts"
                )
        elif (
            self.challenger_components is not None
            or self.baseline_components is not None
            or self.exclusion_receipt is None
        ):
            raise ValueError(
                "COMMON_EXCLUDED details require only the common exclusion receipt"
            )
        return self


class CompetitionPairRow(Closed):
    paper_id: Id
    challenger_hotkey: Id
    baseline_hotkey: Id
    challenger_score: UnitFloat
    baseline_score: UnitFloat
    infrastructure_fault: bool
    details_public: PairPublicDetails

    @model_validator(mode="after")
    def exclusion_is_not_measured_zero(self):
        excluded = self.details_public.evaluation_status == "COMMON_EXCLUDED"
        if self.infrastructure_fault != excluded:
            raise ValueError("infrastructure_fault must match common exclusion status")
        if excluded and (self.challenger_score != 0.0 or self.baseline_score != 0.0):
            raise ValueError(
                "excluded adapter rows use only 0.0 compatibility placeholders"
            )
        return self


class CommonExclusionReceipt(Closed):
    binding: EvaluationBinding
    silver_receipt: FileRef
    reason: ErrorRecord
    terminal_bounds_exhausted: bool
    replacement_ordinal: Positive
    applies_to_all_aliases: Annotated[list[Id], Field(min_length=1)]

    @model_validator(mode="after")
    def common_exclusion_is_not_pipeline_failure(self):
        if self.reason.category == "PIPELINE":
            raise ValueError("a pipeline defect cannot exclude the common paper")
        if len(set(self.applies_to_all_aliases)) != len(self.applies_to_all_aliases):
            raise ValueError("duplicate alias in common exclusion")
        return self


class CommonReplacement(Closed):
    excluded_paper_id: Id
    replacement_paper_id: Id
    exclusion_receipt: FileRef
    precommitted_ordinal: Positive


class SourceGridManifest(Closed):
    protocol_id: Protocol
    round_id: Id
    stage: Literal["prescreen", "screen", "final", "week_one"]
    revision: Nonnegative
    precommitted_order_sha256: Sha256
    initial_paper_ids: Annotated[list[Id], Field(min_length=1)]
    replacements: list[CommonReplacement]

    @model_validator(mode="after")
    def grid_shape(self):
        if len(set(self.initial_paper_ids)) != len(self.initial_paper_ids):
            raise ValueError("initial paper IDs must be unique")
        if self.revision != len(self.replacements):
            raise ValueError(
                "each grid revision records exactly one common replacement"
            )
        all_papers = set(self.initial_paper_ids)
        excluded = set()
        for item in self.replacements:
            if (
                item.excluded_paper_id not in all_papers
                or item.excluded_paper_id in excluded
            ):
                raise ValueError("replacement must exclude an existing active paper")
            if item.replacement_paper_id in all_papers:
                raise ValueError("replacement paper must be new to the grid")
            excluded.add(item.excluded_paper_id)
            all_papers.add(item.replacement_paper_id)
        return self

    def validate_transition_from(
        self,
        previous: SourceGridManifest,
        precommitted_order: list[str],
        verified_order_sha256: str,
    ) -> None:
        if (self.protocol_id, self.round_id, self.stage) != (
            previous.protocol_id,
            previous.round_id,
            previous.stage,
        ):
            raise ValueError("source grid scope is immutable")
        if (
            self.precommitted_order_sha256 != previous.precommitted_order_sha256
            or self.precommitted_order_sha256 != verified_order_sha256
        ):
            raise ValueError("precommitted reserve order is immutable")
        if len(set(precommitted_order)) != len(precommitted_order) or not set(
            self.initial_paper_ids
        ).issubset(precommitted_order):
            raise ValueError("invalid precommitted order or initial paper membership")
        if (
            self.initial_paper_ids != previous.initial_paper_ids
            or self.revision != previous.revision + 1
        ):
            raise ValueError(
                "replacement preserves initial IDs and increments the grid once"
            )
        if (
            len(self.replacements) != len(previous.replacements) + 1
            or self.replacements[:-1] != previous.replacements
        ):
            raise ValueError("replacement history is append-only")
        added = self.replacements[-1]
        used = set(previous.initial_paper_ids) | {
            x.replacement_paper_id for x in previous.replacements
        }
        next_unused = next(
            (
                (i, pid)
                for i, pid in enumerate(precommitted_order, 1)
                if pid not in used
            ),
            None,
        )
        if next_unused != (added.precommitted_ordinal, added.replacement_paper_id):
            raise ValueError("replacement must be the next unused precommitted paper")


class ComparisonPlan(Closed):
    protocol_id: Protocol
    round_id: Id
    stage: Literal["prescreen", "screen", "final", "week_one"]
    plan_revision: Nonnegative
    source_grid_revision: Nonnegative
    reference_fallback_used: bool
    baseline_alias: Id
    challenger_aliases: Annotated[list[Id], Field(min_length=1)]
    reference_alias: Id
    source_grid: FileRef
    parent_plan_sha256: Sha256 | None
    reason: Literal["INITIAL", "REFERENCE_FALLBACK", "COMMON_REPLACEMENT"]

    @model_validator(mode="after")
    def plan_transition(self):
        if (
            len(set(self.challenger_aliases)) != len(self.challenger_aliases)
            or self.baseline_alias in self.challenger_aliases
        ):
            raise ValueError("comparison roles must be unique")
        if self.reason == "INITIAL":
            if (
                self.plan_revision != 0
                or self.source_grid_revision != 0
                or self.parent_plan_sha256 is not None
                or self.reference_fallback_used
            ):
                raise ValueError(
                    "INITIAL plan has zero revisions, no parent, and no used fallback"
                )
        elif self.plan_revision == 0 or self.parent_plan_sha256 is None:
            raise ValueError(
                "a subsequent plan requires a positive revision and parent"
            )
        if self.reason == "REFERENCE_FALLBACK" and not self.reference_fallback_used:
            raise ValueError("REFERENCE_FALLBACK records the used fallback")
        if self.reference_fallback_used and self.baseline_alias != self.reference_alias:
            raise ValueError("a used reference fallback retains the reference baseline")
        if self.reason == "COMMON_REPLACEMENT" and self.source_grid_revision == 0:
            raise ValueError(
                "COMMON_REPLACEMENT requires a positive source grid revision"
            )
        return self

    def validate_transition_from(
        self, previous: ComparisonPlan, previous_sha256: str
    ) -> None:
        if self.reason == "INITIAL" or self.parent_plan_sha256 != previous_sha256:
            raise ValueError("successor must name its exact preceding plan hash")
        if self.plan_revision != previous.plan_revision + 1:
            raise ValueError("plan revisions must increase by exactly one")
        if (self.protocol_id, self.round_id, self.stage, self.reference_alias) != (
            previous.protocol_id,
            previous.round_id,
            previous.stage,
            previous.reference_alias,
        ):
            raise ValueError(
                "protocol, round, stage, and reference identity are immutable"
            )
        if self.challenger_aliases != previous.challenger_aliases:
            raise ValueError("a plan transition cannot change the challenger set")
        if self.reason == "REFERENCE_FALLBACK":
            if previous.reference_fallback_used or not self.reference_fallback_used:
                raise ValueError("reference fallback may be used only once")
            if (
                self.source_grid_revision != previous.source_grid_revision
                or self.source_grid != previous.source_grid
            ):
                raise ValueError("fallback preserves the complete source grid")
        else:
            if self.source_grid_revision != previous.source_grid_revision + 1:
                raise ValueError("common replacement increments the source grid once")
            if (
                self.reference_fallback_used != previous.reference_fallback_used
                or self.baseline_alias != previous.baseline_alias
            ):
                raise ValueError(
                    "common replacement preserves roles and fallback state"
                )
            if self.source_grid.sha256 == previous.source_grid.sha256:
                raise ValueError(
                    "common replacement must bind a new source grid manifest"
                )
