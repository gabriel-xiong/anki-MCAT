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

import math
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
# Thresholds — score abstain gates. TWO pre-registered profiles.
#
# See MCAT/docs/EVAL-THRESHOLDS.md + docs/DECISIONS.md (§33) for the full
# rationale. In short:
#
#   * "strict"  — the full-course production gates (the original
#     data/scoring-config.json give_up: 200 reviews, 30 attempts, 21-day card
#     maturity). These are what a GRADED readiness claim would use.
#   * "tester"  — FRIEND-TESTER ENGAGEMENT gates. Deliberately LOW so a casual
#     friend who studies for a single ~15-20 min sitting (≈10-20 cards rated,
#     ≈8-12 questions across 1-2 topics) still sees all three PROVISIONAL scores
#     populate — so the build "feels" alive. NOT used for graded readiness
#     claims. Honesty is preserved by (a) VERY WIDE confidence intervals at these
#     small n (no false precision), (b) PROVISIONAL / small-n labels on every
#     score, (c) coverage %, a missing-data note and a next action on each, and
#     (d) a truly EMPTY profile (0 data) STILL abstaining.
#
# This build ships the "tester" profile. The device computes live, provisional
# scores; it never fabricates — 0 data always abstains.
# ---------------------------------------------------------------------------
SCORE_PROFILE = "tester"  # "tester" | "strict"
PROVISIONAL_SCORES = SCORE_PROFILE == "tester"

# strict (full-course) gates — retained for reference / a future graded build.
_STRICT_THRESHOLDS = {
    "min_memory_reviews": 200,
    "min_perf_attempts": 30,
    "min_coverage_pct": 50,
    "min_attempts_per_science_section": 5,
    "min_cars_attempts": 5,
    # Memory: require ≥1 card at a mature interval (≥21d) AND score = R × maturity.
    "require_memory_maturity": True,
    "min_started_cards_for_memory": 0,  # governed by the maturity gate instead
}
# tester-scale (friend-tester engagement) gates — ACTIVE this build.
_TESTER_THRESHOLDS = {
    "min_memory_reviews": 10,
    "min_perf_attempts": 8,
    # Coverage unchanged: the 3-topic shipped scope already makes 2/3 = 66%
    # reachable, so 50% is honest AND attainable in one short sitting.
    "min_coverage_pct": 50,
    "min_attempts_per_science_section": 3,
    "min_cars_attempts": 3,
    # Memory: 21-day maturity is unreachable in a weekend, so we do NOT require
    # it; Memory instead reports EARLY RECALL STRENGTH = FSRS retrievability only
    # (score = R × 100), gated on a small floor of reviewed cards so the average
    # is not from 1-2 cards. Caveated as provisional / short-interval everywhere.
    "require_memory_maturity": False,
    "min_started_cards_for_memory": 10,
}
_ACTIVE_THRESHOLDS = (
    _TESTER_THRESHOLDS if SCORE_PROFILE == "tester" else _STRICT_THRESHOLDS
)

MIN_MEMORY_REVIEWS = _ACTIVE_THRESHOLDS["min_memory_reviews"]
MIN_PERF_ATTEMPTS = _ACTIVE_THRESHOLDS["min_perf_attempts"]
MIN_UNLOCKED_TOPICS = 1
MIN_COVERAGE_PCT = _ACTIVE_THRESHOLDS["min_coverage_pct"]
MIN_ATTEMPTS_PER_SCIENCE_SECTION = _ACTIVE_THRESHOLDS["min_attempts_per_science_section"]
MIN_CARS_ATTEMPTS = _ACTIVE_THRESHOLDS["min_cars_attempts"]
# Memory maturity handling (see profile notes above).
REQUIRE_MEMORY_MATURITY = _ACTIVE_THRESHOLDS["require_memory_maturity"]
MIN_STARTED_CARDS_FOR_MEMORY = _ACTIVE_THRESHOLDS["min_started_cards_for_memory"]
# Sample size at/above which the Readiness range needs NO small-n widening (the
# strict full-course attempt count). Below it the range widens so a small-n
# estimate visibly reads as rough (honesty safeguard; see _readiness_half_width).
READINESS_COMFORTABLE_ATTEMPTS = _STRICT_THRESHOLDS["min_perf_attempts"]

