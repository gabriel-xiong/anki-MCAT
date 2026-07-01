# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import json
import random

from anki import mcat_perf
from anki.mcat_perf import (
    PerformanceSession,
    PerfStore,
    export_attempts,
    order_questions,
    read_attempts,
)
from tests.shared import getEmptyCol

# Every key the observed signal vector must carry (see
# PerformanceSession._feature_vector).
EXPECTED_FEATURE_KEYS = {
    "schema",
    "question_id",
    "topic_id",
    "section",
    "split",
    "chosen_index",
    "correct",
    "time_seconds",
    "mastery_snapshot",
    "cognitive_demand",
    "is_trap",
    "trap_type",
    "has_content_tag",
    "maps_to",
    "misconception",
    "inferred_error_type",
    "inferred_confidence",
    "error_source",
    "self_report_error_type",
    "interleaved",
}

SAMPLE_QUESTIONS = [
    {
        "id": "q_dev_001",
        "stem": "GTP or ATP is produced during the conversion of ________.",
        "choices": [
            "isocitrate into α-ketoglutarate",
            "succinyl CoA into succinate",
            "fumarate into malate",
            "malate into oxaloacetate",
        ],
        "correct": "B",
        "topic_id": "bb_citric_acid",
        "section": "BB",
        "skill": "2",
        "source_name": "OpenStax Biology 2e",
        "source_url": "https://openstax.org/books/biology-2e/pages/7-review-questions",
        "source_location": "Ch. 7 Review Q9",
        "split": "dev",
    },
    {
        "id": "q_dev_006",
        "stem": "Which of the following will increase the percent of NH3 converted?",
        "choices": ["addition of NaOH", "addition of HCl", "NH4Cl", "NH3 gas"],
        "correct": "B",
        "topic_id": "cp_acids_bases",
        "section": "CP",
        "skill": "2",
        "source_name": "OpenStax Chemistry 2e",
        "source_url": "https://openstax.org/books/chemistry-2e/pages/14-exercises",
        "source_location": "Ch. 14 Exercises Q48",
        "split": "dev",
    },
    {
        "id": "q_cars_001",
        "stem": "A CARS-style passage question.",
        "choices": ["A", "B", "C", "D"],
        "correct": "C",
        "topic_id": "cars_passage",
        "section": "CARS",
        "skill": "3",
        "source_name": "Public domain passage",
        "source_url": "",
        "source_location": "n/a",
        "split": "dev",
    },
]


def test_sidecar_path_is_next_to_collection():
    col = getEmptyCol()
    path = mcat_perf.sidecar_path(col)
    assert path.endswith(".mcat_perf.db")
    assert path != col.path


def test_upsert_is_idempotent():
    col = getEmptyCol()
    with PerfStore(col) as store:
        assert store.upsert_questions(SAMPLE_QUESTIONS) == 3
        assert store.question_count() == 3
        # second load must not duplicate
        store.upsert_questions(SAMPLE_QUESTIONS)
        assert store.question_count() == 3


def test_eligibility_gate_and_cars_bypass():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)

        # no topics unlocked -> only CARS is eligible
        eligible = store.eligible_questions(set())
        ids = {q["id"] for q in eligible}
        assert ids == {"q_cars_001"}

        # unlock one science topic -> that topic + CARS
        eligible = store.eligible_questions({"bb_citric_acid"})
        ids = {q["id"] for q in eligible}
        assert ids == {"q_dev_001", "q_cars_001"}


def test_log_attempt_and_accuracy():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)

        # no attempts -> honest abstain (accuracy None)
        assert store.accuracy()["accuracy"] is None
        assert store.attempt_count() == 0

        store.log_attempt("q_dev_001", correct=True, time_seconds=12.0)
        store.log_attempt(
            "q_dev_001", correct=False, error_type="content_gap", time_seconds=20.0
        )
        store.log_attempt("q_dev_006", correct=True, interleaved=True)

        assert store.attempt_count() == 3

        overall = store.accuracy()
        assert overall["attempts"] == 3
        assert overall["correct"] == 2
        assert abs(overall["accuracy"] - 2 / 3) < 1e-9

        topic = store.accuracy("bb_citric_acid")
        assert topic["attempts"] == 2
        assert topic["correct"] == 1
        assert topic["accuracy"] == 0.5


