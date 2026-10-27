"""Maintainer workspace: review, lease oversight, and cloud-only training."""
import json
import os
import time
import webbrowser
import re
from pathlib import Path
from urllib.parse import urlparse
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QEvent
from PyQt6.QtGui import QImage, QTextTable, QTextLength, QTextOption, QTextCursor
from PyQt6.QtWidgets import (QApplication, QDialog, QFileDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QListWidget, QListWidgetItem, QLineEdit, QSpinBox, QComboBox, QProgressBar, QTextEdit, QTextBrowser, QScrollArea, QMessageBox, QSplitter, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView)
from tempo_config import tempo_preferences, get_base_dir
from tempo_hub import tempo_hub, expired
from tempo_tasks import run_background
from tempo_ui import confirm_action, format_model_metric, FilenameLabel, ErrorDetails, InformationButton, InlineNotice, BusySpinner, FilePickerRow
from tempo_cloud import training_transaction, resolve_disconnected_run


def colab_url(value):
    parsed = urlparse(value.strip())
    if parsed.scheme != 'https' or parsed.hostname != 'colab.research.google.com' or not parsed.path.startswith(('/drive/', '/github/')):
        raise ValueError('Enter a Colab notebook URL (https://colab.research.google.com/drive/…).')
    return value.strip()


from tempo_model_catalog import MODEL_LABELS
COLAB_HOME = 'https://colab.research.google.com/'


def cloud_failure_summary(action, error):
    """Show actionable server failures before the technical disclosure."""
    code = getattr(getattr(error, 'response', None), 'status_code', None)
    if code == 429:
        reason = 'Hugging Face is limiting requests (HTTP 429). Wait before retrying.'
    elif code == 401:
        reason = 'Hugging Face rejected authentication (HTTP 401). Check the token in Account Setup.'
    elif code == 403:
        reason = 'Hugging Face denied access (HTTP 403). Check maintainer access and token permissions.'
    elif code is not None:
        reason = f'Hugging Face returned HTTP {code}. Show Details for the server response.'
    elif isinstance(error, (PermissionError, ValueError)):
        reason = str(error)
    elif isinstance(error, TimeoutError):
        reason = 'The server did not respond in time. Retry after checking the connection.'
    else:
        reason = 'Show Details for the failure reason.'
    return f'{action} {reason}'


