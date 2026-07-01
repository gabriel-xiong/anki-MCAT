# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import random

from anki import mcat_perf
from anki.mcat_perf import PerformanceSession, PerfStore, order_questions
from tests.shared import getEmptyCol

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


def test_infer_fast_alone_is_not_misread_but_unresolved():
    # fast + wrong but NOT a trap and demand is recall -> abstain, never misread
    out = mcat_perf.infer_error_type(
        correct=False,
        cognitive_demand="recall",
        mastery=0.85,
        time_seconds=3.0,
        is_trap=False,
    )
    assert out["error_type"] == mcat_perf.ERR_UNRESOLVED


def test_infer_ambiguous_mastery_is_unresolved():
    # middling M, no trap, no content tag -> abstain (prefer unresolved)
    out = mcat_perf.infer_error_type(
        correct=False, cognitive_demand="application", mastery=0.55, time_seconds=20.0
    )
    assert out["error_type"] == mcat_perf.ERR_UNRESOLVED


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
        ):
            assert c in acols, c
        # deferred columns are NOT added in this slice
        for c in ("first_choice_index", "answer_changes", "feature_json", "recheck_timing"):
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
        # empty collection -> no retrievability -> M imputed mid -> unresolved
        assert row["inferred_error_type"] == "unresolved"
        assert row["error_source"] == "self_report"
