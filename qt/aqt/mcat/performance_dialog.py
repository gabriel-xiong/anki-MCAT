# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""MCAT Speedrun — performance session view (native Qt, embedded in main window).

Select-then-confirm MCQ flow: tap a choice to highlight it, then submit.
On a miss the **correct answer and explanation stay hidden** until after the
content re-check probe (when shown) and error classification. Flow: miss →
probe (if any) → classification confirm → then reveal explanation,
``choice_feedback``, and the correct answer.

On a SCIENCE miss with a resolvable backing sub-concept, an **immediate content
re-check probe** runs before diagnosis (gated recall → self-grade). The probe
first shows ONLY the blanked recall prompt and a single "Show answer" button;
the answer and the two self-grade buttons stay hidden until that button is
clicked, so recall always happens before the student sees the answer. "Show
answer" reveals only the CONCISE answer (the filled cloze / backing card back),
never the full explanation blurb — that still surfaces later in the normal
reveal flow. The user one-tap confirms the inferred hypothesis or overrides via
the v2 3-bucket self-report buttons.
"""

from __future__ import annotations

import html
import sys
import time
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, Optional

from anki.mcat_perf import (
    ERR_APPLICATION,
    ERR_CONTENT_GAP,
    ERR_NONE,
    ERR_UNRESOLVED,
    INFERRED_DISPLAY_LABELS,
    INFERRED_SCIENCE_TYPES,
    LETTERS,
    SESSION_MODE_ASSESSMENT,
    SESSION_MODE_PRACTICE,
    PerformanceSession,
    PerfStore,
)
from aqt import colors
from aqt.qt import (
    QByteArray,
    QColor,
    QComboBox,
    QCursor,
    QDialog,
    QFrame,
    QHBoxLayout,
    QIcon,
    QLabel,
    QLayout,
    QLineEdit,
    QPainter,
    QPixmap,
    QPoint,
    QProgressBar,
    QPushButton,
    QRect,
    QScrollArea,
    QSize,
    QSizePolicy,
    QSplitter,
    Qt,
    QTimer,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.qt import sip
from aqt.theme import theme_manager
from aqt.utils import tooltip

if TYPE_CHECKING:
    from collections.abc import Callable

    from aqt.main import AnkiQt

# Confidence gate for on-screen diagnosis (per ERROR-DIAGNOSIS-SPEC.md).
MEDIUM_CONFIDENCE = 0.5

# Generic fallback starter questions for the opt-in AI assistant. At runtime
# the chips are built PER QUESTION from the item's metadata (see
# ``PerformanceView._starter_questions_for``); these curated generics only top
# up the list when metadata is too thin to specialise. They pre-fill the
# follow-up box (editable, not auto-sent) so a student can start with one tap.
# NO runtime LLM call generates these (AI-off-safe). The runtime prompt builder
# (MCAT/scripts/ai_qa.py::build_followup_prompt) injects the full anchor
# context, so a short question here is enough.
FOLLOWUP_STARTER_QUESTIONS: tuple[str, ...] = (
    "Why are the other answer choices wrong?",
    "Explain the concept behind this question.",
    "How might this show up in a passage?",
    "What's a good way to remember this?",
    "What's the most common mistake here?",
)

# Cap the rendered "Try asking" starter chips so the assistant panel stays
# uncluttered: the per-question builder orders the most useful/specific prompts
# first (chosen distractor, correct choice, error mode), so keeping the top few
# preserves the best ones and drops the long tail.
MAX_STARTER_CHIPS = 3

# Docked AI assistant chat panel sizing (right-hand column of the body
# splitter). The default gives the conversation room to breathe without
# dominating a narrow window; the splitter still lets the student drag the
# divider, and the dock collapses to 0 width when the assistant is hidden.
AI_DOCK_DEFAULT_WIDTH = 460
AI_DOCK_MIN_WIDTH = 340

# Minimum content-box height (px) for an answer-choice button. Used both as the
# QSS ``min-height`` floor in the shared choice style and as the baseline for the
# per-button ``setMinimumHeight`` computed from font metrics. Kept generous so a
# single line of choice text (letter + label) never clips top/bottom when the
# button is re-styled at grade time or when the surrounding splitter reallocates
# vertical space.
CHOICE_MIN_CONTENT_HEIGHT = 24

# Reason categories for the "Report incorrect" flag on an AI answer. Kept short
# so the picker stays compact; the stored value is the lowercase key. Optional —
# the student can leave it on the default and just submit (or add a note).
AI_FLAG_REASONS: tuple[tuple[str, str], ...] = (
    ("wrong", "Wrong"),
    ("misleading", "Misleading"),
    ("incomplete", "Incomplete"),
    ("other", "Other"),
)

# MCAT Speedrun brand accents (match deck-browser dashboard).
_ACCENT_PERF = "#9b5cf6"
_ACCENT_BLUE = "#4c7cf3"
_ACCENT_GREEN = "#2bb673"
_ACCENT_ORANGE = "#f5a623"
_ACCENT_RED = "#cf222e"

# Result markers on the answer choices. The wrong marker is shown IMMEDIATELY on
# the student's chosen distractor the moment they confirm (before the quick-check
# probe), so a miss is unmistakable; the same ✗/✓ pair is reused at full reveal.
_MARK_WRONG = "\u2717"  # ✗
_MARK_CORRECT = "\u2713"  # ✓


def _theme_tokens() -> dict[str, str]:
    """Theme-aware palette aligned with Anki web UI + MCAT dashboard."""
    tm = theme_manager
    night = tm.night_mode
    # Clean white/near-white surfaces in light mode; theme-native in dark.
    # (Anki's colors.CANVAS is light-gray in light mode, so we use white here.)
    warm_canvas = "#ffffff" if not night else tm.var(colors.CANVAS)
    warm_elevated = "#ffffff" if not night else tm.var(colors.CANVAS_ELEVATED)
    return {
        "canvas": warm_canvas,
        "elevated": warm_elevated,
        "fg": tm.var(colors.FG),
        "fg_subtle": tm.var(colors.FG_SUBTLE),
        "border": tm.var(colors.BORDER),
        "border_subtle": tm.var(colors.BORDER_SUBTLE),
        "link": tm.var(colors.FG_LINK),
        "accent": _ACCENT_PERF,
        "accent_blue": _ACCENT_BLUE,
        "accent_green": _ACCENT_GREEN,
        "accent_orange": _ACCENT_ORANGE,
        "accent_red": _ACCENT_RED,
        "choice_bg": tm.var(colors.CANVAS_CODE),
        "choice_selected_bg": "rgba(155,92,246,0.18)" if night else "#f3ebff",
        "choice_correct_bg": "rgba(43,182,115,0.18)" if night else "#eafaf1",
        "choice_wrong_bg": "rgba(207,34,46,0.14)" if night else "#fff5f5",
        "choice_neutral_bg": tm.var(colors.CANVAS_ELEVATED),
        "probe_bg": "rgba(76,124,243,0.14)" if night else "#eef4ff",
        "probe_border": "rgba(76,124,243,0.45)" if night else "#b6c8ff",
        "miss_bg": "rgba(207,34,46,0.10)" if night else "#fff5f5",
        "miss_border": "rgba(207,34,46,0.35)" if night else "#ffcdd2",
        "warn_bg": "rgba(245,166,35,0.12)" if night else "#fff8e6",
        "warn_border": "rgba(245,166,35,0.45)" if night else "#e6cf88",
        # Amber/gold accent for the "Not sure" opt-out button. Warm and clearly
        # visible against the gray surface, but NOT red (this is an encouraged
        # honest opt-out, not an error). Text color is theme-tuned so the label
        # stays legible in BOTH themes: bright amber on dark, deep gold on white
        # (plain amber text on white fails contrast).
        "amber_text": "#f5a623" if night else "#8a5300",
        "amber_bg": "rgba(245,166,35,0.16)" if night else "#fff4d6",
        "amber_bg_hover": "rgba(245,166,35,0.30)" if night else "#ffe6ad",
        "amber_border": "rgba(245,166,35,0.60)" if night else "#e0a428",
        "action_bg": "rgba(43,182,115,0.12)" if night else "#eaf5ea",
        "action_border": "rgba(43,182,115,0.35)" if night else "#b6d8b6",
        "ai_bg": "rgba(155,92,246,0.10)" if night else "#f0f6ff",
        "ai_border": "rgba(155,92,246,0.35)" if night else "#b6d8ff",
    }


def _choice_styles(t: dict[str, str]) -> dict[str, str]:
    # ``min-height`` is part of the SHARED base (identical in every state:
    # default / selected / correct / wrong / neutral), so the graded state keeps
    # the exact same content-box height as the pre-answer state and the letter +
    # choice text can never be clipped top/bottom when the button is re-styled
    # after submit. Only the border/background differ between states. The QSS
    # floor is a secondary guard; the primary, layout-proof floor is the
    # per-button ``setMinimumHeight`` applied at creation (see ``_show_question``)
    # — a widget-level minimum the layout/splitter cannot compress below,
    # regardless of vertical size policy.
    base = (
        "text-align: left; padding: 12px 14px; margin: 4px 0; "
        f"border-radius: 10px; font-size: 14px; min-height: {CHOICE_MIN_CONTENT_HEIGHT}px;"
    )
    hover = f"QPushButton:hover:enabled {{ background: {t['choice_selected_bg']}; }}"
    return {
        "default": (
            f"QPushButton {{ {base} border: 1px solid {t['border']}; "
            f"background: {t['elevated']}; color: {t['fg']}; }}"
            f"{hover}"
        ),
        "selected": (
            f"QPushButton {{ {base} border: 2px solid {t['accent']}; "
            f"background: {t['choice_selected_bg']}; color: {t['fg']}; "
            f"font-weight: 600; }}"
        ),
        "correct": (
            f"QPushButton {{ {base} border: 2px solid {t['accent_green']}; "
            f"background: {t['choice_correct_bg']}; color: {t['fg']}; }}"
        ),
        "wrong": (
            f"QPushButton {{ {base} border: 2px solid {t['accent_red']}; "
            f"background: {t['choice_wrong_bg']}; color: {t['fg']}; }}"
        ),
        "neutral": (
            f"QPushButton {{ {base} border: 1px solid {t['border_subtle']}; "
            f"background: {t['choice_neutral_bg']}; color: {t['fg_subtle']}; }}"
        ),
    }


def _choice_fb_style(t: dict[str, str], kind: str) -> str:
    """Style for a per-choice feedback line rendered as its OWN wrapping label
    below the choice button (so long feedback wraps fully instead of being
    clipped inside a fixed-height button). Color-coded to the choice outcome.
    """
    color = {
        "correct": t["accent_green"],
        "wrong": t["accent_red"],
    }.get(kind, t["fg_subtle"])
    return (
        f"color: {color}; font-size: 12px; line-height: 1.45; "
        f"padding: 2px 14px 10px 32px; margin: 0; "
        f"border: none; background: transparent;"
    )


def _section_header_style(t: dict[str, str]) -> str:
    return (
        f"font-size: 11px; font-weight: 700; color: {t['fg_subtle']}; "
        f"text-transform: uppercase; letter-spacing: 0.05em; "
        f"margin: 4px 0 2px 0; border: none; background: transparent;"
    )


def _starter_chip_style(t: dict[str, str]) -> str:
    """Compact pill styling for the follow-up starter chips (AI accent)."""
    return (
        f"QPushButton {{ background: {t['elevated']}; color: {t['accent']}; "
        f"border: 1px solid {t['ai_border']}; border-radius: 999px; "
        f"padding: 7px 14px; font-size: 13px; font-weight: 600; "
        f"text-align: left; }}"
        f"QPushButton:hover {{ background: {t['choice_selected_bg']}; "
        f"border-color: {t['accent']}; }}"
    )


def _mic_button_style(t: dict[str, str]) -> str:
    """Compact icon-button styling for the dictation (mic) button.

    Matches the follow-up input border/elevated surface and picks up the AI
    accent on hover, so it reads as part of the assistant input cluster rather
    than a foreign control.
    """
    return (
        f"QPushButton {{ background: {t['elevated']}; color: {t['accent']}; "
        f"border: 1px solid {t['border']}; border-radius: 8px; "
        f"padding: 6px; }}"
        f"QPushButton:hover {{ background: {t['choice_selected_bg']}; "
        f"border-color: {t['accent']}; }}"
    )


# Standard vertical microphone (Feather-style, stroke-based, ``currentColor``
# so it themes): capsule mic head + U-shaped cradle + straight stand + base.
# Inline so there is no asset dependency; recolored per theme before rendering.
_MIC_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
    "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
    "stroke-linejoin='round'>"
    "<rect x='9' y='2' width='6' height='11' rx='3'/>"
    "<path d='M5 10v1a7 7 0 0 0 14 0v-1'/>"
    "<line x1='12' y1='18' x2='12' y2='22'/>"
    "<line x1='8' y1='22' x2='16' y2='22'/>"
    "</svg>"
)

# Icon edge (logical px). Sized to sit inside the input-row controls without
# dominating them — a normal small icon button, not an oversized glyph.
_MIC_ICON_PX = 18


def _mic_icon(color: str, px: int = _MIC_ICON_PX) -> "QIcon | None":
    """Render the inline mic SVG to a crisp, themed ``QIcon`` (or None).

    ``currentColor`` is substituted with the theme accent, then the SVG is
    rasterised at 2x and tagged with a device-pixel-ratio so it stays sharp on
    HiDPI. Returns None if the Qt SVG backend is unavailable, so the caller can
    fall back to a hand-drawn mic (never a broken/oversized glyph).
    """
    try:
        from PyQt6.QtSvg import QSvgRenderer
    except Exception:
        return None
    try:
        svg = _MIC_SVG.replace("currentColor", color)
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        scale = 2
        pm = QPixmap(px * scale, px * scale)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        renderer.render(painter)
        painter.end()
        pm.setDevicePixelRatio(scale)
        return QIcon(pm)
    except Exception:
        return None


def _mic_icon_drawn(color: str, px: int = _MIC_ICON_PX) -> QIcon:
    """Fallback vertical mic drawn with QPainter primitives (no SVG backend).

    Same silhouette as ``_MIC_SVG`` (capsule head + cradle + stand + base) so
    the control looks identical whether or not Qt's SVG plugin is present.
    """
    scale = 2
    pm = QPixmap(px * scale, px * scale)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = painter.pen()
    pen.setColor(QColor(color))
    pen.setWidthF(2.0 * scale)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    u = px * scale / 24.0  # map the 24x24 SVG coordinate space onto the pixmap
    painter.drawRoundedRect(
        QRect(round(9 * u), round(2 * u), round(6 * u), round(11 * u)),
        3 * u,
        3 * u,
    )
    painter.drawArc(
        QRect(round(5 * u), round(4 * u), round(14 * u), round(14 * u)),
        180 * 16,
        180 * 16,
    )
    painter.drawLine(round(12 * u), round(18 * u), round(12 * u), round(22 * u))
    painter.drawLine(round(8 * u), round(22 * u), round(16 * u), round(22 * u))
    painter.end()
    pm.setDevicePixelRatio(scale)
    return QIcon(pm)


def _os_dictation_hint() -> str:
    """Per-OS hint for how to start built-in dictation (no cloud, no deps)."""
    if sys.platform.startswith("win"):
        return "Press Win+H to dictate"
    if sys.platform == "darwin":
        return "Press the mic/Fn key to dictate"
    return "Use your OS voice typing to dictate"


def _win_launch_dictation() -> bool:
    """Best-effort: open Windows voice typing (Win+H) via the OS, no new deps.

    Synthesizes the Win+H hotkey through the Win32 ``keybd_event`` API using the
    stdlib ``ctypes`` (no third-party dependency, no network). Returns True if
    the keystrokes were dispatched, False on any failure so the caller can fall
    back to a focus + hint affordance. Non-blocking: ``keybd_event`` only posts
    input events and returns immediately.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        VK_LWIN = 0x5B
        VK_H = 0x48
        KEYEVENTF_KEYUP = 0x0002
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.keybd_event(VK_LWIN, 0, 0, 0)
        user32.keybd_event(VK_H, 0, 0, 0)
        user32.keybd_event(VK_H, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception:
        return False


def _assistant_header_style(t: dict[str, str]) -> str:
    """Bold accent header that marks the AI panel as its own 'Assistant' space."""
    return (
        f"font-size: 14px; font-weight: 800; color: {t['accent']}; "
        f"letter-spacing: 0.02em; background: transparent; border: none;"
    )


def _assistant_sub_style(t: dict[str, str]) -> str:
    return (
        f"font-size: 11px; color: {t['fg_subtle']}; background: transparent; "
        f"border: none;"
    )


def _bubble_style(t: dict[str, str], role: str) -> str:
    """Chat-bubble styling for the follow-up conversation thread.

    ``you`` bubbles use a blue accent bar, ``assistant`` bubbles the perf accent,
    so an extended back-and-forth reads as a threaded conversation rather than a
    one-shot answer box.
    """
    if role == "you":
        bg = t["choice_selected_bg"]
        bar = t["accent_blue"]
    else:
        bg = t["elevated"]
        bar = t["accent"]
    return (
        f"background: {bg}; border: 1px solid {t['ai_border']}; "
        f"border-left: 3px solid {bar}; border-radius: 10px; "
        f"padding: 10px 12px; margin: 3px 0; font-size: 14px; "
        f"color: {t['fg']};"
    )


class _FlowLayout(QLayout):
    """Minimal left-to-right wrapping layout (Qt's classic FlowLayout).

    Lets the starter chips stay compact (sized to content) and wrap onto extra
    rows at any card width instead of stretching or clipping — keeps the panel
    uncluttered on both narrow and wide windows.
    """

    def __init__(
        self, parent: QWidget | None = None, margin: int = 0, spacing: int = 6
    ) -> None:
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)
        self._items: list = []

    def addItem(self, item: Any) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Any:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> Any:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )
        return size

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        x = rect.x()
        y = rect.y()
        line_height = 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if next_x - spacing > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


