# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""MCAT Speedrun — Qt entry points (performance mode).

Performance mode is a SEPARATE session from the reviewer (locked decision:
never flip a flashcard reveal into an instant performance question). Topics are
gated by the Rust mastery query; CARS bypasses the gate.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from anki import mcat_perf
from anki.mcat_perf import PerfStore, unlocked_topic_ids
from aqt.qt import QAction, QMenu, QWidget, qconnect
from aqt.utils import (
    askUser,
    getFile,
    getOnlyText,
    getSaveFile,
    showInfo,
    show_in_folder,
    tooltip,
)

if TYPE_CHECKING:
    from anki.decks import DeckId
    from aqt.main import AnkiQt


def setup_mcat_menu(mw: AnkiQt) -> None:
    """Append MCAT performance actions to the Tools menu."""
    setup_performance_host(mw)
    menu = mw.form.menuTools
    menu.addSeparator()

    act_blocked = QAction("MCAT: Performance session (blocked)", mw)
    qconnect(act_blocked.triggered, lambda: open_blocked_performance(mw))
    menu.addAction(act_blocked)

    act_inter = QAction("MCAT: Performance session (interleaved)", mw)
    qconnect(act_inter.triggered, lambda: open_performance(mw, interleaved=True))
    menu.addAction(act_inter)

    act_assess = QAction("MCAT: Performance assessment (held_out)", mw)
    qconnect(
        act_assess.triggered,
        lambda: open_performance(
            mw,
            interleaved=True,
            session_mode=mcat_perf.SESSION_MODE_ASSESSMENT,
        ),
    )
    menu.addAction(act_assess)

    act_mastery = QAction("MCAT: Topic mastery…", mw)
    qconnect(act_mastery.triggered, lambda: open_mastery(mw))
    menu.addAction(act_mastery)

    act_load = QAction("MCAT: Load question bank…", mw)
    qconnect(act_load.triggered, lambda: load_question_bank(mw))
    menu.addAction(act_load)

    act_load_pool = QAction("MCAT: Load application-practice pool…", mw)
    qconnect(act_load_pool.triggered, lambda: load_remediation_pool(mw))
    menu.addAction(act_load_pool)

    act_export = QAction("MCAT: Export performance data…", mw)
    qconnect(act_export.triggered, lambda: export_performance_data(mw))
    menu.addAction(act_export)

    act_export_bundle = QAction("MCAT: Export sync bundle…", mw)
    qconnect(act_export_bundle.triggered, lambda: export_performance_bundle(mw))
    menu.addAction(act_export_bundle)

    act_import_bundle = QAction("MCAT: Import sync bundle…", mw)
    qconnect(act_import_bundle.triggered, lambda: import_performance_data(mw))
    menu.addAction(act_import_bundle)

    act_reset = QAction("MCAT: Reset performance data…", mw)
    qconnect(act_reset.triggered, lambda: reset_performance_data(mw))
    menu.addAction(act_reset)

    setup_ai_toggle_action(mw, menu)


def setup_ai_toggle_action(mw: AnkiQt, menu: QMenu) -> None:
    """Add the runtime AI on/off toggle to the Tools menu.

    A checkable action backed by the collection config (see
    ``aqt.mcat.ai_bridge.ai_toggle_enabled``). Turning it off force-disables the
    assistant at runtime — the explainer and follow-ups serve the offline,
    source-grounded fallback immediately, no restart and no ``.env`` change,
    even when a provider + key are configured. The env gate is unchanged; this
    is an additional force-off override on top of it.
    """
    from aqt.mcat.ai_bridge import ai_toggle_enabled, set_ai_toggle_enabled

    menu.addSeparator()
    act_ai = QAction("MCAT: AI assistant enabled", mw)
    act_ai.setCheckable(True)
    act_ai.setChecked(ai_toggle_enabled())

    def on_toggle(checked: bool) -> None:
        set_ai_toggle_enabled(checked)
        # Reflect the change in an OPEN performance panel's header control.
        view = getattr(mw, "mcatPerformanceView", None)
        if view is not None and hasattr(view, "sync_ai_toggle_display"):
            try:
                view.sync_ai_toggle_display()
            except Exception:
                pass
        tooltip(
            "AI assistant enabled."
            if checked
            else "AI assistant off — explanations use the offline, "
            "source-grounded fallback.",
            parent=mw,
        )

    qconnect(act_ai.triggered, on_toggle)
    menu.addAction(act_ai)
    mw._mcatAiToggleAction = act_ai  # type: ignore[attr-defined]

    # The menu is built before a collection is open, so the initial checked
    # state may be the default. Refresh from the persisted config each time the
    # Tools menu is shown, and keep it in sync with the in-panel toggle.
    def refresh_checked() -> None:
        try:
            act_ai.setChecked(ai_toggle_enabled())
        except Exception:
            pass

    qconnect(menu.aboutToShow, refresh_checked)


