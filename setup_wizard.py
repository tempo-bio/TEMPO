"""
TEMPO First-Run Onboarding & Zero-Terminal Windows Setup Wizard
==============================================================
Provides a 100% graphical, zero-terminal setup experience for lab collaborators
and PIs on Windows (and Mac), guiding hardware, models, and collaboration profile.
"""

import os
import sys
import subprocess
import shutil

from PyQt6.QtCore import QEvent, Qt, QThread, pyqtSignal, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QLineEdit, QStackedWidget, QFrame, QMessageBox, QScrollArea, QMenu, QBoxLayout
)

from tempo_config import tempo_preferences
from tempo_credentials import safe_profile_name
from tempo_secure_store import save_hf_token, CredentialStorageError
from tempo_shared_settings import CONTROL_REPO
from tempo_hub import tempo_hub
from tempo_tasks import run_read_task, run_task, run_background, run_inline_task
from tempo_ui import ErrorDetails, FlowRow
import tempo_oauth
from tempo_onboarding import SetupGuide, WorkspaceGuideShell, link_button

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class SavedTokenField(QLineEdit):
    """A saved credential is one opaque value, never editable secret text."""
    SAVED_MASK = '\u2022' * 12
    replacement_started = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._saved_present = False
        self._showing_saved = False
        self._editing = False
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self.setInputMethodHints(Qt.InputMethodHint.ImhSensitiveData | Qt.InputMethodHint.ImhNoPredictiveText)
        self.textEdited.connect(self._edited)

    def _edited(self, *_):
        self._editing = True

    @property
    def showing_saved(self):
        return self._showing_saved

    @property
    def replacement_pending(self):
        return self._saved_present and self._editing

    def replacement_token(self):
        # The fixed mask is presentation data, never an API credential.
        return '' if self._showing_saved or self.text() == self.SAVED_MASK else self.text()

    def set_saved_present(self, present):
        self._saved_present = bool(present)
        if self._editing:
            return
        self._showing_saved = bool(present)
        self.setReadOnly(self._showing_saved)
        self.setAcceptDrops(not self._showing_saved)
        self.setPlaceholderText('')
        value = self.SAVED_MASK if self._showing_saved else ''
        if self.text() != value:
            QLineEdit.setText(self, value)

    def mark_saved(self):
        self._editing = False
        self.set_saved_present(True)

    def paste(self):
        if not self._showing_saved:
            super().paste()

    def setText(self, text):
        self._showing_saved = False
        self._editing = True
        self.setReadOnly(False)
        self.setAcceptDrops(True)
        QLineEdit.setText(self, text)

    def clear(self):
        self._showing_saved = False
        self._editing = False
        self.setReadOnly(False)
        QLineEdit.setText(self, '')

    def begin_replacement(self):
        self._showing_saved = False
        self._editing = True
        self.setReadOnly(False)
        self.setAcceptDrops(True)
        QLineEdit.setText(self, '')  # Also discard undo history for the mask.
        self.replacement_started.emit()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if self._showing_saved:
            self.selectAll()

    def mousePressEvent(self, event):
        if self._showing_saved and event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.selectAll()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._showing_saved:
            self.selectAll(); event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._showing_saved:
            self.selectAll(); event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._showing_saved:
            self.selectAll(); event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if not self._showing_saved:
            super().keyPressEvent(event)
            return
        if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            self.begin_replacement()
        elif event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Tab, Qt.Key.Key_Backtab, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            super().keyPressEvent(event)
            return
        else:
            self.selectAll()
        event.accept()

    def contextMenuEvent(self, event):
        if not self._showing_saved:
            super().contextMenuEvent(event)
            return
        self.selectAll()
        menu = QMenu(self)
        menu.addAction('Select All', self.selectAll)
        menu.addAction('Clear', self.begin_replacement)
        menu.exec(event.globalPos())


