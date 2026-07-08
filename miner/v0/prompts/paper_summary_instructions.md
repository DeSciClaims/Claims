You are given structured summaries of all sections of a scientific paper.

Your job is to synthesize a whole-paper understanding scaffold that will help a later extractor interpret each section correctly.

Important:
- Return STRICT JSON ONLY.
- Do not invent findings not present in the section summaries.
- Do not rewrite the paper in long prose.
- Keep the output concise and useful for downstream extraction.
- This output is contextual scaffolding only, not claim provenance.

Return JSON with keys:
- `paper_summary`: short paragraph capturing the paper's main story
- `main_findings`: array of short bullet-like strings
- `limitations`: array of short bullet-like strings
- `evidence_map`: array of short strings describing where the strongest evidence appears across sections
