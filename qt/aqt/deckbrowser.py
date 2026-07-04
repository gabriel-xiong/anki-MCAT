# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import html
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import aqt
import aqt.operations
from anki.collection import Collection, OpChanges
from anki.decks import DeckCollapseScope, DeckId, DeckTreeNode
from aqt import AnkiQt, gui_hooks
from aqt.deckoptions import display_options_for_deck_id
from aqt.operations import QueryOp
from aqt.operations.deck import (
    add_deck_dialog,
    remove_decks,
    rename_deck,
    reparent_decks,
    set_current_deck,
    set_deck_collapsed,
)
from aqt.qt import *
from aqt.sound import av_player
from aqt.toolbar import BottomBar
from aqt.utils import getOnlyText, openLink, shortcut, showInfo, tr


class DeckBrowserBottomBar:
    def __init__(self, deck_browser: DeckBrowser) -> None:
        self.deck_browser = deck_browser


@dataclass
class RenderData:
    """Data from collection that is required to show the page."""

    tree: DeckTreeNode
    current_deck_id: DeckId
    studied_today: str
    sched_upgrade_required: bool


@dataclass
class DeckBrowserContent:
    """Stores sections of HTML content that the deck browser will be
    populated with.

    Attributes:
        tree {str} -- HTML of the deck tree section
        stats {str} -- HTML of the stats section
        mcat_dashboard {str} -- HTML of the MCAT three-score dashboard
    """

    tree: str
    stats: str
    mcat_dashboard: str = ""


@dataclass
class RenderDeckNodeContext:
    current_deck_id: DeckId


