# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""MCAT Speedrun — application-practice remediation dialog (native Qt).

A short, SEPARATE practice set launched from the performance dialog when a
science miss is diagnosed as ``application`` ("Applied reasoning"): the student
had the content but failed to deploy it on an integration item, so the fix is
*targeted practice on similar integration items* — not a flashcard.

Critical isolation contract (see MCAT/docs/APPLICATION-PRACTICE-POOL.md):
this flow is **never scored**. Items come from the isolated ``remediation_items``
store and attempts are logged to the ``remediation_attempts`` channel via
``PerfStore.log_remediation_attempt`` — NOT ``perf_attempts`` — so nothing here
can enter the Performance or Readiness scores. The window is visibly distinct
from the scored session (its own title + a "Practice — not scored" banner).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from anki.mcat_perf import LETTERS, PerfStore
from aqt.qt import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    Qt,
    QVBoxLayout,
    qconnect,
)
from aqt.utils import disable_help_button, restoreGeom, saveGeom

if TYPE_CHECKING:
    from aqt.main import AnkiQt


class RemediationDialog(QDialog):
    """Runs a short, unscored application-practice set (isolated channel)."""

    def __init__(
        self,
        parent: QDialog,
        mw: AnkiQt,
        store: PerfStore,
        items: list[dict[str, Any]],
    ) -> None:
        super().__init__(parent)
        self.mw = mw
        self.store = store
        self.items = items
        self.index = 0
        self.answered = 0
        self.correct = 0
        self._shown_at = 0.0
        self._resolved = False

        self.setWindowTitle("MCAT Applied practice — not scored")
        disable_help_button(self)
        self.resize(600, 400)
        restoreGeom(self, "mcatRemediation")

        self._build_ui()
        self._show_item()

    # UI scaffold -----------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Banner makes the unscored, remedial nature unmistakable — visibly
        # distinct from the scored performance session.
        self.banner = QLabel(
            "Applied-reasoning practice · these items are NOT scored — they "
            "won't affect your Performance or Readiness."
        )
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(
            "background: #fff4e5; border: 1px solid #f0c68a; border-radius: 6px; "
            "padding: 8px; margin-bottom: 6px; color: #7a4a00; font-weight: bold;"
        )
        root.addWidget(self.banner)

        self.header = QLabel()
        self.header.setStyleSheet("font-weight: bold;")
        root.addWidget(self.header)

        self.source = QLabel()
        self.source.setStyleSheet("color: gray; font-size: 11px;")
        self.source.setWordWrap(True)
        root.addWidget(self.source)

        self.stem = QLabel()
        self.stem.setWordWrap(True)
        self.stem.setStyleSheet("font-size: 15px; margin: 10px 0;")
        self.stem.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        root.addWidget(self.stem)

        self.choices_box = QVBoxLayout()
        root.addLayout(self.choices_box)

        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        self.feedback.setStyleSheet("margin-top: 8px;")
        root.addWidget(self.feedback)

        self.explanation_label = QLabel()
        self.explanation_label.setWordWrap(True)
        self.explanation_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.explanation_label.setStyleSheet(
            "background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; "
            "padding: 8px; margin-top: 8px; font-size: 13px;"
        )
        self.explanation_label.setVisible(False)
        root.addWidget(self.explanation_label)

        root.addStretch()

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(line)

        bottom = QHBoxLayout()
        self.progress_label = QLabel()
        self.progress_label.setStyleSheet("color: gray;")
        bottom.addWidget(self.progress_label)
        bottom.addStretch()
        self.next_button = QPushButton("Next")
        qconnect(self.next_button.clicked, self._on_next)
        bottom.addWidget(self.next_button)
        root.addLayout(bottom)

    # Item lifecycle --------------------------------------------------------

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

        self.header.setText(
            f"Practice {self.index + 1} of {len(self.items)}  "
            f"·  {q.get('section', '')} · {q.get('topic_id', '')}"
        )
        src = q.get("source_name") or ""
        loc = q.get("source_location") or ""
        self.source.setText(f"{src} — {loc}".strip(" —"))
        self.stem.setText(q["stem"])

        self._clear_choices()
        self.choice_buttons: list[QPushButton] = []
        for i, choice in enumerate(q["choices"]):
            b = QPushButton(f"{LETTERS[i]}.  {choice}")
            b.setStyleSheet("text-align: left; padding: 8px;")
            qconnect(b.clicked, lambda _=False, idx=i: self._on_choice(idx))
            self.choices_box.addWidget(b)
            self.choice_buttons.append(b)

        self.feedback.setText("")
        self.explanation_label.setVisible(False)
        self.next_button.setEnabled(False)
        self.next_button.setText(
            "Next" if self.index < len(self.items) - 1 else "Done"
        )
        self._update_progress()

    def _on_choice(self, idx: int) -> None:
        if self._resolved:
            return
        q = self._current()
        elapsed = time.time() - self._shown_at
        for b in self.choice_buttons:
            b.setEnabled(False)
        is_correct = LETTERS[idx] == q["correct"]
        # Log to the ISOLATED remediation channel — never perf_attempts.
        self.store.log_remediation_attempt(
            q["id"], is_correct, time_seconds=elapsed
        )
        self.answered += 1
        if is_correct:
            self.correct += 1
            self.feedback.setText("✓ Correct")
            self.feedback.setStyleSheet("color: #1a7f37; font-weight: bold;")
        else:
            correct_idx = LETTERS.index(q["correct"])
            self.feedback.setText(
                f"✗ Incorrect. Correct answer: {q['correct']}. "
                f"{q['choices'][correct_idx]}"
            )
            self.feedback.setStyleSheet("color: #cf222e; font-weight: bold;")
        self._show_explanation()
        self._resolved = True
        self.next_button.setEnabled(True)
        self._update_progress()

    def _show_explanation(self) -> None:
        text = (self._current().get("explanation") or "").strip()
        if not text:
            self.explanation_label.setVisible(False)
            return
        self.explanation_label.setText(f"Why: {text}")
        self.explanation_label.setVisible(True)

    def _update_progress(self) -> None:
        if self.answered:
            self.progress_label.setText(
                f"Practice: {self.correct}/{self.answered} (not scored)"
            )
        else:
            self.progress_label.setText("Practice: 0/0 (not scored)")

    def _on_next(self) -> None:
        if not self._resolved:
            return
        if self.index >= len(self.items) - 1:
            self.accept()
            return
        self.index += 1
        self._show_item()

    def done(self, code: int) -> None:
        saveGeom(self, "mcatRemediation")
        super().done(code)