def setup_performance_host(mw: AnkiQt) -> None:
    """Reserve a main-window slot for embedded performance sessions."""
    if getattr(mw, "mcatPerformanceSlot", None) is not None:
        return
    from aqt import gui_hooks
    from aqt.qt import QVBoxLayout, QWidget

    slot = QWidget(mw)
    slot.setObjectName("mcatPerformanceSlot")
    slot.hide()
    from aqt.qt import QSizePolicy, Qt

    slot.setSizePolicy(
        QSizePolicy.Policy.Expanding,
        QSizePolicy.Policy.Expanding,
    )
    # Opaque host so the deck-browser/dashboard (in mw.web) and the gray
    # main-window canvas can never bleed through/around the performance view.
    # The concrete theme color is applied by the view's apply_theme() at show.
    slot.setAutoFillBackground(True)
    slot.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    slot._layout = QVBoxLayout(slot)  # type: ignore[attr-defined]
    slot._layout.setContentsMargins(0, 0, 0, 0)  # type: ignore[attr-defined]
    slot._layout.setSpacing(0)  # type: ignore[attr-defined]
    mw.mainLayout.insertWidget(1, slot)
    mw.mcatPerformanceSlot = slot
    mw.mcatPerformanceView = None
    gui_hooks.theme_did_change.append(lambda: _refresh_performance_theme(mw))


def _refresh_performance_theme(mw: AnkiQt) -> None:
    view = getattr(mw, "mcatPerformanceView", None)
    if view is not None and view.isVisible():
        view.apply_theme()


def _exit_performance(mw: AnkiQt) -> None:
    """Tear down the embedded view and restore the normal Anki shell."""
    slot = getattr(mw, "mcatPerformanceSlot", None)
    view = getattr(mw, "mcatPerformanceView", None)
    if slot is not None:
        layout = slot._layout  # type: ignore[attr-defined]
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        slot.hide()
    mw.mcatPerformanceView = None
    mw.web.show()
    mw.bottomWeb.show()
    mw.bottomWeb.adjustHeightToFit()
    if mw.state == "deckBrowser":
        mw.deckBrowser.refresh()
    elif mw.state == "overview":
        mw.overview.refresh()


def _show_performance_view(mw: AnkiQt, view: QWidget) -> None:
    """Swap the main web area for the performance session view."""
    from aqt.sound import av_player

    slot = mw.mcatPerformanceSlot
    layout = slot._layout  # type: ignore[attr-defined]
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
    layout.addWidget(view)
    mw.mcatPerformanceView = view
    # Now that the view is parented into the slot, theme both (this paints the
    # slot's opaque background too) BEFORE showing, so nothing flashes through.
    view.apply_theme()
    av_player.stop_and_clear_queue()
    mw.web.hide()
    mw.bottomWeb.hide()
    deck_browser = getattr(mw, "deckBrowser", None)
    if deck_browser is not None:
        deck_browser._refresh_needed = True
    slot.show()
    view.setFocus()


def open_mastery(mw: AnkiQt) -> None:
    if not mw.col:
        return
    # imported lazily to avoid loading Qt widgets at startup
    from aqt.mcat.mastery_dialog import MasteryDialog

    MasteryDialog(mw).exec()


def open_error_report(mw: AnkiQt) -> None:
    """Open the detailed error-diagnosis report (relocated off the dashboard)."""
    if not mw.col:
        return
    # imported lazily to avoid loading Qt widgets at startup
    from aqt.mcat.error_report_dialog import ErrorReportDialog

    ErrorReportDialog(mw).exec()


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


def load_remediation_pool(mw: AnkiQt) -> None:
    """Load the application-practice remediation pool into its ISOLATED store.

    Parallel to ``load_question_bank`` but targets ``remediation_items`` (never
    ``perf_questions``): these items back the ``application`` next-action's short
    practice set and must NEVER enter the Performance/Readiness scores. Pick
    ``data/application-practice.json``. See MCAT/docs/APPLICATION-PRACTICE-POOL.md.
    """
    if not mw.col:
        return

    def on_file(path: str) -> None:
        store = PerfStore(mw.col)
        try:
            n = store.load_remediation(path)
        except Exception as exc:
            showInfo(
                f"Could not load application-practice pool: {exc}",
                parent=mw,
                title="MCAT",
            )
            return
        finally:
            store.close()
        tooltip(
            f"Loaded {n} application-practice items (isolated, not scored).",
            parent=mw,
        )

    getFile(
        mw,
        "Load MCAT application-practice pool (application-practice.json)",
        on_file,
        filter="*.json",
    )


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


