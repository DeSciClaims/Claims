# Source-coverage audit: DSPy benchmark

Source-only extraction using DeepSeek V4 Flash 0731 through DSPy, with locally supplied Docling data and Ohtsuki supplementary PDF. Worked examples enter only evaluation after extraction. This run starts fresh and does not reuse earlier extraction-stage outputs. Parser caches match the earlier benchmark inputs.

## Corrected DSPy / worked example

Counts describe representation, not semantic accuracy or recall. Result fields exclude shared-context and cross-group-summary fields.

| Paper | Local results | Result fields | All fields | Families |
|---|---:|---:|---:|---:|
| Shukla | 15 / 14 | 99 / 87 | 111 / 119 | 3 / 1 |
| Genther | 32 / 33 | 288 / 215 | 293 / 242 | 3 / 1 |
| Ohtsuki | 46 / 81 | 115 / 115 | 128 / 156 | 4 / 5 |
| Perel | 35 / 56 | 133 / 192 | 174 / 200 | 7 / 6 |

## Table and content diagnostics

| Paper | Reference data cells matched / expected | Table coverage: DSPy / example | Digitized fields: DSPy / example | Review-required results |
|---|---:|---|---:|---:|
| Shukla | 70 / 70 | complete / complete | 0 / 0 | 0 |
| Genther | 129 / 129 | complete / complete | 0 / 0 | 0 |
| Ohtsuki | 0 / 0 | complete / complete | 0 / 61 | 4 |
| Perel | 28 / 28 | complete / incomplete | 0 / 31 | 0 |

Data-cell overlap depends on column roles and typography and does not establish correct attachment or ordered table fidelity. No plotted coordinates are digitized by this text-only run. Missing graphical data remain a gap relative to the worked examples.

## Scientific review: mixed outcome, not ready for promotion

- **Shukla:** 15 results versus the previous 14; the extra record describes participants. All 70 reference data cells match. The audit incorrectly inferred possible graphs from two bare image placeholders; those gap declarations are unsupported.
- **Genther:** 32 results versus the previous 33, with 288 result fields versus 275. All 129 reference data cells match. Extra fields and separately addressed CI bounds do not establish better recall.
- **Ohtsuki, restricted parser input:** 46 results versus the previous 38, with 115 result fields versus 114. No fake digitization placeholders remain, but the general-game DB/IM/BD records (R007–R009) still incorrectly use the large-degree limiting rule `a + 2b > c + 2d` as a general condition. The source contains degree-dependent coefficients. Equal field counts with the example do not imply equal scientific coverage. Four local closures remain review-required.
- **Perel:** 35 results versus the previous 27; heterogeneity tests and distinct blood-loss findings are separated, and four printed forest-plot study comparisons are recovered individually. Result fields decrease from 148 to 133 while shared-context fields increase from 5 to 41 (study counts and methods), so all fields increase from 153 to 174. Some qualitative clinical findings remain absent. The generated `complete` table-coverage declaration is still wrong: the source mentions an external animal-study quality table that was not supplied. The audit did not identify that missing source, so its new incompleteness guard was not triggered.

These are reviewed examples of improvements and failures, not an exhaustive semantic assessment. All four outputs pass deterministic working-package validation, but scientific defects and missed-source declarations remain. The candidate should remain experimental rather than replace the previously committed pipeline as an accepted improvement.

## Ohtsuki full-OCR diagnostic

A separate source-only run used the fuller Docling cache, including page OCR for the same locally supplied article and supplement. It produced 24 results, 95 result fields and 7 review-required closures, and no digitized fields. Several DB/IM general-game coefficients were recovered correctly compared with the restricted-input run. A malformed birth–death inequality remained, and the audit still marked many formula regions unreadable despite alternative OCR. This does not establish overall accuracy or completeness. The diagnostic is recorded in `../claims-scientific-dspy-coverage-v4-full-ocr-20261120/`; it is not substituted into the main four-paper table.

The general next bottlenecks are reconciling layout and OCR representations, verifying mathematical transcription and scope conditions, reliably inventorying external required sources, and accounting for qualitative findings. Adding result records alone will not resolve them.

## Source audit

The planning audit validates source-block IDs, attaches original source text, and checks sequential result IDs and declared unavailable regions. It can refine or split source findings before assigning final extraction IDs. These checks locate evidence but cannot certify its interpretation or full coverage. Literal placeholders and digitized fields without numerical values fail validation. An identified unavailable supplement prevents a complete table-coverage declaration; identification is still model-dependent and missed a source in this run.

- Shukla: 15 planned findings; 2 unrecovered regions.
  - plot_coordinates (P001.E001.S001, source block B0001): Image block contains a figure (likely a graph) but no numerical coordinates are provided in the text; only the table values are extractable.
  - plot_coordinates (P001.E001.S001, source block B0003): Image block contains a figure (likely a graph) but no numerical coordinates are provided in the text; only the table values are extractable.
