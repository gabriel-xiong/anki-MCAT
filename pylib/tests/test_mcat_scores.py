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
    # Coverage: full AAMC exam outline denominator; no content until loaded.
    assert data["coverage"]["total"] == mcat_scores.TOTAL_EXAM_TOPICS
    assert data["coverage"]["covered"] == 0
    assert data["coverage"]["pct"] == 0


def test_next_action_without_attempts_points_to_memory():
    col = getEmptyCol()
    with PerfStore(col) as store:
        msg = mcat_scores.next_action(col, store)
    assert "memory" in msg.lower() or "unlock" in msg.lower()


def test_coverage_counts_content_topics_not_user_activity():
    """Coverage = shipped content scope, NOT cards_seen or perf attempts."""
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(QUESTIONS)
        # Attempt only one of two bank topics — content coverage still counts both.
        store.log_attempt("q1", correct=True)

        cov = mcat_scores.coverage_summary(col, store)
        assert set(cov["covered_topics"]) == {"bb_citric_acid", "cp_acids_bases"}
        assert cov["covered"] == 2
        assert cov["total"] == mcat_scores.TOTAL_EXAM_TOPICS
        assert cov["pct"] == round(100 * 2 / mcat_scores.TOTAL_EXAM_TOPICS)


def test_coverage_includes_deck_cards_without_questions():
    col = getEmptyCol()
    for i in range(2):
        note = col.newNote()
        note["Front"] = f"glycolysis {i}"
        note.tags = ["topic:bb_glycolysis"]
        col.addNote(note)
    with PerfStore(col) as store:
        cov = mcat_scores.coverage_summary(col, store)
    assert "bb_glycolysis" in cov["covered_topics"]
    assert cov["covered"] == 1
    assert cov["pct"] == round(100 / mcat_scores.TOTAL_EXAM_TOPICS)


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
    # A full build ships questions for every outline topic; topic_rows is scoped
    # to the shipped bank, so seed one question per outline topic to reproduce
    # the full-outline table.
    full_bank = [
        {
            "id": f"q_{topic}",
            "stem": f"{topic} question.",
            "choices": ["a", "b", "c", "d"],
            "correct": "A",
            "topic_id": topic,
            "section": section,
            "source_name": "OpenStax",
            "split": "dev",
        }
        for section, topic, _ in mcat_scores.OUTLINE
    ]
    with PerfStore(col) as store:
        store.upsert_questions(full_bank)
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
    assert f"have 0/{mcat_scores.MIN_MEMORY_REVIEWS}" in mem["reason"]
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