def test_reset_attempts_clears_attempts_keeps_questions():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)

        store.log_attempt("q_dev_001", correct=True, time_seconds=12.0)
        store.log_attempt(
            "q_dev_001", correct=False, error_type="content_gap", time_seconds=20.0
        )
        assert store.attempt_count() == 2
        assert store.question_count() == 3

        # reset_attempts returns how many it cleared, empties perf_attempts,
        # and leaves the loaded bank (perf_questions) intact
        assert store.reset_attempts() == 2
        assert store.attempt_count() == 0
        assert store.question_count() == 3
        assert store.accuracy()["accuracy"] is None

        # idempotent: resetting again clears nothing
        assert store.reset_attempts() == 0


def test_choices_roundtrip():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        q = store.eligible_questions({"bb_citric_acid"})
        first = next(x for x in q if x["id"] == "q_dev_001")
        assert first["choices"][1] == "succinyl CoA into succinate"
        assert first["correct"] == "B"


def test_order_blocked_groups_by_topic():
    qs = [
        {"id": "a", "topic_id": "t2"},
        {"id": "b", "topic_id": "t1"},
        {"id": "c", "topic_id": "t2"},
        {"id": "d", "topic_id": "t1"},
    ]
    ordered = order_questions(qs, interleaved=False)
    topics = [q["topic_id"] for q in ordered]
    # all of t1 before all of t2 (grouped, not interleaved)
    assert topics == ["t1", "t1", "t2", "t2"]


def test_order_interleaved_is_deterministic_with_rng():
    qs = [{"id": str(i), "topic_id": f"t{i}"} for i in range(6)]
    a = order_questions(qs, interleaved=True, rng=random.Random(0))
    b = order_questions(qs, interleaved=True, rng=random.Random(0))
    assert [q["id"] for q in a] == [q["id"] for q in b]


def _session(col_store):
    col_store.upsert_questions(SAMPLE_QUESTIONS)
    qs = col_store.eligible_questions({"bb_citric_acid", "cp_acids_bases"})
    return PerformanceSession(col_store, qs, interleaved=False)


def test_session_correct_answer_logs_and_advances():
    col = getEmptyCol()
    with PerfStore(col) as store:
        sess = _session(store)
        correct_idx = sess.correct_index()

        assert sess.answer(correct_idx) is True
        assert sess.awaiting_error_type is False
        assert sess.summary() == {"answered": 1, "correct": 1, "accuracy": 1.0}
        assert store.attempt_count() == 1

        sess.advance()
        assert sess.index == 1


def test_session_wrong_answer_requires_error_classification():
    col = getEmptyCol()
    with PerfStore(col) as store:
        sess = _session(store)
        correct_idx = sess.correct_index()
        wrong_idx = (correct_idx + 1) % 4

        assert sess.answer(wrong_idx) is False
        assert sess.awaiting_error_type is True
        # nothing logged until classified
        assert store.attempt_count() == 0

        # cannot advance or answer again until classified
        import pytest

        with pytest.raises(RuntimeError):
            sess.advance()
        with pytest.raises(RuntimeError):
            sess.answer(correct_idx)

        sess.classify_error("reasoning")
        assert sess.awaiting_error_type is False
        assert store.attempt_count() == 1
        assert sess.summary()["correct"] == 0

        row = store.conn.execute(
            "SELECT error_type, correct FROM perf_attempts"
        ).fetchone()
        assert row["error_type"] == "reasoning"
        assert row["correct"] == 0


def test_session_walks_to_completion():
    col = getEmptyCol()
    with PerfStore(col) as store:
        sess = _session(store)
        total = sess.total
        # bb_citric_acid + cp_acids_bases dev questions + CARS (always eligible)
        assert total == 3

        while not sess.finished:
            sess.answer(sess.correct_index())
            sess.advance()

        assert sess.finished
        assert store.attempt_count() == total
        assert sess.summary()["accuracy"] == 1.0


# ---------------------------------------------------------------------------
# v2 error-diagnosis: content-held score M + 3-bucket inference
# ---------------------------------------------------------------------------
def test_content_held_score_geometric_mean():
    # geometric mean of [0.9, 0.4] = sqrt(0.36) = 0.6
    m = mcat_perf.content_held_score([0.9, 0.4])
    assert abs(m - 0.6) < 1e-9
    # single value passes through
    assert abs(mcat_perf.content_held_score([0.81]) - 0.81) < 1e-9