class SetupWorkerThread(QThread):
    """Background thread performing dependency and hardware verification."""
    step_progress = pyqtSignal(int, str)
    finished_signal = pyqtSignal(bool, str)

    def run(self):
        try:
            frozen = bool(getattr(sys, 'frozen', False))
            if not frozen and sys.prefix == sys.base_prefix:
                raise RuntimeError('Setup must run inside the TEMPO virtual environment.')
            if frozen or os.name != 'nt':
                self.step_progress.emit(0, 'Checking desktop packages…')
                import importlib.util
                missing = [name for name in ('PyQt6', 'cv2', 'numpy', 'pandas', 'keyring', 'serial') if importlib.util.find_spec(name) is None]
                if missing:
                    raise RuntimeError('Missing desktop packages: ' + ', '.join(missing))
                self.step_progress.emit(100, 'Desktop packages found.')
                self.finished_signal.emit(True, 'Desktop packages found. Camera connection and model availability are checked separately.')
                return
            options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            def install(*args):
                subprocess.run([sys.executable, '-m', 'pip', 'install', *args], cwd=BASE_DIR,
                               capture_output=True, text=True, check=True, timeout=900, **options)
            if sys.version_info[:2] != (3, 13):
                raise RuntimeError('Windows source setup requires Python 3.13. Create the TEMPO virtual environment with that version.')
            self.step_progress.emit(15, 'Installing desktop and model packages…')
            install('--no-deps', '--require-hashes', '-r', 'requirements-windows.lock')
            subprocess.run([sys.executable, '-m', 'scripts.windows_native_bundle'], cwd=BASE_DIR,
                           capture_output=True, text=True, check=True, timeout=120, **options)
            subprocess.run([sys.executable, '-m', 'pip', 'check'], cwd=BASE_DIR,
                           capture_output=True, text=True, check=True, timeout=120, **options)
            self.step_progress.emit(80, 'Linking Micro-Manager device adapters…')
            paths = [r'C:\Program Files\Micro-Manager-2.0', r'C:\Program Files\Micro-Manager-2.0gamma', '/Applications/Micro-Manager']
            found = next((p for p in paths if os.path.isdir(p)), None)
            if found:
                from pymmcore_plus import CMMCorePlus
                core = CMMCorePlus.instance()
                core.setDeviceAdapterSearchPaths([found])
                tempo_preferences.set('capture', 'mm_adapter_path', found)
                if not tempo_preferences.save():
                    raise OSError('The camera adapter path could not be saved. Check profile folder permissions and retry requirements setup.')
            self.step_progress.emit(100, 'Dependencies ready. Continue to cloud and model setup.')
            self.finished_signal.emit(True, 'Micro-Manager linked.' if found else 'Micro-Manager was not found. USB camera connection is available in Acquisition.')
        except Exception as exc:
            detail = getattr(exc, 'stderr', None) or str(exc)
            self.finished_signal.emit(False, detail)


