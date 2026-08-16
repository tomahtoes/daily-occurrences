"""The "Daily Occurrences Summary" window: a tiny status panel that refreshes
itself on a timer, plus a pause/resume button.

It is fed a ``get_snapshot`` callable returning the current recorder state and a
``set_paused`` callable, so it always reflects and drives the live engine even if
the engine is rebuilt after a config change. The status indicator carries all three
states on one dot: solid green connected, solid red disconnected, and split
diagonally green/red while paused.

A snapshot may also carry an ``error``, which appears as a red line below the
counts. Without it, everything that can stop words being recorded — a rejected
API key, an unreachable server, a response we can't read — is indistinguishable
from simply having nothing to read.
"""

from aqt.qt import (
    QBrush,
    QColor,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPainter,
    QPushButton,
    QRectF,
    Qt,
    QTimer,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import askUser

_CONNECTED = "#3fb950"      # green
_DISCONNECTED = "#f85149"   # red
_REFRESH_MS = 1500
_DOT_PX = 12

# Pie angles are in 1/16th of a degree, 0 at 3 o'clock, counter-clockwise. Starting
# at 225 deg for 180 deg is exactly the half below-right of the "/" diagonal.
_SPLIT_START = 225 * 16
_SPLIT_SPAN = 180 * 16

# Module-level singleton so reopening from the menu raises the existing window.
_dialog = None


class _StatusDot(QWidget):
    """A small filled circle. One colour paints a plain dot; two paint a dot split
    along the "/" diagonal (``base`` above-left, ``overlay`` below-right), which is
    the paused state — and which QLabel rich text can't express, hence painting it
    by hand."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(_DOT_PX, _DOT_PX)
        self._base = _DISCONNECTED
        self._overlay = None

    def set_colors(self, base, overlay=None):
        if (base, overlay) == (self._base, self._overlay):
            return  # unchanged: don't repaint on every refresh tick
        self._base, self._overlay = base, overlay
        self.update()

    def paintEvent(self, _event):
        # Logical coordinates throughout: the backing store is already at the
        # screen's device pixel ratio, so this stays crisp on HiDPI — and keeps
        # working when the window moves to a monitor with different scaling.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        rect = QRectF(0.5, 0.5, _DOT_PX - 1, _DOT_PX - 1)
        painter.setBrush(QBrush(QColor(self._base)))
        painter.drawEllipse(rect)
        if self._overlay is not None:
            painter.setBrush(QBrush(QColor(self._overlay)))
            painter.drawPie(rect, _SPLIT_START, _SPLIT_SPAN)
        painter.end()


class SummaryDialog(QDialog):
    def __init__(self, parent, get_snapshot, set_paused):
        super().__init__(parent)
        self._get_snapshot = get_snapshot
        self._set_paused = set_paused
        self._prompting = False    # guards re-entry from askUser's nested loop
        self._skip_prompt = False  # set by close_without_prompt() on shutdown

        # Without this the widget survives every close: _forget clears the module
        # global, but mw still parents it, so one dialog leaked per open.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self.setWindowTitle("Daily Occurrences Summary")
        self.setMinimumWidth(280)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        self._dot = _StatusDot(self)
        self._status = QLabel()
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)
        status_row.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(self._status, 1)
        layout.addLayout(status_row)

        self._day = QLabel()
        self._distinct = QLabel()
        self._total = QLabel()
        for label in (self._day, self._distinct, self._total):
            layout.addWidget(label)

        # Shown only when something is wrong. A rejected key, an unreachable API
        # and an unreadable response otherwise all look the same from here:
        # connected, zero words, no explanation.
        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.setStyleSheet(f"color: {_DISCONNECTED};")
        self._error.hide()
        layout.addWidget(self._error)

        self._toggle = QPushButton("Resume recording")
        self._toggle.setAutoDefault(False)  # or Enter would toggle recording
        # Pin to the wider of the two captions so the button doesn't jump on flip.
        self._toggle.setMinimumWidth(self._toggle.sizeHint().width())
        self._toggle.clicked.connect(self._on_toggle)
        # Wrapped in a container so _refresh can hide the button and its spacing
        # together; the stretches on either side keep it centred.
        self._button_row = QWidget()
        button_layout = QHBoxLayout(self._button_row)
        button_layout.setContentsMargins(0, 4, 0, 0)
        button_layout.addStretch(1)
        button_layout.addWidget(self._toggle)
        button_layout.addStretch(1)
        layout.addWidget(self._button_row)
        layout.addStretch(1)  # slack collects here, so hiding it doesn't spread
                              # the labels out

        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    def _refresh(self):
        snap = self._get_snapshot()
        if snap["paused"]:
            self._dot.set_colors(_CONNECTED, _DISCONNECTED)
            text = "Paused"
        else:
            self._dot.set_colors(_CONNECTED if snap["connected"] else _DISCONNECTED)
            text = "Connected" if snap["connected"] else "Not connected"
        self._status.setText(text)
        # Caption derived from state on every tick, so it can never drift out of
        # sync with what the engine is actually doing.
        self._toggle.setText("Resume recording" if snap["paused"] else "Pause recording")
        # Nothing to pause while there's no stream — but stay visible whenever
        # paused, or a drop mid-pause would leave no way to resume.
        self._button_row.setVisible(snap["connected"] or snap["paused"])
        self._day.setText(f'Recording for: <b>{snap["day"]}</b>')
        self._distinct.setText(f'Distinct terms today: <b>{snap["distinct"]}</b>')
        self._total.setText(f'Total terms today: <b>{snap["total"]}</b>')
        error = snap.get("error")
        if error:
            self._error.setText(error)
        self._error.setVisible(bool(error))

    def _on_toggle(self):
        # Read fresh rather than trusting the last tick, then repaint at once so the
        # dot and caption never lag a click by up to _REFRESH_MS.
        self._set_paused(not self._get_snapshot()["paused"])
        self._refresh()

    def close_without_prompt(self):
        """Close on profile close / quit, where a modal question would block a
        shutdown the user didn't associate with this window."""
        self._skip_prompt = True
        self.reject()

    def done(self, result):
        # Overriding done() rather than closeEvent() covers every close path: Esc
        # and reject() deliver no close event at all, while the window manager's
        # close button arrives here via QDialog.closeEvent -> reject().
        if not self._prompting and not self._skip_prompt and self._get_snapshot()["paused"]:
            self._prompting = True
            try:
                resume = askUser(
                    "Recording is still paused.\n\nResume recording now?",
                    parent=self,
                    title="Daily Occurrences",
                )
            finally:
                self._prompting = False
            if resume:
                self._set_paused(False)
        # Here rather than in closeEvent: Esc never reached that, so the refresh
        # timer used to outlive the window.
        self._timer.stop()
        super().done(result)


def show_summary(parent, get_snapshot, set_paused):
    """Open (or raise) the summary window."""
    global _dialog
    if _dialog is None:
        _dialog = SummaryDialog(parent, get_snapshot, set_paused)
        _dialog.finished.connect(_forget)
    _dialog.show()
    _dialog.raise_()
    _dialog.activateWindow()


def force_close():
    """Close the window, if open, without asking about a left-over pause."""
    dialog = _dialog  # the finished signal clears the global mid-call
    if dialog is not None:
        dialog.close_without_prompt()


def _forget(_result=0):
    global _dialog
    _dialog = None