class DeckBrowser:
    _render_data: RenderData

    def __init__(self, mw: AnkiQt) -> None:
        self.mw = mw
        self.web = mw.web
        self.bottom = BottomBar(mw, mw.bottomWeb)
        self.scrollPos = QPoint(0, 0)
        self._refresh_needed = False

    def show(self) -> None:
        av_player.stop_and_clear_queue()
        self.web.set_bridge_command(self._linkHandler, self)
        # redraw top bar for theme change
        self.mw.toolbar.redraw()
        self.refresh()

    def refresh(self) -> None:
        if getattr(self.mw, "mcatPerformanceView", None) is not None:
            self._refresh_needed = True
            return
        self._renderPage()
        self._refresh_needed = False

    def refresh_if_needed(self) -> None:
        if self._refresh_needed:
            self.refresh()

    def op_executed(
        self, changes: OpChanges, handler: object | None, focused: bool
    ) -> bool:
        if changes.study_queues and handler is not self:
            self._refresh_needed = True

        if focused:
            self.refresh_if_needed()

        return self._refresh_needed

    # Event handlers
    ##########################################################################

    def _linkHandler(self, url: str) -> Any:
        if ":" in url:
            (cmd, arg) = url.split(":", 1)
        else:
            cmd = url
            arg = ""
        if cmd == "open":
            self.set_current_deck(DeckId(int(arg)))
        elif cmd == "opts":
            self._showOptions(arg)
        elif cmd == "shared":
            self._onShared()
        elif cmd == "import":
            self.mw.onImport()
        elif cmd == "create":
            self._on_create()
        elif cmd == "drag":
            source, target = arg.split(",")
            self._handle_drag_and_drop(DeckId(int(source)), DeckId(int(target or 0)))
        elif cmd == "collapse":
            self._collapse(DeckId(int(arg)))
        elif cmd == "v2upgrade":
            self._confirm_upgrade()
        elif cmd == "v2upgradeinfo":
            if self.mw.col.sched_ver() == 1:
                openLink("https://faqs.ankiweb.net/the-anki-2.1-scheduler.html")
            else:
                openLink("https://faqs.ankiweb.net/the-2021-scheduler.html")
        elif cmd == "select":
            set_current_deck(
                parent=self.mw, deck_id=DeckId(int(arg))
            ).run_in_background()
        elif cmd == "mcat_study":
            # Big "Study flashcards" CTA: open the current deck's overview,
            # which is the normal Anki memory-review entry point.
            self.mw.onOverview()
        elif cmd == "mcat_perf":
            from aqt.mcat import open_blocked_performance

            open_blocked_performance(self.mw)
        elif cmd == "mcat_perf_i":
            from aqt.mcat import open_performance

            open_performance(self.mw, interleaved=True)
        elif cmd == "mcat_mastery":
            from aqt.mcat import open_mastery

            open_mastery(self.mw)
        elif cmd == "mcat_error_report":
            from aqt.mcat import open_error_report

            open_error_report(self.mw)
        elif cmd == "mcat_focus":
            from aqt.mcat import open_focus_area

            open_focus_area(self.mw, arg)
        elif cmd == "mcat_export_my_data":
            # Tester-friendly one-click export of the perf sync bundle.
            from aqt.mcat import export_my_data

            export_my_data(self.mw)
        return False

    def set_current_deck(self, deck_id: DeckId) -> None:
        set_current_deck(parent=self.mw, deck_id=deck_id).success(
            lambda _: self.mw.onOverview()
        ).run_in_background(initiator=self)

    # HTML generation
    ##########################################################################

    _body = """
%(mcat_dashboard)s
<center>
<table cellspacing=0 cellpadding=3>
%(tree)s
</table>

<br>
%(stats)s
</center>
"""

    def _renderPage(self, reuse: bool = False) -> None:
        if not reuse:

            def get_data(col: Collection) -> RenderData:
                return RenderData(
                    tree=col.sched.deck_due_tree(),
                    current_deck_id=col.decks.get_current_id(),
                    studied_today=col.studied_today(),
                    sched_upgrade_required=not col.v3_scheduler(),
                )

            def success(output: RenderData) -> None:
                self._render_data = output
                self.__renderPage(None)

            QueryOp(
                parent=self.mw,
                op=get_data,
                success=success,
            ).run_in_background()
        else:
            self.web.evalWithCallback("window.pageYOffset", self.__renderPage)

    def __renderPage(self, offset: int | None) -> None:
        if getattr(self.mw, "mcatPerformanceView", None) is not None:
            self._refresh_needed = True
            return
        # Re-assert ownership of the shared main webview's pycmd bridge whenever
        # we actually paint the deck-browser page. `mw.web.onBridgeCmd` is a
        # single mutable handler swapped by each state's `show()`; only `show()`
        # calls `set_bridge_command`, but the dashboard is also (re)painted via
        # `refresh()` / `refresh_if_needed()` (e.g. focus change, op_executed,
        # perf-view teardown) which do NOT. If another state's handler — e.g.
        # the reviewer's, whose `_linkHandler` prints "unrecognized anki link:"
        # for anything it doesn't know — is still installed, the MCAT dashboard
        # buttons (`mcat_focus:review:...`, `mcat_perf`, `mcat_study`, …) would
        # be dispatched to it and fail. Reasserting here makes every dashboard
        # render route its own buttons to DeckBrowser._linkHandler.
        self.web.set_bridge_command(self._linkHandler, self)
        data = self._render_data
        content = DeckBrowserContent(
            tree=self._renderDeckTree(data.tree),
            stats=self._renderStats(),
            mcat_dashboard=self._render_mcat_dashboard(),
        )
        gui_hooks.deck_browser_will_render_content(self, content)
        # When the MCAT dashboard is active, wrap the deck list + session
        # launchers in the styled two-section (Memory / Performance) layout.
        # Otherwise fall back to the stock deck-browser body.
        if content.mcat_dashboard:
            body = content.mcat_dashboard + _mcat_lower_html(content.tree, content.stats)
        else:
            body = self._body % content.__dict__
        self.web.stdHtml(
            self._v1_upgrade_message(data.sched_upgrade_required) + body,
            css=["css/deckbrowser.css"],
            js=[
                "js/vendor/jquery.min.js",
                "js/vendor/jquery-ui.min.js",
                "js/deckbrowser.js",
            ],
            context=self,
        )
        self._drawButtons()
        if offset is not None:
            self._scrollToOffset(offset)
        gui_hooks.deck_browser_did_render(self)

    def _scrollToOffset(self, offset: int) -> None:
        self.web.eval("window.scrollTo(0, %d, 'instant');" % offset)

    def _renderStats(self) -> str:
        return '<div id="studiedToday"><span>{}</span></div>'.format(
            self._render_data.studied_today
        )

    # MCAT Speedrun dashboard
    ##########################################################################

    def _render_mcat_dashboard(self) -> str:
        """Three separate scores (memory, performance, readiness) embedded at
        the top of the home screen. Never blends scores; abstains honestly.

        Freshness contract (BUG 2): the three scores are recomputed from the
        LIVE collection + perf store on every call — there is no memoization of
        the summary data here or in ``dashboard_data``. ``__renderPage`` calls
        this on every render, so any deck-browser re-render reflects the current
        DB state (new reviews change ``revlog``/card intervals -> memory; new
        perf attempts land in the perf store -> accuracy/readiness). A fresh
        ``PerfStore`` connection is opened per render so it can't hold a stale
        snapshot. Do NOT cache the returned HTML or ``data`` on the instance.
        """
        try:
            from anki.mcat_perf import PerfStore
            from anki.mcat_scores import dashboard_data

            with PerfStore(self.mw.col) as store:
                data = dashboard_data(self.mw.col, store)
                data["error_spread"] = _mcat_error_spread(store)
            return _mcat_dashboard_html(data)
        except Exception as exc:  # pragma: no cover - defensive
            print("mcat dashboard render failed:", exc)
            return ""

    def _renderDeckTree(self, top: DeckTreeNode) -> str:
        buf = """
<tr><th colspan=5 align=start>{}</th>
<th class=count>{}</th>
<th class=count>{}</th>
<th class=count>{}</th>
<th class=optscol></th></tr>""".format(
            tr.decks_deck(),
            tr.actions_new(),
            tr.decks_learn_header(),
            tr.decks_review_header(),
        )
        buf += self._topLevelDragRow()

        ctx = RenderDeckNodeContext(current_deck_id=self._render_data.current_deck_id)

        for child in top.children:
            buf += self._render_deck_node(child, ctx)

        return buf

    def _render_deck_node(self, node: DeckTreeNode, ctx: RenderDeckNodeContext) -> str:
        if node.collapsed:
            prefix = "+"
        else:
            prefix = "−"

        def indent() -> str:
            return "&nbsp;" * 6 * (node.level - 1)

        if node.deck_id == ctx.current_deck_id:
            klass = "deck current"
        else:
            klass = "deck"

        buf = (
            "<tr class='%s' id='%d' onclick='if(event.shiftKey) return pycmd(\"select:%d\")'>"
            % (
                klass,
                node.deck_id,
                node.deck_id,
            )
        )
        # deck link
        if node.children:
            collapse = (
                "<a class=collapse href=# onclick='return pycmd(\"collapse:%d\")'>%s</a>"
                % (node.deck_id, prefix)
            )
        else:
            collapse = "<span class=collapse></span>"
        if node.filtered:
            extraclass = "filtered"
        else:
            extraclass = ""
        buf += """

        <td class=decktd colspan=5>%s%s<a class="deck %s"
        href=# onclick="return pycmd('open:%d')">%s</a></td>""" % (
            indent(),
            collapse,
            extraclass,
            node.deck_id,
            html.escape(node.name),
        )

        # due counts
        def nonzeroColour(cnt: int, klass: str) -> str:
            if not cnt:
                klass = "zero-count"
            return f'<span class="{klass}">{cnt}</span>'

        review = nonzeroColour(node.review_count, "review-count")
        learn = nonzeroColour(node.learn_count, "learn-count")

        buf += ("<td align=end>%s</td>" * 3) % (
            nonzeroColour(node.new_count, "new-count"),
            learn,
            review,
        )
        # options
        buf += (
            "<td align=center class=opts><a onclick='return pycmd(\"opts:%d\");'>"
            "<img src='/_anki/imgs/gears.svg' class=gears></a></td></tr>" % node.deck_id
        )
        # children
        if not node.collapsed:
            for child in node.children:
                buf += self._render_deck_node(child, ctx)
        return buf

    def _topLevelDragRow(self) -> str:
        return "<tr class='top-level-drag-row'><td colspan='6'>&nbsp;</td></tr>"

    # Options
    ##########################################################################

    def _showOptions(self, did: str) -> None:
        m = QMenu(self.mw)
        a = m.addAction(tr.actions_rename())
        assert a is not None
        qconnect(a.triggered, lambda b, did=did: self._rename(DeckId(int(did))))
        a = m.addAction(tr.actions_options())
        assert a is not None
        qconnect(a.triggered, lambda b, did=did: self._options(DeckId(int(did))))
        a = m.addAction(tr.actions_export())
        assert a is not None
        qconnect(a.triggered, lambda b, did=did: self._export(DeckId(int(did))))
        a = m.addAction(tr.actions_delete())
        assert a is not None
        qconnect(a.triggered, lambda b, did=did: self._delete(DeckId(int(did))))
        gui_hooks.deck_browser_will_show_options_menu(m, int(did))
        m.popup(QCursor.pos())

    def _export(self, did: DeckId) -> None:
        self.mw.onExport(did=did)

    def _rename(self, did: DeckId) -> None:
        def prompt(name: str) -> None:
            new_name = getOnlyText(
                tr.decks_new_deck_name(), default=name, title=tr.actions_rename()
            )
            if not new_name or new_name == name:
                return
            else:
                rename_deck(
                    parent=self.mw, deck_id=did, new_name=new_name
                ).run_in_background()

        QueryOp(
            parent=self.mw, op=lambda col: col.decks.name(did), success=prompt
        ).run_in_background()

    def _options(self, did: DeckId) -> None:
        display_options_for_deck_id(did)

    def _collapse(self, did: DeckId) -> None:
        node = self.mw.col.decks.find_deck_in_tree(self._render_data.tree, did)
        if node:
            node.collapsed = not node.collapsed
            set_deck_collapsed(
                parent=self.mw,
                deck_id=did,
                collapsed=node.collapsed,
                scope=DeckCollapseScope.REVIEWER,
            ).run_in_background()
            self._renderPage(reuse=True)

    def _handle_drag_and_drop(self, source: DeckId, target: DeckId) -> None:
        reparent_decks(
            parent=self.mw, deck_ids=[source], new_parent=target
        ).run_in_background()

    def _delete(self, did: DeckId) -> None:
        deck = self.mw.col.decks.find_deck_in_tree(self._render_data.tree, did)
        assert deck is not None
        deck_name = deck.name
        # MCAT Speedrun: if this deck holds MCAT cards, clear the sidecar
        # performance/readiness data now — before the async removal — so the
        # post-delete re-render shows the honest "not enough data" state rather
        # than stale numbers. No-op for ordinary (non-MCAT) deck deletions.
        from aqt.mcat import reset_performance_on_deck_delete

        reset_performance_on_deck_delete(self.mw, did)
        remove_decks(
            parent=self.mw, deck_ids=[did], deck_name=deck_name
        ).run_in_background()

    # Top buttons
    ######################################################################

    drawLinks = [
        ["", "shared", tr.decks_get_shared()],
        ["", "create", tr.decks_create_deck()],
        ["Ctrl+Shift+I", "import", tr.decks_import_file()],
    ]

    def _drawButtons(self) -> None:
        buf = ""
        drawLinks = deepcopy(self.drawLinks)
        for b in drawLinks:
            if b[0]:
                b[0] = tr.actions_shortcut_key(val=shortcut(b[0]))
            buf += """
<button title='%s' onclick='pycmd(\"%s\");'>%s</button>""" % tuple(b)
        # MCAT: the tester-friendly one-click export is a SHARED/global action,
        # so it sits in the bottom nav row beside Get Shared / Create Deck /
        # Import File (styled like them), not as an oversized dashboard banner.
        # Handler (mcat_export_my_data) is dispatched by _linkHandler.
        buf += (
            "\n<button title='Export your study data to share with your study "
            'group\' onclick=\'pycmd("mcat_export_my_data");\'>'
            "Export my data &#10515;</button>"
        )
        self.bottom.draw(
            buf=buf,
            link_handler=self._linkHandler,
            web_context=DeckBrowserBottomBar(self),
        )

    def _onShared(self) -> None:
        openLink(f"{aqt.appShared}decks/")

    def _on_create(self) -> None:
        if op := add_deck_dialog(
            parent=self.mw, default_text=self.mw.col.decks.current()["name"]
        ):
            op.run_in_background()

    ######################################################################

    def _v1_upgrade_message(self, required: bool) -> str:
        if not required:
            return ""

        update_required = tr.scheduling_update_required().replace("V2", "v3")

        return f"""
<center>
<div class=callout>
    <div>
      {update_required}
    </div>
    <div>
      <button onclick='pycmd("v2upgrade")'>
        {tr.scheduling_update_button()}
      </button>
      <button onclick='pycmd("v2upgradeinfo")'>
        {tr.scheduling_update_more_info_button()}
      </button>
    </div>
</div>
</center>
"""

    def _confirm_upgrade(self) -> None:
        if self.mw.col.sched_ver() == 1:
            self.mw.col.mod_schema(check=True)
            self.mw.col.upgrade_to_v2_scheduler()
        self.mw.col.set_v3_scheduler(True)

        showInfo(tr.scheduling_update_done())
        self.refresh()