# ---------------------------------------------------------------------------
# Score-model params — mirror data/scoring-config.json ("memory"/"performance").
# Hand-mirrored into the Kotlin port (McatScores.kt); keep all three in lockstep.
# ---------------------------------------------------------------------------
# Memory "Recall strength" 0-100 = retrievability × deck maturity.
#   maturity = fraction of *started* cards (>=1 review) that are memory-mature
#   (scheduling interval >= 21 days). This is a card-level MEMORY property
#   (interval/stability); it is NOT exam-topic coverage (coverage stays in
#   Readiness only — the three scores are never blended).
MEMORY_MATURITY_INTERVAL_DAYS = 21
MEMORY_BAND_Z = 1.96  # 95% band around the memory score

# Performance reliability-adjusted accuracy 0-100 = posterior mean of
#   Beta(k + a0, (n-k) + b0) on first-attempt correctness. Weak prior Beta(2,2)
#   (prior mean 0.5, effective prior n=4): converges to raw accuracy as n grows,
#   pulls small n toward 50, never awards ~100 for tiny n.
PERF_PRIOR_A0 = 2.0
PERF_PRIOR_B0 = 2.0
PERF_BAND_Z = 1.96  # 95% credible band (normal approx to the Beta posterior)

# Readiness band per section.
SECTION_MIN = 118
SECTION_MAX = 132
SCIENCE_SECTIONS = ("CP", "BB", "PS")

# ---------------------------------------------------------------------------
# Readiness confidence — a numeric 0-100 summary of how well-supported the
# readiness range is. NEVER-BLEND NOTE: this is derived ONLY from
# readiness-domain inputs (coverage fraction, performance attempt count /
# sample size, and the width of the estimated score range). It does NOT read
# the Memory or Performance HEADLINE scores — the three scores are never
# blended. (Readiness already legitimately consumes coverage + attempts to
# produce its range; this confidence is just a summary of how well that range
# is supported.)
#
# confidence = 100 · (W_COVERAGE·cov_term + W_ATTEMPTS·att_term + W_RANGE·range_term)
#   cov_term   = clamp(coverage_fraction, 0, 1)          — more coverage -> higher
#   att_term   = n / (n + ATTEMPTS_K)                    — more attempts -> higher
#   range_term = 1 - min(width, RANGE_MAX) / RANGE_MAX   — narrower range -> higher
# Weights sum to 1 so the result stays in [0, 100]. Monotonic in each term in
# the intuitive direction.
READINESS_CONF_W_COVERAGE = 0.4
READINESS_CONF_W_ATTEMPTS = 0.4
READINESS_CONF_W_RANGE = 0.2
# Attempts reliability half-saturation: att_term = n / (n + K). K = 60 = 2× the
# MIN_PERF_ATTEMPTS abstain gate, so merely clearing the gate (n=30) yields
# ~0.33 — honestly sub-half for a still-thin sample — and -> 1.0 as n grows.
READINESS_CONF_ATTEMPTS_K = 60.0
# Range-width -> [0, 1]. A spread of RANGE_MAX points (or wider) on the 472–528
# scale contributes zero confidence; the provisional ±6 map (width 12) -> 0.6.
READINESS_CONF_RANGE_MAX = 30.0
# Qualitative bucket cutoffs on the 0-100 confidence (kept as a secondary label
# for backward-compatible consumers; the headline detail shows the NUMBER).
READINESS_CONF_LOW_MAX = 40  # < 40 -> "low"
READINESS_CONF_HIGH_MIN = 70  # >= 70 -> "high"; in between -> "moderate"

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

# Section code -> full section title. Mirrors data/mcat-outline.v1.json
# sections[].name. Keep in sync if that file changes.
NAME_OF_SECTION = {
    "CP": "Chemical and Physical Foundations of Biological Systems",
    "CARS": "Critical Analysis and Reasoning Skills",
    "BB": "Biological and Biochemical Foundations of Living Systems",
    "PS": "Psychological, Social, and Biological Foundations of Behavior",
}


def topic_display_name(topic_id: str) -> str:
    """User-facing topic name for a topic id.

    Returns the curated outline name when known. For an unknown id, strips the
    leading section slug (e.g. ``xx_``) and title-cases the remainder so the UI
    never shows a raw id (``xx_foo_bar`` -> "Foo Bar"). An empty/None id yields
    an empty string.
    """
    if not topic_id:
        return ""
    known = NAME_OF_TOPIC.get(topic_id)
    if known is not None:
        return known
    slug = topic_id.split("_", 1)[1] if "_" in topic_id else topic_id
    return slug.replace("_", " ").title()