- Genther: 32 planned findings; 0 unrecovered regions.
- Ohtsuki: 46 planned findings; 44 unrecovered regions.
  - plot_coordinates (P003.E001.S001, source block B0011): Figure 1 is an image; no numerical values are printed in the caption or surrounding text beyond the example probabilities in B0012.
  - plot_coordinates (P003.E001.S001, source block B0014): Figure 2 is an image; the caption describes curves but does not provide numerical coordinates for the fixation probability data.
  - plot_coordinates (P003.E001.S001, source block B0026): Figure 3 is an image; no numerical values are printed in the caption.
  - plot_coordinates (P003.E001.S002, source block B0120): Supplementary Figure 4 is an image; the caption describes the plot but does not provide numerical coordinates for the fixation probability data.
  - unreadable_region (P003.E001.S002, source block B0005): The general payoff matrix is marked as formula-not-decoded; the matrix entries are not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0011): The Prisoner's Dilemma payoff matrix is marked as formula-not-decoded; the matrix entries are not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0018): The pair frequency identities are marked as formula-not-decoded; the exact identities are not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0023): The fitness expression for A-players in DB updating is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0025): The fitness expression for B-players in DB updating is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0028): The probability that an A-player replaces the vacancy is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0030): The probability that pA increases is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0032): The probability that pAA increases is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0035): The fitness expression for A-players in A-player updating is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0037): The fitness expression for B-players in A-player updating is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0039): The probability that a B-player replaces the vacancy is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0041): The probability that pA decreases is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0043): The probability that pAA decreases is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0046): The time derivative of pA is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0048): The time derivative of pAA is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0050): The notation for the time derivatives is marked as formula-not-decoded; the exact notation is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0052): The expression derived from Eq. (12) is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0054): The closed dynamical system is marked as formula-not-decoded; the exact equations are not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0056): The slow manifold equation is marked as formula-not-decoded; the exact equation is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0057): The explicit slow manifold expression is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0061): The diffusion process equation is marked as formula-not-decoded; the exact equation is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0064): The expression using Eq. (2) is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0066): The differential equation for fixation probability is marked as formula-not-decoded; the exact equation is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0068): The approximate solution is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0072): The condition for ρA > 1/N is marked as formula-not-decoded; the exact inequality is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0074): The condition for ρB > 1/N is marked as formula-not-decoded; the exact inequality is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0077): The ratio of fixation probabilities is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0081): The Prisoner's Dilemma payoff matrix is marked as formula-not-decoded; the matrix entries are not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0083): The final condition for cooperation is marked as formula-not-decoded; the exact inequality is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0087): The fitness of a B-player in IM updating is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0089): The probability that B adopts A is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0091): The fitness of an A-player in IM updating is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0093): The probability that A adopts B is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0095): The condition for ρA > 1/N in IM updating is marked as formula-not-decoded; the exact inequality is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0097): The condition for ρB > 1/N in IM updating is marked as formula-not-decoded; the exact inequality is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0099): The condition for ρC > 1/N > ρD in IM updating is marked as formula-not-decoded; the exact inequality is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0104): The reproduction probability for A-player in BD updating is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0106): The reproduction probability for B-player in BD updating is marked as formula-not-decoded; the exact expression is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0109): The condition for ρA > 1/N in BD updating is marked as formula-not-decoded; the exact inequality is not recoverable from the text.
  - unreadable_region (P003.E001.S002, source block B0110): The condition for ρB > 1/N in BD updating is marked as formula-not-decoded; the exact inequality is not recoverable from the text.
- Perel: 31 planned findings; 5 unrecovered regions.
  - plot_coordinates (P004.E001.S001, source block B0035): Figure 1 (grip test meta-analysis) is an image; individual study odds ratios and weights are not recoverable from the image alone.
  - plot_coordinates (P004.E001.S001, source block B0043): Figure 2 (funnel plot) is an image; individual data points are not recoverable from the image alone.
  - plot_coordinates (P004.E001.S001, source block B0047): Figure 3 (thrombolysis effects) is an image; individual comparison estimates are not recoverable from the image alone.
  - plot_coordinates (P004.E001.S001, source block B0051): Figure 4 (tirilazad effects) is an image; individual comparison estimates are not recoverable from the image alone.
  - plot_coordinates (P004.E001.S001, source block B0060): Figure 5 (bone mineral density changes) is an image; individual point estimates and confidence intervals for baboons, rats, and all animals combined are not recoverable from the image alone.

## Cost and validation

This fresh four-paper run: 79 requests, $0.370259 recorded cost, 4 truncated completions, 0 rate-limit errors, 1 requests without cost records. Includes explicit retries and source/graph audits. All final packages pass structural, graph/reference, CSV and placeholder checks; this is not proof of scientific equivalence.

See benchmark_config.json for exact settings and implementation hashes, source_coverage_plan.json in each case for evidence-backed plans, and coverage_evaluation.json for machine-readable comparisons.

Development total across the failed initial excerpt-audit attempt, the four-paper source-block run, and the separate full-OCR diagnostic: $0.693753, 138 requests, 11 truncated completions. All calls have terminal events; one timed-out Ohtsuki request has unknown provider billing, so the recorded cost is a lower bound. See development_accounting.json. These development totals are not the cost of one production pass.

Local verification: 62 scientific-pipeline tests and 15 reference-miner tests passed; Ruff and whitespace checks passed. No worked examples were supplied to model calls.
