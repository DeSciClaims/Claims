You are summarizing one section of a scientific paper for a later claim extractor.

Important:
- Return STRICT JSON ONLY.
- This summary is for orientation only.
- Do not output claim/evidence schema here.
- Do not invent information outside the section text.

Return JSON with keys:
- `summary_text`: 2-5 sentence summary of what this section is doing
- `section_role`: one of `background`, `methods`, `results`, `discussion`, `supplement`, `mixed`
- `key_entities`: array of important entities named in the section
- `key_findings`: array of important results or assertions in the section
- `extractability_assessment`: short note about whether this section contains self-contained, locally supported claims
- `locality_confidence`: float 0.0-1.0 indicating how likely claims in this section can be extracted with local context and evidence only
