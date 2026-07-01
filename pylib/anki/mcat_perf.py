# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""MCAT Speedrun — performance-mode storage (local sidecar DB).

Performance questions and attempts live in a sidecar SQLite file next to the
collection (``<collection>.mcat_perf.db``), NOT inside ``collection.anki2``.
Rationale: avoids Anki's Check Database / full-sync wiping unknown tables, and
gives us clean schema ownership. See MCAT/docs/DECISIONS.md §5.

No external/cloud database and no AI at runtime — this is local-first, per the
locked product decisions.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from anki.collection import Collection

# Choice letters for MCQs (v1 = 4 options, but tolerate more).
LETTERS = "ABCDEFGH"

# Mirrors data/scoring-config.json performance_eligibility.
MIN_CARDS_SEEN_FOR_PERFORMANCE = 3
MIN_GOOD_OR_EASY_FOR_PERFORMANCE = 5

DEV_SPLIT = "dev"
HELD_OUT_SPLIT = "held_out"
CARS_SECTION = "CARS"

# Mirrors data/scoring-config.json error_types (4 self-report types).
# FROZEN Wednesday self-report enum — kept intact for the shipped dialog. The v2
# science taxonomy (below) is a 3-bucket inference layer added ADDITIVELY on top;
# it does not replace these buttons. See MCAT/docs/ERROR-DIAGNOSIS-SPEC.md.
ERROR_TYPES = [
    ("content_gap", "Content gap"),
    ("passage_mapping", "Passage / mapping"),
    ("reasoning", "Reasoning / calculation"),
    ("misread", "Misread / careless"),
]

# ---------------------------------------------------------------------------
# v2 error-diagnosis inference (science 3-bucket taxonomy).
# Additive: the engine INFERS a hypothesis + confidence; self-report still runs.
# See MCAT/docs/ERROR-DIAGNOSIS-SPEC.md ("Inference rule").
# ---------------------------------------------------------------------------
ERR_NONE = "none"          # correct answer
ERR_CONTENT_GAP = "content_gap"
ERR_APPLICATION = "application"   # "Applied reasoning" (renamed from reasoning)
ERR_MISREAD = "misread"
ERR_UNRESOLVED = "unresolved"     # abstain → fall back to self-report

# Science inference buckets (excludes none/unresolved).
INFERRED_SCIENCE_TYPES = (ERR_CONTENT_GAP, ERR_APPLICATION, ERR_MISREAD)

# Legacy self-report → v2 bucket fold (for reading historical rows; see spec
# "Migration from the Wednesday 4-button enum"). Applied only when reconciling.
LEGACY_ERROR_FOLD = {
    "reasoning": ERR_APPLICATION,
    "passage_mapping": ERR_APPLICATION,
}

# Tunable params (conservative absolute fallbacks until per-item baselines
# exist; documented as tunable in docs/LOOSE-ENDS.md).
HIGH_M = 0.7          # content clearly held
LOW_M = 0.4           # content clearly missing
UNCOVERED_R0 = 0.5    # prior for a backing topic with no retrievability signal
FAST_THRESHOLD_SECONDS = 8.0
# demand factor d in the affinity formula (recall≈0 → no application signal).
DEMAND_FACTOR = {"recall": 0.0, "application": 0.7, "synthesis": 1.0}

# FSRS forgetting-curve constants for the pure-Python per-card retrievability
# computation. Mirrors rslib (FSRS5_DEFAULT_DECAY) so we get the same R the Rust
# mastery query would, without a backend/proto round-trip.
FSRS_DEFAULT_DECAY = 0.5  # FSRS-4.5/5 default abs decay (== rslib FSRS5_DEFAULT_DECAY)
SECONDS_PER_DAY = 86400.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS perf_questions (
  id TEXT PRIMARY KEY,
  stem TEXT NOT NULL,
  choices_json TEXT NOT NULL,
  correct TEXT NOT NULL,
  topic_id TEXT NOT NULL,
  section TEXT NOT NULL,
  skill TEXT,
  source_name TEXT NOT NULL,
  source_url TEXT,
  source_location TEXT,
  split TEXT NOT NULL CHECK (split IN ('dev', 'held_out'))
);
-- cognitive_demand added via _migrate() (ALTER TABLE) so existing sidecar DBs
-- upgrade cleanly; see below.

