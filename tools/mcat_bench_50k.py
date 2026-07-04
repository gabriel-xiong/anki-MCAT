#!/usr/bin/env python3
# Copyright: MCAT Speedrun contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""MCAT Speedrun — 50k-scale benchmark (`make bench`).

Reviewer risk "RS" / evidence items E-1 & D-7: the dashboard-latency and
"scales to a real collection" claims were previously **unproven**. The small-n
sanity bench (`tools/mcat_latency_bench.py`, ~200 cards) is a latency *floor*,
not a scale test. This harness seeds a ~50k-scale collection and measures the
things a user actually waits on:

  1. The modified Rust MASTERY QUERY — all topics (`col.get_topic_mastery`).
  2. The mastery query — single topic (`col.get_topic_mastery_one`).
  3. The end-to-end 3-SCORE DASHBOARD (`anki.mcat_scores.dashboard_data`), which
     is the real user-facing claim: it calls the mastery query ~4x plus the
     perf-store queries (memory / performance / readiness / coverage / focus).

Why a Python timing harness (not a Rust criterion bench): the "dashboard latency
at scale" claim lives in the Python/Qt path (`mcat_scores.dashboard_data`), and
the harness runs against the ALREADY-BUILT fork backend (`out/pylib`) so it needs
no fresh Rust rebuild (avoids contention with the long native build).

Reports p50 (median) / p95 / max over N iterations, plus the seed cost.

Run
---
    anki-MCAT/out/pyenv/Scripts/python.exe tools/mcat_bench_50k.py
Options: --cards N --topics T --reviews R --iters N --dash-iters N --out PATH
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

