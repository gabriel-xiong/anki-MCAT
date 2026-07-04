#!/usr/bin/env python3
# Copyright: MCAT Speedrun contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""MCAT Speedrun — basic latency / reliability numbers (scripted, reproducible).

Reviewer next-focus item #4: "Basic latency or reliability numbers." Measures
simple, honest latencies for:

  1. The modified Rust MASTERY QUERY  (col.get_topic_mastery / _one)  — the
     Speedrun Rust change.
  2. The performance-mode WRITE       (PerfStore.log_attempt → sidecar INSERT+commit).
  3. The performance-mode ROUND-TRIP  (log_attempt + accuracy() recompute).
  4. The perf-data SYNC round-trip    (export_bundle + import_bundle into a fresh
     device) — the project's cross-device perf sync channel (DECISIONS.md §22).

Numbers are small-n and taken on dev hardware with a synthetic deck; they are a
sanity/latency floor, NOT a scale benchmark. (Scale to 50k cards is tracked
separately in MASTERY-QUERY-SPEC.md via `make bench`.)

Run
---
    anki-MCAT/out/pyenv/Scripts/python.exe tools/mcat_latency_bench.py
Optional: --cards N --topics T --iters N --out PATH
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from typing import Any, Callable

# --- bootstrap the merged `anki` namespace package (see undo-integrity note) ---
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)  # anki-MCAT
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]
for _p in (os.path.join(_ROOT, "out", "pylib"), os.path.join(_ROOT, "pylib")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

from tests.shared import getEmptyCol  # noqa: E402

import anki.mcat_perf as mp  # noqa: E402

TOPICS = [
    "cp_electrochem", "cp_acids_bases", "cp_thermo", "cp_kinetics", "cp_fluids",
    "bb_glycolysis", "bb_citric_acid", "bb_enzymes", "bb_membranes", "bb_dna",
    "bb_genetics", "ps_memory", "ps_learning", "ps_social", "ps_demographics",
    "cars_comprehension", "cars_reasoning_within", "cars_reasoning_beyond",
]


def time_calls(fn: Callable[[int], Any], iters: int, warmup: int = 5) -> dict[str, float]:
    for i in range(warmup):
        fn(i)
    samples: list[float] = []
    for i in range(iters):
        t = time.perf_counter()
        fn(i)
        samples.append((time.perf_counter() - t) * 1000.0)  # ms
    samples.sort()
    return {
        "n": iters,
        "min_ms": round(samples[0], 4),
        "median_ms": round(statistics.median(samples), 4),
        "p95_ms": round(samples[min(len(samples) - 1, int(0.95 * len(samples)))], 4),
        "max_ms": round(samples[-1], 4),
        "mean_ms": round(statistics.fmean(samples), 4),
    }


def build_deck(col, topics: int, cards: int) -> dict[str, int]:
    # Raise the new-card daily limit so we can actually review a chunk of cards.
    try:
        conf = col.decks.config_dict_for_deck_id(1)
        conf["new"]["perDay"] = 99999
        conf["rev"]["perDay"] = 99999
        col.decks.save(conf)
    except Exception:
        pass

    topic_list = TOPICS[:topics] if topics <= len(TOPICS) else TOPICS
    per_topic = max(1, cards // len(topic_list))
    made = 0
    for t in topic_list:
        for i in range(per_topic):
            note = col.newNote()
            note["Front"] = f"{t} q{i}"
            note["Back"] = f"{t} a{i}"
            note.tags = [f"topic:{t}"]
            col.addNote(note)
            made += 1

    # Review as many as the scheduler will hand us (gives revlog rows so the
    # mastery query's per-card revlog lookups do real work).
    reviewed = 0
    limit = made * 2
    while reviewed < limit:
        c = col.sched.getCard()
        if c is None:
            break
        col.sched.answerCard(c, 3)  # Good
        reviewed += 1
    return {"cards": made, "reviews": reviewed, "topics": len(topic_list)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards", type=int, default=200)
    ap.add_argument("--topics", type=int, default=18)
    ap.add_argument("--iters", type=int, default=200)
    ap.add_argument("--sync-iters", type=int, default=25)
    ap.add_argument(
        "--out",
        default=os.path.normpath(
            os.path.join(_ROOT, "..", "MCAT", "docs", "artifacts", "latency-results.json")
        ),
    )
    args = ap.parse_args()

    print("MCAT latency / reliability bench (small-n, dev hardware)")
    print("=" * 64)

    col = getEmptyCol()
    deck = build_deck(col, args.topics, args.cards)
    print(f"deck: {deck['cards']} cards / {deck['topics']} topics / {deck['reviews']} reviews")

    store = mp.PerfStore(col)
    questions_json = os.path.normpath(os.path.join(_ROOT, "..", "MCAT", "data", "questions.json"))
    if os.path.exists(questions_json):
        store.load_questions(questions_json)
    if store.question_count() == 0:
        store.upsert_questions(
            [
                {
                    "id": f"q_syn_{i:03d}", "stem": f"s{i}", "choices": ["A", "B", "C", "D"],
                    "correct": "A", "topic_id": "bb_enzymes", "section": "BB",
                    "source_name": "synthetic", "explanation": "x", "split": "dev",
                }
                for i in range(40)
            ]
        )
    qids = [r["id"] for r in store.conn.execute("SELECT id FROM perf_questions ORDER BY id").fetchall()]

    results: dict[str, Any] = {}

    # 1. mastery query (all topics) — the modified Rust engine call.
    results["mastery_query_all"] = time_calls(lambda i: col.get_topic_mastery(), args.iters)
    # 1b. mastery query (single topic).
    topic0 = TOPICS[0]
    results["mastery_query_one"] = time_calls(lambda i: col.get_topic_mastery_one(topic0), args.iters)

    # 2. perf write (sidecar INSERT + commit).
    results["perf_attempt_write"] = time_calls(
        lambda i: store.log_attempt(qids[i % len(qids)], i % 2 == 0, chosen_index=i % 4, time_seconds=12.0),
        args.iters,
    )

    # 3. perf round-trip (write + accuracy recompute).
    def _round_trip(i: int) -> Any:
        store.log_attempt(qids[i % len(qids)], i % 2 == 0, chosen_index=i % 4, time_seconds=9.0)
        return store.accuracy()

    results["perf_round_trip"] = time_calls(_round_trip, args.iters)
    sidecar = store.path
    store.close()

    # 4. perf-data sync round-trip: export the bundle file + import/merge it into
    #    a second device's sidecar. Times only the file round-trip + union merge
    #    (the destination sidecar is created once, so collection-open cost is
    #    excluded). Re-imports are idempotent but still parse+dedup every row.
    import tempfile

    _sync_dir = tempfile.mkdtemp(prefix="mcat_bench_sync_")
    _bundle = os.path.join(_sync_dir, "bundle.json")
    _dest_db = os.path.join(_sync_dir, "dest.mcat_perf.db")

    def _sync(i: int) -> Any:
        mp.export_bundle(sidecar, _bundle)
        return mp.import_bundle(_dest_db, _bundle)

    results["perf_sync_round_trip"] = time_calls(_sync, args.sync_iters, warmup=2)

    col.close()

    env = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "generated_at": int(time.time()),
        "deck": deck,
        "note": "small-n, dev hardware, synthetic deck; NOT a 50k-scale bench",
    }
    payload = {"env": env, "results": results}

    # Pretty table
    print()
    hdr = f"{'measurement':28} {'n':>5} {'min':>9} {'median':>9} {'p95':>9} {'max':>9}"
    print(hdr)
    print("-" * len(hdr))
    labels = {
        "mastery_query_all": "mastery query (all topics)",
        "mastery_query_one": "mastery query (one topic)",
        "perf_attempt_write": "perf attempt write",
        "perf_round_trip": "perf write+score round-trip",
        "perf_sync_round_trip": "perf-data sync round-trip",
    }
    for key, label in labels.items():
        r = results[key]
        print(
            f"{label:28} {r['n']:>5} {r['min_ms']:>8.3f}m {r['median_ms']:>8.3f}m "
            f"{r['p95_ms']:>8.3f}m {r['max_ms']:>8.3f}m"
        )
    print("\n(all values in milliseconds; 'm' = ms)")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"\nsummary -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