def export_performance_bundle(mw: AnkiQt) -> None:
    """Write a portable, versioned sync bundle of this collection's perf data.

    Two-way sync (Friday): the bundle carries the append-only ``perf_attempts``
    (each with a stable ``uuid``) plus the question bank, so another device can
    UNION-MERGE it via 'MCAT: Import sync bundle…'. This is separate from the
    eval CSV/JSON export above — a bundle is the importable interchange format.
    No AI, no network.
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
            title="MCAT Sync",
        )
        return

    default = os.path.splitext(db)[0] + ".perf_bundle.json"
    path = getSaveFile(
        mw,
        "Export MCAT sync bundle",
        "mcat_perf_bundle",
        "MCAT sync bundle",
        ".json",
        os.path.basename(default),
    )
    if not path:
        return
    if not path.lower().endswith(".json"):
        path += ".json"

    try:
        res = mcat_perf.export_bundle(db, path)
    except Exception as exc:
        showInfo(f"Export failed: {exc}", parent=mw, title="MCAT Sync")
        return
    tooltip(
        f"Exported sync bundle ({res['attempts']} attempts, "
        f"{res['questions']} questions) → {path}",
        parent=mw,
    )


# One-click, tester-friendly export ("Export my data") ----------------------
#
# Dashboard entry point for the builder's 6-7 non-technical testers. It reuses
# the SAME sync bundle as ``export_performance_bundle`` (the mergeable format
# the builder re-imports via 'MCAT: Import sync bundle…'), but removes every
# technical decision: defaults the save location to the Desktop, pre-fills a
# clear filename, refuses to write an empty file, and confirms with the exact
# path plus a reveal-in-explorer button. The Tools-menu actions above stay as
# the unchanged power-user path.

_GENERIC_PROFILE_RE = re.compile(r"^\s*(?:user|profile|anki)\s*\d*\s*$", re.IGNORECASE)


def _mcat_slug(text: str) -> str:
    """Filename-safe short label. Collapses runs of unsafe chars to '-'."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-. ")
    return slug or "tester"


def _mcat_tester_label(mw: AnkiQt) -> str:
    """A short label for the export filename.

    Uses the Anki profile name when it is specific. Only when the name is
    generic (e.g. the default 'User 1') do we prompt once for optional
    initials — and it stays fully skippable.
    """
    name = (getattr(mw.pm, "name", None) or "").strip()
    if name and not _GENERIC_PROFILE_RE.match(name):
        return _mcat_slug(name)
    entered = getOnlyText(
        "Optional: type your name or initials so the builder can tell whose "
        "file this is.\n\nYou can leave this blank and just click OK.",
        parent=mw,
        title="MCAT — Export my data",
    ).strip()
    return _mcat_slug(entered or name or "tester")


def _mcat_export_filename(tester: str) -> str:
    from datetime import date

    return f"MCAT-data_{tester}_{date.today().isoformat()}.perf_bundle.json"


def _mcat_default_export_dir() -> str:
    """Best save spot for a non-technical user: Desktop, else Downloads, else home."""
    from aqt.qt import QStandardPaths

    for loc in (
        QStandardPaths.StandardLocation.DesktopLocation,
        QStandardPaths.StandardLocation.DownloadLocation,
        QStandardPaths.StandardLocation.HomeLocation,
    ):
        try:
            d = QStandardPaths.writableLocation(loc)
        except Exception:
            d = ""
        if d and os.path.isdir(d):
            return d
    return os.path.expanduser("~")


