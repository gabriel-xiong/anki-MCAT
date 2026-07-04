# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""MCAT Speedrun — application-practice remediation panel (native Qt, embedded).

A short, SEPARATE practice set launched inline from the performance view when a
science miss is diagnosed as ``application`` ("Applied reasoning").

Critical isolation contract (see MCAT/docs/APPLICATION-PRACTICE-POOL.md):
this flow is **never scored**. Items come from the isolated ``remediation_items``
store and attempts are logged to the ``remediation_attempts`` channel via
``PerfStore.log_remediation_attempt`` — NOT ``perf_attempts``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

from anki.mcat_perf import LETTERS, PerfStore
from anki.mcat_scores import section_display_name, topic_display_name
from aqt import colors
from aqt.qt import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    Qt,
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.theme import theme_manager

if TYPE_CHECKING:
    from aqt.main import AnkiQt

_ACCENT_ORANGE = "#f5a623"
_ACCENT_GREEN = "#2bb673"
_ACCENT_RED = "#cf222e"
_ACCENT_BLUE = "#4c7cf3"


def _tokens() -> dict[str, str]:
    tm = theme_manager
    night = tm.night_mode
    warm_canvas = "#f5f0e8" if not night else tm.var(colors.CANVAS)
    warm_elevated = "#fffcf7" if not night else tm.var(colors.CANVAS_ELEVATED)
    return {
        "canvas": warm_canvas,
        "elevated": warm_elevated,
        "fg": tm.var(colors.FG),
        "fg_subtle": tm.var(colors.FG_SUBTLE),
        "border": tm.var(colors.BORDER),
        "warn_bg": "rgba(245,166,35,0.12)" if night else "#fff8e6",
        "warn_border": "rgba(245,166,35,0.45)" if night else "#e6cf88",
        "choice_bg": warm_elevated,
        "choice_hover": "rgba(245,166,35,0.18)" if night else "#fff3dc",
        "choice_selected": "rgba(245,166,35,0.28)" if night else "#ffe9b8",
    }


