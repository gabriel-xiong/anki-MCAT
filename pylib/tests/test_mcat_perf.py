# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import json
import os
import random
import tempfile

from anki import mcat_perf
from anki.mcat_perf import (
    N_APPLICATION_PRACTICE,
    PerformanceSession,
    PerfStore,
    export_attempts,
    export_bundle,
    import_bundle,
    order_questions,
    read_attempts,
    select_application_practice,
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
        "explanation": (
            "Succinyl-CoA to succinate is the citric acid cycle's only "
            "substrate-level phosphorylation, directly producing one GTP/ATP."
        ),
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
        assert overall["attempts"] == 2  # first attempt per question only
        assert overall["correct"] == 2
        assert abs(overall["accuracy"] - 1.0) < 1e-9

        raw = store.accuracy(first_attempt_only=False)
        assert raw["attempts"] == 3
        assert raw["correct"] == 2
        assert abs(raw["accuracy"] - 2 / 3) < 1e-9

        topic = store.accuracy("bb_citric_acid")
        assert topic["attempts"] == 1
        assert topic["correct"] == 1
        assert topic["accuracy"] == 1.0


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
# "Not sure" (IDK) anti-guessing opt-out
# ---------------------------------------------------------------------------
def test_idk_attempt_excluded_from_accuracy_and_tracked():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)

        store.log_attempt("q_dev_001", correct=True, time_seconds=10.0)
        # An IDK on a DIFFERENT question, logged BEFORE any real attempt on it,
        # must be skipped by the first-attempt subquery (idk = 0 filter) so it
        # neither becomes the counted "first attempt" nor moves accuracy.
        store.log_attempt(
            "q_dev_006", correct=False, error_type="content_gap", idk=True
        )

        acc = store.accuracy()
        assert acc["attempts"] == 1  # only q_dev_001; the IDK is excluded
        assert acc["correct"] == 1
        assert acc["accuracy"] == 1.0
        # ...but the opt-out IS tracked, overall and per topic.
        assert store.idk_count() == 1
        assert store.idk_count("cp_acids_bases") == 1
        assert store.idk_count("bb_citric_acid") == 0
        # The IDK'd topic stays honest-abstain (no scored attempt there yet).
        assert store.accuracy("cp_acids_bases")["accuracy"] is None

        # A real (scored) attempt on the IDK'd question now counts, and the
        # earlier IDK row still does not leak into numerator/denominator.
        store.log_attempt("q_dev_006", correct=True, time_seconds=15.0)
        acc2 = store.accuracy()
        assert acc2["attempts"] == 2
        assert acc2["correct"] == 2
        assert acc2["accuracy"] == 1.0
        assert store.idk_count() == 1  # unchanged
        # attempt_count is a raw row count, so it DOES include the IDK row.
        assert store.attempt_count() == 3


def test_session_not_sure_non_cars_maps_to_content_gap():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        qs = [
            q
            for q in store.eligible_questions({"bb_citric_acid"})
            if q["section"] != "CARS"
        ]
        sess = PerformanceSession(store, qs, interleaved=False)
        assert sess.current_is_cars is False

        sess.not_sure()

        # Not a graded attempt: no awaiting-classification state, accuracy
        # untouched, session summary unaffected, but the opt-out is tallied.
        assert sess.awaiting_error_type is False
        assert sess.idk == 1
        assert sess.summary()["answered"] == 0
        assert sess.summary()["accuracy"] is None
        assert store.accuracy()["accuracy"] is None
        assert store.idk_count() == 1

        row = store.conn.execute(
            "SELECT correct, error_type, idk, error_source FROM perf_attempts"
        ).fetchone()
        assert row["idk"] == 1
        assert row["correct"] == 0  # never counts as correct for mastery/unlock
        assert row["error_type"] == "content_gap"  # routes remediation
        assert row["error_source"] == "idk"

        # Advances like a normal resolved question (nothing to confirm).
        sess.advance()
        assert sess.index == 1


def test_session_not_sure_cars_logs_unresolved():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        qs = [
            q for q in store.eligible_questions(set()) if q["section"] == "CARS"
        ]
        sess = PerformanceSession(store, qs, interleaved=False)
        assert sess.current_is_cars is True

        sess.not_sure()

        assert sess.idk == 1
        assert store.accuracy()["accuracy"] is None
        assert store.idk_count() == 1
        row = store.conn.execute(
            "SELECT correct, error_type, idk FROM perf_attempts"
        ).fetchone()
        assert row["idk"] == 1
        assert row["correct"] == 0
        # CARS already skips error typing → IDK is just unresolved, no content_gap.
        assert row["error_type"] == "unresolved"


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


def test_infer_recheck_fail_is_content_gap_high_conf():
    # Probe FAIL = content not retrievable even when cued now → the STRONGEST
    # content_gap signal (objective). Commits content_gap at high confidence,
    # and does so even for an applied/synthesis item that would otherwise be
    # application, and even on a trap landing.
    out = mcat_perf.infer_error_type(
        correct=False,
        cognitive_demand="synthesis",
        mastery=0.9,
        time_seconds=30.0,
        recheck_correct=False,
    )
    assert out["error_type"] == mcat_perf.ERR_CONTENT_GAP
    assert out["confidence"] >= 0.85


def test_infer_recheck_pass_applied_is_application_content_gap_suppressed():
    # Probe PASS = content demonstrably available → content_gap SUPPRESSED, even
    # when low M AND an authored content tag would otherwise force content_gap.
    # An applied/synthesis miss then routes to application at raised confidence.
    out = mcat_perf.infer_error_type(
        correct=False,
        cognitive_demand="application",
        mastery=0.2,          # low M would normally → content_gap
        has_content_tag=True,  # authored tag would normally → content_gap
        recheck_correct=True,
    )
    assert out["error_type"] == mcat_perf.ERR_APPLICATION
    assert out["confidence"] >= 0.75  # probe corroborates content held


def test_infer_recheck_pass_trap_is_misread():
    # Probe PASS on a trap landing → misread (execution slip on held content),
    # confidence raised by the corroborating probe.
    out = mcat_perf.infer_error_type(
        correct=False,
        cognitive_demand="recall",
        mastery=0.5,
        is_trap=True,
        recheck_correct=True,
    )
    assert out["error_type"] == mcat_perf.ERR_MISREAD
    assert out["confidence"] >= 0.75


def test_infer_recheck_pass_recall_no_trap_is_application_not_misread():
    # Probe PASS on a held recall item, no trap → content was available but the
    # MCQ was still wrong: applied reasoning, NOT misread or content_gap.
    out = mcat_perf.infer_error_type(
        correct=False, cognitive_demand="recall", mastery=0.85, recheck_correct=True
    )
    assert out["error_type"] == mcat_perf.ERR_APPLICATION
    assert out["error_type"] != mcat_perf.ERR_MISREAD
    assert out["error_type"] != mcat_perf.ERR_CONTENT_GAP


