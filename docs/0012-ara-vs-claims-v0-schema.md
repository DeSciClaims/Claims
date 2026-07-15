# Claims Agent Schema vs Claims v0 Schema

This document compares two Claims-owned schemas:

- **Claims v0 schema**: the older flat claim/evidence schema used by `miner.v0`,
  `section_context_v1`, early validators, and review imports.
- **Claims Agent schema**: the newer `agent_v1` schema written to
  `agent_output.json`.

The newer schema is called the **Agent schema** because
agent loops are better suited to filling its richer fields: reasoning layers,
source refs, experiments, trace, and runtime metadata. It also builds on the
artifact layout ideas from
[ARA](https://github.com/ARA-Labs/Agent-Native-Research-Artifact), but it is not
the upstream ARA schema. It is our JSON contract.

## Summary

Claims v0 is a compact extraction schema:

```text
paper
spans
claims[]
evidence_items[]
claim_evidence_links[]
metadata
```

The Claims Agent schema is a richer artifact schema:

```text
ara_version
paper
logic
  problem_observations[]
  gaps[]
  key_insight
  assumptions[]
  claims[]
  concepts[]
  experiments[]
  related_work[]
  constraints[]
evidence
  records[]
  ledger_notes[]
trace
src
metadata
```

The main change is not that v0 "cannot be used by agents." It can. The change is
that the Agent schema gives an agent loop more useful places to put what it
learns while reading a paper: claims, evidence, verification structure,
definitions, assumptions, trace, and source-grounded quotes.

## Why Adopt The Agent Schema

We are making the Agent schema canonical for new miner work because it better
matches the artifact we want miners to produce.

It lets a miner preserve:

- the paper's main claims
- the evidence behind those claims
- the conditions and falsification criteria for claims
- the experiments or verification records that support claims
- important concepts and definitions
- source-grounded quotes and span IDs
- reasoning or exploration trace
- environment, artifact, and runtime metadata

Claims v0 remains useful historical context and compatibility surface, but it is
not the shape we should keep extending.

## Top-Level Comparison

| Area | Claims v0 | Claims Agent schema |
| --- | --- | --- |
| Current role | Legacy/compatibility schema | Canonical miner schema |
| Main output | `section_context_v1_output.json`, `extracted_claims.csv` | `agent_output.json` plus generated markdown |
| Miner path | `miner.v0`, `section_context_v1` | `miner.agent_v1` |
| Validator path | older row/link validators | `validator.agent_v1` |
| Claim location | `claims[]` | `logic.claims[]` |
| Evidence location | `evidence_items[]` | `evidence.records[]` |
| Claim/evidence relation | separate `claim_evidence_links[]` | IDs embedded in claims, evidence, and experiments |
| Source grounding | span IDs | `SourceRef` objects with span IDs, quote, role, and optional path |
| Reasoning structure | limited | `logic`, `trace`, and `src` layers |
| Runtime metadata | generic `metadata` | `metadata.runtime_metrics` and backend details |

## Claims v0 Shape

Claims v0 centers on flat review objects:

```json
{
  "paper": {},
  "spans": [],
  "claims": [],
  "evidence_items": [],
  "claim_evidence_links": [],
  "metadata": {}
}
```

Its main objects are:

- `Claim`: a paper-owned claim row with `claim_text`, semantic
  subject/predicate/object fields, source span IDs, details, and confidence.
- `EvidenceItem`: a support record with summary, method, outcome type,
  presentation type, source span IDs, and details.
- `ClaimEvidenceLink`: a relationship row connecting one claim to one evidence
  item.

This worked well for review tables and link-level scoring. It is direct,
compact, and easy to export.

## Claims Agent Schema Shape

The Agent schema is defined in
[`miner/agent_v1/artifact_models.py`](../miner/agent_v1/artifact_models.py).
At a high level, `agent_output.json` looks like this:

```json
{
  "ara_version": "1.0",
  "paper": {
    "paper_id": "...",
    "title": "...",
    "authors": [],
    "year": 2026,
    "venue": null,
    "doi": null,
    "domain": null,
    "keywords": [],
    "abstract": "...",
    "claims_summary": []
  },
  "logic": {
    "problem_observations": [],
    "gaps": [],
    "key_insight": "...",
    "assumptions": [],
    "claims": [],
    "concepts": [],
    "experiments": [],
    "related_work": [],
    "constraints": []
  },
  "evidence": {
    "records": [],
    "ledger_notes": []
  },
  "trace": {
    "node_id": "Q0",
    "node_type": "question",
    "support_level": "inferred",
    "summary": "...",
    "source_refs": [],
    "evidence": [],
    "children": []
  },
  "src": {
    "environment": [],
    "artifacts": []
  },
  "metadata": {}
}
```

This is a Claims JSON schema inspired by ARA's artifact layout: paper, logic,
evidence, trace, source/artifact notes, and metadata.

A complete passing example from the Codex backend is checked in at
[`examples/agent_v1/rietveld_codex_agent_output.json`](../examples/agent_v1/rietveld_codex_agent_output.json).
It was generated from the Rietveld et al. 2013 Science paper and validates with
zero deterministic schema issues. The paired validation report is
[`examples/agent_v1/rietveld_codex_validation_report.json`](../examples/agent_v1/rietveld_codex_validation_report.json).

For example, its `paper` layer starts like this:

```json
{
  "paper_id": "Rietveld_et_al_2013_Science",
  "title": "GWAS of 126,559 Individuals Identifies Genetic Variants Associated with Educational Attainment",
  "authors": [
    "Cornelius A. Rietveld",
    "Sarah E. Medland",
    "Jaime Derringer",
    "Jian Yang",
    "Tonu Esko",
    "Nicolas W. Martin",
    "Harm-Jan Westra",
    "Peter M. Visscher",
    "Daniel J. Benjamin",
    "David Cesarini",
    "Philipp D. Koellinger",
    "Social Science Genetic Association Consortium"
  ],
  "year": 2013,
  "venue": "Science",
  "doi": "10.1126/science.1235488",
  "domain": "social-science genetics; genome-wide association study; educational attainment",
  "keywords": ["GWAS", "educational attainment", "EduYears", "College completion", "polygenic score"],
  "claims_summary": [
    "Three independent SNPs reached genome-wide significance and replicated for educational attainment phenotypes.",
    "Individual SNP effect sizes on educational attainment are very small."
  ]
}
```

## Field Comparison

| Concept | Claims v0 | Claims Agent schema |
| --- | --- | --- |
| Paper identity | `paper` | `paper` |
| Source spans | `spans[]` | run sidecar `source_payload.json`; output uses `sources[]` on claims and `source_refs[]` elsewhere |
| Claims | `claims[]` | `logic.claims[]` |
| Claim text | `claims[].claim_text` | `logic.claims[].statement` |
| Claim semantics | `subject`, `predicate`, `object`, `claim_kind` | free-form claim fields plus optional `metadata` |
| Claim conditions | usually in `details` or absent | `logic.claims[].conditions` |
| Falsifiability | usually absent | `logic.claims[].falsification_criteria` |
| Evidence | `evidence_items[]` | `evidence.records[]` |
| Evidence summary | `evidence_items[].summary_text` | `evidence.records[].summary` |
| Evidence method/outcome/presentation | `SemanticField` objects | `evidence_method` is a string; `outcome_type` and `presentation_type` are strings or null |
| Claim/evidence links | `claim_evidence_links[]` | `logic.claims[].evidence_ids[]`, `evidence.records[].linked_claim_ids[]`, and experiment links |
| Experiments/proofs | no first-class layer | `logic.experiments[]`, claim `proof[]` |
| Concepts | no first-class layer | `logic.concepts[]` |
| Trace | no first-class layer | `trace` |
| Runtime telemetry | generic `metadata` | `metadata.runtime_metrics` |

## Claim Example

Claims v0 claim:

```json
{
  "claim_id": "C01",
  "paper_id": "paper1",
  "claim_text": "Treatment A reduced median recovery time.",
  "subject": {"value": "Treatment A"},
  "predicate": {"value": "reduced"},
  "object": {"value": "median recovery time"},
  "claim_kind": "result",
  "epistemic_status": "supported",
  "support_origin": "paper",
  "source_span_ids": ["paper1-span-0001"],
  "details": {}
}
```

Claims Agent claim, from the Codex Rietveld run:

```json
{
  "claim_id": "C01",
  "statement": "Large-scale meta-analysis can identify replicable genetic loci for educational attainment where smaller social-science genetics studies had not produced consistent hits.",
  "conditions": "Supported for the harmonized EduYears and College phenotypes in the discovery and replication cohorts described in the provided source; not a claim that the same loci generalize across ancestries or all educational systems.",
  "status": "supported",
  "falsification_criteria": "The claim would be weakened if independent replication under the same phenotype definitions failed for the three genome-wide significant loci or if the reported replication were attributable to unmodeled cohort artifacts.",
  "proof": ["E01", "E02"],
  "evidence_ids": ["EV01", "EV02"],
  "dependencies": [],
  "sources": [
    {
      "source_id": "S01",
      "source_type": "span",
      "path": null,
      "span_ids": ["Rietveld_et_al_2013_Science-p001-001"],
      "quote": "Three independent single-nucleotide polymorphisms (SNPs) are genome-wide significant",
      "role": "result"
    },
    {
      "source_id": "S02",
      "source_type": "span",
      "path": null,
      "span_ids": ["Rietveld_et_al_2013_Science-p001-001"],
      "quote": "all three replicate",
      "role": "result"
    },
    {
      "source_id": "S03",
      "source_type": "span",
      "path": null,
      "span_ids": ["Rietveld_et_al_2013_Science-p001-001"],
      "quote": "discovery sample of 101,069 individuals and a replication sample of 25,490",
      "role": "method"
    }
  ],
  "source_claim_id": null,
  "metadata": {
    "phenotypes": ["EduYears", "College"],
    "replicated_snps": ["rs9320913", "rs11584700", "rs4851266"]
  }
}
```

The Agent claim carries the statement plus the context needed to judge it:
conditions, falsification criteria, proof experiment IDs, evidence IDs, and
source refs. On claims, the field is named `sources`; each item has the shared
`SourceRef` shape.

## Evidence Example

Claims v0 evidence:

```json
{
  "evidence_id": "EV01",
  "paper_id": "paper1",
  "role": "support",
  "summary_text": "Median recovery time was lower in the treatment group.",
  "evidence_method": {"value": "observation", "entity_type": "evidence_method"},
  "outcome_type": {"value": "recovery time", "entity_type": "outcome_type"},
  "presentation_type": {"value": "text", "entity_type": "presentation_type"},
  "source_span_ids": ["paper1-span-0001"],
  "details": {}
}
```

Claims Agent evidence, from the Codex Rietveld run:

```json
{
  "evidence_id": "EV01",
  "title": "Discovery-stage significant loci",
  "role": "metadata",
  "summary": "The discovery-stage GWAS found one genome-wide significant EduYears locus and two genome-wide significant College loci, plus additional suggestive loci.",
  "evidence_method": "GWAS meta-analysis across discovery cohorts",
  "outcome_type": "association_result",
  "presentation_type": "text",
  "source_refs": [
    {
      "source_id": "S31",
      "source_type": "span",
      "path": null,
      "span_ids": ["Rietveld_et_al_2013_Science-p001-001"],
      "quote": "one genome-wide–significant locus",
      "role": "result"
    }
  ],
  "linked_claim_ids": ["C01"],
  "metadata": {
    "table": "Table 1"
  }
}
```

The Agent evidence record keeps the support role, method, outcome, presentation
type, linked claims, and source refs together.

## Experiment Example

Claims v0 has no first-class experiment or proof object. The closest information
usually lives in claim/evidence details.

The Agent schema has `logic.experiments[]`. From the Codex Rietveld run:

```json
{
  "experiment_id": "E01",
  "title": "Discovery-stage GWAS meta-analysis",
  "verifies": ["C01"],
  "setup": "Meta-analyze cohort-level GWAS results for EduYears and College across the discovery cohorts under a prespecified analysis plan.",
  "procedure": "Harmonize phenotypes, impute to HapMap 2 CEU, control principal components, quality-control cohort results, and meta-analyze at independent centers.",
  "expected_outcome": "Genome-wide significant or suggestive loci should emerge if common variants are associated with the phenotypes.",
  "evidence_ids": ["EV01"],
  "run": "Discovery meta-analysis across 42 cohorts",
  "source_refs": [
    {
      "source_id": "S25",
      "source_type": "span",
      "path": null,
      "span_ids": ["Rietveld_et_al_2013_Science-p001-001"],
      "quote": "meta-analysis was performed across 42 cohorts",
      "role": "method"
    }
  ]
}
```

This is the main reason claim `proof[]` uses experiment IDs while
`evidence_ids[]` uses evidence record IDs.

## Concept Example

Claims v0 has semantic fields on claims and evidence, but no separate concept
layer. The Agent schema has `logic.concepts[]`. From the Codex Rietveld run:

```json
{
  "concept_id": "K01",
  "label": "Educational attainment",
  "definition": "The study phenotype, harmonized as EduYears and College completion across cohorts.",
  "source_refs": [
    {
      "source_id": "S19",
      "source_type": "span",
      "path": null,
      "span_ids": ["Rietveld_et_al_2013_Science-p001-001"],
      "quote": "years of schooling",
      "role": "method"
    }
  ]
}
```

## Source Grounding

Claims v0 usually grounds objects with span IDs:

```json
{
  "source_span_ids": ["paper1-span-0001"]
}
```

The Agent schema uses `SourceRef` objects. Claims store them in `sources[]`;
evidence records, concepts, experiments, and trace nodes store them in
`source_refs[]`:

```json
{
  "source_id": "S01",
  "source_type": "span",
  "path": null,
  "span_ids": ["Rietveld_et_al_2013_Science-p001-001"],
  "quote": "Three independent single-nucleotide polymorphisms (SNPs) are genome-wide significant",
  "role": "result"
}
```

Source refs are more expressive. They can preserve the exact quote and explain
whether the cited source is an input, method, result, interpretation, or
metadata source.

## Trace Example

The Agent schema also includes a reasoning or exploration trace. From the Codex
Rietveld run:

```json
{
  "node_id": "Q0",
  "node_type": "question",
  "support_level": "inferred",
  "summary": "Can a very large GWAS meta-analysis identify reliable genetic associations for educational attainment and clarify the architecture of the trait?",
  "source_refs": [
    {
      "source_id": "S40",
      "source_type": "span",
      "path": null,
      "span_ids": ["Rietveld_et_al_2013_Science-p001-001"],
      "quote": "complex behavioral trait—educational attainment",
      "role": "method"
    }
  ],
  "evidence": ["C01", "C02", "C03", "C04", "C05", "C06"],
  "children": [
    {
      "node_id": "N01",
      "node_type": "experiment",
      "support_level": "explicit",
      "summary": "Run discovery-stage GWAS meta-analysis across many cohorts for EduYears and College.",
      "source_refs": [
        {
          "source_id": "S41",
          "source_type": "span",
          "path": null,
          "span_ids": ["Rietveld_et_al_2013_Science-p001-001"],
          "quote": "GW AS meta-analysis was performed across 42 cohorts",
          "role": "method"
        }
      ],
      "evidence": ["E01", "EV01"],
      "children": []
    }
  ]
}
```

## What We Are Adopting

For new miner and validator work, we are adopting the Claims Agent schema as the
canonical contract.

That means:

- miners should produce `agent_output.json`
- validators should validate the Agent schema directly
- rigor checks should understand the Agent schema's logic/evidence/trace layers
- runtime metrics should live in `metadata.runtime_metrics` when available
- docs should describe v0 as the older Claims schema, not the schema new work
  should target

The adoption is not a rejection of v0's core idea. Both schemas are about
source-grounded scientific claims. The Agent schema keeps that foundation and
adds the structure we need for agentic extraction, richer validation, and future
benchmarking across agent backends.