# MCAT Speedrun dashboard HTML
##############################################################################


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{round(value * 100)}%"


_MCAT_ERROR_BUCKETS = (
    ("content_gap", "Content gap", "#e05252"),
    ("passage_mapping", "Passage mapping", "#f5a623"),
    ("reasoning", "Applied reasoning", "#9b5cf6"),
    ("misread", "Misread / careless", "#4c7cf3"),
)

_MCAT_ERROR_ALIASES = {
    "content_gap": "content_gap",
    "passage_mapping": "passage_mapping",
    "reasoning": "reasoning",
    "application": "reasoning",
    "misread": "misread",
}


def _mcat_error_bucket(error_type: str | None, inferred_type: str | None) -> str | None:
    for raw in (error_type, inferred_type):
        if not raw or raw in ("none", "unresolved"):
            continue
        bucket = _MCAT_ERROR_ALIASES.get(raw)
        if bucket:
            return bucket
    return None


def _mcat_error_spread(store: Any) -> dict[str, Any]:
    counts = {key: 0 for key, _, _ in _MCAT_ERROR_BUCKETS}
    rows = store.conn.execute(
        "SELECT error_type, inferred_error_type FROM perf_attempts "
        "WHERE correct = 0"
    ).fetchall()
    for row in rows:
        bucket = _mcat_error_bucket(row["error_type"], row["inferred_error_type"])
        if bucket:
            counts[bucket] += 1
    total = sum(counts.values())
    return {
        "total": total,
        "items": [
            {
                "key": key,
                "label": label,
                "count": counts[key],
                "pct": round((counts[key] / total) * 100) if total else 0,
                "accent": accent,
            }
            for key, label, accent in _MCAT_ERROR_BUCKETS
        ],
    }


