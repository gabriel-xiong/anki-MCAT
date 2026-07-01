# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""MCAT Speedrun — Qt entry points (performance mode).

Performance mode is a SEPARATE session from the reviewer (locked decision:
never flip a flashcard reveal into an instant performance question). Topics are
gated by the Rust mastery query; CARS bypasses the gate.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from anki import mcat_perf
from anki.mcat_perf import PerfStore, unlocked_topic_ids
from aqt.qt import QAction, qconnect
from aqt.utils import askUser, getFile, getSaveFile, showInfo, tooltip

if TYPE_CHECKING:
    from anki.decks import DeckId
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

    act_export = QAction("MCAT: Export performance data…", mw)
    qconnect(act_export.triggered, lambda: export_performance_data(mw))
    menu.addAction(act_export)

    act_reset = QAction("MCAT: Reset performance data…", mw)
    qconnect(act_reset.triggered, lambda: reset_performance_data(mw))
    menu.addAction(act_reset)


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


def export_performance_data(mw: AnkiQt) -> None:
    """Export the current collection's performance attempts for offline eval.

    Resolves the sidecar DB path next to the collection, asks where to save, and
    writes an export (format inferred from the chosen extension; anything other
    than .csv/.json writes both). Read-only on the sidecar DB.
    """
    if not mw.col:
        return
    db = mcat_perf.sidecar_path(mw.col)
    if not os.path.exists(db):
        showInfo(
            "No performance data yet.\n\n"
            "Run a performance session first "
            "(Tools → 'MCAT: Performance session').",
            parent=mw,
            title="MCAT Export",
        )
        return

    default = os.path.splitext(db)[0] + ".perf_export.csv"
    path = getSaveFile(
        mw,
        "Export MCAT performance data",
        "mcat_perf_export",
        "Performance export",
        ".csv",
        os.path.basename(default),
    )
    if not path:
        return

    ext = os.path.splitext(path)[1].lower()
    fmt = "csv" if ext == ".csv" else "json" if ext == ".json" else "both"
    try:
        n = mcat_perf.export_attempts(db, path, fmt=fmt)
    except Exception as exc:
        showInfo(f"Export failed: {exc}", parent=mw, title="MCAT Export")
        return
    tooltip(f"Exported {n} performance attempts → {path}", parent=mw)


def reset_performance_data(mw: AnkiQt) -> None:
    """Delete all recorded performance attempts for the current collection.

    Testing / fresh-start helper. Clears ``perf_attempts`` (which resets the
    performance & readiness scores) but leaves the loaded question bank
    (``perf_questions``) intact so it does not need reloading. Confirmation
    defaults to No.
    """
    if not mw.col:
        return
    db = mcat_perf.sidecar_path(mw.col)
    if not os.path.exists(db):
        showInfo(
            "No performance data yet — nothing to reset.",
            parent=mw,
            title="MCAT Reset",
        )
        return

    if not askUser(
        "Reset all recorded performance attempts?\n\n"
        "This permanently clears every logged attempt and resets your "
        "performance and readiness scores. The loaded question bank is kept.\n\n"
        "This cannot be undone.",
        parent=mw,
        defaultno=True,
        title="MCAT Reset performance data",
    ):
        return

    store = PerfStore(mw.col)
    try:
        n = store.reset_attempts()
    finally:
        store.close()
    tooltip(f"Cleared {n} performance attempts.", parent=mw)


def deck_holds_mcat_cards(mw: AnkiQt, did: DeckId) -> bool:
    """True if a deck (including its subdecks) contains MCAT cards.

    MCAT content is identified by ``topic:<id>`` note tags — mastery and
    performance eligibility are tag-based, not deck-based, so there is no fixed
    "MCAT deck". We detect membership by intersecting the deck's card ids with
    the topic-tagged card ids. Best-effort: any backend hiccup returns ``False``
    so ordinary, non-MCAT deck deletions are never affected. Must be called
    BEFORE the deck is removed, while its cards still exist.
    """
    col = mw.col
    if not col:
        return False
    try:
        deck_cids = set(col.decks.cids(did, children=True))
        if not deck_cids:
            return False
        mcat_cids = set(col.find_cards("tag:topic:*"))
        return not deck_cids.isdisjoint(mcat_cids)
    except Exception:
        return False


def reset_performance_on_deck_delete(mw: AnkiQt, did: DeckId) -> None:
    """Clear MCAT performance attempts when an MCAT deck is deleted.

    Deleting a deck resets the memory/mastery scores automatically (they derive
    from the now-deleted cards), but Performance and Readiness live in the
    independent sidecar DB (``mcat_perf.db``) and would otherwise keep showing
    stale numbers (e.g. "8/29 correct · 28%"). This clears the recorded attempts
    — NOT the loaded question bank — which is exactly what drives the dashboard
    counts, so it returns to its honest "not enough data" state once the
    deck-browser re-renders.

    Scope decision (MCAT-deck-only): performance questions are keyed by topic in
    the sidecar, not tied to a specific Anki deck, so there is no clean
    deck→question mapping. We therefore only fire when the deleted deck actually
    held MCAT-tagged cards — deleting an unrelated deck never wipes performance
    data. Safe no-op when there is no sidecar DB yet or nothing to reset.

    Call this BEFORE scheduling the (async) removal so the post-delete re-render
    reflects the already-cleared sidecar. Deck deletion is undoable; the perf
    reset is not, so an undo restores the cards but leaves attempts cleared —
    an acceptable trade-off for the "clean slate" intent.
    """
    if not mw.col:
        return
    if not deck_holds_mcat_cards(mw, did):
        return
    db = mcat_perf.sidecar_path(mw.col)
    if not os.path.exists(db):
        return
    try:
        store = PerfStore(mw.col)
        try:
            store.reset_attempts()
        finally:
            store.close()
    except Exception:
        # A sidecar hiccup must never break deck deletion.
        pass


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
