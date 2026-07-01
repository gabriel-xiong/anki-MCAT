# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from anki import mcat_scores
from anki.mcat_perf import PerfStore
from tests.shared import getEmptyCol

QUESTIONS = [
    {
        "id": "q1",
        "stem": "Citric acid cycle question.",
        "choices": ["a", "b", "c", "d"],
        "correct": "B",
        "topic_id": "bb_citric_acid",
        "section": "BB",
        "skill": "2",
        "source_name": "OpenStax",
        "split": "dev",
    },
    {
        "id": "q2",
        "stem": "Acids/bases question.",
        "choices": ["a", "b", "c", "d"],
        "correct": "A",
        "topic_id": "cp_acids_bases",
        "section": "CP",
        "skill": "2",
        "source_name": "OpenStax",
        "split": "dev",
    },
]


def test_empty_collection_abstains_everywhere():
    col = getEmptyCol()
    with PerfStore(col) as store:
        data = mcat_scores.dashboard_data(col, store)

    assert data["memory"]["status"] == "abstain"
    assert data["memory"]["total_reviews"] == 0
    assert data["performance"]["status"] == "abstain"
    assert data["readiness"]["status"] == "abstain"
    assert data["readiness"]["range"] is None
    assert data["coverage"]["total"] == mcat_scores.TOTAL_TOPICS
    assert data["coverage"]["measured"] == 0


def test_next_action_without_attempts_points_to_memory():
    col = getEmptyCol()
    with PerfStore(col) as store:
        msg = mcat_scores.next_action(col, store)
    assert "memory" in msg.lower() or "unlock" in msg.lower()


def test_coverage_counts_topics_with_attempts():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(QUESTIONS)
        store.log_attempt("q1", correct=True)
        store.log_attempt("q2", correct=False, error_type="reasoning")

        cov = mcat_scores.coverage_summary(col, store)
        assert cov["measured"] == 2
        assert set(cov["measured_topics"]) == {"bb_citric_acid", "cp_acids_bases"}
        assert cov["pct"] == round(100 * 2 / mcat_scores.TOTAL_TOPICS)


def test_next_action_uses_most_common_error_type():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(QUESTIONS)
        # two reasoning misses on cp_acids_bases, one content_gap on bb
        store.log_attempt("q2", correct=False, error_type="reasoning")
        store.log_attempt("q2", correct=False, error_type="reasoning")
        store.log_attempt("q1", correct=False, error_type="content_gap")

        msg = mcat_scores.next_action(col, store)
        # most common error is reasoning -> interleaved harder questions
        assert "reasoning" in msg.lower() or "interleaved" in msg.lower()
        assert "Acids" in msg  # topic name resolved


def test_topic_rows_cover_full_outline_with_gate_progress():
    col = getEmptyCol()
    with PerfStore(col) as store:
        rows = mcat_scores.topic_rows(col, store)

    # every outline topic is present (honest coverage denominator)
    assert len(rows) == mcat_scores.TOTAL_TOPICS
    ids = {r["topic_id"] for r in rows}
    assert ids == mcat_scores.OUTLINE_TOPIC_IDS

    # empty collection -> nothing unlocked, full gate remaining on science topics
    sci = next(r for r in rows if r["topic_id"] == "bb_citric_acid")
    assert sci["unlocked"] is False
    assert sci["need_seen"] == mcat_scores.MIN_CARDS_SEEN_FOR_PERFORMANCE
    assert sci["need_good"] == mcat_scores.MIN_GOOD_OR_EASY_FOR_PERFORMANCE

    # CARS topics are flagged (they bypass the gate in the UI)
    assert any(r["is_cars"] for r in rows)


def test_focus_area_abstains_without_diagnosed_misses():
    col = getEmptyCol()
    with PerfStore(col) as store:
        fa = mcat_scores.focus_area(col, store)
    assert fa["status"] == "abstain"
    assert fa["error_type"] is None
    # still offers a generic next action to fall back on
    assert fa["action_label"]


def test_focus_area_maps_top_inferred_weakness_to_action():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(QUESTIONS)
        # two application misses on cp_acids_bases, one content_gap on bb
        store.log_attempt(
            "q2", correct=False, error_type="application",
            inferred_error_type="application",
        )
        store.log_attempt(
            "q2", correct=False, error_type="application",
            inferred_error_type="application",
        )
        store.log_attempt(
            "q1", correct=False, error_type="content_gap",
            inferred_error_type="content_gap",
        )

        fa = mcat_scores.focus_area(col, store)
        assert fa["status"] == "ok"
        assert fa["error_type"] == "application"
        assert fa["topic_id"] == "cp_acids_bases"
        assert fa["count"] == 2
        assert fa["kind"] == "performance"
        # one-click launch spec is "kind:topic_id"
        assert fa["launch"] == "performance:cp_acids_bases"
        assert "Acids" in fa["action_label"]


def test_focus_area_content_gap_maps_to_review():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(QUESTIONS)
        store.log_attempt(
            "q1", correct=False, error_type="content_gap",
            inferred_error_type="content_gap",
        )
        fa = mcat_scores.focus_area(col, store)
        assert fa["error_type"] == "content_gap"
        assert fa["kind"] == "review"
        assert fa["launch"] == "review:bb_citric_acid"
        assert "flashcard" in fa["action_label"].lower()


def test_focus_area_folds_legacy_self_report_and_skips_unresolved():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(QUESTIONS)
        # legacy self-report 'reasoning' folds to application; 'unresolved' ignored
        store.log_attempt(
            "q2", correct=False, error_type="reasoning",
            inferred_error_type="unresolved",
        )
        store.log_attempt(
            "q1", correct=False, error_type="misread",
            inferred_error_type="unresolved",
        )
        fa = mcat_scores.focus_area(col, store)
        # inferred is unresolved -> falls back to self_report (error_type):
        # reasoning->application (q2) and misread (q1); both count 1, tie broken
        # deterministically by type name ('application' < 'misread')
        assert fa["status"] == "ok"
        assert fa["error_type"] in {"application", "misread"}


def test_dashboard_data_includes_focus_area():
    col = getEmptyCol()
    with PerfStore(col) as store:
        data = mcat_scores.dashboard_data(col, store)
    assert "focus_area" in data
    assert data["focus_area"]["status"] == "abstain"


def test_performance_accuracy_reported():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(QUESTIONS)
        store.log_attempt("q1", correct=True)
        store.log_attempt("q2", correct=False, error_type="reasoning")

        perf = mcat_scores.performance_summary(col, store)
        assert perf["attempts"] == 2
        assert perf["correct"] == 1
        assert perf["accuracy"] == 0.5
        # still abstains (below MIN_PERF_ATTEMPTS / no unlocked topic)
        assert perf["status"] == "abstain"
