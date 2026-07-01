# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""MCAT Speedrun — performance session dialog (native Qt).

Presents one eligible MCQ at a time, grades it, and on a miss asks the user to
self-classify the error (4 types from scoring-config.json). Every attempt is
logged to the sidecar perf DB. Performance accuracy is shown at the end and is
kept entirely separate from the memory score.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from anki.mcat_perf import ERROR_TYPES, LETTERS, PerformanceSession, PerfStore
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


class PerformanceDialog(QDialog):
    def __init__(
        self,
        mw: AnkiQt,
        store: PerfStore,
        questions: list[dict[str, Any]],
        *,
        interleaved: bool,
    ) -> None:
        super().__init__(mw)
        self.mw = mw
        self.store = store
        self.interleaved = interleaved
        self.session = PerformanceSession(
            store, questions, interleaved=interleaved
        )
        self._shown_at = 0.0

        mode = "Interleaved" if interleaved else "Blocked"
        self.setWindowTitle(f"MCAT Performance — {mode}")
        disable_help_button(self)
        self.resize(640, 420)
        restoreGeom(self, "mcatPerformance")

        self._build_ui()
        self._show_question()

    # UI scaffold -----------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

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

        # choices container (rebuilt per question)
        self.choices_box = QVBoxLayout()
        root.addLayout(self.choices_box)

        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        self.feedback.setStyleSheet("margin-top: 8px;")
        root.addWidget(self.feedback)

        # error-type row (hidden until a miss)
        self.error_label = QLabel("What kind of error was this?")
        root.addWidget(self.error_label)
        self.error_row = QHBoxLayout()
        root.addLayout(self.error_row)
        self.error_buttons: list[QPushButton] = []
        for err_id, label in ERROR_TYPES:
            b = QPushButton(label)
            qconnect(
                b.clicked, lambda _=False, e=err_id: self._on_error_type(e)
            )
            self.error_row.addWidget(b)
            self.error_buttons.append(b)

        root.addStretch()

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(line)

        bottom = QHBoxLayout()
        self.score_label = QLabel()
        self.score_label.setStyleSheet("color: gray;")
        bottom.addWidget(self.score_label)
        bottom.addStretch()
        self.next_button = QPushButton("Next")
        qconnect(self.next_button.clicked, self._on_next)
        bottom.addWidget(self.next_button)
        root.addLayout(bottom)

    # Question lifecycle ----------------------------------------------------

    def _clear_choices(self) -> None:
        while self.choices_box.count():
            item = self.choices_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _set_error_row_visible(self, visible: bool) -> None:
        self.error_label.setVisible(visible)
        for b in self.error_buttons:
            b.setVisible(visible)

    def _show_question(self) -> None:
        q = self.session.current
        self._shown_at = time.time()

        self.header.setText(
            f"Question {self.session.index + 1} of {self.session.total}  "
            f"·  {q['section']} · {q['topic_id']}"
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
        self._set_error_row_visible(False)
        self.next_button.setEnabled(False)
        self._update_score_label()

    def _on_choice(self, idx: int) -> None:
        if self.session.awaiting_error_type:
            return
        q = self.session.current
        elapsed = time.time() - self._shown_at

        for b in self.choice_buttons:
            b.setEnabled(False)

        correct = self.session.answer(idx, time_seconds=elapsed)
        if correct:
            self.feedback.setText("✓ Correct")
            self.feedback.setStyleSheet("color: #1a7f37; font-weight: bold;")
            self.next_button.setEnabled(True)
        else:
            correct_idx = self.session.correct_index()
            self.feedback.setText(
                f"✗ Incorrect. Correct answer: {q['correct']}. "
                f"{q['choices'][correct_idx]}"
            )
            self.feedback.setStyleSheet("color: #cf222e; font-weight: bold;")
            self._set_error_row_visible(True)  # must classify before Next
        self._update_score_label()

    def _on_error_type(self, error_type: str) -> None:
        if not self.session.awaiting_error_type:
            return
        self.session.classify_error(error_type)
        self._set_error_row_visible(False)
        self.next_button.setEnabled(True)
        self._update_score_label()

    def _update_score_label(self) -> None:
        s = self.session.summary()
        if s["answered"]:
            pct = round(100 * s["accuracy"])
            self.score_label.setText(
                f"Session: {s['correct']}/{s['answered']} ({pct}%)"
            )
        else:
            self.score_label.setText("Session: 0/0")

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
        self._set_error_row_visible(False)
        self.header.setText("Session complete")
        self.source.setText("")
        s = self.session.summary()
        pct = round(100 * s["accuracy"]) if s["answered"] else 0
        self.stem.setText(
            f"You answered {s['correct']} of {s['answered']} "
            f"correctly ({pct}%).\n\n"
            "This performance score is separate from your memory score."
        )
        self.feedback.setText("")
        self.next_button.setText("Close")
        self.next_button.setEnabled(True)
        # rewire Next → close
        self.next_button.clicked.disconnect()
        qconnect(self.next_button.clicked, self.accept)

    # Cleanup ---------------------------------------------------------------

    def done(self, code: int) -> None:
        saveGeom(self, "mcatPerformance")
        try:
            self.store.close()
        except Exception:
            pass
        super().done(code)
