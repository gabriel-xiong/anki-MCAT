# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Render-layer regression for the ACCURACY (Performance) score-card honesty bug.

Every score card must show a NUMBER *xor* the "not enough data" badge — never
both. The Accuracy card historically showed e.g. "30/100" together WITH a "not
enough data" badge and a huge CI while below the attempt gate, contradicting the
Memory/Readiness cards (which correctly abstained). This drives the real desktop
render (``deckbrowser._mcat_dashboard_html``) through the real scoring layer.
"""

from __future__ import annotations

import os
import tempfile

# The deck-browser module evaluates ``tr.*`` at class-definition time, so the
# translation backend must exist before importing it (mirrors app startup).
import anki.lang

anki.lang.set_lang("en_US")

from anki import mcat_scores  # noqa: E402
from anki.collection import Collection  # noqa: E402
from anki.mcat_perf import PerfStore  # noqa: E402
from aqt.deckbrowser import _mcat_dashboard_html  # noqa: E402


def _tmp_col() -> Collection:
    fd, path = tempfile.mkstemp(suffix=".anki2")
    os.close(fd)
    os.unlink(path)
    return Collection(path)


def _accuracy_card(dashboard_html: str) -> str:
    """The Accuracy card slice (from its label up to the Readiness label). The
    badge/headline follow the label, so this captures both for the assertions."""
    start = dashboard_html.index(">Accuracy</span>")
    end = dashboard_html.index(">Readiness</span>", start)
    return dashboard_html[start:end]


def _cp_questions(n: int, prefix: str) -> list[dict]:
    return [
        {
            "id": f"{prefix}{i}",
            "stem": f"cp_acids_bases q{i}",
            "choices": ["a", "b", "c", "d"],
            "correct": "A",
            "topic_id": "cp_acids_bases",
            "section": "CP",
            "source_name": "OpenStax",
            "split": "dev",
        }
        for i in range(n)
    ]


def _memory_card(dashboard_html: str) -> str:
    """The Memory card slice (from its label up to the Accuracy label)."""
    start = dashboard_html.index(">Memory</span>")
    end = dashboard_html.index(">Accuracy</span>", start)
    return dashboard_html[start:end]


def test_memory_card_subline_shows_review_progress_when_below_gate():
    col = _tmp_col()
    try:
        with PerfStore(col) as store:
            data = mcat_scores.dashboard_data(col, store)
        out = _mcat_dashboard_html(data)
    finally:
        col.close()

    mem = _memory_card(out)
    assert "No score yet" in mem
    assert "not enough data" in mem
    need = mcat_scores.MIN_MEMORY_REVIEWS
    assert f"Need {need} more reviews" in mem
    assert f"have 0/{need}" in mem


def test_memory_card_subline_names_fsrs_when_disabled():
    col = _tmp_col()
    try:
        n = mcat_scores.MIN_STARTED_CARDS_FOR_MEMORY + 2
        for i in range(n):
            note = col.newNote()
            note["Front"] = f"card {i}"
            col.addNote(note)
        for _ in range(n):
            c = col.sched.getCard()
            assert c is not None
            col.sched.answerCard(c, 3)
        with PerfStore(col) as store:
            data = mcat_scores.dashboard_data(col, store)
        out = _mcat_dashboard_html(data)
    finally:
        col.close()

    mem = _memory_card(out)
    assert "No score yet" in mem
    assert "Enable FSRS" in mem
    assert f"0/{n}" in mem


def test_accuracy_card_abstains_with_no_number_under_gate():
    col = _tmp_col()
    try:
        with PerfStore(col) as store:
            store.upsert_questions(_cp_questions(5, "u"))
            # 5 distinct first attempts — well under the 30-attempt gate.
            for i in range(5):
                store.log_attempt(f"u{i}", correct=True)
            data = mcat_scores.dashboard_data(col, store)
        out = _mcat_dashboard_html(data)
    finally:
        col.close()

    acc = _accuracy_card(out)
    assert "No score yet" in acc  # honest abstain headline
    assert "not enough data" in acc  # abstain badge present
    assert "/100" not in acc  # ...and NO contradictory point number


def test_accuracy_card_shows_number_without_badge_over_gate():
    col = _tmp_col()
    try:
        # Unlock cp_acids_bases (>=3 cards seen + >=5 Good/Easy).
        for i in range(3):
            note = col.newNote()
            note["Front"] = f"c{i}"
            note.tags = ["topic:cp_acids_bases"]
            col.addNote(note)
        for _ in range(5):
            c = col.sched.getCard()
            assert c is not None
            col.sched.answerCard(c, 3)
        with PerfStore(col) as store:
            store.upsert_questions(_cp_questions(30, "o"))
            for i in range(30):  # 30 distinct first attempts -> clears the gate
                store.log_attempt(f"o{i}", correct=(i % 2 == 0))
            data = mcat_scores.dashboard_data(col, store)
        out = _mcat_dashboard_html(data)
    finally:
        col.close()

    acc = _accuracy_card(out)
    assert "/100" in acc  # numeric headline shown
    assert "not enough data" not in acc  # ...and no abstain badge
    # Friend-tester profile: a computed score is labeled PROVISIONAL (honest —
    # rough, not authoritative), never the plain "measured" badge.
    assert "provisional" in acc.lower()
    assert ">measured<" not in acc


def test_all_cards_abstain_on_empty_profile():
    """Even at the low friend-tester gates, a 0-data profile shows "No score
    yet" on all three cards and never a point number — the gates never
    fabricate."""
    col = _tmp_col()
    try:
        with PerfStore(col) as store:
            data = mcat_scores.dashboard_data(col, store)
        out = _mcat_dashboard_html(data)
    finally:
        col.close()

    assert out.count("No score yet") >= 3  # Memory + Accuracy + Readiness
    assert "not enough data" in out
    assert "/100" not in out  # no fabricated point number anywhere
