# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""MCAT Speedrun — Topic Mastery viewer (native Qt).

Surfaces the Rust per-topic mastery query directly so a user can see exactly
why each topic is (or is not) unlocked for performance mode. This is the same
query that gates the performance sessions and drives the home-screen dashboard
— shown here in full, per topic, with the unlock rule spelled out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from anki.mcat_perf import (
    MIN_CARDS_SEEN_FOR_PERFORMANCE,
    MIN_GOOD_OR_EASY_FOR_PERFORMANCE,
)
from anki.mcat_scores import (
    section_display_name,
    topic_display_name,
    topic_rows,
)
from aqt.qt import (
    QAbstractItemView,
    QColor,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    Qt,
    QVBoxLayout,
    qconnect,
)
from aqt.utils import disable_help_button, restoreGeom, saveGeom

if TYPE_CHECKING:
    from aqt.main import AnkiQt

_COLS = [
    "Section",
    "Topic",
    "Cards seen",
    "Good/Easy",
    "Retention",
    "Perf attempts",
    "Accuracy",
    "Status",
]

_UNLOCKED_FG = QColor("#1a7f37")
_LOCKED_FG = QColor("#8a6d00")
_CARS_FG = QColor("#4c7cf3")

# Shared "return home" control styling for the MCAT dialogs. Uses palette()
# roles so it adapts to Anki's light/dark themes without a token system, and
# mirrors the top-left "← Back to dashboard" convention used by the embedded
# performance view (outlined, rounded, subtle hover).
_BACK_TO_DASHBOARD_QSS = (
    "QPushButton { border: 1px solid palette(mid); border-radius: 8px; "
    "padding: 6px 12px; font-weight: 600; } "
    "QPushButton:hover { background: palette(midlight); }"
)


class MasteryDialog(QDialog):
    def __init__(self, mw: AnkiQt) -> None:
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("MCAT — Topic Mastery")
        disable_help_button(self)
        self.resize(980, 720)
        self.setMinimumSize(760, 480)
        restoreGeom(self, "mcatMastery")
        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

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

        title = QLabel("Topic Mastery")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        root.addWidget(title)

        explain = QLabel(
            "This is the per-topic <b>mastery query</b> that unlocks "
            "performance mode. A topic unlocks once you have "
            f"<b>≥ {MIN_CARDS_SEEN_FOR_PERFORMANCE} cards seen</b> and "
            f"<b>≥ {MIN_GOOD_OR_EASY_FOR_PERFORMANCE} Good/Easy</b> reviews. "
            "CARS bypasses the gate (always available). "
            "Memory and performance stay separate scores."
        )
        explain.setWordWrap(True)
        explain.setStyleSheet("color: gray; font-size: 13px; margin-bottom: 8px;")
        root.addWidget(explain)

        self.table = QTableWidget()
        self.table.setColumnCount(len(_COLS))
        self.table.setHorizontalHeaderLabels(_COLS)
        self.table.setStyleSheet("QTableWidget { font-size: 14px; }")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(70)
        for c in (0, 2, 3, 4, 5, 6, 7):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.table)

        self.summary = QLabel()
        self.summary.setStyleSheet("font-size: 13px; margin-top: 6px;")
        root.addWidget(self.summary)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        qconnect(buttons.rejected, self.reject)
        qconnect(buttons.accepted, self.accept)
        root.addWidget(buttons)

    def _populate(self) -> None:
        rows = topic_rows(self.mw.col)
        self.table.setRowCount(len(rows))
        unlocked_n = 0
        for r, row in enumerate(rows):
            retr = (
                f"{round(row['avg_retrievability'] * 100)}%"
                if row["avg_retrievability"] > 0
                else "—"
            )
            acc = (
                f"{round(row['accuracy'] * 100)}%"
                if row["accuracy"] is not None
                else "—"
            )

            if row["is_cars"]:
                status = "CARS · no gate"
                fg = _CARS_FG
            elif row["unlocked"]:
                status = "Unlocked"
                fg = _UNLOCKED_FG
                unlocked_n += 1
            else:
                needs = []
                if row["need_seen"]:
                    needs.append(f"{row['need_seen']} more seen")
                if row["need_good"]:
                    needs.append(f"{row['need_good']} more Good/Easy")
                status = "Locked — " + ", ".join(needs) if needs else "Locked"
                fg = _LOCKED_FG

            values = [
                section_display_name(row["section"]),
                row["name"],
                str(row["cards_seen"]),
                str(row["good_or_easy"]),
                retr,
                str(row["attempts"]),
                acc,
                status,
            ]
            for c, val in enumerate(values):
                item = QTableWidgetItem(val)
                if c in (2, 3, 4, 5, 6):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if c == 1:
                    item.setToolTip(topic_display_name(row["topic_id"]))
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                if c == 7:
                    item.setForeground(fg)
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                self.table.setItem(r, c, item)
        self.table.resizeRowsToContents()

        total = len(rows)
        cars = sum(1 for x in rows if x["is_cars"])
        self.summary.setText(
            f"<b>{unlocked_n}</b> of {total - cars} science topics unlocked "
            f"· {cars} CARS topics always available · {total} topics total."
        )

    def done(self, code: int) -> None:
        saveGeom(self, "mcatMastery")
        super().done(code)
