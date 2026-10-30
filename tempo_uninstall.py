"""Interactive uninstallation dialog and runtime removal launcher."""
import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QGroupBox,
    QHBoxLayout, QLabel, QPushButton, QVBoxLayout
)

from tempo_config import CONFIG_PATH, get_base_dir
from tempo_ui import InlineNotice, set_button_color
from tempo_tasks import run_task


def find_windows_uninstaller():
    """Locate the Inno Setup uninstaller if running an installed Windows build."""
    if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
        return None
    candidate = Path(sys.executable).parent / 'unins000.exe'
    if candidate.exists():
        return candidate
    return None


class UninstallTempoDialog(QDialog):
    """Confirm and execute TEMPO uninstallation, with optional data cleanup."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Uninstall TEMPO')
        self.resize(520, 310)
        self.setMinimumSize(480, 280)

        self.uninstaller = find_windows_uninstaller()
        is_frozen = getattr(sys, 'frozen', False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        if self.uninstaller:
            install_dir = self.uninstaller.parent
            intro_text = (
                'Uninstalling TEMPO will launch the Windows uninstaller to remove '
                'application files and shortcuts from this computer. TEMPO will close automatically.'
            )
            location_label = f'<b>Installed location:</b> {install_dir}'
            action_label = 'Uninstall and Exit'
        else:
            base_dir = Path(get_base_dir()).resolve()
            intro_text = (
                'TEMPO is running from a source checkout. No Windows installer package was detected. '
                'To uninstall, delete the application folder and its virtual environment.'
            )
            location_label = f'<b>Source location:</b> {base_dir}'
            action_label = 'Clean Data and Exit' if is_frozen else 'Clean Settings and Exit'

        lbl_intro = QLabel(intro_text)
        lbl_intro.setWordWrap(True)
        layout.addWidget(lbl_intro)

        lbl_path = QLabel(location_label)
        lbl_path.setStyleSheet('color: #a0aec0; font-size: 11px;')
        layout.addWidget(lbl_path)

        opts_box = QGroupBox('User Data Options')
        self.options_box = opts_box
        opts_layout = QVBoxLayout(opts_box)
        opts_layout.setSpacing(8)

        self.cb_prefs = QCheckBox('Remove user preferences and window configurations')
        self.cb_creds = QCheckBox('Remove saved Hugging Face authentication tokens')
        opts_layout.addWidget(self.cb_prefs)
        opts_layout.addWidget(self.cb_creds)
        layout.addWidget(opts_box)

        lbl_data_note = QLabel(
            'Personal recordings and analysis outputs (Documents\\TEMPO) are kept safe and will not be removed.'
        )
        lbl_data_note.setWordWrap(True)
        lbl_data_note.setStyleSheet('color: #718096; font-size: 11px;')
        layout.addWidget(lbl_data_note)

        # Pre-flight check for unsynced annotations
        self.notice = InlineNotice(self)
        unsynced_count = self._count_unsynced_annotations()
        if unsynced_count > 0:
            draft_word = 'draft' if unsynced_count == 1 else 'drafts'
            self.notice.setText(
                f'Warning: You have {unsynced_count} local annotation {draft_word} pending sync. '
                'Synchronize before uninstalling to avoid losing drafts.'
            )
            self.notice.setStyleSheet('color: #F2994A; font-size: 11px;')
        else:
            self.notice.setText('All cloud annotations are synchronized. No drafts will be lost.')
            self.notice.setStyleSheet('color: #68D391; font-size: 11px;')
        self.notice.show()
        layout.addWidget(self.notice)

        layout.addStretch()

        btn_row = QHBoxLayout()
        self.btn_cancel = QPushButton('Cancel')
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addStretch()

        self.btn_uninstall = QPushButton(action_label)
        set_button_color(self.btn_uninstall, '#E53E3E')
        self.btn_uninstall.clicked.connect(self.execute_uninstall)
        btn_row.addWidget(self.btn_uninstall)
        layout.addLayout(btn_row)

        self._close_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Close), self)
        self._close_shortcut.activated.connect(self.reject)
        self._exit_action = self._finish_uninstall
        self._pending_exit = False
        self._close_poll = QTimer(self)
        self._close_poll.setInterval(150)
        self._close_poll.timeout.connect(self._poll_close)

    def showEvent(self, event):
        for checkbox in (self.cb_prefs, self.cb_creds):
            checkbox.ensurePolished()
            checkbox.setMinimumHeight(checkbox.sizeHint().height())
        self.options_box.setMinimumHeight(self.options_box.minimumSizeHint().height())
        self._fit_height()
        super().showEvent(event)

    def _fit_height(self):
        layout = self.layout()
        if layout is not None:
            self.setMinimumHeight(max(280, layout.totalMinimumSize().height(),
                                      layout.totalHeightForWidth(self.width())))

    def resizeEvent(self, event):
        self._fit_height()
        super().resizeEvent(event)

    def done(self, result):
        self._close_poll.stop()
        self._pending_exit = False
        parent = self.parent()
        if parent is not None and hasattr(parent, 'cancel_close_action'):
            parent.cancel_close_action()
        super().done(result)

    def _count_unsynced_annotations(self):
        parent = self.parent()
        if parent is not None:
            tab_annotator = getattr(parent, 'tab_annotator', None)
            if tab_annotator is not None:
                annotations = getattr(tab_annotator, 'universal_annotations', {})
                return sum(1 for rec in annotations.values()
                           if rec.get('sync_pending') or rec.get('nuclear_sync_pending'))
        return 0

    def execute_uninstall(self):
        if self._pending_exit:
            return
        self._pending_exit = True
        self._options = self.cb_prefs.isChecked(), self.cb_creds.isChecked()
        self.cb_prefs.setEnabled(False)
        self.cb_creds.setEnabled(False)
        self.btn_uninstall.setEnabled(False)
        self.notice.setText('Waiting for TEMPO to finish its current work before uninstalling. Cancel keeps TEMPO installed.')
        parent = self.parent()
        if parent is not None and hasattr(parent, 'request_close_action'):
            parent.request_close_action(self._exit_action)
        elif parent is None or parent.close():
            self._finish_uninstall()
        if self._pending_exit:
            self._close_poll.start()

    def _poll_close(self):
        parent = self.parent()
        if parent is not None and hasattr(parent, 'request_close_action'):
            if getattr(parent, '_close_action', None) is not self._exit_action:
                self._reset_pending('Uninstall cancelled. TEMPO is still installed.')
                return
            parent.close()
        elif parent is None or parent.close():
            self._finish_uninstall()

    def _reset_pending(self, text):
        self._close_poll.stop()
        self._pending_exit = False
        self.cb_prefs.setEnabled(True)
        self.cb_creds.setEnabled(True)
        self.btn_uninstall.setEnabled(True)
        self.notice.setText(text)

    def _finish_uninstall(self):
        """Called only once ordinary shutdown has accepted the close request."""
        self._close_poll.stop()
        completed = []
        def prepare():
            prefs, credentials = self._options
            if prefs:
                try:
                    os.remove(CONFIG_PATH)
                except FileNotFoundError:
                    pass
                completed.append('Preferences removed.')
            if credentials:
                from tempo_secure_store import delete_hf_token
                delete_hf_token()
                completed.append('Saved token removed.')
            if self.uninstaller:
                if not self.uninstaller.is_file():
                    raise FileNotFoundError('The Windows uninstaller is missing. Repair the installation and retry.')
                creationflags = 0
                if sys.platform == 'win32':
                    creationflags = getattr(subprocess, 'DETACHED_PROCESS', 8) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 512)
                subprocess.Popen([str(self.uninstaller)], creationflags=creationflags, close_fds=True)
            return credentials
        try:
            if run_task(self, 'Preparing uninstall…', prepare):
                from tempo_hub import tempo_hub
                tempo_hub.token = None
        except Exception as error:
            from tempo_credentials import redact_credentials
            self._reset_pending(' '.join(completed + ['Uninstall did not finish.', redact_credentials(str(error))]))
            self.notice.setStyleSheet('color: #F2994A; font-size: 11px;')
            parent = self.parent()
            if parent is not None:
                parent.show()
            self.show()
            return False
        self._pending_exit = False
        self._close_poll.stop()
        self.accept()
        return True