class SetupWizardDialog(QDialog):
    """Zero-terminal graphical onboarding wizard."""
    account_saved = pyqtSignal()
    def __init__(self, parent=None, account_only=False):
        super().__init__(parent)
        self.account_only = account_only
        self._oauth_generation = 0
        self._oauth_attempt = None
        self._oauth_preparing = False
        self._pending_oauth_token = None
        self._pending_oauth_name = None
        self._oauth_client_id = tempo_oauth.client_id()
        self._model_task = None
        self._diagnostics_task = None
        self._models_ready = False
        self._requirements_ready = False
        self.setWindowTitle("Account Setup" if account_only else "TEMPO Setup")
        font = self.font()
        font.setPointSizeF(max(10.5, font.pointSizeF()))
        self.setFont(font)
        self.resize(840, 660)
        self.setMinimumSize(480, 420)
        self.setStyleSheet("""
            QFrame#setupRail { background: #151c26; border: none; }
            QFrame#setupFooter { border: none; border-top: 1px solid #354454; }
            QPushButton[onboardingLink="true"] { background: transparent; color: #79c0ff;
                border: none; padding: 2px 0; text-align: left; }
            QPushButton[onboardingLink="true"]:hover { color: #b6deff; }
        """)
        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.stack = QStackedWidget()
        self.footers = QStackedWidget()
        self._page_footers = {}
        self._help_buttons = []
        self._footer_rows = []
        main_layout.addWidget(self.stack, 1)
        main_layout.addWidget(self.footers)
        self.guide = SetupGuide(self)
        self.shell = WorkspaceGuideShell(main, self._open_guide)
        if account_only:
            self.shell.labels[0].hide()
            self.shell.labels[2].hide()
        self.logo = self.shell.logo
        self.shell.compact_changed.connect(self._compact_changed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.shell)

        self._build_page1_welcome()
        self._build_page2_diagnostics()
        self._build_page3_profile()
        self._build_models_page()
        self._build_ready_page()
        self._token_status_generation = 0
        self._token_check_pending = False
        self.stack.setCurrentWidget(self.account_page if account_only else self.welcome_page)
        self.stack.currentChanged.connect(self._page_changed)
        self._page_changed()
        self.finished.connect(self._invalidate_token_check)
        self.finished.connect(self._discard_browser_sign_in)
        self.finished.connect(self.guide.close)
        self._sync_fonts()

    def _open_guide(self, key):
        self.guide.open_guide(key)

    def _compact_changed(self, narrow):
        for button in self._help_buttons:
            button.setVisible(narrow)

    def _new_page(self, title, subtitle):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 28, 30, 22)
        layout.setSpacing(14)
        header = self._add_setup_text(layout, title, heading=True)
        header.setProperty('setupHeading', True)
        font = header.font()
        font.setPointSizeF(font.pointSizeF() * 1.7)
        font.setBold(False)
        header.setFont(font)
        if subtitle:
            self._add_setup_text(layout, subtitle).setStyleSheet('color: #aab8c8;')
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        self.stack.addWidget(scroll)
        return scroll, layout

    def _footer(self, page, text, callback, *, back=None, guide='start'):
        footer = QFrame()
        footer.setObjectName('setupFooter')
        row = QBoxLayout(QBoxLayout.Direction.LeftToRight, footer)
        row.setContentsMargins(24, 16, 24, 16)
        row.setSpacing(14)
        navigation = QWidget()
        nav = QHBoxLayout(navigation)
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(14)
        back_button = QPushButton('Back')
        back_button.setAutoDefault(False)
        back_button.setVisible(back is not None)
        if back:
            back_button.clicked.connect(back)
        nav.addWidget(back_button)
        help_button = link_button('Guide', lambda: self._open_guide(guide))
        help_button.hide()
        self._help_buttons.append(help_button)
        nav.addWidget(help_button)
        nav.addStretch()
        row.addWidget(navigation, 1)
        actions = QWidget()
        buttons = QHBoxLayout(actions)
        buttons.setContentsMargins(0, 0, 0, 0)
        primary = QPushButton(text)
        primary.setAutoDefault(False)
        primary.setProperty('primary', True)
        primary.clicked.connect(callback)
        buttons.addWidget(primary)
        row.addWidget(actions, 0, Qt.AlignmentFlag.AlignRight)
        self._footer_rows.append((footer, row, navigation, actions))
        self.footers.addWidget(footer)
        self._page_footers[page] = footer
        return primary, back_button, buttons

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_footers()

    def _fit_footers(self):
        for footer, row, nav, actions in self._footer_rows:
            need = nav.minimumSizeHint().width() + actions.minimumSizeHint().width() + 62
            row.setDirection(QBoxLayout.Direction.TopToBottom if footer.width() < need
                             else QBoxLayout.Direction.LeftToRight)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange) and hasattr(self, 'shell'):
            self._sync_fonts()

    def _sync_fonts(self):
        # Qt stylesheets otherwise pin descendants to their original font.
        for widget in self.findChildren(QWidget):
            font = self.font()
            if widget.property('setupHeading'):
                font.setPointSizeF(font.pointSizeF() * 1.7)
            else:
                font.setBold(widget.font().bold())
            widget.setFont(font)
        QTimer.singleShot(0, self._fit_footers)

    def _page_changed(self, *_):
        page = self.stack.currentWidget()
        self.footers.setCurrentWidget(self._page_footers[page])
        stage = 1 if page is self.account_page else 2 if page is self.models_page else 3 if page is self.ready_page else 0
        self.shell.set_stage(stage)
        if self.account_only:
            self.shell.labels[1].setText('Account')
        self._account_page_changed()
        QTimer.singleShot(0, self._fit_footers)

    def _build_page1_welcome(self):
        self.welcome_page, layout = self._new_page(
            'Set up TEMPO', None)
        for number, title, detail in (
            (1, 'Check this computer', 'Confirm the app has the packages it needs.'),
            (2, 'Connect your workspace', 'Sign in with your own Hugging Face account.'),
            (3, 'Install your models', 'Get the published models for cell analysis.'),
        ):
            row = QHBoxLayout()
            number_label = QLabel(str(number))
            number_label.setStyleSheet('color: #7ce4ba;')
            row.addWidget(number_label, 0, Qt.AlignmentFlag.AlignTop)
            copy = QVBoxLayout()
            copy.setSpacing(4)
            self._add_setup_text(copy, title, heading=True)
            self._add_setup_text(copy, detail).setStyleSheet('color: #aab8c8;')
            row.addLayout(copy, 1)
            row.setSpacing(14)
            layout.addLayout(row)
        self._add_setup_text(layout, 'Camera and stimulation hardware can be connected later in Acquisition.').setStyleSheet('color: #aab8c8;')
        self._welcome_layout = layout

    def _build_page2_diagnostics(self):
        layout = self._welcome_layout
        self.diag_log = self._add_setup_text(layout, '')
        self.diag_log.hide()
        self.progress = QProgressBar()
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.summary_box = ErrorDetails()
        self.lbl_diag_details = self.summary_box.details
        layout.addWidget(self.summary_box)
        layout.addStretch()
        self.btn_run_diag, self.btn_diag_back, buttons = self._footer(
            self.welcome_page, 'Check this computer', self._run_diagnostics)
        self.btn_diag_next = QPushButton('Continue')
        self.btn_diag_next.setAutoDefault(False)
        self.btn_diag_next.setProperty('primary', True)
        self.btn_diag_next.setEnabled(False)
        self.btn_diag_next.hide()
        self.btn_diag_next.clicked.connect(lambda: self.stack.setCurrentWidget(self.account_page))
        buttons.addWidget(self.btn_diag_next)

    def _run_diagnostics(self):
        if self._diagnostics_task is not None:
            return
        self.btn_run_diag.setEnabled(False)
        self.btn_run_diag.setText('Checking…')
        self.diag_log.setText('Installing requirements…' if os.name == 'nt' and not getattr(sys, 'frozen', False) else 'Checking requirements…')
        self.diag_log.show()
        self.summary_box.clear()
        self.progress.setRange(0, 0)
        self.progress.show()
        self.worker = SetupWorkerThread(self)
        self.worker.step_progress.connect(self._on_diag_progress)
        self.worker.finished_signal.connect(self._on_diag_finished)
        self._diagnostics_task = run_inline_task(
            self, 'Checking this computer…', self.worker.run, self._requirements_finished,
            wait_status=self.diag_log)

    def _requirements_finished(self, value, error):
        self._diagnostics_task = None
        if error:
            self._on_diag_finished(False, str(error))
        self._diagnostics_joined()

    def _on_diag_progress(self, val: int, msg: str):
        self.diag_log.setText(msg)

    def _on_diag_finished(self, success: bool, details: str):
        self._requirements_ready = success
        self.progress.hide()
        self.diag_log.setText('Required packages are available.' if success else 'Requirements check failed.')
        if success:
            self.summary_box.clear()
        else:
            self.summary_box.set_error('Review the details, then check again.', RuntimeError(details))
        self.btn_run_diag.setText('Check again')
        self._diagnostics_joined()

    def _diagnostics_joined(self):
        joined = self._diagnostics_task is None
        self.btn_run_diag.setEnabled(joined)
        self.btn_diag_next.setEnabled(joined and self._requirements_ready)
        self.btn_run_diag.setVisible(not self._requirements_ready)
        self.btn_diag_next.setVisible(self._requirements_ready)

    def _build_page3_profile(self):
        self.account_page, layout = self._new_page(
            'Your account' if self.account_only and tempo_hub.token else 'Connect your workspace',
            'Use your own Hugging Face account for shared images, annotations, and models.')
        self.lbl_account_identity = QLabel()
        self.lbl_account_identity.setWordWrap(True)
        layout.addWidget(self.lbl_account_identity)

        name_label = QLabel('Your name or &laboratory')
        layout.addWidget(name_label)
        self.txt_collab_id = QLineEdit()
        name_label.setBuddy(self.txt_collab_id)
        self.txt_collab_id.setAccessibleName('Profile or laboratory name')
        self.txt_collab_id.setText(safe_profile_name(
            tempo_preferences.get("collaboration", "collaborator_id", ""), tempo_hub.token))
        self.txt_collab_id.setPlaceholderText("Researcher or laboratory name")
        if self.txt_collab_id.text() == "default_collaborator": self.txt_collab_id.clear()
        layout.addWidget(self.txt_collab_id)
        self._add_setup_text(layout, 'This name appears alongside your annotations.').setStyleSheet('color: #aab8c8;')

        self.browser_access_notice = self._add_setup_text(
            layout, 'Browser sign-in includes read/write access to your personal Hugging Face repositories. '
            'Use a fine-grained token for workspace-only access.')
        self.btn_browser_sign_in = QPushButton('Sign in with Hugging Face')
        self.btn_browser_sign_in.setEnabled(bool(self._oauth_client_id))
        self.btn_browser_sign_in.clicked.connect(self._begin_browser_sign_in)
        browser_row = FlowRow()
        browser_row.flow.addWidget(self.btn_browser_sign_in)
        browser_row.flow.addWidget(link_button('Need an account?', lambda: self._open_guide('account')))
        layout.addWidget(browser_row)
        self.oauth_status = QLabel()
        self.oauth_status.setWordWrap(True)
        self.oauth_status.setTextFormat(Qt.TextFormat.PlainText)
        self.oauth_status.hide()
        layout.addWidget(self.oauth_status)
        if not self._oauth_client_id:
            self._set_oauth_status('Browser sign-in is not configured in this build. Use an access token.')
        self._add_setup_text(layout, 'Accept your workspace invitations before signing in.')
        invitation_row = QHBoxLayout()
        invitation_row.addWidget(link_button('Invitation help', lambda: self._open_invitation_help()))
        invitation_row.addStretch()
        layout.addLayout(invitation_row)
        self.btn_manual_sign_in = QPushButton('Use an access token')
        self.btn_manual_sign_in.setVisible(bool(self._oauth_client_id))
        self.btn_manual_sign_in.clicked.connect(self._toggle_manual_sign_in)
        manual_row = QHBoxLayout()
        manual_row.addWidget(self.btn_manual_sign_in)
        manual_row.addStretch()
        layout.addLayout(manual_row)
        self.manual_account_fields = QWidget()
        token_layout = QVBoxLayout(self.manual_account_fields)
        token_layout.setContentsMargins(0, 0, 0, 0)
        token_layout.setSpacing(12)
        token_label = QLabel('Hugging Face &Token:')
        token_layout.addWidget(token_label)
        self.lbl_token_status = QLabel()
        self.lbl_token_status.setWordWrap(True)
        token_layout.addWidget(self.lbl_token_status)
        self.txt_token = SavedTokenField()
        token_label.setBuddy(self.txt_token)
        self.txt_token.setAccessibleName('Hugging Face token')
        self._refresh_token_status()
        self.txt_token.replacement_started.connect(self._refresh_token_status)
        token_layout.addWidget(self.txt_token)
        self.finished.connect(self.txt_token.clear)
        token_help = QLabel('Use a fine-grained token with dataset read/write, model read, '
                            'and read access to tempo-bio-settings/workspace.')
        token_help.setWordWrap(True)
        token_layout.addWidget(token_help)
        create_token = QPushButton('Create a token')
        create_token.clicked.connect(lambda: QDesktopServices.openUrl(QUrl('https://huggingface.co/settings/tokens')))
        create_row = QHBoxLayout()
        create_row.addWidget(create_token)
        create_row.addStretch()
        token_layout.addLayout(create_row)
        self.manual_account_fields.setVisible(not bool(self._oauth_client_id))
        layout.addWidget(self.manual_account_fields)

        self.account_error = ErrorDetails()
        layout.addWidget(self.account_error)
        if tempo_hub.credential_error:
            self.account_error.set_error('Saved credentials could not be opened.',
                                         RuntimeError(tempo_hub.credential_error))
        self.txt_collab_id.textChanged.connect(self._guard_profile_name)
        self.txt_collab_id.textChanged.connect(self._update_save_action)
        self.txt_token.textEdited.connect(self._update_save_action)

        layout.addStretch()

        self.btn_finish, self.btn_account_back, _ = self._footer(
            self.account_page, 'Save Account' if self.account_only else 'Save and continue',
            self._save_and_finish,
            back=None if self.account_only else lambda: self.stack.setCurrentWidget(self.welcome_page),
            guide='account')
        self._refresh_account_identity()
        self._update_save_action()

    @staticmethod
    def _add_setup_text(layout, text, *, heading=False):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        if heading:
            font = label.font()
            font.setBold(True)
            label.setFont(font)
        layout.addWidget(label)
        return label

    @staticmethod
    def _workspace_organizations():
        owners = {}
        for repo, purpose in ((tempo_hub.get_dataset_repo(), 'shared images and annotations'),
                              (tempo_hub.get_models_repo(), 'published models'),
                              (CONTROL_REPO, 'workspace settings')):
            owners.setdefault(repo.split('/')[0], []).append(purpose)
        return owners

    def _open_invitation_help(self):
        self._open_guide('start')
        organizations = '\n'.join(f'• {owner} — {", ".join(purposes)}'
                                  for owner, purposes in self._workspace_organizations().items())
        # Workspace names are drawn from the current configuration, including
        # installations whose organizations differ from the defaults.
        self.guide.browser.setPlainText(
            'Join your workspace\n\nSend your Hugging Face username to your workspace administrator. '
            'Accept the invitation emails for these organizations before signing in:\n\n' + organizations
            + '\n\nUse your own personal account. Ask your administrator if an invitation has expired or access is still unavailable.')
        self.guide.setWindowTitle('Workspace invitations')

    def _build_models_page(self):
        from tempo_model_catalog import MODEL_FILES, MODEL_LABELS
        self.models_page, layout = self._new_page(
            'Get your cell-analysis models', 'Verify and install the published release for this workspace.')
        self.model_statuses = {}
        descriptions = {'nano_scout': 'Scouting', 'stardist': 'Nuclear segmentation', 'cellpose': 'Cell segmentation'}
        for key, filename in MODEL_FILES.items():
            row = QHBoxLayout()
            copy = QVBoxLayout()
            copy.setSpacing(4)
            self._add_setup_text(copy, MODEL_LABELS[key], heading=True)
            self._add_setup_text(copy, descriptions[key]).setStyleSheet('color: #aab8c8;')
            row.addLayout(copy, 2)
            status = QLabel('Not checked')
            status.setWordWrap(True)
            status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(status, 1)
            layout.addLayout(row)
            divider = QFrame()
            divider.setFixedHeight(1)
            divider.setStyleSheet('background: #354454; border: none;')
            layout.addWidget(divider)
            self.model_statuses[filename] = status
        self.model_wait_status = self._add_setup_text(layout, '')
        self.model_wait_status.hide()
        self.model_progress = QProgressBar()
        self.model_progress.setRange(0, 0)
        self.model_progress.setTextVisible(False)
        self.model_progress.setFixedHeight(6)
        self.model_progress.hide()
        layout.addWidget(self.model_progress)
        self.model_error = ErrorDetails()
        layout.addWidget(self.model_error)
        layout.addStretch()
        self.btn_models, self.btn_models_back, _ = self._footer(
            self.models_page, 'Install models', self._install_models,
            back=lambda: self.stack.setCurrentWidget(self.account_page), guide='models')

    def _build_ready_page(self):
        self.ready_page, layout = self._new_page(
            'You’re ready to use TEMPO', 'Start annotating, analyze a recording, or connect your hardware in Acquisition.')
        layout.addWidget(link_button('Your first shared annotation', lambda: self._open_guide('start')),
                         alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(link_button('Models and hardware guide', lambda: self._open_guide('models')),
                         alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        self.btn_open, _, _ = self._footer(self.ready_page, 'Open TEMPO', self.accept)

    def _install_models(self):
        if self._model_task is not None:
            return
        if self._models_ready:
            self.stack.setCurrentWidget(self.ready_page)
            return
        self.model_error.clear()
        self.model_wait_status.setText('Keep TEMPO open while the release is verified and installed.')
        self.model_wait_status.show()
        self.model_progress.show()
        self.btn_models.setEnabled(False)
        self.btn_models.setText('Installing…')
        self.btn_models_back.setEnabled(False)
        for status in self.model_statuses.values():
            status.setText('Verifying / downloading…')

        def install():
            from tempo_model_catalog import MODEL_FILES
            manifest = tempo_hub.model_manifest()
            if set(MODEL_FILES.values()) - set(manifest['models']):
                raise RuntimeError('The published model release is incomplete. Setup requires every model in the catalog.')
            tempo_hub.download_model_release(manifest)
            try:
                with open(os.path.join(sys.prefix, 'tempo_setup_complete'), 'w') as stream:
                    stream.write('1')
            except OSError as error:
                raise RuntimeError('Models were installed, but setup completion could not be recorded. Check folder permissions and retry.') from error

        self._model_task = run_inline_task(
            self, 'Installing the published model release…', install, self._models_finished,
            wait_status=self.model_wait_status)

    def _models_finished(self, value, error):
        self._model_task = None
        self.model_progress.hide()
        self.model_wait_status.hide()
        self.btn_models.setEnabled(True)
        self.btn_models_back.setEnabled(True)
        self._models_ready = error is None
        for status in self.model_statuses.values():
            status.setText('Ready' if error is None else 'Check again')
        if error:
            self.btn_models.setText('Retry installation')
            self.model_error.set_error(
                'Your account was saved, but model installation did not finish. Review the details and retry.',
                error, secrets=(tempo_hub.token,))
        else:
            self.btn_models.setText('Finish setup')

    def _account_page_changed(self, *_):
        if self.stack.currentWidget() is self.account_page:
            self._check_saved_token()
        else:
            self._invalidate_token_check()
            self._cancel_browser_sign_in()

    def _refresh_account_identity(self, error=None):
        token = tempo_hub.token
        verified = bool(token and getattr(tempo_hub, '_identity_token', None) == token
                        and getattr(tempo_hub, '_account_id', None))
        self.lbl_account_identity.setText('Signed in as ' + tempo_hub.get_account_display_name()
                                         if verified and not error else '')
        self.lbl_account_identity.setVisible(self.account_only and verified and not error
                                            and not self._pending_oauth_token)

    def _update_save_action(self, *_):
        if not hasattr(self, 'btn_finish'):
            return
        available = bool(self._pending_oauth_token or self.txt_token.replacement_token()
                         or tempo_hub.token or not tempo_hub._credentials_ready)
        self.btn_finish.setEnabled(bool(self.txt_collab_id.text().strip()) and available
                                   and not self._oauth_preparing and self._oauth_attempt is None)

    def _toggle_manual_sign_in(self):
        self._cancel_browser_sign_in()
        self._pending_oauth_token = self._pending_oauth_name = None
        visible = self.manual_account_fields.isHidden()
        self.manual_account_fields.setVisible(visible)
        self.btn_manual_sign_in.setText('Use browser sign-in' if visible else 'Use an access token')
        self._set_oauth_status('')
        self._refresh_account_identity()
        self._update_save_action()

    def _set_browser_busy(self, busy):
        self.btn_browser_sign_in.setText('Cancel sign-in' if busy else 'Sign in with Hugging Face')
        self.btn_browser_sign_in.setEnabled(bool(self._oauth_client_id))
        self.btn_manual_sign_in.setEnabled(not busy)
        self.manual_account_fields.setEnabled(not busy)
        self._update_save_action()

    def _set_oauth_status(self, text):
        self.oauth_status.setText(text)
        self.oauth_status.setVisible(bool(text))

    def _cancel_browser_sign_in(self):
        self._oauth_generation += 1
        if self._oauth_attempt is not None:
            self._oauth_attempt.cancel()
        self._oauth_attempt = None
        was_busy = self._oauth_preparing or self.btn_browser_sign_in.text() == 'Cancel sign-in'
        self._oauth_preparing = False
        self._set_browser_busy(False)
        if was_busy:
            self._set_oauth_status('Sign-in cancelled.')

    def _discard_browser_sign_in(self, *_):
        self._cancel_browser_sign_in()
        self._pending_oauth_token = self._pending_oauth_name = None

    def _begin_browser_sign_in(self):
        from PyQt6 import sip
        if self._oauth_preparing or self._oauth_attempt is not None:
            self._cancel_browser_sign_in()
            return
        if not self._oauth_client_id:
            return
        self._invalidate_token_check()
        self.account_error.clear()
        self._pending_oauth_token = self._pending_oauth_name = None
        self._oauth_generation += 1
        generation = self._oauth_generation
        self._oauth_preparing = True
        previous_token = tempo_hub.token
        dataset, models = tempo_hub.get_dataset_repo(), tempo_hub.get_models_repo()
        self._set_browser_busy(True)
        self._set_oauth_status('Opening Hugging Face sign-in…')

        def prepared(attempt, error):
            if sip.isdeleted(self) or generation != self._oauth_generation or not self.isVisible():
                if attempt is not None:
                    attempt.close()
                return
            self._oauth_preparing = False
            if error:
                self._set_browser_busy(False)
                self._set_oauth_status('')
                self.account_error.set_error('Could not start browser sign-in.', error)
                return
            self._oauth_attempt = attempt
            if not QDesktopServices.openUrl(QUrl(attempt.authorization_url)):
                attempt.close()
                self._oauth_attempt = None
                self._set_browser_busy(False)
                self._set_oauth_status('')
                self.account_error.set_error('Could not open your browser. Use an access token or try again.',
                                             tempo_oauth.SignInError('No browser accepted the sign-in URL.'))
                return
            owners = list(self._workspace_organizations())
            names = ' and '.join(owners) if len(owners) <= 2 else ', '.join(owners[:-1]) + ', and ' + owners[-1]
            self._set_oauth_status(f'In your browser, approve access to {names}, then return here. You can cancel here.')

            def authenticate():
                token = attempt.wait()
                try:
                    from huggingface_hub import HfApi
                    api = HfApi(token=token)
                    who = api.whoami()
                    if who.get('type') != 'user' or not isinstance(who.get('id'), str) or not who['id']:
                        raise ValueError('A personal account is required.')
                    api.auth_check(repo_id=dataset, repo_type='dataset', write=True)
                    api.repo_info(repo_id=models, repo_type='model')
                    control = api.repo_info(repo_id=CONTROL_REPO, repo_type='model')
                    if control.private is not True:
                        raise ValueError('The settings repository must be private.')
                    name = safe_profile_name(who.get('name'), token)
                    if not name:
                        raise ValueError('The connected account could not be identified.')
                    return token, name
                except Exception:
                    raise tempo_oauth.SignInError(
                        'Workspace access could not be confirmed. Approve the workspace organizations in Hugging Face '
                        'and ask the administrator to check your dataset write, model read, and settings read access. '
                        'If access is correct, check your connection and try again.') from None

            def authenticated(result, error):
                if sip.isdeleted(self) or generation != self._oauth_generation or not self.isVisible():
                    return
                self._oauth_attempt = None
                self._set_browser_busy(False)
                self._set_oauth_status('')
                if error:
                    self.account_error.set_error('Could not connect your Hugging Face account.', error)
                    return
                if (previous_token != tempo_hub.token or dataset != tempo_hub.get_dataset_repo()
                        or models != tempo_hub.get_models_repo()):
                    self.account_error.set_error('Your account or workspace changed. Start sign-in again.',
                                                 tempo_oauth.SignInError('The sign-in result no longer matches the active workspace.'))
                    return
                self._pending_oauth_token, self._pending_oauth_name = result
                self.manual_account_fields.hide()
                self.btn_manual_sign_in.setText('Use an access token')
                self.txt_token.set_saved_present(bool(tempo_hub.token))
                self._set_oauth_status(f'Sign-in complete as {self._pending_oauth_name}. '
                                       + ('Save Account' if self.account_only else 'Save and continue')
                                       + ' to connect this account to TEMPO.')
                self._refresh_account_identity()
                self._update_save_action()
            run_background(authenticate, authenticated)

        identifier = self._oauth_client_id
        run_background(lambda: tempo_oauth.BrowserSignIn(identifier), prepared)

    def _refresh_token_status(self, error=None):
        token = tempo_hub.token
        self.txt_token.set_saved_present(bool(token))
        verified = bool(token and getattr(tempo_hub, '_identity_token', None) == token and getattr(tempo_hub, '_account_id', None))
        if tempo_hub.credential_error or isinstance(error, CredentialStorageError):
            status = 'Saved token unavailable'
        elif error:
            status = 'Token saved, verification failed' if token else 'No token saved'
        elif not tempo_hub._credentials_ready:
            status = 'Checking saved token…'
        elif not token:
            status = 'No token saved'
        else:
            status = 'Token active' if verified else 'Token saved, not verified'
        if self.txt_token.replacement_pending:
            status = 'Token change not saved'
        self.lbl_token_status.setText(status)
        self._update_save_action()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_token_status()
        QTimer.singleShot(0, self._check_saved_token)

    def _invalidate_token_check(self, *_):
        self._token_status_generation += 1
        self._token_check_pending = False

    def _check_saved_token(self):
        from PyQt6 import sip
        if (sip.isdeleted(self) or not self.isVisible() or self.stack.currentWidget() is not self.account_page
                or self._token_check_pending or self._pending_oauth_token or self._oauth_preparing
                or self._oauth_attempt is not None):
            return
        if tempo_hub._credentials_ready and not tempo_hub.token:
            self._refresh_token_status()
            return
        self._token_check_pending = True
        self._token_status_generation += 1
        generation = self._token_status_generation
        expected = {'token': tempo_hub.token}
        self.lbl_token_status.setText('Checking token…' if tempo_hub.token else 'Checking saved token…')
        def check():
            tempo_hub.ensure_credentials()
            expected['token'] = tempo_hub.token
            return tempo_hub.authenticated_account(force_refresh=True) if tempo_hub.token else None
        def ready(identity, error):
            if sip.isdeleted(self) or generation != self._token_status_generation:
                return
            self._token_check_pending = False
            if not self.isVisible():
                return
            if expected['token'] != tempo_hub.token:
                self._refresh_token_status()
                self._refresh_account_identity()
                self._check_saved_token()
                return
            self._refresh_token_status(error)
            self._refresh_account_identity(error)
            if error and not self.account_error.summary.text():
                message = 'Saved credentials could not be opened.' if isinstance(error, CredentialStorageError) else 'Could not verify the saved token.'
                self.account_error.set_error(message, error, secrets=(tempo_hub.token,))
        run_background(check, ready)

    def _guard_profile_name(self, value):
        if value and safe_profile_name(value, tempo_hub.token, self._pending_oauth_token, self.txt_token.replacement_token()) != value:
            self.txt_collab_id.clear()
            self.account_error.set_error('Access tokens belong in the token field. Enter your profile or laboratory name.',
                                         ValueError('Access tokens and account IDs cannot be used as profile names.'))

    def _save_and_finish(self):
        if self._oauth_preparing or self._oauth_attempt is not None:
            return
        self._token_status_generation += 1
        self._token_check_pending = False
        self.account_error.clear()
        collab_id = self.txt_collab_id.text().strip()
        if not collab_id or collab_id == 'default_collaborator':
            self.account_error.set_error('Enter your profile or laboratory name before saving.', ValueError('A profile or laboratory name is required.'))
            self.txt_collab_id.setFocus()
            return
        new_token = self._pending_oauth_token or self.txt_token.replacement_token().strip()
        if self.txt_token.replacement_pending and not new_token:
            self.account_error.set_error('Enter a replacement token before saving.', ValueError('The saved token was cleared from the form. Cancel to keep it, or enter a replacement.'))
            self.txt_token.setFocus()
            return
        token = new_token or tempo_hub.token
        if safe_profile_name(collab_id, token) != collab_id:
            self._guard_profile_name(collab_id)
            return
        recovery = 'Sign in with Hugging Face or enter an access token before saving.'
        try:
            if not new_token and not tempo_hub._credentials_ready:
                recovery = 'The saved account could not be opened. Unlock secure storage and retry.'
                run_task(self, 'Opening saved account securely…', lambda: tempo_hub.ensure_credentials(migrate=True))
                token = tempo_hub.token
            if not token:
                self.txt_token.setFocus()
                raise ValueError(recovery)
            if token:
                recovery = 'Account access could not be verified. Check your connection and token permissions, then save again.'
                from huggingface_hub import HfApi
                def check_access():
                    api = HfApi(token=token)
                    api.whoami()
                    api.repo_info(repo_id=tempo_hub.get_dataset_repo(), repo_type='dataset')
                    api.auth_check(repo_id=tempo_hub.get_dataset_repo(), repo_type='dataset', write=True)
                    api.repo_info(repo_id=tempo_hub.get_models_repo(), repo_type='model')
                    api.repo_info(repo_id=CONTROL_REPO, repo_type='model')
                run_read_task(self, 'Checking shared dataset and model access…', check_access)
                # Validate remotely before replacing the OS-protected credential.
                recovery = 'Cloud credentials could not be saved. Review the details and retry saving your account.'
                if new_token:
                    run_task(self, 'Saving account securely…', lambda: save_hf_token(token))
                recovery = ('Cloud credentials were saved, but the account connection could not be refreshed. Review the details and save again.'
                            if new_token else 'The saved account connection could not be refreshed. Review the details and try again.')
                tempo_hub._init_credentials()
                run_read_task(self, 'Identifying the connected account…', tempo_hub.authenticated_account)
                self._pending_oauth_token = self._pending_oauth_name = None
                self._set_oauth_status('')
                self._refresh_account_identity()
                self.txt_token.mark_saved()
                self._refresh_token_status()
            recovery = ('Cloud credentials were saved, but the annotation profile could not be saved. Check folder permissions and save again.'
                        if new_token else 'The annotation profile could not be saved. Check folder permissions and try again.')
            previous_profile = tempo_preferences.get('collaboration', 'collaborator_id')
            tempo_preferences.set('collaboration', 'collaborator_id', collab_id)
            try:
                if not tempo_preferences.save():
                    raise RuntimeError('The annotation profile could not be saved. Check folder permissions and retry.')
            except Exception:
                tempo_preferences.set('collaboration', 'collaborator_id', previous_profile)
                raise
            recovery = 'Your account and profile were saved locally, but the shared display name could not be updated. Save again to retry.'
            run_task(self, 'Updating your shared profile name…', tempo_hub.publish_collaborator_profile)
            self.account_saved.emit()
            if self.account_only:
                self.accept()
                return
            self._models_ready = False
            self.btn_models.setText('Install models')
            for status in self.model_statuses.values():
                status.setText('Not checked')
            self.stack.setCurrentWidget(self.models_page)
        except Exception as exc:
            self.account_error.set_error(recovery, exc, secrets=(token, tempo_hub.token))

    def _work_pending(self):
        return (self._model_task is not None or self._diagnostics_task is not None
                or (hasattr(self, 'worker') and not self.worker.wait(0)))

    def done(self, result):
        if self._work_pending():
            return
        super().done(result)

    def reject(self):
        self.done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event):
        if self._work_pending():
            event.ignore()
        else:
            super().closeEvent(event)


def run_setup_application(app):
    """Settle and destroy first-run Qt objects before launching the desktop."""
    from tempo_tasks import dispose_closed_views, dispose_application
    wizard = SetupWizardDialog()
    try:
        result = wizard.exec()
    finally:
        # Closing a dialog does not delete it. Do not leave its Python/Qt
        # ownership cycles for SIP's interpreter-exit cleanup under Rosetta.
        dispose_closed_views(wizard)
        dispose_application(app)
    if result == QDialog.DialogCode.Accepted:
        subprocess.Popen([sys.executable, os.path.join(BASE_DIR, 'tempo_gui.py')], cwd=BASE_DIR,
                         **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    return 0


if __name__ == '__main__':
    from PyQt6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    sys.exit(run_setup_application(app))
