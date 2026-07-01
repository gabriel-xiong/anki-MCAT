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
        elif cmd == "mcat_perf":
            from aqt.mcat import open_performance

            open_performance(self.mw, interleaved=False)
        elif cmd == "mcat_perf_i":
            from aqt.mcat import open_performance

            open_performance(self.mw, interleaved=True)
        elif cmd == "mcat_mastery":
            from aqt.mcat import open_mastery

            open_mastery(self.mw)
        elif cmd == "mcat_focus":
            from aqt.mcat import open_focus_area

            open_focus_area(self.mw, arg)
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
        data = self._render_data
        content = DeckBrowserContent(
            tree=self._renderDeckTree(data.tree),
            stats=self._renderStats(),
            mcat_dashboard=self._render_mcat_dashboard(),
        )
        gui_hooks.deck_browser_will_render_content(self, content)
        self.web.stdHtml(
            self._v1_upgrade_message(data.sched_upgrade_required)
            + self._body % content.__dict__,
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

        Wrapped defensively so a dashboard error can never break the deck list.
        """
        try:
            from anki.mcat_scores import dashboard_data

            data = dashboard_data(self.mw.col)
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


def _mcat_card(
    *,
    accent: str,
    label: str,
    headline: str,
    sub: str,
    note: str,
    abstaining: bool,
) -> str:
    badge = (
        "<span class='mcat-badge mcat-badge-abstain'>not enough data</span>"
        if abstaining
        else "<span class='mcat-badge'>measured</span>"
    )
    note_html = (
        f"<div class='mcat-note'>{html.escape(note)}</div>" if note else ""
    )
    return f"""
<div class="mcat-card" style="--mcat-accent:{accent};">
  <div class="mcat-card-top">
    <span class="mcat-card-label">{html.escape(label)}</span>{badge}
  </div>
  <div class="mcat-headline">{headline}</div>
  <div class="mcat-sub">{html.escape(sub)}</div>
  {note_html}
</div>"""


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
    action = data["next_action"]
    focus_html = _mcat_focus_html(data.get("focus_area"))

    # Memory card
    mem_card = _mcat_card(
        accent="#4c7cf3",
        label="Memory",
        headline=f"{mem['total_reviews']}",
        sub=f"reviews · {mem['topics_studied']}/{cov['total']} topics · "
        f"{mem['topics_unlocked']} unlocked",
        note=mem["reason"] or "",
        abstaining=mem["status"] == "abstain",
    )

    # Performance card — show accuracy even while abstaining, labeled provisional
    perf_headline = _pct(perf["accuracy"])
    perf_card = _mcat_card(
        accent="#9b5cf6",
        label="Performance",
        headline=perf_headline,
        sub=f"{perf['correct']}/{perf['attempts']} correct · "
        f"{perf['unlocked_topics']} topic(s) unlocked",
        note=perf["reason"] or "",
        abstaining=perf["status"] == "abstain",
    )

    # Readiness card
    if read["status"] == "ok" and read["range"]:
        lo, hi = read["range"]
        read_headline = f"{lo}–{hi}"
        read_sub = f"coverage {read['coverage_pct']}% · {read.get('confidence','low')} confidence"
    else:
        read_headline = "—"
        read_sub = f"coverage {read['coverage_pct']}% (need ≥50%)"
    read_card = _mcat_card(
        accent="#2bb673",
        label="Readiness (472–528)",
        headline=read_headline,
        sub=read_sub,
        note=read["reason"] or "",
        abstaining=read["status"] == "abstain",
    )

    cov_pct = cov["pct"]
    return f"""
<style>
.mcat-dash {{
  max-width: 880px; margin: 14px auto 6px auto; padding: 0 12px;
  text-align: start; font-size: 14px;
}}
.mcat-dash-head {{
  display: flex; align-items: baseline; justify-content: space-between;
  margin-bottom: 10px;
}}
.mcat-dash-title {{ font-size: 17px; font-weight: 700; }}
.mcat-dash-sub {{ color: var(--fg-subtle, #888); font-size: 12px; }}
.mcat-cards {{ display: flex; gap: 12px; flex-wrap: wrap; }}
.mcat-card {{
  flex: 1 1 0; min-width: 200px; border: 1px solid var(--border, #d7d7d7);
  border-top: 3px solid var(--mcat-accent); border-radius: 10px;
  padding: 12px 14px; background: var(--canvas-elevated, rgba(127,127,127,0.06));
}}
.mcat-card-top {{ display: flex; align-items: center; justify-content: space-between; }}
.mcat-card-label {{ font-size: 12px; font-weight: 600; color: var(--fg-subtle, #888);
  text-transform: uppercase; letter-spacing: .04em; }}
.mcat-badge {{ font-size: 10px; padding: 2px 7px; border-radius: 999px;
  background: var(--mcat-accent); color: white; font-weight: 600; }}
.mcat-badge-abstain {{ background: var(--fg-subtle, #999); }}
.mcat-headline {{ font-size: 30px; font-weight: 700; line-height: 1.15; margin: 6px 0 2px;
  color: var(--mcat-accent); }}
.mcat-sub {{ font-size: 12px; color: var(--fg-subtle, #888); }}
.mcat-note {{ font-size: 11px; margin-top: 8px; color: var(--fg-subtle, #888);
  border-top: 1px dashed var(--border, #ddd); padding-top: 6px; }}
.mcat-cover-wrap {{ margin-top: 12px; }}
.mcat-cover-bar {{ height: 8px; border-radius: 999px; overflow: hidden;
  background: var(--border, #e2e2e2); }}
.mcat-cover-fill {{ height: 100%; background: #2bb673; width: {cov_pct}%; }}
.mcat-cover-label {{ font-size: 12px; color: var(--fg-subtle, #888); margin-top: 4px; }}
.mcat-action {{ margin-top: 12px; display: flex; align-items: center;
  justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
.mcat-action-text {{ font-size: 13px; }}
.mcat-action-text b {{ color: var(--fg, inherit); }}
.mcat-btns button {{ margin-left: 6px; padding: 6px 12px; border-radius: 6px;
  border: 1px solid var(--border, #ccc); cursor: pointer;
  background: var(--canvas-elevated, transparent); color: inherit; }}
.mcat-btns button.primary {{ background: #9b5cf6; color: white; border-color: #9b5cf6; }}
.mcat-btns button.mastery {{ border-color: #4c7cf3; color: #4c7cf3; font-weight: 600; }}
.mcat-cover-head {{ display: flex; align-items: baseline; justify-content: space-between; }}
.mcat-cover-link {{ font-size: 12px; color: #4c7cf3; cursor: pointer; font-weight: 600; }}
.mcat-cover-link:hover {{ text-decoration: underline; }}
.mcat-focus {{ margin-top: 14px; display: flex; align-items: center;
  justify-content: space-between; gap: 14px; flex-wrap: wrap;
  border: 1px solid var(--border, #d7d7d7); border-left: 4px solid #f5a623;
  border-radius: 10px; padding: 14px 16px;
  background: var(--canvas-elevated, rgba(245,166,35,0.08)); }}
.mcat-focus-abstain {{ border-left-color: var(--fg-subtle, #999);
  background: var(--canvas-elevated, rgba(127,127,127,0.06)); }}
.mcat-focus-tag {{ font-size: 11px; font-weight: 700; color: #f5a623;
  text-transform: uppercase; letter-spacing: .05em; }}
.mcat-focus-abstain .mcat-focus-tag {{ color: var(--fg-subtle, #999); }}
.mcat-focus-head {{ font-size: 19px; font-weight: 700; margin: 4px 0 2px; }}
.mcat-focus-sub {{ font-size: 12px; color: var(--fg-subtle, #888); }}
.mcat-focus-btn {{ padding: 9px 16px; border-radius: 8px; cursor: pointer;
  border: none; background: #f5a623; color: #1a1a1a; font-weight: 700;
  font-size: 13px; white-space: nowrap; }}
.mcat-focus-btn:hover {{ filter: brightness(1.06); }}
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
    <div class="mcat-cover-head">
      <span class="mcat-cover-label">Coverage: {cov['measured']}/{cov['total']} topics measured ({cov_pct}%)</span>
      <span class="mcat-cover-link" onclick='pycmd("mcat_mastery")'>View topic mastery &rarr;</span>
    </div>
    <div class="mcat-cover-bar"><div class="mcat-cover-fill"></div></div>
  </div>
  <div class="mcat-action">
    <div class="mcat-action-text"><b>Next:</b> {html.escape(action)}</div>
    <div class="mcat-btns">
      <button class="mastery" onclick='pycmd("mcat_mastery")'>Topic mastery</button>
      <button onclick='pycmd("mcat_perf")'>Blocked session</button>
      <button class="primary" onclick='pycmd("mcat_perf_i")'>Interleaved session</button>
    </div>
  </div>
</div>"""