def _mcat_plural(n: int, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or f"{singular}s")


def _mcat_info_popover(measures: str, calc: str, gate: str) -> str:
    """In-webview info reveal for a score card's circled-``i``.

    Rendered as a self-contained CSS hover/focus popover instead of a native
    ``title`` tooltip (which is unreliable / near-invisible under QtWebEngine).
    The trigger is a focusable ``<button>``, so the panel appears on hover AND
    on click/tab focus via ``:hover`` / ``:focus-within`` — no pycmd routing
    and no dependence on an OS tooltip.
    """
    rows = "".join(
        f"<span class='mcat-info-pop-row'><b>{label}</b>{html.escape(text)}</span>"
        for label, text in (("What", measures), ("How", calc), ("When", gate))
        if text
    )
    return (
        "<span class='mcat-info-wrap'>"
        "<button type='button' class='mcat-info' "
        "aria-label='What this measures'>&#9432;</button>"
        f"<span class='mcat-info-pop' role='tooltip'>{rows}</span>"
        "</span>"
    )


def _mcat_card(
    *,
    accent: str,
    label: str,
    headline: str,
    sub: str,
    blurb: str,
    info_measures: str,
    info_calc: str,
    info_gate: str,
    abstaining: bool,
) -> str:
    # Methodology lives behind the circled-info popover (hover/click); the
    # visible surface keeps just a one-line "what it measures" blurb.
    info_html = _mcat_info_popover(info_measures, info_calc, info_gate)
    badge = (
        "<span class='mcat-badge mcat-badge-abstain'>not enough data</span>"
        if abstaining
        else "<span class='mcat-badge'>measured</span>"
    )
    return f"""
<div class="mcat-card" style="--mcat-accent:{accent};">
  <div class="mcat-card-top">
    <span class="mcat-card-label">{html.escape(label)}</span>
    <span class="mcat-card-meta">{info_html}{badge}</span>
  </div>
  <div class="mcat-headline">{html.escape(headline)}</div>
  <div class="mcat-blurb">{html.escape(blurb)}</div>
  <div class="mcat-sub">{html.escape(sub)}</div>
</div>"""


def _mcat_error_summary(spread: dict[str, Any] | None) -> str:
    """One-line summary of diagnosed misses for the secondary-action row.

    The full four-bucket breakdown lives in the ``ErrorReportDialog`` (pycmd
    ``mcat_error_report``); here we return only the short lead-in text, so it
    rides quietly beside the secondary links rather than as its own button row.
    """
    if not spread:
        return ""
    total = int(spread.get("total") or 0)
    items = spread.get("items") or []
    if total:
        top = max(items, key=lambda it: int(it.get("count") or 0), default=None)
        top_label = str(top.get("label") or "") if top else ""
        return (
            f"{total} diagnosed miss{'es' if total != 1 else ''}"
            + (f" · top: {top_label}" if top_label else "")
        )
    return "No diagnosed misses yet"


def _mcat_focus_html(focus: dict[str, Any] | None) -> str:
    """Prominent 'Focus area' card: the top diagnosed weakness + one-click
    action. Same visual weight as the three score cards; theme-aware."""
    if not focus:
        return ""
    label = html.escape(focus.get("action_label") or "")
    if focus.get("status") != "ok":
        reason = html.escape(focus.get("reason") or "")
        return f"""
<div class="mcat-focus mcat-focus-abstain">
  <div class="mcat-focus-body">
    <span class="mcat-focus-tag">Focus area</span>
    <div class="mcat-focus-head">Not enough data yet</div>
    <div class="mcat-focus-sub">{reason}</div>
  </div>
</div>"""

    kind = focus.get("kind") or ""
    etype = html.escape(focus.get("error_type") or "")
    topic = html.escape(focus.get("topic_name") or "")
    launch = html.escape(focus.get("launch") or "")
    verb = {
        "review": "Review flashcards",
        "performance": "Practice applied",
        "pacing": "Start drill",
    }.get(kind, "Start")
    return f"""
<div class="mcat-focus">
  <div class="mcat-focus-body">
    <span class="mcat-focus-tag">Focus area</span>
    <div class="mcat-focus-head">{label}</div>
    <div class="mcat-focus-sub">Top diagnosed weakness: <b>{etype}</b>{(' · ' + topic) if topic else ''}</div>
  </div>
  <button class="mcat-focus-btn" onclick='pycmd("mcat_focus:{launch}")'>{html.escape(verb)} &rarr;</button>
</div>"""