def test_content_held_score_imputes_r0_for_missing_and_zero():
    # None and <=0 are imputed to the conservative prior r0
    m = mcat_perf.content_held_score([None, 0.0], r0=0.5)
    assert abs(m - 0.5) < 1e-9
    # nothing to score -> None (honest)
    assert mcat_perf.content_held_score([]) is None


def test_fsrs_retrievability_curve_endpoints():
    S = 10.0  # stability in days
    # t = 0 -> R = 1.0 (just reviewed)
    assert abs(mcat_perf.fsrs_retrievability(S, 0.0, 0.5) - 1.0) < 1e-9
    # t = S -> R = 0.9 exactly (FSRS 90%-retention convention)
    assert abs(mcat_perf.fsrs_retrievability(S, S * 86400, 0.5) - 0.9) < 1e-6
    # monotonically decreasing beyond S
    assert mcat_perf.fsrs_retrievability(S, 4 * S * 86400, 0.5) < 0.9


def test_fsrs_retrievability_sign_robust_and_default_decay():
    S = 10.0
    r_pos = mcat_perf.fsrs_retrievability(S, S * 86400, 0.5)
    r_neg = mcat_perf.fsrs_retrievability(S, S * 86400, -0.5)
    r_none = mcat_perf.fsrs_retrievability(S, S * 86400, None)
    assert abs(r_pos - r_neg) < 1e-9  # magnitude taken, sign can't flip the curve
    assert abs(r_none - r_pos) < 1e-9  # None -> FSRS-5 default decay


def test_fsrs_retrievability_no_memory_state_is_none():
    # non-positive stability = no usable memory state -> None (imputed downstream)
    assert mcat_perf.fsrs_retrievability(0.0, 100.0, 0.5) is None
    assert mcat_perf.fsrs_retrievability(-1.0, 100.0, 0.5) is None


def test_infer_correct_is_none():
    out = mcat_perf.infer_error_type(correct=True)
    assert out["error_type"] == mcat_perf.ERR_NONE
    assert out["confidence"] == 1.0


def test_infer_mastered_synthesis_miss_is_application():
    # high M + application/synthesis demand + missed -> applied reasoning
    out = mcat_perf.infer_error_type(
        correct=False, cognitive_demand="synthesis", mastery=0.9, time_seconds=30.0
    )
    assert out["error_type"] == mcat_perf.ERR_APPLICATION
    assert out["confidence"] >= 0.8  # high confidence (clear margin)


def test_infer_low_mastery_miss_is_content_gap():
    out = mcat_perf.infer_error_type(
        correct=False, cognitive_demand="application", mastery=0.2
    )
    assert out["error_type"] == mcat_perf.ERR_CONTENT_GAP
    assert out["confidence"] > 0.5


def test_infer_fast_mastered_trap_is_misread():
    out = mcat_perf.infer_error_type(
        correct=False,
        cognitive_demand="recall",
        mastery=0.85,
        time_seconds=3.0,
        is_trap=True,
    )
    assert out["error_type"] == mcat_perf.ERR_MISREAD


def test_infer_fast_alone_is_not_misread():
    # fast + wrong but NOT a trap: must never be called misread (that needs a
    # trap landing). It is also NOT application here: this is a RECALL item, and
    # a recall miss has no reasoning step to fail — the demand-aware default
    # routes it to content_gap (the fact itself). A high-M recall miss is
    # contradictory (FSRS says held), so confidence is honestly low.
    out = mcat_perf.infer_error_type(
        correct=False,
        cognitive_demand="recall",
        mastery=0.85,
        time_seconds=3.0,
        is_trap=False,
    )
    assert out["error_type"] != mcat_perf.ERR_MISREAD
    assert out["error_type"] == mcat_perf.ERR_CONTENT_GAP
    assert out["confidence"] < 0.55  # low: high-M recall miss is contradictory


def test_infer_ambiguous_mastery_commits_to_application():
    # Middling M on an application/synthesis item now COMMITS to application
    # (content presumed held via the perf gate; "content available but not
    # deployed" is the expected MCAT miss) at moderate, honest confidence —
    # rather than over-abstaining. See the rebalance note in infer_error_type.
    out = mcat_perf.infer_error_type(
        correct=False, cognitive_demand="application", mastery=0.55, time_seconds=20.0
    )
    assert out["error_type"] == mcat_perf.ERR_APPLICATION
    assert 0.5 <= out["confidence"] < 0.8  # moderate, not fabricated-high