def _mcat_confirm_export_saved(
    mw: AnkiQt, path: str, res: dict[str, int], participant: str | None = None
) -> None:
    """Friendly 'saved!' dialog with the exact path + a reveal-in-explorer button."""
    import html as _html

    from aqt.qt import QMessageBox, Qt

    who = (
        f"labelled “{_html.escape(participant)}” · " if participant else ""
    )
    # Surface any AI flags carried in the bundle (additive observability).
    flags = res.get("flags", 0)
    flags_note = f" · {flags} AI flags" if flags else ""
    mb = QMessageBox(mw)
    mb.setIcon(QMessageBox.Icon.Information)
    mb.setWindowTitle("MCAT — Data exported")
    mb.setTextFormat(Qt.TextFormat.RichText)
    mb.setText(
        "Your data file is saved. Send it to your study group "
        "(attach it to an email or upload it).<br><br>"
        f"<b>{_html.escape(os.path.basename(path))}</b><br>"
        f"<span style='color:gray'>{_html.escape(path)}</span><br><br>"
        f"<span style='color:gray'>{who}{res['attempts']} attempts · "
        f"{res['questions']} questions · "
        f"{res.get('revlog', 0)} card reviews{flags_note}</span>"
    )
    open_btn = mb.addButton("Open folder", QMessageBox.ButtonRole.ActionRole)
    ok_btn = mb.addButton(QMessageBox.StandardButton.Ok)
    mb.setDefaultButton(ok_btn)
    mb.exec()
    if mb.clickedButton() is open_btn:
        show_in_folder(path)


def export_my_data(mw: AnkiQt) -> None:
    """One-click, tester-friendly export of the performance sync bundle.

    Same importable bundle as ``export_performance_bundle`` (the builder merges
    it back in via 'MCAT: Import sync bundle…'), wrapped so a non-technical
    tester can produce one shareable file with a single click: Desktop default,
    pre-filled clear filename, empty-state guard, and a confirmation with the
    exact path and an 'Open folder' reveal button.
    """
    if not mw.col:
        return

    db = mcat_perf.sidecar_path(mw.col)
    # Empty-state guard: never write an empty bundle. Only open the store if the
    # sidecar already exists (opening would otherwise create it).
    attempts = 0
    if os.path.exists(db):
        store = PerfStore(mw.col)
        try:
            attempts = store.attempt_count()
        finally:
            store.close()
    # Memory-calibration data lives in the collection's revlog (FSRS history),
    # NOT the perf sidecar, so a tester who only did card reviews still has
    # exportable data. Read it read-only from the open collection.
    revlog = mcat_perf.read_revlog_from_col(mw.col)
    if attempts == 0 and not revlog:
        showInfo(
            "No study data yet — review some cards or do a practice session "
            "first.\n\n"
            "Do some flashcard reviews (Study Flashcards) and/or open "
            "“Practice Questions”, then come back and export.",
            parent=mw,
            title="MCAT — Export my data",
        )
        return

    tester = _mcat_tester_label(mw)
    default_path = os.path.join(
        _mcat_default_export_dir(), _mcat_export_filename(tester)
    )

    from aqt.qt import QFileDialog

    path, _selected = QFileDialog.getSaveFileName(
        mw,
        "Save my MCAT data file",
        default_path,
        "MCAT data file (*.perf_bundle.json)",
    )
    if not path:
        return
    if not path.lower().endswith(".json"):
        path += ".perf_bundle.json"

    try:
        # Embed the tester label INSIDE the payload (participant) so attribution
        # survives renames/merges, and carry the revlog for memory calibration.
        res = mcat_perf.export_bundle(
            db, path, revlog=revlog, participant=tester
        )
    except Exception as exc:
        showInfo(
            f"Export failed: {exc}", parent=mw, title="MCAT — Export my data"
        )
        return
    _mcat_confirm_export_saved(mw, path, res, participant=tester)


def import_performance_data(mw: AnkiQt) -> None:
    """Merge a sync bundle from another device into this collection's perf data.

    Two-way sync (Friday): append-only UNION merge — attempts are deduped by
    their stable ``uuid`` so re-importing the same bundle is a no-op, and the
    merge is order-independent. After each device imports the other's bundle,
    both converge to the union of all attempts. No AI, no network.
    """
    if not mw.col:
        return

    def on_file(path: str) -> None:
        store = PerfStore(mw.col)
        try:
            res = store.import_bundle(path)
        except Exception as exc:
            showInfo(f"Import failed: {exc}", parent=mw, title="MCAT Sync")
            return
        finally:
            store.close()
        flags_added = res.get("flags_added", 0)
        flags_note = f"; {flags_added} AI flags" if flags_added else ""
        tooltip(
            f"Imported {res['attempts_added']} new attempts "
            f"({res['attempts_skipped']} already present; "
            f"{res['questions_added']} questions added{flags_note}).",
            parent=mw,
        )

    getFile(
        mw,
        "Import MCAT sync bundle (JSON)",
        on_file,
        filter="*.json",
    )


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


