# How the structured-result validator works

Claims is moving from extracting individual prose claims to extracting
**structured scientific results**: quantities, their meaning, the conditions
under which they apply, and the relationships between them.

The validator checks whether those pieces **jointly represent the scientific
finding faithfully**. Copying a number correctly is not enough.

## A simple example

Suppose a paper reports a treatment effect of **−5**, a standard error of **1.2**,
and **p = 0.01**.

An extraction can contain all three correct numbers and still be wrong if the
standard error belongs to a different estimate, the p-value tests another
comparison, or the result applies to a different population.

The validator checks the values **and their connections and context**. Related
objects form a local result; results answering a common scientific question from
the same investigation can belong to a broader result family.

## The intended evaluation process

1. **Check the package mechanically.** Code checks the schema, IDs, references,
   relationships, computed result membership and actual table CSV files.
2. **Choose checks from the source.** Select scientific findings and numerical
   table regions independently of competing pipelines. This makes omissions
   detectable, including findings every pipeline missed.
3. **Verify scientific meaning.** An AI judge compares each pipeline's
   representation with the original paper and supplied supplements. Separate
   audits check table transcription and unsupported scientific assertions.
4. **Score quality and review exceptions.** Record clear findings and send
   uncertain or disputed cases for additional independent review.

Mechanical checks are inexpensive. A scientific review can cover several related
objects together. Sampling and exception review concentrate expensive judgments
where they are useful.

**The full process above is the design target.** The current implementation and
remaining work are distinguished below.

## What the proposed score rewards

| Dimension | Question |
|---|---|
| **G — Mechanical compliance gate** | Does the extraction satisfy the mandatory mechanical requirements? |
| **C — Scientific content** | Is the sampled scientific information represented correctly and completely, including relationships and context? |
| **T — Table fidelity** | Is the sampled numerical-table content faithfully preserved? |
| **P — Output reliability** | Are the pipeline's submitted scientific assertions supported by the source? |

For papers with qualifying numerical tables, the proposed score is:

**Q = G × P × (7C + 2T) / 9**

When no qualifying numerical tables exist:

**Q = G × P × C**

A failed mechanical gate gives no credit. Unsupported assertions reduce output
reliability and therefore reduce the contribution of both content and tables.
Producing more objects, local results or families does not itself earn points.

## Evaluation is separate from database curation

The competition evaluates extraction quality against the source. It does not
need to assemble competing outputs into one canonical reference dataset.
Different scientifically valid organizations can receive credit.

Building a curated scientific database is a separate process. Worked examples
help test the schema and implementation; their object counts are not scoring
targets or a substitute for checking the paper.

## What is implemented today

| Implemented | Still to implement or verify |
|---|---|
| Shared mechanical checks for JSON, graphs and delivered CSVs | Independent source sampling and its sampling policy |
| DSPy programs for source, scientific-attachment and family review | C, T and P scoring, calibration and the Q calculation |
| Per-rule reports with evidence and explicit uncertainty | Additional independent review and exception resolution |
| Reference preflight and an opt-in trusted controller gate | Live verification of scientific-review accuracy |

The current reviewer processes submitted records in batches and performs broader
coverage reviews. **That is not yet the independent source-sampling workflow.**
The current submission-readiness gate also does not calculate the proposed
competition score.

Checks report **pass**, **fail**, **review required**, **not checked**, or
**not applicable**. An unavailable reviewer or insufficient evidence does not
become a pass. The trusted controller runs its own validation; it does not accept
a miner's claim that its output is valid.

Required supplements must be supplied upstream or declared unavailable. Figure
values read from plotted positions need appropriate image evidence and review
capability. The schema can represent both; the validator must disclose evidence
or capability gaps.

## Code and usage

The shared validator lives here in `Claims/validator/scientific_result_v1`.
The miner uses it for diagnostic checks, `claims-reference-miner` uses it for
preflight, and the scientific controller path independently validates delivered
packages. Existing `agent_v1` rounds are not automatically migrated.

See [implementation and operation](IMPLEMENTATION.md) for commands, source bundles,
rule coverage, report fields, review budgets and controller configuration.