def test_infer_no_signal_miss_is_unresolved():
    # The genuinely truly-dark case is preserved as an honest abstain: no
    # mastery signal at all, UNKNOWN/missing demand, no trap, no content tag.
    # (A recall-demand miss is no longer dark — demand alone routes it to
    # content_gap; see test_infer_recall_miss_is_never_application.)
    out = mcat_perf.infer_error_type(
        correct=False, cognitive_demand=None, mastery=None, is_trap=False
    )
    assert out["error_type"] == mcat_perf.ERR_UNRESOLVED


def test_infer_recall_miss_is_never_application():
    # A pure-recall miss (e.g. "which enzyme joins Okazaki fragments?") has no
    # reasoning step to fail, so it must NEVER be labelled application — even at
    # the cold-start imputed mid-M (~0.5) where the old engine emitted a
    # constant "application @ 0.55". The demand-aware default routes it to
    # content_gap (the fact itself).
    for m in (None, 0.5, 0.85):
        out = mcat_perf.infer_error_type(
            correct=False, cognitive_demand="recall", mastery=m, time_seconds=20.0
        )
        assert out["error_type"] != mcat_perf.ERR_APPLICATION
        assert out["error_type"] == mcat_perf.ERR_CONTENT_GAP


def test_infer_confidence_is_not_a_single_constant():
    # Regression for the "everything reads 55%" bug: confidence must VARY with
    # signal strength, not collapse to one number across differing-signal misses.
    confs = {
        # recall, cold-start imputed mid-M -> content_gap (moderate)
        mcat_perf.infer_error_type(
            correct=False, cognitive_demand="recall", mastery=0.5
        )["confidence"],
        # recall, high-M (contradictory) -> content_gap (low)
        mcat_perf.infer_error_type(
            correct=False, cognitive_demand="recall", mastery=0.85
        )["confidence"],
        # application, mid-M -> application (moderate)
        mcat_perf.infer_error_type(
            correct=False, cognitive_demand="application", mastery=0.55
        )["confidence"],
        # synthesis, high-M -> application (strong, cross-system divergence)
        mcat_perf.infer_error_type(
            correct=False, cognitive_demand="synthesis", mastery=0.9
        )["confidence"],
        # low-M -> content_gap (strong)
        mcat_perf.infer_error_type(
            correct=False, cognitive_demand="application", mastery=0.15
        )["confidence"],
    }
    # at least four distinct confidence values across these five cases (no single
    # constant dominating the output)
    assert len(confs) >= 4, confs


def test_infer_application_requires_content_presence():
    # application demand but LOW M -> content_gap, NOT application (honesty gate)
    out = mcat_perf.infer_error_type(
        correct=False, cognitive_demand="application", mastery=0.25
    )
    assert out["error_type"] == mcat_perf.ERR_CONTENT_GAP


def test_infer_content_tag_used_when_no_mastery():
    # no mastery signal, but the chosen distractor encodes a content misconception
    out = mcat_perf.infer_error_type(
        correct=False, cognitive_demand=None, mastery=None, has_content_tag=True
    )
    assert out["error_type"] == mcat_perf.ERR_CONTENT_GAP


def test_migration_adds_v1_essential_columns():
    col = getEmptyCol()
    with PerfStore(col) as store:
        qcols = mcat_perf._existing_columns(store.conn, "perf_questions")
        assert "cognitive_demand" in qcols
        acols = mcat_perf._existing_columns(store.conn, "perf_attempts")
        for c in (
            "chosen_index",
            "mastery_snapshot",
            "inferred_error_type",
            "inferred_confidence",
            "recheck_card_id",
            "recheck_correct",
            "error_source",
            "feature_json",
        ):
            assert c in acols, c
        # deferred columns are NOT added (need select-then-confirm churn logging
        # + re-check probe UI the dialog doesn't provide yet)
        for c in ("first_choice_index", "answer_changes", "recheck_timing"):
            assert c not in acols, c


def test_log_attempt_persists_inference_columns():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        store.log_attempt(
            "q_dev_001",
            correct=False,
            error_type="content_gap",
            chosen_index=2,
            mastery_snapshot=0.42,
            inferred_error_type="content_gap",
            inferred_confidence=0.8,
            error_source="inferred_confirmed",
        )
        row = store.conn.execute(
            "SELECT chosen_index, mastery_snapshot, inferred_error_type, "
            "inferred_confidence, error_source FROM perf_attempts"
        ).fetchone()
        assert row["chosen_index"] == 2
        assert abs(row["mastery_snapshot"] - 0.42) < 1e-9
        assert row["inferred_error_type"] == "content_gap"
        assert abs(row["inferred_confidence"] - 0.8) < 1e-9
        assert row["error_source"] == "inferred_confirmed"