def section_display_name(code: str) -> str:
    """Full section title for a section code, falling back to the code itself."""
    return NAME_OF_SECTION.get(code, code)

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
# Rounding — half-up, so the desktop (Python) and phone (Kotlin) score ports
# agree EXACTLY even on .5 boundaries. Python's built-in round() uses banker's
# rounding (round-half-to-even) whereas Kotlin's Math.round() rounds half up;
# for x >= 0, math.floor(x + 0.5) == Math.round(x). Every new 0-100 score/band
# below is rounded through this helper (all values are >= 0).
# ---------------------------------------------------------------------------
def _round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


# ---------------------------------------------------------------------------
# Memory — headline "Recall strength" 0-100 = retrievability × deck maturity.
#
# NEVER-BLEND NOTE: Memory uses ONLY memory-model signals — FSRS predicted
# retrievability and card-level maturity (scheduling interval/stability). It does
# NOT read MCAT topic coverage; that belongs to Readiness alone.
# ---------------------------------------------------------------------------
def _deck_maturity(col: Collection) -> tuple[int, int, float]:
    """(mature_cards, started_cards, maturity_fraction).

    "Deck maturity" here is a *memory-domain* property of the cards, not exam
    coverage: the fraction of *started* cards (>=1 review, ``reps > 0``) whose
    scheduling interval has reached the Anki mature threshold
    (``ivl >= MEMORY_MATURITY_INTERVAL_DAYS``, i.e. 21 days). Mature cards are a
    strict subset of started cards, so ``maturity`` ∈ [0, 1].
    """
    started = int(col.db.scalar("select count() from cards where reps > 0") or 0)
    mature = int(
        col.db.scalar(
            "select count() from cards where reps > 0 and ivl >= ?",
            MEMORY_MATURITY_INTERVAL_DAYS,
        )
        or 0
    )
    maturity = (mature / started) if started else 0.0
    return mature, started, maturity


def _memory_score(
    retrievability: Optional[float], maturity: float
) -> Optional[int]:
    """0-100 Recall strength = ``round(R × Mat × 100)``.

    Monotonic increasing in both R∈[0,1] and Mat∈[0,1]. We keep the plain
    product (documented choice — no sqrt/softening) so the number is a literal
    "how much of a fully-mature, fully-recalled deck you have". Returns ``None``
    when retrievability is unknown (no FSRS memory state on any seen card).
    """
    if retrievability is None:
        return None
    return _round_half_up(retrievability * maturity * 100)


def _memory_band(
    retrievability: Optional[float],
    maturity: float,
    n_cards: int,
    z: float = MEMORY_BAND_Z,
) -> tuple[Optional[int], Optional[int]]:
    """(low, high) uncertainty band in 0-100 score points around the score.

    Propagates the standard error of the mean retrievability across the ``n``
    contributing (started) cards through the score ``= R × Mat × 100``. Since
    ``d(score)/dR = Mat × 100``:

        SE(R) ≈ sqrt(R·(1-R) / n)   (mean of a bounded [0,1] quantity)
        half  = z · SE(R) · Mat · 100

    The band therefore *widens when few cards contribute* and *narrows as the
    card count grows*. Clamped to [0, 100]. Returns ``(None, None)`` when there
    is no retrievability estimate or no started cards.

    IMPLEMENTATION NOTE — boundary-safe interval: we use the WILSON interval on
    the mean recall probability (modeling ``k = round(R·n)`` of ``n`` started
    cards as "currently recalled"), scaled by the maturity factor. The plain
    normal SE ``z·sqrt(R(1-R)/n)`` COLLAPSES to zero as ``R → 1``, which shows
    FALSE PRECISION right after a review (FSRS retrievability ≈ 1.0). Wilson
    stays honestly WIDE at small ``n`` even at the boundary (e.g. 18/18 →
    ~[0.82, 1.0]) — the small-sample honesty safeguard — while still narrowing
    as ``n`` grows.
    """
    if retrievability is None or n_cards <= 0:
        return (None, None)
    k = max(0, min(n_cards, int(round(retrievability * n_cards))))
    lo_p, hi_p = _wilson(k, n_cards, z)
    low = max(0.0, lo_p * maturity * 100)
    high = min(100.0, hi_p * maturity * 100)
    return (_round_half_up(low), _round_half_up(high))


