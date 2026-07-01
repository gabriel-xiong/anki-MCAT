# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""MCAT Speedrun — three separate scores (memory, performance, readiness).

The scores are NEVER blended. Each can abstain ("not enough data yet") with an
explicit reason, mirroring MCAT/data/scoring-config.json give_up thresholds and
the honesty rule (no readiness without range, coverage %, and a give-up rule).

Pure/computational module — no Qt. The deck-browser renderer turns the returned
dicts into HTML; tests drive these functions directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from anki.mcat_perf import (
    CARS_SECTION,
    MIN_CARDS_SEEN_FOR_PERFORMANCE,
    MIN_GOOD_OR_EASY_FOR_PERFORMANCE,
    PerfStore,
    unlocked_topic_ids,
)

if TYPE_CHECKING:
    from anki.collection import Collection

# ---------------------------------------------------------------------------
# Thresholds — mirror data/scoring-config.json give_up.
# ---------------------------------------------------------------------------
MIN_MEMORY_REVIEWS = 200
MIN_PERF_ATTEMPTS = 30
MIN_UNLOCKED_TOPICS = 1
MIN_COVERAGE_PCT = 50
MIN_ATTEMPTS_PER_SCIENCE_SECTION = 5
MIN_CARS_ATTEMPTS = 5

# Readiness band per section.
SECTION_MIN = 118
SECTION_MAX = 132
SCIENCE_SECTIONS = ("CP", "BB", "PS")

# ---------------------------------------------------------------------------
# Outline — coverage denominator. Mirrors data/mcat-outline.v1.json (v1-subset).
# Keep in sync if that file changes. (section_id, topic_id, name)
# ---------------------------------------------------------------------------
OUTLINE: list[tuple[str, str, str]] = [
    ("CP", "cp_electrochem", "Electrochemistry"),
    ("CP", "cp_acids_bases", "Acids, Bases, and Buffers"),
    ("CP", "cp_thermo", "Thermodynamics and Spontaneity"),
    ("CP", "cp_kinetics", "Chemical Kinetics"),
    ("CP", "cp_fluids", "Fluids and Circulation (physics)"),
    ("CARS", "cars_comprehension", "Foundations of Comprehension"),
    ("CARS", "cars_reasoning_within", "Reasoning Within the Text"),
    ("CARS", "cars_reasoning_beyond", "Reasoning Beyond the Text"),
    ("BB", "bb_glycolysis", "Glycolysis and Glucose Metabolism"),
    ("BB", "bb_citric_acid", "Citric Acid Cycle and Oxidative Phosphorylation"),
    ("BB", "bb_enzymes", "Enzyme Kinetics and Regulation"),
    ("BB", "bb_membranes", "Membrane Structure and Transport"),
    ("BB", "bb_dna", "DNA Structure and Replication"),
    ("BB", "bb_genetics", "Mendelian Genetics and Inheritance"),
    ("PS", "ps_memory", "Memory and Cognition"),
    ("PS", "ps_learning", "Learning and Conditioning"),
    ("PS", "ps_social", "Social Processes and Behavior"),
    ("PS", "ps_demographics", "Demographics and Health Disparities"),
]
OUTLINE_TOPIC_IDS = {t for _, t, _ in OUTLINE}
SECTION_OF_TOPIC = {t: s for s, t, _ in OUTLINE}
NAME_OF_TOPIC = {t: n for _, t, n in OUTLINE}
TOTAL_TOPICS = len(OUTLINE)

# Error type -> next-action template (mirrors scoring-config.json error_types).
NEXT_ACTION_BY_ERROR = {
    "content_gap": "Review memory cards in {topic}.",
    "passage_mapping": "Do passage-style questions in {topic}.",
    "reasoning": "Do harder reasoning questions (interleaved) in {topic}.",
    "misread": "Retry timed questions — no new content.",
}

# ---------------------------------------------------------------------------
# Focus area — top diagnosed weakness (v2 3-bucket taxonomy) -> one-click action.
# Mirrors docs/ERROR-DIAGNOSIS-SPEC.md "Next-action mapping". Distinct from the
# legacy NEXT_ACTION_BY_ERROR string above (kept for the frozen self-report).
# ---------------------------------------------------------------------------
# Fold the frozen Wednesday self-report enum into the v2 buckets so historical
# rows map cleanly (reasoning/passage_mapping -> application).
_FOCUS_FOLD = {"reasoning": "application", "passage_mapping": "application"}
FOCUS_BUCKETS = ("content_gap", "application", "misread")

