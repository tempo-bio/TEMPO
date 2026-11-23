"""Acquisition's capture, labeling, and held-out focus review window."""

from pathlib import Path
import time

import cv2
from PyQt6.QtCore import QSignalBlocker, QTimer, Qt
from PyQt6.QtGui import QColor, QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog, QGraphicsScene,
                             QGraphicsView, QHeaderView, QHBoxLayout, QLabel, QListWidget,
                             QListWidgetItem, QPushButton, QTabWidget, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from tempo_release import APP_DISPLAY_NAME
from tempo_focus_calibration import (FocusCalibration, FocusSettings, LABELS, previous_profile,
                                     read_focus_reference, update_profile)
from tempo_recording import display_frame
from tempo_tasks import run_background, run_inline_task
from tempo_ui import ErrorDetails, FlowRow, InformationButton, confirm_action, get_current_theme, help_controller

USER_ROLE = Qt.ItemDataRole.UserRole


def plane_name(plane, index):
    if plane['z_index'] is not None:
        return f"Z index {plane['z_index']:+d}"
    if plane['source']:
        return plane['source']
    return f'Plane {index + 1}'


class PlaneView(QWidget):
    def __init__(self, accessible_name):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        toolbar.addWidget(self.title, 1)
        self.zoom = QComboBox()
        self.zoom.setAccessibleName(f'{accessible_name} magnification')
        self.zoom.addItems(('Fit Image', 'Actual Pixels'))
        self.zoom.setEnabled(False)
        self.zoom.currentIndexChanged.connect(self._adjust)
        toolbar.addWidget(self.zoom)
        layout.addLayout(toolbar)
        self.scene = QGraphicsScene(self)
        self.image = self.scene.addPixmap(QPixmap())
        self.requested = None
        self.loading = False
        self.view = QGraphicsView(self.scene)
        self.view.setAccessibleName(accessible_name)
        self.view.setMinimumSize(180, 150)
        self.view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        layout.addWidget(self.view, 1)
        self.refit = QTimer(self)
        self.refit.setSingleShot(True)
        self.refit.timeout.connect(self._adjust)

    def set_frame(self, frame=None, bits=None, title=''):
        self.title.setText(title)
        if frame is None:
            pixmap = QPixmap()
        else:
            pixels = display_frame(frame, bits)
            if pixels.ndim == 2:
                mode = QImage.Format.Format_Grayscale8
            else:
                pixels = cv2.cvtColor(pixels, cv2.COLOR_BGRA2RGB if pixels.shape[2] == 4 else cv2.COLOR_BGR2RGB)
                mode = QImage.Format.Format_RGB888
            image = QImage(pixels.data, pixels.shape[1], pixels.shape[0], pixels.strides[0], mode)
            pixmap = QPixmap.fromImage(image)
        old_size = self.image.pixmap().size()
        self.image.setPixmap(pixmap)
        self.zoom.setEnabled(not pixmap.isNull())
        if pixmap.size() != old_size:
            self.scene.setSceneRect(self.image.boundingRect())
            self.refit.start(0)

    def _adjust(self):
        actual_pixels = self.zoom.currentIndex() == 1
        scroll = Qt.ScrollBarPolicy.ScrollBarAsNeeded if actual_pixels else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        self.view.setHorizontalScrollBarPolicy(scroll)
        self.view.setVerticalScrollBarPolicy(scroll)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag if actual_pixels else QGraphicsView.DragMode.NoDrag)
        self.view.resetTransform()
        if not actual_pixels and not self.image.pixmap().isNull():
            self.view.fitInView(self.image, Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.zoom.currentIndex() == 0:
            self.refit.start(0)

    def showEvent(self, event):
        super().showEvent(event)
        self.refit.start(0)


class FocusCalibrationDialog(QDialog):
    def __init__(self, parent, *, data_dir, config_dir, camera_action, camera_available,
                 current_well, current_z, profile_for, can_apply, on_profile):
        super().__init__(parent)
        self.setWindowTitle(f'{APP_DISPLAY_NAME} · Focus Calibration')
        self.resize(1040, 670)
        self.data_dir = Path(data_dir)
        self.config_dir = Path(config_dir)
        self.camera_action = camera_action
        self.camera_available = camera_available
        self.current_well = current_well
        self.current_z = current_z
        self.profile_for = profile_for
        self.can_apply = can_apply
        self.on_profile = on_profile
        self.store = None
        self.draft = {}
        self.outcomes = []
        self.has_previous = False
        self.live = True
        self.last_live_at = 0.0
        self._preview_signature = None
        self._busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        header = FlowRow()
        self.settings = QLabel()
        self.settings.hide()
        header.flow.addWidget(self.settings)
        self.use_camera = QPushButton('Use Camera Settings')
        self.use_camera.clicked.connect(self.select_camera)
        header.flow.addWidget(self.use_camera)
        self.use_metadata = QPushButton('Use Recording Metadata…')
        self.use_metadata.clicked.connect(self.select_metadata)
        header.flow.addWidget(self.use_metadata)
        header.flow.addWidget(InformationButton('Calibration settings',
            'Keep the objective, illumination, exposure, gain and binning fixed. '
            'Use recording metadata to calibrate earlier TIFFs with their original camera settings.'))
        layout.addWidget(header)

        self.error = ErrorDetails()
        layout.addWidget(self.error)
        self.pending = QLabel()
        self.pending.setWordWrap(True)
        self.pending.hide()
        layout.addWidget(self.pending)
        self.tabs = QTabWidget()
        self.capture_page = self._capture_page()
        self.review_page = self._review_page()
        self.tabs.addTab(self.capture_page, 'Capture and Label')
        self.tabs.addTab(self.review_page, 'Review Results')
        self.tabs.currentChanged.connect(self.update_actions)
        layout.addWidget(self.tabs, 1)
        QShortcut(QKeySequence.StandardKey.Close, self, activated=self.close)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.escape)
        self.update_actions()

    @property
    def operation_in_progress(self):
        return self._busy

    def reject(self):
        if not self._busy:
            super().reject()

    def escape(self):
        help_popup = help_controller()
        if help_popup.source is not None and help_popup.source.window() is self:
            help_popup.dismiss()
        else:
            self.reject()

    def closeEvent(self, event):
        if self._busy:
            event.ignore()
        else:
            super().closeEvent(event)

    def _capture_page(self):
        page = QWidget()
        body = QVBoxLayout(page)
        content = QHBoxLayout()
        rail = QWidget()
        rail.setMinimumWidth(175)
        rail.setMaximumWidth(280)
        navigation = QVBoxLayout(rail)
        navigation.setContentsMargins(0, 0, 0, 0)
        navigation.addWidget(QLabel('Fields'))
        self.fields = QListWidget()
        self.fields.setAccessibleName('Calibration fields')
        self.fields.currentItemChanged.connect(self.select_field)
        self.fields.setMinimumHeight(75)
        self.fields.setMaximumHeight(145)
        navigation.addWidget(self.fields)
        self.no_fields = QLabel('No fields yet.')
        navigation.addWidget(self.no_fields)
        self.new_field = QPushButton('New Field')
        self.new_field.clicked.connect(self.add_field)
        navigation.addWidget(self.new_field, 0, Qt.AlignmentFlag.AlignLeft)
        self.partition = QComboBox()
        self.partition.setAccessibleName('Use selected field')
        self.partition.addItem('Use to fit', 'train')
        self.partition.addItem('Hold back for checking', 'test')
        self.partition.currentIndexChanged.connect(self.edit_partition)
        navigation.addWidget(self.partition)
        self.planes_title = QLabel('Planes')
        navigation.addWidget(self.planes_title)
        self.planes = QListWidget()
        self.planes.setAccessibleName('Focal planes')
        self.planes.currentItemChanged.connect(self.select_plane)
        self.planes.setMinimumHeight(75)
        self.planes.setMaximumHeight(165)
        navigation.addWidget(self.planes)
        self.no_planes = QLabel('No planes in this field.')
        navigation.addWidget(self.no_planes)
        navigation.addStretch(1)
        content.addWidget(rail)
        detail = QVBoxLayout()
        self.capture_view = PlaneView('Calibration image')
        detail.addWidget(self.capture_view, 1)
        tools = FlowRow()
        self.show_live = QPushButton('Show Live Preview')
        self.show_live.clicked.connect(self.set_live)
        tools.flow.addWidget(self.show_live)
        self.take_plane = QPushButton('Capture Plane')
        self.take_plane.clicked.connect(self.capture_plane)
        tools.flow.addWidget(self.take_plane)
        self.import_tiffs = QPushButton('Import TIFFs…')
        self.import_tiffs.clicked.connect(self.import_planes)
        tools.flow.addWidget(self.import_tiffs)
        self.labels = QComboBox()
        self.labels.setAccessibleName('Your focus label for the selected plane')
        for key in (None, 'sharp', 'out_of_focus', 'skip'):
            self.labels.addItem('Unlabeled' if key is None else LABELS[key], key)
        self.labels.currentIndexChanged.connect(self.edit_label)
        label_row = QWidget()
        label_layout = QHBoxLayout(label_row)
        label_layout.setContentsMargins(0, 0, 0, 0)
        label_title = QLabel('Your label')
        label_title.setBuddy(self.labels)
        label_layout.addWidget(label_title)
        label_layout.addWidget(self.labels)
        tools.flow.addWidget(label_row)
        detail.addWidget(tools)
        content.addLayout(detail, 1)
        body.addLayout(content, 1)
        footer = QHBoxLayout()
        self.instructions = QLabel()
        self.instructions.setWordWrap(True)
        footer.addWidget(self.instructions, 1)
        self.fit_button = QPushButton('Fit and Check')
        self.fit_button.clicked.connect(self.fit)
        footer.addWidget(self.fit_button)
        body.addLayout(footer)
        return page

    def _review_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        body = QHBoxLayout()
        detail = QVBoxLayout()
        self.review_view = PlaneView('Held-out focus image')
        detail.addWidget(self.review_view, 1)
        self.comparison = QLabel()
        self.comparison.setWordWrap(True)
        detail.addWidget(self.comparison)
        body.addLayout(detail, 11)
        results = QVBoxLayout()
        results.addWidget(QLabel('Check results'))
        self.results = QTableWidget(0, 4)
        self.results.setAccessibleName('Held-out field results')
        self.results.setHorizontalHeaderLabels(('Field', 'Plane', 'Your label', APP_DISPLAY_NAME))
        self.results.verticalHeader().hide()
        header = self.results.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        font = self.results.fontMetrics()
        field_width = font.horizontalAdvance('A01 · 100') + 12
        plane_width = font.horizontalAdvance('Z +999') + 12
        label_width = font.horizontalAdvance('Out of focus') + 12
        for index, width in enumerate((field_width, plane_width)):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Fixed)
            self.results.setColumnWidth(index, width)
        self.results.setMinimumWidth(field_width + plane_width + 2 * label_width + 8)
        self.results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.currentCellChanged.connect(self.select_result)
        results.addWidget(self.results, 1)
        body.addLayout(results, 10)
        outer.addLayout(body, 1)
        footer = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        footer.addWidget(self.summary, 1)
        self.restore_button = QPushButton('Restore Previous Profile…')
        self.restore_button.clicked.connect(self.restore_previous)
        footer.addWidget(self.restore_button)
        self.apply_button = QPushButton('Apply Profile')
        self.apply_button.clicked.connect(self.apply)
        footer.addWidget(self.apply_button)
        outer.addLayout(footer)
        return page

    def _run(self, action, done):
        if self._busy:
            return
        self.error.clear()
        self._busy = True
        self.update_actions()
        try:
            value = action()
            if value is not None:
                done(value)
        except Exception as exc:
            self.error.set_error(str(exc) or 'The focus calibration operation failed.', exc)
        finally:
            self._busy = False
            self.update_actions()

    def _work(self, title, action, done):
        if self._busy:
            return
        self.error.clear()
        self._busy = True
        self.pending.setText(title + '…')
        self.pending.show()
        self.update_actions()

        def completed(value, error):
            try:
                if error:
                    raise error
                done(value)
            except Exception as exc:
                self._sync_choices()
                self.error.set_error(str(exc) or 'The focus calibration operation failed.', exc)
            finally:
                self._busy = False
                self.pending.hide()
                self.update_actions()

        try:
            run_inline_task(self, title, action, completed, wait_status=self.pending)
        except Exception as exc:
            completed(None, exc)

    def _open_settings(self, settings):
        store = FocusCalibration(self.data_dir, settings)
        return store, store.load(), bool(previous_profile(self.config_dir, settings.signature))

    def _open_draft(self, result):
        self.store, draft, self.has_previous = result
        settings = self.store.settings
        h, w = settings.shape[:2]
        self.settings.setText(f'{w} × {h} px · {settings.sensor_bits}-bit')
        self.settings.show()
        self.refresh(draft)

    def select_camera(self):
        self._run(lambda: self.camera_action('Reading focus settings', lambda frame, context:
            self._open_settings(FocusSettings.from_frame(frame, context))), self._open_draft)

    def select_metadata(self):
        name, _ = QFileDialog.getOpenFileName(self, 'Choose the reference recording', '',
                                              'TEMPO recordings (*.tif *.tiff *.recording.json)')
        if name:
            self._work('Reading focus settings', lambda: self._open_settings(read_focus_reference(name)), self._open_draft)

    def _field(self):
        selected = self.fields.currentItem()
        if selected is not None:
            return next((field for field in self.draft['fields'] if field['id'] == selected.data(USER_ROLE)), None)
        return None

    def _plane(self):
        field = self._field()
        selected = self.planes.currentItem()
        if field and selected:
            return next((plane for plane in field['planes'] if plane['id'] == selected.data(USER_ROLE)), None)
        return None

    def refresh(self, draft, *, field_id=None, plane_id=None):
        field, plane = self._field(), self._plane()
        field_id = field_id if field_id is not None else (field['id'] if field else None)
        plane_id = plane_id if plane_id is not None else (plane['id'] if plane else None)
        self.draft = draft
        with QSignalBlocker(self.fields), QSignalBlocker(self.planes):
            self.fields.clear()
            chosen = 0
            for index, item in enumerate(draft['fields']):
                use = 'Fit' if item['partition'] == 'train' else 'Check'
                entry = QListWidgetItem(f"{item['name']} · {use}", self.fields)
                entry.setData(USER_ROLE, item['id'])
                if item['id'] == field_id:
                    chosen = index
            self.fields.setCurrentRow(chosen if self.fields.count() else -1)
        self.select_field(plane_id=plane_id)
        self.show_results()
        self.update_actions()

    def select_field(self, *_args, plane_id=None):
        field = self._field()
        with QSignalBlocker(self.planes), QSignalBlocker(self.partition):
            self.planes.clear()
            if field:
                self.partition.setCurrentIndex(self.partition.findData(field['partition']))
                selected = 0
                for index, plane in enumerate(field['planes']):
                    label = LABELS.get(plane['label'], 'Unlabeled')
                    entry = QListWidgetItem(f'{plane_name(plane, index)} · {label}', self.planes)
                    entry.setData(USER_ROLE, plane['id'])
                    if plane['id'] == plane_id:
                        selected = index
                if self.planes.count():
                    self.planes.setCurrentRow(selected)
        self.select_plane()

    def _display_saved(self, view, plane, title):
        view.requested = self.store, plane['id'], title
        if not view.loading:
            self._load_preview(view)

    def _load_preview(self, view):
        requested = view.requested
        store, plane_id, title = requested
        view.loading = True
        view.set_frame(title=title)

        def received(frame, error):
            view.loading = False
            if requested == view.requested:
                if error:
                    view.set_frame(title='Saved plane is unavailable.')
                    self.error.set_error(str(error), error)
                else:
                    view.set_frame(frame, store.settings.sensor_bits, title)
            elif view.requested:
                self._load_preview(view)

        run_background(lambda: store.read_plane(plane_id), received)

    def _sync_choices(self):
        field, plane = self._field(), self._plane()
        with QSignalBlocker(self.partition), QSignalBlocker(self.labels):
            if field:
                self.partition.setCurrentIndex(self.partition.findData(field['partition']))
            self.labels.setCurrentIndex(self.labels.findData(plane['label'] if plane else None))

    def select_plane(self, *_args):
        field, plane = self._field(), self._plane()
        with QSignalBlocker(self.labels):
            self.labels.setCurrentIndex(self.labels.findData(plane['label'] if plane else None))
        if plane:
            self.live = False
            title = f"{field['name']} · {plane_name(plane, self.planes.currentRow())}"
            self._display_saved(self.capture_view, plane, title)
        else:
            self.set_live()
        self.update_actions()

    def set_live(self):
        self.live = True
        self.capture_view.requested = None
        self.capture_view.set_frame(title='Start the camera feed in Acquisition.' if self.camera_available() else '')
        self.update_actions()

    def observe_preview(self, frame, context):
        if not self.isVisible() or self._busy or self.tabs.currentWidget() is not self.capture_page or not self.live:
            return
        now = time.monotonic()
        if now - self.last_live_at < 0.14:
            return
        self.last_live_at = now
        self._preview_signature = (context or {}).get('quality_signature')
        self.capture_view.set_frame(frame, (context or {}).get('sensor_bits'), 'Live preview')
        self.update_actions()

    def add_field(self):
        store, revision, well = self.store, self.draft['revision'], self.current_well()
        self._work('Saving focus field', lambda: store.add_field(revision, well),
                   lambda draft: self.refresh(draft, field_id=draft['fields'][-1]['id']))

    def edit_partition(self):
        field = self._field()
        partition = self.partition.currentData()
        if field and partition != field['partition']:
            store, revision, field_id = self.store, self.draft['revision'], field['id']
            self._work('Saving field assignment', lambda: store.set_partition(revision, field_id, partition), self.refresh)

    def capture_plane(self):
        field = self._field()
        if field:
            store, revision, field_id, z_index = self.store, self.draft['revision'], field['id'], self.current_z()
            self._run(lambda: self.camera_action('Capturing focus plane', lambda frame, context:
                store.capture_plane(revision, field_id, frame, FocusSettings.from_frame(frame, context), z_index)),
                lambda draft: self._select_last_plane(draft, field_id))

    def _select_last_plane(self, draft, field_id):
        field = next(field for field in draft['fields'] if field['id'] == field_id)
        self.refresh(draft, field_id=field_id, plane_id=field['planes'][-1]['id'])

    def import_planes(self):
        field = self._field()
        if not field:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, 'Import TIFFs from one XY field', '', 'TIFF images (*.tif *.tiff)')
        if not paths or not confirm_action(self, 'Import focal planes',
                'Were these planes captured at this XY field using the calibration camera settings?', 'Import Planes'):
            return
        store, revision, field_id = self.store, self.draft['revision'], field['id']
        self._work('Importing focus planes', lambda: store.import_planes(revision, field_id, paths),
                   lambda draft: self._select_last_plane(draft, field_id))

    def edit_label(self):
        field, plane = self._field(), self._plane()
        label = self.labels.currentData()
        if field and plane and label != plane['label']:
            store, revision = self.store, self.draft['revision']
            self._work('Saving focus label', lambda: store.label_plane(revision, field['id'], plane['id'], label),
                       self.refresh)

    def _labels_by_partition(self):
        labels = {'train': set(), 'test': set()}
        for field in self.draft.get('fields', []):
            labels[field['partition']].update(plane['label'] for plane in field['planes'])
        return labels

    def fit(self):
        store, revision = self.store, self.draft['revision']
        def done(draft):
            self.refresh(draft)
            self.tabs.setCurrentWidget(self.review_page)
        self._work('Fitting and checking focus', lambda: store.fit(revision), done)

    def show_results(self):
        self.outcomes = []
        lines = (self.draft.get('candidate') or {}).get('validation', [])
        lookup = {plane['id']: (plane, plane_name(plane, index))
                  for field in self.draft.get('fields', []) for index, plane in enumerate(field['planes'])}
        with QSignalBlocker(self.results):
            self.results.setRowCount(len(lines))
            first_mismatch = None
            for row, outcome in enumerate(lines):
                plane, name = lookup[outcome['plane_id']]
                self.outcomes.append((outcome, plane, name))
                values = (outcome['field'].replace('Field ', '', 1), name.replace('Z index ', 'Z ', 1),
                          LABELS[outcome['expected']], LABELS[outcome['actual']])
                for col, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    item.setToolTip(outcome['field'] if col == 0 else text)
                    if col == 3 and outcome['actual'] != outcome['expected']:
                        item.setToolTip(f"Expected {LABELS[outcome['expected']]}. {outcome['reason']}".strip())
                        if first_mismatch is None:
                            first_mismatch = row
                        if self._false_sharp(outcome):
                            item.setForeground(QColor(get_current_theme().get('warning_text', '#f09663')))
                    self.results.setItem(row, col, item)
            if lines:
                self.results.selectRow(first_mismatch if first_mismatch is not None else 0)
        self.select_result()

    def select_result(self, *_args):
        row = self.results.currentRow()
        if self.outcomes and 0 <= row < len(self.outcomes):
            outcome, plane, name = self.outcomes[row]
            self._display_saved(self.review_view, plane, f"{outcome['field']} · {name}")
            comparison = f"Your label · {LABELS[outcome['expected']]}    {APP_DISPLAY_NAME} · {LABELS[outcome['actual']]}"
            warnings = {'saturation': 'Saturated pixels', 'inadequate_signal': 'Inadequate image signal'}
            details = [warnings[flag] for flag in outcome['flags'] if flag in warnings]
            if outcome['reason']:
                details.append(outcome['reason'])
            self.comparison.setText('\n'.join([comparison, *details]))
        else:
            self.review_view.requested = None
            self.review_view.set_frame(title='Fit and check to review held-out images.')
            self.comparison.clear()

    @staticmethod
    def _false_sharp(outcome):
        return outcome['expected'] == 'out_of_focus' and outcome['actual'] == 'sharp'

    def _apply_done(self, profile):
        self.on_profile(profile)
        self.has_previous = bool(previous_profile(self.config_dir, self.store.settings.signature))
        self.update_actions()

    def apply(self):
        candidate = self.draft.get('candidate')
        if not candidate or not self.can_apply():
            return
        if any(self._false_sharp(row) for row in candidate['validation']) and not confirm_action(
                self, 'Apply profile with disagreements',
                f'{APP_DISPLAY_NAME} marked blurred planes Sharp in the held-out check. Apply this profile to live focus and automatic collection?', 'Apply Profile'):
            return
        store, revision, config_dir = self.store, self.draft['revision'], self.config_dir
        self._work('Applying focus profile', lambda: store.apply(revision, config_dir), self._apply_done)

    def restore_previous(self):
        if not self.has_previous or not self.can_apply():
            return
        if confirm_action(self, 'Restore previous focus profile',
                'Use the previously applied profile for these camera settings?', 'Restore Profile'):
            config_dir, signature = self.config_dir, self.store.settings.signature
            self._work('Restoring focus profile', lambda: update_profile(config_dir, signature), self._apply_done)

    def update_actions(self, *_args):
        field, plane = self._field(), self._plane()
        idle = not self._busy
        camera = self.camera_available()
        self.fields.setVisible(self.fields.count() > 0)
        self.no_fields.setVisible(self.fields.count() == 0)
        self.planes_title.setVisible(field is not None)
        self.planes.setVisible(self.planes.count() > 0)
        self.no_planes.setVisible(field is not None and self.planes.count() == 0)
        self.fields.setEnabled(idle)
        self.planes.setEnabled(idle)
        self.use_camera.setEnabled(idle and camera)
        self.use_metadata.setEnabled(idle)
        self.new_field.setEnabled(idle and self.store is not None)
        self.partition.setEnabled(idle and field is not None)
        self.show_live.setEnabled(idle and camera and not self.live)
        self.take_plane.setEnabled(idle and camera and field is not None)
        self.import_tiffs.setEnabled(idle and field is not None)
        self.labels.setEnabled(idle and plane is not None and not self.live)
        required = {'sharp', 'out_of_focus'}
        labels = self._labels_by_partition()
        self.fit_button.setEnabled(idle and all(required <= values for values in labels.values()))
        if self.store is None:
            instruction = ('Choose camera settings to start.' if camera else
                           f'Connect a camera or choose metadata from a matching {APP_DISPLAY_NAME} recording.')
        elif field is None:
            instruction = 'Choose New Field to capture or import its planes.'
        else:
            missing = [name for role, name in (('train', 'fit'), ('test', 'check')) if not required <= labels[role]]
            instruction = (f"Label sharp and out-of-focus planes in the {' and '.join(missing)} fields."
                           if missing else 'Move to a different XY position before adding another field.')
            if missing == ['check'] and all(item['partition'] != 'test' for item in self.draft['fields']):
                instruction = 'Add a different XY field and hold it back for checking.'
        if self.store is not None and self.live and camera and self._preview_signature and self._preview_signature != self.store.settings.signature:
            instruction = 'Camera settings changed. Restore the settings used for these planes or choose Use Camera Settings.'
        self.instructions.setText(instruction)
        candidate = self.draft.get('candidate')
        active_id = None
        if self.store:
            active_id = (self.profile_for(self.store.settings.signature) or {}).get('id')
        applied = bool(candidate and candidate['id'] == active_id)
        self.apply_button.setText('Profile Applied' if applied else 'Apply Profile')
        can_apply = idle and self.can_apply()
        self.apply_button.setEnabled(can_apply and bool(candidate) and not applied)
        self.restore_button.setVisible(self.store is not None and self.has_previous)
        self.restore_button.setEnabled(can_apply)
        if candidate:
            rows = candidate['validation']
            matched = sum(row['expected'] == row['actual'] for row in rows)
            false_sharp = sum(self._false_sharp(row) for row in rows)
            count = 'plane' if false_sharp == 1 else 'planes'
            self.summary.setText(f'{matched} of {len(rows)} match · {false_sharp} blurred {count} reported as Sharp')
        else:
            self.summary.setText('No check results yet.')
