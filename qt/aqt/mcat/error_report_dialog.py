# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""MCAT Speedrun — Error-diagnosis report (native Qt).

Detailed breakdown of Performance-mode misses by error type. This detail used
to live inline on the home dashboard as a large four-bar block; it is now
relocated here behind a compact "View error diagnosis report" entry point so the
dashboard stays uncluttered. Read-only.

The four diagnostic buckets (BrainLift error typing) drive the dashboard's next
action, so ``misread`` is always shown even at zero. The spread is computed by
the same ``_mcat_error_spread`` helper the dashboard uses, so counts stay
consistent between the compact summary and this report.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from anki.mcat_perf import PerfStore
from aqt.qt import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    Qt,
    QVBoxLayout,
    qconnect,
)
from aqt.utils import disable_help_button, restoreGeom, saveGeom

if TYPE_CHECKING:
    from aqt.main import AnkiQt

# Shared "return home" control styling for the MCAT dialogs. palette() roles
# adapt to light/dark; mirrors the top-left "← Back to dashboard" convention
# used by the embedded performance view.
_BACK_TO_DASHBOARD_QSS = (
    "QPushButton { border: 1px solid palette(mid); border-radius: 8px; "
    "padding: 6px 12px; font-weight: 600; } "
    "QPushButton:hover { background: palette(midlight); }"
)


class ErrorReportDialog(QDialog):
    """Small, read-only breakdown of diagnosed misses by error type."""

    def __init__(self, mw: AnkiQt) -> None:
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("MCAT — Error diagnosis")
        disable_help_button(self)
        self.resize(480, 440)
        self.setMinimumSize(400, 340)
        restoreGeom(self, "mcatErrorReport")
        self._build_ui()

    def _spread(self) -> dict[str, Any]:
        # Reuse the dashboard's computation so the report and the compact
        # home-screen summary can never disagree. Imported lazily to avoid a
        # module-load cycle with aqt.deckbrowser.
        from aqt.deckbrowser import _mcat_error_spread

        with PerfStore(self.mw.col) as store:
            return _mcat_error_spread(store)

    def _build_ui(self) -> None:
        spread = self._spread()
        total = int(spread.get("total") or 0)
        items = spread.get("items") or []

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(4)

        # Discoverable, consistent "return home" control (top-left), matching the
        # performance view and the other MCAT dialogs. Closing this modal dialog
        # returns to the dashboard underneath — no teardown wiring needed.
        top_row = QHBoxLayout()
        back_button = QPushButton("← Back to dashboard")
        back_button.setStyleSheet(_BACK_TO_DASHBOARD_QSS)
        qconnect(back_button.clicked, self.reject)
        top_row.addWidget(back_button)
        top_row.addStretch()
        root.addLayout(top_row)

        title = QLabel("Error diagnosis")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        root.addWidget(title)

        total_text = (
            f"{total} diagnosed miss{'es' if total != 1 else ''} from "
            "Performance mode."
            if total
            else "No diagnosed misses yet."
        )
        subtitle = QLabel(
            f"{total_text} Each miss is typed into one of four buckets; the "
            "biggest bucket drives your dashboard next action."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: gray; font-size: 13px; margin-bottom: 12px;")
        root.addWidget(subtitle)

        for item in items:
            root.addLayout(self._bucket_row(item, total))

        root.addStretch()

        note = QLabel(
            "Drives next action: your top bucket determines what the dashboard "
            "recommends next — content review, passage mapping, applied "
            "practice, or careful-reading pacing."
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            "color: gray; font-size: 12px; margin-top: 12px; font-style: italic;"
        )
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        qconnect(buttons.rejected, self.reject)
        qconnect(buttons.accepted, self.accept)
        root.addWidget(buttons)

    def _bucket_row(self, item: dict[str, Any], total: int) -> QVBoxLayout:
        label = str(item.get("label") or "")
        count = int(item.get("count") or 0)
        pct = int(item.get("pct") or 0)
        accent = str(item.get("accent") or "#9b5cf6")

        row = QVBoxLayout()
        row.setSpacing(4)
        row.setContentsMargins(0, 6, 0, 6)

        head = QHBoxLayout()
        head.setSpacing(8)

        dot = QFrame()
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(f"background: {accent}; border-radius: 5px;")
        head.addWidget(dot)

        name = QLabel(label)
        name.setStyleSheet("font-size: 14px; font-weight: 700;")
        head.addWidget(name)
        head.addStretch()

        stat = QLabel(f"{count} · {pct}%")
        stat.setStyleSheet("font-size: 13px; color: gray; font-weight: 600;")
        head.addWidget(stat)
        row.addLayout(head)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(pct)
        bar.setTextVisible(False)
        bar.setFixedHeight(8)
        bar.setStyleSheet(
            "QProgressBar { background: rgba(127,127,127,0.18); border: none; "
            "border-radius: 4px; } "
            f"QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}"
        )
        row.addWidget(bar)
        return row

    def done(self, code: int) -> None:
        saveGeom(self, "mcatErrorReport")
        super().done(code)
