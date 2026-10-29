"""Qt display for measured model transfers and verified installation stages."""
from collections import deque
import time

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QGridLayout, QLabel, QProgressBar
from tempo_model_catalog import MODEL_FILES, MODEL_LABELS


def byte_text(value):
    amount = max(0, value)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if amount < 1000 or unit == 'GB':
            return f'{amount:.0f} {unit}' if unit == 'B' else f'{amount:.1f} {unit}'
        amount /= 1000


def duration_text(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds//60} min {seconds%60} s' if seconds >= 60 else f'{seconds} s'


class ModelProgressWidget(QWidget):
    def __init__(self, parent=None, *, states=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.phase = QLabel('Checking model release…')
        self.phase.setWordWrap(True)
        layout.addWidget(self.phase)
        self.rows_layout = QGridLayout()
        self.rows_layout.setColumnStretch(0, 1)
        layout.addLayout(self.rows_layout)
        self.rows = dict(states or {})
        self.bar = QProgressBar()
        layout.addWidget(self.bar)
        self.transfer = QLabel()
        self.transfer.setWordWrap(True)
        layout.addWidget(self.transfer)
        self.elapsed = QLabel()
        self.elapsed.setWordWrap(True)
        layout.addWidget(self.elapsed)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh_time)
        self.begin(MODEL_FILES.values())
        self.timer.stop()

    def add_model(self, filename):
        if filename in self.rows:
            return
        labels = {filename: MODEL_LABELS[key] for key, filename in MODEL_FILES.items()}
        name, state = QLabel(labels.get(filename, filename)), QLabel('Waiting')
        name.setWordWrap(True)
        state.setWordWrap(True)
        state.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row = len(self.rows)
        self.rows_layout.addWidget(name, row, 0)
        self.rows_layout.addWidget(state, row, 1)
        self.rows[filename] = state

    def begin(self, filenames):
        for filename in filenames:
            self.add_model(filename)
        for state in self.rows.values():
            state.setText('Waiting')
        self.started = time.monotonic()
        self.stage = 'checking'
        self.filename = ''
        self.received, self.total = 0, None
        self.samples = deque()
        self.last_data = None
        self.phase.setText('Checking model release…')
        self.transfer.clear()
        self.bar.show()
        self.bar.setRange(0, 0)
        self.refresh_time()
        self.timer.start()

    def update_progress(self, event):
        self.stage = event.stage
        if event.stage not in ('complete', 'failed') and not self.timer.isActive():
            self.timer.start()
        if event.filename:
            self.add_model(event.filename)
            if self.filename != event.filename:
                self.filename = event.filename
                self.samples.clear()
                self.last_data = None
                self.transfer.clear()
                self.received, self.total = 0, None
        titles = {'checking': 'Checking download cache…', 'downloading': 'Downloading models',
                  'verifying': 'Verifying model files', 'verified': 'Verifying model files',
                  'installing': 'Installing verified models', 'complete': 'Models installed',
                  'failed': 'Installation did not finish'}
        self.phase.setText(titles.get(event.stage, 'Installing models…'))
        states = {'checking': 'Checking cache', 'downloading': 'Downloading',
                  'verifying': 'Verifying', 'verified': 'Verified', 'installing': 'Installing'}
        if event.filename:
            self.rows[event.filename].setText(states.get(event.stage, event.stage))
        if event.stage == 'downloading':
            now = time.monotonic()
            if event.received < self.received:
                self.samples.clear()  # Server restarted a range request.
            if self.last_data is None or event.received != self.received:
                self.last_data = now
            self.received, self.total = event.received, event.total
            self.samples.append((now, self.received))
            while len(self.samples) > 2 and now-self.samples[0][0] > 5:
                self.samples.popleft()
            if self.total:
                self.bar.setRange(0, 1000)
                self.bar.setValue(min(1000, int(1000*self.received/self.total)))
            else:
                self.bar.setRange(0, 0)
        elif event.stage == 'complete':
            for state in self.rows.values():
                state.setText('Installed')
            self.bar.setRange(0, 1000)
            self.bar.setValue(1000)
            self.transfer.clear()
            self.timer.stop()
        elif event.stage == 'failed':
            for state in self.rows.values():
                state.setText('Check again')
            self.bar.hide()
            self.transfer.clear()
            self.timer.stop()
        else:
            self.bar.setRange(0, 0)
            self.transfer.clear()
        self.refresh_time()

    def refresh_time(self):
        now = time.monotonic()
        elapsed = 'Elapsed ' + duration_text(now-self.started)
        if self.stage == 'downloading':
            text = byte_text(self.received)
            if self.total:
                text += ' of ' + byte_text(self.total)
            if self.last_data is not None and now-self.last_data >= 10:
                elapsed += ' · Waiting for data (' + duration_text(now-self.last_data) + ')'
            elif len(self.samples) > 1:
                start_time, start_bytes = self.samples[0]
                end_time, end_bytes = self.samples[-1]
                if end_time > start_time and end_bytes > start_bytes:
                    text += ' · ' + byte_text((end_bytes-start_bytes)/(end_time-start_time)) + '/s'
            self.transfer.setText(text)
        self.elapsed.setText(elapsed)