# --- bootstrap the merged `anki` namespace package (mirrors mcat_latency_bench) ---
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)  # anki-MCAT
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]
for _p in (os.path.join(_ROOT, "out", "pylib"), os.path.join(_ROOT, "pylib")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

from tests.shared import getEmptyCol  # noqa: E402

import anki.mcat_perf as mp  # noqa: E402
import anki.mcat_scores as ms  # noqa: E402

# Full 18-topic v1 outline (matches mcat_scores.OUTLINE / latency bench).
TOPICS = [
    "cp_electrochem", "cp_acids_bases", "cp_thermo", "cp_kinetics", "cp_fluids",
    "bb_glycolysis", "bb_citric_acid", "bb_enzymes", "bb_membranes", "bb_dna",
    "bb_genetics", "ps_memory", "ps_learning", "ps_social", "ps_demographics",
    "cars_comprehension", "cars_reasoning_within", "cars_reasoning_beyond",
]


def summarize(samples: list[float]) -> dict[str, float]:
    samples = sorted(samples)
    n = len(samples)
    return {
        "n": n,
        "min_ms": round(samples[0], 4),
        "median_ms": round(statistics.median(samples), 4),  # p50
        "p95_ms": round(samples[min(n - 1, int(0.95 * n))], 4),
        "max_ms": round(samples[-1], 4),
        "mean_ms": round(statistics.fmean(samples), 4),
    }


def time_calls(fn: Callable[[int], Any], iters: int, warmup: int = 3) -> dict[str, float]:
    for i in range(warmup):
        fn(i)
    samples: list[float] = []
    for i in range(iters):
        t = time.perf_counter()
        fn(i)
        samples.append((time.perf_counter() - t) * 1000.0)  # ms
    return summarize(samples)


def build_deck(col, topics: int, cards: int, reviews: int) -> dict[str, Any]:
    """Seed a ~50k-scale collection.

    - `cards` notes/cards spread evenly across `topics` (tag `topic:{id}`); the
      mastery query iterates ALL of them + does a per-card revlog lookup, so the
      total card count is what drives the O(n) cost regardless of how many were
      reviewed.
    - `reviews` cards are actually graded (Good) so a realistic fraction carries
      revlog rows + FSRS memory_state (exercises card_retrievability too).
    """
    topic_list = TOPICS[:topics] if topics <= len(TOPICS) else TOPICS
    per_topic = max(1, cards // len(topic_list))

    t0 = time.perf_counter()
    made = 0
    for t in topic_list:
        tag = f"topic:{t}"
        for i in range(per_topic):
            note = col.newNote()
            note["Front"] = f"{t} q{i}"
            note["Back"] = f"{t} a{i}"
            note.tags = [tag]
            col.addNote(note)
            made += 1
    seed_add_s = time.perf_counter() - t0

    # The v3 scheduler defaults to 20 new cards/day; lift today's limit so we can
    # grade a realistic fraction of the deck (custom-study "increase new" path).
    target = min(reviews, made)
    try:
        col.sched.extend_limits(target + 100, 0)
    except Exception:
        pass

    t1 = time.perf_counter()
    reviewed = 0
    while reviewed < target:
        c = col.sched.getCard()
        if c is None:
            break
        col.sched.answerCard(c, 3)  # Good
        reviewed += 1
    seed_review_s = time.perf_counter() - t1

    return {
        "cards": made,
        "reviews": reviewed,
        "topics": len(topic_list),
        "seed_add_s": round(seed_add_s, 2),
        "seed_review_s": round(seed_review_s, 2),
    }


def setup_perf_store(col) -> mp.PerfStore:
    store = mp.PerfStore(col)
    questions_json = os.path.normpath(
        os.path.join(_ROOT, "..", "MCAT", "data", "questions.json")
    )
    if os.path.exists(questions_json):
        try:
            store.load_questions(questions_json)
        except Exception:
            pass
    if store.question_count() == 0:
        store.upsert_questions(
            [
                {
                    "id": f"q_syn_{i:03d}", "stem": f"s{i}", "choices": ["A", "B", "C", "D"],
                    "correct": "A", "topic_id": TOPICS[i % len(TOPICS)], "section": "BB",
                    "source_name": "synthetic", "explanation": "x", "split": "dev",
                }
                for i in range(60)
            ]
        )
    # Seed some attempts so the performance/readiness scores do real work rather
    # than instantly abstaining.
    qids = [
        r["id"]
        for r in store.conn.execute("SELECT id FROM perf_questions ORDER BY id").fetchall()
    ]
    for i in range(min(120, len(qids) * 4)):
        store.log_attempt(
            qids[i % len(qids)], i % 3 != 0, chosen_index=i % 4, time_seconds=11.0
        )
    return store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards", type=int, default=50000, help="~50k target card count")
    ap.add_argument("--topics", type=int, default=18)
    ap.add_argument("--reviews", type=int, default=5000, help="cards actually graded")
    ap.add_argument("--iters", type=int, default=30, help="mastery-query samples")
    ap.add_argument("--dash-iters", type=int, default=20, help="dashboard samples")
    ap.add_argument(
        "--out",
        default=os.path.normpath(
            os.path.join(_ROOT, "..", "MCAT", "docs", "artifacts", "bench-50k-results.json")
        ),
    )
    args = ap.parse_args()

    print("MCAT 50k-scale bench (dashboard latency at scale)")
    print("=" * 64)
    print(f"seeding ~{args.cards} cards across {args.topics} topics "
          f"({args.reviews} graded)...", flush=True)

    col = getEmptyCol()
    deck = build_deck(col, args.topics, args.cards, args.reviews)
    print(
        f"deck: {deck['cards']} cards / {deck['topics']} topics / "
        f"{deck['reviews']} reviews  (add {deck['seed_add_s']}s, "
        f"review {deck['seed_review_s']}s)",
        flush=True,
    )

    store = setup_perf_store(col)
    revlog_rows = int(col.db.scalar("select count() from revlog") or 0)

    results: dict[str, Any] = {}

    print("timing mastery query (all topics)...", flush=True)
    results["mastery_query_all"] = time_calls(lambda i: col.get_topic_mastery(), args.iters)

    print("timing mastery query (one topic)...", flush=True)
    topic0 = TOPICS[0]
    results["mastery_query_one"] = time_calls(
        lambda i: col.get_topic_mastery_one(topic0), args.iters
    )

    print("timing 3-score dashboard (dashboard_data)...", flush=True)
    results["dashboard_data"] = time_calls(
        lambda i: ms.dashboard_data(col, store), args.dash_iters
    )

    store.close()
    col.close()

    env = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "generated_at": int(time.time()),
        "deck": deck,
        "revlog_rows": revlog_rows,
        "note": (
            "synthetic ~50k-scale deck; single dev machine, no isolation; "
            "prebuilt fork backend (no fresh Rust rebuild). Scale sanity test."
        ),
    }
    payload = {"env": env, "results": results}

    print()
    hdr = f"{'measurement':28} {'n':>4} {'min':>9} {'p50':>9} {'p95':>9} {'max':>9}"
    print(hdr)
    print("-" * len(hdr))
    labels = {
        "mastery_query_all": "mastery query (all topics)",
        "mastery_query_one": "mastery query (one topic)",
        "dashboard_data": "3-score dashboard (end-to-end)",
    }
    for key, label in labels.items():
        r = results[key]
        print(
            f"{label:28} {r['n']:>4} {r['min_ms']:>8.3f}m {r['median_ms']:>8.3f}m "
            f"{r['p95_ms']:>8.3f}m {r['max_ms']:>8.3f}m"
        )
    print("\n(all values in milliseconds; 'm' = ms; p50 = median)")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"\nsummary -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
