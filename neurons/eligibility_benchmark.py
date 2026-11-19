"""Private, resumable judge benchmark. Planning never makes inference requests."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from validator.benchmarks.eligibility import (
    build_report,
    load_config,
    load_packets,
    plan,
    prepare_packets,
    run,
    finalize_interrupted_trials,
)
from validator.benchmarks.eligibility_transport import write_private


def supervise(command: list[str], output: Path, seconds: float):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.stat().st_mode & 0o077:
        raise ValueError("benchmark output directory must be private (chmod 700)")
    fd = os.open(
        output / "supervisor-child.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
    )
    expired = False
    started = time.time()
    with os.fdopen(fd, "w") as log:
        child = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            while child.poll() is None:
                remaining = started + seconds - time.time()
                if remaining <= 0:
                    expired = True
                    child.terminate()
                    break
                time.sleep(min(0.25, remaining))
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
        except BaseException:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
            raise
    finalize_interrupted_trials(output, child.pid)
    write_private(
        output / "supervisor.json",
        {
            "runner_pid": child.pid,
            "wall_time_seconds": seconds,
            "started_at": started,
            "ended_at": time.time(),
            "child_returncode": child.returncode,
            "stopped_by_wall_timeout": expired,
            "child_log": "supervisor-child.log",
        },
    )
    return build_report(output), 124 if expired else child.returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare", help="export saved production task/schema files to private JSONL"
    )
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--corpus", type=Path, required=True)
    prepare.add_argument("--include-recovery", action="store_true")
    for name in ("plan", "run"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--corpus", type=Path, required=True)
        command.add_argument("--phase", action="append", required=True)
        if name == "run":
            command.add_argument(
                "--in-process", action="store_true", help=argparse.SUPPRESS
            )
            command.add_argument("--output", type=Path, required=True)
            command.add_argument(
                "--live",
                action="store_true",
                help="explicitly enable paid API requests",
            )
    report = commands.add_parser("report")
    report.add_argument("--output", type=Path, required=True)
    report.add_argument(
        "--labels",
        type=Path,
        help="private packet_id/decisions JSONL; never sent to models",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_packets(
                args.source, args.corpus, include_recovery=args.include_recovery
            )
        elif args.command == "report":
            result = build_report(args.output, args.labels)
        else:
            config, packets = load_config(args.config), load_packets(args.corpus)
            result = plan(config, packets, args.phase)
            if args.command == "run" and args.live:
                if not args.in_process:
                    command = [
                        sys.executable,
                        "-m",
                        "neurons.eligibility_benchmark",
                        "run",
                        "--config",
                        str(args.config.resolve()),
                        "--corpus",
                        str(args.corpus.resolve()),
                        "--output",
                        str(args.output.resolve()),
                        "--live",
                        "--in-process",
                    ]
                    for phase in args.phase:
                        command.extend(["--phase", phase])
                    result, code = supervise(
                        command, args.output, config["wall_time_seconds"]
                    )
                    print(json.dumps(result, sort_keys=True))
                    return code
                result = run(config, packets, args.output, args.phase)
        # Public console output contains aggregates only, never prompts or credentials.
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        result = {"status": "benchmark_error", "error_type": type(exc).__name__}
        if type(exc) is ValueError:
            result["detail"] = str(exc)[:240]
        print(json.dumps(result))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
