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
import uuid
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

# Application-practice remediation pool markers + sizing. The pool is a separate
# store (remediation_items) that NEVER feeds Performance/Readiness. See
# MCAT/docs/APPLICATION-PRACTICE-POOL.md ("Spec for the wiring pass").
REMEDIATION_SPLIT = "remediation"
APPLICATION_PRACTICE_POOL = "application_practice"
# N: how many sibling integration items to serve as a lightweight nudge on an
# `application` miss. Each science topic has 3 pool items, so excluding the
# just-missed concept still leaves >= 2. Config constant per the spec (step 5).
N_APPLICATION_PRACTICE = 2

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

# error_source values (perf_attempts.error_source). The first three are the
# frozen self-report reconciliation labels; ``ERROR_SOURCE_RECHECK`` marks a call
# where the objective content re-check probe informed the (confirmed) diagnosis.
ERROR_SOURCE_INFERRED = "inferred_confirmed"
ERROR_SOURCE_OVERRIDE = "self_report_override"
ERROR_SOURCE_SELF_REPORT = "self_report"
ERROR_SOURCE_RECHECK = "inference+recheck"  # probe-informed + self-report agreed

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

# Version tag stamped into every feature_json blob so the eval/export pipeline
# can tell which capture schema produced a row. Bump when the vector changes.
FEATURE_SCHEMA_VERSION = "mcat_perf_features_v1"

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

-- ---------------------------------------------------------------------------
-- Application-practice remediation pool (ISOLATED from scored state).
--
-- This is a SEPARATE store from perf_questions on purpose: remediation items
-- must NEVER enter the Performance or Readiness scores. They live in their own
-- table (never joined by accuracy()/eligible_questions()), and the CHECK
-- constraint below hard-guards that only `split = 'remediation'` rows can land
-- here — a scored ('dev'/'held_out') item is rejected at the DB layer. The sync
-- bundle (_read_bundle_from_conn) dumps only perf_questions/perf_attempts, so
-- these tables stay strictly local and cannot leak cross-device either.
-- See MCAT/docs/APPLICATION-PRACTICE-POOL.md + ERROR-DIAGNOSIS-SPEC.md.
CREATE TABLE IF NOT EXISTS remediation_items (
  id TEXT PRIMARY KEY,
  stem TEXT NOT NULL,
  choices_json TEXT NOT NULL,
  correct TEXT NOT NULL,
  topic_id TEXT NOT NULL,
  section TEXT NOT NULL,
  skill TEXT,
  cognitive_demand TEXT,
  concept TEXT,
  choice_diagnosis TEXT,
  explanation TEXT,
  source_name TEXT NOT NULL,
  source_url TEXT,
  source_location TEXT,
  pool TEXT NOT NULL,
  split TEXT NOT NULL CHECK (split = 'remediation')
);

-- Practice attempts on the remediation pool. Its OWN channel — deliberately NOT
-- perf_attempts, so it can never be counted by accuracy() (which counts every
-- perf_attempts row). Also serves as the persisted "seen-set" for the selection
-- algorithm (exclude already-practiced ids so repeat misses surface new items).
CREATE TABLE IF NOT EXISTS remediation_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL,
  correct INTEGER NOT NULL,
  time_seconds REAL,
  ts INTEGER NOT NULL,
  channel TEXT NOT NULL DEFAULT 'application_practice',
  FOREIGN KEY (item_id) REFERENCES remediation_items(id)
);

CREATE INDEX IF NOT EXISTS idx_remediation_items_topic
  ON remediation_items(topic_id);
CREATE INDEX IF NOT EXISTS idx_remediation_attempts_item
  ON remediation_attempts(item_id);