def test_infer_recheck_confidence_varies_with_signals():
    # Regression for the "always 90% content / 65% applied" bug: the PROBE branch
    # (branch 0) is what the live UI actually shows (record_probe_outcome re-runs
    # inference with recheck_correct set), and it must NOT emit a per-outcome
    # constant. Vary M / demand / timing under a FIXED probe outcome and assert
    # the confidence genuinely moves.
    fail_confs = {
        mcat_perf.infer_error_type(
            correct=False, cognitive_demand="recall", mastery=m,
            recheck_correct=False,
        )["confidence"]
        for m in (0.2, 0.55, 0.9)  # low / mid / high M
    }
    assert len(fail_confs) >= 3, fail_confs  # not one flat FAIL constant
    pass_confs = {
        mcat_perf.infer_error_type(
            correct=False, cognitive_demand="recall", mastery=0.5,
            recheck_correct=True,
        )["confidence"],
        mcat_perf.infer_error_type(
            correct=False, cognitive_demand="application", mastery=0.5,
            recheck_correct=True,
        )["confidence"],
        mcat_perf.infer_error_type(
            correct=False, cognitive_demand="synthesis", mastery=0.9,
            recheck_correct=True,
        )["confidence"],
        mcat_perf.infer_error_type(
            correct=False, cognitive_demand="application", mastery=0.5,
            time_seconds=3.0, recheck_correct=True,
        )["confidence"],
    }
    assert len(pass_confs) >= 3, pass_confs  # not one flat PASS constant
    # The old bug pinned FAIL→0.9 and PASS→0.65; assert they no longer collapse
    # to a single shared value.
    assert fail_confs != pass_confs


def test_infer_no_recheck_unchanged():
    # recheck_correct=None (no probe fired) leaves the demand-aware ladder
    # untouched — a low-M applied miss is still content_gap (honesty gate).
    out = mcat_perf.infer_error_type(
        correct=False, cognitive_demand="application", mastery=0.2
    )
    assert out["error_type"] == mcat_perf.ERR_CONTENT_GAP


# Fine mastery grid used to lock in "confidence genuinely MOVES with M" (the
# 2026-07-03 fix) rather than collapsing to per-branch constants. Anchors span
# the low / mid / high bands around LOW_M=0.4 and HIGH_M=0.7.
_M_SWEEP = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]


def test_infer_probe_fail_content_gap_confidence_varies_with_mastery():
    # Branch 0 FAIL is what the LIVE UI shows on essentially every miss. Lock in
    # that its confidence genuinely MOVES across the M grid (the fixed
    # degeneracy) and trends DOWN as mastery rises (low M corroborates the gap
    # most; high M weakly contradicts), while staying content_gap and honestly
    # bounded high. Assert variation + ordering, NOT brittle magic numbers.
    confs = []
    for m in _M_SWEEP:
        out = mcat_perf.infer_error_type(
            correct=False,
            cognitive_demand="recall",
            mastery=m,
            time_seconds=30.0,
            recheck_correct=False,
        )
        assert out["error_type"] == mcat_perf.ERR_CONTENT_GAP
        assert 0.85 <= out["confidence"] <= 0.97  # bounded; never fabricated
        confs.append(out["confidence"])
    # not one flat FAIL constant across mastery
    assert len(set(confs)) >= 3, confs
    # non-increasing in M, and low-M is strictly more confident than high-M
    assert confs == sorted(confs, reverse=True), confs
    assert confs[0] > confs[-1], confs


def test_infer_no_probe_applied_transitions_and_confidence_rises_with_mastery():
    # Offline-replay ladder (recheck None): an applied-demand miss must
    # TRANSITION content_gap (low M — content not held) -> application (high M —
    # held but misapplied), and the application confidence must RISE with M.
    rows = [
        (
            m,
            mcat_perf.infer_error_type(
                correct=False,
                cognitive_demand="application",
                mastery=m,
                time_seconds=30.0,
            ),
        )
        for m in _M_SWEEP
    ]
    # error_type transitions across the mastery grid
    assert rows[0][1]["error_type"] == mcat_perf.ERR_CONTENT_GAP  # M=0.0
    assert rows[-1][1]["error_type"] == mcat_perf.ERR_APPLICATION  # M=1.0
    # confidence genuinely varies across the whole grid (not a single constant)
    all_confs = [out["confidence"] for _, out in rows]
    assert len(set(all_confs)) >= 3, all_confs
    # within the application region confidence is non-decreasing in M, and the
    # top of the grid is strictly more confident than the first applied point
    app = [
        out["confidence"]
        for _, out in rows
        if out["error_type"] == mcat_perf.ERR_APPLICATION
    ]
    assert app == sorted(app), app
    assert app[-1] > app[0], app


def test_infer_probe_pass_applied_confidence_rises_with_mastery():
    # The other half of the fix: probe PASS routes to application and M NUDGES
    # the confidence up (the probe already settled content-held), so it must
    # still MOVE with mastery rather than emit a flat PASS constant.
    low = mcat_perf.infer_error_type(
        correct=False,
        cognitive_demand="application",
        mastery=0.2,
        time_seconds=30.0,
        recheck_correct=True,
    )
    high = mcat_perf.infer_error_type(
        correct=False,
        cognitive_demand="application",
        mastery=0.95,
        time_seconds=30.0,
        recheck_correct=True,
    )
    assert low["error_type"] == high["error_type"] == mcat_perf.ERR_APPLICATION
    assert high["confidence"] > low["confidence"], (low, high)


def test_resolve_probe_target_skips_cars_falls_back_for_science():
    # CARS → skip (None). A science item with no discoverable backing card in an
    # empty collection falls back to a concept probe (card_id None, fallback).
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        cars = store.eligible_questions(set())[0]
        assert cars["section"] == "CARS"
        assert mcat_perf.resolve_probe_target(col, cars) is None

        sci = store.eligible_questions({"bb_citric_acid"})
        sci = next(q for q in sci if q["id"] == "q_dev_001")
        target = mcat_perf.resolve_probe_target(col, sci)
        assert target is not None
        assert target["fallback"] is True
        assert target["card_id"] is None
        assert "bb_citric_acid" not in target["front"]
        assert "underlying rule or relationship" in target["front"]


def test_resolve_probe_target_uses_question_card_map_when_no_collection_cards():
    col = getEmptyCol()
    mcat_perf._QUESTION_CARD_MAP["q_dev_001"] = {
        "topic_id": "bb_citric_acid",
        "fronts": [
            "In the citric acid cycle, GTP/ATP is made during the conversion of "
            "{{c1::succinyl-CoA to succinate}}."
        ],
    }
    try:
        with PerfStore(col) as store:
            store.upsert_questions(SAMPLE_QUESTIONS)
            sci = next(
                q
                for q in store.eligible_questions({"bb_citric_acid"})
                if q["id"] == "q_dev_001"
            )
            target = mcat_perf.resolve_probe_target(col, sci)
            assert target is not None
            assert target["fallback"] is True
            assert target["card_id"] is None
            # The cloze deletion (the ANSWER) must be HIDDEN in the pre-reveal
            # prompt, not filled in — mirroring the real-card path.
            assert "succinyl-CoA to succinate" not in target["front"]
            assert "[…]" in target["front"]
            assert "{{c1" not in target["front"]
            assert "bb_citric_acid" not in target["front"]
            # The answer still lives in the reveal field so "Reveal answer" works.
            assert "succinyl-CoA" in sci["explanation"] or target["back"]
    finally:
        mcat_perf._QUESTION_CARD_MAP.clear()