def open_blocked_performance(mw: AnkiQt) -> None:
    """Blocked session with a user-CHOSEN topic (or CARS), not a random one.

    Presents a compact dropdown of the unlocked science topics (outline display
    names) plus a single CARS entry, then launches a blocked session filtered to
    that choice. CARS is always offered because it bypasses the memory gate; if
    no science topics are unlocked yet, CARS is still selectable. Interleaved and
    assessment sessions are unaffected — they keep launching directly.
    """
    if not mw.col:
        return
    from anki.mcat_scores import (
        SECTION_OF_TOPIC,
        section_display_name,
        topic_display_name,
    )

    entries: list[tuple[str, dict[str, str]]] = []
    for tid in unlocked_topic_ids(mw.col):
        # CARS is offered once as a combined entry (it can span several cars_*
        # topics and bypasses the gate), so skip any CARS topic here.
        if SECTION_OF_TOPIC.get(tid) == mcat_perf.CARS_SECTION:
            continue
        section = SECTION_OF_TOPIC.get(tid, "")
        name = topic_display_name(tid)
        section_name = section_display_name(section) if section else ""
        label = f"{section_name} · {name}" if section_name else name
        entries.append((label, {"topic_id": tid}))
    entries.sort(key=lambda e: e[0])
    # CARS always available (no memory gate) — a single combined entry.
    entries.append(
        (
            f"{section_display_name(mcat_perf.CARS_SECTION)} "
            "— reading (always available)",
            {"section": mcat_perf.CARS_SECTION},
        )
    )

    # imported lazily to avoid loading Qt widgets at startup
    from aqt.mcat.performance_dialog import BlockedTopicDialog

    dlg = BlockedTopicDialog(mw, entries)
    if not dlg.exec() or not dlg.selected:
        return
    sel = dlg.selected
    open_performance(
        mw,
        interleaved=False,
        topic_id=sel.get("topic_id"),
        section=sel.get("section"),
    )


