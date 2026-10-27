"""Responsive software updates and checksum-pinned model releases."""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QTimer, QUrl
from PyQt6.QtWidgets import QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar, QTextEdit, QMessageBox, QSizePolicy
from PyQt6.QtGui import QDesktopServices
from tempo_hub import tempo_hub
from tempo_tasks import run_read_task, run_task, run_background
from tempo_ui import format_model_metric, ErrorDetails
from tempo_training import MODEL_FILES
from tempo_shared_settings import fetch_shared_settings, apply_shared_settings
from tempo_logo import AnimatedTempoLogo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_RELEASES_URL = 'https://github.com/tempo-bio/TEMPO/releases'


def hidden_process_options():
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def checked_command(command):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never')
    result = subprocess.run(command, cwd=BASE_DIR, capture_output=True, text=True, timeout=900, env=env, **hidden_process_options())
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f'{command[0]} failed')
    return result.stdout.strip()


def perform_software_update():
    if getattr(sys, 'frozen', False):
        raise RuntimeError('Packaged TEMPO uses the release installer. Download it from GitHub Releases, close TEMPO, then run the installer.')
    if sys.prefix == sys.base_prefix:
        raise RuntimeError('Restart TEMPO from its virtual environment before updating.')
    branch = checked_command(['git','branch','--show-current'])
    if branch != 'main':
        raise RuntimeError('Automatic updates require the main branch. Your checkout was left unchanged.')
    previous_revision = checked_command(['git','rev-parse','HEAD'])
    stash = None
    if checked_command(['git','status','--porcelain']):
        checked_command(['git','stash','push','--include-untracked','-m','TEMPO automatic update backup'])
        stash = checked_command(['git','rev-parse','refs/stash'])
    try:
        checked_command(['git','pull','--ff-only','origin','main'])
        changed = checked_command(['git','rev-parse','HEAD']) != previous_revision
        if changed:
            requirements = 'requirements-windows.txt' if os.name == 'nt' else 'requirements.txt'
            checked_command([sys.executable,'-m','pip','install','-r',requirements])
    except Exception as exc:
        if stash:
            try:
                checked_command(['git','stash','apply',stash])
            except Exception:
                raise RuntimeError(f'{exc}\nLocal changes remain in stash {stash}; automatic restoration needs review.') from exc
        raise
    if stash:
        try:
            checked_command(['git','stash','apply',stash])
        except Exception as exc:
            raise RuntimeError(f'Update fetched, but restoring local changes needs review. Backup: {stash}. No restart performed.') from exc
    if not changed:
        return None
    return 'Software and dependencies updated.' + (f' Local changes restored; backup retained in stash {stash}.' if stash else '')