def test_cognitive_demand_roundtrips_through_store():
    col = getEmptyCol()
    q = dict(SAMPLE_QUESTIONS[0], cognitive_demand="synthesis")
    with PerfStore(col) as store:
        store.upsert_questions([q])
        loaded = store.eligible_questions({"bb_citric_acid"})[0]
        assert loaded["cognitive_demand"] == "synthesis"


# choice_diagnosis (authored distractor tags): storage passthrough + flags.
# id q_dev_001 correct="B" (index 1). index 0 = trap, index 2 = content_gap,
# index 3 = explicit null entry (must survive the round-trip as None).
QUESTION_WITH_DIAGNOSIS = dict(
    SAMPLE_QUESTIONS[0],
    cognitive_demand="recall",
    choice_diagnosis=[
        {"maps_to": None, "trap": "negation", "misconception": "flips the sign"},
        None,
        {"maps_to": "content_gap", "misconception": "confuses the intermediate"},
        None,
    ],
)


def test_choice_diagnosis_roundtrips_through_store():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions([QUESTION_WITH_DIAGNOSIS])
        loaded = store.eligible_questions({"bb_citric_acid"})[0]
        cd = loaded["choice_diagnosis"]
        assert isinstance(cd, list) and len(cd) == 4
        # structure intact
        assert cd[0]["trap"] == "negation"
        assert cd[0]["misconception"] == "flips the sign"
        assert cd[2]["maps_to"] == "content_gap"
        # null entries preserved at the correct indices
        assert cd[1] is None
        assert cd[3] is None


def test_choice_diagnosis_absent_stores_null():
    col = getEmptyCol()
    with PerfStore(col) as store:
        # SAMPLE_QUESTIONS[0] has no choice_diagnosis key
        store.upsert_questions([SAMPLE_QUESTIONS[0]])
        loaded = store.eligible_questions({"bb_citric_acid"})[0]
        assert loaded["choice_diagnosis"] is None
        raw = store.conn.execute(
            "SELECT choice_diagnosis FROM perf_questions WHERE id = ?",
            ("q_dev_001",),
        ).fetchone()
        assert raw["choice_diagnosis"] is None


def test_migration_adds_choice_diagnosis_column():
    col = getEmptyCol()
    with PerfStore(col) as store:
        qcols = mcat_perf._existing_columns(store.conn, "perf_questions")
        assert "choice_diagnosis" in qcols


def test_choice_flags_trap_and_content_and_null():
    q = QUESTION_WITH_DIAGNOSIS
    # trap choice -> is_trap True
    is_trap, has_content = PerformanceSession._choice_flags(q, 0)
    assert is_trap is True and has_content is False
    # content_gap choice -> has_content_tag True
    is_trap, has_content = PerformanceSession._choice_flags(q, 2)
    assert is_trap is False and has_content is True
    # null entry -> (False, False)
    assert PerformanceSession._choice_flags(q, 3) == (False, False)


def test_choice_flags_missing_diagnosis_is_false():
    assert PerformanceSession._choice_flags(SAMPLE_QUESTIONS[0], 0) == (False, False)


def test_trap_choice_yields_misread_end_to_end():
    # Full runtime path: upsert -> eligible_questions -> _choice_flags feeds
    # infer_error_type. With high M + fast + trap choice, the engine now yields
    # misread (previously impossible because choice_diagnosis was dropped).
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions([QUESTION_WITH_DIAGNOSIS])
        loaded = store.eligible_questions({"bb_citric_acid"})[0]
        is_trap, has_content = PerformanceSession._choice_flags(loaded, 0)
        assert is_trap is True
        out = mcat_perf.infer_error_type(
            correct=False,
            cognitive_demand=loaded.get("cognitive_demand"),
            mastery=0.85,
            time_seconds=3.0,
            is_trap=is_trap,
            has_content_tag=has_content,
        )
        assert out["error_type"] == mcat_perf.ERR_MISREAD