def memory_summary(col: Collection) -> dict[str, Any]:
    total_reviews = int(col.db.scalar("select count() from revlog") or 0)
    mastery = list(col.get_topic_mastery())
    topics_studied = sum(1 for m in mastery if m.cards_seen > 0)
    unlocked = sum(1 for m in mastery if m.performance_unlocked)

    # R = mean FSRS predicted retrievability over seen/reviewed cards. The Rust
    # query returns per-topic means, so weight by cards_seen instead of giving a
    # tiny topic the same influence as a large one.
    retr_weight = sum(
        m.cards_seen for m in mastery if m.avg_retrievability > 0 and m.cards_seen > 0
    )
    avg_retr = (
        sum(
            m.avg_retrievability * m.cards_seen
            for m in mastery
            if m.avg_retrievability > 0 and m.cards_seen > 0
        )
        / retr_weight
        if retr_weight
        else None
    )

    # Mat = card-level memory maturity (interval >= 21d), NOT exam coverage.
    mature_cards, started_cards, maturity = _deck_maturity(col)

    # PROFILE-DEPENDENT score basis (the three scores are still never blended —
    # this only changes WHICH memory-domain signals feed the Memory headline):
    #   * strict  — Recall strength = retrievability × deck maturity. Rewards
    #     durable, long-interval memory (needs ≥1 mature card at ≥21d).
    #   * tester  — EARLY RECALL STRENGTH = retrievability only (maturity factor
    #     forced to 1.0). A weekend friend cannot reach a 21-day interval, so we
    #     measure "how well you'd recall right now what you've reviewed", NOT
    #     long-term durability. Explicitly provisional / short-interval.
    mat_factor = maturity if REQUIRE_MEMORY_MATURITY else 1.0

    # Headline + uncertainty band (widens at low card count via SE(R) ∝ 1/√n).
    score = _memory_score(avg_retr, mat_factor)
    score_low, score_high = _memory_band(avg_retr, mat_factor, started_cards)
    mature_pct = round(100 * maturity)

    # Single authoritative status so the badge and headline can NEVER disagree.
    # A real number is only displayable when the review-count gate clears, the
    # profile's maturity/coverage gate clears, AND a retrievability estimate
    # exists (score is not None). When it can't be shown we abstain with the
    # specific blocker, mirroring the headline copy — never "measured" + a "no
    # score" headline at once.
    review_gate_met = total_reviews >= MIN_MEMORY_REVIEWS
    if REQUIRE_MEMORY_MATURITY:
        maturity_gate_met = mature_cards > 0
    else:
        # tester: need a small floor of reviewed cards so the retrievability
        # average is not from just 1-2 cards (keeps the small-n honest).
        maturity_gate_met = started_cards >= MIN_STARTED_CARDS_FOR_MEMORY
    score_displayable = review_gate_met and maturity_gate_met and score is not None

    if not review_gate_met:
        reason: Optional[str] = (
            f"Needs ≥{MIN_MEMORY_REVIEWS} graded reviews "
            f"(have {total_reviews})."
        )
    elif REQUIRE_MEMORY_MATURITY and mature_cards == 0:
        # strict maturity blocker: with no mature cards the maturity factor is 0,
        # so any number would be a meaningless 0 — treat as not-yet-measured.
        need = max(1, started_cards - mature_cards)
        reason = (
            f"Need {need} mature cards "
            f"(interval ≥{MEMORY_MATURITY_INTERVAL_DAYS}d)."
        )
    elif not maturity_gate_met:
        # tester floor blocker: review a few more cards for a stable estimate.
        need = max(1, MIN_STARTED_CARDS_FOR_MEMORY - started_cards)
        reason = (
            f"Review {need} more {'card' if need == 1 else 'cards'} "
            f"(have {started_cards})."
        )
    elif score is None:
        reason = "Need FSRS memory data."
    else:
        reason = None

    return {
        # "measured" ONLY when a real 0-100 score is displayable; else "abstain".
        "status": "measured" if score_displayable else "abstain",
        # True iff the headline shows a real Recall-strength number.
        "score_available": score_displayable,
        # 0-100 Recall strength. strict: retrievability × maturity. tester:
        # retrievability only (EARLY recall strength — see maturity_applied).
        "memory_score": score,
        "score_low": score_low,
        "score_high": score_high,
        # Provisional / basis flags so the renderer can label honestly.
        "provisional": PROVISIONAL_SCORES and score_displayable,
        "profile": SCORE_PROFILE,
        "maturity_applied": REQUIRE_MEMORY_MATURITY,
        # Secondary raw stats (kept honest beneath the headline).
        "total_reviews": total_reviews,
        "topics_studied": topics_studied,
        "topics_unlocked": unlocked,
        "avg_retrievability": avg_retr,
        "maturity": maturity,
        "mature_cards": mature_cards,
        "started_cards": started_cards,
        "mature_pct": mature_pct,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------
def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion.

    ``k`` successes out of ``n`` trials; ``z`` is the standard-normal quantile
    (1.96 -> 95%). Returns ``(low, high)`` as proportions in [0, 1], clamped.

    The interval widens as ``n`` shrinks, which is *why* Performance abstains
    below ``MIN_PERF_ATTEMPTS`` and still carries residual uncertainty above it.
    This is complementary to — and never replaces — the abstain gate.
    """
    if n <= 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    low = max(0.0, center - half)
    high = min(1.0, center + half)
    return (low, high)


def _perf_shrinkage(
    k: int, n: int, a0: float = PERF_PRIOR_A0, b0: float = PERF_PRIOR_B0
) -> Optional[float]:
    """Posterior mean of ``Beta(k + a0, (n-k) + b0)`` on first-attempt
    correctness (``k`` correct of ``n``).

    With the default weak prior ``Beta(2, 2)`` (prior mean 0.5, effective prior
    n = a0 + b0 = 4) this Bayesian shrinkage estimate:
      (a) converges to raw accuracy ``k/n`` as ``n`` grows,
      (b) pulls small ``n`` toward 0.5, and
      (c) never awards ~1.0 for tiny ``n`` (e.g. 1/1 -> 3/5 = 0.60, not 1.0).
    Returns the posterior mean in [0, 1], or ``None`` when ``n <= 0``.
    """
    if n <= 0:
        return None
    return (k + a0) / (n + a0 + b0)


def _perf_band(
    k: int,
    n: int,
    a0: float = PERF_PRIOR_A0,
    b0: float = PERF_PRIOR_B0,
    z: float = PERF_BAND_Z,
) -> tuple[Optional[float], Optional[float]]:
    """Credible band for the shrinkage score: a normal approximation to the
    ``Beta(k + a0, (n-k) + b0)`` posterior credible interval.

    Centered on the *same* posterior the headline uses (so band and score stay
    consistent). With ``α = k + a0``, ``β = (n-k) + b0`` the posterior mean is
    ``α/(α+β)`` and its variance is ``αβ / ((α+β)² (α+β+1))``; the band is
    ``mean ± z·sd``, clamped to [0, 1]. It narrows as ``n`` grows. Returns
    ``(None, None)`` when ``n <= 0``.
    """
    if n <= 0:
        return (None, None)
    a = k + a0
    b = (n - k) + b0
    total = a + b
    mean = a / total
    var = (a * b) / (total * total * (total + 1))
    sd = math.sqrt(max(0.0, var))
    low = max(0.0, mean - z * sd)
    high = min(1.0, mean + z * sd)
    return (low, high)


def performance_summary(col: Collection, store: PerfStore) -> dict[str, Any]:
    overall = store.accuracy()
    attempts = overall["attempts"]
    correct = overall["correct"]
    accuracy = overall["accuracy"]
    unlocked = unlocked_topic_ids(col)

    # Raw accuracy + Wilson interval are honest SECONDARY stats (shown under
    # "Why?"), kept whenever there is any attempt regardless of the gate.
    if accuracy is None:
        accuracy_low: Optional[float] = None
        accuracy_high: Optional[float] = None
    else:
        accuracy_low, accuracy_high = _wilson(correct, attempts)

    abstain_reasons = []
    if attempts < MIN_PERF_ATTEMPTS:
        abstain_reasons.append(
            f"needs ≥{MIN_PERF_ATTEMPTS} attempts (have {attempts})"
        )
    if len(unlocked) < MIN_UNLOCKED_TOPICS:
        abstain_reasons.append(
            f"needs ≥{MIN_UNLOCKED_TOPICS} unlocked topic"
        )
    abstaining = bool(abstain_reasons)

    # HONESTY (mirrors Memory/Readiness): the 0-100 headline number AND its
    # credible band are populated ONLY when the gate clears. Below the gate we
    # abstain cleanly — never a point number AND a "not enough data" badge at
    # once (the self-contradictory "30/100 + not enough data + CI 3–57" bug).
    if abstaining or accuracy is None:
        perf_score: Optional[int] = None
        score_low: Optional[int] = None
        score_high: Optional[int] = None
    else:
        post_mean = _perf_shrinkage(correct, attempts)
        assert post_mean is not None  # attempts > 0 here
        perf_score = _round_half_up(post_mean * 100)
        band_lo, band_hi = _perf_band(correct, attempts)
        score_low = _round_half_up(band_lo * 100)  # type: ignore[arg-type]
        score_high = _round_half_up(band_hi * 100)  # type: ignore[arg-type]

    return {
        "status": "abstain" if abstaining else "ok",
        # True iff the headline shows a real 0-100 number (parity with Memory's
        # score_available). The renderer keys the badge/headline off this so they
        # can NEVER disagree.
        "score_available": not abstaining,
        # Provisional flag (small-n friend-tester profile) so the renderer labels
        # a computed score as rough rather than authoritative.
        "provisional": PROVISIONAL_SCORES and not abstaining,
        "profile": SCORE_PROFILE,
        # 0-100 reliability-adjusted accuracy + credible band (None below gate).
        "perf_score": perf_score,
        "score_low": score_low,
        "score_high": score_high,
        # Secondary raw stats (kept honest beneath the headline).
        "attempts": attempts,
        "correct": correct,
        "accuracy": accuracy,
        "accuracy_low": accuracy_low,
        "accuracy_high": accuracy_high,
        "unlocked_topics": len(unlocked),
        "reason": "; ".join(abstain_reasons) or None,
    }


# ---------------------------------------------------------------------------
# Shipped scope — the coverage denominator and the Readiness per-section attempt
# gates are scoped to what THIS build actually ships (the perf sidecar is the
# shipped source of truth), NOT the full 18-topic outline. A scoped tester build
# (e.g. 3 content topics in CP+BB) must be judged against ITS OWN topics/sections
# — otherwise coverage caps at 3/18 = 17% and the PS/CARS section gates can never
# clear, so Readiness could NEVER compute. A full build that ships every outline
# topic and all four sections reproduces the previous behavior exactly (scope ==
# outline, sections == {CP, BB, PS, CARS}).
# ---------------------------------------------------------------------------
def shipped_scope_topics(store: PerfStore) -> set[str]:
    """Outline topics this build ships questions for.

    Intersected with the outline so an unrecognized id can never inflate the
    coverage denominator. Empty when no question bank is loaded.
    """
    rows = store.conn.execute("SELECT DISTINCT topic_id FROM perf_questions")
    shipped = {r["topic_id"] for r in rows}
    return {t for t in OUTLINE_TOPIC_IDS if t in shipped}


def shipped_scope_sections(store: PerfStore) -> set[str]:
    """Sections this build ships questions for.

    Drives which per-section Readiness attempt gates apply, so a build that
    ships no PS/CARS content is not blocked forever by those sections.
    """
    rows = store.conn.execute("SELECT DISTINCT section FROM perf_questions")
    return {r["section"] for r in rows if r["section"]}


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------
def coverage_summary(col: Collection, store: PerfStore) -> dict[str, Any]:
    """Fraction of the SHIPPED-scope topics with measurement (cards seen or
    scored attempts). The denominator is the topics this build ships questions
    for (see ``shipped_scope_topics``), so a 3-topic tester is measured against
    3 — unlocking 2 of 3 is 66% (≥ the 50% Readiness gate)."""
    scope = shipped_scope_topics(store)
    measured: set[str] = set()
    for m in col.get_topic_mastery():
        if m.cards_seen > 0 and m.topic_id in scope:
            measured.add(m.topic_id)
    # IDK ("Not sure") rows are abstentions, not scored engagement — exclude
    # them so a topic answered only with "Not sure" does NOT count toward
    # Readiness coverage. Mirrors accuracy()'s ``a.idk = 0`` filter.
    for row in store.conn.execute(
        "SELECT DISTINCT q.topic_id FROM perf_attempts a "
        "JOIN perf_questions q ON q.id = a.question_id "
        "WHERE a.idk = 0"
    ):
        if row["topic_id"] in scope:
            measured.add(row["topic_id"])

    total = len(scope)
    pct = round(100 * len(measured) / total) if total else 0
    return {
        "measured": len(measured),
        "total": total,
        "pct": pct,
        "measured_topics": sorted(measured),
    }


# ---------------------------------------------------------------------------
# Readiness (abstains aggressively; honest range when eligible)
# ---------------------------------------------------------------------------
def _readiness_confidence(
    coverage_fraction: float, attempts: int, range_width: float
) -> int:
    """0-100 confidence: how well-supported the readiness range is.

    Monotonic — rises with higher coverage, more attempts (larger sample), and a
    NARROWER range; falls when any of those weaken. A normalized weighted blend
    of three readiness-domain terms only (never the Memory/Performance headline
    scores — the three scores are never blended):

        cov_term   = clamp(coverage_fraction, 0, 1)
        att_term   = n / (n + ATTEMPTS_K)               # reliability from n
        range_term = 1 - min(width, RANGE_MAX) / RANGE_MAX   # narrower -> larger

        confidence = 100 · (0.4·cov_term + 0.4·att_term + 0.2·range_term)

    See the READINESS_CONF_* constants for the exact weights/constants.
    """
    cov_term = min(1.0, max(0.0, coverage_fraction))
    n = max(0, attempts)
    att_term = n / (n + READINESS_CONF_ATTEMPTS_K)
    width = max(0.0, range_width)
    range_term = 1.0 - min(width, READINESS_CONF_RANGE_MAX) / READINESS_CONF_RANGE_MAX
    blend = (
        READINESS_CONF_W_COVERAGE * cov_term
        + READINESS_CONF_W_ATTEMPTS * att_term
        + READINESS_CONF_W_RANGE * range_term
    )
    return _round_half_up(100 * blend)


def _confidence_bucket(score: int) -> str:
    """Qualitative label for a 0-100 confidence (secondary descriptor only)."""
    if score < READINESS_CONF_LOW_MAX:
        return "low"
    if score < READINESS_CONF_HIGH_MIN:
        return "moderate"
    return "high"


def _section_attempt_counts(store: PerfStore) -> dict[str, int]:
    counts: dict[str, int] = {}
    # These per-section counts gate Readiness (MIN_ATTEMPTS_PER_SCIENCE_SECTION /
    # MIN_CARS_ATTEMPTS), so they must reflect *scored* attempts only. IDK
    # ("Not sure") rows are abstentions — exclude them (``a.idk = 0``) so an
    # abstain never helps satisfy a section gate.
    for row in store.conn.execute(
        "SELECT q.section section, COUNT(*) n FROM perf_attempts a "
        "JOIN perf_questions q ON q.id = a.question_id "
        "WHERE a.idk = 0 GROUP BY q.section"
    ):
        counts[row["section"]] = int(row["n"])
    return counts


def readiness_summary(col: Collection, store: PerfStore) -> dict[str, Any]:
    mem = memory_summary(col)
    perf = performance_summary(col, store)
    cov = coverage_summary(col, store)
    section_attempts = _section_attempt_counts(store)
    # Only gate on sections THIS build actually ships (see shipped_scope_sections).
    # A 3-topic tester in CP+BB must not be blocked forever by the absent PS/CARS
    # sections; a full build shipping all four is gated on all four exactly as
    # before.
    shipped_sections = shipped_scope_sections(store)

    reasons: list[str] = []
    if mem["total_reviews"] < MIN_MEMORY_REVIEWS:
        reasons.append(f"reviews < {MIN_MEMORY_REVIEWS}")
    if perf["attempts"] < MIN_PERF_ATTEMPTS:
        reasons.append(f"attempts < {MIN_PERF_ATTEMPTS}")
    if cov["pct"] < MIN_COVERAGE_PCT:
        reasons.append(f"coverage < {MIN_COVERAGE_PCT}%")
    for sec in SCIENCE_SECTIONS:
        if (
            sec in shipped_sections
            and section_attempts.get(sec, 0) < MIN_ATTEMPTS_PER_SCIENCE_SECTION
        ):
            reasons.append(f"{sec} attempts < {MIN_ATTEMPTS_PER_SCIENCE_SECTION}")
    if (
        CARS_SECTION in shipped_sections
        and section_attempts.get(CARS_SECTION, 0) < MIN_CARS_ATTEMPTS
    ):
        reasons.append(f"CARS attempts < {MIN_CARS_ATTEMPTS}")

    if reasons:
        return {
            "status": "abstain",
            "coverage_pct": cov["pct"],
            "reason": "; ".join(reasons),
            "range": None,
        }

    # Eligible — provisional map (coefficients deferred). The range WIDENS when
    # the performance sample is small (honest: fewer attempts -> noisier section
    # accuracy -> a less certain mapped score), so a friend-tester's small-n
    # Readiness visibly reads as rough. Attach a numeric confidence summarizing
    # how well-supported the range is (coverage, attempts, range width) — NOT a
    # blend of the other two scores.
    low, high = _provisional_range(store, perf["attempts"])
    confidence_score = _readiness_confidence(
        cov["pct"] / 100.0, perf["attempts"], high - low
    )
    return {
        "status": "ok",
        "coverage_pct": cov["pct"],
        "reason": None,
        "range": [low, high],
        # numeric 0-100 confidence (headline detail). Bucket label kept as a
        # secondary descriptor for backward-compatible consumers.
        "confidence_score": confidence_score,
        "confidence": _confidence_bucket(confidence_score),
        # Provisional flag + honesty note + single next action (mandatory).
        "provisional": PROVISIONAL_SCORES,
        "note": (
            "Provisional / small-n estimate — wide range. "
            "Not a graded readiness claim."
            if PROVISIONAL_SCORES
            else None
        ),
        "next_action": (
            "Answer more questions across both sections to tighten the range."
        ),
    }


def _readiness_half_width(attempts: int) -> int:
    """Half-width (± points) of the provisional Readiness range.

    Base ±6, widened when the performance sample is small so a low-n estimate is
    visibly rough (no false precision). At/above READINESS_COMFORTABLE_ATTEMPTS
    the widening is zero (the strict full-course behavior, ±6, is preserved);
    below it the deficit grows the half-width up to ``base + extra_max``. Purely
    a function of sample size — never of the point value — so it cannot be tuned
    to flatter a number.
    """
    base = 6
    extra_max = 18
    comfortable = READINESS_COMFORTABLE_ATTEMPTS
    n = max(0, attempts)
    deficit = (max(0, comfortable - n) / comfortable) if comfortable else 0.0
    return base + _round_half_up(extra_max * deficit)


def _provisional_range(store: PerfStore, attempts: int = 0) -> tuple[int, int]:
    total = 0.0
    for sec in ("CP", "CARS", "BB", "PS"):
        # AVG(a.correct) drives the provisional readiness range. IDK rows are
        # logged with correct=0 but are abstentions, so counting them would
        # falsely drag the section accuracy (and thus the range) down — exclude
        # them (``a.idk = 0``) to match accuracy()'s scored-only semantics.
        row = store.conn.execute(
            "SELECT AVG(a.correct) acc FROM perf_attempts a "
            "JOIN perf_questions q ON q.id = a.question_id "
            "WHERE q.section = ? AND a.idk = 0",
            (sec,),
        ).fetchone()
        acc = row["acc"] if row and row["acc"] is not None else 0.0
        total += SECTION_MIN + acc * (SECTION_MAX - SECTION_MIN)
    center = round(total)
    half = _readiness_half_width(attempts)
    return (max(472, center - half), min(528, center + half))


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
    """Every SHIPPED-scope topic joined with the Rust mastery query + performance.

    This is the exact data the unlock gate runs on, exposed for the UI so a
    user can see *why* a topic is (or isn't) unlocked. Scoped to the topics this
    build actually ships (see ``shipped_scope_topics``) so a 3-topic tester shows
    3 rows, not the full 18-topic outline; a full build shows every topic. Topics
    in scope with no cards still appear (all zeros), so coverage is honest.
    """
    own_store = store is None
    if store is None:
        store = PerfStore(col)
    try:
        scope = shipped_scope_topics(store)
        mastery = {m.topic_id: m for m in col.get_topic_mastery()}
        rows: list[dict[str, Any]] = []
        for section, topic_id, name in OUTLINE:
            if topic_id not in scope:
                continue
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