CREATE TABLE IF NOT EXISTS perf_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question_id TEXT NOT NULL,
  correct INTEGER NOT NULL,
  error_type TEXT,
  time_seconds REAL,
  interleaved INTEGER NOT NULL DEFAULT 0,
  ts INTEGER NOT NULL,
  FOREIGN KEY (question_id) REFERENCES perf_questions(id)
);

CREATE INDEX IF NOT EXISTS idx_perf_questions_topic ON perf_questions(topic_id);
CREATE INDEX IF NOT EXISTS idx_perf_attempts_qid ON perf_attempts(question_id);
"""

# Additive columns applied on top of _SCHEMA via ALTER TABLE (idempotent). These
# guard cleanly against older sidecar DBs that predate the v2 error-diagnosis
# layer. ONLY the v1-essential columns from the spec are migrated here; the
# deferred columns (first_choice_index, answer_changes, feature_json,
# recheck_timing) wait for the select-then-confirm UI + ML work.
_MIGRATIONS: dict[str, dict[str, str]] = {
    "perf_questions": {
        "cognitive_demand": "TEXT",  # recall | application | synthesis (science)
    },
    "perf_attempts": {
        "chosen_index": "INTEGER",         # option finally submitted (0–3)
        "mastery_snapshot": "REAL",        # content-held score M at attempt time
        "inferred_error_type": "TEXT",     # v2 inference hypothesis
        "inferred_confidence": "REAL",     # p_top of the inferred type
        "recheck_card_id": "INTEGER",      # backing card probed (re-check; deferred UI)
        "recheck_correct": "INTEGER",      # objective probe outcome (deferred UI)
        "error_source": "TEXT",            # inferred_confirmed | self_report_override
                                           #  | self_report | unresolved
    },
}


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any missing additive columns. Safe to run on every open."""
    for table, columns in _MIGRATIONS.items():
        have = _existing_columns(conn, table)
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def sidecar_path(col: Collection) -> str:
    """Path to the sidecar DB derived from the collection path.

    ``/path/collection.anki2`` -> ``/path/collection.mcat_perf.db``
    """
    base = os.path.splitext(col.path)[0]
    return base + ".mcat_perf.db"


def unlocked_topic_ids(col: Collection) -> set[str]:
    """Topics where the Rust mastery query reports performance_unlocked."""
    return {
        m.topic_id for m in col.get_topic_mastery() if m.performance_unlocked
    }