class TrainingWorkflowDialog(QDialog):
    def __init__(self, parent=None, *, source_name='TRAINING_WORKFLOW.md',title='Training Workflow'):
        allowed={'TRAINING_WORKFLOW.md','MODEL_EVALUATION_WORKFLOW.md'}
        if source_name not in allowed:raise ValueError('Unknown training document.')
        source = Path(get_base_dir()) / 'docs'/source_name
        workflow = source.read_text(encoding='utf-8')
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 640)
        layout = QVBoxLayout(self)
        self.reader = QTextBrowser()
        self.reader.setAccessibleName(title)
        self.reader.setOpenLinks(False)
        self.reader.setMarkdown(workflow)
        self.reader.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        block=self.reader.document().begin()
        while block.isValid():
            formatting=block.blockFormat()
            if formatting.nonBreakableLines():
                formatting.setNonBreakableLines(False);QTextCursor(block).setBlockFormat(formatting)
            block=block.next()
        def follow(url):
            name=url.toString()
            if name in allowed:
                dialog=TrainingWorkflowDialog(self,source_name=name,title='Model Evaluation' if name!='TRAINING_WORKFLOW.md' else 'Training Workflow')
                dialog.exec();dialog.deleteLater()
        self.reader.anchorClicked.connect(follow)
        def fit_tables(frame):
            for child in frame.childFrames():
                if isinstance(child, QTextTable):
                    formatting = child.format()
                    formatting.setWidth(QTextLength(QTextLength.Type.PercentageLength, 98))
                    child.setFormat(formatting)
                fit_tables(child)
        fit_tables(self.reader.document().rootFrame())
        layout.addWidget(self.reader, 1)
        close = QPushButton('Close'); close.setAutoDefault(False)
        close.clicked.connect(self.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self.reader.setFocus()


class MaintainerTab(QWidget):
    access_checked = pyqtSignal(bool)
    account_changed = pyqtSignal()
    notebook_settings_requested = pyqtSignal()
    def __init__(self, annotator, parent=None):
        super().__init__(parent)
        self.setObjectName('MaintainerWorkspace')
        self.annotator = annotator
        self.request = None
        self._activity_training_status = {}
        self._polling = False
        self._poll_generation = 0
        self._refresh_requested = False
        self._resolving = False
        self._cancelling = False
        self._pending_writes = 0
        self._allowed = False
        layout = QVBoxLayout(self)
        self.retry_access = QPushButton('Retry Access Check')
        self.retry_access.clicked.connect(self.verify_access)
        self.retry_access.hide()
        layout.addWidget(self.retry_access)
        self.account_setup = QPushButton('Account Setup')
        self.account_setup.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.account_setup.clicked.connect(self.open_account_setup)
        self.account_setup.hide(); layout.addWidget(self.account_setup)
        self.notice = InlineNotice(); self.notice.setText('Verifying maintainer access…'); layout.addWidget(self.notice)
        self.access_error = ErrorDetails(); layout.addWidget(self.access_error)
        self.pages = QTabWidget(); self.pages.setEnabled(False); layout.addWidget(self.pages)
        self.local = QWidget(); local_layout = QVBoxLayout(self.local)
        from annotation_review_dialog import ReviewCanvas
        self.image_filter = QComboBox()
        self.image_filter.addItems(['All annotated images', 'Drafts', 'Approved', 'Awaiting review'])
        self.image_filter.currentIndexChanged.connect(self.refresh_local)
        local_layout.addWidget(self.image_filter)
        self.image_name = FilenameLabel(); self.image_name.hide()
        local_layout.addWidget(self.image_name)
        image_split = QSplitter()
        self.local_list = QListWidget()
        self.local_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.local_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.local_list.currentItemChanged.connect(self.preview_local)
        image_split.addWidget(self.local_list)
        self.local_preview = ReviewCanvas(editable=False)
        image_split.addWidget(self.local_preview)
        image_split.setStretchFactor(1, 2)
        local_layout.addWidget(image_split, 1)
        self.image_status = QLabel('Select an image to inspect its annotations.')
        self.image_status.setWordWrap(True)
        self.image_status.setTextFormat(Qt.TextFormat.PlainText)
        local_layout.addWidget(self.image_status)
        self.image_error = ErrorDetails(); local_layout.addWidget(self.image_error)
        self.image_action_status = QLabel(); self.image_action_status.setWordWrap(True)
        local_layout.addWidget(self.image_action_status)
        self.approve_image = QPushButton('Approve Image')
        self.approve_image.setProperty('primary', True)
        self.approve_image.setEnabled(False)
        self.approve_image.clicked.connect(self.approve_local)
        buttons = QHBoxLayout()
        refresh_images = QPushButton('Refresh')
        refresh_images.clicked.connect(self.refresh_local)
        buttons.addWidget(refresh_images); buttons.addStretch(); buttons.addWidget(self.approve_image)
        local_layout.addLayout(buttons)
        self.pages.addTab(self.local, 'Images')
        pool_page = QWidget(); pool_layout = QVBoxLayout(pool_page)
        self.pool_summary = QLabel('Shared images have not been loaded.'); self.pool_summary.setWordWrap(True)
        pool_layout.addWidget(self.pool_summary)
        self.pool_status = QLabel(''); self.pool_status.setWordWrap(True)
        self.pool_list = QListWidget(); pool_layout.addWidget(self.pool_list)
        self.pool_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.pool_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        pool_actions = QHBoxLayout()
        pool_refresh = QPushButton('Refresh Pool'); pool_refresh.clicked.connect(self.refresh_remote)
        pool_actions.addWidget(pool_refresh); pool_actions.addStretch()
        self.upload_images_button = QPushButton('Add Images'); self.upload_images_button.clicked.connect(self.upload_pool_images)
        pool_actions.addWidget(self.upload_images_button); pool_layout.addLayout(pool_actions)
        pool_layout.addWidget(self.pool_status)
        self.pool_error = ErrorDetails(); pool_layout.addWidget(self.pool_error)
        self.pages.addTab(pool_page, 'Image Pool')
        self.leases_page = QWidget(); leases_layout = QVBoxLayout(self.leases_page)
        self.lease_search = QLineEdit(); self.lease_search.setPlaceholderText('Find an image or collaborator')
        self.lease_search.textChanged.connect(self.filter_leases); leases_layout.addWidget(self.lease_search)
        self.leases = QTableWidget(0, 5)
        self.leases.setWordWrap(False)
        self.leases.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.leases.setHorizontalHeaderLabels(['Image', 'Collaborator', 'Reservation', 'Expires', 'Review'])
        self.leases.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.leases.verticalHeader().hide(); self.leases.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.leases.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        leases_layout.addWidget(self.leases)
        self.lease_summary = QLabel('Reservations have not been loaded.')
        leases_layout.addWidget(self.lease_summary)
        lease_refresh = QPushButton('Refresh Reservations'); lease_refresh.clicked.connect(self.refresh_remote)
        leases_layout.addWidget(lease_refresh)
        self.pages.addTab(self.leases_page, 'Reservations')
        self.cloud = QWidget(); cloud_layout = QVBoxLayout(self.cloud)
        cloud_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.cloud_notice = InlineNotice()
        self.connection_status = QLabel('Training status has not been checked.')
        self.connection_status.setWordWrap(True)
        self.connection_status.hide()
        self.training_workflow = QPushButton('Training Workflow')
        self.training_workflow.clicked.connect(self.open_training_workflow)
        cloud_layout.addWidget(self.training_workflow, alignment=Qt.AlignmentFlag.AlignLeft)
        self.url = QLineEdit(tempo_preferences.get('collaboration','colab_notebook_url',''))
        self.url.setReadOnly(True)
        self.url.setPlaceholderText('https://colab.research.google.com/drive/…')
        cloud_layout.addWidget(QLabel('Notebook URL'))
        self.edit_notebook = QPushButton('Edit Notebook URL')
        self.edit_notebook.clicked.connect(self.notebook_settings_requested.emit)
        cloud_layout.addWidget(FilePickerRow(self.url, self.edit_notebook))
        actions = QHBoxLayout()
        self.reopen = QPushButton('Open Notebook'); self.reopen.clicked.connect(self.open_colab); actions.addWidget(self.reopen)
        self.url.textChanged.connect(self.update_notebook_action)
        self.update_notebook_action()
        self.stop = QPushButton('Cancel Run'); self.stop.setEnabled(False); self.stop.clicked.connect(self.cancel_cloud); actions.addWidget(self.stop)
        actions.addStretch()
        cloud_layout.addLayout(actions)
        cloud_layout.addWidget(self.cloud_notice)
        self.cloud_error = ErrorDetails(); cloud_layout.addWidget(self.cloud_error)
        self.status_error = ErrorDetails(); cloud_layout.addWidget(self.status_error)
        self.reset_stale = QPushButton('Resolve Disconnected Run')
        self.reset_stale.setEnabled(False); self.reset_stale.clicked.connect(self.clear_disconnected)
        self.reset_stale.hide()
        cloud_layout.addWidget(self.reset_stale, alignment=Qt.AlignmentFlag.AlignLeft)
        self.phase = QLabel('No training run.'); self.phase.setWordWrap(True); cloud_layout.addWidget(self.phase)
        cloud_layout.addWidget(self.connection_status)
        self.models = {}
        for name in ('nano_scout','stardist','cellpose'):
            label = QLabel(MODEL_LABELS[name]); bar = QProgressBar(); bar.setRange(0,1); bar.setValue(0)
            label.setWordWrap(True)
            cloud_layout.addWidget(label); cloud_layout.addWidget(bar); self.models[name]=(label,bar)
            label.hide(); bar.hide()
        self.details = QTextEdit(); self.details.setReadOnly(True); self.details.setTabChangesFocus(True); cloud_layout.addWidget(self.details)
        self.details.hide()
        self.cloud_scroll = QScrollArea(); self.cloud_scroll.setWidgetResizable(True); self.cloud_scroll.setWidget(self.cloud)
        self.pages.addTab(self.cloud_scroll,'Cloud training')
        from model_evaluation import ModelEvaluationPage
        self.model_evaluation=ModelEvaluationPage(self)
        self.pages.addTab(self.model_evaluation,'Model Evaluation')
        self.refresh_status = QPushButton('Refresh Status')
        self.refresh_status.clicked.connect(lambda: self.refresh_remote(manual=True))
        refresh_row = QHBoxLayout()
        refresh_row.addWidget(self.refresh_status)
        self.refresh_spinner = BusySpinner()
        refresh_row.addWidget(self.refresh_spinner, alignment=Qt.AlignmentFlag.AlignVCenter)
        refresh_row.addStretch()
        cloud_layout.addLayout(refresh_row)
        self.poll_deadline = QTimer(self); self.poll_deadline.setSingleShot(True)
        self.poll_deadline.setInterval(30000); self.poll_deadline.timeout.connect(self.status_read_timed_out)
        self.timer = QTimer(self); self.timer.setInterval(15000); self.timer.timeout.connect(self.refresh_remote)
        self.pages.currentChanged.connect(self.review_tab_opened)
        if hasattr(self.annotator, 'submission_completed'):
            self.annotator.submission_completed.connect(self.submission_changed)
        QTimer.singleShot(0, self.verify_access)

    def open_training_workflow(self):
        try:
            dialog = TrainingWorkflowDialog(self)
        except OSError as error:
            self.cloud_error.set_error('Training workflow could not be opened.', error)
            return
        dialog.exec()
        dialog.deleteLater()

    def review_tab_opened(self, _index):
        if self.pages.currentWidget() is self.model_evaluation and self._allowed:
            self.model_evaluation.refresh_list()
        if hasattr(self, 'review') and self.pages.currentWidget() is self.review:
            self.review.request_refresh()

    def submission_changed(self):
        if hasattr(self, 'review'):
            self.review.request_refresh()
        self.refresh_remote()

    def review_finished(self):
        self.refresh_remote()
        self.annotator.refresh_universal_file_list()

    def upload_pool_images(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'Add Images to Shared Pool', '', 'Images (*.png *.jpg *.jpeg *.tif *.tiff)')
        if not paths:
            return
        self.pool_error.clear()
        names = [Path(path).name for path in paths]
        if len(set(names)) != len(names):
            self.pool_status.setText('Two selected images have the same filename. Rename one before uploading.'); return
        unreadable = [Path(path).name for path in paths if QImage(path).isNull()]
        if unreadable:
            self.pool_status.setText('Unable to read: ' + ', '.join(unreadable)); return
        if not confirm_action(self, 'Add Images to Shared Pool',
                f'{len(paths)} images will become available for collaborators to reserve and annotate. Existing images cannot be overwritten.', 'Upload Images'):
            return
        self.upload_images_button.setEnabled(False)
        self.pool_status.setText(f'Uploading {len(paths)} images…')
        def ready(result, error):
            self.upload_images_button.setEnabled(True)
            if error or not result[0]:
                self.pool_status.setText('Image upload did not finish.')
                self.pool_error.set_error('Review the details and refresh the pool before retrying Add Images. Some selected images may already have uploaded.', error or RuntimeError(result[1]))
                return
            self.pool_status.setText(f'{result[0]} images added to the shared pool.')
            self.refresh_remote()
        self.run_write(lambda: tempo_hub.upload_raw_images(paths), ready)

    def run_write(self, action, completed):
        self._pending_writes += 1
        def settled(value, error):
            try:
                completed(value, error)
            finally:
                self._pending_writes -= 1
        try:
            run_background(action, settled)
        except Exception:
            self._pending_writes -= 1
            raise

    def pending_work(self):
        return bool(self._pending_writes)

    def open_account_setup(self):
        from setup_wizard import SetupWizardDialog
        dialog = SetupWizardDialog(self, account_only=True)
        dialog.account_saved.connect(self.account_changed.emit)
        dialog.account_saved.connect(self.verify_access)
        dialog.exec()

    def verify_access(self):
        self._activity_training_status = {}
        self.model_evaluation.set_allowed(False)
        self.invalidate_status_read()
        self.access_error.clear()
        self.pages.setEnabled(False)
        self.timer.stop()
        self.retry_access.setEnabled(False)
        run_background(lambda: tempo_hub.is_maintainer(raise_on_error=True), self.access_ready)

    def access_ready(self, allowed, error):
        self.retry_access.setEnabled(True)
        self._allowed = bool(allowed) and error is None
        self.model_evaluation.set_allowed(self._allowed)
        self.access_checked.emit(self._allowed or error is not None)
        self.retry_access.setVisible(not self._allowed)
        self.account_setup.setVisible(not self._allowed)
        self.pages.setEnabled(self._allowed)
        if not self._allowed:
            self.notice.setText('Unable to verify maintainer access.' if error else 'This account does not have maintainer access.')
            if error:
                self.access_error.set_error('Check your connection and choose Retry Access Check. Use Account Setup to change credentials.', error)
            else:
                self.access_error.clear()
            return
        self.access_error.clear()
        self.notice.clear()
        from annotation_review_dialog import AnnotationReviewDialog
        if not hasattr(self, 'review'):
            self.review = AnnotationReviewDialog(self, verified=True)
            self.review.setWindowFlags(Qt.WindowType.Widget)
            self.review.review_completed.connect(self.review_finished)
            self.pages.insertTab(1,self.review,'Review Submissions')
        else:
            self.review.request_refresh()
        self.refresh_local(); self.refresh_remote(); self.timer.start()

    def refresh_local(self):
        current = self.local_list.currentItem()
        selected = current.data(Qt.ItemDataRole.UserRole) if current else None
        self.local_list.clear()
        for name, record in sorted(self.annotator.universal_annotations.items(), key=lambda pair: [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', pair[1].get('original_filename') or pair[0])]):
            if not record.get('boxes'):
                continue
            state = self.image_review_state(name, record)
            approved = state == 'approved'
            if self.image_filter.currentIndex() == 1 and state in ('approved', 'submitted'):
                continue
            if self.image_filter.currentIndex() == 2 and not approved:
                continue
            if self.image_filter.currentIndex() == 3 and state != 'submitted':
                continue
            label = {'approved':'Approved', 'submitted':'Awaiting review'}.get(state, 'Draft')
            count = len(record['boxes'])
            summary = f"{label} · {count} {'annotation' if count == 1 else 'annotations'}"
            item = QListWidgetItem(f"{record.get('original_filename') or name}\n{summary}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setToolTip(f"{record.get('original_filename') or name}\n{summary}")
            self.local_list.addItem(item)
            if name == selected:
                self.local_list.setCurrentItem(item)

        if not self.local_list.count():
            self.image_status.setText('No annotated images match this filter.' if self.image_filter.currentIndex() else 'No annotated images are available locally. Open an image in Segmentation Annotation to begin.')

    def image_review_state(self, name, record):
        remote = getattr(self, '_remote_images', {}).get(name)
        if remote is None:
            remote = getattr(self.annotator, 'lock_manifest', {}).get('images', {}).get(name, {})
        return remote.get('status', record.get('status', 'draft'))

    def update_notebook_action(self):
        self.reopen.setEnabled(True)
        self.reopen.setAccessibleDescription('Open the linked notebook in the default browser.' if self.url.text().strip()
                                             else 'Open Colab to choose a notebook.')

    def preview_local(self, current, previous=None):
        self.image_status.setToolTip('')
        self.image_error.clear()
        self.approve_image.setEnabled(False)
        self.local_preview.clear_image()
        if current is None:
            self.image_name.clear(); self.image_name.hide()
            self.image_status.setText('Select an image to inspect its annotations.')
            return
        name = current.data(Qt.ItemDataRole.UserRole)
        record = self.annotator.universal_annotations[name]
        self.image_name.setText(record.get('original_filename') or name); self.image_name.show()
        from tempo_backend import UNIVERSAL_DATASET_DIR
        try:
            self.local_preview.set_image_and_masks(str(Path(UNIVERSAL_DATASET_DIR) / name), record)
        except Exception as exc:
            self.image_status.setText('Image preview unavailable.')
            self.image_error.set_error('The local image could not be displayed. Check that the file is available, then select the image again.', exc)
            return
        state = self.image_review_state(name, record)
        label = {'approved':'Approved', 'submitted':'Awaiting review'}.get(state, 'Draft')
        count = len(record['boxes'])
        self.image_status.setText(f"{label} · {count} {'annotation' if count == 1 else 'annotations'}")
        if state == 'submitted':
            self.image_status.setText(self.image_status.text() + '\nOpen Review Submissions to review this image with its submission.')
        self.approve_image.setEnabled(state not in ('approved', 'submitted'))
        reservation = getattr(self, '_remote_images', {}).get(name)
        if reservation is None:
            reservation = getattr(self.annotator, 'lock_manifest', {}).get('images', {}).get(name, {})
        if state in ('locked', 'submitted') and reservation.get('claimed_by'):
            owner_label = 'Last reserved by' if expired(reservation) else 'Reserved by'
            owner = reservation['claimed_by']
            self.image_status.setText(self.image_status.text() + f"\n{owner_label}: {tempo_hub.display_collaborator(owner, record=reservation, profiles=getattr(self, '_collaborators', {}))}")
            self.image_status.setToolTip(owner)
        elif record.get('draft_owner') and state != 'approved':
            owner = record['draft_owner']
            profiles = getattr(self, '_collaborators', {}) or getattr(self.annotator, 'lock_manifest', {}).get('collaborators', {})
            name = tempo_hub.display_collaborator(owner, record=record, profiles=profiles)
            self.image_status.setText(self.image_status.text() + f'\nLocal draft by: {name}')
            self.image_status.setToolTip(owner)
        if state == 'locked' and reservation and not tempo_hub._owned(reservation, getattr(self.annotator, 'collaborator_id', tempo_hub.get_collaborator_id())):
            self.approve_image.setEnabled(False)
            self.image_status.setText(self.image_status.text() + '\nThis image is not reserved by you.')

    def approve_local(self):
        current = self.local_list.currentItem()
        if current is None or not self.approve_image.isEnabled():
            return
        self.image_error.clear()
        self.image_action_status.clear()
        try:
            approved = self.annotator.approve_selected_images(names=[current.data(Qt.ItemDataRole.UserRole)])
        except Exception as exc:
            if getattr(self.annotator, 'approval_remote_succeeded', False):
                self.approve_image.setEnabled(False)
                message = 'Images were approved, but the local update failed. Annotations remain locked. Refresh image statuses before continuing.'
            else:
                message = 'Approval did not finish. Refresh image statuses before retrying; remote approval may already have succeeded.'
            self.image_error.set_error(message, exc)
            return
        self.image_action_status.setText(self.annotator.submission_status.text())
        if approved:
            self.refresh_local()

    def open_colab(self):
        self.cloud_error.clear()
        try:
            provided = self.url.text().strip()
            url = colab_url(provided) if provided else COLAB_HOME
            if not webbrowser.open(url):
                raise OSError('Unable to open the browser. Open the notebook URL manually.')
            if not provided:
                self.cloud_notice.setText('Colab opened. Choose a saved notebook or upload the TEMPO notebook.')
        except ValueError as exc:
            self.cloud_error.set_error(str(exc), exc)
        except Exception as exc:
            self.cloud_error.set_error('The browser could not be opened. Open the notebook link or colab.research.google.com directly.', exc)

    def refresh_notebook_link(self):
        self.url.setText(tempo_preferences.get('collaboration', 'colab_notebook_url', ''))

    def read_context(self):
        return tempo_hub.token, tempo_hub.get_dataset_repo(), tempo_hub.get_models_repo()

    def invalidate_status_read(self):
        self._poll_generation += 1
        self._polling = False
        self._refresh_requested = False
        self.poll_deadline.stop()
        self.refresh_spinner.set_busy(False)

    def status_read_timed_out(self):
        if not self._polling:
            return
        self.invalidate_status_read()
        self.remote_ready(None, TimeoutError('Status fetch did not finish within 30 seconds.'))

    def refresh_remote(self, *, manual=False):
        if not self._allowed:
            return
        if self._polling:
            self._refresh_requested |= manual
            return
        self._poll_generation += 1
        generation, context = self._poll_generation, self.read_context()
        self._polling=True
        self.refresh_spinner.set_busy(True)
        self.poll_deadline.start()
        status_only = self.pages.currentWidget() is self.cloud_scroll
        def fetch():
            revision=tempo_hub._head()
            request=tempo_hub._read('training/request.json',revision,{})
            status=tempo_hub._read(f"training/{request['id']}/status.json",revision,{}) if request else {}
            return request,status,None if status_only else tempo_hub._read('lock_manifest.json',revision,{'images':{}})
        def ready(result, error):
            if generation != self._poll_generation:
                return
            if context != self.read_context():
                self.invalidate_status_read()
                self.verify_access()
                return
            pending = self._refresh_requested
            self._refresh_requested = False
            self.remote_ready(result, error)
            if pending:
                QTimer.singleShot(0, lambda: self.refresh_remote(manual=True))
        try:
            run_background(fetch,ready)
        except Exception as error:
            ready(None, error)

    def remote_ready(self,result,error):
        self._polling=False
        self.poll_deadline.stop()
        self.refresh_spinner.set_busy(False)
        if error:
            self._activity_training_status = {}
            self.status_error.set_error(cloud_failure_summary('Run status could not be checked.', error) +
                ' Choose Refresh Status to retry.', error, secrets=(tempo_hub.token,))
            self.phase.setText('Unable to refresh run status.'); self.stop.setEnabled(False); self.reset_stale.setEnabled(False); return
        self.status_error.clear()
        self.request,status,manifest=result
        if manifest is not None:
            self.apply_remote_images(manifest)
        self.apply_cloud_status(status)

    def apply_remote_images(self, manifest):
        profiles_changed = getattr(self, '_collaborators', {}) != manifest.get('collaborators', {})
        self._collaborators = manifest.get('collaborators', {})
        if profiles_changed or getattr(self, '_remote_images', None) != manifest.get('images', {}):
            self._remote_images = manifest.get('images', {})
            self.refresh_local()
            if hasattr(self, 'review'):
                self.review.request_refresh()
        self.pool_list.clear()
        def original_name(name, record):
            return record.get('original_filename') or self.annotator.universal_annotations.get(name, {}).get('original_filename') or name
        ordered_images = sorted(manifest.get('images', {}).items(), key=lambda pair:[int(v) if v.isdigit() else v.lower() for v in re.split(r'(\d+)', original_name(*pair))])
        for name, record in ordered_images:
            state = record.get('status', 'available')
            status_text = {'available':'Available', 'locked':'Reserved', 'submitted':'Awaiting review', 'approved':'Approved'}.get(state, state)
            if state in ('locked', 'submitted') and expired(record): status_text = 'Available to adopt'
            item = QListWidgetItem(f"{original_name(name, record)}\n{status_text}")
            item.setToolTip(original_name(name, record)); self.pool_list.addItem(item)
        self.pool_summary.setText(f'{self.pool_list.count()} shared images.' if self.pool_list.count() else 'The shared image pool is empty. Add images for collaborators to annotate.')
        self.leases.setRowCount(0)
        for name,record in ordered_images:
            state=record.get('status','available')
            if state in ('locked','submitted') and expired(record): state='expired · adoptable now'
            if state == 'approved' or state == 'available':
                continue
            original = original_name(name, record)
            expiry = record.get('expires_at')
            try:
                from datetime import datetime
                expiry = datetime.fromisoformat(expiry).astimezone().strftime('%b %d, %Y at %H:%M') if expiry else 'Unknown'
            except (ValueError, TypeError):
                expiry = 'Unknown'
            row = self.leases.rowCount(); self.leases.insertRow(row)
            state = {'locked':'Active', 'submitted':'Active', 'expired · adoptable now':'Available to adopt'}.get(state,state)
            owner = record.get('claimed_by')
            label = tempo_hub.display_collaborator(owner, record=record, profiles=self._collaborators)
            for column, value in enumerate([original, label, state, expiry, 'Awaiting review' if record.get('status') == 'submitted' else 'Draft']):
                item = QTableWidgetItem(value); item.setToolTip(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column == 1:
                    item.setToolTip(f'{label}\n{owner}' if owner else 'Unassigned')
                    item.setData(Qt.ItemDataRole.UserRole, owner)
                self.leases.setItem(row,column,item)
        self.leases.horizontalHeader().setMinimumSectionSize(max(
            self.leases.fontMetrics().horizontalAdvance(self.leases.horizontalHeaderItem(column).text()) + 24
            for column in range(self.leases.columnCount())))
        self.leases.resizeRowsToContents()
        self.filter_leases()

    def apply_cloud_status(self, status):
        self._activity_training_status = dict(status)
        phase=status.get('phase','No cloud request')
        age=max(0,int(time.time()-status.get('updated_at',time.time())))
        stale=f' · last heartbeat {age}s ago; check Colab' if age>60 and phase not in ('completed','failed','cancelled','awaiting_colab','disconnected') else ''
        seconds = int(status.get('elapsed_seconds',0)); minutes, seconds = divmod(seconds,60)
        phase_label = {'checking_runtime':'Checking GPU and packages', 'preparing_dataset':'Preparing images',
                       'preparing_baselines':'Loading current models', 'checking_training_steps':'Running training trial',
                       'publishing':'Saving results and model updates'}.get(phase,phase.replace('_',' ').capitalize())
        self.phase.setText(f"{phase_label} · GPU: {status.get('gpu','Not assigned')} · {minutes}:{seconds:02d} elapsed{stale}" if self.request else 'No training run.')
        self.connection_status.setVisible(bool(self.request) and phase not in ('completed','failed','cancelled','disconnected'))
        if self.request:
            self.connection_status.setText(f'Last status update: {age}s ago.' if age <= 60 else
                                           f'No status update for {age}s. Check Colab.')
        for name,(label,bar) in self.models.items():
            label.setVisible(bool(self.request)); bar.setVisible(bool(self.request))
            trial = phase == 'checking_training_steps'
            display_name = MODEL_LABELS[name] + (' · Trial' if trial else '')
            model=status.get('models',{}).get(name,{})
            state=model.get('phase','waiting'); epoch=model.get('epoch'); total=model.get('total')
            state_label = {'waiting':'Waiting', 'preparing':'Preparing data', 'training':'Training', 'validating':'Validating', 'completed':'Complete'}.get(state, state.replace('_',' ').capitalize())
            label.setText(f"{display_name} · {state_label}" + (f' · epoch {epoch}/{total}' if state == 'training' and epoch is not None and total else ''))
            if phase in ('failed','cancelled','disconnected') and state != 'completed':
                outcome = phase.capitalize() if model else 'No result reported'
                label.setText(f"{display_name} · {outcome}" + (f' · last epoch {epoch}/{total}' if epoch is not None and total else ''))
                bar.setRange(0,1); bar.setValue(0); bar.setFormat(outcome)
            elif state=='completed': bar.setRange(0,1); bar.setValue(1); bar.setFormat('Trial complete' if trial else 'Training and validation complete')
            elif state in ('validating', 'validation'): bar.setRange(0,0); bar.setFormat('Validating')
            elif epoch is not None and total: bar.setRange(0,total); bar.setValue(epoch); bar.setFormat(f'Epoch {epoch} of {total}')
            elif state=='waiting': bar.setRange(0,1); bar.setValue(0); bar.setFormat('')
            elif phase in ('failed','cancelled','disconnected'): bar.setRange(0,1); bar.setValue(0); bar.setFormat(phase)
            else: bar.setRange(0,0)
        metrics = status.get('metrics',{})
        detail_lines = []
        if status.get('error'):
            detail_lines.append('Run stopped: ' + str(status['error']))
        if metrics:
            detail_lines.extend(f"{MODEL_LABELS.get(name,name)} — {format_model_metric(record)}" for name,record in metrics.items())
            basis = status.get('evaluation_basis', 'held-out-images')
            detail_lines.append('Scores use held-out images.' if basis == 'held-out-images' else 'Scores use training images and are diagnostic only.')
            for name, outcome in status.get('model_outcomes', {}).items():
                previous = outcome.get('baseline_metrics')
                previous_score = format_model_metric(previous) if previous else 'Unavailable'
                detail_lines.append(f"{MODEL_LABELS.get(name,name)}: {outcome['outcome']} · Previous model score: {previous_score}")
        self.details.setPlainText('\n\n'.join(detail_lines))
        self.details.setVisible(bool(metrics) or bool(status.get('error')))
        self.stop.setEnabled(not self._resolving and not self._cancelling and bool(self.request) and phase not in ('completed','failed','cancelled','publishing','disconnected'))
        self.update_notebook_action()
        recoverable = bool(self.request) and age>120 and phase not in ('completed','failed','cancelled','awaiting_colab','disconnected')
        self.reset_stale.setEnabled(recoverable and not self._resolving and not self._cancelling)
        self.reset_stale.setVisible(recoverable or self._resolving)
        if phase=='completed':
            count = status.get('updated_count')
            self.cloud_notice.setText(f'Current models updated: {count}.' if count is not None else 'Run completed.')
        elif self.cloud_notice.text() == 'Run completed.' or self.cloud_notice.text().startswith('Current models updated:'):
            self.cloud_notice.clear()

    def filter_leases(self):
        query = self.lease_search.text().strip().casefold()
        visible = 0
        for row in range(self.leases.rowCount()):
            values = ' '.join(self.leases.item(row,col).text() + ' ' + self.leases.item(row,col).toolTip()
                              for col in range(self.leases.columnCount()))
            matches = query in values.casefold()
            self.leases.setRowHidden(row, not matches)
            visible += matches
        count = self.leases.rowCount()
        if query:
            self.lease_summary.setText(f'{visible} of {count} reservations match.')
        else:
            self.lease_summary.setText(f"{count} {'reservation' if count == 1 else 'reservations'}" if count else 'No active or expired reservations.')

    def cancel_cloud(self):
        if not self.request or self._cancelling or self._resolving:return
        self._cancelling = True
        self.stop.setEnabled(False); self.stop.setText('Requesting…')
        self.reset_stale.setEnabled(False)
        self.cloud_error.clear()
        request_id=self.request['id']
        def cancel():
            tempo_hub.ensure_credentials()
            if not tempo_hub.is_maintainer(raise_on_error=True):raise PermissionError('Maintainer access is required to cancel a run.')
            def mutate(manifest,revision):
                path=f'training/{request_id}/status.json'; state=tempo_hub._read(path,revision,{})
                if state.get('phase') in ('completed','failed','cancelled','publishing','disconnected'):
                    raise ValueError('This run has finished or publication has already started. Refresh its status.')
                files={f'training/{request_id}/control.json':json.dumps({'cancel':True}).encode()}
                if state.get('phase')=='awaiting_colab':
                    state['phase']='cancelled'; files[path]=json.dumps(state).encode()
                return files,True
            return training_transaction(tempo_hub, mutate, 'Cancel Colab training run')
        def ready(_, error):
            self._cancelling = False
            self.stop.setText('Cancel Run')
            if error:
                self.cloud_error.set_error(cloud_failure_summary('Cancellation could not be confirmed.', error) +
                    ' Refresh Status before retrying Cancel Run. To stop immediately, interrupt the cell in Colab.',
                    error, secrets=(tempo_hub.token,))
            else:
                self.cloud_notice.setText('Cancellation requested. Running Colab checks within about 10 seconds; if disconnected, stop the cell in Colab.')
            self.refresh_remote(manual=True)
        try:
            self.run_write(cancel, ready)
        except Exception as error:
            ready(None, error)

    def clear_disconnected(self):
        if not self.request or self._resolving or self._cancelling:return
        request_id = self.request['id']
        context = self.read_context()
        if not confirm_action(self, 'Resolve Disconnected Run',
            'Clear the run record only after Colab execution has stopped. This permits another run but does not '
            'stop a Colab process. Saved logs, results and model files are kept.', 'Clear Run Record'):
            return
        if context != self.read_context():
            self.cloud_error.set_error('Account changed. Check access before resolving the run.', PermissionError('Account or repositories changed.'))
            return
        self.invalidate_status_read()
        self._resolving = True
        self.reset_stale.setEnabled(False); self.reset_stale.setText('Resolving…')
        self.stop.setEnabled(False)
        self.cloud_notice.setText('Checking the latest run record…')
        self.cloud_error.clear()
        def ready(result, error):
            self._resolving = False
            self.reset_stale.setText('Resolve Disconnected Run')
            if context != self.read_context():
                self.verify_access()
                return
            if error:
                self.cloud_notice.setText('Run record was not confirmed cleared.')
                self.cloud_error.set_error(cloud_failure_summary('Run recovery failed.', error) +
                    ' Refresh Status to check the record before retrying Resolve Disconnected Run.',
                    error, secrets=(tempo_hub.token,))
                self.refresh_remote(manual=True)
            else:
                self.invalidate_status_read()
                self.remote_ready((result['request'], result['status'], None), None)
                self.cloud_notice.setText({'cleared':'Run record cleared. Run the notebook to start another run.',
                    'finished':'The run has already finished. Its latest status is shown.',
                    'changed':'The active run changed. Its latest status is shown; no record was cleared.'}[result['outcome']])
        try:
            self.run_write(lambda: resolve_disconnected_run(tempo_hub, request_id, expected_context=context), ready)
        except Exception as error:
            ready(None, error)
