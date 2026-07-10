You are repairing scientific claim-evidence pairs extracted from one section of a paper.

This is a narrow repair stage. Do not perform broad extraction from scratch.

You are given:
- raw section text
- candidate spans
- classified spans
- decomposed units
- current claims
- current evidence items
- current claim-evidence links
- optional validation feedback from a previous repair attempt

Return STRICT JSON ONLY with keys:
- `repair_actions`
- `claims`
- `evidence_items`
- `claim_evidence_links`

Your job:
- Detect final claims that are compound or bundled.
- Split compound claims into atomic, self-contained claims.
- Detect final claims that include evidence payloads such as P values, odds ratios, sample sizes, confidence intervals, R2 values, standard errors, or method details.
- Move evidence payloads out of `claim_text` and into linked evidence items unless the proposition being asserted is specifically about the numeric quantity itself.
- Detect evidence items that simply repeat `claim_text`.
- Rewrite duplicate evidence items so they preserve the source-side observation, statistic, result, figure/table output, method detail, or reported datum that evaluates the claim.
- Preserve valid atomic claims.
- Preserve or create evidence items that directly support each repaired claim.
- Return the full revised claim/evidence/link set for this section, not only the changed records.

Atomicity rules:
- One final claim must assert one checkable proposition.
- If a claim contains multiple entities, variants, predictors, outcomes, samples, models, thresholds, conditions, or timepoints with distinct numeric/statistical payloads, split it.
- Split claims with cues such as "two loci", "three SNPs", "four outcomes", "A and B", "respectively", or multiple parenthetical identifiers/statistics.
- Split claims that contain more than one concrete identifier, named entity, locus, SNP, variant, gene, score, model, outcome, cohort, or sample when each item is part of a separable result.
- This remains true after moving statistics into evidence: `Variant A and Variant B are associated with Trait Y` is still two claims, not one.
- When a source sentence provides item-level identifiers and item-level statistics, prefer one claim per item over a count-level summary.
- Keep a count-level claim only when item identities or item-level payloads are not available in the local section text.
- Do not merge claims merely because they share the same source sentence or evidence item.

Claim/evidence rules:
- Claim = the proposition being asserted.
- Evidence = the observation, measurement, statistic, result, figure/table output, model estimate, or reported datum used to evaluate the claim.
- A paper-owned result finding can be a claim when the finding itself is the paper's contribution.
- Evidence item text should preserve source-side reported datum/result wording and should not be a polished duplicate of the claim when avoidable.
- A claim should generally not contain P values, odds ratios, effect sizes, confidence intervals, sample sizes, R2 values, standard errors, or variance-explained percentages. Put those in evidence.
- A claim should generally not contain effect magnitudes such as "1.8 percentage-point difference" or "0.022% of phenotypic variance"; put those in evidence.
- A claim may contain a numeric value only when the proposition itself cannot be stated without the number, such as a benchmark claim about a specific estimated upper bound or a direct comparison between numeric benchmarks.
- If an extracted claim says "X is associated with Y with P value p", repair it to claim "X is associated with Y" and evidence "The section reports X associated with Y with P value p."
- If an extracted claim says "X has odds ratio r for Y", repair it to claim "X is associated with Y" and evidence "The section reports odds ratio r for X and Y."
- If an extracted claim says "X corresponds to a d percentage-point difference in Y", repair it to claim "X is associated with Y" and evidence "The section reports X corresponds to a d percentage-point difference in Y."
- If an extracted claim says "X explains q% of variance in Y", repair it to claim "X explains variance in Y" and evidence "The section reports X explains q% of variance in Y."
- Prefer de-self-referenced claim wording. Repair "we identified X for Y" to "X is associated with Y" or "X is a genome-wide-significant locus for Y" when supported by the source.
- If `claim_text` and `evidence_items[*].summary_text` are identical or nearly identical, rewrite one or both so the claim is proposition-only and the evidence is source-side support.
- Every returned claim must have at least one link to an evidence item.
- Every returned evidence item must be linked to at least one claim.

Final output self-check:
- Before returning JSON, inspect every returned `claim_text`.
- If any `claim_text` contains "two", "three", "four", "respectively", or "and" joining multiple identifiers/entities in a scientific result, split it before returning.
- If any `claim_text` contains two or more identifiers such as two SNP IDs, variant names, gene symbols, model names, cohorts, or outcomes, split it before returning unless the exact proposition is explicitly about the group as a group.
- If any `claim_text` contains routine support statistics such as `P =`, `p <`, `odds ratio`, `OR =`, `R2`, `% of variance`, `percentage-point`, `confidence interval`, `SE =`, or sample sizes, move those payloads into evidence before returning.
- If any `summary_text` exactly repeats its linked `claim_text`, rewrite the evidence before returning.

For each returned claim, include:
- `claim_text`
- `source_candidate_ids`
- `claim_subtype` when clear
- `modality` when clear
- `polarity` when clear
- `attribution` when clear
- `extractor_confidence` when clear

For each returned evidence item, include:
- `role`
- `summary_text`
- `source_candidate_ids`
- `evidence_type` when clear
- `rhetorical_role` when clear
- `evidence_method` when clear
- `outcome_type` when clear
- `presentation_type` when clear
- `extractor_confidence` when clear

For links:
- Use `claim_index` and `evidence_index`.
- Include `relation`.
- Include `confidence` when clear.

Repair action examples:
{
  "repair_actions": [
    {
      "action": "split_claim",
      "reason": "The original claim bundled Variant A and Variant B with separate P values.",
      "source_claim_index": 1,
      "new_claim_indexes": [1, 2]
    },
    {
      "action": "separate_claim_evidence",
      "reason": "The original claim included a P value that belongs in linked evidence.",
      "source_claim_index": 0,
      "new_claim_indexes": [0]
    },
    {
      "action": "rewrite_duplicate_evidence",
      "reason": "The evidence item repeated the claim instead of preserving the supporting statistic.",
      "source_claim_index": 0,
      "source_evidence_index": 0
    }
  ]
}

Concrete split example:
- Current claim: `Two genome-wide-significant loci, Variant A with P value p1 and Variant B with P value p2, were identified for Trait Y.`
- Repaired claims:
  - `Variant A is a genome-wide-significant locus for Trait Y.`
  - `Variant B is a genome-wide-significant locus for Trait Y.`
- Repaired evidence:
  - `The section reports Variant A as genome-wide significant for Trait Y with P value p1.`
  - `The section reports Variant B as genome-wide significant for Trait Y with P value p2.`

Concrete claim/evidence separation example:
- Current claim: `Variant A has an odds ratio of r for outcome Y.`
- Current evidence: `Variant A has an odds ratio of r for outcome Y.`
- Repaired claim: `Variant A is associated with outcome Y.`
- Repaired evidence: `The section reports an odds ratio of r for Variant A and outcome Y.`

Concrete post-statistic split example:
- Current claim: `Variant A and Variant B are genome-wide-significant loci for Trait Y.`
- Repaired claims:
  - `Variant A is a genome-wide-significant locus for Trait Y.`
  - `Variant B is a genome-wide-significant locus for Trait Y.`

If no repair is needed, return the original claims, evidence items, and links unchanged, with a `repair_actions` record explaining that no compound claims were found.

If `validation_feedback_json` is non-empty, it identifies specific remaining issues in your previous repaired output. Fix those exact issues before returning. Do not ignore feedback records such as `bundled_multiple_identifiers`, `claim_contains_support_statistic`, or `evidence_repeats_claim`.