class PerfStore:
    """Thin wrapper over the sidecar SQLite DB."""

    def __init__(self, col: Collection) -> None:
        self.col = col
        self.path = sidecar_path(col)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        _migrate(self.conn)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> PerfStore:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # Loading ---------------------------------------------------------------

    def load_questions(self, path: str) -> int:
        """Idempotent upsert of questions from a questions.json file.

        Returns the number of rows written.
        """
        with open(path, encoding="utf-8") as f:
            questions = json.load(f)
        return self.upsert_questions(questions)

    def upsert_questions(self, questions: list[dict[str, Any]]) -> int:
        rows = []
        for q in questions:
            rows.append(
                (
                    q["id"],
                    q["stem"],
                    json.dumps(q["choices"], ensure_ascii=False),
                    q["correct"],
                    q["topic_id"],
                    q["section"],
                    q.get("skill"),
                    q.get("cognitive_demand"),
                    q["source_name"],
                    q.get("source_url"),
                    q.get("source_location"),
                    q.get("split", DEV_SPLIT),
                )
            )
        self.conn.executemany(
            """
            INSERT INTO perf_questions
              (id, stem, choices_json, correct, topic_id, section,
               skill, cognitive_demand, source_name, source_url,
               source_location, split)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              stem=excluded.stem,
              choices_json=excluded.choices_json,
              correct=excluded.correct,
              topic_id=excluded.topic_id,
              section=excluded.section,
              skill=excluded.skill,
              cognitive_demand=excluded.cognitive_demand,
              source_name=excluded.source_name,
              source_url=excluded.source_url,
              source_location=excluded.source_location,
              split=excluded.split
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def question_count(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) FROM perf_questions").fetchone()[0]
        )

    # Eligibility -----------------------------------------------------------

    def eligible_questions(
        self,
        unlocked_topics: set[str],
        *,
        split: str = DEV_SPLIT,
        topic_id: Optional[str] = None,
        demands: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        """Questions whose topic is unlocked, or whose section is CARS.

        CARS bypasses the memory gate (locked decision). ``unlocked_topics``
        is passed in so this stays testable without the Rust backend; callers
        in the app use ``unlocked_topic_ids(col)``.

        Optional filters (used by the dashboard Focus-area launcher):
        ``topic_id`` restricts to one topic; ``demands`` restricts to a set of
        ``cognitive_demand`` values (e.g. an ``application`` focus session).
        """
        rows = self.conn.execute(
            "SELECT * FROM perf_questions WHERE split = ?", (split,)
        ).fetchall()
        out = []
        for r in rows:
            eligible = (
                r["section"] == CARS_SECTION or r["topic_id"] in unlocked_topics
            )
            if not eligible:
                continue
            if topic_id is not None and r["topic_id"] != topic_id:
                continue
            if demands is not None and (r["cognitive_demand"] or "") not in demands:
                continue
            out.append(self._row_to_question(r))
        return out

    @staticmethod
    def _row_to_question(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": r["id"],
            "stem": r["stem"],
            "choices": json.loads(r["choices_json"]),
            "correct": r["correct"],
            "topic_id": r["topic_id"],
            "section": r["section"],
            "skill": r["skill"],
            "cognitive_demand": r["cognitive_demand"],
            "source_name": r["source_name"],
            "source_url": r["source_url"],
            "source_location": r["source_location"],
            "split": r["split"],
        }

    # Attempts --------------------------------------------------------------

    def log_attempt(
        self,
        question_id: str,
        correct: bool,
        *,
        error_type: Optional[str] = None,
        time_seconds: Optional[float] = None,
        interleaved: bool = False,
        chosen_index: Optional[int] = None,
        mastery_snapshot: Optional[float] = None,
        inferred_error_type: Optional[str] = None,
        inferred_confidence: Optional[float] = None,
        recheck_card_id: Optional[int] = None,
        recheck_correct: Optional[bool] = None,
        error_source: Optional[str] = None,
    ) -> int:
        """Log an attempt. The v2 inference columns are all optional and default
        to None so existing callers (and the frozen self-report path) are
        unaffected."""
        cur = self.conn.execute(
            """
            INSERT INTO perf_attempts
              (question_id, correct, error_type, time_seconds, interleaved, ts,
               chosen_index, mastery_snapshot, inferred_error_type,
               inferred_confidence, recheck_card_id, recheck_correct,
               error_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                question_id,
                1 if correct else 0,
                error_type,
                time_seconds,
                1 if interleaved else 0,
                int(time.time()),
                chosen_index,
                mastery_snapshot,
                inferred_error_type,
                inferred_confidence,
                recheck_card_id,
                None if recheck_correct is None else (1 if recheck_correct else 0),
                error_source,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def attempt_count(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) FROM perf_attempts").fetchone()[0]
        )

    # Scoring (never blended with memory) -----------------------------------

    def accuracy(self, topic_id: Optional[str] = None) -> dict[str, Any]:
        """Performance accuracy overall or for one topic.

        Returns ``{"attempts": n, "correct": k, "accuracy": float|None}``.
        Accuracy is ``None`` when there are no attempts (honest abstain).
        """
        if topic_id is None:
            row = self.conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(correct), 0) k FROM perf_attempts"
            ).fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT COUNT(*) n, COALESCE(SUM(a.correct), 0) k
                FROM perf_attempts a
                JOIN perf_questions q ON q.id = a.question_id
                WHERE q.topic_id = ?
                """,
                (topic_id,),
            ).fetchone()
        n, k = int(row["n"]), int(row["k"])
        return {
            "attempts": n,
            "correct": k,
            "accuracy": (k / n) if n else None,
        }


def order_questions(
    questions: list[dict[str, Any]],
    *,
    interleaved: bool,
    rng: Optional[random.Random] = None,
) -> list[dict[str, Any]]:
    """Study feature: interleaved = shuffled topics; blocked = grouped by topic."""
    out = list(questions)
    if interleaved:
        (rng or random).shuffle(out)
    else:
        out.sort(key=lambda q: q["topic_id"])
    return out


# ---------------------------------------------------------------------------
# v2 error-diagnosis: content-held score M + 3-bucket inference (pure funcs).
# See MCAT/docs/ERROR-DIAGNOSIS-SPEC.md. These are Qt-free and backend-free so
# they can be unit-tested directly.
# ---------------------------------------------------------------------------
def content_held_score(
    retrievabilities: list[Optional[float]],
    *,
    r0: float = UNCOVERED_R0,
) -> Optional[float]:
    """Content-held score ``M`` = geometric mean of the backing cards'/topics'
    current FSRS retrievability.

    Weakest-link sensitive and stable across prerequisite counts (spec). A
    missing / no-signal value (``None`` or ``<= 0``) is imputed to the
    conservative prior ``r0`` (uncovered prerequisite). Returns ``None`` when
    there is nothing at all to score.

    M path: fed **per-card FSRS retrievability** for the question's backing cards
    (``PerformanceSession._mastery_for`` → ``card_retrievability``), computed in
    pure Python from each card's memory state — no backend/proto round-trip. A
    new / unreviewed / SM-2 card (no memory state) contributes ``None`` and is
    imputed to ``r0``. Topic-level ``avg_retrievability`` remains only as a
    backstop when a question has no discoverable backing cards.
    """
    if not retrievabilities:
        return None
    vals = [
        (r if (r is not None and r > 0) else r0) for r in retrievabilities
    ]
    product = 1.0
    for v in vals:
        product *= v
    return product ** (1.0 / len(vals))


def _affinity_conf(top: float, other: float) -> float:
    total = top + other
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, top / total))


def infer_error_type(
    *,
    correct: bool,
    cognitive_demand: Optional[str] = None,
    mastery: Optional[float] = None,
    time_seconds: Optional[float] = None,
    is_trap: bool = False,
    has_content_tag: bool = False,
    fast_threshold: float = FAST_THRESHOLD_SECONDS,
) -> dict[str, Any]:
    """Infer the science error type + confidence from independent signals.

    Transparent bootstrap rule (spec "Inference rule"), ordered by signal
    strength. Prefers ``unresolved`` over a shaky guess (honesty rule). Returns
    ``{"error_type", "confidence"}``.

    Key guards:
    - ``misread`` fires ONLY on the strict conjunction fast + high ``M`` + trap;
      fast alone is weak → ``unresolved``.
    - ``application`` requires established content presence (high ``M``) AND
      an application/synthesis item — never inferred without content presence.
    - low ``M`` → ``content_gap``; ambiguous / no signal → ``unresolved``.
    """
    if correct:
        return {"error_type": ERR_NONE, "confidence": 1.0}

    m = mastery
    demand = (cognitive_demand or "").lower()
    d = DEMAND_FACTOR.get(demand, 0.0)
    fast = time_seconds is not None and time_seconds < fast_threshold
    high_m = m is not None and m >= HIGH_M
    low_m = m is not None and m < LOW_M

    # misread — strict conjunction, checked first (a fast, predictable-trap pick
    # on an item they clearly know). Fast ALONE is weak and must not land here.
    if high_m and fast and is_trap:
        return {"error_type": ERR_MISREAD, "confidence": 0.6}

    # 1. STRONGEST: cross-system divergence (memory ⟂ performance).
    if high_m and demand in ("application", "synthesis"):
        return {
            "error_type": ERR_APPLICATION,
            "confidence": _affinity_conf(m * d, 1.0 - m),
        }
    if low_m:
        return {
            "error_type": ERR_CONTENT_GAP,
            "confidence": _affinity_conf(1.0 - m, m * d),
        }

    # 3. content axis (authored distractor tag), science-only.
    if has_content_tag:
        return {"error_type": ERR_CONTENT_GAP, "confidence": 0.55}

    # 4. nothing decisive → abstain (fast-alone, ambiguous M, mastered-recall
    #    miss without a trap). Falls back to self-report downstream.
    return {"error_type": ERR_UNRESOLVED, "confidence": 0.0}


def topic_retrievability_map(col: Collection) -> dict[str, float]:
    """topic_id -> current avg FSRS retrievability from the Rust mastery query.

    Coarse ``M`` source (see ``content_held_score`` note). Defensive: returns
    ``{}`` if the backend query is unavailable.
    """
    try:
        return {
            m.topic_id: float(m.avg_retrievability) for m in col.get_topic_mastery()
        }
    except Exception:
        return {}


def backing_topics_for_question(
    col: Collection, question_id: str, fallback_topic: Optional[str]
) -> list[str]:
    """Invert ``supports_question`` → the topics of cards that back this
    question. Best-effort: if the note scan fails or finds nothing, fall back to
    the question's own topic (science stems are backed within their own topic).
    """
    topics: set[str] = set()
    try:
        nids = col.find_notes(f'"supports_question:*{question_id}*"')
        for nid in nids:
            note = col.get_note(nid)
            for tag in note.tags:
                if tag.startswith("topic:"):
                    t = tag[len("topic:") :]
                    if t:
                        topics.add(t)
    except Exception:
        topics = set()
    if not topics and fallback_topic:
        topics.add(fallback_topic)
    return sorted(topics)


def fsrs_retrievability(
    stability: float, elapsed_seconds: float, decay: Optional[float]
) -> Optional[float]:
    """Current FSRS retrievability R ∈ (0, 1] — pure math, no collection needed.

    Power forgetting curve (FSRS-4.5/5/6):
        R(t) = (1 + FACTOR · t_days / S) ^ (−|decay|),  FACTOR = 0.9^(1/−|decay|) − 1
    which gives R = 0.9 exactly when t_days == S (the 90%-retention convention).
    ``decay`` is taken by magnitude so the sign convention of the stored value
    can't flip the curve; ``None`` falls back to the FSRS-5 default. Returns
    ``None`` for a non-positive stability (no usable memory state).
    """
    if stability is None or stability <= 0:
        return None
    decay_abs = abs(decay) if decay else FSRS_DEFAULT_DECAY
    if decay_abs <= 0:
        decay_abs = FSRS_DEFAULT_DECAY
    t_days = max(0.0, elapsed_seconds) / SECONDS_PER_DAY
    factor = 0.9 ** (1.0 / -decay_abs) - 1.0
    r = (1.0 + factor * t_days / stability) ** (-decay_abs)
    return max(0.0, min(1.0, r))


def card_retrievability(col: Collection, cid: int) -> Optional[float]:
    """Per-card current FSRS retrievability, computed in pure Python.

    ``None`` when the card has no FSRS memory state (new / unreviewed / SM-2) or
    no recorded ``last_review_time`` — content_held_score imputes those to the
    ``UNCOVERED_R0`` prior. We deliberately avoid ``col.card_stats_data(cid)``:
    it can backfill ``last_review_time`` as a side effect, which must not happen
    on a read-only scoring path (see MCAT/docs/PERFORMANCE-MODE-SPEC.md).
    """
    try:
        card = col.get_card(cid)
    except Exception:
        return None
    state = card.memory_state
    if state is None:
        return None
    last = card.last_review_time
    if last is None:
        return None
    elapsed = int(time.time()) - int(last)
    return fsrs_retrievability(float(state.stability), float(elapsed), card.decay)


def backing_card_ids_for_question(col: Collection, question_id: str) -> list[int]:
    """Invert ``supports_question`` → the ids of every card on a note that backs
    this question. Best-effort: returns ``[]`` if the note scan fails.

    The 3-digit q_syn_/q_dev_/q_ho_ ids are collision-free under substring match
    (no id is a substring of another), so the wildcard field search is safe.
    """
    cids: list[int] = []
    try:
        nids = col.find_notes(f'"supports_question:*{question_id}*"')
        for nid in nids:
            cids.extend(int(c) for c in col.card_ids_of_note(nid))
    except Exception:
        return []
    return cids


class PerformanceSession:
    """Qt-free controller for a performance session.

    Owns ordering, grading, error classification, attempt logging, and running
    score. The Qt dialog renders this; tests drive it directly without a GUI.
    """

    def __init__(
        self,
        store: PerfStore,
        questions: list[dict[str, Any]],
        *,
        interleaved: bool,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.store = store
        self.interleaved = interleaved
        self.questions = order_questions(
            questions, interleaved=interleaved, rng=rng
        )
        self.index = 0
        self.answered = 0
        self.correct = 0
        self._awaiting_error = False
        self._pending_time: Optional[float] = None
        self._pending_choice: Optional[int] = None
        # v2 inference stashed on a miss, logged when the error is resolved.
        self._pending_inference: Optional[dict[str, Any]] = None
        self._pending_mastery: Optional[float] = None
        self._retr_map: Optional[dict[str, float]] = None

    @property
    def total(self) -> int:
        return len(self.questions)

    @property
    def finished(self) -> bool:
        return self.index >= len(self.questions)

    @property
    def current(self) -> dict[str, Any]:
        return self.questions[self.index]

    @property
    def awaiting_error_type(self) -> bool:
        return self._awaiting_error

    @property
    def pending_inference(self) -> Optional[dict[str, Any]]:
        """The inferred (error_type, confidence) for the current miss, or None.

        The dialog may read this to surface the hypothesis; the frozen 3/4-button
        self-report still runs regardless (additive)."""
        return self._pending_inference

    def correct_index(self) -> int:
        return LETTERS.index(self.current["correct"])

    def _mastery_for(self, question: dict[str, Any]) -> Optional[float]:
        """Content-held score M for a question from **per-card FSRS-R** of its
        backing cards (geometric mean; weakest-link sensitive). Falls back to the
        coarse topic-level retrievability only when no backing cards are found.
        Backend-guarded → None on error.
        """
        try:
            cids = backing_card_ids_for_question(self.store.col, question["id"])
            if cids:
                return content_held_score(
                    [card_retrievability(self.store.col, cid) for cid in cids]
                )
            # Backstop: no discoverable backing cards → topic-level R.
            if self._retr_map is None:
                self._retr_map = topic_retrievability_map(self.store.col)
            topics = backing_topics_for_question(
                self.store.col, question["id"], question.get("topic_id")
            )
            return content_held_score(
                [self._retr_map.get(t) for t in topics]
            )
        except Exception:
            return None

    @staticmethod
    def _choice_flags(question: dict[str, Any], choice_idx: int) -> tuple[bool, bool]:
        """(is_trap, has_content_tag) from an authored choice_diagnosis, if any.

        choice_diagnosis bulk authoring is deferred, so this is usually
        (False, False) and inference degrades gracefully to cross-system+timing.
        """
        cd = question.get("choice_diagnosis")
        if not isinstance(cd, list) or not (0 <= choice_idx < len(cd)):
            return (False, False)
        entry = cd[choice_idx]
        if not entry:
            return (False, False)
        is_trap = bool(entry.get("trap"))
        maps = entry.get("maps_to")
        has_content = maps == ERR_CONTENT_GAP or (
            isinstance(maps, list) and ERR_CONTENT_GAP in maps
        )
        return (is_trap, has_content)

    def answer(self, choice_idx: int, *, time_seconds: Optional[float] = None) -> bool:
        """Grade a choice. Correct → logged immediately. Wrong → awaits error type.

        Returns True if correct. Raises if a prior wrong answer is unclassified.
        """
        if self._awaiting_error:
            raise RuntimeError("classify the previous error before answering again")
        q = self.current
        is_correct = LETTERS[choice_idx] == q["correct"]
        if is_correct:
            self.store.log_attempt(
                q["id"],
                correct=True,
                time_seconds=time_seconds,
                interleaved=self.interleaved,
                chosen_index=choice_idx,
                inferred_error_type=ERR_NONE,
                error_source=None,
            )
            self.answered += 1
            self.correct += 1
        else:
            self._awaiting_error = True
            self._pending_time = time_seconds
            self._pending_choice = choice_idx
            # Compute the v2 inference now (behavior + cross-system M), stash it
            # to log alongside the resolved self-report type.
            m = self._mastery_for(q)
            is_trap, has_content_tag = self._choice_flags(q, choice_idx)
            self._pending_mastery = m
            self._pending_inference = infer_error_type(
                correct=False,
                cognitive_demand=q.get("cognitive_demand"),
                mastery=m,
                time_seconds=time_seconds,
                is_trap=is_trap,
                has_content_tag=has_content_tag,
            )
        return is_correct

    def classify_error(self, error_type: str) -> None:
        """Log the pending wrong answer with its self-reported error type.

        The frozen self-report ``error_type`` is stored unchanged (downstream
        resolved type). Additively, the v2 inference (hypothesis + confidence +
        mastery snapshot) and an ``error_source`` consistency label are logged
        alongside — inference never overrides the user's report here.
        """
        if not self._awaiting_error:
            raise RuntimeError("no wrong answer awaiting classification")
        q = self.current
        inf = self._pending_inference or {}
        inferred = inf.get("error_type", ERR_UNRESOLVED)
        error_source = self._reconcile_source(error_type, inferred)
        self.store.log_attempt(
            q["id"],
            correct=False,
            error_type=error_type,
            time_seconds=self._pending_time,
            interleaved=self.interleaved,
            chosen_index=self._pending_choice,
            mastery_snapshot=self._pending_mastery,
            inferred_error_type=inferred,
            inferred_confidence=inf.get("confidence"),
            error_source=error_source,
        )
        self.answered += 1
        self._awaiting_error = False
        self._pending_time = None
        self._pending_choice = None
        self._pending_inference = None
        self._pending_mastery = None

    @staticmethod
    def _reconcile_source(self_report: str, inferred: str) -> str:
        """Consistency channel between the inference and the self-report.

        - inference abstained (unresolved/none) → ``self_report``
        - self-report agrees with inference (folding the legacy enum) →
          ``inferred_confirmed``
        - otherwise → ``self_report_override``
        """
        if inferred in (ERR_UNRESOLVED, ERR_NONE):
            return "self_report"
        folded = LEGACY_ERROR_FOLD.get(self_report, self_report)
        return "inferred_confirmed" if folded == inferred else "self_report_override"

    def advance(self) -> None:
        """Move to the next question. Requires the current one be resolved."""
        if self._awaiting_error:
            raise RuntimeError("classify the error before advancing")
        self.index += 1

    def summary(self) -> dict[str, Any]:
        return {
            "answered": self.answered,
            "correct": self.correct,
            "accuracy": (self.correct / self.answered) if self.answered else None,
        }
