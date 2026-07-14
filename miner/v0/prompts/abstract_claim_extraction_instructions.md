You are extracting all scientific claims made in the abstract of one paper.

The abstract is the only source for claims in this stage. The whole-paper summary is context only.
Do not extract claims from the summary, title, section summaries, or your background knowledge.
If `validation_feedback_json` is non-empty, it identifies problems in a previous abstract claim list. Fix those exact problems and return the full revised list.

Return STRICT JSON ONLY with key:
- `abstract_claims`

Critical rules:
- Extract every contribution claim made in the abstract.
- A contribution claim is a statement the paper presents as its own finding, result, estimate, demonstrated association, identified entity/locus/effect, method contribution, comparative result, mechanistic interpretation grounded in this study, or conclusion drawn from this study.
- Do not extract background facts, prior-work findings, motivation, field consensus, problem importance, generic definitions, citation context, or speculative future work unless the abstract explicitly frames the statement as something this paper found, showed, estimated, identified, demonstrated, developed, tested, or concluded.
- If a statement is attributed to prior literature, widely accepted background, or a generic motivation for the study, exclude it.
- Include central findings, conclusions, hypotheses, method-performance claims, comparison claims, association claims, mechanistic claims, and claims about what the study identified or demonstrated.
- Do not require evidence to be present in the abstract. Evidence will be linked in a later full-paper stage.
- Do not extract background, prior-work motivation, or generic domain facts unless the abstract presents the statement as this paper's own conclusion or contribution.
- Make claims atomic and self-contained.
- Split compound abstract sentences into separate claims when they assert multiple independent findings, outcomes, mechanisms, samples, models, entities, or comparisons.
- Split abstract claims that contain multiple SNPs, variants, genes, loci, outcomes, samples, models, phenotypes, or targets when each item is part of a separable result.
- Split "both A and B" claims when the abstract asserts the same result for two outcomes or measures. For example, a polygenic score claim about both educational attainment and cognitive function should become one claim per outcome.
- Split claims with lists such as "health, cognitive, and central nervous system phenotypes" when the abstract asserts separate associations with each phenotype category.
- Split claims with multiple identifiers such as `rs9320913, rs11584700, rs4851266`; do not emit one bundled claim unless the proposition is only about the group as a group.
- Preserve meaning-critical modality, scope, comparator, population, condition, and qualifier language.
- Do not make claims stronger than the abstract wording.
- Do not include routine support statistics such as P values, odds ratios, confidence intervals, sample size, R2, or standard errors in `claim_text` unless the proposition itself is about that numeric value.
- Treat claim text as the paper's asserted takeaway. Put evidence payloads such as P values, table rows, replication statistics, and sample sizes into evidence later unless the numeric magnitude is itself the abstract claim.
- Preserve parent context for atomic splits. When one abstract sentence yields multiple atomic claims, give them the same `claim_group_id` and include `decomposition_parent_text`.
- Include evidence requirements for each claim: the key entity, outcome, statistic, comparator, sample, condition, figure/table, or qualifier that later evidence must mention to be valid.
- Do not include markdown fences, explanations, or commentary.

For each abstract claim, include:
- `claim_text`
- `source_candidate_ids`: use short IDs such as `a0`, `a1`, `a2` for the abstract clause or sentence that produced the claim
- `claim_group_id`: stable short ID for the abstract sentence/clause group, such as `ag0`
- `decomposition_parent_text`: original abstract sentence/clause text when the claim was split from a compound statement, otherwise empty string
- `evidence_requirements`: list of key source-side requirements that valid evidence should explicitly satisfy
- `contribution_eligible`: always `true` for returned claims
- `contribution_role`: one of `main_finding`, `secondary_finding`, `method_contribution`, `estimate`, `interpretation`, or `conclusion`
- `contribution_gate_reason`: short reason this is a contribution from this paper rather than background or prior work
- `claim_subtype`: one of `hypothesis`, `causal`, `associational`, `mechanistic`, `comparative`, `descriptive`, `model_performance`, `none`, or `unclear`
- `modality`: one of `certain`, `probable`, `possible`, `speculative`, or `unclear`
- `polarity`: one of `positive`, `negative`, `null`, `mixed`, or `unclear`
- `attribution`: use `own_work` for claims made by this paper
- `extractor_confidence`

Output example:
{
  "abstract_claims": [
    {
      "claim_text": "Variant A is associated with trait Y.",
      "source_candidate_ids": ["a0"],
      "claim_group_id": "ag0",
      "decomposition_parent_text": "",
      "evidence_requirements": ["Variant A", "trait Y", "association result"],
      "contribution_eligible": true,
      "contribution_role": "main_finding",
      "contribution_gate_reason": "The abstract presents this association as a result identified by this study.",
      "claim_subtype": "associational",
      "modality": "certain",
      "polarity": "positive",
      "attribution": "own_work",
      "extractor_confidence": 0.91
    },
    {
      "claim_text": "Model M improves prediction of outcome Z compared with baseline models.",
      "source_candidate_ids": ["a1"],
      "claim_subtype": "model_performance",
      "modality": "certain",
      "polarity": "positive",
      "attribution": "own_work",
      "extractor_confidence": 0.87
    }
  ]
}

Atomicity examples:
- Abstract says: `Three independent SNPs are genome-wide significant (rsA, rsB, rsC), and all three replicate.`
  - Return one claim for `rsA` being genome-wide significant and replicated.
  - Return one claim for `rsB` being genome-wide significant and replicated.
  - Return one claim for `rsC` being genome-wide significant and replicated.
- Abstract says: `A linear polygenic score accounts for ≈2% of the variance in both educational attainment and cognitive function.`
  - Return one claim about educational attainment.
  - Return one claim about cognitive function.
- Abstract says: `Genes in the region of the loci have previously been associated with health, cognitive, and central nervous system phenotypes.`
  - Return separate claims for health phenotypes, cognitive phenotypes, and central nervous system phenotypes if the abstract asserts each association.

If the abstract contains no paper-owned claims, return:
{
  "abstract_claims": []
}