class RemediationPanel(QWidget):
    """Runs a short, unscored application-practice set inline (not a popup)."""

    def __init__(
        self,
        mw: AnkiQt,
        store: PerfStore,
        items: list[dict[str, Any]],
        *,
        on_done: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.mw = mw
        self.store = store
        self.items = items
        self._on_done = on_done
        self.index = 0
        self.answered = 0
        self.correct = 0
        self._shown_at = 0.0
        self._resolved = False
        self._selected_idx: int | None = None
        self.choice_buttons: list[QPushButton] = []

        self._build_ui()
        self.apply_theme()
        self._show_item()

    def apply_theme(self) -> None:
        t = _tokens()
        self.setStyleSheet(f"background: {t['canvas']}; color: {t['fg']};")
        self.back_button.setStyleSheet(
            f"QPushButton {{ color: {t['fg']}; border: 1px solid {t['border']}; "
            f"border-radius: 8px; padding: 6px 12px; background: transparent; "
            f"font-weight: 600; }}"
            f"QPushButton:hover {{ background: {t['elevated']}; }}"
        )
        self.card.setStyleSheet(
            f"QFrame#mcatRemedCard {{ background: {t['elevated']}; "
            f"border: 1px solid {t['border']}; border-top: 3px solid {_ACCENT_ORANGE}; "
            f"border-radius: 12px; }}"
        )
        self.banner.setStyleSheet(
            f"background: {t['warn_bg']}; border: 1px solid {t['warn_border']}; "
            f"border-left: 4px solid {_ACCENT_ORANGE}; border-radius: 10px; "
            f"padding: 10px; color: {t['fg']}; font-weight: 700;"
        )
        self.section_label.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {t['fg_subtle']}; "
            f"text-transform: uppercase; letter-spacing: 0.05em; "
            f"background: transparent; border: none;"
        )
        self.next_button.setStyleSheet(
            f"QPushButton {{ background: {_ACCENT_BLUE}; color: white; "
            f"border: none; border-radius: 8px; padding: 8px 18px; font-weight: 700; }}"
        )
        self.confirm_button.setStyleSheet(
            f"QPushButton {{ background: {_ACCENT_ORANGE}; color: #1a1a1a; "
            f"border: none; border-radius: 8px; padding: 10px 22px; font-weight: 700; }}"
            f"QPushButton:disabled {{ background: {t['border']}; color: {t['fg_subtle']}; }}"
        )
        self._refresh_choice_styles()

    def _choice_style(self, kind: str) -> str:
        t = _tokens()
        base = (
            "text-align: left; padding: 12px 14px; margin: 4px 0; "
            "border-radius: 10px; font-size: 14px;"
        )
        if kind == "default":
            return (
                f"QPushButton {{ {base} border: 1px solid {t['border']}; "
                f"background: {t['choice_bg']}; color: {t['fg']}; }}"
                f"QPushButton:hover:enabled {{ background: {t['choice_hover']}; }}"
            )
        if kind == "selected":
            return (
                f"QPushButton {{ {base} border: 2px solid {_ACCENT_ORANGE}; "
                f"background: {t['choice_selected']}; color: {t['fg']}; font-weight: 600; }}"
            )
        if kind == "correct":
            return (
                f"QPushButton {{ {base} border: 2px solid {_ACCENT_GREEN}; "
                f"background: rgba(43,182,115,0.18); color: {t['fg']}; }}"
            )
        return (
            f"QPushButton {{ {base} border: 2px solid {_ACCENT_RED}; "
            f"background: rgba(207,34,46,0.14); color: {t['fg']}; }}"
        )

    def _refresh_choice_styles(self) -> None:
        for i, b in enumerate(self.choice_buttons):
            if not b.isEnabled():
                continue
            b.setStyleSheet(
                self._choice_style("selected")
                if i == self._selected_idx
                else self._choice_style("default")
            )

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # Discoverable "return" control (top-left), consistent in placement/style
        # with the "← Back to dashboard" button on the other MCAT screens. This
        # panel is embedded INSIDE the performance session, so "back" goes to the
        # session (not the dashboard): it calls the same on_done the final "Back
        # to session" button uses. Safe to leave early — this practice set is
        # unscored and each item is logged per-answer as it is confirmed.
        top_row = QHBoxLayout()
        self.back_button = QPushButton("← Back to session")
        qconnect(self.back_button.clicked, self._on_back)
        top_row.addWidget(self.back_button)
        top_row.addStretch()
        root.addLayout(top_row)

        self.card = QFrame()
        self.card.setObjectName("mcatRemedCard")
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(10)

        self.banner = QLabel(
            "Applied-reasoning practice · NOT scored — won't affect Performance "
            "or Readiness"
        )
        self.banner.setWordWrap(True)
        card_layout.addWidget(self.banner)

        self.header = QLabel()
        self.header.setStyleSheet(
            "font-weight: 700; font-size: 15px; "
            "background: transparent; border: none;"
        )
        card_layout.addWidget(self.header)

        self.progress_label = QLabel()
        self.progress_label.setStyleSheet(
            "font-size: 12px; background: transparent; border: none;"
        )
        card_layout.addWidget(self.progress_label)

        self.source = QLabel()
        self.source.setWordWrap(True)
        card_layout.addWidget(self.source)

        self.section_label = QLabel("Question")
        card_layout.addWidget(self.section_label)

        self.stem = QLabel()
        self.stem.setWordWrap(True)
        self.stem.setStyleSheet(
            "font-size: 16px; line-height: 1.45; padding: 6px 0; "
            "background: transparent; border: none;"
        )
        self.stem.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        card_layout.addWidget(self.stem)

        choices_hdr = QLabel("Answer choices · tap one, then confirm")
        choices_hdr.setStyleSheet(
            "font-size: 11px; font-weight: 700; text-transform: uppercase; "
            "letter-spacing: 0.05em; background: transparent; border: none;"
        )
        card_layout.addWidget(choices_hdr)

        self.choices_box = QVBoxLayout()
        self.choices_box.setSpacing(6)
        card_layout.addLayout(self.choices_box)

        confirm_row = QHBoxLayout()
        confirm_row.addStretch()
        self.confirm_button = QPushButton("Confirm answer")
        self.confirm_button.setEnabled(False)
        qconnect(self.confirm_button.clicked, self._on_confirm)
        confirm_row.addWidget(self.confirm_button)
        card_layout.addLayout(confirm_row)

        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        card_layout.addWidget(self.feedback)

        self.explanation_label = QLabel()
        self.explanation_label.setWordWrap(True)
        self.explanation_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.explanation_label.setVisible(False)
        card_layout.addWidget(self.explanation_label)

        root.addWidget(self.card)

        self.footer = QFrame()
        footer_layout = QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(0, 8, 0, 0)
        footer_layout.addStretch()
        self.next_button = QPushButton("Next")
        self.next_button.setEnabled(False)
        qconnect(self.next_button.clicked, self._on_next)
        footer_layout.addWidget(self.next_button)
        root.addWidget(self.footer)

    def _clear_choices(self) -> None:
        while self.choices_box.count():
            item = self.choices_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _current(self) -> dict[str, Any]:
        return self.items[self.index]

    def _show_item(self) -> None:
        q = self._current()
        self._shown_at = time.time()
        self._resolved = False
        self._selected_idx = None

        self.header.setText(
            f"Practice {self.index + 1} of {len(self.items)}  "
            f"·  {section_display_name(q.get('section', ''))} · "
            f"{topic_display_name(q.get('topic_id', ''))}"
        )
        src = q.get("source_name") or ""
        loc = q.get("source_location") or ""
        self.source.setText(f"{src} — {loc}".strip(" —"))
        self.stem.setText(q["stem"])

        self._clear_choices()
        self.choice_buttons = []
        for i, choice in enumerate(q["choices"]):
            b = QPushButton(f"{LETTERS[i]}.  {choice}")
            qconnect(b.clicked, lambda _=False, idx=i: self._on_choice_select(idx))
            self.choices_box.addWidget(b)
            self.choice_buttons.append(b)

        self.feedback.setText("")
        self.feedback.setStyleSheet("")
        self.explanation_label.setVisible(False)
        self.confirm_button.setVisible(True)
        self.confirm_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.next_button.setText(
            "Next" if self.index < len(self.items) - 1 else "Back to session"
        )
        self.apply_theme()
        self._update_progress()

    def _on_choice_select(self, idx: int) -> None:
        if self._resolved:
            return
        self._selected_idx = idx
        self.confirm_button.setEnabled(True)
        self._refresh_choice_styles()

    def _on_confirm(self) -> None:
        if self._resolved or self._selected_idx is None:
            return
        idx = self._selected_idx
        q = self._current()
        elapsed = time.time() - self._shown_at
        for b in self.choice_buttons:
            b.setEnabled(False)
        self.confirm_button.setVisible(False)
        is_correct = LETTERS[idx] == q["correct"]
        self.store.log_remediation_attempt(
            q["id"], is_correct, time_seconds=elapsed
        )
        self.answered += 1
        if is_correct:
            self.correct += 1
            self.feedback.setText("✓ Correct")
            self.feedback.setStyleSheet(
                f"color: {_ACCENT_GREEN}; font-weight: bold; margin-top: 8px; "
                f"background: transparent; border: none;"
            )
            self.choice_buttons[idx].setStyleSheet(self._choice_style("correct"))
        else:
            correct_idx = LETTERS.index(q["correct"])
            self.feedback.setText(
                f"✗ Incorrect. Correct answer: {q['correct']}. "
                f"{q['choices'][correct_idx]}"
            )
            self.feedback.setStyleSheet(
                f"color: {_ACCENT_RED}; font-weight: bold; margin-top: 8px; "
                f"background: transparent; border: none;"
            )
            self.choice_buttons[idx].setStyleSheet(self._choice_style("wrong"))
            self.choice_buttons[correct_idx].setStyleSheet(
                self._choice_style("correct")
            )
        self._show_explanation()
        self._resolved = True
        self.next_button.setEnabled(True)
        self._update_progress()

    def _show_explanation(self) -> None:
        t = _tokens()
        text = (self._current().get("explanation") or "").strip()
        if not text:
            self.explanation_label.setVisible(False)
            return
        self.explanation_label.setText(f"Why: {text}")
        self.explanation_label.setStyleSheet(
            f"background: {t['elevated']}; border: 1px solid {t['border']}; "
            f"border-left: 4px solid {_ACCENT_GREEN}; border-radius: 10px; "
            f"padding: 10px; margin-top: 8px; font-size: 13px;"
        )
        self.explanation_label.setVisible(True)

    def _update_progress(self) -> None:
        t = _tokens()
        if self.answered:
            self.progress_label.setText(
                f"Practice: {self.correct}/{self.answered} (not scored)"
            )
        else:
            self.progress_label.setText("Practice: 0/0 (not scored)")
        self.progress_label.setStyleSheet(
            f"color: {t['fg_subtle']}; font-size: 12px; "
            f"background: transparent; border: none;"
        )
        self.source.setStyleSheet(
            f"color: {t['fg_subtle']}; font-size: 11px; "
            f"text-transform: uppercase; letter-spacing: .04em; "
            f"background: transparent; border: none;"
        )

    def _on_back(self) -> None:
        """Leave the unscored practice early and return to the session.

        Uses the same ``on_done`` callback the final "Back to session" button
        fires, so the performance view restores itself. Safe at any point:
        confirmed items are already logged to the isolated ``remediation``
        channel per-answer, so an early exit never corrupts scores.
        """
        if self._on_done:
            self._on_done()

    def _on_next(self) -> None:
        if not self._resolved:
            return
        if self.index >= len(self.items) - 1:
            if self._on_done:
                self._on_done()
            return
        self.index += 1
        self._show_item()


# Backwards-compatible alias (tests / legacy imports).
RemediationDialog = RemediationPanel