def test_content_gap_choice_yields_content_gap_end_to_end():
    # content_gap distractor tag drives the content axis when no mastery signal.
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions([QUESTION_WITH_DIAGNOSIS])
        loaded = store.eligible_questions({"bb_citric_acid"})[0]
        is_trap, has_content = PerformanceSession._choice_flags(loaded, 2)
        assert has_content is True
        out = mcat_perf.infer_error_type(
            correct=False,
            cognitive_demand=None,
            mastery=None,
            is_trap=is_trap,
            has_content_tag=has_content,
        )
        assert out["error_type"] == mcat_perf.ERR_CONTENT_GAP


def test_session_cars_miss_abstains_not_science_bucket():
    # CARS is a separate track: a CARS miss must NOT be labelled with a science
    # bucket (content_gap/application/misread). Post-rebalance the science engine
    # commits aggressively, so the session must short-circuit CARS to unresolved.
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        qs = store.eligible_questions(set())  # only CARS is eligible w/o unlocks
        assert [q["section"] for q in qs] == ["CARS"]
        sess = PerformanceSession(store, qs, interleaved=False)
        wrong = (sess.correct_index() + 1) % 4
        sess.answer(wrong, time_seconds=20.0)
        assert sess.pending_inference["error_type"] == mcat_perf.ERR_UNRESOLVED
        sess.classify_error("application")
        row = store.conn.execute(
            "SELECT inferred_error_type, error_source FROM perf_attempts"
        ).fetchone()
        assert row["inferred_error_type"] == "unresolved"
        assert row["error_source"] == "self_report"


def test_session_wrong_answer_logs_inference_and_source():
    col = getEmptyCol()
    q = dict(SAMPLE_QUESTIONS[0], cognitive_demand="application")
    with PerfStore(col) as store:
        store.upsert_questions([q])
        qs = store.eligible_questions({"bb_citric_acid"})
        sess = PerformanceSession(store, qs, interleaved=False)
        wrong = (sess.correct_index() + 1) % 4
        sess.answer(wrong, time_seconds=20.0)
        # inference is available before the user self-reports
        assert sess.pending_inference is not None
        sess.classify_error("content_gap")

        row = store.conn.execute(
            "SELECT error_type, inferred_error_type, error_source, chosen_index "
            "FROM perf_attempts"
        ).fetchone()
        # self-report preserved verbatim
        assert row["error_type"] == "content_gap"
        assert row["chosen_index"] == wrong
        # empty collection -> no retrievability -> M imputed mid (0.5). Post
        # rebalance, a mid-M miss on an application-demand item COMMITS to
        # application (content presumed held via the gate) instead of abstaining;
        # the self-report (content_gap) then disagrees -> self_report_override.
        assert row["inferred_error_type"] == "application"
        assert row["error_source"] == "self_report_override"


# ---------------------------------------------------------------------------
# feature_json capture (full observed signal vector) + export round-trip
# ---------------------------------------------------------------------------
def test_session_correct_attempt_logs_feature_json():
    col = getEmptyCol()
    q = dict(SAMPLE_QUESTIONS[0], cognitive_demand="recall")
    with PerfStore(col) as store:
        store.upsert_questions([q])
        qs = store.eligible_questions({"bb_citric_acid"})
        sess = PerformanceSession(store, qs, interleaved=True)
        correct_idx = sess.correct_index()
        sess.answer(correct_idx, time_seconds=11.0)

        row = store.conn.execute(
            "SELECT correct, chosen_index, inferred_error_type, "
            "inferred_confidence, feature_json FROM perf_attempts"
        ).fetchone()
        # objective label columns written on the correct path too
        assert row["correct"] == 1
        assert row["chosen_index"] == correct_idx
        assert row["inferred_error_type"] == mcat_perf.ERR_NONE
        assert abs(row["inferred_confidence"] - 1.0) < 1e-9

        assert row["feature_json"] is not None
        feat = json.loads(row["feature_json"])
        assert set(feat) >= EXPECTED_FEATURE_KEYS
        assert feat["schema"] == mcat_perf.FEATURE_SCHEMA_VERSION
        assert feat["correct"] is True
        assert feat["chosen_index"] == correct_idx
        assert feat["time_seconds"] == 11.0
        assert feat["interleaved"] is True
        assert feat["cognitive_demand"] == "recall"
        assert feat["topic_id"] == "bb_citric_acid"
        assert feat["section"] == "BB"
        assert feat["inferred_error_type"] == mcat_perf.ERR_NONE
        assert feat["self_report_error_type"] is None