def _mcat_dashboard_html(data: dict[str, Any]) -> str:
    mem = data["memory"]
    perf = data["performance"]
    read = data["readiness"]
    cov = data["coverage"]
    focus_html = _mcat_focus_html(data.get("focus_area"))
    error_summary = _mcat_error_summary(data.get("error_spread"))

    # Memory card: compact visible surface; methodology is tucked behind Why?
    # Badge is driven by the SAME authoritative status the headline reads, so
    # the "measured" badge can never coexist with a "No score yet / Need N
    # mature cards" headline. memory_summary() reports status == "measured" ONLY
    # when a real 0-100 Recall-strength number is displayable (review-count gate
    # met AND >=1 mature card AND a retrievability estimate); otherwise
    # "abstain" with the blocker reason.
    mem_abstaining = mem.get("status") != "measured"
    mem_score = mem.get("memory_score")
    mature_cards = int(mem.get("mature_cards") or 0)
    started_cards = int(mem.get("started_cards") or 0)
    mature_needed = max(0, started_cards - mature_cards)
    if mem_score is None or mem_abstaining:
        mem_headline = "No score yet"
        if mature_needed:
            mem_sub = (
                f"Need {mature_needed} "
                f"{_mcat_plural(mature_needed, 'mature card')}"
            )
        elif int(mem["total_reviews"]) < 200:
            need_reviews = 200 - int(mem["total_reviews"])
            mem_sub = (
                f"Need {need_reviews} "
                f"{_mcat_plural(need_reviews, 'graded review')}"
            )
        else:
            mem_sub = "Need FSRS memory data"
    else:
        mem_headline = f"{mem_score}/100"
        mem_sub = (
            f"{mem['mature_pct']}% mature \u00b7 "
            f"{mem['total_reviews']} reviews"
        )
    mem_card = _mcat_card(
        accent="#4c7cf3",
        label="Memory",
        headline=mem_headline,
        sub=mem_sub,
        blurb="Recall strength from your reviews",
        info_measures="How well you'd recall what you've studied right now.",
        info_calc="FSRS predicted retrievability \u00d7 how many of your "
        "started cards are mature (interval \u226521d).",
        info_gate="Shows a score once you have \u2265200 graded reviews.",
        abstaining=mem_abstaining,
    )

    # Performance card: headline + uncertainty only; raw accuracy is in Why?
    perf_abstaining = perf["status"] == "abstain"
    perf_score = perf.get("perf_score")
    if perf_score is None:
        perf_headline = "No score yet"
        perf_ci = ""
        need_attempts = max(0, 30 - int(perf["attempts"]))
        perf_sub = (
            f"Need {need_attempts} {_mcat_plural(need_attempts, 'attempt')}"
            if need_attempts
            else "Start a performance session"
        )
    else:
        perf_headline = f"{perf_score}/100"
        # Posterior 95% credible interval (consistent with the score); narrows
        # as attempts accumulate. Shown as a subordinate band beneath the score.
        p_lo, p_hi = perf.get("score_low"), perf.get("score_high")
        perf_ci = (
            f"95% confidence interval: {p_lo}\u2013{p_hi}"
            if p_lo is not None and p_hi is not None
            else ""
        )
        perf_sub = perf_ci or f"{perf['attempts']} attempts"
    perf_card = _mcat_card(
        accent="#9b5cf6",
        label="Accuracy",
        headline=perf_headline,
        sub=perf_sub,
        blurb="Exam-question accuracy, reliability-adjusted",
        info_measures="Your accuracy on new exam-style questions in unlocked "
        "topics.",
        info_calc="Adjusted for how many you've answered \u2014 Bayesian "
        "shrinkage toward 50% when the sample is small, so 1/1 isn't 100.",
        info_gate="Measured after \u226530 answered questions. A topic unlocks "
        "at \u22653 cards seen + \u22655 Good/Easy reviews.",
        abstaining=perf_abstaining,
    )

    # Readiness card
    read_abstaining = read["status"] != "ok" or not read["range"]
    cov_pct_r = read["coverage_pct"]
    if not read_abstaining:
        lo, hi = read["range"]
        read_headline = f"{lo}–{hi}"
        # Headline detail is now the NUMERIC 0-100 confidence; the qualitative
        # bucket is kept only as a quiet parenthetical descriptor.
        conf_score = read.get("confidence_score")
        if conf_score is not None:
            bucket = str(read.get("confidence", "")).strip()
            read_sub = (
                f"Confidence: {conf_score}/100 ({bucket})"
                if bucket
                else f"Confidence: {conf_score}/100"
            )
        else:
            confidence = str(read.get("confidence", "low")).capitalize()
            read_sub = f"{confidence} confidence"
    else:
        read_headline = "No score yet"
        # Show coverage honestly: mark it met when \u226550% so it doesn't read
        # like the blocker (the real blockers live in the gate note below).
        blockers = str(read["reason"] or "").split("; ")
        read_sub = (
            "Need 50% coverage"
            if cov_pct_r < 50
            else (blockers[0].replace("<", "under") if blockers else "Need more data")
        )
    read_card = _mcat_card(
        accent="#2bb673",
        label="Readiness",
        headline=read_headline,
        sub=read_sub,
        blurb="Estimated MCAT score range",
        info_measures="Estimated MCAT score range (472\u2013528).",
        info_calc="Never blends Memory or Performance. Confidence (0\u2013100) "
        "summarizes only readiness inputs \u2014 coverage, attempts, and how "
        "narrow the range is.",
        info_gate="Shown only once there's enough data across sections; "
        "abstains (no number) below coverage/attempt thresholds.",
        abstaining=read_abstaining,
    )

    cov_pct = cov["pct"]
    return f"""
<style>
.mcat-dash {{
  max-width: 880px; margin: 8px auto 4px auto; padding: 0 12px;
  text-align: start; font-size: 14px;
}}
.mcat-dash-head {{
  display: flex; align-items: baseline; justify-content: space-between;
  margin-bottom: 7px;
}}
.mcat-dash-title {{ font-size: 15px; font-weight: 700; }}
.mcat-dash-sub {{ color: var(--fg-subtle, #888); font-size: 11px; }}
.mcat-cards {{ display: flex; gap: 12px; flex-wrap: wrap; }}
.mcat-card {{
  flex: 1 1 0; min-width: 200px; border: 1px solid var(--border, #e4e4e7);
  border-top: 3px solid var(--mcat-accent); border-radius: 12px;
  padding: 11px 14px 12px; background: var(--canvas-elevated, #fff);
  box-shadow: 0 1px 3px rgba(15,23,42,0.06), 0 1px 2px rgba(15,23,42,0.04);
}}
.mcat-card-top {{ display: flex; align-items: center; justify-content: space-between;
  gap: 10px; }}
.mcat-card-label {{ font-size: 11px; font-weight: 600; color: var(--fg-subtle, #888);
  text-transform: uppercase; letter-spacing: .04em; }}
.mcat-card-meta {{ display: flex; align-items: center; gap: 7px; }}
.mcat-badge {{ font-size: 9px; padding: 2px 7px; border-radius: 999px;
  background: var(--border, #ececf0); color: var(--fg-subtle, #77777f);
  font-weight: 600; text-transform: uppercase; letter-spacing: .03em; }}
.mcat-badge-abstain {{ background: var(--border, #ececf0);
  color: var(--fg-subtle, #999); }}
.mcat-info-wrap {{ position: relative; display: inline-flex; }}
.mcat-info {{ font: inherit; font-size: 14px; line-height: 1;
  color: var(--fg-subtle, #9a9aa2); background: none; border: none;
  padding: 0; margin: 0; cursor: help; }}
.mcat-info:hover, .mcat-info:focus {{ color: var(--mcat-accent); outline: none; }}
/* Reliable in-webview info reveal: no native OS `title` tooltip. Appears on
   hover of the wrapper and on click/tab focus of the button (:focus-within),
   both pure CSS — QtWebEngine is Chromium so :focus-within is supported. */
.mcat-info-pop {{
  position: absolute; top: calc(100% + 8px); right: 0; z-index: 30;
  width: 240px; padding: 10px 12px; border-radius: 10px; text-align: start;
  background: var(--canvas-elevated, #fff); color: var(--fg, #27272a);
  border: 1px solid var(--border, #e4e4e7);
  box-shadow: 0 6px 20px rgba(15,23,42,0.18);
  font-size: 11.5px; font-weight: 400; line-height: 1.45;
  opacity: 0; visibility: hidden; transform: translateY(-4px);
  transition: opacity .12s ease, transform .12s ease; pointer-events: none;
}}
.mcat-info-wrap:hover .mcat-info-pop,
.mcat-info-wrap:focus-within .mcat-info-pop {{
  opacity: 1; visibility: visible; transform: translateY(0); pointer-events: auto;
}}
.mcat-info-pop-row {{ display: block; }}
.mcat-info-pop-row + .mcat-info-pop-row {{ margin-top: 6px; }}
.mcat-info-pop-row b {{ display: block; font-size: 9.5px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .04em;
  color: var(--fg-subtle, #8a8a92); margin-bottom: 1px; }}
.mcat-headline {{ font-size: 23px; font-weight: 700; line-height: 1.15;
  margin: 6px 0 2px; color: var(--mcat-accent); }}
.mcat-blurb {{ font-size: 11.5px; color: var(--fg, inherit); opacity: .78; }}
.mcat-sub {{ font-size: 11.5px; color: var(--fg-subtle, #888); min-height: 15px;
  margin-top: 2px; }}
.mcat-cover-wrap {{ margin-top: 12px; }}
.mcat-cover-bar {{ height: 16px; border-radius: 999px; overflow: hidden;
  margin-top: 8px; background: var(--border, #e2e2e2);
  box-shadow: inset 0 1px 2px rgba(15,23,42,0.12); }}
.mcat-cover-fill {{ height: 100%; border-radius: 999px; background: #2bb673;
  width: {cov_pct}%; box-shadow: 0 1px 2px rgba(43,182,115,0.35); }}
.mcat-cover-label {{ font-size: 12.5px; font-weight: 600;
  color: var(--fg-subtle, #888); }}
.mcat-focus {{ margin-top: 12px; display: flex; align-items: center;
  justify-content: space-between; gap: 14px; flex-wrap: wrap;
  border: 1px solid var(--border, #e4e4e7); border-left: 4px solid #f5a623;
  border-radius: 12px; padding: 11px 14px;
  background: var(--canvas-elevated, rgba(245,166,35,0.08));
  box-shadow: 0 1px 3px rgba(15,23,42,0.06), 0 1px 2px rgba(15,23,42,0.04); }}
.mcat-focus-abstain {{ border-left-color: var(--fg-subtle, #999);
  background: var(--canvas-elevated, rgba(127,127,127,0.06)); }}
.mcat-focus-tag {{ font-size: 11px; font-weight: 700; color: #f5a623;
  text-transform: uppercase; letter-spacing: .05em; }}
.mcat-focus-abstain .mcat-focus-tag {{ color: var(--fg-subtle, #999); }}
.mcat-focus-head {{ font-size: 17px; font-weight: 700; margin: 3px 0 2px; }}
.mcat-focus-sub {{ font-size: 12px; color: var(--fg-subtle, #888); }}
.mcat-focus-btn {{ padding: 9px 16px; border-radius: 8px; cursor: pointer;
  border: none; background: #f5a623; color: #1a1a1a; font-weight: 700;
  font-size: 13px; white-space: nowrap; }}
.mcat-focus-btn:hover {{ filter: brightness(1.06); }}
/* One tidy secondary-action row: quiet summary on the left, two low-emphasis
   link-buttons on the right — replaces the old stack of full-width buttons. */
.mcat-secondary-row {{ margin-top: 12px; padding-top: 10px;
  border-top: 1px solid var(--border-subtle, #ececf0);
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px 16px; flex-wrap: wrap; }}
.mcat-secondary-sum {{ font-size: 12px; color: var(--fg-subtle, #888); }}
.mcat-secondary-actions {{ display: flex; align-items: center; gap: 8px;
  flex-wrap: wrap; }}
.mcat-linkbtn {{ font: inherit; font-size: 12.5px; font-weight: 600;
  color: var(--mcat-accent, #4c7cf3); background: transparent; border: none;
  padding: 4px 2px; cursor: pointer; white-space: nowrap; }}
.mcat-linkbtn:hover {{ text-decoration: underline; }}
.mcat-linkbtn-sep {{ color: var(--fg-subtle, #bcbcc4); font-size: 12px;
  user-select: none; }}
</style>
<div class="mcat-dash">
  <div class="mcat-dash-head">
    <span class="mcat-dash-title">MCAT Speedrun</span>
    <span class="mcat-dash-sub">three separate scores · never blended</span>
  </div>
  <div class="mcat-cards">
    {mem_card}
    {perf_card}
    {read_card}
  </div>
  {focus_html}
  <div class="mcat-cover-wrap">
    <span class="mcat-cover-label">Coverage: {cov['measured']}/{cov['total']} topics measured ({cov_pct}%)</span>
    <div class="mcat-cover-bar"><div class="mcat-cover-fill"></div></div>
  </div>
  <div class="mcat-secondary-row">
    <span class="mcat-secondary-sum">{html.escape(error_summary)}</span>
    <span class="mcat-secondary-actions">
      <button class="mcat-linkbtn" onclick='pycmd("mcat_mastery")'>View topic mastery &rarr;</button>
      <span class="mcat-linkbtn-sep" aria-hidden="true">·</span>
      <button class="mcat-linkbtn" onclick='pycmd("mcat_error_report")'>View error diagnosis report &rarr;</button>
    </span>
  </div>
</div>"""