def open_performance(
    mw: AnkiQt,
    *,
    interleaved: bool,
    topic_id: str | None = None,
    demands: set[str] | None = None,
    section: str | None = None,
    session_mode: str = mcat_perf.SESSION_MODE_PRACTICE,
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
    split = (
        mcat_perf.HELD_OUT_SPLIT
        if session_mode == mcat_perf.SESSION_MODE_ASSESSMENT
        else mcat_perf.DEV_SPLIT
    )
    eligible = store.eligible_questions(
        unlocked, split=split, topic_id=topic_id, demands=demands
    )
    # CARS spans several cars_* topics, so a "CARS" blocked session is selected
    # by SECTION rather than a single topic_id. Backend eligibility already lets
    # CARS bypass the gate; we just narrow the already-eligible set here (no
    # mcat_perf changes). topic_id and section are mutually exclusive in the UI.
    if section:
        eligible = [q for q in eligible if (q.get("section") or "") == section]
    filter_key = mcat_perf.session_filter_key(
        session_mode=session_mode,
        interleaved=interleaved,
        topic_id=topic_id,
        demands=demands,
    )
    if section:
        # Keep resume/session state distinct from an all-topics session.
        filter_key = f"{filter_key}|section={section}"
    saved = store.load_session_state(filter_key)
    resume: dict | None = None
    questions: list[dict] = []
    if saved:
        id_to_q = {q["id"]: q for q in eligible}
        restored = [
            id_to_q[qid] for qid in saved["question_ids"] if qid in id_to_q
        ]
        if (
            len(restored) == len(saved["question_ids"])
            and saved["index"] < len(restored)
        ):
            questions = restored
            resume = saved
        else:
            store.clear_session_state(filter_key)
    if not questions:
        questions = mcat_perf.prepare_session_questions(
            eligible,
            session_mode=session_mode,
            interleaved=interleaved,
        )
    if not questions:
        store.close()
        # A filtered focus session may legitimately have no eligible items yet.
        extra = (
            " for that focus filter"
            if (topic_id or demands or section)
            else ""
        )
        mode_note = (
            " (assessment uses held_out split only)"
            if session_mode == mcat_perf.SESSION_MODE_ASSESSMENT
            else ""
        )
        showInfo(
            f"No eligible questions{extra}{mode_note} yet.\n\n"
            "Unlock a topic in memory mode first "
            f"(≥{mcat_perf.MIN_CARDS_SEEN_FOR_PERFORMANCE} cards seen and "
            f"≥{mcat_perf.MIN_GOOD_OR_EASY_FOR_PERFORMANCE} Good/Easy per topic), "
            "or add CARS questions (no gate).",
            parent=mw,
            title="MCAT Performance",
        )
        return

    # imported lazily to avoid loading Qt widgets at startup
    from aqt.mcat.performance_dialog import PerformanceView

    view = PerformanceView(
        mw,
        store,
        questions,
        interleaved=interleaved,
        session_mode=session_mode,
        filter_key=filter_key,
        resume=resume,
        on_close=lambda: _exit_performance(mw),
    )
    _show_performance_view(mw, view)


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
    """Start a real memory-mode REVIEW session for the topic's cards.

    Builds (or rebuilds an existing) dedicated filtered/dynamic deck scoped to
    the topic's tag, then enters the reviewer on it so the user immediately
    starts reviewing — this is normal Anki spaced repetition (memory mode), NOT
    performance mode. ``reschedule=True`` keeps it a true FSRS review (answers
    update scheduling), not a cram/preview.

    Reuse: the deck is keyed by name (``MCAT Focus: <topic>``) and rebuilt in
    place rather than duplicated. Empty case: if nothing is eligible we remove
    the just-built empty deck and show a tooltip instead of dropping the user
    into an empty reviewer. Runs mutations through Anki's CollectionOp path so
    undo/refresh work and the GUI thread is never blocked.
    """
    if not topic_id:
        # No specific topic → keep the surrounding convention (mastery view).
        open_mastery(mw)
        return

    from anki.decks import DeckId, FilteredDeckConfig
    from anki.mcat_scores import topic_display_name

    display = topic_display_name(topic_id)
    deck_name = f"MCAT Focus: {display}"
    # Normal-review semantics: due (review + learning) plus new cards for the
    # topic. If none match, the built deck is empty and we surface a tooltip.
    search = f"tag:topic:{topic_id} (is:due or is:new)"

    try:
        from aqt.operations import CollectionOp
        from aqt.operations.scheduling import add_or_update_filtered_deck

        existing = mw.col.decks.id_for_name(deck_name) or 0
        deck = mw.col.sched.get_or_create_filtered_deck(deck_id=DeckId(existing))
    except Exception:
        tooltip(
            f"Review your '{display}' cards in memory mode.",
            parent=mw,
        )
        return

    deck.name = deck_name
    # Build even when empty so we can detect it explicitly and clean up, rather
    # than have the backend raise on an empty search.
    deck.allow_empty = True
    config = deck.config
    config.reschedule = True
    del config.search_terms[:]
    config.search_terms.append(
        FilteredDeckConfig.SearchTerm(
            search=search,
            limit=200,
            order=FilteredDeckConfig.SearchTerm.Order.DUE,
        )
    )

    def enter_review() -> None:
        mw.col.startTimebox()
        mw.moveToState("review")

    def discard_empty(did: DeckId) -> None:
        # Never leave a broken empty focus deck behind; remove it quietly and
        # tell the user there is nothing to review right now.
        CollectionOp(mw, lambda col: col.decks.remove([did])).success(
            lambda _: tooltip(f"No {display} cards due right now.", parent=mw)
        ).run_in_background()

    def on_built(out: object) -> None:
        did = DeckId(out.id)  # type: ignore[attr-defined]
        try:
            has_cards = bool(mw.col.decks.cids(did))
        except Exception:
            has_cards = False
        if not has_cards:
            discard_empty(did)
            return
        # The build op has ALREADY made this filtered deck the current deck
        # (rslib add_or_update_filtered_deck_inner sets CurrentDeckId) and
        # invalidated the study queues (it moved cards, so the op requires a
        # queue rebuild). So we can enter the reviewer right here, in the
        # build's own success callback: moveToState("review") rebuilds the
        # queue for the now-current deck and starts the session in a SINGLE
        # click. Doing it here — rather than after a second set_current_deck
        # round-trip — avoids the intermediate deck-browser re-render that
        # previously left the user parked on the dashboard with the deck merely
        # selected (hence the old "click twice" behaviour).
        enter_review()
        # Safety net for the "cards gathered but nothing actually due right
        # now" edge: the reviewer bounces straight back out of the "review"
        # state (to overview) when it finds no studyable card. Don't strand the
        # user on an empty/overview screen — bin the focus deck and return to
        # the dashboard with an honest note, exactly like the no-cards case.
        if mw.state != "review":
            discard_empty(did)

    add_or_update_filtered_deck(parent=mw, deck=deck).success(
        on_built
    ).run_in_background()
