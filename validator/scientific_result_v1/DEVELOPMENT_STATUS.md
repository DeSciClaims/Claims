# Structured-result validator development status

Implementation history, benchmark results and remaining rollout/verification work.
Historical observations below describe their recorded implementation revisions.


The source-task scorer lives in [`source_tasks/`](source_tasks/). It implements
source preparation, committed sampling, original-byte indexing, scientific review,
exact C/T/P/Q arithmetic, retained evidence and provider-free score replay.
The competition adapter keeps this protocol separate from legacy prose-claim scoring.
The older diagnostic validator remains available for miner preflight.

Scripted P005 tests exercise perfect outputs, scientific errors, omissions, shared
storage, table precision, mechanical failures, common holds and oversized independent
shards. Additional checks exercise immutable transport, stale writes and atomic
score-projection recovery. The frozen judge routes use Qwen3.8 Flash (source/primary), DeepSeek V4 Flash
0731 (secondary), and Kimi K2.5 (arbiter), with a 16,384-token output allowance.
Docling remains the PDF/OCR/table parser. Its native rows and merged cells are
retained as parser evidence; table layout does not require model-written crop
coordinates or fabricated source-review approvals. Figure/OCR capability preflights
do not establish scientific accuracy.

The owner increased validator source-evidence and complete review-packet allowances
to **32,000 tokens** on 21 November 2026. Every source call can use the full allowance;
up to two additional context retrievals remain permitted within it. Judge output
stays at 16,384 tokens. These validator settings do not alter miner limits.

The live synthetic P005 text/table smoke test passed on 21 November 2026 using
Qwen/DeepSeek/Kimi and the 16,384-token judge output allowance. Independent source
preparation identified one two-row table. The unchanged example scored Q=1; a
copy with a mean changed from 3.50 to 35 scored Q=0 (C=1/2, T=0, P=0).
Provider-free replay passed. Five technical/contract retries recovered; there was
no output truncation. Recorded judge cost for the successful run was $0.172594,
plus one timed-out request whose billing is unknown. These are validator expenses,
separate from the miner's $0.10 limit. This synthetic check does not verify real-paper
PDF/image review, operational readiness, or scientific accuracy.

The earlier real-PDF diagnostic used visual source inventory and failed on
inspection manifests and crop coordinates. The owner then chose to rely on
independent Docling PDF parsing. The active PDF path now uses parser evidence and
has no image-crop dependency. The four-paper diagnostic completed under that policy
on 21 November 2026; all five supplied PDF files (including the originally supplied
Ohtsuki supplement) were independently parsed. Provider-free replay passed for all
four retained receipts.

| Paper | Mechanical gate | Scientific score |
| --- | --- | --- |
| Shukla | Pass | Held: source-context and complete-packet bounds |
| Genther | Pass | Held: source-context requests and complete-packet bounds |
| Ohtsuki | Fail: seven V13 closure errors | Q=0; scientific judging skipped |
| Perel | Fail: eight V13 closure errors | Q=0; scientific judging skipped |

The unmodified historical miner packages are the inputs, not worked-example gold.
Validator failures leave Q unset; they do not turn into miner failures. This run
recorded $2.372561 in validator provider charges, with no unknown-billing attempts.
It is retained at `outputs/claims-validator-four-papers-docling-batch-v2-20261121`
in the workspace. Subsequent source-card changes add bounded context retrieval
from retained Docling evidence and stop downstream judging after unresolved common
task construction. Those changes have offline regression coverage. This diagnostic does not
establish scientific judge accuracy or production readiness.

The subsequent 32k retry is retained at
`outputs/claims-validator-four-papers-docling-32k-20261121` in the workspace.
Shukla still holds on complete joint-packet size; a review of one insulin table
row pulled all 15 results into a roughly 130k-token packet. Genther holds on
ambiguous source-table correspondence. Neither paper now reports an 8k source
budget failure. Ohtsuki and Perel retain their mechanical Q=0 outcomes. Offline
replay passed for all four receipts. This retry recorded $1.972705 in validator
charges with no unknown billing. All 113 offline regression tests passed before
the run. Raising the allowance alone did not resolve packet construction or table
matching; these remain implementation work.

The next implementation revision fixes unassigned candidate noise, shared-context
closure expansion and table-only identity matching, with independent arbitration.
It uses lossless transport for assertions and repeated source hashes; original
audit records and literal values stay retained. The matcher now returns one typed
requirement-match list, from which code derives candidate references. DeepSeek is
pinned to DeepInfra after repeated fenced responses from Baidu. Scientific status
and counterexample constraints are present in the provider JSON schema as well as
runtime validation. All 129 regression tests pass. Targeted live preflights pass
for the simplified matcher, source-review format and pinned scientific reviewer.
Source-only drafts also distinguish scientific findings from source context;
independent review verifies that scope. Complete context requirements may use
separate mandatory shards, without splitting numerical findings. Audit ownership
is exactly packet.match.candidate_refs; related assertions are grouped coherently,
and replay checks exhaustive disjoint coverage of each parent bucket. Context-only
association headers are explicitly partial original identity/scope definitions,
not missing or inferred numerical values. A fresh full live retry is required
before claiming complete real-paper scoring.

The provider response schema is now bound to each request's task/packet IDs,
applicable scientific/table dimensions, source witnesses and shown output pointers.
Outcome-dependent checks and counterexample requirements remain strict. Array
uniqueness is checked in code because the selected provider grammars do not support
that JSON Schema keyword. Targeted live tests for the previously exhausted binding
and table-vector/pointer failures pass on their first calls. The complete run still
must finish before claiming final real-paper verification.

**Scientific judge accuracy is unmeasured.** On 21 November 2026 the deployment
owner waived the unavailable signed 200-positive/200-negative truth corpus as an
activation requirement. This exception is explicit in the amended guide and frozen
policy. Conformance, operational testing, cost/capacity and recovery checks remain.
The implementation must not report unmeasured accuracy as qualified.

Full E01–E50 conformance and the operational pilot remain to be completed. Automatic
common replacement and sealed-reference fallback still require full competition
integration; the current path blocks incompatible publication and fresh reference
reruns. This README does not certify production readiness or activate a live round.

See [implementation and operation](IMPLEMENTATION.md) for entry points and the
[amended validation guide](../../schemas/scientific-result-extraction/v1.1/VALIDATION_GUIDE.md)
for the complete policy and contracts.

The packet revision now keeps unrelated family co-members as explicit original
identity/scope headers, with full focused assertions and scientific dependencies.
Transport v2 pools repeated long original JSON literal strings and round-trips
exactly to the retained audit packet. Source-only drafting and independent review
use complete condition-specific requirements within the same coherent parent card;
every required shard must pass and sampling weights stay unchanged. All 131
regression tests pass. The r5 four-paper diagnostic retained successful table
correspondences but still held Shukla/Genther on compound packet bounds; a fresh
r6 run verifies these final construction changes. Historical reports are retained
with their original settings and results.

Independent index batches now use up to four request contexts per case, with the
existing global 16-request gate and four case workers. Ordered aggregation keeps
matching results and coverage identical to serial scanning. All attempt records
survive a failed batch; no incomplete search becomes absence. All 133 tests pass.
The r6 run resolved packet/table holds and scored Shukla; Genther reached the
unchanged 30-minute generation deadline. The fresh r7 run checks this scheduling
correction without raising evidence, request, retry, or deadline limits.