def test_probe_fallback_hides_cloze_deletion_genetics():
    """The screenshot-1 genetics cloze must render answer-HIDDEN in fallback."""
    col = getEmptyCol()
    mcat_perf._QUESTION_CARD_MAP["q_dev_030"] = {
        "topic_id": "bb_genetics",
        "fronts": [
            "A monohybrid cross Aa × Aa gives a phenotypic ratio of "
            "{{c1::3:1}} and a genotypic ratio of {{c1::1:2:1}}."
        ],
    }
    try:
        q = {
            "id": "q_dev_030",
            "stem": "Monohybrid cross ratio probe test",
            "choices": ["A", "B", "C", "D"],
            "correct": "A",
            "topic_id": "bb_genetics",
            "section": "BB",
            "explanation": "Aa × Aa → 3:1 phenotypic, 1:2:1 genotypic.",
            "source_name": "test",
            "split": "dev",
        }
        with PerfStore(col) as store:
            store.upsert_questions([q])
            target = mcat_perf.resolve_probe_target(col, q)
            assert target is not None
            assert target["fallback"] is True
            # Neither ratio (the answers) may appear in the pre-reveal prompt.
            assert "3:1" not in target["front"]
            assert "1:2:1" not in target["front"]
            assert target["front"].count("[…]") == 2
            assert "{{c" not in target["front"]
            # The reveal still carries the answer.
            assert "3:1" in target["back"]
    finally:
        mcat_perf._QUESTION_CARD_MAP.clear()


def test_hide_cloze_front_variants():
    """Unit check: blank plain deletions, keep hints, leave non-cloze intact."""
    assert (
        mcat_perf._hide_cloze_front("ratio is {{c1::3:1}}.") == "ratio is […]."
    )
    # Hint form {{cN::answer::hint}} shows the hint, never the answer.
    assert (
        mcat_perf._hide_cloze_front("capital is {{c1::Paris::city}}.")
        == "capital is [city]."
    )
    # Multiple deletions each blank independently.
    assert (
        mcat_perf._hide_cloze_front("{{c1::A}} and {{c2::B}}") == "[…] and […]"
    )
    # Non-cloze fronts are unaffected.
    plain = "What two steps write the complement of a DNA strand?"
    assert mcat_perf._hide_cloze_front(plain) == plain


def test_resolve_probe_target_map_prefers_basic_front():
    col = getEmptyCol()
    mcat_perf._QUESTION_CARD_MAP["q_syn_018"] = {
        "topic_id": "bb_dna",
        "fronts": [
            "In DNA, A pairs with {{c1::T}} and G pairs with {{c1::C}}.",
            "To write the complement of a DNA strand in the 5'→3' direction, "
            "what two steps do you take?",
        ],
    }
    try:
        with PerfStore(col) as store:
            q = {
                "id": "q_syn_018",
                "stem": "DNA complement probe test",
                "choices": ["A", "B", "C", "D"],
                "correct": "A",
                "topic_id": "bb_dna",
                "section": "BB",
                "explanation": "Pair bases, then reverse for antiparallel strands.",
                "source_name": "test",
                "split": "dev",
            }
            store.upsert_questions([q])
            target = mcat_perf.resolve_probe_target(col, q)
            assert target is not None
            assert "complement of a DNA strand" in target["front"]
            assert "Pair bases" in target["back"]
    finally:
        mcat_perf._QUESTION_CARD_MAP.clear()


def test_resolve_probe_target_uses_supports_question_tag():
    col = getEmptyCol()
    note = col.newNote()
    note["Front"] = "What enzyme makes GTP in the citric acid cycle?"
    note["Back"] = "Succinyl-CoA synthetase"
    note.tags = ["topic:bb_citric_acid", "supports_question:q_dev_001"]
    col.addNote(note)
    cid = col.find_cards("tag:supports_question:q_dev_001")[0]

    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        sci = next(
            q
            for q in store.eligible_questions({"bb_citric_acid"})
            if q["id"] == "q_dev_001"
        )
        target = mcat_perf.resolve_probe_target(col, sci)
        assert target is not None
        assert target["fallback"] is False
        assert target["card_id"] == cid
        assert "GTP" in target["front"]
        assert "Succinyl" in target["back"]


def test_resolve_probe_target_self_heals_stale_map_from_sidecar():
    # Regression: a bank loaded before question-card-map.json existed (or a map
    # since regenerated with new entries) leaves the in-memory map without this
    # question. resolve_probe_target must force-reload from the sidecar's
    # recorded bank path and use the authored front instead of going generic.
    col = getEmptyCol()
    with tempfile.TemporaryDirectory() as d:
        qpath = os.path.join(d, "questions.json")
        q = {
            "id": "q_dev_electro",
            "stem": "Nernst probe self-heal test",
            "choices": ["A", "B", "C", "D"],
            "correct": "A",
            "topic_id": "cp_electrochem",
            "section": "CP",
            "explanation": "Raising product-ion concentration raises Q, lowering Ecell.",
            "source_name": "test",
            "split": "dev",
        }
        with open(qpath, "w", encoding="utf-8") as f:
            json.dump([q], f)
        with PerfStore(col) as store:
            store.load_questions(qpath)  # records question_bank_path in sidecar
        # Simulate the map being written/regenerated AFTER the bank was loaded,
        # while the in-memory copy is stale (empty here).
        mpath = os.path.join(d, "question-card-map.json")
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "q_dev_electro": {
                        "topic_id": "cp_electrochem",
                        "fronts": [
                            "By the Nernst equation, raising product-ion "
                            "concentration raises {{c1::Q}}."
                        ],
                    }
                },
                f,
            )
        mcat_perf._QUESTION_CARD_MAP.clear()
        try:
            target = mcat_perf.resolve_probe_target(col, q)
            assert target is not None
            assert target["fallback"] is True
            # Self-heal used the AUTHORED front from the sidecar map (not the
            # generic fallback): the sentence context is present, with the cloze
            # answer HIDDEN as a blank so nothing leaks pre-reveal.
            assert "raising product-ion concentration raises" in target["front"]
            assert "[…]" in target["front"]
            assert "Q" not in target["front"]
            assert "{{c1" not in target["front"]
            assert "cp_electrochem" not in target["front"]
            # The reveal fills ONLY the blanked cloze from the same sentence —
            # no "Answer:" prefix and no explanation blurb ("Ecell" is unique to
            # the question's explanation, so it must not appear in the reveal).
            assert "Q" in target["back"]
            assert "Answer:" not in target["back"]
            assert "Ecell" not in target["back"]
        finally:
            mcat_perf._QUESTION_CARD_MAP.clear()


