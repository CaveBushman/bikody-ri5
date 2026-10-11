#!/usr/bin/env python3
"""Offline ACK statistics and isolated SQLite queue benchmark (stdlib only)."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import platform
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

ACK = re.compile(
    r"Průjezdy (?P<decoder>[\w-]+): (?P<frames>\d+) rámců, "
    r"potvrzení serveru (?P<ms>\d+(?:\.\d+)?) ms(?P<slow> !)?\s*$"
)


def distribution(values):
    ordered = sorted(values)

    def percentile(percent):
        # Nearest rank: p95 is an observed value, even for small samples.
        return ordered[max(0, math.ceil(len(ordered) * percent / 100) - 1)]

    return {
        "samples": len(ordered),
        "min": ordered[0],
        "mean": statistics.mean(ordered),
        "p50": percentile(50),
        "p95": percentile(95),
        "p99": percentile(99),
        "max": ordered[-1],
    }


def summarize_ack(lines):
    batches = {}
    ignored = 0
    over_budget = 0
    for line in lines:
        match = ACK.search(line)
        if not match:
            ignored += 1
            continue
        decoder = match["decoder"]
        entry = batches.setdefault(decoder, {"frames": 0, "ack_ms": []})
        entry["frames"] += int(match["frames"])
        elapsed = float(match["ms"])
        entry["ack_ms"].append(elapsed)
        # Agent rounds milliseconds, but marks >200 ms before rounding.
        over_budget += bool(match["slow"]) or elapsed > 200
    if not batches:
        raise ValueError("Žádné potvrzené dávky v logu; latenci nelze spočítat.")
    values = [value for entry in batches.values() for value in entry["ack_ms"]]
    return {
        "measurement": "successful_batch_ack",
        "scope": "Successful batches only; not one-way latency or loss rate.",
        "percentiles": "nearest_rank_per_batch",
        "ignored_lines": ignored,
        "frames": sum(entry["frames"] for entry in batches.values()),
        "ack_ms": distribution(values),
        "batches_over_200_ms": over_budget,
        "by_decoder": {
            decoder: {"frames": entry["frames"], "ack_ms": distribution(entry["ack_ms"])}
            for decoder, entry in sorted(batches.items())
        },
    }


def queue_benchmark(args):
    source = Path(__file__).resolve().parents[1] / "agent" / "track_agent.py"
    spec = importlib.util.spec_from_file_location("ri5_benchmark_agent", source)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    if args.frames * 128 > agent.PRELIV_MAX_BAJTU:
        raise ValueError("Počet rámců překračuje kapacitu fronty pro rámce délky 128.")
    # Always a new temporary DB, optionally on the same volume as the agent.
    # Never open a live queue, contact Cloud, or restart any service.
    with (
        tempfile.TemporaryDirectory(prefix="ri5-benchmark-", dir=args.directory) as folder,
        agent.FrameQueue(Path(folder) / "queue.sqlite3") as queue,
    ):
        if queue.pridej(["A" * 128] * args.frames):
            raise ValueError("Zkušební rámce se nepodařilo uložit.")
        samples = {"count_scan": [], "maintained_count": []}

        def count_scan():
            with queue._lock:
                return queue.db.execute("SELECT COUNT(*) FROM frames").fetchone()[0]

        methods = [("count_scan", count_scan), ("maintained_count", queue.ceka)]
        for _, method in methods:
            assert method() == args.frames
        for repeat in range(args.rounds):
            # Alternate order to reduce systematic cache/temperature bias.
            for name, method in methods[::1 if repeat % 2 == 0 else -1]:
                started = time.perf_counter_ns()
                for _ in range(args.iterations):
                    method()
                samples[name].append(
                    (time.perf_counter_ns() - started) / args.iterations / 1000
                )
        medians = {name: statistics.median(values) for name, values in samples.items()}
    return {
        "measurement": "isolated_queue_count_microbenchmark",
        "scope": "Warm local SQLite reads; not end-to-end passing latency.",
        "agent_version": agent.VERSION,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "frames": args.frames,
        "frame_characters": 128,
        "rounds": args.rounds,
        "reads_per_round": args.iterations,
        "median_us_per_read": medians,
        "speedup": medians["count_scan"] / medians["maintained_count"],
    }


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Hodnota musí být kladné celé číslo.")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ack = commands.add_parser("ack", help="Summarize journalctl -o cat output.")
    ack.add_argument("log", nargs="?", default="-", help="UTF-8 log file or - for stdin")
    queue = commands.add_parser("queue", help="Benchmark a disposable local queue.")
    queue.add_argument("--directory", type=Path, help="Parent directory on the disk being measured")
    queue.add_argument("--frames", type=positive_int, default=60000)
    queue.add_argument("--rounds", type=positive_int, default=5)
    queue.add_argument("--iterations", type=positive_int, default=2000)
    args = parser.parse_args(argv)
    try:
        if args.command == "queue":
            result = queue_benchmark(args)
        elif args.log == "-":
            result = summarize_ack(sys.stdin)
        else:
            with Path(args.log).open(encoding="utf-8") as stream:
                result = summarize_ack(stream)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Chyba měření: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