def software_release(timeout=1.2):
    if getattr(sys, 'frozen', False):
        raise RuntimeError('Check GitHub Releases for a TEMPO installer update.')
    # Use the same authenticated remote as the update itself, including SSH remotes.
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never')
    branch = subprocess.run(['git', 'branch', '--show-current'], cwd=BASE_DIR,
                            capture_output=True, text=True, timeout=timeout,
                            env=env, **hidden_process_options())
    if branch.returncode:
        raise RuntimeError('Could not identify the software branch. Verify repository access and Git credentials.')
    if branch.stdout.strip() != 'main':
        return False, 'Automatic software updates are available on main. This checkout was left unchanged.'
    remote = subprocess.run(['git', 'ls-remote', '--exit-code', 'origin', 'refs/heads/main'],
                            cwd=BASE_DIR, capture_output=True, text=True, timeout=timeout,
                            env=env, **hidden_process_options())
    if remote.returncode:
        raise RuntimeError('Could not read the configured Git origin. Verify repository access and Git credentials.')
    rows = [line.split() for line in remote.stdout.splitlines()]
    revisions = [row[0] for row in rows if len(row) == 2 and row[1] == 'refs/heads/main']
    if len(revisions) != 1:
        raise RuntimeError('The configured Git origin did not return its main branch.')
    local = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=BASE_DIR, capture_output=True,
                           text=True, timeout=timeout, **hidden_process_options())
    if local.returncode or not local.stdout.strip():
        raise RuntimeError('Could not identify the installed software revision.')
    if revisions[0] == local.stdout.strip():
        return False, 'Software is up to date.'
    comparison_command = ['git', 'rev-list', '--left-right', '--count',
                          f'{local.stdout.strip()}...{revisions[0]}']
    def compare():
        return subprocess.run(comparison_command, cwd=BASE_DIR, capture_output=True,
                              text=True, timeout=timeout, env=env, **hidden_process_options())
    comparison = compare()
    if comparison.returncode:
        # A newly published commit may not exist locally yet. Fetch its history
        # without merging, switching branches, or overwriting FETCH_HEAD.
        fetched = subprocess.run(['git', 'fetch', '--no-tags', '--no-recurse-submodules',
                                  '--no-write-fetch-head', 'origin', 'refs/heads/main'],
                                 cwd=BASE_DIR, capture_output=True, text=True, timeout=timeout,
                                 env=env, **hidden_process_options())
        if fetched.returncode:
            raise RuntimeError('Could not retrieve the software history. Check the connection and Git credentials, then check again.')
        comparison = compare()
    if comparison.returncode:
        raise RuntimeError('Could not compare the installed and published software history. Check again before updating.')
    try:
        ahead, behind = map(int, comparison.stdout.split())
        if ahead < 0 or behind < 0:
            raise ValueError
    except ValueError as exc:
        raise RuntimeError('Git returned an invalid software history comparison.') from exc
    if ahead and behind:
        return False, ('This copy and GitHub have different changes. Automatic updating is unavailable; '
                       'the changes need to be merged manually.')
    if ahead:
        return False, 'This copy is newer than the published version.'
    return bool(behind), f'Available software revision: {revisions[0][:12]}' if behind else 'Software is up to date.'


def model_release_probe():
    # Startup probes may read secure credentials, but cannot migrate them.
    tempo_hub.ensure_credentials()
    headers = {'User-Agent':'TEMPO'}
    if tempo_hub.token:
        headers['Authorization'] = 'Bearer ' + tempo_hub.token
    request = urllib.request.Request(f'https://huggingface.co/{tempo_hub.get_models_repo()}/resolve/main/model_manifest.json',headers=headers)
    from tempo_evaluation_notebook import urlopen
    with urlopen(request, timeout=1.2) as response:
        manifest = json.load(response)
    from tempo_hub import _model_records
    from tempo_model_catalog import local_model_path
    manifest['revision'] = 'startup-probe'
    for name, record in _model_records(manifest).items():
        path = local_model_path(name)
        if not os.path.isfile(path) or tempo_hub.calculate_sha256(path) != record['sha256']:
            return True
    return False


class StartupUpdateCheck(QObject):
    """Bounded startup reads cannot keep the application alive after Quit.

    run_background uses daemon threads. A GUI timer bounds the startup wait,
    and responses after that deadline are ignored. Validated workspace settings
    apply before the dashboard opens; software and models are never installed here.
    """
    check_finished = pyqtSignal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._started = False
        self._finished = False
        self._pending = {'software', 'models', 'workspace'}
        self._results = {}
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.setInterval(5000)
        self.deadline.timeout.connect(self.finish)

    def start(self):
        if self._started:
            return
        self._started = True
        self.deadline.start()
        run_background(software_release, lambda value, error: self.received('software', value, error))
        def workspace():
            tempo_hub.ensure_credentials()
            token = tempo_hub.token
            return token, fetch_shared_settings(tempo_hub) if token else None
        run_background(workspace, lambda value, error: self.received('workspace', value, error))

    def received(self, name, value, error):
        if self._finished or name not in self._pending:
            return
        self._pending.remove(name)
        if name == 'workspace':
            if error is None:
                token, record = value
                if token == tempo_hub.token:
                    try:
                        apply_shared_settings(record)
                    except Exception as failure:
                        error = failure
            if error is not None:
                self._results['workspace_error'] = 'Shared workspace settings could not be refreshed. Saved settings were retained.'
            run_background(model_release_probe, lambda result, failure: self.received('models', result, failure))
        if error is None:
            self._results[name] = value
        if not self._pending:
            self.finish()

    def finish(self):
        if self._finished:
            return
        self._finished = True
        self.deadline.stop()
        found, details = False, []
        software = self._results.get('software')
        if software and software[0]:
            found = True
            details.append(software[1])
        if self._results.get('models'):
            found = True
            details.append('Model weights update available. Open Update Models in Settings.')
        self.check_finished.emit(found, '\n\n'.join(details))

    def abandon(self):
        """A dismissed splash must never reroute an already-open dashboard."""
        self._finished = True
        self.deadline.stop()