class PerformanceView(QWidget):
    """Full-width performance session embedded in the main Anki window."""

    def __init__(
        self,
        mw: AnkiQt,
        store: PerfStore,
        questions: list[dict[str, Any]],
        *,
        interleaved: bool,
        session_mode: str = SESSION_MODE_PRACTICE,
        filter_key: str | None = None,
        resume: dict[str, Any] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(mw)
        # Paint an OPAQUE, theme-matched background across the whole view. As a
        # QWidget subclass, its stylesheet ``background`` is only honored with
        # WA_StyledBackground — without it the transparent header/footer bars
        # (and any uncovered region) reveal the main window's gray canvas, which
        # reads as the dashboard/gray "bleeding" into the performance screen.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.mw = mw
        self.store = store
        self.interleaved = interleaved
        self.session_mode = session_mode
        self._on_close = on_close
        self.session = PerformanceSession(
            store,
            questions,
            interleaved=interleaved,
            session_mode=session_mode,
            filter_key=filter_key,
            resume=resume,
        )
        self._shown_at = 0.0
        self._chosen_idx: Optional[int] = None
        self._inferred_type: Optional[str] = None
        self._practice_items: list[dict[str, Any]] = []
        self._probe_target: Optional[dict[str, Any]] = None
        self._selected_idx: Optional[int] = None
        self._first_choice_idx: Optional[int] = None
        self._answer_changes = 0
        self._submitted = False
        self._revealed = False
        self._ai_panel_visible = False
        # Last AI output shown in the panel (explainer or follow-up), tracked so
        # the "Report incorrect" flag captures the EXACT text + attribution the
        # student saw. None until an AI answer is actually rendered.
        self._last_ai_text: Optional[str] = None
        self._last_ai_source: Optional[str] = None
        self._last_ai_kind: Optional[str] = None
        # Monotonic request id for the opt-in AI calls. Each new request (or any
        # panel reset / question change) bumps this; a background callback only
        # touches the UI when its captured id still matches, so a late reply from
        # a superseded request can't clobber the panel or crash a closed view.
        self._ai_generation = 0
        self._choice_styles = _choice_styles(_theme_tokens())

        self._mode_label = (
            "Assessment"
            if session_mode == SESSION_MODE_ASSESSMENT
            else ("Interleaved" if interleaved else "Blocked")
        )

        self._build_ui()
        self.apply_theme()
        self._show_question()
        if self.session.awaiting_error_type:
            self._restore_pending_miss()

    def _restore_pending_miss(self) -> None:
        """Re-render a saved in-progress miss (session resume)."""
        idx = getattr(self.session, "_pending_choice", None)
        if idx is None:
            return
        self._submitted = True
        self._chosen_idx = idx
        self._selected_idx = idx
        self.submit_button.setVisible(False)
        self.submit_button.setEnabled(False)
        self.not_sure_button.setVisible(False)
        self.not_sure_button.setEnabled(False)
        self.not_sure_info.setVisible(False)
        for b in self.choice_buttons:
            b.setEnabled(False)
        self._mark_choice_wrong(idx)
        t = _theme_tokens()
        self.feedback.setText(
            f"{_MARK_WRONG} Not quite — quick check and error type first, "
            "then the explanation."
        )
        self.feedback.setStyleSheet(
            f"color: {t['accent_red']}; font-weight: 600; font-style: italic; "
            f"margin-top: 8px; background: transparent; border: none;"
        )
        if self.session.current_is_cars:
            # A resumed CARS miss skips the science error-type step too.
            self._resolve_cars_miss()
            return
        if self.session.probe_informed:
            self._show_diagnosis()
            return
        self._probe_target = self.session.probe_target()
        if self._probe_target is not None:
            self._start_probe()
        else:
            self._show_diagnosis()

    # UI scaffold -----------------------------------------------------------

    def _style_host(self, t: dict[str, str]) -> None:
        """Paint the embedding host slot opaque + theme-matched.

        The view is swapped into ``mw.mcatPerformanceSlot`` (see aqt.mcat
        ``_show_performance_view``). Keeping that slot opaque means no gray
        main-window background can flash around the view on show/resize.
        """
        host = self.parentWidget()
        if host is not None and host.objectName() == "mcatPerformanceSlot":
            host.setStyleSheet(
                f"QWidget#mcatPerformanceSlot {{ background: {t['canvas']}; }}"
            )

    def apply_theme(self) -> None:
        """Refresh styles when Anki theme changes (light/dark)."""
        t = _theme_tokens()
        self._choice_styles = _choice_styles(t)
        self.setStyleSheet(
            f"PerformanceView {{ background: {t['canvas']}; color: {t['fg']}; }}"
        )
        self._style_host(t)
        # Paint the whole left column opaque with the themed canvas: the scroll
        # area, its (transparent-by-default) viewport, the scroll content, and
        # the two centering wrappers. This keeps the host's stale grey/beige
        # from ever showing through around the centered card.
        self.scroll.setStyleSheet(
            f"QScrollArea {{ background: {t['canvas']}; border: none; }}"
            f"QScrollArea > QWidget#mcatPerfViewport {{ background: {t['canvas']}; }}"
            f"QScrollArea > QWidget > QWidget {{ background: {t['canvas']}; }}"
        )
        self.scroll.viewport().setStyleSheet(
            f"QWidget#mcatPerfViewport {{ background: {t['canvas']}; }}"
        )
        self.scroll_content.setStyleSheet(
            f"QWidget#mcatPerfScrollContent {{ background: {t['canvas']}; }}"
        )
        self.content_stack.setStyleSheet(
            f"QWidget#mcatPerfContentStack {{ background: {t['canvas']}; }}"
        )
        self.session_page.setStyleSheet(
            f"QWidget#mcatPerfSessionPage {{ background: {t['canvas']}; }}"
        )
        self.header_bar.setStyleSheet(
            f"QFrame#mcatPerfHeader {{ background: transparent; "
            f"border-bottom: 2px solid {t['accent']}; }}"
        )
        self.title_label.setStyleSheet(
            f"font-size: 18px; font-weight: 700; color: {t['fg']}; "
            f"background: transparent; border: none;"
        )
        self.subtitle_label.setStyleSheet(
            f"font-size: 12px; color: {t['fg_subtle']}; margin-top: 2px; "
            f"background: transparent; border: none;"
        )
        self.mode_badge.setStyleSheet(
            f"background: {t['accent']}; color: white; font-size: 11px; "
            f"font-weight: 700; padding: 4px 10px; border-radius: 999px;"
        )
        self.back_button.setStyleSheet(
            f"QPushButton {{ color: {t['link']}; border: 1px solid {t['border']}; "
            f"border-radius: 8px; padding: 6px 12px; background: transparent; "
            f"font-weight: 600; }}"
            f"QPushButton:hover {{ background: {t['elevated']}; }}"
        )
        self.header.setStyleSheet(
            f"font-weight: 700; font-size: 15px; color: {t['fg']}; "
            f"background: transparent; border: none;"
        )
        self.progress.setStyleSheet(
            f"QProgressBar {{ background: {t['border_subtle']}; border: none; "
            f"border-radius: 4px; }}"
            f"QProgressBar::chunk {{ background: {t['accent']}; border-radius: 4px; }}"
        )
        self.source.setStyleSheet(
            f"color: {t['fg_subtle']}; font-size: 11px; "
            f"text-transform: uppercase; letter-spacing: .04em; "
            f"background: transparent; border: none;"
        )
        self.stem.setStyleSheet(
            f"font-size: 16px; line-height: 1.45; padding: 10px 0 14px 0; "
            f"color: {t['fg']}; background: transparent; border: none;"
        )
        self.content_card.setStyleSheet(
            f"QFrame#mcatPerfContent {{ background: {t['elevated']}; "
            f"border: 1px solid {t['border']}; border-top: 3px solid {t['accent']}; "
            f"border-radius: 12px; }}"
        )
        self.submit_button.setStyleSheet(
            f"QPushButton {{ background: {t['accent']}; color: white; "
            f"border: none; border-radius: 8px; padding: 10px 22px; "
            f"font-weight: 700; font-size: 14px; }}"
            f"QPushButton:disabled {{ background: {t['border_subtle']}; "
            f"color: {t['fg_subtle']}; }}"
            f"QPushButton:hover:enabled {{ filter: brightness(1.06); }}"
        )
        # Amber/gold outline treatment: distinct and visible against the gray
        # surface (so the honest opt-out is discoverable), but still clearly
        # SECONDARY to the solid accent-fill Confirm button — outlined + tinted
        # rather than a competing solid fill. Amber, never red: this is an
        # encouraged anti-guessing choice, not a failure state.
        self.not_sure_button.setStyleSheet(
            f"QPushButton {{ color: {t['amber_text']}; "
            f"border: 1.5px solid {t['amber_border']}; border-radius: 8px; "
            f"padding: 10px 18px; background: {t['amber_bg']}; "
            f"font-weight: 700; font-size: 13px; }}"
            f"QPushButton:hover:enabled {{ background: {t['amber_bg_hover']}; "
            f"color: {t['fg']}; }}"
            f"QPushButton:disabled {{ color: {t['border_subtle']}; "
            f"border-color: {t['border_subtle']}; background: transparent; }}"
        )
        self.not_sure_info.setStyleSheet(
            f"QToolButton#mcatNotSureInfo {{ color: {t['fg_subtle']}; "
            f"font-size: 15px; padding: 0 2px; margin: 0; background: transparent; "
            f"border: none; }}"
            f"QToolButton#mcatNotSureInfo:hover {{ color: {t['accent']}; "
            f"background: transparent; }}"
            f"QToolButton#mcatNotSureInfo:pressed {{ background: transparent; }}"
        )
        self.probe_frame.setStyleSheet(
            f"QFrame#mcatProbeFrame {{ background: {t['probe_bg']}; "
            f"border: 1px solid {t['probe_border']}; border-left: 4px solid "
            f"{t['accent_blue']}; border-radius: 10px; padding: 4px; }}"
        )
        self.probe_prompt.setStyleSheet(
            f"border: none; background: transparent; font-size: 14px; "
            f"color: {t['fg']}; padding: 8px 10px 4px 10px;"
        )
        self.probe_answer_label.setStyleSheet(
            f"border: none; background: transparent; font-size: 14px; "
            f"color: {t['fg']}; padding: 2px 10px 6px 10px;"
        )
        self.probe_reveal_button.setStyleSheet(
            f"QPushButton {{ background: {t['accent_blue']}; color: white; "
            f"border: none; border-radius: 8px; padding: 8px 14px; "
            f"font-weight: 600; }}"
        )
        self.probe_knew_button.setStyleSheet(
            f"QPushButton {{ background: {t['accent_green']}; color: white; "
            f"border: none; border-radius: 8px; padding: 8px 14px; "
            f"font-weight: 600; }}"
        )
        self.probe_missed_button.setStyleSheet(
            f"QPushButton {{ background: {t['accent_orange']}; color: #1a1a1a; "
            f"border: none; border-radius: 8px; padding: 8px 14px; "
            f"font-weight: 600; }}"
        )
        self.choice_feedback_label.setStyleSheet(
            f"background: {t['warn_bg']}; border: 1px solid {t['warn_border']}; "
            f"border-radius: 10px; padding: 10px; margin-top: 8px; "
            f"font-size: 13px; color: {t['fg']};"
        )
        self.explanation_label.setStyleSheet(
            f"background: {t['elevated']}; border: 1px solid {t['border']}; "
            f"border-left: 4px solid {t['accent_green']}; border-radius: 10px; "
            f"padding: 10px; margin-top: 8px; font-size: 13px; color: {t['fg']};"
        )
        self.ask_ai_button.setStyleSheet(
            f"color: {t['accent']}; text-align: left; padding: 6px 0; "
            f"border: none; font-weight: 600; background: transparent;"
        )
        # Full-height right dock: a distinct tinted surface with an accent left
        # edge that separates it from the question column (no rounded card /
        # margin — it spans the splitter pane).
        self.ai_panel.setStyleSheet(
            f"QFrame#mcatAiPanel {{ background: {t['ai_bg']}; "
            f"border: none; border-left: 3px solid {t['accent']}; }}"
        )
        # The inner conversation scroll is transparent so the dock tint shows
        # through; only its scrollbar chrome is themed by Qt.
        self.ai_scroll.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
            f"QScrollArea > QWidget > QWidget {{ background: transparent; }}"
        )
        self.assistant_header_label.setStyleSheet(_assistant_header_style(t))
        self.assistant_sub_label.setStyleSheet(_assistant_sub_style(t))
        self.sync_ai_toggle_display()
        self.assistant_divider.setStyleSheet(
            f"background: {t['ai_border']}; border: none;"
        )
        self.ai_explainer_label.setStyleSheet(
            f"border: none; background: transparent; font-size: 15px; "
            f"color: {t['fg']};"
        )
        for _role, _bubble in getattr(self, "_conversation_bubbles", []):
            _bubble.setStyleSheet(_bubble_style(t, _role))
        self.followup_input.setStyleSheet(
            f"QLineEdit {{ background: {t['elevated']}; color: {t['fg']}; "
            f"border: 1px solid {t['border']}; border-radius: 8px; "
            f"padding: 8px 12px; font-size: 14px; }}"
            f"QLineEdit:focus {{ border-color: {t['accent']}; }}"
        )
        self.followup_button.setStyleSheet(
            f"QPushButton {{ background: {t['accent']}; color: white; "
            f"border: none; border-radius: 8px; padding: 8px 18px; "
            f"font-weight: 700; font-size: 13px; }}"
            f"QPushButton:disabled {{ background: {t['border_subtle']}; "
            f"color: {t['fg_subtle']}; }}"
        )
        self.mic_button.setStyleSheet(_mic_button_style(t))
        self._apply_mic_icon(t)
        self.flag_link.setStyleSheet(
            f"QPushButton {{ color: {t['fg_subtle']}; text-align: right; "
            f"padding: 2px 0; border: none; background: transparent; "
            f"font-size: 11px; }}"
            f"QPushButton:hover {{ color: {t['accent_red']}; }}"
            f"QPushButton:disabled {{ color: {t['accent_green']}; }}"
        )
        self.flag_form.setStyleSheet("QFrame { border: none; background: transparent; }")
        self.flag_reason_combo.setStyleSheet(
            f"QComboBox {{ background: {t['elevated']}; color: {t['fg']}; "
            f"border: 1px solid {t['border']}; border-radius: 6px; "
            f"padding: 3px 6px; font-size: 12px; }}"
        )
        self.flag_note_input.setStyleSheet(
            f"QLineEdit {{ background: {t['elevated']}; color: {t['fg']}; "
            f"border: 1px solid {t['border']}; border-radius: 6px; "
            f"padding: 4px 8px; font-size: 12px; }}"
        )
        self.flag_submit_button.setStyleSheet(
            f"QPushButton {{ background: {t['accent']}; color: white; "
            f"border: none; border-radius: 6px; padding: 4px 12px; "
            f"font-weight: 600; font-size: 12px; }}"
        )
        self.flag_cancel_button.setStyleSheet(
            f"QPushButton {{ color: {t['fg_subtle']}; border: none; "
            f"background: transparent; font-size: 12px; padding: 4px 6px; }}"
        )
        if hasattr(self, "starter_hint_label"):
            self.starter_hint_label.setStyleSheet(_section_header_style(t))
        chip_style = _starter_chip_style(t)
        for chip in getattr(self, "starter_chips", []):
            chip.setStyleSheet(chip_style)
        self.miss_frame.setStyleSheet(
            f"QFrame {{ background: {t['miss_bg']}; border: 1px solid {t['miss_border']}; "
            f"border-left: 4px solid {t['accent_red']}; border-radius: 10px; "
            f"padding: 4px; margin-top: 8px; }}"
        )
        self.diag_label.setStyleSheet(
            f"font-size: 14px; border: none; background: transparent; color: {t['fg']};"
        )
        self.diag_hint_label.setStyleSheet(
            f"color: {t['fg_subtle']}; font-size: 12px; border: none; "
            f"background: transparent;"
        )
        self.misconception_label.setStyleSheet(
            f"color: {t['fg_subtle']}; font-style: italic; border: none; "
            f"background: transparent;"
        )
        self.confirm_button.setStyleSheet(
            f"QPushButton {{ background: {t['accent']}; color: white; "
            f"border: none; border-radius: 8px; padding: 8px 14px; "
            f"font-weight: 700; }}"
        )
        self.override_toggle.setStyleSheet(
            f"color: {t['link']}; text-align: left; padding: 4px; "
            f"border: none; background: transparent; font-weight: 600;"
        )
        self.next_action_label.setStyleSheet(
            f"background: {t['action_bg']}; border: 1px solid {t['action_border']}; "
            f"border-radius: 10px; padding: 10px; margin-top: 8px; "
            f"font-size: 13px; color: {t['fg']};"
        )
        self.practice_button.setStyleSheet(
            f"QPushButton {{ background: {t['accent_green']}; color: white; "
            f"border: none; border-radius: 8px; padding: 8px 14px; "
            f"font-weight: 700; }}"
        )
        self.score_label.setStyleSheet(
            f"color: {t['fg_subtle']}; font-size: 13px; "
            f"background: transparent; border: none;"
        )
        self.next_button.setStyleSheet(
            f"QPushButton {{ background: {t['accent_blue']}; color: white; "
            f"border: none; border-radius: 8px; padding: 8px 18px; "
            f"font-weight: 700; }}"
            f"QPushButton:disabled {{ background: {t['border_subtle']}; "
            f"color: {t['fg_subtle']}; }}"
        )
        err_btn = (
            f"QPushButton {{ background: {t['elevated']}; color: {t['fg']}; "
            f"border: 1px solid {t['border']}; border-radius: 8px; "
            f"padding: 8px 12px; font-weight: 600; font-size: 12px; }}"
            f"QPushButton:hover {{ background: {t['choice_selected_bg']}; "
            f"border-color: {t['accent']}; }}"
        )
        for b in getattr(self, "error_buttons", []):
            b.setStyleSheet(err_btn)
        if hasattr(self, "question_section_label"):
            self.question_section_label.setStyleSheet(_section_header_style(t))
        if hasattr(self, "choices_section_label"):
            self.choices_section_label.setStyleSheet(_section_header_style(t))
        if hasattr(self, "remediation_slot"):
            self.remediation_slot.setStyleSheet(f"background: {t['canvas']};")
        self.footer_bar.setStyleSheet(
            f"QFrame#mcatPerfFooter {{ background: transparent; "
            f"border-top: 1px solid {t['border_subtle']}; }}"
        )
        if hasattr(self, "choice_buttons"):
            self._update_choice_styles()
            if self._submitted and self._revealed:
                self._show_choice_feedback()
            elif (
                self._submitted
                and not self._revealed
                and self._chosen_idx is not None
            ):
                # Pre-reveal miss (probe/diagnosis in progress): keep the
                # immediate ✗ on the chosen-wrong option across a theme change.
                self._mark_choice_wrong(self._chosen_idx)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.header_bar = QFrame()
        self.header_bar.setObjectName("mcatPerfHeader")
        header_layout = QHBoxLayout(self.header_bar)
        header_layout.setContentsMargins(16, 10, 16, 10)
        header_layout.setSpacing(12)

        # Shared "return home" convention across MCAT screens: a visible,
        # top-left "← Back to dashboard" control (matches the dialogs). Wired to
        # the SAME safe teardown as before (_confirm_exit → _finish_session →
        # on_close = _exit_performance), so scores refresh on the dashboard and
        # there is no view bleed; the mid-question guard in _confirm_exit stays.
        self.back_button = QPushButton("← Back to dashboard")
        qconnect(self.back_button.clicked, self._confirm_exit)
        header_layout.addWidget(self.back_button)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        self.title_label = QLabel("MCAT Speedrun · Performance")
        self.subtitle_label = QLabel(
            "Separate from memory score · select an answer, then confirm"
        )
        title_col.addWidget(self.title_label)
        title_col.addWidget(self.subtitle_label)
        header_layout.addLayout(title_col, stretch=1)

        self.mode_badge = QLabel(self._mode_label)
        header_layout.addWidget(self.mode_badge, alignment=Qt.AlignmentFlag.AlignRight)
        outer.addWidget(self.header_bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # The QScrollArea viewport is TRANSPARENT by default, so the host's
        # stale grey/beige canvas can bleed through around the centered card.
        # Give the viewport a stable object name (styled opaque in apply_theme)
        # and paint its own background so nothing behind it shows through.
        self.scroll.viewport().setObjectName("mcatPerfViewport")
        self.scroll.viewport().setAutoFillBackground(True)
        self.scroll_content = QWidget()
        self.scroll_content.setObjectName("mcatPerfScrollContent")
        self.scroll_content.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True
        )
        self.scroll_content.setAutoFillBackground(True)
        content_outer = QVBoxLayout(self.scroll_content)
        content_outer.setContentsMargins(20, 16, 20, 16)
        content_outer.setSpacing(12)

        # Centered column (matches deck-browser dashboard max-width). Content is
        # TOP-anchored (AlignTop) so the card sits near the top of the page and
        # never floats in the vertical middle; horizontal AlignHCenter keeps it
        # centered within the max-width column. Both wrappers paint the themed
        # canvas so the gaps around the ``content_card`` (left/right at wide
        # widths, below at short heights) never expose the stale host background.
        self.content_stack = QWidget()
        self.content_stack.setObjectName("mcatPerfContentStack")
        self.content_stack.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True
        )
        self.content_stack.setAutoFillBackground(True)
        stack_layout = QVBoxLayout(self.content_stack)
        stack_layout.setContentsMargins(0, 0, 0, 0)
        stack_layout.setSpacing(12)
        stack_layout.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
        )

        self.session_page = QWidget()
        self.session_page.setObjectName("mcatPerfSessionPage")
        self.session_page.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True
        )
        self.session_page.setAutoFillBackground(True)
        session_layout = QVBoxLayout(self.session_page)
        session_layout.setContentsMargins(0, 0, 0, 0)
        session_layout.setSpacing(12)
        session_layout.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
        )

        self.content_card = QFrame()
        self.content_card.setObjectName("mcatPerfContent")
        self.content_card.setMaximumWidth(880)
        self.content_card.setMinimumWidth(320)
        root = QVBoxLayout(self.content_card)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        self.header = QLabel()
        root.addWidget(self.header)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        root.addWidget(self.progress)

        self.source = QLabel()
        self.source.setWordWrap(True)
        root.addWidget(self.source)

        self.question_section_label = QLabel("Question")
        root.addWidget(self.question_section_label)

        self.stem = QLabel()
        self.stem.setWordWrap(True)
        self.stem.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        root.addWidget(self.stem)

        self.choices_section_label = QLabel("Answer choices · tap one, then confirm")
        root.addWidget(self.choices_section_label)

        self.choices_box = QVBoxLayout()
        self.choices_box.setSpacing(6)
        root.addLayout(self.choices_box)

        submit_row = QHBoxLayout()
        # Secondary "Not sure" (IDK) affordance beneath the choices, distinct
        # from the choices themselves: an honest opt-out for a student who would
        # otherwise guess. Not a choice submission and not scored (see
        # _on_not_sure). Left-aligned so the primary Confirm stays on the right.
        self.not_sure_button = QPushButton("Not sure")
        self.not_sure_button.setMinimumWidth(120)
        self.not_sure_button.setToolTip(
            "Don't guess — reveal the answer without a right/wrong score."
        )
        qconnect(self.not_sure_button.clicked, self._on_not_sure)
        submit_row.addWidget(self.not_sure_button)
        # Circled-info (ⓘ) indicator mirroring the dashboard score cards: the
        # detailed "what this does" explanation lives on hover here rather than
        # as always-on inline text next to the button. Honest, softened wording
        # (no "not a reasoning error" claim). Implemented as a flat QToolButton
        # rather than a bare QLabel: a real interactive control reliably delivers
        # QEvent.ToolTip on hover (a decorative QLabel's tooltip delivery is
        # flaky for a single narrow glyph in this embedded layout), and clicking
        # it also surfaces the same copy via QToolTip.showText — belt-and-
        # suspenders so the explanation is always reachable even if hover is
        # finicky.
        self._not_sure_info_text = (
            "Mark this as a knowledge gap to review. "
            "It doesn't count for or against your accuracy."
        )
        self.not_sure_info = QToolButton()
        self.not_sure_info.setObjectName("mcatNotSureInfo")
        self.not_sure_info.setText("\u24d8")
        self.not_sure_info.setAutoRaise(True)
        self.not_sure_info.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.not_sure_info.setToolTip(self._not_sure_info_text)
        self.not_sure_info.setCursor(Qt.CursorShape.WhatsThisCursor)
        qconnect(self.not_sure_info.clicked, self._on_not_sure_info_clicked)
        submit_row.addWidget(self.not_sure_info)
        submit_row.addStretch()
        self.submit_button = QPushButton("Confirm answer")
        self.submit_button.setEnabled(False)
        self.submit_button.setMinimumWidth(160)
        qconnect(self.submit_button.clicked, self._on_submit)
        submit_row.addWidget(self.submit_button)
        root.addLayout(submit_row)

        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        root.addWidget(self.feedback)

        # Content re-check probe (SCIENCE miss, before explanation).
        self.probe_frame = QFrame()
        self.probe_frame.setObjectName("mcatProbeFrame")
        probe_layout = QVBoxLayout(self.probe_frame)
        probe_layout.setContentsMargins(10, 8, 10, 10)
        probe_layout.setSpacing(8)

        probe_title = QLabel("Quick check — recall before the explanation")
        probe_title.setStyleSheet("font-weight: 700; border: none; background: transparent;")
        probe_layout.addWidget(probe_title)

        self.probe_prompt = QLabel()
        self.probe_prompt.setWordWrap(True)
        self.probe_prompt.setTextFormat(Qt.TextFormat.RichText)
        self.probe_prompt.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        probe_layout.addWidget(self.probe_prompt)

        # Gated recall: the blanked prompt shows first with ONLY the "Show
        # answer" button. The concise answer below and the self-grade buttons
        # stay hidden until that click, so recall happens before the student
        # sees the answer. The reveal shows just the concise answer (the filled
        # cloze / backing card back) — NEVER the full explanation blurb (that
        # still surfaces later via the normal reveal flow).
        self.probe_answer_label = QLabel()
        self.probe_answer_label.setWordWrap(True)
        self.probe_answer_label.setTextFormat(Qt.TextFormat.RichText)
        self.probe_answer_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.probe_answer_label.setVisible(False)
        probe_layout.addWidget(self.probe_answer_label)

        # Pre-reveal control: a single "Show answer" button (no answer text and
        # no grade buttons until it is clicked).
        self.probe_reveal_row = QHBoxLayout()
        self.probe_reveal_button = QPushButton("Show answer")
        qconnect(self.probe_reveal_button.clicked, self._on_probe_reveal)
        self.probe_reveal_row.addWidget(self.probe_reveal_button)
        self.probe_reveal_row.addStretch()
        probe_layout.addLayout(self.probe_reveal_row)

        # Post-reveal: the two self-grade buttons (hidden until Show answer).
        self.probe_row = QHBoxLayout()
        self.probe_knew_button = QPushButton("I knew it")
        qconnect(self.probe_knew_button.clicked, lambda: self._on_probe_grade(True))
        self.probe_row.addWidget(self.probe_knew_button)
        self.probe_missed_button = QPushButton("I didn't know it")
        qconnect(
            self.probe_missed_button.clicked, lambda: self._on_probe_grade(False)
        )
        self.probe_row.addWidget(self.probe_missed_button)
        self.probe_row.addStretch()
        probe_layout.addLayout(self.probe_row)
        self.probe_frame.setVisible(False)
        root.addWidget(self.probe_frame)

        # Per-choice static feedback (NO-AI), shown inline on choices after reveal.
        self.choice_feedback_label = QLabel()
        self.choice_feedback_label.setWordWrap(True)
        self.choice_feedback_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.choice_feedback_label.setVisible(False)
        root.addWidget(self.choice_feedback_label)

        # Static (NO-AI) correct-answer explanation — after classification on miss.
        self.explanation_label = QLabel()
        self.explanation_label.setWordWrap(True)
        self.explanation_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.explanation_label.setVisible(False)
        root.addWidget(self.explanation_label)

        # Opt-in AI (post-miss only; no calls until student taps Ask more).
        self.ask_ai_button = QPushButton("Ask more (AI)")
        self.ask_ai_button.setVisible(False)
        qconnect(self.ask_ai_button.clicked, self._on_ask_ai)
        root.addWidget(self.ask_ai_button)

        # The AI assistant is a DEDICATED right-hand chat dock (its own column in
        # the body splitter — see the _build_ui assembly below), NOT part of the
        # question's vertical stack. Structure: a fixed header, a scrolling
        # conversation thread (its OWN QScrollArea so bubbles never clip and grow
        # / scroll independently of the question column), and a pinned input row
        # + flag affordance at the bottom that stay reachable at any height.
        self.ai_panel = QFrame()
        self.ai_panel.setObjectName("mcatAiPanel")
        self.ai_panel.setMinimumWidth(AI_DOCK_MIN_WIDTH)
        ai_layout = QVBoxLayout(self.ai_panel)
        ai_layout.setContentsMargins(14, 12, 14, 12)
        ai_layout.setSpacing(8)

        self.assistant_header_row = QWidget()
        assistant_header_layout = QHBoxLayout(self.assistant_header_row)
        assistant_header_layout.setContentsMargins(0, 0, 0, 0)
        assistant_header_layout.setSpacing(8)
        assistant_titles = QWidget()
        assistant_titles_layout = QVBoxLayout(assistant_titles)
        assistant_titles_layout.setContentsMargins(0, 0, 0, 0)
        assistant_titles_layout.setSpacing(1)
        self.assistant_header_label = QLabel("✨ Assistant")
        assistant_titles_layout.addWidget(self.assistant_header_label)
        self.assistant_sub_label = QLabel(
            "Optional AI help · grounded in the cited source"
        )
        self.assistant_sub_label.setWordWrap(True)
        assistant_titles_layout.addWidget(self.assistant_sub_label)
        assistant_header_layout.addWidget(assistant_titles, stretch=1)
        # Runtime AI on/off toggle, reachable right where the AI is used. Backed
        # by the SAME collection config as the Tools-menu action (see
        # ai_bridge.ai_toggle_enabled); flipping it takes effect on the next
        # explanation/follow-up with no restart.
        self.ai_toggle_button = QPushButton()
        self.ai_toggle_button.setCheckable(True)
        self.ai_toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ai_toggle_button.setToolTip(
            "Turn the AI assistant on or off. When off, explanations and "
            "follow-ups use the offline, source-grounded fallback — no restart."
        )
        qconnect(self.ai_toggle_button.clicked, self._on_toggle_ai_enabled)
        assistant_header_layout.addWidget(
            self.ai_toggle_button, alignment=Qt.AlignmentFlag.AlignTop
        )
        ai_layout.addWidget(self.assistant_header_row)

        self.assistant_divider = QFrame()
        self.assistant_divider.setFrameShape(QFrame.Shape.HLine)
        self.assistant_divider.setFixedHeight(1)
        ai_layout.addWidget(self.assistant_divider)

        # Scrolling conversation thread: the explainer + follow-up bubbles + the
        # starter chips live inside their OWN widget-resizable QScrollArea, so a
        # long back-and-forth scrolls within the dock (new messages auto-scroll
        # to the bottom) and can NEVER clip against the pinned input row below.
        self.ai_scroll = QScrollArea()
        self.ai_scroll.setWidgetResizable(True)
        self.ai_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.ai_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.ai_thread_content = QWidget()
        thread_layout = QVBoxLayout(self.ai_thread_content)
        thread_layout.setContentsMargins(0, 0, 0, 0)
        thread_layout.setSpacing(8)

        # Opening explainer — the assistant's first "message" in the thread.
        self.ai_explainer_label = QLabel()
        self.ai_explainer_label.setWordWrap(True)
        self.ai_explainer_label.setTextFormat(Qt.TextFormat.RichText)
        self.ai_explainer_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        thread_layout.addWidget(self.ai_explainer_label)

        # Threaded follow-up Q&A: each Ask appends a "You" bubble + an
        # "Assistant" bubble here (stacked, room to grow) instead of overwriting
        # a single one-shot answer box.
        self.conversation_container = QWidget()
        self.conversation_layout = QVBoxLayout(self.conversation_container)
        self.conversation_layout.setContentsMargins(0, 0, 0, 0)
        self.conversation_layout.setSpacing(4)
        # A plain QWidget's size policy does NOT enable heightForWidth, so its
        # parent layout would size it to a single-line hint and clip the wrapped
        # bubbles. Enable heightForWidth (vertical Minimum) so the container's
        # height tracks the stacked bubbles and grows/scrolls instead of
        # clipping. Hidden until the first follow-up so no empty labeled box
        # shows before anything is asked.
        conv_sp = self.conversation_container.sizePolicy()
        conv_sp.setHeightForWidth(True)
        conv_sp.setVerticalPolicy(QSizePolicy.Policy.Minimum)
        self.conversation_container.setSizePolicy(conv_sp)
        self.conversation_container.setVisible(False)
        self._conversation_bubbles: list[tuple[str, QLabel]] = []
        thread_layout.addWidget(self.conversation_container)

        # Starter question chips: one-tap pre-fill of the follow-up box (the
        # student can still edit before sending). Built PER QUESTION from the
        # item's metadata (see ``_starter_questions_for``); NO runtime LLM call.
        self.starter_hint_label = QLabel("Try asking")
        thread_layout.addWidget(self.starter_hint_label)

        self.starter_chips_container = QWidget()
        # Keep a ref: this custom QLayout overrides virtuals, so the Python
        # wrapper must outlive _build_ui for wrapping/height to keep working.
        self._starter_flow = _FlowLayout(self.starter_chips_container, spacing=6)
        self.starter_chips: list[QPushButton] = []
        sp = self.starter_chips_container.sizePolicy()
        sp.setHeightForWidth(True)
        self.starter_chips_container.setSizePolicy(sp)
        thread_layout.addWidget(self.starter_chips_container)

        # Absorb slack so the thread stays TOP-anchored (messages read top-down)
        # instead of floating in the vertical middle of the dock.
        thread_layout.addStretch(1)
        self.ai_scroll.setWidget(self.ai_thread_content)
        ai_layout.addWidget(self.ai_scroll, stretch=1)

        follow_row = QHBoxLayout()
        self.followup_input = QLineEdit()
        self.followup_input.setPlaceholderText(
            "Ask a follow-up (e.g. Why isn't my choice right?)"
        )
        qconnect(self.followup_input.returnPressed, self._on_followup_ask)
        follow_row.addWidget(self.followup_input, stretch=1)
        # OS-native speech-to-text: focuses the field and triggers the built-in
        # dictation (Win+H on Windows) — no new deps, no cloud calls. Degrades to
        # a focus + hint tooltip when OS dictation can't be launched.
        self.mic_button = QPushButton()
        self.mic_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic_button.setToolTip(_os_dictation_hint())
        self._apply_mic_icon(_theme_tokens())
        qconnect(self.mic_button.clicked, self._on_mic_dictate)
        follow_row.addWidget(self.mic_button)
        self.followup_button = QPushButton("Ask")
        qconnect(self.followup_button.clicked, self._on_followup_ask)
        follow_row.addWidget(self.followup_button)
        ai_layout.addLayout(follow_row)

        # Observability affordance: a small, link-style "Report incorrect" that
        # is shown ONLY once an AI answer is on screen (the AI-off path never
        # builds it into view). Tapping it reveals a compact one-row form.
        self.flag_link = QPushButton("⚑ Report incorrect")
        self.flag_link.setFlat(True)
        self.flag_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.flag_link.setVisible(False)
        qconnect(self.flag_link.clicked, self._on_flag_ai)
        ai_layout.addWidget(
            self.flag_link, alignment=Qt.AlignmentFlag.AlignRight
        )

        self.flag_form = QFrame()
        flag_layout = QHBoxLayout(self.flag_form)
        flag_layout.setContentsMargins(0, 2, 0, 2)
        flag_layout.setSpacing(6)
        self.flag_reason_combo = QComboBox()
        for _key, _label in AI_FLAG_REASONS:
            self.flag_reason_combo.addItem(_label, _key)
        flag_layout.addWidget(self.flag_reason_combo)
        self.flag_note_input = QLineEdit()
        self.flag_note_input.setPlaceholderText("Add a note (optional)")
        flag_layout.addWidget(self.flag_note_input, stretch=1)
        self.flag_submit_button = QPushButton("Send")
        qconnect(self.flag_submit_button.clicked, self._on_submit_flag)
        flag_layout.addWidget(self.flag_submit_button)
        self.flag_cancel_button = QPushButton("Cancel")
        self.flag_cancel_button.setFlat(True)
        qconnect(self.flag_cancel_button.clicked, self._on_cancel_flag)
        flag_layout.addWidget(self.flag_cancel_button)
        self.flag_form.setVisible(False)
        ai_layout.addWidget(self.flag_form)

        # Hidden until the student opens the assistant; as a splitter pane this
        # collapses the dock to 0 width (question column takes the full body).
        self.ai_panel.setVisible(False)

        # v2 diagnosis panel (miss flow; hidden until probe done).
        self.miss_frame = QFrame()
        self.miss_frame.setMinimumHeight(80)
        miss_layout = QVBoxLayout(self.miss_frame)
        miss_layout.setContentsMargins(12, 10, 12, 10)
        miss_layout.setSpacing(8)

        self.diag_label = QLabel()
        self.diag_label.setWordWrap(True)
        self.diag_label.setTextFormat(Qt.TextFormat.RichText)
        miss_layout.addWidget(self.diag_label)

        self.diag_hint_label = QLabel()
        self.diag_hint_label.setWordWrap(True)
        miss_layout.addWidget(self.diag_hint_label)

        self.misconception_label = QLabel()
        self.misconception_label.setWordWrap(True)
        miss_layout.addWidget(self.misconception_label)

        self.diag_row = QHBoxLayout()
        self.confirm_button = QPushButton("Confirm")
        qconnect(self.confirm_button.clicked, self._on_confirm)
        self.diag_row.addWidget(self.confirm_button)
        self.override_toggle = QPushButton("Actually, something else ▾")
        self.override_toggle.setFlat(True)
        qconnect(self.override_toggle.clicked, self._on_toggle_override)
        self.diag_row.addWidget(self.override_toggle)
        self.diag_row.addStretch()
        miss_layout.addLayout(self.diag_row)

        self.error_label = QLabel("What kind of error was this?")
        self.error_label.setStyleSheet("border: none; background: transparent;")
        miss_layout.addWidget(self.error_label)
        self.error_row = QHBoxLayout()
        miss_layout.addLayout(self.error_row)
        self.error_buttons: list[QPushButton] = []
        self.error_button_ids: list[str] = []
        for err_id in INFERRED_SCIENCE_TYPES:
            b = QPushButton(INFERRED_DISPLAY_LABELS.get(err_id, err_id))
            b.setMinimumHeight(32)
            qconnect(
                b.clicked, lambda _=False, e=err_id: self._on_error_type(e)
            )
            self.error_row.addWidget(b)
            self.error_buttons.append(b)
            self.error_button_ids.append(err_id)

        self.miss_frame.setVisible(False)
        root.addWidget(self.miss_frame)

        self.next_action_label = QLabel()
        self.next_action_label.setWordWrap(True)
        self.next_action_label.setVisible(False)
        root.addWidget(self.next_action_label)

        self.practice_row = QHBoxLayout()
        self.practice_button = QPushButton()
        qconnect(self.practice_button.clicked, self._on_launch_practice)
        self.practice_row.addWidget(self.practice_button)
        self.practice_row.addStretch()
        root.addLayout(self.practice_row)
        self.practice_button.setVisible(False)

        session_layout.addWidget(self.content_card)
        stack_layout.addWidget(self.session_page)

        self.remediation_slot = QWidget()
        self.remediation_slot.setVisible(False)
        self.remediation_slot.setMaximumWidth(880)
        self.remediation_layout = QVBoxLayout(self.remediation_slot)
        self.remediation_layout.setContentsMargins(0, 0, 0, 0)
        stack_layout.addWidget(self.remediation_slot)

        content_outer.addWidget(self.content_stack)
        # Absorb leftover vertical space BELOW the content so the card stays
        # top-anchored instead of being centered/floated in the viewport.
        content_outer.addStretch(1)
        self.scroll.setWidget(self.scroll_content)

        # Body = question/passage column (left, scrolls) + assistant chat dock
        # (right, its own scroll). A horizontal splitter lets the student drag
        # the divider to trade space between them. The dock starts collapsed and
        # is revealed on demand (see _show_ai_dock), so a fresh question shows
        # the full-width question column with nothing clipped.
        self.body_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.body_splitter.setObjectName("mcatPerfBody")
        self.body_splitter.setChildrenCollapsible(True)
        self.body_splitter.addWidget(self.scroll)
        self.body_splitter.addWidget(self.ai_panel)
        # Left column takes new space on resize; the dock keeps its set width.
        self.body_splitter.setStretchFactor(0, 1)
        self.body_splitter.setStretchFactor(1, 0)
        outer.addWidget(self.body_splitter, stretch=1)

        self.footer_bar = QFrame()
        self.footer_bar.setObjectName("mcatPerfFooter")
        footer_layout = QHBoxLayout(self.footer_bar)
        footer_layout.setContentsMargins(16, 10, 16, 10)
        self.score_label = QLabel()
        footer_layout.addWidget(self.score_label)
        footer_layout.addStretch()
        self.next_button = QPushButton("Next")
        self.next_button.setToolTip(
            "Continue to the next question after you confirm your answer "
            "and review any feedback."
        )
        qconnect(self.next_button.clicked, self._on_next)
        footer_layout.addWidget(self.next_button)
        outer.addWidget(self.footer_bar)

    def _confirm_exit(self) -> None:
        """Leave performance mode and return to the main Anki shell."""
        if self.session.awaiting_error_type:
            tooltip(
                "Finish the error check for this question before exiting.",
                parent=self,
            )
            return
        self._finish_session()

    def _finish_session(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass
        if self._on_close:
            self._on_close()

    # Question lifecycle ----------------------------------------------------

    def _clear_choices(self) -> None:
        while self.choices_box.count():
            item = self.choices_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _set_self_report_visible(
        self, visible: bool, exclude: Optional[str] = None
    ) -> None:
        """Show/hide the frozen self-report enum (override / low-conf fallback).

        When ``exclude`` is given (the confident-miss override case), the button
        for that already-suggested type is hidden — confirming the suggestion is
        handled by the Confirm button, so the override lists only the OTHER
        types. The low-confidence fallback passes ``exclude=None`` and shows all.
        """
        self.error_label.setVisible(visible)
        for b, err_id in zip(self.error_buttons, self.error_button_ids):
            b.setVisible(visible and err_id != exclude)

    def _hide_probe(self) -> None:
        """Hide the whole re-check probe UI."""
        self.probe_frame.setVisible(False)
        self.probe_prompt.setVisible(False)
        self.probe_answer_label.setVisible(False)
        self.probe_reveal_button.setVisible(False)
        self.probe_knew_button.setVisible(False)
        self.probe_missed_button.setVisible(False)

    def _hide_diagnosis(self) -> None:
        """Hide the whole miss UI (probe, diagnosis, next-action)."""
        self._hide_probe()
        self.miss_frame.setVisible(False)
        self.diag_label.setVisible(False)
        self.diag_hint_label.setVisible(False)
        self.misconception_label.setVisible(False)
        self.confirm_button.setVisible(False)
        self.override_toggle.setVisible(False)
        self._set_self_report_visible(False)
        self._hide_next_action()

    def _hide_reveal(self) -> None:
        """Hide post-answer explanation / choice feedback until reveal time."""
        self.explanation_label.setVisible(False)
        self.choice_feedback_label.setVisible(False)
        self._hide_ai_panel()
        self._revealed = False

    def _hide_ai_panel(self) -> None:
        """Hide opt-in AI UI (no network until student taps Ask more)."""
        # Invalidate any in-flight AI request so its late callback becomes a
        # no-op (e.g. the student advanced to the next question mid-fetch).
        self._ai_generation += 1
        self.ask_ai_button.setVisible(False)
        self.ask_ai_button.setText("Ask more (AI)")
        self.ai_panel.setVisible(False)
        self.ai_explainer_label.clear()
        self.followup_input.clear()
        self.followup_input.setEnabled(True)
        self.followup_button.setText("Ask")
        self._clear_conversation()
        self._ai_panel_visible = False
        self.ask_ai_button.setEnabled(True)
        self.followup_button.setEnabled(True)
        # Reset the observability flag affordance for the next question.
        self.flag_form.setVisible(False)
        self.flag_link.setVisible(False)
        self.flag_note_input.clear()
        self._last_ai_text = None
        self._last_ai_source = None
        self._last_ai_kind = None

    def _show_ai_dock(self) -> None:
        """Reveal the right-hand assistant dock with a sensible default width.

        The dock lives in the body splitter and collapses to 0 when hidden; on
        (re)open we give it ~``AI_DOCK_DEFAULT_WIDTH`` — bounded so it never
        dominates a narrow window and never starves the question column — while
        leaving the student free to drag the splitter afterward.
        """
        self.ai_panel.setVisible(True)
        sizes = self.body_splitter.sizes()
        if len(sizes) == 2 and sizes[1] < AI_DOCK_MIN_WIDTH:
            total = sum(sizes) or max(self.width(), 640)
            dock = min(AI_DOCK_DEFAULT_WIDTH, max(AI_DOCK_MIN_WIDTH, (total * 2) // 5))
            self.body_splitter.setSizes([max(total - dock, 320), dock])

    def _scroll_ai_to_bottom(self) -> None:
        """Auto-scroll the assistant thread to the newest message.

        Deferred to the next event-loop turn so the just-added bubble has been
        laid out (its wrapped height is known) before we jump to the bottom.
        """
        scroll = getattr(self, "ai_scroll", None)
        if scroll is None:
            return

        def _to_bottom() -> None:
            try:
                if sip.isdeleted(self) or sip.isdeleted(scroll):
                    return
            except Exception:
                return
            bar = scroll.verticalScrollBar()
            bar.setValue(bar.maximum())

        QTimer.singleShot(0, _to_bottom)

    def _hide_next_action(self) -> None:
        """Hide the application next-action panel + clear any pending set."""
        self.next_action_label.setVisible(False)
        self.practice_button.setVisible(False)
        self._practice_items = []

    def _update_progress(self) -> None:
        total = max(self.session.total, 1)
        done = self.session.index
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    def _update_choice_styles(self) -> None:
        for i, b in enumerate(self.choice_buttons):
            if not b.isEnabled():
                continue
            b.setStyleSheet(
                self._choice_styles["selected"]
                if i == self._selected_idx
                else self._choice_styles["default"]
            )

    def _show_question(self) -> None:
        q = self.session.current
        self._shown_at = time.time()

        if self.interleaved:
            self.header.setText(
                f"Question {self.session.index + 1} of {self.session.total}"
            )
        else:
            self.header.setText(
                f"Question {self.session.index + 1} of {self.session.total}  "
                f"·  {q['section']} · {q['topic_id']}"
            )
        self._update_progress()
        src = q.get("source_name") or ""
        loc = q.get("source_location") or ""
        self.source.setText(f"{src} — {loc}".strip(" —"))
        self.stem.setText(q["stem"])

        self._clear_choices()
        self.choice_buttons = []
        self.choice_fb_labels = []
        for i, choice in enumerate(q["choices"]):
            # Each choice is a button PLUS a dedicated wrapping feedback label
            # beneath it. The label (not the button text) carries the per-choice
            # feedback at reveal, so long feedback wraps and stays fully visible
            # instead of being squished/clipped inside a fixed-height button.
            row = QWidget()
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(0)
            b = QPushButton(f"{LETTERS[i]}.  {choice}")
            b.setStyleSheet(self._choice_styles["default"])
            # Vertical policy Minimum: the button may GROW to fit taller/wrapped
            # text but is never squeezed below the hard ``setMinimumHeight`` floor
            # below, so re-styling after grading (and the added ✓/✗ mark) — and
            # the splitter/scroll-area reallocating vertical space — can't compress
            # or clip the row. Text stays fully visible in every state.
            b.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
            )
            # Hard, layout-proof minimum height computed from the button's own
            # font metrics: one text line + vertical padding (12+12) + border
            # (2+2) + a little slack for ascenders/descenders and the grade mark.
            # A widget-level minimum outranks any layout stretch/compression, so
            # rows can no longer intermittently shrink under pressure.
            fm = b.fontMetrics()
            b.setMinimumHeight(
                max(CHOICE_MIN_CONTENT_HEIGHT + 28, fm.height() + 24 + 4 + 8)
            )
            qconnect(b.clicked, lambda _=False, idx=i: self._on_choice_select(idx))
            row_layout.addWidget(b)
            # The row wrapper must not donate height either: Minimum vertical
            # policy means it reports its content (button + optional feedback
            # label) as the floor and can grow, but never collapses below it.
            row.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
            )
            fb = QLabel()
            fb.setWordWrap(True)
            fb.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            fb.setVisible(False)
            row_layout.addWidget(fb)
            self.choices_box.addWidget(row)
            self.choice_buttons.append(b)
            self.choice_fb_labels.append(fb)

        self.feedback.setText("")
        self.feedback.setStyleSheet(
            "margin-top: 4px; background: transparent; border: none;"
        )
        self._hide_reveal()
        self._hide_diagnosis()
        self._chosen_idx = None
        self._selected_idx = None
        self._first_choice_idx = None
        self._answer_changes = 0
        self._submitted = False
        self._inferred_type = None
        self._probe_target = None
        self.submit_button.setVisible(True)
        self.submit_button.setEnabled(False)
        # "Not sure" is available immediately (no choice selection required).
        self.not_sure_button.setVisible(True)
        self.not_sure_button.setEnabled(True)
        self.not_sure_info.setVisible(True)
        self.next_button.setEnabled(False)
        self._update_score_label()

    def _on_choice_select(self, idx: int) -> None:
        """Highlight a choice; grading waits for Confirm."""
        if self._submitted or self.session.awaiting_error_type:
            return
        if self._first_choice_idx is None:
            self._first_choice_idx = idx
        elif idx != self._selected_idx:
            self._answer_changes += 1
        self._selected_idx = idx
        self.submit_button.setEnabled(True)
        self._update_choice_styles()

    def _on_submit(self) -> None:
        if self._submitted or self._selected_idx is None:
            return
        if self.session.awaiting_error_type:
            return

        self._submitted = True
        self.submit_button.setEnabled(False)
        self.submit_button.setVisible(False)
        # Submitting a choice supersedes the "Not sure" opt-out.
        self.not_sure_button.setEnabled(False)
        self.not_sure_button.setVisible(False)
        self.not_sure_info.setVisible(False)
        for b in self.choice_buttons:
            b.setEnabled(False)

        idx = self._selected_idx
        self._chosen_idx = idx
        elapsed = time.time() - self._shown_at

        answer_kwargs: dict[str, Any] = {}
        if self._first_choice_idx is not None:
            answer_kwargs["first_choice_index"] = self._first_choice_idx
        if self._answer_changes:
            answer_kwargs["answer_changes"] = self._answer_changes

        correct = self.session.answer(idx, time_seconds=elapsed, **answer_kwargs)
        if correct:
            self._show_reveal(correct=True)
            self.next_button.setEnabled(True)
        else:
            # Immediate, unmistakable WRONG indicator on the chosen option, the
            # moment they confirm — before the quick-check probe / diagnosis.
            self._mark_choice_wrong(idx)
            if self.session.current_is_cars:
                # CARS runs a SEPARATE diagnosis track: no re-check probe and no
                # science 3-bucket error-type confirm. Resolve the miss and go
                # straight to the reveal so the session proceeds normally.
                self._resolve_cars_miss()
            else:
                t = _theme_tokens()
                self.feedback.setText(
                    f"{_MARK_WRONG} Not quite — quick check and error type first, "
                    "then the explanation."
                )
                self.feedback.setStyleSheet(
                    f"color: {t['accent_red']}; font-weight: 600; "
                    f"font-style: italic; margin-top: 8px; background: transparent; "
                    f"border: none;"
                )
                self._probe_target = self.session.probe_target()
                if self._probe_target is not None:
                    self._start_probe()
                else:
                    self._show_diagnosis()
        self._update_score_label()

    def _resolve_cars_miss(self) -> None:
        """Resolve a CARS miss WITHOUT the science error-type step.

        CARS uses a separate diagnosis track, so there is no re-check probe and
        no 3-bucket self-report: log the miss (unresolved) and go straight to
        the normal reveal + Next. ``_show_reveal`` overwrites the interim
        feedback with the correct-answer line, and the opt-in AI assistant is
        still offered, so the post-miss flow matches non-CARS minus the skipped
        error-type confirm.
        """
        self._hide_probe()
        self._hide_diagnosis()
        self.session.classify_cars_miss()
        self._show_reveal(correct=False)
        self.next_button.setEnabled(True)

    def _on_not_sure_info_clicked(self) -> None:
        """Show the ⓘ explanation on click as well as hover.

        Belt-and-suspenders next to the hover tooltip: some environments make a
        hover tooltip finicky, so a tap on the ⓘ surfaces the exact same honest
        copy right at the cursor. Does NOT trigger the IDK opt-out — this is a
        pure explanatory affordance.
        """
        QToolTip.showText(
            QCursor.pos(), self._not_sure_info_text, self.not_sure_info
        )

    def _on_not_sure(self) -> None:
        """Handle the "Not sure" (IDK) opt-out.

        This is NOT a choice submission and is NOT scored right or wrong. Log the
        abstention (non-CARS → content_gap route so the dashboard focus area
        picks it up; CARS → unresolved), then reveal the correct answer +
        explanation as a learning moment and let the student advance like a
        normal post-answer state (the "Ask more" AI panel stays available).
        Guarded against a double-fire exactly like _on_submit.
        """
        if self._submitted or self.session.awaiting_error_type:
            return
        self._submitted = True
        self.submit_button.setEnabled(False)
        self.submit_button.setVisible(False)
        self.not_sure_button.setEnabled(False)
        self.not_sure_button.setVisible(False)
        self.not_sure_info.setVisible(False)
        for b in self.choice_buttons:
            b.setEnabled(False)
        # No chosen option: leave _chosen_idx None so the reveal marks only the
        # correct answer (no distractor flagged wrong). Session logs the IDK,
        # mapping non-CARS straight to content_gap (no probe / no confirm step).
        self.session.not_sure()
        self._show_reveal(correct=False, idk=True)
        self.next_button.setEnabled(True)
        self._update_score_label()

    def _show_reveal(self, *, correct: bool, idk: bool = False) -> None:
        """Show correct/incorrect, choice_feedback, and explanation.

        ``idk=True`` renders a neutral "Not sure — not scored" banner (still a
        learning moment) instead of the ✗ "Incorrect" line, since an abstention
        is neither right nor wrong.
        """
        if self._revealed:
            return
        self._revealed = True
        q = self.session.current
        t = _theme_tokens()
        if idk:
            correct_idx = self.session.correct_index()
            # Brief confirmation only — the "what this does" detail (knowledge
            # gap / not counted for or against accuracy) now lives in the ⓘ
            # tooltip next to the button, so this line stays lightweight.
            self.feedback.setText(
                f"Not sure — not scored. "
                f"Correct answer: {q['correct']}. {q['choices'][correct_idx]}"
            )
            self.feedback.setStyleSheet(
                f"color: {t['fg_subtle']}; font-weight: 600; margin-top: 8px; "
                f"background: transparent; border: none;"
            )
        elif correct:
            self.feedback.setText("✓ Correct")
            self.feedback.setStyleSheet(
                f"color: {t['accent_green']}; font-weight: bold; margin-top: 8px; "
                f"background: transparent; border: none;"
            )
        else:
            correct_idx = self.session.correct_index()
            self.feedback.setText(
                f"✗ Incorrect. Correct answer: {q['correct']}. "
                f"{q['choices'][correct_idx]}"
            )
            self.feedback.setStyleSheet(
                f"color: {t['accent_red']}; font-weight: bold; margin-top: 8px; "
                f"background: transparent; border: none;"
            )
        self._show_choice_feedback()
        self._show_explanation()
        if not correct:
            self.ask_ai_button.setVisible(True)

    # AI request threading (off the GUI thread) --------------------------------

    def _begin_ai_request(self) -> int:
        """Start a new AI request generation and return its id.

        Bumping the counter invalidates any request already in flight, so only
        the newest request's callback is allowed to touch the UI.
        """
        self._ai_generation += 1
        return self._ai_generation

    def _ai_request_stale(self, gen: int) -> bool:
        """True when a background AI callback must NOT touch the UI.

        Stale if the request was superseded (newer Ask/follow-up, question
        change, or panel reset — all bump the generation) or if the view itself
        has been destroyed (performance mode closed mid-fetch).
        """
        if gen != self._ai_generation:
            return True
        try:
            return bool(sip.isdeleted(self))
        except Exception:
            return False

    @staticmethod
    def _future_result(fut: Future) -> tuple[Optional[str], str]:
        """Unwrap a background AI future; a raised error (incl. timeout) becomes
        ``(None, "error")`` so the finish handler shows an honest message rather
        than letting the exception surface as an unhandled crash."""
        try:
            text, tag = fut.result()
            return text, tag
        except Exception as exc:
            detail = f"{exc.__class__.__name__} {exc}".lower()
            if "timeout" in detail or "timed out" in detail:
                return None, "timeout"
            return None, "error"

    def _reset_ask_ai_button(self) -> None:
        """Clear the 'Thinking…' loader and restore the Ask-more entry point."""
        self.ask_ai_button.setEnabled(True)
        self.ask_ai_button.setText("Ask more (AI)")
        self.ask_ai_button.setVisible(True)

    # AI on/off toggle (panel header) -----------------------------------------

    @staticmethod
    def _ai_toggle_style(t: dict[str, str], on: bool) -> str:
        """Small pill styling for the header AI on/off control."""
        if on:
            return (
                f"QPushButton {{ color: {t['accent']}; background: transparent; "
                f"border: 1.5px solid {t['accent']}; border-radius: 999px; "
                f"padding: 2px 10px; font-size: 11px; font-weight: 700; }}"
                f"QPushButton:hover {{ background: {t['ai_border']}; }}"
            )
        return (
            f"QPushButton {{ color: {t['fg_subtle']}; background: transparent; "
            f"border: 1.5px solid {t['border']}; border-radius: 999px; "
            f"padding: 2px 10px; font-size: 11px; font-weight: 700; }}"
            f"QPushButton:hover {{ color: {t['accent']}; "
            f"border-color: {t['accent']}; }}"
        )

    def sync_ai_toggle_display(self) -> None:
        """Reflect the AI on/off state HONESTLY in the header control.

        The pill shows "AI: On" ONLY when the user toggle is on AND a live
        backend (hosted proxy or direct provider) is actually configured — so it
        never claims "On" while the panel is really serving the offline
        source-based fallback. Three states:
          * toggle off              -> "AI: Off"        (muted)
          * on + backend configured -> "AI: On"         (accent)
          * on + NOT configured     -> "AI: Not set up" (muted, honest)
        The checkbox still mirrors the user's *preference*; only the label +
        style reflect effective availability. Called on build, theme change, and
        whenever either toggle entry point flips the setting.
        """
        if not hasattr(self, "ai_toggle_button"):
            return
        from aqt.mcat.ai_bridge import ai_provider_configured, ai_toggle_enabled

        pref_on = ai_toggle_enabled()
        # Cheap, network-free config-presence check (see ai_bridge). Guarded so a
        # bridge hiccup can never break the header render.
        try:
            configured = ai_provider_configured()
        except Exception:
            configured = False
        effective_on = pref_on and configured

        self.ai_toggle_button.blockSignals(True)
        self.ai_toggle_button.setChecked(pref_on)
        self.ai_toggle_button.blockSignals(False)
        if not pref_on:
            label = "AI: Off"
        elif configured:
            label = "AI: On"
        else:
            label = "AI: Not set up"
        self.ai_toggle_button.setText(label)
        self.ai_toggle_button.setStyleSheet(
            self._ai_toggle_style(_theme_tokens(), effective_on)
        )
        if hasattr(self, "assistant_sub_label"):
            if effective_on:
                sub = "Live AI help · grounded in the cited source"
            elif pref_on:
                sub = "AI not set up · offline source-based help"
            else:
                sub = "AI off · offline source-based help"
            self.assistant_sub_label.setText(sub)

    def _on_toggle_ai_enabled(self) -> None:
        """Persist the runtime AI override and keep the Tools-menu action synced.

        Takes effect immediately: the next Ask/follow-up consults the toggle at
        the ai_bridge choke point, so OFF serves the static fallback with no
        restart. Does not tear down any answer already on screen.
        """
        from aqt.mcat.ai_bridge import set_ai_toggle_enabled

        enabled = self.ai_toggle_button.isChecked()
        set_ai_toggle_enabled(enabled)
        self.sync_ai_toggle_display()
        act = getattr(self.mw, "_mcatAiToggleAction", None)
        if act is not None:
            try:
                act.setChecked(enabled)
            except Exception:
                pass
        tooltip(
            "AI assistant on."
            if enabled
            else "AI assistant off — using offline, source-grounded "
            "explanations.",
            parent=self,
        )

    def _on_ask_ai(self) -> None:
        """Opt-in: fetch per-choice AI explainer (live LLM when configured).

        The fetch may hit the network, so it runs OFF the GUI thread via the
        task manager: the event loop stays responsive and the "Thinking…" state
        can never freeze. The result is applied back on the main thread and only
        when this request is still current (generation guard) and the view is
        still alive — a late/superseded reply is dropped, not rendered.
        """
        if self._chosen_idx is None or self._ai_panel_visible:
            return
        from aqt.mcat.ai_bridge import ai_available, fetch_ai_explanation

        self.ask_ai_button.setEnabled(False)
        self.ask_ai_button.setText("Thinking…")
        # ai_available() is a cheap, network-free config check (the SDKs are
        # imported lazily only when a call actually runs), so it stays inline.
        # When AI is OFF/not configured there is no network to hit: render the
        # static, source-grounded explanation immediately (with an honest note)
        # rather than dead-ending on a tooltip. The panel is never left empty.
        if not ai_available():
            self._finish_ask_ai(None, "off")
            return

        q = self.session.current
        chosen_idx = self._chosen_idx
        gen = self._begin_ai_request()

        def op() -> tuple[Optional[str], str]:
            return fetch_ai_explanation(q, chosen_idx)

        def on_done(fut: Future) -> None:
            if self._ai_request_stale(gen):
                return
            text, tag = self._future_result(fut)
            self._finish_ask_ai(text, tag)

        # uses_collection=False: this is a network/subprocess call, not a DB op,
        # so it runs on the parallel pool and never serializes behind (or blocks)
        # the single collection worker while it waits on the network.
        self.mw.taskman.run_in_background(op, on_done, uses_collection=False)

    def _finish_ask_ai(self, text: Optional[str], tag: str) -> None:
        """Apply the AI explainer result on the main thread.

        EVERY path clears the loader and renders content — the panel is NEVER
        left empty. On a live answer it renders that; on no answer (off /
        not-configured / missing SDK / timeout / transport error) it shows an
        honest one-line note and FALLS BACK to the static, source-grounded
        explanation beneath it, so the student always sees a real explanation.
        Only reached for the current, live request.
        """
        if not text:
            note = self._explainer_status_note(tag)
            static_body = self._static_explanation_text()
            self._clear_conversation()
            blocks = [
                f"<div style='font-weight:700; margin-bottom:6px; "
                f"color:{_theme_tokens()['accent']};'>{html.escape(note)}</div>"
            ]
            if static_body:
                blocks.append(self._format_ai_explainer(static_body))
            self.ai_explainer_label.setText("".join(blocks))
            # Offline starter chips are templated from the item (no LLM), so
            # they stay useful even when the live call is unavailable.
            self._rebuild_starter_chips(
                self._starter_questions_for(self.session.current, self._chosen_idx)
            )
            self.starter_hint_label.setVisible(bool(static_body))
            self.starter_chips_container.setVisible(bool(static_body))
            self._show_ai_dock()
            # Leave the panel "not owned" by a live answer so tapping Ask again
            # can retry a live call (harmless for the off/missing-SDK cases).
            self._ai_panel_visible = False
            self._reset_ask_ai_button()
            return
        self._clear_conversation()
        self.ai_explainer_label.setText(self._format_ai_explainer(text))
        self.starter_hint_label.setVisible(True)
        self.starter_chips_container.setVisible(True)
        # Question-specific starter chips, templated from THIS item's metadata
        # (offline / no LLM). Rebuilt each time the panel opens so the chips
        # always name the current distractor, correct choice, and topic.
        self._rebuild_starter_chips(
            self._starter_questions_for(self.session.current, self._chosen_idx)
        )
        self._show_ai_dock()
        self._ai_panel_visible = True
        self.ask_ai_button.setEnabled(True)
        self.ask_ai_button.setText("Ask more (AI)")
        self.ask_ai_button.setVisible(False)
        self._note_ai_output(text, tag, "explainer")

    @staticmethod
    def _explainer_status_note(tag: Optional[str]) -> str:
        """Honest one-line note shown above the static fallback explanation.

        Distinguishes *not set up* (no proxy/provider configured) from a
        *configured but failed* live call (missing SDK / timeout /
        transport-or-auth error), so the student knows why they're seeing the
        source-based explanation and whether retrying could help. The "not set
        up" case is intentionally key-agnostic: the graded build reaches AI via
        a hosted proxy (no per-user key), so it never tells a grader to add one.
        """
        if tag in ("off", "unavailable"):
            return (
                "AI assistant isn't set up on this build — showing the "
                "source-based explanation instead."
            )
        if tag == "missing_sdk":
            return (
                "AI assistant unavailable — its provider package isn't "
                "installed. Showing the source-based explanation instead."
            )
        if tag == "timeout":
            return (
                "AI assistant timed out — showing the source-based explanation "
                "instead. Tap “Ask more (AI)” to retry."
            )
        return (
            "Couldn't reach the AI assistant — showing the source-based "
            "explanation instead. Tap “Ask more (AI)” to retry."
        )

    def _static_explanation_text(self) -> str:
        """Always-available, source-grounded explanation for the current item.

        Prefers the bridge's deterministic ``static_explanation`` (grounded in
        the item + cited source); if the MCAT scripts can't be located, degrades
        to the item's own ``explanation`` text so the panel is never empty.
        """
        if self._chosen_idx is None:
            return ""
        q = self.session.current
        try:
            from aqt.mcat.ai_bridge import static_explanation

            text = static_explanation(q, self._chosen_idx)
        except Exception:
            text = None
        if text:
            return text
        expl = (q.get("explanation") or "").strip()
        if not expl:
            return ""
        correct = str(q.get("correct") or "").strip()
        head = f"Correct: {correct}" if correct else ""
        return "\n".join(p for p in (head, expl) if p)

    def _set_followup_busy(self, busy: bool) -> None:
        """Toggle the follow-up 'Thinking…' loader (input + Ask button).

        Called on both entry and completion of a follow-up, so the busy state is
        always cleared — success, failure, or timeout alike.
        """
        self.followup_button.setEnabled(not busy)
        self.followup_button.setText("Thinking…" if busy else "Ask")
        self.followup_input.setEnabled(not busy)
        if not busy:
            self.followup_input.setFocus()

    def _on_followup_ask(self) -> None:
        """Opt-in follow-up Q&A after the initial AI explainer.

        Like the explainer, the (possibly networked) answer fetch runs OFF the
        GUI thread. The input + Ask button show a bounded "Thinking…" state that
        is always cleared when the reply — or a failure/timeout — returns, and a
        superseded/late reply is dropped via the generation guard.
        """
        if self._chosen_idx is None:
            return
        # Dedupe guard: the follow-up is wired to BOTH ``returnPressed`` (Enter)
        # and the Ask button's ``clicked``. If a single Enter/tap fans out to
        # both signals, the second call must NOT append the "you" bubble again.
        # Once a send is accepted we disable the input + Ask button (see
        # ``_set_followup_busy``), so a duplicate trigger no-ops here and the
        # user's message is added EXACTLY ONCE.
        if not self.followup_input.isEnabled() or not self.followup_button.isEnabled():
            return
        question = self.followup_input.text().strip()
        if not question:
            return
        from aqt.mcat.ai_bridge import fetch_followup_answer

        # Post the student's turn immediately, then the assistant's reply below
        # it — the thread grows instead of overwriting a single answer box.
        self._append_conversation("you", question)
        self.followup_input.clear()
        self._set_followup_busy(True)

        q = self.session.current
        chosen_idx = self._chosen_idx
        gen = self._begin_ai_request()

        def op() -> tuple[Optional[str], str]:
            return fetch_followup_answer(q, chosen_idx, question)

        def on_done(fut: Future) -> None:
            if self._ai_request_stale(gen):
                return
            text, tag = self._future_result(fut)
            self._finish_followup(text, tag)

        self.mw.taskman.run_in_background(op, on_done, uses_collection=False)

    def _finish_followup(self, text: Optional[str], tag: str) -> None:
        """Append the follow-up reply (or an honest failure line) on the main
        thread and always clear the busy state. Current, live request only."""
        if text:
            self._append_conversation("assistant", text)
            # A follow-up answer is itself an AI output — flag it (not the
            # earlier explainer) so a report captures what the student reacted to.
            self._note_ai_output(text, tag, "followup")
        else:
            # Distinguish the failure states instead of one vague message.
            self._append_conversation(
                "assistant", self._followup_failure_message(tag)
            )
        self._set_followup_busy(False)

    @staticmethod
    def _followup_failure_message(tag: Optional[str]) -> str:
        """Human message for a follow-up that produced no answer, by reason."""
        if tag == "off":
            return (
                "AI isn't set up on this build — the explanation above is the "
                "offline, source-grounded fallback. Follow-ups need the AI "
                "assistant configured."
            )
        if tag == "blocked":
            return (
                "The AI couldn't answer that from the cited source, so it held "
                "back rather than guess. Review the explanation and source above."
            )
        if tag == "unavailable":
            return (
                "AI follow-ups aren't available here — the MCAT scripts weren't "
                "found next to Anki."
            )
        if tag == "timeout":
            return "Couldn't reach the assistant in time — try again."
        return (
            "Something went wrong reaching the AI. Check your connection and "
            "key, then try again."
        )

    def _on_starter_chip(self, question: str) -> None:
        """Pre-fill (not send) the follow-up box with a starter question.

        The student can edit the text before tapping Ask, so the short question
        is only a starting point — no network call happens here.
        """
        self.followup_input.setText(question)
        self.followup_input.setFocus()
        self.followup_input.end(False)

    def _apply_mic_icon(self, t: dict[str, str]) -> None:
        """Set the themed mic icon and keep the button a compact square.

        Uses the inline stroke-based SVG (themed to the AI accent); if the Qt
        SVG backend is missing, falls back to the hand-drawn mic. The button is
        sized to a small square that matches the input-row height so it reads as
        a normal icon button — never the wide/oversized emoji glyph.
        """
        icon = _mic_icon(t["accent"]) or _mic_icon_drawn(t["accent"])
        self.mic_button.setText("")
        self.mic_button.setIcon(icon)
        self.mic_button.setIconSize(QSize(_MIC_ICON_PX, _MIC_ICON_PX))
        # Square footprint sized to the input-row controls (icon + padding +
        # border), so the mic matches the "Ask" button / input height and reads
        # as a normal small icon button rather than a wide glyph.
        side = _MIC_ICON_PX + 16
        self.mic_button.setFixedSize(QSize(side, side))

    def _on_mic_dictate(self) -> None:
        """Start OS-native dictation into the follow-up box (no deps, no cloud).

        Always focuses the input first so dictated text lands there, then makes
        a best-effort attempt to open the OS voice-typing overlay (Win+H on
        Windows). If that can't be launched (other OS, disabled, or any error)
        it degrades to a one-line hint — never an error and never blocking the
        GUI thread (the hotkey is dispatched instantly via the OS input queue).
        """
        self.followup_input.setFocus()
        launched = _win_launch_dictation()
        hint = _os_dictation_hint()
        if launched:
            tooltip(f"Dictation on — speak your follow-up. {hint}", parent=self)
        else:
            tooltip(hint, parent=self)

    # AI assistant panel (formatting, chips, conversation) ------------------

    @staticmethod
    def _format_ai_explainer(text: str) -> str:
        """Render the opening explainer with the first line as a bold header.

        The bridge returns a plain multi-line body whose first line is the
        "You picked X · Correct: Y" summary; bolding it (and dropping any empty
        lines) gives the panel visual hierarchy without parsing the AI content.
        """
        lines = [ln for ln in (text or "").splitlines() if ln.strip()]
        if not lines:
            return ""
        head = html.escape(lines[0])
        rest = "<br>".join(html.escape(ln) for ln in lines[1:])
        block = (
            f"<div style='font-weight:700; margin-bottom:4px;'>{head}</div>"
        )
        if rest:
            block += f"<div style='line-height:1.5;'>{rest}</div>"
        return block

    def _clear_conversation(self) -> None:
        """Remove all follow-up Q&A bubbles (new question / panel reset)."""
        if not hasattr(self, "conversation_layout"):
            return
        while self.conversation_layout.count():
            item = self.conversation_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._conversation_bubbles = []
        if hasattr(self, "conversation_container"):
            self.conversation_container.setVisible(False)

    def _append_conversation(self, role: str, text: str) -> None:
        """Append a chat bubble (``you`` / ``assistant``) to the thread.

        Bubbles are created ON DEMAND here (never pre-created empty), and each
        one auto-sizes to its wrapped content: word wrap on, no fixed/max
        height, and a heightForWidth size policy so a long answer grows the
        bubble (and scrolls) rather than clipping to a sliver. Empty text is
        ignored so a blank labeled box never appears.
        """
        body_lines = [ln for ln in (text or "").splitlines() if ln.strip()]
        if not body_lines:
            return
        t = _theme_tokens()
        caption = "You" if role == "you" else "Assistant"
        cap_color = t["accent_blue"] if role == "you" else t["accent"]
        body = "<br>".join(html.escape(ln) for ln in body_lines)
        bubble = QLabel()
        bubble.setWordWrap(True)
        bubble.setTextFormat(Qt.TextFormat.RichText)
        bubble.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Let the bubble grow vertically to fit its wrapped content.
        bubble_sp = QSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        bubble_sp.setHeightForWidth(True)
        bubble.setSizePolicy(bubble_sp)
        bubble.setText(
            f"<div style='color:{cap_color}; font-weight:700; font-size:11px; "
            f"letter-spacing:0.04em;'>{caption.upper()}</div>"
            f"<div style='line-height:1.5;'>{body}</div>"
        )
        bubble.setStyleSheet(_bubble_style(t, role))
        self.conversation_layout.addWidget(bubble)
        self._conversation_bubbles.append((role, bubble))
        self.conversation_container.setVisible(True)
        # Keep the newest message in view as the thread grows.
        self._scroll_ai_to_bottom()

    def _rebuild_starter_chips(self, questions: list[str]) -> None:
        """Replace the starter chips with a fresh, per-question set."""
        while self._starter_flow.count():
            item = self._starter_flow.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.starter_chips = []
        chip_style = _starter_chip_style(_theme_tokens())
        for text in questions[:MAX_STARTER_CHIPS]:
            chip = QPushButton(text)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setStyleSheet(chip_style)
            qconnect(
                chip.clicked,
                lambda _=False, q=text: self._on_starter_chip(q),
            )
            self._starter_flow.addWidget(chip)
            self.starter_chips.append(chip)
        self.starter_chips_container.updateGeometry()

    @staticmethod
    def _topic_display(q: dict[str, Any]) -> str:
        """Readable topic phrase from ``topic_id`` (e.g. ``bb_glycolysis`` →
        ``glycolysis``). Drops a leading 2–3 letter section code."""
        raw = str(q.get("topic_id") or "").strip()
        if not raw:
            return "this concept"
        parts = raw.replace("-", "_").split("_")
        if len(parts) > 1 and 1 <= len(parts[0]) <= 3:
            parts = parts[1:]
        phrase = " ".join(p for p in parts if p).strip()
        return phrase or "this concept"

    @staticmethod
    def _shorten_choice(text: str, *, words: int = 6, chars: int = 42) -> str:
        """Trim a choice string to a short, chip-friendly label."""
        clean = " ".join(str(text or "").split())
        toks = clean.split(" ")
        if len(toks) > words:
            clean = " ".join(toks[:words]) + "…"
        if len(clean) > chars:
            clean = clean[: chars - 1].rstrip() + "…"
        return clean

    def _starter_questions_for(
        self, q: dict[str, Any], chosen_idx: Optional[int]
    ) -> list[str]:
        """Build question-specific starter prompts from the item's metadata.

        Composition (max 3, deduped): **exactly ONE** answer-choice-centric chip
        — and it is about the USER'S OWN submitted choice only (never the correct
        answer or the other distractors) — plus **TWO CONCEPTUAL** chips about
        the underlying concept/topic being tested (no option letters). Templated
        ENTIRELY from the current question dict (topic, the user's choice, error
        type, cognitive demand) — no LLM call, so it stays instant and
        AI-off-safe. Falls back to conceptual generics when metadata is thin.
        """
        choices = q.get("choices") or []
        topic = self._topic_display(q)
        have_topic = topic != "this concept"
        etype = self._inferred_type or ""
        demand = str(q.get("cognitive_demand") or "").lower()

        correct_idx = None
        try:
            correct_idx = self.session.correct_index()
        except Exception:
            correct_idx = None

        out: list[str] = []

        # (1) EXACTLY ONE answer-choice-centric chip — the USER'S submitted
        # choice only. On a miss, probe why their pick was wrong; on a correct
        # pick, sanity-check their reasoning. Never references the correct answer
        # or any other option.
        if chosen_idx is not None and 0 <= chosen_idx < len(choices):
            label = self._shorten_choice(choices[chosen_idx])
            if correct_idx is not None and chosen_idx == correct_idx:
                out.append(
                    f"I picked {LETTERS[chosen_idx]} ({label}) — "
                    "was my reasoning right?"
                )
            else:
                out.append(
                    f"Why is {LETTERS[chosen_idx]} ({label}) wrong here?"
                )

        # (2) Prioritised pool of CONCEPTUAL chips — about the concept/topic, not
        # any answer option, and free of option letters. Content-specific via the
        # item's topic / cognitive demand / resolved error mode.
        concept: list[str] = []
        if have_topic:
            if etype == ERR_CONTENT_GAP:
                concept.append(f"What core {topic} concept am I missing?")
            concept.append(f"What core concept does this {topic} question test?")
            if demand == "application" or etype == ERR_APPLICATION:
                concept.append(f"How do I apply {topic} step by step?")
            elif demand == "recall":
                concept.append(f"What's the key fact to remember about {topic}?")
            else:
                concept.append(f"How does {topic} work?")
            concept.append(f"What's the key distinction to get {topic} right?")
            concept.append(f"How could {topic} show up in a passage?")
        else:
            concept.append("What core concept does this question test?")
            concept.append("What's the key distinction being tested here?")
            if demand == "application" or etype == ERR_APPLICATION:
                concept.append("How do I apply this concept step by step?")
            elif demand == "recall":
                concept.append("What's the key fact to remember here?")
            else:
                concept.append("How does the underlying concept work?")
            concept.append("How could this concept show up in a passage?")

        seen: set[str] = {c.lower() for c in out}
        conceptual_added = 0
        for c in concept:
            key = c.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
            conceptual_added += 1
            if conceptual_added >= 2:
                break

        # Fallback top-up (thin metadata / no submitted choice): fill from the
        # curated generics but SKIP any that reference the answer choices, so the
        # "no chip about other options" invariant always holds.
        if len(out) < MAX_STARTER_CHIPS:
            for item in FOLLOWUP_STARTER_QUESTIONS:
                key = item.lower()
                if key in seen or "answer choice" in key:
                    continue
                seen.add(key)
                out.append(item)
                if len(out) >= MAX_STARTER_CHIPS:
                    break

        return out[:MAX_STARTER_CHIPS]

    # AI-output flagging (observability) ------------------------------------

    def _note_ai_output(self, text: str, source: str, kind: str) -> None:
        """Record the AI output currently on screen and offer the flag link.

        The flag affordance only appears once there is a real AI answer to
        report, so the AI-off path (which never shows an answer) is untouched.
        """
        self._last_ai_text = text
        self._last_ai_source = source
        self._last_ai_kind = kind
        self.flag_form.setVisible(False)
        self.flag_link.setText("⚑ Report incorrect")
        self.flag_link.setEnabled(True)
        self.flag_link.setVisible(True)

    def _on_flag_ai(self) -> None:
        """Reveal the compact reason + note form for the shown AI answer."""
        if self._last_ai_text is None:
            return
        self.flag_note_input.clear()
        self.flag_reason_combo.setCurrentIndex(0)
        self.flag_form.setVisible(True)
        self.flag_note_input.setFocus()

    def _on_cancel_flag(self) -> None:
        self.flag_form.setVisible(False)

    def _participant_label(self) -> Optional[str]:
        """Best-effort tester label from the Anki profile (never prompts)."""
        name = (getattr(getattr(self.mw, "pm", None), "name", None) or "").strip()
        return name or None

    def _on_submit_flag(self) -> None:
        """Persist a flag on the shown AI answer to the perf sidecar (ai_flags).

        Captured per flag: timestamp, participant label (if known), question id
        + topic + chosen index, the exact AI answer text, its source/model
        attribution, the chosen reason category, and the free-text note.
        """
        if self._last_ai_text is None:
            return
        q = self.session.current
        reason = self.flag_reason_combo.currentData()
        note = self.flag_note_input.text().strip() or None
        try:
            self.store.log_ai_flag(
                ai_answer=self._last_ai_text,
                question_id=q.get("id"),
                topic_id=q.get("topic_id"),
                chosen_index=self._chosen_idx,
                ai_kind=self._last_ai_kind,
                ai_source=self._last_ai_source,
                reason_category=reason,
                note=note,
                participant=self._participant_label(),
            )
        except Exception:
            tooltip("Couldn't save the report — please try again.", parent=self)
            return
        self.flag_form.setVisible(False)
        self.flag_link.setText("✓ Flagged — thanks")
        self.flag_link.setEnabled(False)
        tooltip("Thanks — flagged for review.", parent=self)

    def _mark_choice_wrong(self, idx: int) -> None:
        """Immediately flag the chosen distractor as WRONG at confirm time.

        Applies the red 'wrong' choice style and prepends a ✗ to the choice
        label the moment the student confirms a miss — before the quick-check
        probe and independent of the later reveal — so it is unmistakable they
        got it wrong. Idempotent (rebuilds the label text from the source).
        """
        if not (0 <= idx < len(self.choice_buttons)):
            return
        b = self.choice_buttons[idx]
        b.setStyleSheet(self._choice_styles["wrong"])
        choice_text = self.session.current["choices"][idx]
        b.setText(f"{_MARK_WRONG}  {LETTERS[idx]}.  {choice_text}")

    def _show_choice_feedback(self) -> None:
        """Annotate each choice after reveal: color-code the button, mark the
        correct (✓) and chosen-wrong (✗) options, and render each choice's
        static feedback in its OWN wrapping label beneath it (fully visible,
        never clipped)."""
        q = self.session.current
        cf = q.get("choice_feedback")
        have_cf = isinstance(cf, list)
        correct_idx = self.session.correct_index()
        chosen_idx = self._chosen_idx
        t = _theme_tokens()

        for i, b in enumerate(self.choice_buttons):
            choice_text = q["choices"][i]
            if i == correct_idx:
                style = self._choice_styles["correct"]
                kind = "correct"
                mark = f"{_MARK_CORRECT}  "
            elif i == chosen_idx:
                style = self._choice_styles["wrong"]
                kind = "wrong"
                mark = f"{_MARK_WRONG}  "
            else:
                style = self._choice_styles["neutral"]
                kind = "neutral"
                mark = ""

            b.setStyleSheet(style)
            b.setText(f"{mark}{LETTERS[i]}.  {choice_text}")

            fb = self.choice_fb_labels[i]
            line = cf[i].strip() if (have_cf and i < len(cf) and (cf[i] or "").strip()) else ""
            if line:
                fb.setText(line)
                fb.setStyleSheet(_choice_fb_style(t, kind))
                fb.setVisible(True)
            else:
                fb.setVisible(False)

        self.choice_feedback_label.setVisible(False)

    def _show_explanation(self) -> None:
        """Reveal the static correct-answer explanation for the current item.

        Shown after answering (correct or miss). No-op when the question carries
        no stored explanation (legacy rows), keeping the panel hidden.
        """
        text = (self.session.current.get("explanation") or "").strip()
        if not text:
            self.explanation_label.setVisible(False)
            return
        self.explanation_label.setText(f"Why: {text}")
        self.explanation_label.setVisible(True)

    # Content re-check probe ------------------------------------------------

    def _start_probe(self) -> None:
        """Show the immediate recall probe for the current miss.

        Sequenced BEFORE the explanation + diagnosis: the student first tries to
        recall the backing sub-concept, then reveals the answer to check
        themselves, self-grades, and only then do we surface the (now
        probe-informed) diagnosis + explanation.

        Pre-reveal the probe shows ONLY the blanked recall prompt (``front``)
        plus a single "Show answer" button — the concise answer and the two
        self-grade buttons stay hidden until that click, so nothing is revealed
        before recall. The concise answer (filled cloze / backing card ``back``)
        appears on click, but NEVER the full explanation blurb — that still
        appears afterward via the normal reveal flow (``_show_explanation``).
        """
        target = self._probe_target or {}
        self.probe_prompt.setText(target.get("front") or "")
        self.probe_answer_label.clear()
        self.probe_answer_label.setVisible(False)
        self.probe_frame.setVisible(True)
        self.probe_prompt.setVisible(True)
        # Pre-reveal: only "Show answer" — no answer text, no grade buttons yet.
        self.probe_reveal_button.setEnabled(True)
        self.probe_reveal_button.setVisible(True)
        self.probe_knew_button.setVisible(False)
        self.probe_missed_button.setVisible(False)
        # Diagnosis + reveal stay hidden until the probe is graded and the user
        # confirms/overrides the error type.
        self.miss_frame.setVisible(False)
        self.diag_label.setVisible(False)
        self.diag_hint_label.setVisible(False)
        self.misconception_label.setVisible(False)
        self.confirm_button.setVisible(False)
        self.override_toggle.setVisible(False)
        self._set_self_report_visible(False)
        self._hide_reveal()
        self.next_button.setEnabled(False)

    def _on_probe_reveal(self) -> None:
        """Reveal the CONCISE probe answer, then enable self-grading.

        Gate for the recall check: the blanked prompt shows first with only the
        "Show answer" button; the concise answer and the two self-grade buttons
        appear only after this click, so the student always recalls before
        seeing the answer. Shows just the concise answer (filled cloze / backing
        card ``back``) — never the full explanation blurb (that comes in the
        normal post-grade reveal).
        """
        if not self.session.awaiting_error_type:
            return
        answer = self._probe_answer_html()
        if answer:
            # Fill the blank ONLY — the same sentence with the deletion filled in
            # and highlighted, exactly like cloze-card recall. No "Answer:" label
            # and no explanation blurb (that still follows in the normal reveal).
            self.probe_answer_label.setText(answer)
        else:
            self.probe_answer_label.setText(
                "No short answer to show here — grade from memory; the full "
                "explanation follows after you grade."
            )
        self.probe_answer_label.setVisible(True)
        self.probe_reveal_button.setVisible(False)
        self.probe_knew_button.setVisible(True)
        self.probe_missed_button.setVisible(True)

    def _probe_answer_html(self) -> str:
        """Fill-the-blank reveal for the probe — never the full explanation.

        When ``back_is_html`` is set the reveal is already safe, concise HTML —
        either the cloze source with its deletion filled in and highlighted
        (map-backed cloze probe) or a rendered backing card's answer (filled
        cloze / Basic back) — so it is shown verbatim, exactly like cloze-card
        recall. The only plain-text case left is a Basic/generic probe with no
        blank to fill; there we condense the question's explanation to its first
        sentence (and escape it) so the whole blurb doesn't leak here.
        """
        target = self._probe_target or {}
        back = (target.get("back") or "").strip()
        if not back:
            return ""
        if target.get("back_is_html"):
            return back
        return html.escape(self._first_sentence(back))

    @staticmethod
    def _first_sentence(text: str, *, max_chars: int = 240) -> str:
        """First sentence (or a hard-capped prefix) of a plain-text blurb."""
        clean = " ".join(str(text or "").split())
        if not clean:
            return ""
        for i, ch in enumerate(clean):
            if ch in ".!?" and i >= 20:
                return clean[: i + 1]
        if len(clean) <= max_chars:
            return clean
        return clean[: max_chars - 1].rstrip() + "…"

    def _on_probe_grade(self, knew_it: bool) -> None:
        if not self.session.awaiting_error_type:
            return
        # Record probe outcome + re-run inference; diagnosis follows (no reveal yet).
        self.session.record_probe_outcome(
            knew_it, card_id=(self._probe_target or {}).get("card_id")
        )
        self._hide_probe()
        self._show_diagnosis()

    # Diagnosis panel -------------------------------------------------------

    def _chosen_misconception(self) -> Optional[str]:
        """A ``misconception`` one-liner from the chosen content-gap distractor.

        Reads the question's authored ``choice_diagnosis`` (aligned 1:1 with
        choices). Returns None unless the chosen entry maps to ``content_gap``
        and carries a non-empty misconception string.
        """
        idx = self._chosen_idx
        cd = self.session.current.get("choice_diagnosis")
        if idx is None or not isinstance(cd, list) or not (0 <= idx < len(cd)):
            return None
        entry = cd[idx]
        if not entry:
            return None
        maps = entry.get("maps_to")
        is_content = maps == ERR_CONTENT_GAP or (
            isinstance(maps, list) and ERR_CONTENT_GAP in maps
        )
        misc = entry.get("misconception")
        if is_content and isinstance(misc, str) and misc.strip():
            return misc.strip()
        return None

    def _show_diagnosis(self) -> None:
        """Render the confidence-gated diagnosis for the current miss."""
        self.miss_frame.setVisible(True)
        inf = self.session.pending_inference or {}
        etype = inf.get("error_type", ERR_UNRESOLVED)
        conf = float(inf.get("confidence") or 0.0)

        confident = (
            etype in INFERRED_SCIENCE_TYPES
            and etype not in (ERR_UNRESOLVED, ERR_NONE)
            and conf >= MEDIUM_CONFIDENCE
        )
        if not confident:
            self.diag_label.setVisible(False)
            self.diag_hint_label.setVisible(False)
            self.misconception_label.setVisible(False)
            self.confirm_button.setVisible(False)
            self.override_toggle.setVisible(False)
            self._set_self_report_visible(True)
            return

        self._inferred_type = etype
        label = self.session.inferred_display_label or etype
        pct = round(100 * conf)
        self.diag_label.setText(f"Looks like: <b>{label}</b> · {pct}%")
        self.diag_label.setVisible(True)
        self.diag_hint_label.setVisible(False)

        misc = self._chosen_misconception()
        if misc:
            self.misconception_label.setText(f"Possible misconception: {misc}")
            self.misconception_label.setVisible(True)
        else:
            self.misconception_label.setVisible(False)

        self.confirm_button.setText(f"Confirm: {label}")
        self.confirm_button.setVisible(True)
        self.override_toggle.setText("Actually, something else ▾")
        self.override_toggle.setVisible(True)
        self._set_self_report_visible(False)

    def _on_toggle_override(self) -> None:
        expanded = self.error_label.isVisible()
        # This toggle only exists on a confident miss, where _inferred_type is
        # set; hide that already-suggested type so the override lists only the
        # remaining options. (Confirm handles the suggested type.)
        self._set_self_report_visible(not expanded, exclude=self._inferred_type)
        self.override_toggle.setText(
            "Actually, something else ▴"
            if not expanded
            else "Actually, something else ▾"
        )

    def _on_confirm(self) -> None:
        if not self.session.awaiting_error_type or self._inferred_type is None:
            return
        # Confirming the inferred type → classify_error reconciles it as
        # ``inferred_confirmed`` (self_report matches the inference).
        resolved = self._inferred_type
        self.session.classify_error(resolved)
        self._hide_diagnosis()
        self._show_reveal(correct=False)
        self._present_next_action(resolved)
        self.next_button.setEnabled(True)
        self._update_score_label()

    def _on_error_type(self, error_type: str) -> None:
        if not self.session.awaiting_error_type:
            return
        self.session.classify_error(error_type)
        self._hide_diagnosis()
        self._show_reveal(correct=False)
        self._present_next_action(error_type)
        self.next_button.setEnabled(True)
        self._update_score_label()

    # Application next-action (targeted practice) ---------------------------

    def _present_next_action(self, resolved_type: str) -> None:
        """Surface the application next-action for a resolved ``application`` miss.

        Scoped to the ``application`` channel ONLY — ``content_gap`` (review the
        backing concept) and ``misread`` (pacing nudge) are left untouched. When
        the isolated remediation pool has eligible items for this topic/concept
        we present a concrete, launchable practice set (real short flow, visibly
        unscored); otherwise we degrade to the generic message rather than
        promise a bank that isn't there. Must be called while still on the missed
        question (before Next advances the session).
        """
        if resolved_type != ERR_APPLICATION:
            return
        topic = self.session.current.get("topic_id") or "this topic"
        try:
            items = self.session.application_practice_set()
        except Exception:
            items = []
        self._practice_items = items
        if items:
            n = len(items)
            self.next_action_label.setText(
                f"Next action — applied-reasoning gap: you had the content but "
                f"missed the deployment. Practice <b>{n}</b> similar integration "
                f"item{'s' if n != 1 else ''} in <b>{topic}</b> (separate, not "
                f"scored)."
            )
            self.next_action_label.setVisible(True)
            self.practice_button.setText(
                f"Practice {n} similar item{'s' if n != 1 else ''} →"
            )
            self.practice_button.setVisible(True)
        else:
            # Degradation contract: no eligible pool items → generic message.
            self.next_action_label.setText(
                f"Next action: practice more applied items in <b>{topic}</b>."
            )
            self.next_action_label.setVisible(True)
            self.practice_button.setVisible(False)

    def _on_launch_practice(self) -> None:
        if not self._practice_items:
            return
        from aqt.mcat.remediation_dialog import RemediationPanel

        items = self._practice_items
        self.practice_button.setVisible(False)
        self.content_card.setVisible(False)
        self.footer_bar.setVisible(False)

        while self.remediation_layout.count():
            item = self.remediation_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        def on_done() -> None:
            self.content_card.setVisible(True)
            self.footer_bar.setVisible(True)
            self.remediation_slot.setVisible(False)
            self.next_action_label.setText(
                "Applied practice complete (not scored). Continue when ready."
            )

        panel = RemediationPanel(
            self.mw,
            self.store,
            items,
            on_done=on_done,
            parent=self.remediation_slot,
        )
        self.remediation_layout.addWidget(panel)
        self.remediation_slot.setVisible(True)

    def _update_score_label(self) -> None:
        s = self.session.summary()
        total = self.session.total
        current = self.session.index + 1
        # Honest opt-out tally: "Not sure" answers are excluded from the score,
        # so surface them separately rather than hiding them.
        idk = getattr(self.session, "idk", 0)
        suffix = f"  ·  {idk} not sure" if idk else ""
        progress = f"Question {current} of {total}"
        if s["answered"]:
            pct = round(100 * s["accuracy"])
            self.score_label.setText(
                f"{progress}  ·  Score: {s['correct']}/{s['answered']} "
                f"({pct}%){suffix}"
            )
        else:
            self.score_label.setText(f"{progress}{suffix}")

    def _on_next(self) -> None:
        if self.session.awaiting_error_type:
            return
        self.session.advance()
        if self.session.finished:
            self._show_summary()
        else:
            self._show_question()

    def _show_summary(self) -> None:
        self._clear_choices()
        self._hide_diagnosis()
        self._hide_reveal()
        self.submit_button.setVisible(False)
        self.not_sure_button.setVisible(False)
        self.not_sure_info.setVisible(False)
        self.progress.setValue(self.session.total)
        self.header.setText("Session complete")
        self.source.setText("")
        s = self.session.summary()
        pct = round(100 * s["accuracy"]) if s["answered"] else 0
        idk = getattr(self.session, "idk", 0)
        # "Not sure" answers are excluded from the score — report them honestly.
        idk_line = (
            f"\nMarked \u201cNot sure\u201d on {idk} "
            f"question{'s' if idk != 1 else ''} (not scored)."
            if idk
            else ""
        )
        self.stem.setText(
            f"You answered {s['correct']} of {s['answered']} "
            f"correctly ({pct}%).{idk_line}\n\n"
            "This performance score is separate from your memory score."
        )
        self.feedback.setText("")
        self.next_button.setText("Done")
        self.next_button.setEnabled(True)
        self.next_button.clicked.disconnect()
        qconnect(self.next_button.clicked, self._finish_session)


class BlockedTopicDialog(QDialog):
    """Compact topic picker shown before a BLOCKED performance session.

    Populated by the caller with the unlocked science topics (display names from
    the outline) PLUS a single CARS entry. Each combo item carries a payload —
    ``{"topic_id": ...}`` for a science topic or ``{"section": "CARS"}`` — read
    back via ``selected`` and threaded into the existing session builder. Styled
    with the same theme tokens as the performance view for consistency.
    """

    def __init__(
        self, mw: AnkiQt, entries: list[tuple[str, dict[str, str]]]
    ) -> None:
        super().__init__(mw)
        self.setWindowTitle("MCAT — Blocked session")
        self.setModal(True)
        self.selected: Optional[dict[str, str]] = None
        t = _theme_tokens()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        title = QLabel("Choose a topic to drill")
        title.setStyleSheet(
            f"font-size: 15px; font-weight: 700; color: {t['fg']}; "
            f"background: transparent;"
        )
        layout.addWidget(title)

        hint = QLabel(
            "Blocked = one topic at a time. CARS needs no unlocked memory."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"font-size: 12px; color: {t['fg_subtle']}; background: transparent;"
        )
        layout.addWidget(hint)

        self.combo = QComboBox()
        for label, payload in entries:
            self.combo.addItem(label, payload)
        self.combo.setStyleSheet(
            f"QComboBox {{ background: {t['elevated']}; color: {t['fg']}; "
            f"border: 1px solid {t['border']}; border-radius: 8px; "
            f"padding: 6px 10px; font-size: 13px; min-width: 340px; }}"
        )
        layout.addWidget(self.combo)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_button = QPushButton("Cancel")
        cancel_button.setStyleSheet(
            f"QPushButton {{ color: {t['fg_subtle']}; "
            f"border: 1px solid {t['border']}; border-radius: 8px; "
            f"padding: 7px 14px; background: transparent; }}"
            f"QPushButton:hover {{ background: {t['elevated']}; }}"
        )
        qconnect(cancel_button.clicked, self.reject)
        btn_row.addWidget(cancel_button)
        start_button = QPushButton("Start session")
        start_button.setDefault(True)
        start_button.setStyleSheet(
            f"QPushButton {{ background: {t['accent']}; color: white; "
            f"border: none; border-radius: 8px; padding: 7px 16px; "
            f"font-weight: 700; }}"
            f"QPushButton:hover:enabled {{ filter: brightness(1.06); }}"
        )
        qconnect(start_button.clicked, self._on_start)
        btn_row.addWidget(start_button)
        layout.addLayout(btn_row)

        self.setStyleSheet(f"QDialog {{ background: {t['canvas']}; }}")

    def _on_start(self) -> None:
        self.selected = self.combo.currentData()
        self.accept()


# Backwards-compatible alias (tests / imports).
PerformanceDialog = PerformanceView
