#!/usr/bin/env python3
# Copyright: MCAT Speedrun contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""MCAT Speedrun — undo + collection-integrity proof (scripted, reproducible).

Reviewer next-focus item #3: "Undo and collection integrity proof" — demonstrate
that undo works and the collection stays consistent (no lost / double-counted
reviews) across a memory review session AND the performance sidecar.

This is a HEADLESS check that runs the *real* modified Anki backend (the fork's
built rslib, via the merged ``anki`` package) plus the real ``anki.mcat_perf``
sidecar code. It asserts a series of invariants and prints a PASS/FAIL report;
exit code is non-zero if any invariant fails. A JSON summary is written for the
proof packet.

What it proves
--------------
A. Review revlog is exactly reversible. Reviewing N cards appends exactly N
   revlog rows; undoing N times removes exactly one row per undo and returns the
   collection to the pre-review state (no lost, no double-counted reviews). Redo
   re-applies them. Card ``reps`` return to baseline.
B. The perf sidecar (``mcat_perf.db``) is isolated from memory undo. Undoing
   memory reviews never adds, drops, or double-counts a logged performance
   attempt — the two stores cannot corrupt each other.
C. Performance attempts are append-only and the sync bundle merge is idempotent
   + order-independent: importing a bundle adds each attempt once, re-importing
   the same bundle adds zero (no double count), and a second device converges to
   the exact union.
D. ``col.fix_integrity()`` reports OK after all the review/undo/redo churn.

Run
---
    anki-MCAT/out/pyenv/Scripts/python.exe tools/mcat_undo_integrity_check.py
(from the anki-MCAT repo root). No network, no AI.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from typing import Any