# MCAT Speedrun — lower half: Memory vs Performance sections
##############################################################################

_MCAT_LOWER_CSS = """
<style>
/* Memory vs Performance — one shared design system with the score cards
   above: same 14px radius, 1px border, soft elevation shadow, theme-aware
   surface, and per-side accent (blue = Memory, purple = Performance). */
.mcat-lower {
  max-width: 880px; margin: 6px auto 4px auto; padding: 0 12px;
  text-align: start; font-size: 14px; -webkit-font-smoothing: antialiased;
}
.mcat-modes { display: flex; gap: 16px; flex-wrap: wrap; align-items: stretch; }
.mcat-mode {
  flex: 1 1 340px; min-width: 300px; display: flex; flex-direction: column;
  border: 1px solid var(--border, #e4e4e7);
  border-top: 3px solid var(--mcat-mode-accent, #888);
  border-radius: 14px; padding: 18px 20px;
  background: var(--canvas-elevated, #fff);
  box-shadow: 0 1px 3px rgba(15,23,42,0.06), 0 1px 2px rgba(15,23,42,0.04);
}
.mcat-mode-memory { --mcat-mode-accent: #4c7cf3; }
.mcat-mode-perf { --mcat-mode-accent: #9b5cf6; }
.mcat-mode-tag {
  font-size: 11px; font-weight: 700; letter-spacing: .07em;
  text-transform: uppercase; color: var(--mcat-mode-accent);
}
.mcat-mode-title { font-size: 20px; font-weight: 700; margin: 4px 0 4px;
  line-height: 1.2; }
.mcat-mode-desc {
  font-size: 12.5px; color: var(--fg-subtle, #71717a); line-height: 1.5;
  margin-bottom: 16px;
}

/* Deck list (Memory side): restyled to match the cards instead of the raw
   default-Anki gray table. Only the visual layer changes — the row markup,
   ids, links, gear, collapse arrows and drag/drop hooks are untouched, so
   deck navigation and every pycmd handler keep working exactly as before. */
.mcat-deck-wrap {
  flex: 1; margin-bottom: 16px; border: 1px solid var(--border-subtle, #ececf0);
  border-radius: 10px; overflow-x: auto; padding: 6px 8px;
  background: var(--canvas-inset, rgba(76,124,243,0.03));
}
.mcat-deck-table {
  width: 100% !important; padding: 0 !important; margin: 0 !important;
  background: transparent !important; border: none !important;
  box-shadow: none !important; border-collapse: collapse;
}
.mcat-deck-table th {
  color: var(--fg-subtle, #a1a1aa); font-size: 10.5px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .05em;
  border-bottom: 1px solid var(--border-subtle, #ececf0) !important;
  padding: 4px 10px 7px !important;
}
.mcat-deck-table td { font-size: 13px; padding: 5px 10px !important;
  border: none !important; }
.mcat-deck-table a.deck { color: var(--fg, #27272a); font-weight: 500; }
.mcat-deck-table .decktd { min-width: 0; }
.mcat-deck-table .collapse { color: var(--fg-subtle, #a1a1aa); }
.mcat-deck-table .gears { opacity: .5; }
/* Memory-blue row highlight (current + hover) instead of the default gray. */
.mcat-deck-wrap tr.deck.current td,
.mcat-deck-wrap tr.deck:hover:not(.top-level-drag-row) td {
  background: rgba(76,124,243,0.10) !important;
}
.mcat-deck-wrap tr.deck.current td:first-child,
.mcat-deck-wrap tr.deck:hover:not(.top-level-drag-row) td:first-child {
  border-top-left-radius: 8px; border-bottom-left-radius: 8px;
}
.mcat-deck-wrap tr.deck.current td:last-child,
.mcat-deck-wrap tr.deck:hover:not(.top-level-drag-row) td:last-child {
  border-top-right-radius: 8px; border-bottom-right-radius: 8px;
}
.mcat-deck-wrap tr.deck.current a.deck { color: #4c7cf3; font-weight: 700; }
.mcat-deck-wrap tr.deck:hover .gears,
.mcat-deck-wrap tr.deck.current .gears { visibility: visible; }

.mcat-cta-group { display: flex; flex-direction: column; gap: 10px; flex: 1; }
.mcat-cta {
  display: block; width: 100%; border: none; border-radius: 10px;
  cursor: pointer; font: inherit; padding: 15px 18px; color: #fff;
  text-align: center; box-sizing: border-box;
}
.mcat-cta-memory {
  background: #4c7cf3; font-size: 16px; font-weight: 700; margin-top: auto;
}
.mcat-cta-perf { background: #9b5cf6; position: relative; text-align: start; }
.mcat-cta-perf-alt {
  background: transparent; color: var(--fg, inherit);
  border: 1.5px solid #9b5cf6; text-align: start;
}
/* Hover states are pinned explicitly (background + color + border) with
   !important so Anki's global `button:not(.btn,.btn-close):hover` rule
   (ts/lib/sass/base.scss -> button-mixins `background`) can't repaint the
   filled CTAs with its light gradient and leave white text on a white/near-
   white background. Do not rely on `filter` alone — it doesn't set a color or
   background, so the global hover would still win. */
.mcat-cta-memory:hover {
  background: #3f6ee0 !important; color: #fff !important; border: none !important;
}
.mcat-cta-perf:hover {
  background: #8a4bf0 !important; color: #fff !important; border: none !important;
}
.mcat-cta-perf-alt:hover {
  background: rgba(155,92,246,0.12) !important; color: #9b5cf6 !important;
  border: 1.5px solid #9b5cf6 !important;
}
.mcat-cta-perf-alt:hover .mcat-cta-main { color: #9b5cf6 !important; }
.mcat-cta-main { display: block; font-size: 16px; font-weight: 700; }
.mcat-cta-sub { display: block; font-size: 12px; opacity: .88; margin-top: 3px; }
.mcat-cta-perf-alt .mcat-cta-sub { color: var(--fg-subtle, #888); opacity: 1; }
.mcat-cta-badge {
  position: absolute; top: 13px; right: 14px;
  background: rgba(255,255,255,0.22); color: #fff; font-size: 10px;
  font-weight: 700; padding: 3px 9px; border-radius: 999px;
  text-transform: uppercase; letter-spacing: .04em;
}
.mcat-mode-foot { margin-top: 12px; text-align: center; }
.mcat-secondary-link {
  font-size: 12px; font-weight: 600; color: #9b5cf6; cursor: pointer;
}
.mcat-secondary-link:hover { text-decoration: underline; }

.mcat-stats-wrap {
  max-width: 880px; margin: 4px auto 12px auto; padding: 0 12px;
  text-align: center; color: var(--fg-subtle, #888); font-size: 12px;
}
</style>
"""


