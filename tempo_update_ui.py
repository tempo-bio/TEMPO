"""Packaged update dialog; all network and preparation work stays off Qt."""
import queue
import threading

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar,
                             QPushButton, QTextEdit, QVBoxLayout)

from tempo_tasks import run_inline_task
from tempo_ui import ErrorDetails
from tempo_update_service import FullDownloadRequired, UpdateCancelled


def _size_text(size):
    if size >= 1024 ** 3:
        return f'{size / (1024 ** 3):.3f} GB'
    return f'{size / (1024 * 1024):.1f} MB' if size >= 1024 * 1024 else f'{size / 1024:.1f} KB'


class PackagedSoftwareUpdateDialog(QDialog):
    """Review, download, and request a guarded restart for an authenticated plan."""
    def __init__(self, parent=None, *, coordinator):
        super().__init__(parent)
        self.coordinator = coordinator
        self.setWindowTitle('Software Update')
        self.resize(620, 420)
        self._state = 'checking'
        self._cancel = threading.Event()
        self._mailbox = queue.SimpleQueue()
        self._close_when_done = False
        self._component_preparing = False
        self._owned_close_action = None
        self._task = None

        layout = QVBoxLayout(self)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setTabChangesFocus(True)
        layout.addWidget(self.log_view, 1)
        self.error_details = ErrorDetails(self)
        layout.addWidget(self.error_details)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.hide()
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        row = QHBoxLayout()
        self.retry = QPushButton('Check Again')
        self.retry.clicked.connect(self.check_release)
        row.addWidget(self.retry)
        row.addStretch()
        self.btn_update = QPushButton('Download Update')
        self.btn_update.setProperty('primary', True)
        self.btn_update.clicked.connect(self._action)
        row.addWidget(self.btn_update)
        layout.addLayout(row)
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(50)
        self._progress_timer.timeout.connect(self._drain_progress)
        self._handoff_timer = QTimer(self)
        self._handoff_timer.setInterval(150)
        self._handoff_timer.timeout.connect(self._check_handoff)
        self.log_view.setPlainText('Checking the latest software release…')
        self._set_state('checking')
        QTimer.singleShot(0, self.check_release)

    def _set_state(self, state):
        self._state = state
        busy = state in ('checking', 'downloading', 'cancelling', 'install_check')
        self.retry.setEnabled(not busy and state != 'waiting')
        text = {'checking': 'Download Update', 'available': 'Download Update',
                'downloading': 'Cancel Download', 'cancelling': 'Cancelling…',
                'ready': 'Install and Restart', 'install_check': 'Install and Restart',
                'waiting': 'Install and Restart', 'full_required': 'Download Full Update',
                'current': 'Download Update', 'error': 'Download Update'}[state]
        self.btn_update.setText(text)
        self.btn_update.setEnabled(state in ('available', 'downloading', 'ready', 'full_required'))
        self.progress.setVisible(state in ('downloading', 'cancelling', 'install_check'))
        if state in ('downloading', 'cancelling', 'install_check'):
            self.progress.setRange(0, 0)
        if state != 'waiting':
            self.status.hide()

    def _run(self, action, done):
        self._cancel.clear()
        self._progress_timer.start()
        self._task = run_inline_task(self, 'Software Update', action, done,
                                     wait_status=self.status)

    def _drain_progress(self):
        latest = None
        while True:
            try:
                latest = self._mailbox.get_nowait()
            except queue.Empty:
                break
        if latest is not None and self._state in ('downloading', 'cancelling'):
            label, received, total = latest
            if label == 'Preparing NVIDIA support':
                self._component_preparing = True
                self.btn_update.setEnabled(False)
                self.btn_update.setText('Preparing…')
                self.status.setText('Preparing NVIDIA support…')
                self.status.show()
                if total <= 0:
                    self.progress.setRange(0, 0)
            if total > 0:
                self.progress.setRange(0, 100)
                self.progress.setValue(min(100, max(0, int(received * 100 / total))))
                self.progress.setFormat(label + ': %p%')

    def _finish_background(self):
        self._progress_timer.stop()
        self._drain_progress()
        self._component_preparing = False
        self._task = None
        if self._close_when_done:
            self._close_when_done = False
            self.reject()
            return True
        return False

    def _display_plan(self, plan):
        target = plan.target
        notes = target.release_notes or 'Release notes are not available.'
        size = sum(asset.size for asset in plan.assets)
        extras = [(self.coordinator.recovery_download_size, 'offline recovery'),
                  (self.coordinator.component_download_size, 'NVIDIA support')]
        total = size + sum(amount for amount, _ in extras)
        summary = f'Installed: {self.coordinator.version}  \nDownload: {_size_text(total)}'
        details = ', '.join(f'{_size_text(amount)} for {label}' for amount, label in extras if amount)
        if details:
            summary += f' (includes {details})'
        self.log_view.setMarkdown(f'## TEMPO {target.version}\n\n{summary}\n\n{notes}')

    def check_release(self):
        if self._task is not None:
            return
        self.error_details.clear()
        self._set_state('checking')
        self._run(lambda: self.coordinator.check(cancel=self._cancel.is_set), self._checked)

    def _checked(self, plan, error):
        if self._finish_background():
            return
        if error:
            self._error('Could not check for updates. Check your connection and try again.', error)
        elif plan is None:
            self.log_view.setPlainText('TEMPO is up to date.')
            self._set_state('current')
        else:
            self._display_plan(plan)
            self._set_state('available')

    def _action(self):
        if self._state == 'downloading':
            self._cancel.set()
            self._set_state('cancelling')
        elif self._state in ('available', 'full_required'):
            full = self._state == 'full_required'
            self.error_details.clear()
            self._component_preparing = False
            self._set_state('downloading')
            def transfer():
                def report(label, received, total):
                    self._mailbox.put((label, received, total))
                if full:
                    self.coordinator.use_full_download()
                self.coordinator.download(cancel=self._cancel.is_set,
                    progress=lambda received, total: report('Downloading update', received, total))
                if self._cancel.is_set():
                    raise UpdateCancelled('Update cancelled.')
                if self.coordinator.component_required:
                    self._mailbox.put(('Preparing NVIDIA support', 0, 0))
                    self.coordinator.prepare_components(
                        progress=lambda received, total: report('Preparing NVIDIA support', received, total))
                if self._cancel.is_set():
                    raise UpdateCancelled('Update cancelled.')
                return True
            self._run(transfer, self._downloaded)
        elif self._state == 'ready':
            self.error_details.clear()
            self._set_state('install_check')
            self._run(self.coordinator.installation_consequences, self._installation_checked)

    def _downloaded(self, value, error):
        if self._finish_background():
            return
        if isinstance(error, UpdateCancelled):
            self._display_plan(self.coordinator.plan)
            self._set_state('available')
        elif isinstance(error, FullDownloadRequired):
            size = _size_text(self.coordinator.plan.target.full.size + self.coordinator.component_download_size
                              + self.coordinator.recovery_download_size)
            self.error_details.set_error(f'The incremental update could not be prepared. Download the full update ({size}) or try later.', error)
            self._set_state('full_required')
        elif error:
            self._error('The update could not be prepared. Review the details and try again.', error)
        else:
            self._set_state('ready')

    def _installation_checked(self, value, error):
        if self._finish_background():
            return
        if error:
            self._error('The update cannot be installed yet. Review the details.', error)
            return
        parent = self.parent()
        prior = getattr(parent, '_close_action', None)
        try:
            accepted = self.coordinator.request_install(parent)
        except Exception as exc:
            self._error('The update could not be installed. Review the details.', exc)
            return
        action = getattr(parent, '_close_action', None)
        self._owned_close_action = action if action is not None and action is not prior else None
        if accepted:
            if parent is not None and not parent.isVisible():
                self._owned_close_action = None
                self.accept()
                return
            self._set_state('waiting')
            self.status.setText('Waiting for TEMPO to finish its current work before restarting.')
            self.status.show()
            if self._owned_close_action is not None:
                self._handoff_timer.start()
        else:
            self._set_state('ready')

    def _check_handoff(self):
        parent = self.parent()
        if self._owned_close_action is not None and getattr(parent, '_close_action', None) is not self._owned_close_action:
            self._handoff_timer.stop()
            self._owned_close_action = None
            if parent is not None and not parent.isVisible():
                self.accept()
                return
            self._set_state('ready')
            self.status.setText('Restart cancelled. The update is ready to install.')
            self.status.show()

    def _error(self, summary, error):
        self.error_details.set_error(str(error) or summary, error)
        self._set_state('error')

    def reject(self):
        if self._task is not None:
            self._close_when_done = True
            self._cancel.set()
            if self._state == 'downloading' and not self._component_preparing:
                self._set_state('cancelling')
            elif self._component_preparing:
                self.status.setText('Finishing NVIDIA preparation before closing…')
                self.status.show()
            return
        self._handoff_timer.stop()
        parent = self.parent()
        if (self._owned_close_action is not None and parent is not None
                and getattr(parent, '_close_action', None) is self._owned_close_action
                and hasattr(parent, 'cancel_close_action')):
            parent.cancel_close_action()
        self._owned_close_action = None
        super().reject()

    def closeEvent(self, event):
        if self._task is not None:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