def test_session_incorrect_attempt_logs_feature_json():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions([QUESTION_WITH_DIAGNOSIS])
        qs = store.eligible_questions({"bb_citric_acid"})
        sess = PerformanceSession(store, qs, interleaved=False)
        # index 2 = content_gap distractor (correct is "B" == index 1)
        sess.answer(2, time_seconds=25.0)
        sess.classify_error("content_gap")

        row = store.conn.execute(
            "SELECT correct, chosen_index, error_type, mastery_snapshot, "
            "inferred_error_type, error_source, feature_json, "
            "recheck_card_id, recheck_correct FROM perf_attempts"
        ).fetchone()
        # objective label columns
        assert row["correct"] == 0
        assert row["chosen_index"] == 2
        assert row["error_type"] == "content_gap"
        assert row["inferred_error_type"] is not None
        assert row["error_source"] is not None
        # deferred probe columns stay NULL (no dialog/probe wiring yet)
        assert row["recheck_card_id"] is None
        assert row["recheck_correct"] is None

        assert row["feature_json"] is not None
        feat = json.loads(row["feature_json"])
        assert set(feat) >= EXPECTED_FEATURE_KEYS
        assert feat["correct"] is False
        assert feat["chosen_index"] == 2
        assert feat["self_report_error_type"] == "content_gap"
        assert feat["time_seconds"] == 25.0
        assert feat["cognitive_demand"] == "recall"
        # authored choice_diagnosis signals for the chosen option
        assert feat["has_content_tag"] is True
        assert feat["maps_to"] == "content_gap"
        assert feat["misconception"] == "confuses the intermediate"
        assert feat["is_trap"] is False
        assert feat["trap_type"] is None
        assert feat["inferred_error_type"] == row["inferred_error_type"]
        assert feat["error_source"] == row["error_source"]


def test_export_round_trips_attempts(tmp_path):
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        store.log_attempt(
            "q_dev_001",
            correct=True,
            time_seconds=9.0,
            chosen_index=1,
            mastery_snapshot=0.72,
            feature_json={"schema": "t", "correct": True, "note": "café"},
        )
        store.log_attempt(
            "q_dev_006",
            correct=False,
            error_type="misread",
            chosen_index=0,
            mastery_snapshot=0.5,
            feature_json={"schema": "t", "correct": False},
        )
        db_path = store.path

    # read_attempts joins question context and parses feature_json
    rows = read_attempts(db_path)
    assert len(rows) == 2
    assert rows[0]["question_id"] == "q_dev_001"
    assert rows[0]["topic_id"] == "bb_citric_acid"
    assert rows[0]["section"] == "BB"
    assert rows[0]["feature"]["correct"] is True
    # non-ASCII survives (ensure_ascii=False)
    assert rows[0]["feature"]["note"] == "café"
    assert rows[1]["question_id"] == "q_dev_006"
    assert rows[1]["correct"] == 0

    # JSON export round-trips
    out_json = str(tmp_path / "export.json")
    assert export_attempts(db_path, out_json, fmt="json") == 2
    with open(out_json, encoding="utf-8") as f:
        loaded = json.load(f)
    assert [r["question_id"] for r in loaded] == ["q_dev_001", "q_dev_006"]
    assert loaded[0]["feature"]["note"] == "café"

    # CSV export expands feature into feat_* columns
    out_csv = str(tmp_path / "export.csv")
    assert export_attempts(db_path, out_csv, fmt="csv") == 2
    import csv

    with open(out_csv, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        csv_rows = list(reader)
    assert len(csv_rows) == 2
    assert "feat_correct" in reader.fieldnames
    assert "question_id" in reader.fieldnames
    assert csv_rows[0]["question_id"] == "q_dev_001"

    # both -> writes sibling .csv + .json from a base path
    base = str(tmp_path / "dump")
    assert export_attempts(db_path, base + ".ignored", fmt="both") == 2
    import os

    assert os.path.exists(base + ".csv")
    assert os.path.exists(base + ".json")


def test_export_rejects_unknown_format(tmp_path):
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        store.log_attempt("q_dev_001", correct=True)
        db_path = store.path
    import pytest

    with pytest.raises(ValueError):
        export_attempts(db_path, str(tmp_path / "x.txt"), fmt="xml")
