"""Truthful, model-specific activity display for Annotator assistance."""
import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar


_OPERATIONS = {
    'scout': ('Counting cells', 'Prepare image and scout model', 'Run cell count', 'Prepare count estimate'),
    'stardist': ('Finding nuclei', 'Prepare image and StarDist model', 'Find nuclei', 'Prepare nuclear proposals'),
    'cellpose': ('Outlining cells', 'Prepare image and Cellpose model', 'Outline cells', 'Prepare cell outlines'),
}
_STAGES = {'weights': 0, 'image': 0, 'nuclear': 0, 'inference': 1, 'result': 2}
_DETAILS = {
    'weights': 'Locating model weights…',
    'image': 'Reading the image…',
    'nuclear': 'Preparing nuclear guidance…',
    'inference': 'Waiting for the model result…',
    'result': 'Preparing the suggested annotations…',
}


class ModelAssistanceProgress(QWidget):
    """Three observed stages with elapsed time; no fabricated completion percent."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('modelAssistanceProgress')
        self.setStyleSheet('''
            QWidget#modelAssistanceProgress { background: #193047; border: 1px solid #4f789b; border-radius: 6px; }
            QWidget#modelAssistanceProgress QLabel { border: none; background: transparent; color: #e9f2f9; }
            QWidget#modelAssistanceProgress QProgressBar { min-height: 5px; max-height: 5px;
                border: none; background: #29435a; }
            QWidget#modelAssistanceProgress QProgressBar::chunk { background: #64a9df; }
        ''')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 8, 9, 8)
        layout.setSpacing(4)
        title_row = QHBoxLayout()
        self.title = QLabel()
        self.title.setStyleSheet('font-weight: 700;')
        self.elapsed = QLabel()
        title_row.addWidget(self.title)
        title_row.addStretch()
        title_row.addWidget(self.elapsed)
        layout.addLayout(title_row)
        self.steps = []
        for _ in range(3):
            label = QLabel()
            label.setWordWrap(True)
            layout.addWidget(label)
            self.steps.append(label)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        self.activity = QProgressBar()
        self.activity.setRange(0, 0)
        self.activity.setTextVisible(False)
        layout.addWidget(self.activity)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._started = None
        self._last_stage_at = None
        self._last_stage = None
        self._phase = 0
        self._operation = None
        self._stopping = False
        self._fitting = False
        self.hide()

    def begin(self, operation, *, now=None):
        if operation not in _OPERATIONS:
            raise ValueError('Unknown model assistance operation.')
        self._operation = operation
        self._stopping = False
        self._phase = 0
        self._last_stage = None
        self._started = time.monotonic() if now is None else float(now)
        self._last_stage_at = self._started
        self.title.setText(_OPERATIONS[operation][0])
        self.detail.setText('Preparing the image and model…')
        self._render_steps()
        self.refresh(now=self._started)
        self.show()
        self._timer.start()

    def note_stage(self, stage, *, now=None):
        if self._operation is None or self._stopping or stage not in _STAGES:
            return
        phase = _STAGES[stage]
        if phase < self._phase:
            return
        self._phase = phase
        self._last_stage = stage
        self._last_stage_at = time.monotonic() if now is None else float(now)
        self.detail.setText(_DETAILS[stage])
        self._render_steps()
        self.refresh(now=self._last_stage_at)

    def _render_steps(self):
        names = _OPERATIONS[self._operation][1:]
        for index, (label, name) in enumerate(zip(self.steps, names)):
            mark = '✓' if index < self._phase else '●' if index == self._phase else '○'
            label.setText(f'{mark}  {name}')
            label.setStyleSheet('font-weight: 700;' if index == self._phase else '')

    def refresh(self, *, now=None):
        if self._started is None:
            return
        current = time.monotonic() if now is None else float(now)
        elapsed = max(0, int(current - self._started))
        self.elapsed.setText(f'{elapsed // 60}m {elapsed % 60:02d}s elapsed')
        if self._stopping:
            self.detail.setText('Stopping the model…')
        elif current - self._last_stage_at >= 30 and self._phase == 0:
            self.detail.setText('Waiting for model preparation; TEMPO is responding.')
        elif current - self._last_stage_at >= 30 and self._phase == 1:
            self.detail.setText('Waiting for a model result; TEMPO is responding.')
        elif current - self._last_stage_at >= 30 and self._phase == 2:
            self.detail.setText('Waiting for annotation results; TEMPO is responding.')
        self._fit_content()

    def stopping(self):
        self._stopping = True
        self.refresh()

    def _fit_content(self):
        # The inspector scroll area may otherwise compress a wrapped status
        # line beneath the activity bar at small window widths.
        if self._fitting:
            return
        self._fitting = True
        try:
            self.setMinimumHeight(0)
            needed = self.sizeHint().height()
            self.setMinimumHeight(needed)
            self.updateGeometry()
        finally:
            self._fitting = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_content()

    def finish(self):
        self._timer.stop()
        self._operation = None
        self._stopping = False
        self._started = None
        self.hide()
