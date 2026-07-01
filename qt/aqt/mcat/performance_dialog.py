# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""MCAT Speedrun — performance session dialog (native Qt).

Presents one eligible MCQ at a time, grades it, and on a miss shows a
confidence-gated diagnosis panel driven by the session's v2 inference
(``content_gap`` / ``application`` / ``misread``). The user either one-tap
confirms the inferred hypothesis or expands the frozen self-report enum to
override it; low-confidence misses fall back to the raw self-report buttons.
Every attempt is logged to the sidecar perf DB via the session's existing
``classify_error`` path (which records ``error_source`` = inferred_confirmed /
self_report_override / self_report). Performance accuracy is shown at the end
and is kept entirely separate from the memory score.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Optional

from anki.mcat_perf import (
    ERR_APPLICATION,
    ERR_CONTENT_GAP,
    ERR_MISREAD,
    ERR_NONE,
    ERR_UNRESOLVED,
    INFERRED_SCIENCE_TYPES,
    LETTERS,
    PerformanceSession,
    PerfStore,
)
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

# Confidence gates for the on-screen diagnosis (per ERROR-DIAGNOSIS-SPEC.md).
# The session does not expose a confidence threshold (its HIGH_M is a mastery
# gate, not a p_top gate), so we use the spec's ~0.7 / ~0.5 fallbacks here.
HIGH_CONFIDENCE = 0.7
MEDIUM_CONFIDENCE = 0.5

# User-facing labels for the v2 3-bucket inference. These same three labels are
# reused for both the confirmed-hypothesis panel AND the self-report block (the
# low-confidence fallback and the override), so the whole dialog speaks the v2
# 3-bucket taxonomy — the frozen 4-type Wednesday enum is never surfaced in UI.
V2_LABELS = {
    ERR_CONTENT_GAP: "Content gap",
    ERR_APPLICATION: "Applied reasoning",
    ERR_MISREAD: "Misread / careless",
}


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
        # Chosen option index for the current miss (stashed on click so we can
        # read the authored choice_diagnosis without reaching into session
        # internals). The inferred type awaiting a Confirm tap.
        self._chosen_idx: Optional[int] = None
        self._inferred_type: Optional[str] = None

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

        # Vertically center the question content: this leading stretch balances
        # the trailing one before the footer, so the stem/choices block sits in
        # the middle of the available space instead of pinned to the top.
        # (Horizontal alignment / word-wrap are unchanged.)
        root.addStretch()

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

        # --- v2 diagnosis panel (hidden until a confident miss) -------------
        # Inferred hypothesis + confidence, e.g. "Looks like: Applied reasoning · 72%".
        self.diag_label = QLabel()
        self.diag_label.setWordWrap(True)
        self.diag_label.setTextFormat(Qt.TextFormat.RichText)
        self.diag_label.setStyleSheet("margin-top: 6px; font-size: 14px;")
        root.addWidget(self.diag_label)

        # Optional one-liner when the chosen distractor encodes a misconception.
        self.misconception_label = QLabel()
        self.misconception_label.setWordWrap(True)
        self.misconception_label.setStyleSheet(
            "color: #57606a; font-style: italic; margin-bottom: 4px;"
        )
        root.addWidget(self.misconception_label)

        # Confirm the hypothesis with one tap, or open the override enum.
        self.diag_row = QHBoxLayout()
        self.confirm_button = QPushButton("Confirm")
        qconnect(self.confirm_button.clicked, self._on_confirm)
        self.diag_row.addWidget(self.confirm_button)
        self.override_toggle = QPushButton("Actually, something else ▾")
        self.override_toggle.setFlat(True)
        self.override_toggle.setStyleSheet(
            "color: #0969da; text-align: left; padding: 4px;"
        )
        qconnect(self.override_toggle.clicked, self._on_toggle_override)
        self.diag_row.addWidget(self.override_toggle)
        self.diag_row.addStretch()
        root.addLayout(self.diag_row)

        # v2 self-report block — the override options, and the low-confidence
        # fallback. Uses the 3-bucket taxonomy (content_gap / applied reasoning /
        # misread), NOT the frozen 4-type Wednesday enum. Hidden until a miss.
        self.error_label = QLabel("What kind of error was this?")
        root.addWidget(self.error_label)
        self.error_row = QHBoxLayout()
        root.addLayout(self.error_row)
        self.error_buttons: list[QPushButton] = []
        # Parallel list of the err_id each button reports, so the confident-miss
        # override can hide the already-suggested type (see _set_self_report_visible).
        self.error_button_ids: list[str] = []
        for err_id in INFERRED_SCIENCE_TYPES:
            b = QPushButton(V2_LABELS.get(err_id, err_id))
            qconnect(
                b.clicked, lambda _=False, e=err_id: self._on_error_type(e)
            )
            self.error_row.addWidget(b)
            self.error_buttons.append(b)
            self.error_button_ids.append(err_id)

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

    def _hide_diagnosis(self) -> None:
        """Hide the whole miss UI (correct answers, new question, resolved)."""
        self.diag_label.setVisible(False)
        self.misconception_label.setVisible(False)
        self.confirm_button.setVisible(False)
        self.override_toggle.setVisible(False)
        self._set_self_report_visible(False)

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
        self._hide_diagnosis()
        self._chosen_idx = None
        self._inferred_type = None
        self.next_button.setEnabled(False)
        self._update_score_label()

    def _on_choice(self, idx: int) -> None:
        if self.session.awaiting_error_type:
            return
        q = self.session.current
        elapsed = time.time() - self._shown_at

        for b in self.choice_buttons:
            b.setEnabled(False)

        self._chosen_idx = idx
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
            self._show_diagnosis()  # must classify before Next
        self._update_score_label()

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
        inf = self.session.pending_inference or {}
        etype = inf.get("error_type", ERR_UNRESOLVED)
        conf = float(inf.get("confidence") or 0.0)

        confident = (
            etype in INFERRED_SCIENCE_TYPES
            and etype not in (ERR_UNRESOLVED, ERR_NONE)
            and conf >= MEDIUM_CONFIDENCE
        )
        if not confident:
            # Low confidence / abstain → raw self-report only, no hypothesis.
            self.diag_label.setVisible(False)
            self.misconception_label.setVisible(False)
            self.confirm_button.setVisible(False)
            self.override_toggle.setVisible(False)
            self._set_self_report_visible(True)
            return

        # High and medium look identical here: the session exposes only a single
        # top hypothesis (no ranked affinities), so both show top-1 + Confirm +
        # an expandable override. (Threshold kept for spec fidelity / future.)
        self._inferred_type = etype
        label = V2_LABELS.get(etype, etype)
        pct = round(100 * conf)
        self.diag_label.setText(f"Looks like: <b>{label}</b> · {pct}%")
        self.diag_label.setVisible(True)

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
        self._set_self_report_visible(False)  # collapsed until expanded

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
        self.session.classify_error(self._inferred_type)
        self._hide_diagnosis()
        self.next_button.setEnabled(True)
        self._update_score_label()

    def _on_error_type(self, error_type: str) -> None:
        if not self.session.awaiting_error_type:
            return
        # Self-report path. classify_error records the source as
        # ``self_report_override`` when the inference was confident but differs,
        # or ``self_report`` when the inference abstained.
        self.session.classify_error(error_type)
        self._hide_diagnosis()
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
        self._hide_diagnosis()
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