"""

# Additive columns applied on top of _SCHEMA via ALTER TABLE (idempotent). These
# guard cleanly against older sidecar DBs that predate the v2 error-diagnosis
# layer.
#
# `feature_json` captures the full signal vector genuinely observed at classify
# time (see PerformanceSession._feature_vector / FEATURE_SCHEMA_VERSION). The
# recheck_card_id/recheck_correct probe outcomes are now POPULATED when the
# immediate content re-check probe fires on a miss (see resolve_probe_target +
# PerformanceSession.record_probe_outcome and the Qt dialog wiring); they stay
# NULL for correct answers, CARS/unmapped misses, and any miss where no probe
# ran. The remaining deferred columns (first_choice_index, answer_changes,
# recheck_timing) are still intentionally NOT populated: they need
# select-then-confirm churn logging + the delayed-probe variant the dialog does
# not provide. We capture only what is genuinely observed — those stay NULL.
# See MCAT/docs/LOOSE-ENDS.md ("Deferred v2 UX").
_MIGRATIONS: dict[str, dict[str, str]] = {
    "perf_questions": {
        "cognitive_demand": "TEXT",  # recall | application | synthesis (science)
        "choice_diagnosis": "TEXT",  # JSON list aligned 1:1 with choices; each
                                     #  entry null or {maps_to, misconception?, trap?}
        "explanation": "TEXT",       # static (NO-AI) correct-answer rationale
                                     #  shown after answering; also the AI-off
                                     #  fallback / baseline for the AI explainer.
    },
    "perf_attempts": {
        "chosen_index": "INTEGER",         # option finally submitted (0–3)
        "mastery_snapshot": "REAL",        # content-held score M at attempt time
        "inferred_error_type": "TEXT",     # v2 inference hypothesis
        "inferred_confidence": "REAL",     # p_top of the inferred type
        "recheck_card_id": "INTEGER",      # backing card probed by the immediate
                                           #  content re-check probe (NULL for a
                                           #  fallback concept probe / no probe)
        "recheck_correct": "INTEGER",      # objective probe outcome (1/0); NULL
                                           #  when no probe fired for the miss
        "error_source": "TEXT",            # inferred_confirmed | inference+recheck
                                           #  | self_report_override | self_report
                                           #  | unresolved
        "feature_json": "TEXT",            # full observed signal vector (JSON,
                                           #  ensure_ascii=False); schema-tagged
        "uuid": "TEXT",                    # globally-stable attempt key for
                                           #  cross-device sync (the autoincrement
                                           #  ``id`` is device-local, NOT portable).
                                           #  Backfilled + UNIQUE-indexed by
                                           #  _migrate(); see the sync section
                                           #  (DECISIONS.md §22).
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
    _backfill_attempt_uuids(conn)


def _backfill_attempt_uuids(conn: sqlite3.Connection) -> None:
    """Assign a stable ``uuid`` to any attempt that lacks one, then enforce
    uniqueness.

    The autoincrement ``id`` is device-local and cannot survive an
    export/import round-trip, so cross-device sync dedups on this ``uuid``
    instead. Older sidecar DBs (pre-sync) have NULL uuids; we fill them once
    with a random uuid4 (persisted, so it is stable thereafter). Idempotent:
    once every row has a uuid this is a no-op. SQLite permits multiple NULLs in
    a UNIQUE index, so we backfill before creating it.
    """
    if "uuid" not in _existing_columns(conn, "perf_attempts"):
        return
    for row in conn.execute(
        "SELECT id FROM perf_attempts WHERE uuid IS NULL"
    ).fetchall():
        conn.execute(
            "UPDATE perf_attempts SET uuid = ? WHERE id = ?",
            (uuid.uuid4().hex, row[0]),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_perf_attempts_uuid "
        "ON perf_attempts(uuid)"
    )


def _connect(db_path: str) -> sqlite3.Connection:
    """Open a sidecar DB, apply the schema + additive migrations, and commit.

    Shared by ``PerfStore`` (app path) and the headless sync helpers so schema
    ownership stays in one place.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


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
        self.conn = _connect(self.path)

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
            cd = q.get("choice_diagnosis")
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
                    # absent/None -> store NULL; otherwise serialize the list
                    # (aligned 1:1 with choices, entries null or dicts).
                    None if cd is None else json.dumps(cd, ensure_ascii=False),
                    # static (NO-AI) correct-answer rationale; stored verbatim.
                    q.get("explanation"),
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
               skill, cognitive_demand, choice_diagnosis, explanation,
               source_name, source_url, source_location, split)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              stem=excluded.stem,
              choices_json=excluded.choices_json,
              correct=excluded.correct,
              topic_id=excluded.topic_id,
              section=excluded.section,
              skill=excluded.skill,
              cognitive_demand=excluded.cognitive_demand,
              choice_diagnosis=excluded.choice_diagnosis,
              explanation=excluded.explanation,
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

    # Application-practice remediation pool (ISOLATED) ----------------------
    #
    # Parallel to load_questions/upsert_questions but writes to a SEPARATE table
    # (remediation_items), never perf_questions. Nothing here is ever read by
    # accuracy() or eligible_questions(), so the pool cannot enter the scored
    # Performance/Readiness state. See the schema note above.

    def load_remediation(self, path: str) -> int:
        """Idempotent upsert of the application-practice pool from a JSON file.

        Mirrors ``load_questions`` but targets the isolated ``remediation_items``
        table. Returns the number of rows written.
        """
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
        return self.upsert_remediation(items)

    def upsert_remediation(self, items: list[dict[str, Any]]) -> int:
        """Upsert remediation-pool items into the isolated store.

        Only ``split == 'remediation'`` items are accepted (the DB CHECK would
        reject others anyway); this raises early with a clear message so a
        scored item can never be silently dropped into the pool table.
        """
        rows = []
        for q in items:
            split = q.get("split", REMEDIATION_SPLIT)
            if split != REMEDIATION_SPLIT:
                raise ValueError(
                    f"remediation pool item {q.get('id')!r} has split={split!r}; "
                    f"only {REMEDIATION_SPLIT!r} items belong in remediation_items"
                )
            cd = q.get("choice_diagnosis")
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
                    q.get("concept"),
                    None if cd is None else json.dumps(cd, ensure_ascii=False),
                    q.get("explanation"),
                    q["source_name"],
                    q.get("source_url"),
                    q.get("source_location"),
                    q.get("pool", APPLICATION_PRACTICE_POOL),
                    split,
                )
            )
        self.conn.executemany(
            """
            INSERT INTO remediation_items
              (id, stem, choices_json, correct, topic_id, section, skill,
               cognitive_demand, concept, choice_diagnosis, explanation,
               source_name, source_url, source_location, pool, split)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              stem=excluded.stem,
              choices_json=excluded.choices_json,
              correct=excluded.correct,
              topic_id=excluded.topic_id,
              section=excluded.section,
              skill=excluded.skill,
              cognitive_demand=excluded.cognitive_demand,
              concept=excluded.concept,
              choice_diagnosis=excluded.choice_diagnosis,
              explanation=excluded.explanation,
              source_name=excluded.source_name,
              source_url=excluded.source_url,
              source_location=excluded.source_location,
              pool=excluded.pool,
              split=excluded.split
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def remediation_count(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM remediation_items"
            ).fetchone()[0]
        )

    def remediation_items_for_topic(self, topic_id: str) -> list[dict[str, Any]]:
        """All application-practice pool items for a topic (unfiltered).

        The topic filter of the selection algorithm; concept/seen filtering and
        ranking happen in ``select_application_practice`` (a pure function) so
        the logic stays testable without a DB.
        """
        rows = self.conn.execute(
            "SELECT * FROM remediation_items WHERE topic_id = ?", (topic_id,)
        ).fetchall()
        return [self._row_to_remediation(r) for r in rows]

    @staticmethod
    def _row_to_remediation(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": r["id"],
            "stem": r["stem"],
            "choices": json.loads(r["choices_json"]),
            "correct": r["correct"],
            "topic_id": r["topic_id"],
            "section": r["section"],
            "skill": r["skill"],
            "cognitive_demand": r["cognitive_demand"],
            "concept": r["concept"],
            "choice_diagnosis": (
                None
                if r["choice_diagnosis"] is None
                else json.loads(r["choice_diagnosis"])
            ),
            "explanation": r["explanation"],
            "source_name": r["source_name"],
            "source_url": r["source_url"],
            "source_location": r["source_location"],
            "pool": r["pool"],
            "split": r["split"],
        }

    def seen_remediation_ids(self) -> set[str]:
        """Pool ids the student has already practiced (the seen-set).

        Read from the isolated ``remediation_attempts`` channel so repeated
        ``application`` misses in a topic surface NEW practice items.
        """
        return {
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT item_id FROM remediation_attempts"
            ).fetchall()
        }

    def log_remediation_attempt(
        self,
        item_id: str,
        correct: bool,
        *,
        time_seconds: Optional[float] = None,
        channel: str = APPLICATION_PRACTICE_POOL,
    ) -> int:
        """Log a practice attempt to the ISOLATED remediation channel.

        Deliberately NOT ``perf_attempts``: accuracy() counts every
        perf_attempts row, so logging practice there would leak into the
        Performance score. This keeps remediation practice out of every scored
        query while still recording what was seen (for the seen-set).
        """
        cur = self.conn.execute(
            """
            INSERT INTO remediation_attempts
              (item_id, correct, time_seconds, ts, channel)
            VALUES (?, ?, ?, ?, ?)
            """,
            (item_id, 1 if correct else 0, time_seconds, int(time.time()), channel),
        )
        self.conn.commit()
        return int(cur.lastrowid)

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
            # NULL -> None; else the JSON list (with its null entries preserved).
            "choice_diagnosis": (
                None
                if r["choice_diagnosis"] is None
                else json.loads(r["choice_diagnosis"])
            ),
            # static (NO-AI) correct-answer rationale (may be NULL on legacy rows).
            "explanation": r["explanation"],
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
        feature_json: Optional[dict[str, Any]] = None,
    ) -> int:
        """Log an attempt. The v2 inference columns are all optional and default
        to None so existing callers (and the frozen self-report path) are
        unaffected. ``feature_json`` (a dict) is serialized with
        ``ensure_ascii=False`` and stored verbatim as the observed signal vector.
        """
        cur = self.conn.execute(
            """
            INSERT INTO perf_attempts
              (question_id, correct, error_type, time_seconds, interleaved, ts,
               chosen_index, mastery_snapshot, inferred_error_type,
               inferred_confidence, recheck_card_id, recheck_correct,
               error_source, feature_json, uuid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                (
                    None
                    if feature_json is None
                    else json.dumps(feature_json, ensure_ascii=False)
                ),
                # Stable, portable key stamped at creation time. Persisted so an
                # export/import round-trip dedups on it (the autoincrement id is
                # device-local). See _backfill_attempt_uuids + the sync section.
                uuid.uuid4().hex,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def attempt_count(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) FROM perf_attempts").fetchone()[0]
        )

    # Reset (testing / fresh-start helpers) ---------------------------------

    def reset_attempts(self) -> int:
        """Delete every logged attempt, leaving the loaded question bank intact.

        Clears all recorded performance/readiness signal (``perf_attempts``)
        while keeping ``perf_questions`` so the bank does not need reloading.
        Returns the number of attempts deleted. Single DELETE + commit.
        """
        n = self.attempt_count()
        self.conn.execute("DELETE FROM perf_attempts")
        self.conn.commit()
        return n

    def reset_all(self) -> tuple[int, int]:
        """Clear attempts AND the loaded question bank.

        Returns ``(attempts_deleted, questions_deleted)``. Prefer
        ``reset_attempts()`` for testing — this also wipes the bank, requiring a
        reload. Not wired into the default menu action.
        """
        attempts = self.attempt_count()
        questions = self.question_count()
        self.conn.execute("DELETE FROM perf_attempts")
        self.conn.execute("DELETE FROM perf_questions")
        self.conn.commit()
        return attempts, questions

    # Sync (append-only union merge) ----------------------------------------

    def import_bundle(self, path: str) -> dict[str, int]:
        """Merge a portable sync bundle into this store's sidecar DB.

        Append-only union merge: attempts are deduped by their stable ``uuid``
        (re-importing the same bundle is a no-op) and question rows are inserted
        by ``id`` when missing (the bank is deterministic from the curated
        source, so device-local copies are not trusted over existing rows).
        Order-independent and idempotent. Returns
        ``{"attempts_added", "attempts_skipped", "questions_added"}``.
        """
        return merge_bundle(self.conn, _load_bundle(path))

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


def _rank_for_variety(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministically order items to prefer covering distinct concepts first.

    Greedy: the first time a ``concept`` slug appears it goes to the front block
    (in id order), later repeats of an already-seen concept trail behind. So the
    top-N slice maximises concept variety. Fully deterministic (id tie-break) so
    the same miss always surfaces the same practice set — no AI, no randomness.
    """
    seen_concepts: set[Any] = set()
    primary: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    for it in sorted(items, key=lambda x: x["id"]):
        concept = it.get("concept")
        if concept not in seen_concepts:
            seen_concepts.add(concept)
            primary.append(it)
        else:
            secondary.append(it)
    return primary + secondary


def select_application_practice(
    pool: list[dict[str, Any]],
    *,
    topic_id: str,
    missed_concept: Optional[str] = None,
    seen_ids: Optional[set[str]] = None,
    n: int = N_APPLICATION_PRACTICE,
) -> list[dict[str, Any]]:
    """Select up to ``n`` sibling integration items to practice after an
    ``application`` miss. Pure + deterministic (AI-off) — reads only the local
    pool. Implements the spec's selection algorithm
    (MCAT/docs/APPLICATION-PRACTICE-POOL.md → "Spec for the wiring pass"):

    1. **Filter** to ``topic_id == T`` and ``pool == 'application_practice'``.
    2. **Exclude the just-missed concept** (drop ``concept == missed_concept``)
       so the student practices *sibling* concepts — but if that empties the
       set, fall back to including the same-concept items.
    3. **Exclude already-seen** pool ids (``seen_ids``).
    4. **Rank for variety** (distinct concepts first, deterministic id order).
    5. **Take N.**

    Degradation: returns ``[]`` when nothing is eligible (no pool coverage for
    the topic, or everything already seen) so the caller can fall back to the
    generic message rather than promising a bank that isn't there.
    """
    seen = seen_ids or set()
    # 1. topic + pool-membership filter.
    topic_items = [
        it
        for it in pool
        if it.get("topic_id") == topic_id
        and it.get("pool", APPLICATION_PRACTICE_POOL) == APPLICATION_PRACTICE_POOL
    ]
    if not topic_items:
        return []
    # 2. exclude the just-missed concept; fall back to same-concept if empty.
    if missed_concept is not None:
        sibling = [it for it in topic_items if it.get("concept") != missed_concept]
        candidates = sibling if sibling else topic_items
    else:
        candidates = topic_items
    # 3. exclude already-seen ids.
    candidates = [it for it in candidates if it.get("id") not in seen]
    if not candidates:
        return []
    # 4. rank for concept variety; 5. take N.
    return _rank_for_variety(candidates)[: max(0, n)]


# ---------------------------------------------------------------------------
# Export (eval pipeline). Read-only on the sidecar DB. The local sidecar is the
# source of truth; this dumps perf_attempts (joined with perf_questions for
# topic/section/split context) to CSV and/or JSON for offline eval. No AI, no
# network. See MCAT/docs/DECISIONS.md §5 and tools/mcat_export_perf.py.
# ---------------------------------------------------------------------------
EXPORT_FEATURE_PREFIX = "feat_"

# Attempt columns joined with a small, useful slice of question context. We use
# ``a.*`` (rather than naming every attempt column) so the export still works on
# older sidecar DBs that predate some additive columns.
_EXPORT_QUERY = """
SELECT a.*,
       q.topic_id       AS topic_id,
       q.section        AS section,
       q.split          AS split,
       q.cognitive_demand AS cognitive_demand,
       q.source_name    AS source_name
FROM perf_attempts a
LEFT JOIN perf_questions q ON q.id = a.question_id
ORDER BY a.id
"""


def read_attempts(db_path: str) -> list[dict[str, Any]]:
    """Read every attempt (joined with question context) from a sidecar DB.

    Opens the DB **read-only** (URI ``mode=ro``) so an export can never mutate
    the collection's performance data. ``feature_json`` is parsed into a nested
    ``feature`` dict alongside the raw string. Returns a list of plain dicts.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(_EXPORT_QUERY).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        raw = d.get("feature_json")
        d["feature"] = json.loads(raw) if raw else None
        out.append(d)
    return out


def _write_json(rows: list[dict[str, Any]], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _write_csv(rows: list[dict[str, Any]], path: str) -> None:
    import csv

    # Base columns = the flat attempt/question columns (everything but the
    # nested parsed ``feature``). feature_json stays as a raw column too.
    base_keys: list[str] = []
    feat_keys: set[str] = set()
    for r in rows:
        for k in r:
            if k != "feature" and k not in base_keys:
                base_keys.append(k)
        if isinstance(r.get("feature"), dict):
            feat_keys.update(r["feature"].keys())
    # feature vector expanded into stable feat_<key> columns for eval tooling.
    feat_cols = [EXPORT_FEATURE_PREFIX + k for k in sorted(feat_keys)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(base_keys + feat_cols)
        for r in rows:
            feature = r.get("feature") or {}
            row = [r.get(k) for k in base_keys]
            row += [feature.get(k) for k in sorted(feat_keys)]
            writer.writerow(row)


def export_attempts(db_path: str, out_path: str, *, fmt: str = "both") -> int:
    """Export attempts from a sidecar DB to CSV and/or JSON. Returns row count.

    ``fmt`` is ``csv`` | ``json`` | ``both``. For ``both`` the extension of
    ``out_path`` is dropped and ``.csv`` + ``.json`` siblings are written.
    Read-only on the DB.
    """
    fmt = fmt.lower()
    if fmt not in ("csv", "json", "both"):
        raise ValueError(f"unknown format {fmt!r} (use csv|json|both)")
    rows = read_attempts(db_path)
    if fmt == "both":
        base = os.path.splitext(out_path)[0]
        _write_csv(rows, base + ".csv")
        _write_json(rows, base + ".json")
    elif fmt == "csv":
        _write_csv(rows, out_path)
    else:
        _write_json(rows, out_path)
    return len(rows)


# ---------------------------------------------------------------------------
# Calibration view (probe-aware). Read-only. Filters perf_attempts to the rows
# the objective content re-check probe LABELED (``recheck_correct IS NOT NULL``)
# and emits one FLAT, analysis-ready row per attempt, plus a diagnosis/probe
# agreement tally. Purpose: make it turnkey to answer "how often does the
# heuristic diagnosis agree with the objective probe?" for honest accuracy
# reporting AND to calibrate the hand-set weights (``w_mis``, confidence bands,
# HIGH_M / LOW_M) against the objective label — this is NOT ML. See
# MCAT/docs/DECISIONS.md (calibration export) and tools/mcat_export_perf.py.
# ---------------------------------------------------------------------------

# Stable, flat column order for the calibration CSV/JSON (analysis-ready). No
# nested objects: every value is a scalar so the export drops straight into a
# spreadsheet / pandas without post-processing.
CALIBRATION_COLUMNS = [
    "uuid",
    "question_id",
    "topic_id",
    "cognitive_demand",
    "chosen_index",
    "correct",
    "time_seconds",
    "mastery_snapshot",
    "high_m",                 # derived: mastery_snapshot >= HIGH_M
    "low_m",                  # derived: mastery_snapshot <  LOW_M
    "is_trap",                # from feature_json (tolerant of legacy rows)
    "trap_type",
    "has_content_tag",
    "maps_to",
    "recheck_card_id",
    "recheck_correct",        # OBJECTIVE probe label: 1 = PASS, 0 = FAIL
    "heuristic_error_type",   # engine hypothesis WITHOUT the probe (pre-probe)
    "heuristic_confidence",
    "inferred_error_type",    # engine hypothesis AS STORED (probe-informed)
    "inferred_confidence",
    "error_source",
]


def _flatten_calibration_row(attempt: dict[str, Any]) -> dict[str, Any]:
    """Flatten one joined attempt into an analysis-ready calibration row.

    Tolerant of older / partial rows: any missing ``feature_json`` key degrades
    to ``None``/``False`` rather than crashing. Besides copying the stored
    columns it (a) derives the ``high_m`` / ``low_m`` threshold flags from
    ``mastery_snapshot`` using the tunable HIGH_M / LOW_M bands, and (b)
    RECOMPUTES the engine's PRE-PROBE heuristic diagnosis
    (``recheck_correct=None``). The recompute matters: the STORED
    ``inferred_error_type`` is probe-informed (the probe is evaluated first in
    ``infer_error_type``), so comparing it to the probe is tautological. The
    pre-probe ``heuristic_error_type`` is what the hand-tuned weights would say
    on their own, so it is the honest thing to agree/disagree with the probe.
    """
    feat = attempt.get("feature")
    if not isinstance(feat, dict):
        feat = {}
    m = attempt.get("mastery_snapshot")
    is_trap = feat.get("is_trap")
    has_content_tag = feat.get("has_content_tag")
    heuristic = infer_error_type(
        correct=bool(attempt.get("correct")),
        cognitive_demand=attempt.get("cognitive_demand"),
        mastery=m,
        time_seconds=attempt.get("time_seconds"),
        is_trap=bool(is_trap),
        has_content_tag=bool(has_content_tag),
        recheck_correct=None,
    )
    return {
        "uuid": attempt.get("uuid"),
        "question_id": attempt.get("question_id"),
        "topic_id": attempt.get("topic_id"),
        "cognitive_demand": attempt.get("cognitive_demand"),
        "chosen_index": attempt.get("chosen_index"),
        "correct": attempt.get("correct"),
        "time_seconds": attempt.get("time_seconds"),
        "mastery_snapshot": m,
        "high_m": None if m is None else (m >= HIGH_M),
        "low_m": None if m is None else (m < LOW_M),
        "is_trap": None if is_trap is None else bool(is_trap),
        "trap_type": feat.get("trap_type"),
        "has_content_tag": (
            None if has_content_tag is None else bool(has_content_tag)
        ),
        "maps_to": feat.get("maps_to"),
        "recheck_card_id": attempt.get("recheck_card_id"),
        "recheck_correct": attempt.get("recheck_correct"),
        "heuristic_error_type": heuristic.get("error_type"),
        "heuristic_confidence": heuristic.get("confidence"),
        "inferred_error_type": attempt.get("inferred_error_type"),
        "inferred_confidence": attempt.get("inferred_confidence"),
        "error_source": attempt.get("error_source"),
    }


def build_calibration_rows(db_path: str) -> list[dict[str, Any]]:
    """Probe-labeled attempts only, flattened for calibration. Read-only.

    Filters to ``recheck_correct IS NOT NULL`` (the rows the objective content
    re-check probe actually labeled) and returns one flat dict per attempt
    (columns == ``CALIBRATION_COLUMNS``).
    """
    return [
        _flatten_calibration_row(a)
        for a in read_attempts(db_path)
        if a.get("recheck_correct") is not None
    ]


def calibration_agreement(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Decision-relevant agreement between the heuristic diagnosis and the probe.

    Over the probe-labeled rows, a 2x2 confusion-style tally of the ONE decision
    that matters for calibration — did the engine call it a content gap, and did
    the objective probe agree the content was missing?

      * probe FAIL (``recheck_correct == 0``) SHOULD map to engine ``content_gap``
        (content was demonstrably not retrievable when cued).
      * probe PASS (``recheck_correct == 1``) SHOULD map to engine
        NOT-``content_gap`` (application / misread — the content was available).

    Agreement uses the PRE-PROBE ``heuristic_error_type`` (not the stored,
    probe-informed label — that would be tautological). ``agreement_rate`` =
    (agree cells) / (labeled rows). Returns the tally, the labeled count, the
    agree count, and the rate (``None`` when there is nothing labeled — honest
    abstain).
    """
    tally = {
        "fail_content_gap": 0,  # probe FAIL & engine content_gap   -> AGREE
        "fail_other": 0,        # probe FAIL & engine NOT content_gap-> disagree
        "pass_content_gap": 0,  # probe PASS & engine content_gap    -> disagree
        "pass_other": 0,        # probe PASS & engine NOT content_gap-> AGREE
    }
    for r in rows:
        rc = r.get("recheck_correct")
        if rc is None:
            continue
        engine_content_gap = r.get("heuristic_error_type") == ERR_CONTENT_GAP
        if not rc:  # 0 == probe FAIL
            tally["fail_content_gap" if engine_content_gap else "fail_other"] += 1
        else:  # 1 == probe PASS
            tally["pass_content_gap" if engine_content_gap else "pass_other"] += 1
    labeled = sum(tally.values())
    agree = tally["fail_content_gap"] + tally["pass_other"]
    return {
        "tally": tally,
        "labeled": labeled,
        "agree": agree,
        "agreement_rate": (agree / labeled) if labeled else None,
    }


def _write_calibration_csv(rows: list[dict[str, Any]], path: str) -> None:
    import csv

    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CALIBRATION_COLUMNS)
        for r in rows:
            writer.writerow([r.get(k) for k in CALIBRATION_COLUMNS])


def write_calibration(
    rows: list[dict[str, Any]], out_path: str, *, fmt: str = "both"
) -> int:
    """Write pre-built calibration ``rows`` to CSV and/or JSON. Returns count.

    ``fmt`` is ``csv`` | ``json`` | ``both``. For ``both`` the extension of
    ``out_path`` is dropped and ``.csv`` + ``.json`` siblings are written.
    """
    fmt = fmt.lower()
    if fmt not in ("csv", "json", "both"):
        raise ValueError(f"unknown format {fmt!r} (use csv|json|both)")
    if fmt == "both":
        base = os.path.splitext(out_path)[0]
        _write_calibration_csv(rows, base + ".csv")
        _write_json(rows, base + ".json")
    elif fmt == "csv":
        _write_calibration_csv(rows, out_path)
    else:
        _write_json(rows, out_path)
    return len(rows)


# ---------------------------------------------------------------------------
# Two-way sync — portable export/import bundle (DECISIONS.md §22).
#
# Stock Anki sync is schema-aware and will not propagate our custom sidecar
# tables, so cross-device perf sync uses a portable, versioned JSON bundle that
# is UNION-MERGED on import. ``perf_attempts`` is append-only truth: each row
# carries a globally-stable ``uuid`` (the autoincrement ``id`` is device-local),
# so the merge dedups on ``uuid`` — making it idempotent (re-importing the same
# bundle changes nothing) and order-independent (import order can't affect the
# result). ``perf_questions`` is deterministic from the curated OpenStax source,
# so the bundle carries the bank verbatim and import inserts questions by ``id``
# only when missing (never overwriting a local row). Two devices that each
# export their bundle and import the other's converge to the exact union of
# attempts. No AI, no network — a plain local file the user copies between
# devices. See tools/mcat_export_bundle.py and tools/mcat_import_perf.py.
# ---------------------------------------------------------------------------
BUNDLE_FORMAT = "mcat_perf_bundle"
BUNDLE_FORMAT_VERSION = 1


def _read_bundle_from_conn(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble a bundle dict from an already-migrated connection.

    Emits every column of ``perf_attempts`` (including the raw ``feature_json``
    string, preserved verbatim so it round-trips byte-for-byte) and every column
    of ``perf_questions``. Devices need not share the exact column set — import
    intersects columns — so this is forward/backward tolerant.
    """
    attempts = [
        dict(r)
        for r in conn.execute("SELECT * FROM perf_attempts ORDER BY id").fetchall()
    ]
    questions = [
        dict(r)
        for r in conn.execute("SELECT * FROM perf_questions ORDER BY id").fetchall()
    ]
    return {
        "format": BUNDLE_FORMAT,
        "format_version": BUNDLE_FORMAT_VERSION,
        "exported_at": int(time.time()),
        "questions": questions,
        "attempts": attempts,
    }


def export_bundle(db_path: str, out_path: str) -> dict[str, int]:
    """Write a portable sync bundle (JSON) for a sidecar DB. Returns counts.

    Unlike the strictly read-only eval export (``export_attempts``), this opens
    the DB read-write to guarantee every attempt has a stable ``uuid`` — that
    backfill is the same additive, idempotent migration that runs on every
    normal open (it never mutates real attempt data). Returns
    ``{"attempts", "questions"}``.
    """
    conn = _connect(db_path)
    try:
        bundle = _read_bundle_from_conn(conn)
    finally:
        conn.close()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)
    return {"attempts": len(bundle["attempts"]), "questions": len(bundle["questions"])}


def _validate_bundle(bundle: Any) -> None:
    if not isinstance(bundle, dict):
        raise ValueError("bundle must be a JSON object")
    fmt = bundle.get("format")
    if fmt != BUNDLE_FORMAT:
        raise ValueError(
            f"unrecognized bundle format {fmt!r} (expected {BUNDLE_FORMAT!r})"
        )
    ver = bundle.get("format_version")
    if ver != BUNDLE_FORMAT_VERSION:
        raise ValueError(
            f"unsupported bundle format_version {ver!r} "
            f"(this build reads version {BUNDLE_FORMAT_VERSION})"
        )


def _load_bundle(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        bundle = json.load(f)
    _validate_bundle(bundle)
    return bundle


def _attempt_fingerprint(a: dict[str, Any]) -> str:
    """Deterministic fallback key for legacy bundles whose rows predate ``uuid``.

    Hashes the immutable identifying fields of an attempt so an old export can
    still be deduped. New exports always carry a real ``uuid`` and never reach
    this path.
    """
    import hashlib

    payload = json.dumps(
        [
            a.get("question_id"),
            a.get("ts"),
            a.get("chosen_index"),
            a.get("correct"),
            a.get("time_seconds"),
            a.get("error_type"),
            a.get("interleaved"),
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return "fp_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _merge_questions(
    conn: sqlite3.Connection, questions: list[dict[str, Any]]
) -> int:
    """Insert bundle questions by ``id`` when missing. Never overwrites a local
    row (the bank is deterministic from source). Returns rows added."""
    if not questions:
        return 0
    local_cols = _existing_columns(conn, "perf_questions")
    existing = {
        r[0] for r in conn.execute("SELECT id FROM perf_questions").fetchall()
    }
    added = 0
    for q in questions:
        qid = q.get("id")
        if qid is None or qid in existing:
            continue
        cols = [c for c in q.keys() if c in local_cols]
        placeholders = ",".join("?" for _ in cols)
        conn.execute(
            f"INSERT OR IGNORE INTO perf_questions ({','.join(cols)}) "
            f"VALUES ({placeholders})",
            [q.get(c) for c in cols],
        )
        existing.add(qid)
        added += 1
    return added


def _merge_attempts(
    conn: sqlite3.Connection, attempts: list[dict[str, Any]]
) -> tuple[int, int]:
    """Union-merge bundle attempts, deduped by stable ``uuid``. The device-local
    autoincrement ``id`` is dropped on insert (a new local one is assigned).
    Returns ``(added, skipped)``."""
    if not attempts:
        return 0, 0
    local_cols = _existing_columns(conn, "perf_attempts")
    existing = {
        r[0]
        for r in conn.execute(
            "SELECT uuid FROM perf_attempts WHERE uuid IS NOT NULL"
        ).fetchall()
    }
    added = skipped = 0
    for a in attempts:
        key = a.get("uuid") or _attempt_fingerprint(a)
        if key in existing:
            skipped += 1
            continue
        # Insert every shared column EXCEPT the device-local id; force the dedup
        # key into the uuid column (covers legacy rows keyed by fingerprint).
        cols = [c for c in a.keys() if c in local_cols and c not in ("id", "uuid")]
        cols.append("uuid")
        placeholders = ",".join("?" for _ in cols)
        values = [a.get(c) for c in cols[:-1]] + [key]
        conn.execute(
            f"INSERT INTO perf_attempts ({','.join(cols)}) "
            f"VALUES ({placeholders})",
            values,
        )
        existing.add(key)
        added += 1
    return added, skipped


def merge_bundle(conn: sqlite3.Connection, bundle: dict[str, Any]) -> dict[str, int]:
    """Append-only union merge of a bundle into a migrated sidecar connection.

    Questions merged first (so attempt foreign keys resolve), then attempts
    deduped by ``uuid``. Idempotent and order-independent. Commits on success.
    Returns ``{"attempts_added", "attempts_skipped", "questions_added"}``.
    """
    _validate_bundle(bundle)
    q_added = _merge_questions(conn, bundle.get("questions") or [])
    added, skipped = _merge_attempts(conn, bundle.get("attempts") or [])
    conn.commit()
    return {
        "attempts_added": added,
        "attempts_skipped": skipped,
        "questions_added": q_added,
    }


def import_bundle(db_path: str, bundle_path: str) -> dict[str, int]:
    """Headless import: open a sidecar DB (migrating it), merge a bundle, close.

    Mirrors ``export_bundle`` for CLI/scripted sync without a live Collection.
    """
    bundle = _load_bundle(bundle_path)
    conn = _connect(db_path)
    try:
        return merge_bundle(conn, bundle)
    finally:
        conn.close()


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
    recheck_correct: Optional[bool] = None,
    fast_threshold: float = FAST_THRESHOLD_SECONDS,
) -> dict[str, Any]:
    """Infer the science error type + confidence from independent signals.

    Transparent bootstrap rule (spec "Inference rule"), ordered by signal
    strength. Commits to a diagnosis in the common case and reserves
    ``unresolved`` for the genuinely no-signal miss. Returns
    ``{"error_type", "confidence"}``.

    Demand-aware rebalance (2026-07-01) — the engine both over-abstained AND,
    after the first rebalance, over-applied ``application``: cold-start /
    gate-imputed ``M`` lands in the ambiguous ``[LOW_M, HIGH_M)`` band for
    almost every attempt (no review history → ``UNCOVERED_R0`` ~0.5), so a
    single ``mid-M`` branch fired for nearly all misses and emitted a *constant*
    ``application`` @ 0.55 — even for pure ``recall`` items, where there is no
    reasoning step to fail (a category error). The default is now
    **demand-aware** and confidence **varies with signal strength** instead of a
    flat 0.55.

    Content re-check probe (2026-07-02) — ``recheck_correct`` is the objective
    oracle from the IMMEDIATE post-miss recall probe (memory mode used to type a
    performance-mode miss). When provided it is evaluated **first** (branch 0),
    ahead of the behavioral/demand ladder, because it is the single strongest
    content signal:
      - probe **FAIL** (``recheck_correct is False``) → commit ``content_gap`` at
        **high** confidence (0.9): the content was demonstrably not retrievable
        even when cued right now. This is the strongest content-gap evidence.
      - probe **PASS** (``recheck_correct is True``) → **suppress ``content_gap``
        entirely** (content is available) and route by demand/behavior with
        confidence **raised** because the probe corroborates that content was
        held: trap landing → ``misread`` (0.8); applied/synthesis demand →
        ``application`` (≥0.75); otherwise (a held recall/un-typed item that was
        still missed) → ``misread`` (0.6, a careless slip). Per the team's
        IMMEDIATE-variant decision the PASS is treated as **solid** evidence and
        is NOT discounted for the "the MCQ just primed recall" bias; the only
        asymmetry kept is that FAIL is the single strongest signal.

    Decision order (first match wins):
    0. Content re-check probe outcome (``recheck_correct`` not None) — objective;
       see above. FAIL → ``content_gap`` @0.9; PASS → suppress ``content_gap``,
       route to ``application``/``misread`` at raised confidence.
    1. Chosen distractor carries an authored ``content_gap`` misconception →
       ``content_gap`` (M-independent: acting on a specific wrong belief is
       direct content evidence; spec's ``w_mis`` "keeps content_gap in play even
       at high M").
    2. Low ``M`` (content demonstrably not held) → ``content_gap``.
    3. Predictable-``trap`` landing → ``misread`` (execution slip); strongest
       when fast + high ``M``, still committed (moderate) otherwise.
    4. Content presumed held → **demand-aware default**:
       - ``application``/``synthesis`` demand → ``application`` (the expected
         gated-miss failure: had the pieces, deployment/transfer failed). High
         ``M`` + applied demand is the strongest (cross-system divergence,
         ≥0.7); mid/ambiguous ``M`` commits ``application`` at honestly lower
         confidence.
       - ``recall`` demand → ``content_gap`` (the failure is about the *fact*,
         not reasoning; on a pure-recall miss the student likely does not truly
         hold it). **NEVER ``application``** — a recall item has no reasoning
         step to fail. Confidence is weaker than a corroborated low-``M`` gap,
         and *lower still* when ``M`` is high (FSRS says held yet a recall fact
         was missed → contradictory, flag for the re-check probe).
       - unknown/missing demand → lean ``content_gap`` at low/moderate
         confidence when any ``M`` reading exists (the honest default for an
         un-typed item), NOT ``application``.
    5. Truly no signal (``M`` unavailable AND unknown demand AND no tag AND no
       trap) → ``unresolved`` → self-report fallback downstream.

    Honesty is preserved by *confidence*, not by abstaining: confidence is
    **strong** when ``M`` is clearly high or low, when a tag/trap is authored,
    or when demand strongly matches the label; **weaker** (and honestly so) when
    ``M`` is imputed/ambiguous. No single constant dominates the output. A
    low-``M`` or tagged miss still routes to ``content_gap`` so real content
    gaps are not silently absorbed. See MCAT/docs/ERROR-DIAGNOSIS-SPEC.md
    ("Inference rule").
    """
    if correct:
        return {"error_type": ERR_NONE, "confidence": 1.0}

    m = mastery
    demand = (cognitive_demand or "").lower()
    d = DEMAND_FACTOR.get(demand, 0.0)
    fast = time_seconds is not None and time_seconds < fast_threshold
    high_m = m is not None and m >= HIGH_M
    low_m = m is not None and m < LOW_M
    mid_m = m is not None and not high_m and not low_m
    demand_applied = demand in ("application", "synthesis")
    demand_recall = demand == "recall"

    # 0. Content re-check probe (objective oracle; IMMEDIATE variant). Evaluated
    #    BEFORE the behavioral/demand ladder because it is the single strongest
    #    content signal. FAIL = content not retrievable even when cued now →
    #    strongest content_gap signal (high confidence). PASS = content available
    #    → SUPPRESS content_gap and route by demand/behavior with confidence
    #    RAISED (the probe corroborates content was held). PASS is treated as
    #    SOLID (not discounted for MCQ priming) per the team's decision; the only
    #    asymmetry kept is that FAIL is the strongest signal.
    if recheck_correct is not None:
        if not recheck_correct:
            return {"error_type": ERR_CONTENT_GAP, "confidence": 0.9}
        # PASS → content demonstrably available → never content_gap.
        if is_trap:
            return {"error_type": ERR_MISREAD, "confidence": 0.8}
        if demand_applied:
            if m is not None:
                conf = round(max(0.75, _affinity_conf(m * d, 1.0 - m)), 3)
            else:
                conf = 0.75
            return {"error_type": ERR_APPLICATION, "confidence": conf}
        # A held recall/un-typed item that was still missed → careless slip.
        return {"error_type": ERR_MISREAD, "confidence": 0.6}

    # Content-gap confidence that VARIES with M: the lower the retrievability,
    # the more it corroborates a genuine gap; an imputed/ambiguous mid-M reading
    # stays honestly moderate. Bounded so a thin signal never fabricates
    # certainty. Used by the demand-aware default (branches 4b/4c).
    def _gap_conf() -> float:
        if m is None:
            return 0.45
        return round(min(0.6, max(0.4, 0.4 + (HIGH_M - m))), 3)

    # 1. Authored content-misconception on the chosen distractor. Independent of
    #    M (which wrong belief was acted on), so it commits content_gap even at
    #    high M; low M corroborates → higher confidence (high M weakly opposes).
    if has_content_tag:
        return {
            "error_type": ERR_CONTENT_GAP,
            "confidence": 0.8 if low_m else (0.7 if high_m else 0.65),
        }

    # 2. Low mastery: content demonstrably not held → content_gap. Confidence
    #    scales with how far M sits below the "clearly missing" line.
    if low_m:
        return {
            "error_type": ERR_CONTENT_GAP,
            "confidence": round(min(0.85, 0.6 + (LOW_M - m)), 3),
        }

    # 3. Predictable-trap landing → misread (execution slip). Strongest fast +
    #    high M; still committed (moderate) otherwise. Content is ruled in/out
    #    above, so a trap here rides on a held-content pick. Applies regardless
    #    of demand: a trap landing is an execution slip even on a recall item.
    if is_trap:
        if fast and high_m:
            conf = 0.75
        elif fast or high_m:
            conf = 0.6
        else:
            conf = 0.55
        return {"error_type": ERR_MISREAD, "confidence": conf}

    # 4. Content presumed held → DEMAND-AWARE default (never a demand-blind
    #    constant). What the item *tests* decides the failure mode:

    # 4a. application / synthesis demand → application (the expected gated-miss:
    #     had the pieces, the deployment/transfer step failed). High M + applied
    #     demand is the strongest signal (cross-system divergence, ≥0.7);
    #     mid/ambiguous/imputed M commits application at honestly lower conf.
    if demand_applied:
        if high_m:
            return {
                "error_type": ERR_APPLICATION,
                "confidence": round(max(0.7, _affinity_conf(m * d, 1.0 - m)), 3),
            }
        conf = 0.55 if m is None else round(min(0.68, 0.5 + 0.3 * m * d), 3)
        return {"error_type": ERR_APPLICATION, "confidence": conf}

    # 4b. recall demand → the FACT itself, not a reasoning step. A recall item
    #     has no application step to fail, so this is NEVER application; a
    #     recall miss (with no authored tag/trap and M not low — those routed
    #     above) defaults to content_gap: they likely don't truly hold it.
    #     Confidence is honest-moderate, and *lower* when M is high (FSRS says
    #     held yet a recall fact was missed → contradictory; flag for probe).
    if demand_recall:
        return {"error_type": ERR_CONTENT_GAP, "confidence": _gap_conf()}

    # 4c. unknown / missing demand → lean content_gap (the honest default for an
    #     un-typed item), NOT application, whenever we have any M reading. Only
    #     a fully dark miss (below) abstains.
    if m is not None:
        return {"error_type": ERR_CONTENT_GAP, "confidence": _gap_conf()}

    # 5. Truly no signal: M unavailable AND unknown demand AND no tag AND no
    #    trap (incl. fast-but-no-trap). Honest abstain → self-report downstream.
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


def _card_probe_text(col: Collection, cid: int) -> Optional[tuple[int, str, str]]:
    """Render a backing card's front/back for use as a recall probe.

    Returns ``(card_id, front_html, back_html)`` or ``None`` if the card can't be
    resolved/rendered. Uses the card's normal question/answer render so a cloze
    prompt shows the deletion blank (a genuine recall cue).
    """
    try:
        out = col.get_card(cid).render_output()
        front = out.question_text
        back = out.answer_text
    except Exception:
        return None
    if not front:
        return None
    return cid, front, back


def resolve_probe_target(
    col: Collection, question: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Resolve the sub-concept to re-check for a missed SCIENCE question.

    Returns a dict describing the probe, or ``None`` to SKIP the probe (CARS, or
    a science item with no resolvable backing concept — the inference path then
    runs unchanged). Resolution order:

    1. **Real backing card** (preferred): invert ``supports_question`` → backing
       card ids and probe the weakest link first (lowest current FSRS-R; a card
       with no memory state sorts first so unknown/never-reviewed prerequisites
       are checked). The resolved real collection ``card_id`` is stored in
       ``perf_attempts.recheck_card_id``. ``{"card_id": int, "front", "back",
       "fallback": False}``.
    2. **Fallback prompt** (no card cleanly resolvable at runtime): derive a
       plain concept-recall prompt from the question's backing topic so a probe
       still fires and *what* was probed is recorded. ``card_id`` is ``None``
       (nothing real to store) and ``fallback`` is ``True``; the reveal reuses
       the question's static explanation when present.
    3. **Skip** (CARS or genuinely unmapped): ``None``.
    """
    if question.get("section") == CARS_SECTION:
        return None
    try:
        cids = backing_card_ids_for_question(col, question["id"])
    except Exception:
        cids = []
    if cids:
        def _sort_key(cid: int) -> float:
            r = card_retrievability(col, cid)
            # Unknown R (new/unreviewed) → probe first (weakest-link intent).
            return r if r is not None else -1.0

        for cid in sorted(cids, key=_sort_key):
            rendered = _card_probe_text(col, cid)
            if rendered is not None:
                _, front, back = rendered
                return {
                    "card_id": cid,
                    "front": front,
                    "back": back,
                    "fallback": False,
                }
    # Fallback: no resolvable backing card, but a science item always has a
    # backing concept (its topic). Probe the concept as free recall.
    topics = backing_topics_for_question(
        col, question["id"], question.get("topic_id")
    )
    concept = topics[0] if topics else question.get("topic_id")
    if not concept:
        return None
    reveal = (question.get("explanation") or "").strip()
    return {
        "card_id": None,
        "front": (
            "Recall the core concept this question tested "
            f"(topic: <b>{concept}</b>). When you have it, grade yourself."
        ),
        "back": reveal,
        "fallback": True,
    }


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
        # Content re-check probe outcome for the current miss (immediate variant),
        # logged alongside the resolved error. Stay None when no probe fired.
        self._pending_recheck_card_id: Optional[int] = None
        self._pending_recheck_correct: Optional[bool] = None
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

    def application_practice_set(
        self, *, n: int = N_APPLICATION_PRACTICE
    ) -> list[dict[str, Any]]:
        """Selected remediation items for the CURRENT miss (``application`` route).

        Reads the ISOLATED remediation pool + seen-set from the store and applies
        the deterministic ``select_application_practice`` algorithm. The
        just-missed concept is the missed item's own ``concept`` slug when it
        carries one (main-bank items usually don't → ``None``, so no concept is
        excluded and any sibling item qualifies). Returns ``[]`` (→ generic
        fallback) when the pool has no eligible items. Never touches scored state.
        """
        q = self.current
        topic_id = q.get("topic_id")
        if not topic_id:
            return []
        try:
            pool = self.store.remediation_items_for_topic(topic_id)
            seen = self.store.seen_remediation_ids()
        except Exception:
            return []
        return select_application_practice(
            pool,
            topic_id=topic_id,
            missed_concept=q.get("concept"),
            seen_ids=seen,
            n=n,
        )

    def probe_target(self) -> Optional[dict[str, Any]]:
        """Resolve the content re-check probe target for the current miss.

        Returns the probe descriptor (see ``resolve_probe_target``) or ``None``
        to skip the probe (CARS / unmapped / not awaiting an error). The dialog
        calls this immediately after a wrong answer to decide whether to show the
        probe before the diagnosis.
        """
        if not self._awaiting_error:
            return None
        try:
            return resolve_probe_target(self.store.col, self.current)
        except Exception:
            return None

    def record_probe_outcome(
        self, correct: bool, *, card_id: Optional[int] = None
    ) -> dict[str, Any]:
        """Record the immediate re-check probe result and RE-RUN the inference
        with it, so the on-screen hypothesis becomes probe-informed.

        ``correct`` is the objective probe outcome (I-knew-it / I-didn't).
        ``card_id`` is the real backing card probed (stored in
        ``recheck_card_id``); ``None`` for the fallback concept probe. Returns the
        updated pending inference. Must be awaiting error classification.
        """
        if not self._awaiting_error:
            raise RuntimeError("no wrong answer awaiting a re-check probe")
        self._pending_recheck_card_id = card_id
        self._pending_recheck_correct = correct
        q = self.current
        is_trap, has_content_tag = self._choice_flags(q, self._pending_choice)
        self._pending_inference = infer_error_type(
            correct=False,
            cognitive_demand=q.get("cognitive_demand"),
            mastery=self._pending_mastery,
            time_seconds=self._pending_time,
            is_trap=is_trap,
            has_content_tag=has_content_tag,
            recheck_correct=correct,
        )
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
    def _choice_signals(
        question: dict[str, Any], choice_idx: Optional[int]
    ) -> dict[str, Any]:
        """Authored choice_diagnosis signals for the chosen option.

        Returns ``{is_trap, trap_type, has_content_tag, maps_to, misconception}``.
        choice_diagnosis bulk authoring is deferred, so for most items this is the
        empty/false default and inference degrades to cross-system + timing.
        """
        signals: dict[str, Any] = {
            "is_trap": False,
            "trap_type": None,
            "has_content_tag": False,
            "maps_to": None,
            "misconception": None,
        }
        cd = question.get("choice_diagnosis")
        if (
            choice_idx is None
            or not isinstance(cd, list)
            or not (0 <= choice_idx < len(cd))
        ):
            return signals
        entry = cd[choice_idx]
        if not entry:
            return signals
        trap = entry.get("trap")
        signals["is_trap"] = bool(trap)
        # trap is authored as a type string (negation | unit | inverse | ...).
        signals["trap_type"] = trap if isinstance(trap, str) else None
        maps = entry.get("maps_to")
        signals["maps_to"] = maps
        signals["has_content_tag"] = maps == ERR_CONTENT_GAP or (
            isinstance(maps, list) and ERR_CONTENT_GAP in maps
        )
        signals["misconception"] = entry.get("misconception")
        return signals

    @classmethod
    def _choice_flags(
        cls, question: dict[str, Any], choice_idx: int
    ) -> tuple[bool, bool]:
        """(is_trap, has_content_tag) — thin view over ``_choice_signals``."""
        s = cls._choice_signals(question, choice_idx)
        return (s["is_trap"], s["has_content_tag"])

    def _feature_vector(
        self,
        question: dict[str, Any],
        *,
        choice_idx: Optional[int],
        correct: bool,
        time_seconds: Optional[float],
        mastery: Optional[float],
        inference: Optional[dict[str, Any]],
        error_source: Optional[str],
        self_report_error_type: Optional[str],
    ) -> dict[str, Any]:
        """Assemble the full signal vector genuinely observed at classify time.

        Captures what is actually available here, now INCLUDING the immediate
        content re-check probe outcome (``recheck_card_id`` / ``recheck_correct``)
        when a probe fired for this miss (None otherwise). The select-then-confirm
        churn fields (first_choice_index, answer_changes) remain NOT included/faked
        — they still await select-then-confirm wiring (see MCAT/docs/LOOSE-ENDS.md
        "Deferred v2 UX"). Stored via log_attempt with ensure_ascii=False.
        """
        signals = self._choice_signals(question, choice_idx)
        inference = inference or {}
        return {
            "schema": FEATURE_SCHEMA_VERSION,
            "question_id": question.get("id"),
            "topic_id": question.get("topic_id"),
            "section": question.get("section"),
            "split": question.get("split"),
            "chosen_index": choice_idx,
            "correct": correct,
            "time_seconds": time_seconds,
            "mastery_snapshot": mastery,
            "cognitive_demand": question.get("cognitive_demand"),
            "is_trap": signals["is_trap"],
            "trap_type": signals["trap_type"],
            "has_content_tag": signals["has_content_tag"],
            "maps_to": signals["maps_to"],
            "misconception": signals["misconception"],
            "inferred_error_type": inference.get("error_type"),
            "inferred_confidence": inference.get("confidence"),
            "recheck_card_id": self._pending_recheck_card_id,
            "recheck_correct": self._pending_recheck_correct,
            "error_source": error_source,
            "self_report_error_type": self_report_error_type,
            "interleaved": self.interleaved,
        }

    def answer(self, choice_idx: int, *, time_seconds: Optional[float] = None) -> bool:
        """Grade a choice. Correct → logged immediately. Wrong → awaits error type.

        Returns True if correct. Raises if a prior wrong answer is unclassified.
        """
        if self._awaiting_error:
            raise RuntimeError("classify the previous error before answering again")
        q = self.current
        is_correct = LETTERS[choice_idx] == q["correct"]
        if is_correct:
            # Correct attempts still capture the observable signal vector (M,
            # timing, choice signals) so the eval pipeline has a symmetric row.
            m = self._mastery_for(q)
            inference = {"error_type": ERR_NONE, "confidence": 1.0}
            feature = self._feature_vector(
                q,
                choice_idx=choice_idx,
                correct=True,
                time_seconds=time_seconds,
                mastery=m,
                inference=inference,
                error_source=None,
                self_report_error_type=None,
            )
            self.store.log_attempt(
                q["id"],
                correct=True,
                time_seconds=time_seconds,
                interleaved=self.interleaved,
                chosen_index=choice_idx,
                mastery_snapshot=m,
                inferred_error_type=ERR_NONE,
                inferred_confidence=1.0,
                error_source=None,
                feature_json=feature,
            )
            self.answered += 1
            self.correct += 1
        else:
            self._awaiting_error = True
            self._pending_time = time_seconds
            self._pending_choice = choice_idx
            # Fresh miss → no probe recorded yet (set later if one fires).
            self._pending_recheck_card_id = None
            self._pending_recheck_correct = None
            # Compute the v2 inference now (behavior + cross-system M), stash it
            # to log alongside the resolved self-report type.
            m = self._mastery_for(q)
            is_trap, has_content_tag = self._choice_flags(q, choice_idx)
            self._pending_mastery = m
            if q.get("section") == CARS_SECTION:
                # CARS is a SEPARATE diagnosis track (skill-archetype accuracy +
                # pacing) and must NOT use the science 3-bucket engine — content_gap
                # can't occur (no outside knowledge) and there is no mastery oracle.
                # Abstain here so the dialog falls to self-report, rather than the
                # rebalanced science default mislabeling a CARS miss as application.
                # See MCAT/docs/ERROR-DIAGNOSIS-SPEC.md ("Two tracks").
                self._pending_inference = {
                    "error_type": ERR_UNRESOLVED,
                    "confidence": 0.0,
                }
            else:
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
        probe_informed = self._pending_recheck_correct is not None
        error_source = self._reconcile_source(
            error_type, inferred, probe_informed=probe_informed
        )
        feature = self._feature_vector(
            q,
            choice_idx=self._pending_choice,
            correct=False,
            time_seconds=self._pending_time,
            mastery=self._pending_mastery,
            inference=inf,
            error_source=error_source,
            self_report_error_type=error_type,
        )
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
            recheck_card_id=self._pending_recheck_card_id,
            recheck_correct=self._pending_recheck_correct,
            error_source=error_source,
            feature_json=feature,
        )
        self.answered += 1
        self._awaiting_error = False
        self._pending_time = None
        self._pending_choice = None
        self._pending_inference = None
        self._pending_mastery = None
        self._pending_recheck_card_id = None
        self._pending_recheck_correct = None

    @staticmethod
    def _reconcile_source(
        self_report: str, inferred: str, *, probe_informed: bool = False
    ) -> str:
        """Consistency channel between the inference and the self-report.

        - inference abstained (unresolved/none) → ``self_report``
        - self-report agrees with inference (folding the legacy enum) →
          ``inferred_confirmed``, or ``inference+recheck`` when an objective
          content re-check probe informed the (agreed) diagnosis
        - otherwise → ``self_report_override``
        """
        if inferred in (ERR_UNRESOLVED, ERR_NONE):
            return ERROR_SOURCE_SELF_REPORT
        folded = LEGACY_ERROR_FOLD.get(self_report, self_report)
        if folded == inferred:
            return ERROR_SOURCE_RECHECK if probe_informed else ERROR_SOURCE_INFERRED
        return ERROR_SOURCE_OVERRIDE

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
