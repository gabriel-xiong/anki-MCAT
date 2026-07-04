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

        # Wilson 95% interval present and sane: bounds bracket the point
        # estimate and stay within [0, 1].
        lo, hi = perf["accuracy_low"], perf["accuracy_high"]
        assert lo is not None and hi is not None
        assert 0.0 <= lo <= perf["accuracy"] <= hi <= 1.0
        # at only n=2 the interval is wide (mostly uninformative)
        assert hi - lo > 0.5


def test_performance_interval_none_without_attempts():
    col = getEmptyCol()
    with PerfStore(col) as store:
        perf = mcat_scores.performance_summary(col, store)
    # no attempts -> no fake range (honest abstain)
    assert perf["accuracy"] is None
    assert perf["accuracy_low"] is None
    assert perf["accuracy_high"] is None


def test_performance_interval_narrows_with_n():
    # Same accuracy (50%), growing n: the Wilson interval must get strictly
    # narrower. This is the promised "range widens at low n" behaviour and is
    # exactly the helper performance_summary() uses.
    def width(k: int, n: int) -> float:
        lo, hi = mcat_scores._wilson(k, n)
        assert 0.0 <= lo <= k / n <= hi <= 1.0  # brackets phat, within [0,1]
        return hi - lo

    widths = [width(n // 2, n) for n in (10, 30, 100, 1000)]
    assert widths == sorted(widths, reverse=True)
    assert len(set(widths)) == len(widths)  # strictly monotonic
    # concrete n=10 vs n=100 comparison called out in the spec
    assert width(5, 10) > width(50, 100)


# ---------------------------------------------------------------------------
# Memory — 0-100 "Recall strength" = retrievability × deck maturity.
# ---------------------------------------------------------------------------
def test_memory_score_monotonic_in_retrievability_and_maturity():
    ms = mcat_scores._memory_score

    # Monotonic non-decreasing in R at fixed maturity, and it VARIES.
    r_scores = [ms(r, 0.6) for r in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)]
    assert r_scores == sorted(r_scores)
    assert r_scores[0] < r_scores[-1]

    # Monotonic non-decreasing in maturity at fixed R, and it VARIES.
    m_scores = [ms(0.7, m) for m in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert m_scores == sorted(m_scores)
    assert m_scores[0] < m_scores[-1]

    # Bounds + the exact product identity + None passthrough.
    assert ms(1.0, 1.0) == 100
    assert ms(0.0, 1.0) == 0
    assert ms(1.0, 0.0) == 0
    assert all(0 <= s <= 100 for s in r_scores + m_scores)
    assert ms(None, 0.5) is None  # no retrievability estimate -> no score


def test_memory_band_narrows_as_cards_grow_and_widens_when_few():
    band = mcat_scores._memory_band
    widths = []
    for n in (5, 20, 100, 1000):
        lo, hi = band(0.6, 0.5, n)
        assert 0 <= lo <= hi <= 100  # clamped + ordered
        widths.append(hi - lo)
    # Widens when few cards contribute; narrows strictly as card count grows.
    assert widths == sorted(widths, reverse=True)
    assert len(set(widths)) == len(widths)
    # No estimate / no started cards -> honest empty band.
    assert band(None, 0.5, 100) == (None, None)
    assert band(0.6, 0.5, 0) == (None, None)


def test_deck_maturity_counts_started_and_mature_cards():
    col = getEmptyCol()
    # Empty deck: nothing started, nothing mature.
    assert mcat_scores._deck_maturity(col) == (0, 0, 0.0)

    for i in range(4):
        note = col.newNote()
        note["Front"] = f"card {i}"
        col.addNote(note)
    # Answer each once so reps > 0 (a "started" card).
    for _ in range(4):
        c = col.sched.getCard()
        assert c is not None
        col.sched.answerCard(c, 3)

    # Force two started cards to a mature interval (>= 21 days).
    ids = [r[0] for r in col.db.all("select id from cards order by id limit 2")]
    placeholders = ",".join("?" for _ in ids)
    col.db.execute(
        f"update cards set ivl = ? where id in ({placeholders})",
        mcat_scores.MEMORY_MATURITY_INTERVAL_DAYS + 5,
        *ids,
    )

    mature, started, maturity = mcat_scores._deck_maturity(col)
    assert started == 4
    assert mature == 2
    assert maturity == 0.5


def test_memory_summary_exposes_headline_band_and_abstains_when_empty():
    col = getEmptyCol()
    mem = mcat_scores.memory_summary(col)
    # Abstain gate still fires below MIN_MEMORY_REVIEWS.
    assert mem["status"] == "abstain"
    assert str(mcat_scores.MIN_MEMORY_REVIEWS) in mem["reason"]
    # New headline + band fields present; None on an empty deck (no data).
    assert mem["memory_score"] is None
    assert mem["score_low"] is None and mem["score_high"] is None
    # Secondary raw stats present + honest.
    assert mem["total_reviews"] == 0
    assert mem["started_cards"] == 0
    assert mem["mature_cards"] == 0
    assert mem["mature_pct"] == 0
    # Empty deck is not measurable, and the authoritative status is never
    # "measured" (drives the dashboard "measured" badge).
    assert mem["status"] != "measured"
    assert mem["score_available"] is False


def test_memory_not_measured_with_reviews_but_no_mature_cards():
    """BUG 1: >= MIN_MEMORY_REVIEWS graded reviews but 0 mature cards must NOT
    report status "measured".

    Regression guard for the contradiction where the dashboard showed the
    "measured" badge (gated only on review COUNT) while the headline said
    "No score yet / Need N mature cards" (a separate maturity gate). The single
    authoritative status must reflect whether a real Recall-strength number is
    displayable, so these can never disagree.
    """
    col = getEmptyCol()

    # One started card, kept immature (interval < MEMORY_MATURITY_INTERVAL_DAYS).
    note = col.newNote()
    note["Front"] = "immature card"
    col.addNote(note)
    c = col.sched.getCard()
    assert c is not None
    col.sched.answerCard(c, 3)
    col.db.execute("update cards set ivl = 1 where reps > 0")

    # Flood revlog past the review-count gate so ONLY the maturity gate blocks.
    base = 1_600_000_000_000
    rows = [
        (base + i, c.id, -1, 3, 1, 1, 2500, 1000, 1)
        for i in range(mcat_scores.MIN_MEMORY_REVIEWS + 5)
    ]
    col.db.executemany(
        "insert into revlog "
        "(id, cid, usn, ease, ivl, lastIvl, factor, time, type) "
        "values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )

    mem = mcat_scores.memory_summary(col)
    # Review-count gate is satisfied ...
    assert mem["total_reviews"] >= mcat_scores.MIN_MEMORY_REVIEWS
    # ... but there are no mature cards ...
    assert mem["mature_cards"] == 0
    # ... so it is NOT "measured" (the whole point of the fix).
    assert mem["status"] != "measured"
    assert mem["status"] == "abstain"
    assert mem["score_available"] is False
    # The blocker reason points at the maturity gate, mirroring the headline.
    assert "mature" in (mem["reason"] or "").lower()


# ---------------------------------------------------------------------------
# Performance — 0-100 reliability-adjusted accuracy (Beta shrinkage).
# ---------------------------------------------------------------------------
def _perf_score(k: int, n: int) -> int:
    """Mirror performance_summary's headline rounding of the shrinkage mean."""
    mean = mcat_scores._perf_shrinkage(k, n)
    assert mean is not None
    return mcat_scores._round_half_up(mean * 100)


def test_perf_score_pulls_small_n_and_never_awards_full_for_tiny_n():
    # 1/1 must NOT read ~100 — Beta(2,2) shrinks it to 3/5 = 60.
    assert _perf_score(1, 1) == 60
    # 0/1 is pulled UP toward 50 (2/5 = 40), not 0.
    assert _perf_score(0, 1) == 40
    # Small n pulled toward 50: raw 75% -> 63 at 3/4.
    assert _perf_score(3, 4) == 63
    assert 50 < _perf_score(3, 4) < 75
    # n <= 0 -> None (honest abstain, no fabricated score).
    assert mcat_scores._perf_shrinkage(0, 0) is None


def test_perf_score_converges_to_raw_accuracy_as_n_grows():
    shrink = mcat_scores._perf_shrinkage
    for acc in (0.3, 0.5, 0.8):
        prev_gap = None
        for n in (10, 100, 1000, 10000):
            k = round(acc * n)
            gap = abs(shrink(k, n) - acc)
            if prev_gap is not None:
                assert gap <= prev_gap + 1e-12  # gap to raw accuracy shrinks
            prev_gap = gap
        assert prev_gap < 1e-3  # effectively converged at large n
    # Spec demonstration points: convergence toward raw 75%.
    assert _perf_score(18, 28) == 63  # small n, still pulled toward 50
    assert _perf_score(60, 80) == 74  # closing in on raw 75
    assert _perf_score(600, 800) == 75  # converged to raw accuracy


def test_perf_band_narrows_with_n():
    band = mcat_scores._perf_band
    widths = []
    for n in (4, 30, 100, 1000):
        lo, hi = band(n // 2, n)
        assert 0.0 <= lo <= hi <= 1.0  # clamped + ordered
        widths.append(hi - lo)
    assert widths == sorted(widths, reverse=True)
    assert len(set(widths)) == len(widths)  # strictly narrowing
    assert band(0, 0) == (None, None)  # no attempts -> no band


def test_performance_summary_headline_is_shrinkage_score_with_band():
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(QUESTIONS)
        store.log_attempt("q1", correct=True)  # first-attempt 1/1
        perf = mcat_scores.performance_summary(col, store)

    # Headline shrinks 1/1 to 60 (not ~100); raw accuracy stays honest at 100%.
    assert perf["perf_score"] == 60
    assert perf["accuracy"] == 1.0
    # Still abstains below MIN_PERF_ATTEMPTS.
    assert perf["status"] == "abstain"
    # Credible band present, brackets the score, stays within [0, 100].
    lo, hi = perf["score_low"], perf["score_high"]
    assert lo is not None and hi is not None
    assert 0 <= lo <= perf["perf_score"] <= hi <= 100


def test_performance_summary_score_none_without_attempts():
    col = getEmptyCol()
    with PerfStore(col) as store:
        perf = mcat_scores.performance_summary(col, store)
    # No attempts -> no fabricated score or band (honest abstain).
    assert perf["perf_score"] is None
    assert perf["score_low"] is None and perf["score_high"] is None


# ---------------------------------------------------------------------------
# Readiness — numeric 0-100 confidence (in-lane: coverage, attempts, range width).
# ---------------------------------------------------------------------------
def test_readiness_confidence_in_range_and_monotonic():
    conf = mcat_scores._readiness_confidence
    # Bounded to [0, 100].
    assert 0 <= conf(0.0, 0, 56) <= 100
    assert 0 <= conf(1.0, 10000, 0) <= 100

    # Rises with coverage (attempts + range width held fixed).
    assert conf(0.3, 50, 12) < conf(0.6, 50, 12) < conf(1.0, 50, 12)
    # Rises with attempts / sample size (coverage + range width fixed).
    assert conf(0.7, 10, 12) < conf(0.7, 60, 12) < conf(0.7, 300, 12)
    # Rises as the range NARROWS (coverage + attempts fixed).
    assert conf(0.7, 60, 30) < conf(0.7, 60, 12) < conf(0.7, 60, 0)


def test_readiness_confidence_low_vs_high_examples():
    conf = mcat_scores._readiness_confidence
    # Bare-minimum eligibility (coverage 50%, 30 attempts, provisional width 12)
    # yields a modest, honest number; a well-supported map yields a large one.
    low = conf(0.50, 30, 12)
    high = conf(1.00, 300, 12)
    assert low < high
    assert mcat_scores._confidence_bucket(low) in ("low", "moderate")
    assert mcat_scores._confidence_bucket(high) == "high"
    # Bucket cutoffs behave as documented (<40 low, 40-70 moderate, 70+ high).
    assert mcat_scores._confidence_bucket(39) == "low"
    assert mcat_scores._confidence_bucket(40) == "moderate"
    assert mcat_scores._confidence_bucket(69) == "moderate"
    assert mcat_scores._confidence_bucket(70) == "high"


def test_readiness_summary_abstains_without_confidence_number():
    # No data -> abstain, no range, and NO fabricated confidence number.
    col = getEmptyCol()
    with PerfStore(col) as store:
        read = mcat_scores.readiness_summary(col, store)
    assert read["status"] == "abstain"
    assert read["range"] is None
    assert "confidence_score" not in read


def test_idk_rows_are_invisible_to_readiness():
    """"Not sure" (IDK) abstentions contribute NO signal to Readiness.

    Guards the three direct ``perf_attempts`` reads in this module that must
    each filter ``idk = 0`` (mirroring accuracy()/idk_count() in mcat_perf):
    coverage_summary, _section_attempt_counts, and _provisional_range. An IDK
    row is logged (correct=0, content_gap route) so the focus area still sees
    it, but it must move neither coverage, nor the provisional range, nor the
    section attempt counts.
    """
    col = getEmptyCol()
    # q1 -> bb_citric_acid/BB, q2 -> cp_acids_bases/CP (from QUESTIONS). q3 is a
    # topic whose ONLY engagement will be IDK, so it must never count.
    idk_only_q = {
        "id": "q3",
        "stem": "Memory and cognition question.",
        "choices": ["a", "b", "c", "d"],
        "correct": "C",
        "topic_id": "ps_memory",
        "section": "PS",
        "skill": "2",
        "source_name": "OpenStax",
        "split": "dev",
    }
    with PerfStore(col) as store:
        store.upsert_questions(QUESTIONS + [idk_only_q])
        # Real, scored attempts — the only thing Readiness should ever see.
        store.log_attempt("q1", correct=True)
        store.log_attempt("q1", correct=False, error_type="content_gap")
        store.log_attempt("q2", correct=True)

        baseline_cov = mcat_scores.coverage_summary(col, store)
        baseline_sections = mcat_scores._section_attempt_counts(store)
        baseline_range = mcat_scores._provisional_range(store)

        # Flood "Not sure" abstentions: extra IDK on the graded topics PLUS a
        # topic (ps_memory) whose only engagement is IDK. Non-CARS IDK routes
        # to content_gap and is stored correct=0.
        for _ in range(5):
            store.log_attempt("q1", correct=False, error_type="content_gap", idk=True)
            store.log_attempt("q2", correct=False, error_type="content_gap", idk=True)
            store.log_attempt("q3", correct=False, error_type="content_gap", idk=True)

        after_cov = mcat_scores.coverage_summary(col, store)
        after_sections = mcat_scores._section_attempt_counts(store)
        after_range = mcat_scores._provisional_range(store)

        # The IDK rows really were stored — so the equalities below prove the
        # filters exclude them, not that logging silently dropped the rows.
        assert store.idk_count() == 15

    # IDK moves NONE of the three Readiness inputs.
    assert after_cov == baseline_cov
    assert after_sections == baseline_sections
    assert after_range == baseline_range

    # Concretely: the IDK-only topic/section never becomes "measured", and the
    # graded baseline actually carried signal (equality isn't comparing empties).
    assert set(baseline_cov["measured_topics"]) == {"bb_citric_acid", "cp_acids_bases"}
    assert "ps_memory" not in after_cov["measured_topics"]
    assert "PS" not in after_sections
    assert baseline_sections.get("BB") == 2
    assert baseline_sections.get("CP") == 1