def test_session_probe_fail_commits_content_gap_and_persists():
    col = getEmptyCol()
    q = dict(SAMPLE_QUESTIONS[0], cognitive_demand="synthesis")
    with PerfStore(col) as store:
        store.upsert_questions([q])
        qs = store.eligible_questions({"bb_citric_acid"})
        sess = PerformanceSession(store, qs, interleaved=False)
        wrong = (sess.correct_index() + 1) % 4
        sess.answer(wrong, time_seconds=30.0)
        # simulate the immediate probe: student could NOT recall it → FAIL
        inf = sess.record_probe_outcome(False, card_id=987654321)
        assert inf["error_type"] == mcat_perf.ERR_CONTENT_GAP
        assert inf["confidence"] >= 0.85
        # self-report agrees → error_source marks the probe informed the call
        sess.classify_error("content_gap")

        row = store.conn.execute(
            "SELECT inferred_error_type, inferred_confidence, recheck_card_id, "
            "recheck_correct, error_source, feature_json FROM perf_attempts"
        ).fetchone()
        assert row["inferred_error_type"] == "content_gap"
        assert row["inferred_confidence"] >= 0.85
        assert row["recheck_card_id"] == 987654321
        assert row["recheck_correct"] == 0
        assert row["error_source"] == "inference+recheck"
        feat = json.loads(row["feature_json"])
        assert feat["recheck_correct"] is False
        assert feat["recheck_card_id"] == 987654321


def test_session_probe_pass_applied_routes_to_application():
    col = getEmptyCol()
    q = dict(SAMPLE_QUESTIONS[0], cognitive_demand="application")
    with PerfStore(col) as store:
        store.upsert_questions([q])
        qs = store.eligible_questions({"bb_citric_acid"})
        sess = PerformanceSession(store, qs, interleaved=False)
        wrong = (sess.correct_index() + 1) % 4
        sess.answer(wrong, time_seconds=25.0)
        inf = sess.record_probe_outcome(True, card_id=None)
        assert inf["error_type"] == mcat_perf.ERR_APPLICATION
        sess.classify_error("application")

        row = store.conn.execute(
            "SELECT inferred_error_type, recheck_card_id, recheck_correct, "
            "error_source FROM perf_attempts"
        ).fetchone()
        assert row["inferred_error_type"] == "application"
        assert row["recheck_card_id"] is None  # fallback concept probe
        assert row["recheck_correct"] == 1
        assert row["error_source"] == "inference+recheck"


def test_probe_outcome_round_trips_through_bundle(tmp_path):
    # The objective probe columns travel in the portable sync bundle.
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        store.log_attempt(
            "q_dev_001",
            correct=False,
            error_type="content_gap",
            recheck_card_id=42,
            recheck_correct=False,
            error_source="inference+recheck",
        )
        src_db = store.path
    bundle = str(tmp_path / "b.json")
    export_bundle(src_db, bundle)

    col2 = getEmptyCol()
    with PerfStore(col2) as dst:
        dst.import_bundle(bundle)
        row = dst.conn.execute(
            "SELECT recheck_card_id, recheck_correct, error_source "
            "FROM perf_attempts"
        ).fetchone()
        assert row["recheck_card_id"] == 42
        assert row["recheck_correct"] == 0
        assert row["error_source"] == "inference+recheck"


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


# explanation (static, NO-AI correct-answer rationale): additive column +
# store->load round-trip + absent-stores-null passthrough.
def test_migration_adds_explanation_column():
    col = getEmptyCol()
    with PerfStore(col) as store:
        qcols = mcat_perf._existing_columns(store.conn, "perf_questions")
        assert "explanation" in qcols


def test_explanation_roundtrips_through_store():
    col = getEmptyCol()
    with PerfStore(col) as store:
        # SAMPLE_QUESTIONS[0] carries an explanation
        store.upsert_questions([SAMPLE_QUESTIONS[0]])
        loaded = store.eligible_questions({"bb_citric_acid"})[0]
        assert loaded["explanation"] == SAMPLE_QUESTIONS[0]["explanation"]
        # persisted verbatim in the perf_questions row
        raw = store.conn.execute(
            "SELECT explanation FROM perf_questions WHERE id = ?",
            ("q_dev_001",),
        ).fetchone()
        assert raw["explanation"] == SAMPLE_QUESTIONS[0]["explanation"]


def test_explanation_absent_stores_null():
    col = getEmptyCol()
    with PerfStore(col) as store:
        # SAMPLE_QUESTIONS[1] has no explanation key (legacy / unauthored)
        store.upsert_questions([SAMPLE_QUESTIONS[1]])
        loaded = store.eligible_questions({"cp_acids_bases"})[0]
        assert loaded["explanation"] is None
        raw = store.conn.execute(
            "SELECT explanation FROM perf_questions WHERE id = ?",
            ("q_dev_006",),
        ).fetchone()
        assert raw["explanation"] is None


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


