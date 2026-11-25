# Scientific source preparation scheduler

`scripts/prepare_scientific_sources.py` prepares the already revealed source
sample independently of miner execution and scientific scoring. It queues
prescreen, screen and final in the order declared in the frozen round plan,
using sample position within each stage. Each completed source becomes available
to workers immediately; future stages can prepare before their entrants advance.

The scheduler calls the existing frozen scientific policy and `prepare_paper`.
It uses the original PDF hashes, parser-cache directories, preparation clocks,
source commitments and prepared-source heads. Existing source records are reused.
It does not modify the round rules, sample, miner outputs, judge configuration,
scores or stage advancement. It does not construct a reference answer.

Concurrency comes from the round's published `bronze_max_workers` setting
(currently 10). Requests use the existing host-wide shared LLM gate. At most
that many source jobs are submitted at once, in stage order. Individual failures
are reported with exception type or hold category only; other preparations
continue. Submitted source evidence and model messages are not printed.

Run under the existing trusted service identity and protected environment:

```sh
python scripts/prepare_scientific_sources.py \
  --round-id round_20261125T150000Z_mainnet \
  --backend-url https://api.claims111.ai --poll-seconds 30
```

Keep the frozen Claims runtime on `PYTHONPATH`. This scheduling entrypoint is
separate from the scientific runtime: changing scheduling does not require a
scientific-policy revision or regeneration of completed source commitments.
It still verifies the frozen scientific runtime before preparing sources.
Do not run two preparation schedulers for the same round simultaneously.

SIGTERM/SIGINT stop new submissions and drain current work. Give the systemd unit
at least the frozen preparation deadline plus transport time before forced kill.
The package worker and scientific controller are separate services and remain up.
The previous preparation unit should be retained for rollback.