# error_type -> (verb template, launch kind). {n}/{topic} filled at render.
FOCUS_ACTION = {
    "content_gap": {
        "label": "Review {n} flashcard{s} in {topic}",
        "kind": "review",  # opens filtered memory review for the topic
    },
    "application": {
        "label": "Practice {n} applied question{s} in {topic}",
        "kind": "performance",  # perf session filtered to application/synthesis
    },
    "misread": {
        "label": "Careful-reading / pacing drill",
        "kind": "pacing",
    },
}


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
def memory_summary(col: Collection) -> dict[str, Any]:
    total_reviews = int(col.db.scalar("select count() from revlog") or 0)
    mastery = list(col.get_topic_mastery())
    topics_studied = sum(1 for m in mastery if m.cards_seen > 0)
    unlocked = sum(1 for m in mastery if m.performance_unlocked)

    retr_vals = [
        m.avg_retrievability for m in mastery if m.avg_retrievability > 0
    ]
    avg_retr = sum(retr_vals) / len(retr_vals) if retr_vals else None

    abstain = total_reviews < MIN_MEMORY_REVIEWS
    return {
        "status": "abstain" if abstain else "ok",
        "total_reviews": total_reviews,
        "topics_studied": topics_studied,
        "topics_unlocked": unlocked,
        "avg_retrievability": avg_retr,
        "reason": (
            f"Needs ≥{MIN_MEMORY_REVIEWS} graded reviews "
            f"(have {total_reviews})."
            if abstain
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------
def performance_summary(col: Collection, store: PerfStore) -> dict[str, Any]:
    overall = store.accuracy()
    attempts = overall["attempts"]
    unlocked = unlocked_topic_ids(col)

    abstain_reasons = []
    if attempts < MIN_PERF_ATTEMPTS:
        abstain_reasons.append(
            f"needs ≥{MIN_PERF_ATTEMPTS} attempts (have {attempts})"
        )
    if len(unlocked) < MIN_UNLOCKED_TOPICS:
        abstain_reasons.append(
            f"needs ≥{MIN_UNLOCKED_TOPICS} unlocked topic"
        )

    return {
        "status": "abstain" if abstain_reasons else "ok",
        "attempts": attempts,
        "correct": overall["correct"],
        "accuracy": overall["accuracy"],
        "unlocked_topics": len(unlocked),
        "reason": "; ".join(abstain_reasons) or None,
    }


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------
def coverage_summary(col: Collection, store: PerfStore) -> dict[str, Any]:
    """Fraction of outline topics with measurement (cards seen or attempts)."""
    measured: set[str] = set()
    for m in col.get_topic_mastery():
        if m.cards_seen > 0 and m.topic_id in OUTLINE_TOPIC_IDS:
            measured.add(m.topic_id)
    for row in store.conn.execute(
        "SELECT DISTINCT q.topic_id FROM perf_attempts a "
        "JOIN perf_questions q ON q.id = a.question_id"
    ):
        if row["topic_id"] in OUTLINE_TOPIC_IDS:
            measured.add(row["topic_id"])

    pct = round(100 * len(measured) / TOTAL_TOPICS) if TOTAL_TOPICS else 0
    return {
        "measured": len(measured),
        "total": TOTAL_TOPICS,
        "pct": pct,
        "measured_topics": sorted(measured),
    }


# ---------------------------------------------------------------------------
# Readiness (abstains aggressively; honest range when eligible)
# ---------------------------------------------------------------------------
def _section_attempt_counts(store: PerfStore) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in store.conn.execute(
        "SELECT q.section section, COUNT(*) n FROM perf_attempts a "
        "JOIN perf_questions q ON q.id = a.question_id GROUP BY q.section"
    ):
        counts[row["section"]] = int(row["n"])
    return counts


def readiness_summary(col: Collection, store: PerfStore) -> dict[str, Any]:
    mem = memory_summary(col)
    perf = performance_summary(col, store)
    cov = coverage_summary(col, store)
    section_attempts = _section_attempt_counts(store)

    reasons: list[str] = []
    if mem["total_reviews"] < MIN_MEMORY_REVIEWS:
        reasons.append(f"reviews < {MIN_MEMORY_REVIEWS}")
    if perf["attempts"] < MIN_PERF_ATTEMPTS:
        reasons.append(f"attempts < {MIN_PERF_ATTEMPTS}")
    if cov["pct"] < MIN_COVERAGE_PCT:
        reasons.append(f"coverage < {MIN_COVERAGE_PCT}%")
    for sec in SCIENCE_SECTIONS:
        if section_attempts.get(sec, 0) < MIN_ATTEMPTS_PER_SCIENCE_SECTION:
            reasons.append(f"{sec} attempts < {MIN_ATTEMPTS_PER_SCIENCE_SECTION}")
    if section_attempts.get(CARS_SECTION, 0) < MIN_CARS_ATTEMPTS:
        reasons.append(f"CARS attempts < {MIN_CARS_ATTEMPTS}")

    if reasons:
        return {
            "status": "abstain",
            "coverage_pct": cov["pct"],
            "reason": "; ".join(reasons),
            "range": None,
        }

    # Eligible — provisional, low-confidence map (coefficients deferred).
    low, high = _provisional_range(store)
    return {
        "status": "ok",
        "coverage_pct": cov["pct"],
        "reason": None,
        "range": [low, high],
        "confidence": "low",
    }


def _provisional_range(store: PerfStore) -> tuple[int, int]:
    total = 0.0
    for sec in ("CP", "CARS", "BB", "PS"):
        row = store.conn.execute(
            "SELECT AVG(a.correct) acc FROM perf_attempts a "
            "JOIN perf_questions q ON q.id = a.question_id WHERE q.section = ?",
            (sec,),
        ).fetchone()
        acc = row["acc"] if row and row["acc"] is not None else 0.0
        total += SECTION_MIN + acc * (SECTION_MAX - SECTION_MIN)
    center = round(total)
    return (max(472, center - 6), min(528, center + 6))


# ---------------------------------------------------------------------------
# Next action (from most common error type)
# ---------------------------------------------------------------------------
def next_action(col: Collection, store: PerfStore) -> str:
    row = store.conn.execute(
        "SELECT error_type, COUNT(*) n FROM perf_attempts "
        "WHERE correct = 0 AND error_type IS NOT NULL "
        "GROUP BY error_type ORDER BY n DESC LIMIT 1"
    ).fetchone()
    if not row:
        unlocked = unlocked_topic_ids(col)
        if unlocked:
            return "Start a performance session on an unlocked topic."
        return (
            "Study memory cards until a topic unlocks "
            "(≥3 cards seen, ≥5 Good/Easy)."
        )

    error_type = row["error_type"]
    topic_row = store.conn.execute(
        "SELECT q.topic_id tid, COUNT(*) n FROM perf_attempts a "
        "JOIN perf_questions q ON q.id = a.question_id "
        "WHERE a.correct = 0 AND a.error_type = ? "
        "GROUP BY q.topic_id ORDER BY n DESC LIMIT 1",
        (error_type,),
    ).fetchone()
    topic_id = topic_row["tid"] if topic_row else None
    topic_name = NAME_OF_TOPIC.get(topic_id, topic_id or "your weakest topic")
    template = NEXT_ACTION_BY_ERROR.get(error_type, "Review {topic}.")
    return template.format(topic=topic_name)


# ---------------------------------------------------------------------------
# Focus area (top diagnosed weakness -> concrete one-click action)
# ---------------------------------------------------------------------------
def focus_area(col: Collection, store: PerfStore) -> dict[str, Any]:
    """The single highest-impact next action, mapped from the top diagnosed
    weakness (error_type × topic).

    Prefers the v2 inferred diagnosis (``inferred_error_type``) and falls back to
    the frozen self-report (``error_type``) so it works with Wednesday data too.
    ``unresolved`` / ``none`` / null are ignored (honest abstain). Returns a
    structured dict the dashboard renders as a prominent tile; ``status`` is
    ``ok`` or ``abstain``.
    """
    rows = store.conn.execute(
        "SELECT a.inferred_error_type AS itype, a.error_type AS etype, "
        "q.topic_id AS tid FROM perf_attempts a "
        "JOIN perf_questions q ON q.id = a.question_id "
        "WHERE a.correct = 0"
    ).fetchall()

    counts: dict[tuple[str, str], int] = {}
    for r in rows:
        # Prefer a decisive v2 inference; when it abstained (unresolved/none/
        # null) fall back to the weak self-report so the focus area still works.
        itype = r["itype"]
        if itype and itype not in ("unresolved", "none"):
            dtype = itype
        else:
            dtype = r["etype"]
        if not dtype:
            continue
        dtype = _FOCUS_FOLD.get(dtype, dtype)
        if dtype not in FOCUS_ACTION:  # skip anything not in the 3 buckets
            continue
        key = (dtype, r["tid"])
        counts[key] = counts.get(key, 0) + 1

    if not counts:
        return {
            "status": "abstain",
            "reason": "No diagnosed misses yet — answer performance questions "
            "to surface your focus area.",
            "action_label": next_action(col, store),
            "kind": None,
            "error_type": None,
            "topic_id": None,
        }

    # Top weakness: most-frequent (type, topic); ties broken deterministically.
    (dtype, topic_id), n = max(
        counts.items(), key=lambda kv: (kv[1], kv[0][0], kv[0][1])
    )
    topic_name = NAME_OF_TOPIC.get(topic_id, topic_id or "your weakest topic")
    spec = FOCUS_ACTION[dtype]
    label = spec["label"].format(
        n=n, s="" if n == 1 else "s", topic=topic_name
    )
    return {
        "status": "ok",
        "error_type": dtype,
        "topic_id": topic_id,
        "topic_name": topic_name,
        "count": n,
        "kind": spec["kind"],
        "action_label": label,
        # launch spec consumed by the dashboard link handler: "kind:topic_id"
        "launch": f"{spec['kind']}:{topic_id}",
        "reason": None,
    }


# ---------------------------------------------------------------------------
# Per-topic mastery view (surfaces the Rust mastery query directly)
# ---------------------------------------------------------------------------
def topic_rows(
    col: Collection, store: Optional[PerfStore] = None
) -> list[dict[str, Any]]:
    """Every outline topic joined with the Rust mastery query + performance.

    This is the exact data the unlock gate runs on, exposed for the UI so a
    user can see *why* a topic is (or isn't) unlocked. Topics with no cards
    still appear (all zeros), so coverage is honest.
    """
    own_store = store is None
    if store is None:
        store = PerfStore(col)
    try:
        mastery = {m.topic_id: m for m in col.get_topic_mastery()}
        rows: list[dict[str, Any]] = []
        for section, topic_id, name in OUTLINE:
            m = mastery.get(topic_id)
            cards_total = int(m.cards_total) if m else 0
            cards_seen = int(m.cards_seen) if m else 0
            good_or_easy = int(m.good_or_easy_count) if m else 0
            retr = float(m.avg_retrievability) if m else 0.0
            unlocked = bool(m.performance_unlocked) if m else False
            acc = store.accuracy(topic_id)
            rows.append(
                {
                    "section": section,
                    "topic_id": topic_id,
                    "name": name,
                    "cards_total": cards_total,
                    "cards_seen": cards_seen,
                    "good_or_easy": good_or_easy,
                    "avg_retrievability": retr,
                    "unlocked": unlocked,
                    "is_cars": section == CARS_SECTION,
                    "attempts": acc["attempts"],
                    "accuracy": acc["accuracy"],
                    # progress toward the two gate conditions
                    "need_seen": max(0, MIN_CARDS_SEEN_FOR_PERFORMANCE - cards_seen),
                    "need_good": max(
                        0, MIN_GOOD_OR_EASY_FOR_PERFORMANCE - good_or_easy
                    ),
                }
            )
        return rows
    finally:
        if own_store:
            store.close()


# ---------------------------------------------------------------------------
# Bundle for the dashboard
# ---------------------------------------------------------------------------
def dashboard_data(col: Collection, store: Optional[PerfStore] = None) -> dict[str, Any]:
    own_store = store is None
    if store is None:
        store = PerfStore(col)
    try:
        return {
            "memory": memory_summary(col),
            "performance": performance_summary(col, store),
            "readiness": readiness_summary(col, store),
            "coverage": coverage_summary(col, store),
            "next_action": next_action(col, store),
            "focus_area": focus_area(col, store),
        }
    finally:
        if own_store:
            store.close()