class SplashScreen(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setFixedSize(480,230)
        self.update_available = False; self.update_details = ''
        self.software_update_available = self.model_update_available = False
        layout = QVBoxLayout(self)
        self.logo = AnimatedTempoLogo(self, width=280)
        layout.addWidget(self.logo, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.lbl_status = QLabel('Starting TEMPO…'); layout.addWidget(self.lbl_status)
        progress = QProgressBar(); progress.setRange(0,0); layout.addWidget(progress)
        self.update_check = StartupUpdateCheck(self)
        self.update_check.check_finished.connect(self._finished)
        self.finished.connect(lambda _: self.update_check.abandon())
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._show_check_status)
        self.finished.connect(self._status_timer.stop)
        self._status_timer.start(100)
        self.update_check.start()

    def _show_check_status(self):
        if self.isVisible():
            self.lbl_status.setText('Checking for updates…')

    def _finished(self, found, details):
        self.update_available, self.update_details = found, details
        software = self.update_check._results.get('software')
        self.software_update_available = bool(software and software[0])
        self.model_update_available = bool(self.update_check._results.get('models'))
        self.accept()


class SoftwareUpdateWizard(QDialog):
    def __init__(self, parent=None, commit_details=''):
        super().__init__(parent)
        self.setWindowTitle('Software Update'); self.resize(620,420)
        layout = QVBoxLayout(self)
        self.log_view = QTextEdit(); self.log_view.setReadOnly(True); self.log_view.setTabChangesFocus(True); layout.addWidget(self.log_view)
        self.log_view.setMarkdown(commit_details or 'Checking the latest software release…')
        self.error_details = ErrorDetails(); layout.addWidget(self.error_details, 1)
        row = QHBoxLayout()
        self.retry = QPushButton('Check Again'); self.retry.clicked.connect(self.check_release); row.addWidget(self.retry)
        row.addStretch()
        self.btn_update = QPushButton('Update and Restart'); self.btn_update.setProperty('primary', True); self.btn_update.clicked.connect(self.run_update); row.addWidget(self.btn_update)
        close = QPushButton('Close'); close.clicked.connect(self.reject); row.addWidget(close); layout.addLayout(row)
        self.btn_update.setEnabled(False)
        QTimer.singleShot(0,self.check_release)

    def check_release(self):
        self.error_details.clear(); self.log_view.show()
        if getattr(sys, 'frozen', False):
            self.log_view.setPlainText('Check GitHub Releases for the latest TEMPO installer. Sign in to GitHub if the repository is private.\n\nDownload the installer, close TEMPO, then run it.')
            self.btn_update.setText('Open Releases')
            self.btn_update.setEnabled(True)
            self.retry.hide()
            return
        self.btn_update.setEnabled(False)
        self.retry.setEnabled(False)
        try:
            found, details = run_read_task(self,'Checking software release…',lambda:software_release(timeout=5))
            self.log_view.setPlainText(details or ('An update is available.' if found else 'Software is up to date.'))
            self.btn_update.setEnabled(found)
        except Exception as exc:
            self.log_view.hide()
            self.error_details.set_error('Could not check software updates. Check your connection and choose Check Again.', exc)
        finally:
            self.retry.setEnabled(True)

    def run_update(self):
        self.error_details.clear()
        if getattr(sys, 'frozen', False):
            if not QDesktopServices.openUrl(QUrl(SOFTWARE_RELEASES_URL)):
                self.error_details.set_error('Could not open GitHub Releases. Open this address in your browser.', SOFTWARE_RELEASES_URL)
            return
        self.btn_update.setEnabled(False)
        self.retry.setEnabled(False)
        recovery = 'The update did not finish. Review the details before checking for updates again.'
        try:
            message = run_task(self,'Updating software and dependencies…',perform_software_update)
            if message is None:
                self.check_release()
                return
            self.log_view.setPlainText(message)
            recovery = 'Software was updated, but TEMPO could not restart. Close TEMPO and open it again to use the update.'
            os.execl(sys.executable,sys.executable,*sys.argv)
        except Exception as exc:
            self.log_view.hide()
            self.error_details.set_error(recovery, exc)
        finally:
            self.retry.setEnabled(True)


class ModelWeightsUpdateWizard(QDialog):
    MODELS = list(MODEL_FILES.items())

    def __init__(self,parent=None):
        super().__init__(parent)
        self.setWindowTitle('TEMPO · Update Models'); self.resize(620,430)
        layout = QVBoxLayout(self)
        heading = QLabel('Published model release')
        heading.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(heading)
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setTabChangesFocus(True); layout.addWidget(self.log)
        self.log.setPlainText('Checking installed models against the published release…')
        self.error_details = ErrorDetails(); layout.addWidget(self.error_details, 1)
        row = QHBoxLayout()
        self.retry = QPushButton('Check Again'); self.retry.clicked.connect(self.audit); row.addWidget(self.retry)
        row.addStretch()
        self.sync = QPushButton('Install Updates'); self.sync.setProperty('primary', True); self.sync.setEnabled(False); self.sync.clicked.connect(self.download_all); row.addWidget(self.sync)
        close = QPushButton('Close'); close.clicked.connect(self.accept); row.addWidget(close)
        layout.addLayout(row)
        self.manifest = None
        QTimer.singleShot(0,self.audit)

    def audit(self):
        self.error_details.clear(); self.log.show()
        self.sync.setEnabled(False)
        self.retry.setEnabled(False)
        self.manifest = None
        def inspect():
            manifest = tempo_hub.model_manifest()
            missing = set(MODEL_FILES.values()) - set(manifest['models'])
            if missing:
                raise RuntimeError('The published release is incomplete. The complete three-model release is required.')
            from tempo_model_catalog import MODEL_LABELS, local_model_path
            labels = {filename:MODEL_LABELS[role] for role,filename in MODEL_FILES.items()}
            lines, updates = [], 0
            for name,record in manifest['models'].items():
                path = local_model_path(name)
                current = os.path.isfile(path) and tempo_hub.calculate_sha256(path) == record['sha256']
                updates += not current
                lines.append(f"{labels.get(name, name)}: {'Up to date' if current else 'Update available'}")
                if record.get("metrics"):
                    lines[-1] += "\n" + format_model_metric(record["metrics"])
            return manifest, '\n\n'.join(lines), updates
        try:
            self.manifest,text,updates = run_read_task(self,'Checking installed models…',inspect)
            self.log.setPlainText(text + ('\n\nAll models are up to date.' if not updates else f'\n\n{updates} models have updates.'))
            self.sync.setEnabled(bool(updates))
        except Exception as exc:
            self.log.hide(); self.sync.setEnabled(False)
            self.error_details.set_error('Could not check model updates. Check your connection and account access, then choose Check Again.', exc)
        finally:
            self.retry.setEnabled(True)

    def download_all(self):
        if not self.manifest:
            return
        self.error_details.clear()
        def download():
            return tempo_hub.download_model_release(self.manifest)
        try:
            run_task(self,'Downloading model weights…',download)
            self.audit()
        except Exception as exc:
            self.error_details.set_error('Model installation did not finish. Review the details, then retry Install Updates.', exc)
