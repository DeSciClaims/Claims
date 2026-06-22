# Contributing

Thanks for contributing to the Claims subnet repo.

This repository is still evolving, so the main goal is to keep contributions
small, readable, and easy to review.

## Workflow

1. Fork the repository.
2. Create a branch for your change.
3. Make your changes in your fork.
4. Open a pull request back to `main`.

If your change is substantial, include a short note in the PR description
covering:

- what changed
- why it changed
- how you verified it

## Contribution Types

There are two main kinds of contributions in this repo:

- adding a new miner or judge pipeline version
- improving an existing pipeline or the shared docs/schemas around it

## Adding A New Pipeline

If you are contributing a new miner or judge pipeline, scope it to its own
folder.

Examples:

- `miner/<pipeline_name>/`
- `validator/<judge_name>/`

Each pipeline folder should be self-contained as much as possible. That usually
means keeping its own:

- `README.md`
- `requirements.txt`
- runtime code
- prompts or rubric files
- local helpers that are specific to that pipeline

The current repo direction is to prefer explicit folder-scoped implementations
over a large shared internal package layer.

## Enhancing An Existing Pipeline

If you are not adding a new pipeline folder, assume you are improving an
existing one.

That usually means changes inside a folder such as:

- `miner/section_context_v1/`
- `validator/judge_v1/`

When enhancing an existing pipeline:

- preserve the existing folder boundary
- avoid moving pipeline-specific logic into new shared abstractions unless there
  is a strong reason
- update the local `README.md` if the setup, runtime behavior, or outputs change

## Docs And Schema Changes

Documentation and schema improvements are welcome.

If your change affects behavior, structure, or expected outputs, update the
relevant docs in:

- `docs/`
- the pipeline-local `README.md`
- root `README.md` when the repo entrypoint or contributor expectations change

If you change object shapes or validation expectations, review the matching
files in `schemas/`.

## Verification

Before opening a PR, run the smallest reasonable checks for your change.

Useful examples in this repo:

- `python -m py_compile miner/section_context_v1/*.py validator/judge_v1/*.py`
- `python -m validator.judge_v1 --help`
- `python -m miner.section_context_v1 --help`
- validator smoke runs with `--judge-version none`

If your pipeline depends on external services or credentials, note clearly in
the PR what you could and could not verify locally.

## Style

- Keep changes focused.
- Prefer simple folder-local code over clever abstractions.
- Keep miner and validator packages understandable on their own.
- Update filenames, numbering, and links together when editing docs.

## Pull Requests

A good PR here is:

- scoped to one main change
- documented well enough for another contributor to follow
- consistent with the pipeline-version structure of the repo

If you are unsure where something belongs, default to the nearest existing
pipeline folder and keep the change local.