def test_memory_not_measured_when_reviews_met_but_card_floor_unmet():
    """BUG 1 (profile-aware): the review-count gate can be satisfied while the
    profile's CARD gate is NOT — Memory must abstain, never "measured" + a
    "No score yet" headline at once.

    strict profile: card gate = >=1 mature card (interval >=21d).
    tester profile: card gate = >= MIN_STARTED_CARDS_FOR_MEMORY started cards.
    A single immature/started card with a flooded revlog fails BOTH, so this is
    a stable regression guard under either active profile. The single
    authoritative status must reflect whether a real number is displayable.
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

    # Flood revlog past the review-count gate so ONLY the card gate blocks.
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
    # ... but there are no mature cards and only 1 started card ...
    assert mem["mature_cards"] == 0
    assert mem["started_cards"] == 1
    # ... so it is NOT "measured" (the whole point of the fix).
    assert mem["status"] != "measured"
    assert mem["status"] == "abstain"
    assert mem["score_available"] is False
    # A real blocker reason is present (mirrors the headline); no fabricated
    # number is shown alongside it.
    assert mem["reason"]
    assert mem["memory_score"] is None


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


def test_performance_abstains_cleanly_under_gate_with_no_numeric_headline():
    """ACCURACY-CARD HONESTY BUG (regression).

    Below MIN_PERF_ATTEMPTS the Accuracy card must ABSTAIN with NO point number
    (and no credible band) — never the self-contradictory "30/100 + not enough
    data + CI 3–57". Raw accuracy stays available as an honest SECONDARY stat.
    """
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(QUESTIONS)
        store.log_attempt("q1", correct=True)  # first-attempt 1/1, n=1 << gate
        perf = mcat_scores.performance_summary(col, store)

    # Under the gate: abstain, no headline number, no band — consistently.
    assert perf["status"] == "abstain"
    assert perf["score_available"] is False
    assert perf["perf_score"] is None
    assert perf["score_low"] is None and perf["score_high"] is None
    # ...but the raw accuracy is still there for the honest "Why?" detail.
    assert perf["accuracy"] == 1.0
    assert perf["accuracy_low"] is not None and perf["accuracy_high"] is not None


def _topic_questions(topic_id, section, n, prefix):
    """n distinct questions for one topic (the headline counts FIRST attempts per
    question, so clearing the 30-attempt gate needs 30 distinct items)."""
    return [
        {
            "id": f"{prefix}{i}",
            "stem": f"{topic_id} q{i}.",
            "choices": ["a", "b", "c", "d"],
            "correct": "A",
            "topic_id": topic_id,
            "section": section,
            "source_name": "OpenStax",
            "split": "dev",
        }
        for i in range(n)
    ]


def test_performance_shows_number_when_gate_clears():
    """At/over the gate the Accuracy card shows a numeric headline + band and is
    NOT abstaining — the complement of the honesty guard above."""
    col = getEmptyCol()
    # Unlock cp_acids_bases so the "≥1 unlocked topic" gate is satisfied
    # (3 cards seen + 5 Good/Easy), mirroring the mastery-unlock test.
    for i in range(3):
        note = col.newNote()
        note["Front"] = f"acids card {i}"
        note.tags = ["topic:cp_acids_bases"]
        col.addNote(note)
    for _ in range(5):
        c = col.sched.getCard()
        assert c is not None
        col.sched.answerCard(c, 3)

    with PerfStore(col) as store:
        bank = _topic_questions("cp_acids_bases", "CP", 30, "pa")
        store.upsert_questions(bank)
        # 30 distinct first attempts -> clears MIN_PERF_ATTEMPTS.
        for i, q in enumerate(bank):
            store.log_attempt(q["id"], correct=(i % 3 != 0))
        perf = mcat_scores.performance_summary(col, store)

    assert perf["status"] == "ok"
    assert perf["score_available"] is True
    assert perf["attempts"] == 30
    assert perf["perf_score"] is not None
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
    assert perf["score_available"] is False


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

    Guards the direct ``perf_attempts`` reads in this module that must each
    filter ``idk = 0``: _section_attempt_counts and _provisional_range. Coverage
    is content-scope (deck/bank), so IDK attempts never affect it.
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

        # Flood "Not sure" abstentions on graded topics.
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

    # Content coverage unchanged by IDK (all three bank topics still covered).
    assert after_cov == baseline_cov
    assert after_sections == baseline_sections
    assert after_range == baseline_range

    assert set(baseline_cov["covered_topics"]) == {
        "bb_citric_acid",
        "cp_acids_bases",
        "ps_memory",
    }
    assert baseline_sections.get("BB") == 2
    assert baseline_sections.get("CP") == 1
    assert "PS" not in baseline_sections


# ---------------------------------------------------------------------------
# Content coverage — outline denominator, deck/bank numerator (PRD §6.5 C-1–C-4).
# ---------------------------------------------------------------------------
# A 3-topic CP+BB bank, exactly like the shipped tester (eval trio).
SCOPED_BANK = [
    {
        "id": "sc_acids",
        "stem": "Acids/bases.",
        "choices": ["a", "b", "c", "d"],
        "correct": "A",
        "topic_id": "cp_acids_bases",
        "section": "CP",
        "source_name": "OpenStax",
        "split": "dev",
    },
    {
        "id": "sc_kinetics",
        "stem": "Kinetics.",
        "choices": ["a", "b", "c", "d"],
        "correct": "A",
        "topic_id": "cp_kinetics",
        "section": "CP",
        "source_name": "OpenStax",
        "split": "dev",
    },
    {
        "id": "sc_enzymes",
        "stem": "Enzymes.",
        "choices": ["a", "b", "c", "d"],
        "correct": "A",
        "topic_id": "bb_enzymes",
        "section": "BB",
        "source_name": "OpenStax",
        "split": "dev",
    },
]


def _flood_reviews(col, n: int) -> None:
    """Insert ``n`` graded revlog rows so the memory-review Readiness gate clears
    (independent of cards; Readiness only reads the review COUNT)."""
    base = 1_600_000_000_000
    rows = [(base + i, 1, -1, 3, 1, 1, 2500, 1000, 1) for i in range(n)]
    col.db.executemany(
        "insert into revlog "
        "(id, cid, usn, ease, ivl, lastIvl, factor, time, type) "
        "values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def test_exam_outline_has_forty_nine_topics():
    assert mcat_scores.TOTAL_EXAM_TOPICS == 49
    assert mcat_scores.TOTAL_TOPICS == 18
    assert mcat_scores.OUTLINE_TOPIC_IDS <= mcat_scores.EXAM_OUTLINE_TOPIC_IDS


def test_coverage_three_topic_bank_is_six_percent():
    """Prototype 3-topic bank → 3/49 ≈ 6% content coverage (not user progress)."""
    col = getEmptyCol()
    with PerfStore(col) as store:
        store.upsert_questions(SCOPED_BANK)
        cov = mcat_scores.coverage_summary(col, store)

    assert cov["covered"] == 3
    assert cov["total"] == mcat_scores.TOTAL_EXAM_TOPICS
    assert cov["pct"] == round(100 * 3 / mcat_scores.TOTAL_EXAM_TOPICS)
    assert cov["pct"] == 6
    assert cov["pct"] < mcat_scores.MIN_COVERAGE_PCT


def test_readiness_abstains_on_content_coverage_for_scoped_bank():
    """3-topic bank ≈ 6% exam outline coverage → Readiness abstains (<50%)."""
    col = getEmptyCol()
    _flood_reviews(col, mcat_scores.MIN_MEMORY_REVIEWS + 5)
    with PerfStore(col) as store:
        store.upsert_questions(
            _topic_questions("cp_acids_bases", "CP", 20, "rc_cp")
            + _topic_questions("bb_enzymes", "BB", 15, "rc_bb")
            + _topic_questions("cp_kinetics", "CP", 3, "rc_k")
        )
        for q in _topic_questions("cp_acids_bases", "CP", 20, "rc_cp"):
            store.log_attempt(q["id"], correct=True)
        for q in _topic_questions("bb_enzymes", "BB", 15, "rc_bb"):
            store.log_attempt(q["id"], correct=True)
        read = mcat_scores.readiness_summary(col, store)
        cov = mcat_scores.coverage_summary(col, store)

    assert cov["covered"] == 3
    assert cov["pct"] == 6
    assert cov["pct"] < mcat_scores.MIN_COVERAGE_PCT
    assert read["status"] == "abstain"
    assert read["range"] is None
    assert "exam outline coverage" in read["reason"]
    assert read["coverage_pct"] == cov["pct"]


def test_readiness_still_enforces_shipped_section_gaps():
    """Scoping does NOT weaken honesty: a shipped section with too few attempts
    still blocks Readiness (here BB is shipped but unattempted), while the sole
    blocker is BB — never the un-shipped PS/CARS sections."""
    col = getEmptyCol()
    _flood_reviews(col, mcat_scores.MIN_MEMORY_REVIEWS + 5)
    with PerfStore(col) as store:
        store.upsert_questions(
            _topic_questions("cp_acids_bases", "CP", 20, "rs_cp")
            + _topic_questions("cp_kinetics", "CP", 15, "rs_k")
            + _topic_questions("bb_enzymes", "BB", 3, "rs_bb")  # shipped, unattempted
        )
        # 35 distinct attempts but ALL in CP; content coverage 3/49 ≈ 6%.
        for q in _topic_questions("cp_acids_bases", "CP", 20, "rs_cp"):
            store.log_attempt(q["id"], correct=True)
        for q in _topic_questions("cp_kinetics", "CP", 15, "rs_k"):
            store.log_attempt(q["id"], correct=True)
        read = mcat_scores.readiness_summary(col, store)

    assert read["status"] == "abstain"
    assert read["range"] is None
    assert f"BB attempts < {mcat_scores.MIN_ATTEMPTS_PER_SCIENCE_SECTION}" in read["reason"]
    assert "exam outline coverage" in read["reason"]


# ---------------------------------------------------------------------------
# Friend-tester ENGAGEMENT profile — low/attainable gates, PROVISIONAL scores,
# and the honesty safeguards that make the low gates defensible (very wide CIs
# at small n; a truly empty profile still abstains). NOT the graded methodology.
# ---------------------------------------------------------------------------
def test_active_profile_is_low_and_provisional():
    """The shipped build runs the low friend-tester gates + provisional flag."""
    assert mcat_scores.SCORE_PROFILE == "tester"
    assert mcat_scores.PROVISIONAL_SCORES is True
    # Attainable in a single ~15-20 min sitting.
    assert mcat_scores.MIN_PERF_ATTEMPTS <= 10
    assert mcat_scores.MIN_MEMORY_REVIEWS < 100
    assert mcat_scores.MIN_STARTED_CARDS_FOR_MEMORY <= 15
    # Maturity is NOT required at tester scale (21d is unreachable in a weekend).
    assert mcat_scores.REQUIRE_MEMORY_MATURITY is False


def test_readiness_range_widens_at_small_n():
    """Honesty safeguard: the provisional Readiness range is WIDER at small n
    and collapses to the strict ±6 at/above the comfortable attempt count."""
    hw = mcat_scores._readiness_half_width
    # Monotonic non-increasing as the sample grows.
    widths = [hw(n) for n in (mcat_scores.MIN_PERF_ATTEMPTS, 15, 30, 100)]
    assert widths == sorted(widths, reverse=True)
    # At/above the comfortable count the widening is zero (strict ±6 preserved).
    assert hw(mcat_scores.READINESS_COMFORTABLE_ATTEMPTS) == 6
    assert hw(1000) == 6
    # At the tester gate the half-width is much wider (no false precision).
    assert hw(mcat_scores.MIN_PERF_ATTEMPTS) >= 15


def test_perf_credible_interval_is_very_wide_at_tester_gate():
    """At the tester attempts gate the credible interval must be VERY wide, so a
    computed Accuracy score visibly reads as rough (no false precision)."""
    n = mcat_scores.MIN_PERF_ATTEMPTS
    lo, hi = mcat_scores._perf_band(n // 2, n)  # ~50% accuracy at the gate
    # Span at least ~30 points on the 0-100 scale at this small n.
    assert (hi - lo) * 100 >= 30


def test_readiness_abstains_for_full_v1_bank_on_exam_outline():
    """Even the full v1 prototype bank (18 topics) is only 18/49 — Readiness abstains."""
    col = getEmptyCol()
    _flood_reviews(col, mcat_scores.MIN_MEMORY_REVIEWS + 5)
    full_bank = [
        {
            "id": f"q_{topic}",
            "stem": f"{topic} question.",
            "choices": ["a", "b", "c", "d"],
            "correct": "A",
            "topic_id": topic,
            "section": section,
            "source_name": "OpenStax",
            "split": "dev",
        }
        for section, topic, _ in mcat_scores.OUTLINE
    ]
    with PerfStore(col) as store:
        store.upsert_questions(full_bank)
        for q in full_bank:
            store.log_attempt(q["id"], correct=True)
        read = mcat_scores.readiness_summary(col, store)
        cov = mcat_scores.coverage_summary(col, store)

    assert cov["covered"] == mcat_scores.TOTAL_TOPICS
    assert cov["total"] == mcat_scores.TOTAL_EXAM_TOPICS
    assert cov["pct"] == round(100 * 18 / 49)
    assert cov["pct"] < mcat_scores.MIN_COVERAGE_PCT
    assert read["status"] == "abstain"
    assert read["range"] is None
    assert "exam outline coverage" in read["reason"]


def test_empty_profile_still_abstains_under_tester_gates():
    """Even at the low friend-tester gates, a profile with ZERO data must
    abstain on all three scores — the gates never fabricate a number."""
    col = getEmptyCol()
    with PerfStore(col) as store:
        data = mcat_scores.dashboard_data(col, store)
    assert data["memory"]["status"] == "abstain"
    assert data["memory"]["memory_score"] is None
    assert data["performance"]["status"] == "abstain"
    assert data["performance"]["perf_score"] is None
    assert data["readiness"]["status"] == "abstain"
    assert data["readiness"]["range"] is None


def test_memory_computes_from_retrievability_without_maturity_at_tester_scale():
    """tester profile: Memory computes from FSRS retrievability (EARLY recall
    strength) WITHOUT any 21-day-mature card, once the small started-card floor
    is met — and it is flagged provisional with a band present."""
    if mcat_scores.REQUIRE_MEMORY_MATURITY:  # pragma: no cover - strict build
        import pytest

        pytest.skip("strict profile requires maturity")

    from anki.config import Config

    col = getEmptyCol()
    if col.sched_ver() != 2:
        col.upgrade_to_v2_scheduler()
    col.set_config_bool(Config.Bool.SCHED_2021, True)
    col.set_config("fsrs", True)
    col._load_scheduler()

    n = mcat_scores.MIN_STARTED_CARDS_FOR_MEMORY + 2
    for i in range(n):
        note = col.newNote()
        note["Front"] = f"mem card {i}"
        note.tags = ["topic:cp_acids_bases"]
        col.addNote(note)
    for _ in range(n):
        c = col.sched.getCard()
        assert c is not None
        col.sched.answerCard(c, 3)  # Good -> FSRS memory_state populated
    # Force every card immature so the score CANNOT be coming from maturity.
    col.db.execute("update cards set ivl = 1 where reps > 0")

    mem = mcat_scores.memory_summary(col)
    assert mem["mature_cards"] == 0  # nothing is 21-day mature
    assert mem["started_cards"] >= mcat_scores.MIN_STARTED_CARDS_FOR_MEMORY
    assert mem["avg_retrievability"] is not None and mem["avg_retrievability"] > 0
    assert mem["status"] == "measured"
    assert mem["memory_score"] is not None
    assert mem["maturity_applied"] is False
    assert mem["provisional"] is True
    assert mem["score_low"] is not None and mem["score_high"] is not None


def test_memory_scores_untagged_default_deck_cards():
    """BUG: Memory abstained after a full Default-deck pass because retrievability
    was computed only from topic:-tagged cards while review/card gates counted
    the whole deck. Untagged reviewed cards must contribute."""
    if mcat_scores.REQUIRE_MEMORY_MATURITY:  # pragma: no cover
        import pytest

        pytest.skip("strict profile requires maturity")

    from anki.config import Config

    col = getEmptyCol()
    if col.sched_ver() != 2:
        col.upgrade_to_v2_scheduler()
    col.set_config_bool(Config.Bool.SCHED_2021, True)
    col.set_config("fsrs", True)
    col._load_scheduler()

    n = mcat_scores.MIN_STARTED_CARDS_FOR_MEMORY + 3
    for i in range(n):
        note = col.newNote()
        note["Front"] = f"default deck card {i}"
        # Deliberately NO topic: tag — mirrors the shipped Default deck.
        col.addNote(note)
    for _ in range(n):
        c = col.sched.getCard()
        assert c is not None
        col.sched.answerCard(c, 3)

    assert col.get_topic_mastery() == []
    mem = mcat_scores.memory_summary(col)
    assert mem["total_reviews"] >= n
    assert mem["started_cards"] >= mcat_scores.MIN_STARTED_CARDS_FOR_MEMORY
    assert mem["retrievability_cards"] >= mcat_scores.MIN_STARTED_CARDS_FOR_MEMORY
    assert mem["avg_retrievability"] is not None and mem["avg_retrievability"] > 0
    assert mem["status"] == "measured"
    assert mem["memory_score"] is not None
    assert mem["score_available"] is True


def test_memory_abstain_message_when_fsrs_disabled():
    """When reviews exist but FSRS is off, the blocker must name FSRS explicitly."""
    col = getEmptyCol()
    n = mcat_scores.MIN_STARTED_CARDS_FOR_MEMORY + 2
    for i in range(n):
        note = col.newNote()
        note["Front"] = f"card {i}"
        col.addNote(note)
    for _ in range(n):
        c = col.sched.getCard()
        assert c is not None
        col.sched.answerCard(c, 3)

    mem = mcat_scores.memory_summary(col)
    assert mem["started_cards"] >= mcat_scores.MIN_STARTED_CARDS_FOR_MEMORY
    assert mem["retrievability_cards"] == 0
    assert mem["status"] == "abstain"
    assert "FSRS" in mem["reason"]
    assert f"0/{mem['started_cards']}" in mem["reason"]
    assert "Enable FSRS" in mem["reason"]