# ---------------------------------------------------------------------------
# Calibration view (probe-aware): flat labeled rows + diagnosis/probe agreement
# ---------------------------------------------------------------------------
def test_calibration_export_and_agreement(tmp_path):
    # Three probe-labeled misses spanning agree/disagree, plus a correct attempt
    # with NO probe (must be excluded from the calibration view entirely).
    calib_qs = [
        dict(SAMPLE_QUESTIONS[0], id="q_dev_101", topic_id="t_gap",
             cognitive_demand="application"),
        dict(SAMPLE_QUESTIONS[0], id="q_dev_102", topic_id="t_app",
             cognitive_demand="synthesis"),
        dict(SAMPLE_QUESTIONS[0], id="q_dev_103", topic_id="t_dis",
             cognitive_demand="application"),
    ]
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(calib_qs)
        # probe FAIL + low M -> pre-probe heuristic content_gap -> AGREE
        store.log_attempt(
            "q_dev_101", correct=False, error_type="content_gap",
            chosen_index=0, mastery_snapshot=0.2, time_seconds=25.0,
            inferred_error_type="content_gap", inferred_confidence=0.9,
            recheck_card_id=111, recheck_correct=False,
            error_source="inference+recheck",
            feature_json={"is_trap": False, "has_content_tag": False,
                          "trap_type": None, "maps_to": None},
        )
        # probe PASS + high M synthesis -> pre-probe heuristic application
        # (NOT content_gap) -> AGREE
        store.log_attempt(
            "q_dev_102", correct=False, error_type="application",
            chosen_index=0, mastery_snapshot=0.9, time_seconds=30.0,
            inferred_error_type="application", inferred_confidence=0.8,
            recheck_card_id=None, recheck_correct=True,
            error_source="inference+recheck",
            feature_json={"is_trap": False, "has_content_tag": False},
        )
        # probe FAIL + high M application, no tag -> pre-probe heuristic
        # application (NOT content_gap) -> DISAGREE (probe says content missing)
        store.log_attempt(
            "q_dev_103", correct=False, error_type="reasoning",
            chosen_index=0, mastery_snapshot=0.9, time_seconds=30.0,
            inferred_error_type="content_gap", inferred_confidence=0.9,
            recheck_card_id=222, recheck_correct=False,
            error_source="inference+recheck",
            feature_json={"is_trap": False, "has_content_tag": False},
        )
        # correct attempt, no probe -> excluded from the calibration view
        store.log_attempt(
            "q_dev_101", correct=True, chosen_index=1, mastery_snapshot=0.9
        )
        db_path = store.path

    rows = mcat_perf.build_calibration_rows(db_path)
    # only the three probe-labeled misses (the correct/no-probe row is dropped)
    assert len(rows) == 3
    # every flat calibration column is present, and there are no nested objects
    for r in rows:
        assert set(mcat_perf.CALIBRATION_COLUMNS) <= set(r)
        for v in r.values():
            assert not isinstance(v, (dict, list))

    by_id = {r["question_id"]: r for r in rows}
    r101 = by_id["q_dev_101"]
    assert r101["recheck_correct"] == 0  # objective FAIL label preserved
    assert r101["low_m"] is True
    assert r101["high_m"] is False
    assert r101["cognitive_demand"] == "application"
    assert r101["is_trap"] is False
    assert r101["heuristic_error_type"] == mcat_perf.ERR_CONTENT_GAP
    assert r101["error_source"] == "inference+recheck"
    # PASS row's pre-probe heuristic is application, not content_gap
    assert by_id["q_dev_102"]["recheck_correct"] == 1
    assert by_id["q_dev_102"]["heuristic_error_type"] == mcat_perf.ERR_APPLICATION

    summary = mcat_perf.calibration_agreement(rows)
    assert summary["labeled"] == 3
    assert summary["tally"]["fail_content_gap"] == 1  # q_dev_101 (agree)
    assert summary["tally"]["pass_other"] == 1        # q_dev_102 (agree)
    assert summary["tally"]["fail_other"] == 1        # q_dev_103 (disagree)
    assert summary["tally"]["pass_content_gap"] == 0
    assert summary["agree"] == 2
    assert abs(summary["agreement_rate"] - 2 / 3) < 1e-9

    # both -> writes sibling .csv + .json with the flat header
    base = str(tmp_path / "calibration")
    assert mcat_perf.write_calibration(rows, base + ".ignored", fmt="both") == 3
    assert os.path.exists(base + ".csv")
    assert os.path.exists(base + ".json")
    with open(base + ".json", encoding="utf-8") as f:
        loaded = json.load(f)
    assert {r["question_id"] for r in loaded} == {
        "q_dev_101", "q_dev_102", "q_dev_103"
    }
    import csv

    with open(base + ".csv", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        csv_rows = list(reader)
    assert reader.fieldnames == mcat_perf.CALIBRATION_COLUMNS
    assert len(csv_rows) == 3


def test_calibration_agreement_empty_is_honest_abstain():
    # No probe-labeled rows -> agreement rate is None (abstain), not a fake 0/1.
    summary = mcat_perf.calibration_agreement([])
    assert summary["labeled"] == 0
    assert summary["agree"] == 0
    assert summary["agreement_rate"] is None


def test_calibration_flatten_tolerates_legacy_missing_feature():
    # A probe-labeled row with NO feature_json (legacy) must flatten without
    # crashing: feature-derived fields degrade to None, M-flags still derive.
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions([SAMPLE_QUESTIONS[0]])
        store.log_attempt(
            "q_dev_001", correct=False, error_type="content_gap",
            mastery_snapshot=0.3, recheck_correct=False,
        )
        db_path = store.path
    rows = mcat_perf.build_calibration_rows(db_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["is_trap"] is None  # no feature_json -> tolerant None
    assert r["has_content_tag"] is None
    assert r["low_m"] is True    # derived from mastery_snapshot regardless
    assert r["recheck_correct"] == 0


def test_export_rejects_unknown_format(tmp_path):
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        store.log_attempt("q_dev_001", correct=True)
        db_path = store.path
    import pytest

    with pytest.raises(ValueError):
        export_attempts(db_path, str(tmp_path / "x.txt"), fmt="xml")


# ---------------------------------------------------------------------------
# Two-way sync — stable attempt uuid + append-only union-merge bundle
# ---------------------------------------------------------------------------
def _attempt_uuids(store):
    return {
        r[0]
        for r in store.conn.execute(
            "SELECT uuid FROM perf_attempts"
        ).fetchall()
    }


def test_migration_adds_attempt_uuid_column_and_unique_index():
    col = getEmptyCol()
    with PerfStore(col) as store:
        # additive column present
        acols = mcat_perf._existing_columns(store.conn, "perf_attempts")
        assert "uuid" in acols
        # UNIQUE index enforcing the stable key exists
        idx = {
            r["name"]
            for r in store.conn.execute(
                "PRAGMA index_list(perf_attempts)"
            ).fetchall()
        }
        assert "idx_perf_attempts_uuid" in idx


def test_log_attempt_stamps_stable_unique_uuid():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        store.log_attempt("q_dev_001", correct=True)
        store.log_attempt("q_dev_001", correct=False, error_type="misread")
        uuids = [
            r[0]
            for r in store.conn.execute(
                "SELECT uuid FROM perf_attempts"
            ).fetchall()
        ]
        # every attempt carries a non-null uuid, and they are distinct
        assert all(u for u in uuids)
        assert len(set(uuids)) == len(uuids) == 2


def test_migration_backfills_legacy_null_uuids():
    # Simulate an older sidecar row that predates the uuid column, then re-run
    # the additive migration and confirm it is backfilled + stays unique.
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        store.log_attempt("q_dev_001", correct=True)
        store.log_attempt("q_dev_006", correct=True)
        store.conn.execute(
            "UPDATE perf_attempts SET uuid = NULL WHERE question_id = 'q_dev_001'"
        )
        store.conn.commit()
        assert None in _attempt_uuids(store)

        mcat_perf._migrate(store.conn)
        store.conn.commit()

        uuids = _attempt_uuids(store)
        assert None not in uuids
        assert len(uuids) == 2  # still unique


def test_bundle_round_trip_preserves_attempts(tmp_path):
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
            "q_dev_006", correct=False, error_type="misread", chosen_index=0
        )
        src_db = store.path

    bundle = str(tmp_path / "bundle.json")
    res = export_bundle(src_db, bundle)
    assert res["attempts"] == 2
    assert res["questions"] == 3

    # bundle is a versioned JSON envelope
    with open(bundle, encoding="utf-8") as f:
        raw = json.load(f)
    assert raw["format"] == mcat_perf.BUNDLE_FORMAT
    assert raw["format_version"] == mcat_perf.BUNDLE_FORMAT_VERSION

    # import into a fresh, empty device
    col2 = getEmptyCol()
    with PerfStore(col2) as dst:
        r = dst.import_bundle(bundle)
        assert r["attempts_added"] == 2
        assert r["attempts_skipped"] == 0
        assert r["questions_added"] == 3
        assert dst.attempt_count() == 2
        assert dst.question_count() == 3
        # question context reconstructed + feature_json (non-ASCII) preserved
        row = dst.conn.execute(
            "SELECT feature_json FROM perf_attempts WHERE question_id = 'q_dev_001'"
        ).fetchone()
        assert json.loads(row["feature_json"])["note"] == "café"


def test_bundle_merge_preserves_question_explanation(tmp_path):
    # The explanation column travels in the portable sync bundle and is
    # reinserted on a fresh device via the dynamic-column question merge.
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions([SAMPLE_QUESTIONS[0]])
        src_db = store.path
    bundle = str(tmp_path / "b.json")
    export_bundle(src_db, bundle)

    col2 = getEmptyCol()
    with PerfStore(col2) as dst:
        dst.import_bundle(bundle)
        loaded = dst.eligible_questions({"bb_citric_acid"})[0]
        assert loaded["explanation"] == SAMPLE_QUESTIONS[0]["explanation"]


def test_bundle_union_merge_two_disjoint_devices_converge(tmp_path):
    # Device A and B each log different attempts against the same bank.
    colA = getEmptyCol()
    with PerfStore(colA) as A:
        A.upsert_questions(SAMPLE_QUESTIONS)
        A.log_attempt("q_dev_001", correct=True)
        A.log_attempt("q_dev_001", correct=False, error_type="content_gap")
        a_db = A.path

    colB = getEmptyCol()
    with PerfStore(colB) as B:
        B.upsert_questions(SAMPLE_QUESTIONS)
        B.log_attempt("q_dev_006", correct=True)
        b_db = B.path

    bundle_a = str(tmp_path / "A.json")
    bundle_b = str(tmp_path / "B.json")
    export_bundle(a_db, bundle_a)
    export_bundle(b_db, bundle_b)

    # two-way: each device imports the other's bundle
    with PerfStore(colA) as A:
        A.import_bundle(bundle_b)
    with PerfStore(colB) as B:
        B.import_bundle(bundle_a)

    # both converge to the union (3 attempts) with identical uuid sets
    with PerfStore(colA) as A, PerfStore(colB) as B:
        ua, ub = _attempt_uuids(A), _attempt_uuids(B)
        assert A.attempt_count() == 3
        assert B.attempt_count() == 3
        assert ua == ub


def test_bundle_reimport_is_idempotent(tmp_path):
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        store.log_attempt("q_dev_001", correct=True)
        store.log_attempt("q_dev_006", correct=False, error_type="reasoning")
        src_db = store.path
    bundle = str(tmp_path / "b.json")
    export_bundle(src_db, bundle)

    col2 = getEmptyCol()
    with PerfStore(col2) as dst:
        first = dst.import_bundle(bundle)
        assert first["attempts_added"] == 2
        # re-importing the exact same bundle changes nothing
        second = dst.import_bundle(bundle)
        assert second["attempts_added"] == 0
        assert second["attempts_skipped"] == 2
        assert dst.attempt_count() == 2


def test_bundle_merge_is_order_independent(tmp_path):
    # Two source bundles; importing them in either order yields the same union.
    colA = getEmptyCol()
    with PerfStore(colA) as A:
        A.upsert_questions(SAMPLE_QUESTIONS)
        A.log_attempt("q_dev_001", correct=True)
        a_db = A.path
    colB = getEmptyCol()
    with PerfStore(colB) as B:
        B.upsert_questions(SAMPLE_QUESTIONS)
        B.log_attempt("q_dev_006", correct=False, error_type="misread")
        B.log_attempt("q_cars_001", correct=True)
        b_db = B.path
    bundle_a = str(tmp_path / "A.json")
    bundle_b = str(tmp_path / "B.json")
    export_bundle(a_db, bundle_a)
    export_bundle(b_db, bundle_b)

    col_ab = getEmptyCol()
    with PerfStore(col_ab) as ab:
        ab.import_bundle(bundle_a)
        ab.import_bundle(bundle_b)
        uuids_ab = _attempt_uuids(ab)

    col_ba = getEmptyCol()
    with PerfStore(col_ba) as ba:
        ba.import_bundle(bundle_b)
        ba.import_bundle(bundle_a)
        uuids_ba = _attempt_uuids(ba)

    assert uuids_ab == uuids_ba
    assert len(uuids_ab) == 3


def test_import_bundle_rejects_bad_envelope(tmp_path):
    import pytest

    col = getEmptyCol()
    with PerfStore(col) as store:
        bad = str(tmp_path / "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            json.dump({"format": "something_else", "attempts": []}, f)
        with pytest.raises(ValueError):
            store.import_bundle(bad)

        wrong_ver = str(tmp_path / "wrong_ver.json")
        with open(wrong_ver, "w", encoding="utf-8") as f:
            json.dump(
                {"format": mcat_perf.BUNDLE_FORMAT, "format_version": 999}, f
            )
        with pytest.raises(ValueError):
            store.import_bundle(wrong_ver)


def test_import_bundle_headless_creates_db(tmp_path):
    # The module-level import_bundle (CLI path) opens/creates a sidecar DB
    # without a live Collection and merges into it.
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        store.log_attempt("q_dev_001", correct=True)
        src_db = store.path
    bundle = str(tmp_path / "b.json")
    export_bundle(src_db, bundle)

    fresh_db = str(tmp_path / "fresh.mcat_perf.db")
    res = import_bundle(fresh_db, bundle)
    assert res["attempts_added"] == 1
    assert res["questions_added"] == 3
    assert os.path.exists(fresh_db)


# ---------------------------------------------------------------------------
# Application-practice remediation pool: isolated store + selection algorithm.
# The pool must NEVER enter scored state (perf_questions / perf_attempts /
# accuracy / eligible_questions). See MCAT/docs/APPLICATION-PRACTICE-POOL.md.
# ---------------------------------------------------------------------------
def _pool_item(item_id, topic_id, concept, *, demand="application"):
    return {
        "id": item_id,
        "stem": f"Integration stem for {concept}",
        "choices": ["A", "B", "C", "D"],
        "correct": "A",
        "topic_id": topic_id,
        "section": "CP",
        "skill": "2",
        "cognitive_demand": demand,
        "concept": concept,
        "explanation": f"Because of {concept}.",
        "source_name": "OpenStax Chemistry 2e",
        "source_url": "https://openstax.org/books/chemistry-2e",
        "source_location": "Ch. 17",
        "split": "remediation",
        "pool": "application_practice",
    }


# Topic A: 3 distinct concepts (mirrors the real 3-per-topic pool).
POOL_TOPIC_A = [
    _pool_item("ap_a_01", "cp_electrochem", "standard_cell_potential"),
    _pool_item("ap_a_02", "cp_electrochem", "gibbs_cell_relationship",
               demand="synthesis"),
    _pool_item("ap_a_03", "cp_electrochem", "nernst_qualitative"),
]
# Topic B: distinct topic, so topic filtering is observable.
POOL_TOPIC_B = [
    _pool_item("ap_b_01", "cp_acids_bases", "henderson_hasselbalch"),
    _pool_item("ap_b_02", "cp_acids_bases", "buffer_capacity"),
]
# Topic C: all items share ONE concept, to exercise the same-concept fallback.
POOL_TOPIC_C = [
    _pool_item("ap_c_01", "bb_enzymes", "michaelis_menten"),
    _pool_item("ap_c_02", "bb_enzymes", "michaelis_menten"),
]
ALL_POOL = POOL_TOPIC_A + POOL_TOPIC_B + POOL_TOPIC_C


def test_remediation_store_isolated_from_scored_bank():
    # Upserting the pool populates remediation_items only — perf_questions (the
    # scored bank) stays empty, so nothing can reach accuracy/eligibility.
    col = getEmptyCol()
    with PerfStore(col) as store:
        assert store.upsert_remediation(ALL_POOL) == len(ALL_POOL)
        assert store.remediation_count() == len(ALL_POOL)
        # scored bank untouched
        assert store.question_count() == 0
        assert store.eligible_questions({"cp_electrochem"}) == []
        # idempotent upsert
        store.upsert_remediation(ALL_POOL)
        assert store.remediation_count() == len(ALL_POOL)


def test_remediation_rejects_scored_split():
    # A scored ('dev'/'held_out') item must never land in the pool table.
    import pytest

    col = getEmptyCol()
    with PerfStore(col) as store:
        bad = dict(_pool_item("ap_x", "cp_electrochem", "c"), split="dev")
        with pytest.raises(ValueError):
            store.upsert_remediation([bad])
        assert store.remediation_count() == 0


def test_remediation_items_for_topic_filters():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_remediation(ALL_POOL)
        a = store.remediation_items_for_topic("cp_electrochem")
        assert {i["id"] for i in a} == {"ap_a_01", "ap_a_02", "ap_a_03"}
        b = store.remediation_items_for_topic("cp_acids_bases")
        assert {i["id"] for i in b} == {"ap_b_01", "ap_b_02"}
        # unknown topic -> empty
        assert store.remediation_items_for_topic("nope") == []


def test_select_filters_by_topic():
    got = select_application_practice(
        ALL_POOL, topic_id="cp_acids_bases", missed_concept=None
    )
    assert {i["id"] for i in got} == {"ap_b_01", "ap_b_02"}
    assert all(i["topic_id"] == "cp_acids_bases" for i in got)


def test_select_excludes_missed_concept():
    # Missed standard_cell_potential -> practice the two SIBLING concepts.
    got = select_application_practice(
        ALL_POOL,
        topic_id="cp_electrochem",
        missed_concept="standard_cell_potential",
    )
    ids = {i["id"] for i in got}
    assert "ap_a_01" not in ids  # the missed-concept item is excluded
    assert ids == {"ap_a_02", "ap_a_03"}
    assert len(got) == 2


def test_select_same_concept_fallback_when_excluding_empties():
    # Topic C's items all share one concept; excluding it would empty the set,
    # so the algorithm falls back to including same-concept items.
    got = select_application_practice(
        ALL_POOL, topic_id="bb_enzymes", missed_concept="michaelis_menten"
    )
    assert {i["id"] for i in got} == {"ap_c_01", "ap_c_02"}


def test_select_excludes_seen_ids():
    # Already-practiced ids are dropped so repeat misses surface NEW items.
    got = select_application_practice(
        ALL_POOL,
        topic_id="cp_electrochem",
        missed_concept="standard_cell_potential",
        seen_ids={"ap_a_02"},
    )
    assert {i["id"] for i in got} == {"ap_a_03"}


def test_select_returns_n_two_by_default():
    # Default N = 2 even though the topic has 3 eligible items.
    assert N_APPLICATION_PRACTICE == 2
    got = select_application_practice(
        ALL_POOL, topic_id="cp_electrochem", missed_concept=None
    )
    assert len(got) == 2
    # variety ranking prefers distinct concepts (all A concepts are distinct)
    assert len({i["concept"] for i in got}) == 2


def test_select_degrades_to_empty():
    # No pool coverage for the topic -> [] (caller shows the generic message).
    assert select_application_practice(
        ALL_POOL, topic_id="ps_thermo", missed_concept=None
    ) == []
    # everything already seen -> [] as well
    assert select_application_practice(
        ALL_POOL,
        topic_id="cp_acids_bases",
        missed_concept=None,
        seen_ids={"ap_b_01", "ap_b_02"},
    ) == []


def test_remediation_attempts_never_touch_scored_state():
    # Practice attempts log to the ISOLATED channel: they do NOT appear in
    # perf_attempts and never move the Performance accuracy.
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SAMPLE_QUESTIONS)
        store.upsert_remediation(ALL_POOL)
        # a real scored attempt for contrast
        store.log_attempt("q_dev_001", correct=True)

        store.log_remediation_attempt("ap_a_01", correct=False, time_seconds=12.0)
        store.log_remediation_attempt("ap_a_02", correct=True)

        # scored perf_attempts count is unchanged by the practice logging
        assert store.attempt_count() == 1
        assert store.accuracy()["attempts"] == 1
        assert store.accuracy("cp_electrochem")["attempts"] == 0
        # the seen-set reflects the practiced pool ids
        assert store.seen_remediation_ids() == {"ap_a_01", "ap_a_02"}
        # remediation ids are absent from the scored bank entirely
        row = store.conn.execute(
            "SELECT COUNT(*) FROM perf_questions WHERE id LIKE 'ap_%'"
        ).fetchone()
        assert row[0] == 0


def test_pool_items_never_returned_by_eligible_questions():
    # Even if a topic is unlocked, eligible_questions (the scored-session feed)
    # reads perf_questions only and can never surface a remediation item.
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_remediation(ALL_POOL)
        eligible = store.eligible_questions(
            {"cp_electrochem", "cp_acids_bases", "bb_enzymes"}
        )
        assert eligible == []


def test_session_application_practice_set_end_to_end():
    # Through the session helper, reading the isolated store: a miss on a
    # cp_electrochem item returns the deterministic N=2 practice set. Main-bank
    # perf_questions carry no `concept` column (only the pool does), so the
    # session sees missed_concept=None and variety-ranking picks the first two
    # distinct-concept items by id.
    col = getEmptyCol()
    q = dict(
        SAMPLE_QUESTIONS[0],
        id="q_dev_050",
        topic_id="cp_electrochem",
        cognitive_demand="application",
    )
    with PerfStore(col) as store:
        store.upsert_questions([q])
        store.upsert_remediation(ALL_POOL)
        qs = store.eligible_questions({"cp_electrochem"})
        sess = PerformanceSession(store, qs, interleaved=False)
        wrong = (sess.correct_index() + 1) % 4
        sess.answer(wrong, time_seconds=25.0)
        practice = sess.application_practice_set()
        assert len(practice) == 2
        assert {i["id"] for i in practice} == {"ap_a_01", "ap_a_02"}
        # all drawn from the isolated pool for this topic
        assert all(i["pool"] == "application_practice" for i in practice)
        assert all(i["topic_id"] == "cp_electrochem" for i in practice)
        sess.classify_error("application")


# ---------------------------------------------------------------------------
# Backend fixes: choice_feedback, session cap/modes, resume, display labels
# ---------------------------------------------------------------------------
QUESTION_WITH_FEEDBACK = dict(
    SAMPLE_QUESTIONS[0],
    choice_feedback=[
        "Incorrect — wrong step.",
        "Correct — substrate-level phosphorylation.",
        "Incorrect — confuses intermediate.",
        "Incorrect — near miss.",
    ],
)


def test_choice_feedback_roundtrips_through_store():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions([QUESTION_WITH_FEEDBACK])
        loaded = store.eligible_questions({"bb_citric_acid"})[0]
        assert loaded["choice_feedback"][1].startswith("Correct")
        assert len(loaded["choice_feedback"]) == 4


def test_migration_adds_choice_feedback_column():
    col = getEmptyCol()
    with PerfStore(col) as store:
        qcols = mcat_perf._existing_columns(store.conn, "perf_questions")
        assert "choice_feedback" in qcols


def test_cap_session_questions_limits_practice_pool():
    qs = [
        dict(SAMPLE_QUESTIONS[0], id=f"q_{i}", topic_id=f"t{i % 3}")
        for i in range(30)
    ]
    capped = mcat_perf.cap_session_questions(
        qs, session_mode=mcat_perf.SESSION_MODE_PRACTICE, rng=random.Random(0)
    )
    assert len(capped) == mcat_perf.DEFAULT_SESSION_CAP
    assert len(capped) <= mcat_perf.MAX_SESSION_CAP


def test_cap_session_questions_assessment_uses_held_out_only():
    qs = [
        dict(SAMPLE_QUESTIONS[0], id="dev1", split="dev"),
        dict(SAMPLE_QUESTIONS[0], id="ho1", split="held_out"),
        dict(SAMPLE_QUESTIONS[0], id="ho2", split="held_out"),
    ]
    capped = mcat_perf.cap_session_questions(
        qs, session_mode=mcat_perf.SESSION_MODE_ASSESSMENT
    )
    assert {q["id"] for q in capped} <= {"ho1", "ho2"}


def test_prepare_session_questions_blocked_orders_by_topic():
    qs = [
        {"id": "a", "topic_id": "t2", "split": "dev"},
        {"id": "b", "topic_id": "t1", "split": "dev"},
    ]
    out = mcat_perf.prepare_session_questions(
        qs, session_mode=mcat_perf.SESSION_MODE_PRACTICE, interleaved=False
    )
    assert [q["topic_id"] for q in out] == ["t1", "t2"]


def test_session_resume_persists_index_and_pending_miss():
    col = getEmptyCol()
    q = dict(SAMPLE_QUESTIONS[0], cognitive_demand="application")
    with PerfStore(col) as store:
        store.upsert_questions([q])
        qs = store.eligible_questions({"bb_citric_acid"})
        key = mcat_perf.session_filter_key(
            session_mode=mcat_perf.SESSION_MODE_PRACTICE, interleaved=False
        )
        sess = PerformanceSession(
            store, qs, interleaved=False, filter_key=key, session_mode="practice"
        )
        wrong = (sess.correct_index() + 1) % 4
        sess.answer(wrong, time_seconds=12.0)
        assert sess.awaiting_error_type is True
        saved = store.load_session_state(key)
        assert saved is not None
        assert saved["index"] == 0
        assert saved["awaiting_error"] is True
        assert saved["pending_choice"] == wrong

        sess2 = PerformanceSession(
            store,
            qs,
            interleaved=False,
            filter_key=key,
            session_mode="practice",
            resume=saved,
        )
        assert sess2.index == 0
        assert sess2.awaiting_error_type is True
        assert sess2.pending_inference is not None


def test_session_clear_state_on_finish():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions([SAMPLE_QUESTIONS[0]])
        qs = store.eligible_questions({"bb_citric_acid"})
        key = mcat_perf.session_filter_key(
            session_mode=mcat_perf.SESSION_MODE_PRACTICE, interleaved=False
        )
        sess = PerformanceSession(
            store, qs, interleaved=False, filter_key=key, session_mode="practice"
        )
        sess.answer(sess.correct_index())
        sess.advance()
        assert store.load_session_state(key) is None


def test_accuracy_first_attempt_only_and_split_filter():
    col = getEmptyCol()
    dev_q = dict(SAMPLE_QUESTIONS[0], id="q_dev_x", split="dev")
    ho_q = dict(SAMPLE_QUESTIONS[1], id="q_ho_x", split="held_out")
    with PerfStore(col) as store:
        store.upsert_questions([dev_q, ho_q])
        store.log_attempt("q_dev_x", correct=False)
        store.log_attempt("q_dev_x", correct=True)  # repeat — ignored by default
        store.log_attempt("q_ho_x", correct=True)

        headline = store.accuracy()
        assert headline["attempts"] == 2
        assert headline["correct"] == 1
        assert abs(headline["accuracy"] - 0.5) < 1e-9

        held = store.accuracy(split=mcat_perf.HELD_OUT_SPLIT)
        assert held["attempts"] == 1
        assert held["correct"] == 1


def test_answer_churn_logged_in_feature_json():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions([SAMPLE_QUESTIONS[0]])
        qs = store.eligible_questions({"bb_citric_acid"})
        sess = PerformanceSession(store, qs, interleaved=False)
        wrong = (sess.correct_index() + 1) % 4
        first = (wrong + 1) % 4
        sess.answer(
            wrong,
            time_seconds=15.0,
            first_choice_index=first,
            answer_changes=2,
        )
        sess.classify_error("content_gap")
        row = store.conn.execute(
            "SELECT feature_json FROM perf_attempts"
        ).fetchone()
        feat = json.loads(row["feature_json"])
        assert feat["first_choice_index"] == first
        assert feat["answer_changes"] == 2


def test_inferred_display_label_misread():
    assert (
        mcat_perf.inferred_display_label(mcat_perf.ERR_MISREAD) == "Careless read"
    )


def test_inferred_display_label_probe_pass_application():
    label = mcat_perf.inferred_display_label(
        mcat_perf.ERR_APPLICATION, probe_informed=True
    )
    assert label == mcat_perf.PROBE_PASS_REASONING_LABEL
    assert mcat_perf.inferred_display_label(mcat_perf.ERR_APPLICATION) == "Applied reasoning"


def test_session_probe_pass_recall_uses_application_display_label():
    col = getEmptyCol()
    q = dict(SAMPLE_QUESTIONS[0], cognitive_demand="recall")
    with PerfStore(col) as store:
        store.upsert_questions([q])
        qs = store.eligible_questions({"bb_citric_acid"})
        sess = PerformanceSession(store, qs, interleaved=False)
        wrong = (sess.correct_index() + 1) % 4
        sess.answer(wrong, time_seconds=20.0)
        sess.record_probe_outcome(True, card_id=None)
        assert sess.pending_inference["error_type"] == mcat_perf.ERR_APPLICATION
        assert sess.inferred_display_label == mcat_perf.PROBE_PASS_REASONING_LABEL


def test_session_application_practice_set_empty_without_pool():
    # No pool loaded -> the session helper degrades to [] (generic message).
    col = getEmptyCol()
    q = dict(SAMPLE_QUESTIONS[0], cognitive_demand="application")
    with PerfStore(col) as store:
        store.upsert_questions([q])
        qs = store.eligible_questions({"bb_citric_acid"})
        sess = PerformanceSession(store, qs, interleaved=False)
        wrong = (sess.correct_index() + 1) % 4
        sess.answer(wrong, time_seconds=25.0)
        assert sess.application_practice_set() == []