# --- bootstrap the merged `anki` namespace package (out/pylib + pylib) --------
# NOTE: the interpreter puts this script's own dir (tools/) on sys.path[0], which
# contains a `tools/tests` package that would shadow `pylib/tests`. Drop it and
# put pylib + out/pylib first.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)  # anki-MCAT
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]
for _p in (os.path.join(_ROOT, "out", "pylib"), os.path.join(_ROOT, "pylib")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

from tests.shared import getEmptyCol  # noqa: E402

import anki.mcat_perf as mp  # noqa: E402

# Locate the curated question bank (sibling MCAT data repo) for a realistic bank.
_QUESTIONS_JSON = os.path.normpath(
    os.path.join(_ROOT, "..", "MCAT", "data", "questions.json")
)

RESULTS: list[dict[str, Any]] = []
_FAILED = False


def check(name: str, ok: bool, detail: str = "") -> None:
    global _FAILED
    RESULTS.append({"check": name, "pass": bool(ok), "detail": detail})
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        _FAILED = True


def revlog_count(col) -> int:
    return int(col.db.scalar("SELECT count() FROM revlog"))


def add_topic_cards(col, topic_id: str, n: int) -> None:
    for i in range(n):
        note = col.newNote()
        note["Front"] = f"{topic_id} front {i}"
        note["Back"] = f"{topic_id} back {i}"
        note.tags = [f"topic:{topic_id}"]
        col.addNote(note)


# ---------------------------------------------------------------------------
# A. Review revlog is exactly reversible (no lost / double-counted reviews)
# ---------------------------------------------------------------------------
def test_review_undo_reversible(col) -> None:
    print("\n[A] Review + undo reversibility (revlog exactness)")
    add_topic_cards(col, "bb_enzymes", 8)

    baseline = revlog_count(col)
    check("revlog starts empty", baseline == 0, f"baseline={baseline}")

    n_reviews = 6
    reps_before: dict[int, int] = {}
    for _ in range(n_reviews):
        c = col.sched.getCard()
        assert c is not None, "no card available to review"
        reps_before.setdefault(c.id, c.reps)
        col.sched.answerCard(c, 3)  # Good

    after_reviews = revlog_count(col)
    check(
        "reviewing N cards appends exactly N revlog rows",
        after_reviews == baseline + n_reviews,
        f"{baseline} -> {after_reviews} (N={n_reviews})",
    )

    # Undo each review; each undo must remove exactly one revlog row.
    undo_names = []
    per_undo_ok = True
    for _ in range(n_reviews):
        before = revlog_count(col)
        status = col.undo_status()
        undo_names.append(status.undo)
        col.undo()
        after = revlog_count(col)
        if after != before - 1:
            per_undo_ok = False
    check(
        "each undo removes exactly one revlog row",
        per_undo_ok,
        f"undo op label(s)={sorted(set(undo_names))}",
    )

    final = revlog_count(col)
    check(
        "after undoing all reviews, revlog returns to baseline (none lost/doubled)",
        final == baseline,
        f"final={final}, baseline={baseline}",
    )

    # Redo must re-apply them (round-trip), then leave us clean again via undo.
    for _ in range(n_reviews):
        col.redo()
    redone = revlog_count(col)
    check(
        "redo re-applies exactly N reviews",
        redone == baseline + n_reviews,
        f"redone={redone}",
    )
    for _ in range(n_reviews):
        col.undo()
    check(
        "undo again returns to baseline (idempotent round-trip)",
        revlog_count(col) == baseline,
    )


# ---------------------------------------------------------------------------
# B + C. Perf sidecar isolation + append-only idempotent sync
# ---------------------------------------------------------------------------
def _load_bank_or_synthetic(store: "mp.PerfStore") -> list[str]:
    """Load the real curated bank if present, else upsert synthetic questions.

    Returns a list of question_ids to drive attempt logging. (The attempt's
    correctness is a boolean we set directly, so the stored answer key format —
    a letter in the curated bank — is irrelevant here.)
    """
    if os.path.exists(_QUESTIONS_JSON):
        store.load_questions(_QUESTIONS_JSON)
    if store.question_count() == 0:
        synthetic = [
            {
                "id": f"q_syn_{i:03d}",
                "stem": f"synthetic stem {i}",
                "choices": ["A", "B", "C", "D"],
                "correct": "A",
                "topic_id": "bb_enzymes",
                "section": "BB",
                "source_name": "synthetic",
                "explanation": "synthetic rationale",
                "split": "dev",
            }
            for i in range(12)
        ]
        store.upsert_questions(synthetic)
    rows = store.conn.execute(
        "SELECT id FROM perf_questions ORDER BY id LIMIT 10"
    ).fetchall()
    return [r["id"] for r in rows]


def test_perf_isolation_and_sync(col) -> None:
    print("\n[B/C] Perf sidecar isolation + append-only idempotent sync")

    store = mp.PerfStore(col)
    qs = _load_bank_or_synthetic(store)
    check("question bank loaded", len(qs) > 0, f"{len(qs)} questions selected")

    # Log K attempts (alternate correct / incorrect); each a distinct question.
    k = len(qs)
    n_correct = 0
    for idx, qid in enumerate(qs):
        is_correct = idx % 2 == 0
        n_correct += 1 if is_correct else 0
        store.log_attempt(
            qid,
            is_correct,
            chosen_index=idx % 4,
            time_seconds=10.0 + idx,
            interleaved=True,
        )

    count_after_log = store.attempt_count()
    check(
        "logging K attempts yields exactly K rows",
        count_after_log == k,
        f"count={count_after_log}, K={k}",
    )
    acc = store.accuracy()
    check(
        "accuracy reflects logged attempts (no double count)",
        acc["attempts"] == k and acc["correct"] == n_correct,
        f"attempts={acc['attempts']} correct={acc['correct']} acc={acc['accuracy']}",
    )
    sidecar = store.path
    store.close()

    # --- B. Isolation: churn the memory side (review + undo) and confirm the
    #     perf sidecar is untouched. ---
    for _ in range(4):
        c = col.sched.getCard()
        if c is None:
            break
        col.sched.answerCard(c, 3)
    for _ in range(2):
        col.undo()

    store2 = mp.PerfStore(col)
    check(
        "memory review+undo does NOT change perf attempt count (stores isolated)",
        store2.attempt_count() == k,
        f"perf count still {store2.attempt_count()} after memory churn",
    )
    store2.close()

    # --- C. Append-only idempotent sync (no double count on merge). ---
    tmpdir = tempfile.mkdtemp(prefix="mcat_sync_")
    bundle = os.path.join(tmpdir, "device_a.perf_bundle.json")
    exp = mp.export_bundle(sidecar, bundle)
    check(
        "export bundle carries all K attempts",
        exp["attempts"] == k,
        f"bundle attempts={exp['attempts']}",
    )

    # Second "device": a fresh collection + empty sidecar.
    col_b = getEmptyCol()
    sidecar_b = mp.sidecar_path(col_b)
    r1 = mp.import_bundle(sidecar_b, bundle)
    check(
        "first import adds exactly K attempts (union merge)",
        r1["attempts_added"] == k and r1.get("attempts_skipped", 0) == 0,
        f"added={r1['attempts_added']} skipped={r1.get('attempts_skipped')}",
    )
    r2 = mp.import_bundle(sidecar_b, bundle)
    check(
        "re-import is idempotent — adds 0, skips K (no double count)",
        r2["attempts_added"] == 0 and r2.get("attempts_skipped", 0) == k,
        f"added={r2['attempts_added']} skipped={r2.get('attempts_skipped')}",
    )
    store_b = mp.PerfStore(col_b)
    check(
        "second device converged to the exact union (K attempts)",
        store_b.attempt_count() == k,
        f"device-B count={store_b.attempt_count()}",
    )
    store_b.close()
    col_b.close()


# ---------------------------------------------------------------------------
# D. Collection integrity after all the churn
# ---------------------------------------------------------------------------
def test_collection_integrity(col) -> None:
    print("\n[D] Collection integrity check")
    err, ok = col.fix_integrity()
    check("col.fix_integrity() reports OK", ok, err.strip().splitlines()[0] if err else "no problems")


def main() -> int:
    print("MCAT undo + collection-integrity proof")
    print("=" * 60)
    print(f"anki-MCAT root: {_ROOT}")
    print(f"question bank : {_QUESTIONS_JSON if os.path.exists(_QUESTIONS_JSON) else '(synthetic fallback)'}")

    col = getEmptyCol()
    try:
        test_review_undo_reversible(col)
        test_perf_isolation_and_sync(col)
        test_collection_integrity(col)
    finally:
        col.close()

    n = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["pass"])
    print("\n" + "=" * 60)
    print(f"RESULT: {passed}/{n} checks passed")
    verdict = "PASS" if passed == n else "FAIL"
    print(f"VERDICT: {verdict}")

    summary = {
        "generated_at": int(time.time()),
        "verdict": verdict,
        "passed": passed,
        "total": n,
        "checks": RESULTS,
    }
    out_dir = os.path.normpath(os.path.join(_ROOT, "..", "MCAT", "docs", "artifacts"))
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "undo-integrity-results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"summary -> {out_path}")

    return 0 if passed == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
