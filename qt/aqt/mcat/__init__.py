# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""MCAT Speedrun — Qt entry points (performance mode).

Performance mode is a SEPARATE session from the reviewer (locked decision:
never flip a flashcard reveal into an instant performance question). Topics are
gated by the Rust mastery query; CARS bypasses the gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from anki import mcat_perf
from anki.mcat_perf import PerfStore, unlocked_topic_ids
from aqt.qt import QAction, qconnect
from aqt.utils import getFile, showInfo, tooltip

if TYPE_CHECKING:
    from aqt.main import AnkiQt


def setup_mcat_menu(mw: AnkiQt) -> None:
    """Append MCAT performance actions to the Tools menu."""
    menu = mw.form.menuTools
    menu.addSeparator()

    act_blocked = QAction("MCAT: Performance session (blocked)", mw)
    qconnect(act_blocked.triggered, lambda: open_performance(mw, interleaved=False))
    menu.addAction(act_blocked)

    act_inter = QAction("MCAT: Performance session (interleaved)", mw)
    qconnect(act_inter.triggered, lambda: open_performance(mw, interleaved=True))
    menu.addAction(act_inter)

    act_mastery = QAction("MCAT: Topic mastery…", mw)
    qconnect(act_mastery.triggered, lambda: open_mastery(mw))
    menu.addAction(act_mastery)

    act_load = QAction("MCAT: Load question bank…", mw)
    qconnect(act_load.triggered, lambda: load_question_bank(mw))
    menu.addAction(act_load)


def open_mastery(mw: AnkiQt) -> None:
    if not mw.col:
        return
    # imported lazily to avoid loading Qt widgets at startup
    from aqt.mcat.mastery_dialog import MasteryDialog

    MasteryDialog(mw).exec()


def load_question_bank(mw: AnkiQt) -> None:
    if not mw.col:
        return

    def on_file(path: str) -> None:
        store = PerfStore(mw.col)
        try:
            n = store.load_questions(path)
        finally:
            store.close()
        tooltip(f"Loaded {n} questions into the performance bank.", parent=mw)

    getFile(mw, "Load MCAT question bank (questions.json)", on_file, filter="*.json")


def open_performance(
    mw: AnkiQt,
    *,
    interleaved: bool,
    topic_id: str | None = None,
    demands: set[str] | None = None,
) -> None:
    if not mw.col:
        return

    store = PerfStore(mw.col)
    if store.question_count() == 0:
        store.close()
        showInfo(
            "No performance questions loaded yet.\n\n"
            "Use Tools → 'MCAT: Load question bank…' and pick your "
            "data/questions.json.",
            parent=mw,
            title="MCAT Performance",
        )
        return

    unlocked = unlocked_topic_ids(mw.col)
    questions = store.eligible_questions(
        unlocked, topic_id=topic_id, demands=demands
    )
    if not questions:
        store.close()
        # A filtered focus session may legitimately have no eligible items yet.
        extra = (
            " for that focus filter"
            if (topic_id or demands)
            else ""
        )
        showInfo(
            f"No eligible questions{extra} yet.\n\n"
            "Unlock a topic in memory mode first "
            f"(≥{mcat_perf.MIN_CARDS_SEEN_FOR_PERFORMANCE} cards seen and "
            f"≥{mcat_perf.MIN_GOOD_OR_EASY_FOR_PERFORMANCE} Good/Easy per topic), "
            "or add CARS questions (no gate).",
            parent=mw,
            title="MCAT Performance",
        )
        return

    # imported lazily to avoid loading Qt widgets at startup
    from aqt.mcat.performance_dialog import PerformanceDialog

    dialog = PerformanceDialog(mw, store, questions, interleaved=interleaved)
    dialog.exec()


def open_focus_area(mw: AnkiQt, spec: str) -> None:
    """Launch the action for the dashboard 'Focus area' tile.

    ``spec`` is ``"<kind>:<topic_id>"`` where kind is one of ``performance``
    (applied practice, filtered to application/synthesis in the topic),
    ``review`` (open the topic's memory cards), or ``pacing`` (careful-reading
    tip). Defensive: unknown/empty specs fall back to the topic-mastery view.
    """
    if not mw.col:
        return
    kind, _, topic_id = spec.partition(":")
    topic_id = topic_id or None

    if kind == "performance":
        # Applied-reasoning focus: only application/synthesis items in the topic.
        open_performance(
            mw,
            interleaved=False,
            topic_id=topic_id,
            demands={"application", "synthesis"},
        )
    elif kind == "review":
        _open_topic_review(mw, topic_id)
    elif kind == "pacing":
        showInfo(
            "Careful-reading / pacing drill\n\n"
            "Your recent misses look like misreads, not knowledge gaps. Next "
            "session: read the full stem (watch for EXCEPT / LEAST / NOT and "
            "units), and pace yourself — don't rush the questions you know.",
            parent=mw,
            title="MCAT Focus area",
        )
    else:
        open_mastery(mw)


def _open_topic_review(mw: AnkiQt, topic_id: str | None) -> None:
    """Open the topic's backing memory cards for review.

    Minimal, non-destructive: navigate to the existing Browse entry point
    scoped to the topic tag so the user can study those cards. (A dedicated
    filtered-deck launcher is a later enhancement.)
    """
    if not topic_id:
        open_mastery(mw)
        return
    try:
        from aqt import dialogs

        browser = dialogs.open("Browser", mw)
        browser.search_for(f"tag:topic:{topic_id}")
        browser.activateWindow()
    except Exception:
        tooltip(
            f"Review your '{topic_id}' cards in memory mode.", parent=mw
        )
