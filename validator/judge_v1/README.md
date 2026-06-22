# Judge V1 Validator

This folder contains the public `judge_v1` validator, which is the first public
release of the benchmark's internal judge-v3 logic.

## What Lives Here

- `runner.py`: intrinsic and gold evaluation flow
- `__main__.py`: CLI entrypoint
- `rubric.py`: public `judge_v1` rubric and payload flattening helpers
- `review_data.py`: reviewed CSV/XLSX loaders for gold-mode evaluation
- `dspy_runtime.py`: DSPy/OpenRouter setup for the LLM judge
- `JUDGE_V1_VALIDATION_SYSTEM.md`: packaged judge system prompt
- `README.benchmark.md`: original benchmark design note kept for reference

## Install

From the `Claims/` directory:

```bash
pip install -r requirements.txt
```

The LLM-backed judge needs `OPENROUTER_API_KEY`.

## Run

Judge a miner output intrinsically:

```bash
python -m validator.judge_v1 \
  --extraction-output-json /path/to/section_context_v1_output.json \
  --mode intrinsic
```

Run the same command without calling the LLM judge, which is useful for a local
smoke test:

```bash
python -m validator.judge_v1 \
  --extraction-output-json /path/to/section_context_v1_output.json \
  --mode intrinsic \
  --judge-version none
```

Run gold-mode judging against a reviewed CSV or XLSX file:

```bash
python -m validator.judge_v1 \
  --extraction-output-json /path/to/section_context_v1_output.json \
  --mode gold \
  --gold-reviewed-file /path/to/reviewed_claims.csv
```

Write XLSX instead of CSV:

```bash
python -m validator.judge_v1 \
  --extraction-output-json /path/to/section_context_v1_output.json \
  --mode intrinsic \
  --xlsx
```

Override the output directory:

```bash
python -m validator.judge_v1 \
  --extraction-output-json /path/to/section_context_v1_output.json \
  --mode intrinsic \
  --output-dir /tmp/claims-judge
```

## Outputs

Each run writes:

- an intrinsic or gold evaluation CSV/XLSX
- `manifest.json`
