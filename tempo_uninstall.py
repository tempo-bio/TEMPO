"""Interactive uninstallation dialog and runtime removal launcher."""
import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QGroupBox,
    QHBoxLayout, QLabel, QPushButton, QVBoxLayout
)

from tempo_config import CONFIG_PATH, get_base_dir
from tempo_ui import InlineNotice, set_button_color


def find_windows_uninstaller():
    """Locate the Inno Setup uninstaller if running an installed Windows build."""
    if sys.platform != 'win32' and not getattr(sys, 'frozen', False):
        return None
    candidate = Path(sys.executable).parent / 'unins000.exe'
    if candidate.exists():
        return candidate
    local_app_data = os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), 'AppData', 'Local')
    candidate = Path(local_app_data) / 'Programs' / 'TEMPO' / 'unins000.exe'
    return candidate if candidate.exists() else None


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

    def _count_unsynced_annotations(self):
        parent = self.parent()
        if parent is not None:
            tab_annotator = getattr(parent, 'tab_annotator', None)
            if tab_annotator is not None:
                annotations = getattr(tab_annotator, 'universal_annotations', {})
                return sum(1 for rec in annotations.values() if rec.get('sync_pending'))
        return 0

    def execute_uninstall(self):
        """Execute selected cleanup, launch uninstaller if available, and exit."""
        if self.cb_creds.isChecked():
            try:
                from tempo_secure_store import delete_hf_token
                delete_hf_token()
            except Exception:
                pass
            try:
                from tempo_hub import tempo_hub
                tempo_hub.token = None
            except Exception:
                pass

        if self.cb_prefs.isChecked():
            try:
                if os.path.exists(CONFIG_PATH):
                    os.remove(CONFIG_PATH)
            except Exception:
                pass

        if self.uninstaller and self.uninstaller.exists():
            creationflags = 0
            if sys.platform == 'win32':
                creationflags = getattr(subprocess, 'DETACHED_PROCESS', 0x00000008) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
            subprocess.Popen([str(self.uninstaller)], creationflags=creationflags, close_fds=True)

        self.accept()
        parent = self.parent()
        if parent is not None and hasattr(parent, 'close'):
            parent.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()