def _mcat_lower_html(tree_html: str, stats_html: str) -> str:
    """Styled lower half of the dashboard, with two clearly distinct modes:

    - Memory / Study Flashcards (blue): the deck list + a big review CTA.
    - Performance / Practice Questions (purple): big Interleaved (primary) and
      Blocked session CTAs, plus a small Topic-mastery secondary link.

    "Export my data" is a SHARED/global action, so it lives in the bottom
    navigation row (see ``_drawButtons``) beside Get Shared / Create Deck /
    Import File — not as a banner inside the dashboard body.

    All existing pycmd bridge handlers are preserved (open:<id>, opts:<id>,
    collapse:<id>, mcat_study, mcat_perf, mcat_perf_i, mcat_mastery); the
    export handler (mcat_export_my_data) now fires from the bottom nav button.
    """
    return f"""{_MCAT_LOWER_CSS}
<div class="mcat-lower">
  <div class="mcat-modes">
    <section class="mcat-mode mcat-mode-memory">
      <span class="mcat-mode-tag">Memory</span>
      <div class="mcat-mode-title">Study Flashcards</div>
      <div class="mcat-mode-desc">
        Spaced-repetition recall &mdash; build durable memory of facts and
        concepts. This is normal Anki review.
      </div>
      <div class="mcat-deck-wrap">
        <table class="mcat-deck-table" cellspacing="0" cellpadding="3">
{tree_html}
        </table>
      </div>
      <button class="mcat-cta mcat-cta-memory" onclick='pycmd("mcat_study")'>
        Study flashcards &rarr;
      </button>
    </section>

    <section class="mcat-mode mcat-mode-perf">
      <span class="mcat-mode-tag">Performance</span>
      <div class="mcat-mode-title">Practice Questions</div>
      <div class="mcat-mode-desc">
        Exam-style multiple-choice questions &mdash; apply what you know under
        test conditions. Scored separately from memory.
      </div>
      <div class="mcat-cta-group">
        <button class="mcat-cta mcat-cta-perf" onclick='pycmd("mcat_perf_i")'>
          <span class="mcat-cta-badge">Recommended</span>
          <span class="mcat-cta-main">Interleaved session</span>
          <span class="mcat-cta-sub">Mixed topics &mdash; stronger long-term retention</span>
        </button>
        <button class="mcat-cta mcat-cta-perf-alt" onclick='pycmd("mcat_perf")'>
          <span class="mcat-cta-main">Blocked session</span>
          <span class="mcat-cta-sub">One topic at a time &mdash; focused drilling</span>
        </button>
      </div>
      <div class="mcat-mode-foot">
        <span class="mcat-secondary-link" onclick='pycmd("mcat_mastery")'>Topic mastery &rarr;</span>
      </div>
    </section>
  </div>
</div>
<div class="mcat-stats-wrap">{stats_html}</div>"""
