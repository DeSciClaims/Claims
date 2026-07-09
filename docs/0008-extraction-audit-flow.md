# Extraction and Audit Flow

This shows how `section_context_v1` extraction fields relate to `judge_v2` audit fields.

Source docs:

- `miner/section_context_v1/CLAIM_EXTRACTION_FIELDS.md`
- `validator/judge_v2/AUDIT_RECORD_FIELDS.md`

## High-Level Flow

```mermaid
flowchart LR
  Paper[Paper PDF / artifact.json]
  Extractor[section_context_v1 miner]
  Output[section_context_v1_output.json]
  Claims[extracted_claims.csv]
  Judge[judge_v2 validator]
  RunAudit[run_audit_record.csv]
  ClaimAudit[claim_audit_records.csv]
  Diagnostics[optional diagnostic CSVs]

  Paper --> Extractor
  Extractor --> Output
  Extractor --> Claims
  Output --> Judge
  Claims -. import/review .-> Judge
  Judge --> RunAudit
  Judge --> ClaimAudit
  Judge --> Diagnostics
```

## Extraction Objects

```mermaid
flowchart TD
  Output[section_context_v1_output.json]
  Paper[paper metadata]
  Sections[sections]
  Spans[source spans]
  Claims[claims]
  Evidence[evidence_items]
  Links[claim_evidence_links]

  Output --> Paper
  Output --> Sections
  Output --> Spans
  Output --> Claims
  Output --> Evidence
  Output --> Links
  Claims -->|source_span_ids| Spans
  Evidence -->|source_span_ids| Spans
  Links --> Claims
  Links --> Evidence
```

## Field Lineage

```mermaid
flowchart LR
  subgraph Miner["Miner Extraction"]
    C1[claim_id]
    C2[claim_profile]
    C3[claim_text]
    C4[subject / predicate / object]
    C5[context_json]
    C6[details_json]
    C7[linked_evidence_ids]
    C8[evidence_items_json]
    C9[source_span_ids]
  end

  subgraph Validator["Judge V2 Claim Diagnostics"]
    A1[claim_id]
    A2[claim_profile]
    A3[claim_text]
    A4[subject / predicate / object]
    A5[accurate_extraction_score]
    A6[evidence_evaluation_score]
    A7[issue_tags / missing_elements]
    A8[suggested_corrections_json]
  end

  C1 --> A1
  C2 --> A2
  C3 --> A3
  C4 --> A4
  C2 --> A5
  C3 --> A5
  C4 --> A5
  C5 --> A5
  C6 --> A5
  C9 --> A5
  C7 --> A6
  C8 --> A6
  C9 --> A6
  A5 --> A7
  A6 --> A7
  A7 --> A8
```

## Run-Level Audit

The validator keeps claim-level rows for debugging, but the main validator output is the run-level audit.

```mermaid
flowchart TD
  ClaimAudits[claim_audit_records.csv]
  PaperCoverage[gold/reference or full-paper coverage pass]
  Coverage[complete_coverage_score]
  Accuracy[accurate_extraction_score]
  Evidence[evidence_evaluation_score]
  Issues[issue_tags / missing_elements]
  RunAudit[run_audit_record.csv]

  PaperCoverage --> Coverage
  ClaimAudits --> Accuracy
  ClaimAudits --> Evidence
  ClaimAudits --> Issues
  Coverage --> RunAudit
  Accuracy --> RunAudit
  Evidence --> RunAudit
  Issues --> RunAudit
```

## Two Audit Modes

```mermaid
flowchart LR
  Output[section_context_v1_output.json]
  Gold[gold / reviewed extraction]
  Paper[paper sections and spans]

  Output --> GoldMode[gold_comparison]
  Gold --> GoldMode
  Output --> IntrinsicMode[intrinsic_audit]
  Paper --> IntrinsicMode

  GoldMode --> GoldRun[run_audit_record.csv]
  GoldMode --> MissingGold[missing_gold_claims.csv]
  GoldMode --> ExtraClaims[extra_extracted_claims.csv]

  IntrinsicMode --> IntrinsicRun[run_audit_record.csv]
  IntrinsicMode --> CandidateMissing[candidate_missing_claims.csv]
  IntrinsicMode --> WeakClaims[weak_or_unsupported_claims.csv]
```

## Score Meaning

| Score | Gold mode | Intrinsic mode |
| --- | --- | --- |
| Run `complete_coverage_score` | Did extracted claims cover the gold/reference claims exhaustively? | In LLM mode, did extracted claims cover important claims found by a full-paper missing-claim discovery pass? |
| Run `accurate_extraction_score` | Did matched claims preserve the gold claim meaning and fields? | Are extracted claims faithful to their cited spans and profile/schema? |
| Run `evidence_evaluation_score` | Did extracted evidence match or sufficiently support the gold evidence? | Is evidence present, relevant, sufficient, and well-linked? |
| Claim `accurate_extraction_score` | Local diagnostic: does this claim match its gold/reference target? | Local diagnostic: is this claim faithful to its cited source span and schema? |
| Claim `evidence_evaluation_score` | Local diagnostic: does this claim's evidence align with gold/reference evidence? | Local diagnostic: does this claim's evidence support this claim? |

## Practical Rule

Use miner fields to answer:

> What did the miner extract, from where, and with what evidence?

Use validator fields to answer:

> How complete, accurate, and well-supported was that extraction?

The run-level audit is the validator's primary score. Claim-level rows are diagnostic: they explain local extraction and evidence problems, but they do not carry the holistic complete-coverage score.
