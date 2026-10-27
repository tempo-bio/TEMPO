"""Maintainer review with editable vertices and atomic approval."""
import os
import copy
import re
from PyQt6 import sip
from PyQt6.QtCore import Qt, QPointF, pyqtSignal, QTimer, QEvent
from PyQt6.QtGui import QPixmap, QPen, QColor, QBrush, QPolygonF
from PyQt6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QComboBox, QMessageBox, QInputDialog,
    QFileDialog, QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsItem, QGraphicsSimpleTextItem, QCheckBox, QSlider, QWidget)
from tempo_hub import tempo_hub
from tempo_tasks import run_read_task, run_task, run_background
from tempo_annotations import pixel_record, normalize_record
from tempo_regions import object_type
from tempo_nuclei import is_nucleus, migrate_record
from tempo_preprocessing import preview_image
from tempo_ui import confirm_action, FilenameLabel, ErrorDetails, FlowLayout


class VertexHandle(QGraphicsEllipseItem):
    def __init__(self, canvas, polygon_index, vertex_index, point):
        super().__init__(-4, -4, 8, 8)
        self.canvas, self.pi, self.vi = canvas, polygon_index, vertex_index
        self.setBrush(QBrush(QColor('#FFFFFF')))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setPos(QPointF(*point))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setZValue(2)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            return QPointF(max(0, min(self.canvas.width_px, value.x())), max(0, min(self.canvas.height_px, value.y())))
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.canvas.boxes[self.pi]['polygon'][self.vi] = [value.x(), value.y()]
            self.canvas.polygons[self.pi].setPolygon(QPolygonF([QPointF(*p) for p in self.canvas.boxes[self.pi]['polygon']]))
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        self.canvas.remember()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.canvas.changed.emit()


class ReviewCanvas(QGraphicsView):
    changed = pyqtSignal()
    zoom_changed = pyqtSignal(float)
    def __init__(self, parent=None, editable=True):
        super().__init__(parent)
        self.editable = editable
        self.setScene(QGraphicsScene(self))
        self.boxes, self.polygons = [], []
        self.width_px = self.height_px = 1
        self.scene().selectionChanged.connect(self.update_handles)
        self.handles = []
        self.history = []
        self.overlay_visible = True
        self.fill_alpha = 0

    def set_image_and_masks(self, path, record):
        pix = QPixmap(path)
        if pix.isNull():
            raise ValueError('Unable to read review image.')
        self.path = path
        self.width_px, self.height_px = pix.width(), pix.height()
        self.nuclear_only = record.get('nuclear_only',False)
        self.boxes = pixel_record(migrate_record(record)).get('boxes', [])
        self.history = []
        self.redraw(fit=True)

    def set_contrast(self, enabled):
        self.enhanced = enabled
        self.redraw()

    def showEvent(self, event):
        super().showEvent(event)
        if self.scene().items() and (not self.editable or not getattr(self, '_shown_once', False)):
            self.fit_image()
        self._shown_once = True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.editable and self.scene().items():
            self.fit_image()

    def clear_image(self):
        self.handles = []
        self.polygons = []
        self.boxes = []
        self.history = []
        if hasattr(self, 'path'): del self.path
        self.scene().clear()

    def redraw(self, fit=False):
        if not hasattr(self, 'path'):
            return
        self.handles = []
        self.scene().clear()
        self.polygons = []
        self.scene().addPixmap(QPixmap.fromImage(preview_image(self.path,getattr(self,'enhanced',False))))
        for index, box in enumerate(self.boxes):
            color = QColor('#EB5757' if box.get('class_id') == 1 else '#3EA685' if box.get('class_id') == 0 else '#9B51E0')
            fill = QColor(color); fill.setAlpha(self.fill_alpha)
            item = self.scene().addPolygon(QPolygonF([QPointF(*p) for p in box['polygon']]), QPen(color, 2), QBrush(fill))
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable,not getattr(self,'nuclear_only',False) or is_nucleus(box))
            item.setData(0, index)
            self.polygons.append(item)
            item.setVisible(self.overlay_visible)
            if object_type(box) == 'group':
                label = QGraphicsSimpleTextItem('Group', item)
                label.setBrush(QBrush(QColor('#FFFFFF')))
                label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
                center = item.polygon().boundingRect().center()
                label.setPos(center)
            if self.editable and (not getattr(self,'nuclear_only',False) or is_nucleus(box)):
                for vertex, point in enumerate(box['polygon']):
                    handle = VertexHandle(self, index, vertex, point)
                    handle.setVisible(False)
                    self.scene().addItem(handle)
                    self.handles.append(handle)
        self.setSceneRect(0, 0, self.width_px, self.height_px)
        if fit:
            self.fit_image()

    def fit_image(self):
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self.zoom_changed.emit(self.transform().m11())

    def zoom_by(self, factor):
        self.scale(factor, factor)
        self.zoom_changed.emit(self.transform().m11())

    def update_handles(self):
        selected = {item.data(0) for item in self.scene().selectedItems() if item in self.polygons}
        for handle in self.handles:
            handle.setVisible(self.overlay_visible and handle.pi in selected)

    def remember(self):
        self.history.append(copy.deepcopy(self.boxes))
        self.history = self.history[-50:]

    def undo(self):
        if self.history:
            self.boxes = self.history.pop()
            self.redraw()
            self.changed.emit()

    def set_overlay(self, visible):
        self.overlay_visible = visible
        self.redraw()

    def wheelEvent(self, event):
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        self.zoom_by(factor)
        event.accept()

    def delete_selected(self):
        selected = {int(item.data(0)) for item in self.scene().selectedItems() if item in self.polygons}
        if not self.editable or not selected:
            return
        self.remember()
        self.boxes = [b for i,b in enumerate(self.boxes) if i not in selected]
        self.redraw()
        self.changed.emit()

    def reclassify(self, class_id):
        selected = [int(item.data(0)) for item in self.scene().selectedItems() if item in self.polygons]
        if not self.editable or not selected or all(self.boxes[i]['class_id'] == class_id
                and not self.boxes[i].get('requires_viability_review') for i in selected):
            return
        if any(is_nucleus(self.boxes[i]) != (class_id == 2) for i in selected):
            return
        self.remember()
        for index in selected:
            self.boxes[index]['class_id'] = class_id
            self.boxes[index].pop('requires_viability_review', None)
            self.boxes[index].pop('dead', None)
            if class_id == 2:
                self.boxes[index]['object_type'] = 'nucleus'
        self.redraw()
        for index in selected:
            self.polygons[index].setSelected(True)
        self.changed.emit()

    def set_group(self, enabled):
        selected = [int(item.data(0)) for item in self.scene().selectedItems() if item in self.polygons]
        if not self.editable or not selected or any(self.boxes[i]['class_id'] not in (0, 1) for i in selected):
            return
        kind = 'group' if enabled else 'cell'
        if all(object_type(self.boxes[i]) == kind for i in selected):
            return
        self.remember()
        for index in selected:
            if enabled:
                self.boxes[index]['object_type'] = 'group'
            else:
                self.boxes[index].pop('object_type', None)
        self.redraw()
        for index in selected:
            self.polygons[index].setSelected(True)
        self.changed.emit()


class AnnotationReviewDialog(QDialog):
    submission_approved = pyqtSignal(str)
    review_completed = pyqtSignal()

    def __init__(self, parent=None, verified=False):
        super().__init__(parent)
        if not verified and not run_read_task(parent, 'Checking review access…', tempo_hub.is_maintainer):
            raise PermissionError('Maintainer access is required.')
        self.setWindowTitle('Review Submissions')
        self.resize(1100, 720)
        self.current_submission = self.current_image_filename = None
        self.current_annotations = {}
        self.reviewed_snapshot = {}
        self.reviewed_images = set()
        self._generation = 0
        self._frame_generation = 0
        self._loading = False
        self._operation = False
        self._refresh_pending = False
        self._session_context = self.read_context()
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.status = QLabel(''); self.status.setWordWrap(True); top.addWidget(self.status, 1)
        self.refresh_button = QPushButton('Refresh')
        self.refresh_button.clicked.connect(self.refresh_submissions); top.addWidget(self.refresh_button)
        self.retry_button = QPushButton('Retry'); self.retry_button.hide()
        self.retry_button.clicked.connect(self.retry_load); top.addWidget(self.retry_button)
        self._retry_action = None
        layout.addLayout(top)
        self.error_details = ErrorDetails()
        layout.addWidget(self.error_details)
        row = QHBoxLayout()
        self.sub_list, self.frame_list = QListWidget(), QListWidget()
        self.sub_list.setMaximumWidth(200); self.frame_list.setMaximumWidth(220)
        self.sub_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.sub_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.frame_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.frame_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for label, widget in [('Submissions', self.sub_list), ('Images', self.frame_list)]:
            column = QVBoxLayout(); column.addWidget(QLabel(label)); column.addWidget(widget)
            row.addLayout(column)
        self.canvas = ReviewCanvas()
        self.canvas.changed.connect(self.edits_changed)
        self.canvas.scene().selectionChanged.connect(self.update_edit_actions)
        row.addWidget(self.canvas, 1)
        layout.addLayout(row, 1)
        self.image_title = FilenameLabel()
        self.image_title.setText('Select a submission to review.')
        layout.addWidget(self.image_title)
        view_controls = QHBoxLayout()
        self.view_buttons = []
        for title, callback in [('Zoom +', lambda: self.canvas.zoom_by(1.2)),
                                ('Zoom −', lambda: self.canvas.zoom_by(1/1.2)),
                                ('Fit', self.canvas.fit_image)]:
            button = QPushButton(title); button.clicked.connect(callback); view_controls.addWidget(button); self.view_buttons.append(button)
        overlay = QCheckBox('Outlines'); overlay.setChecked(True)
        overlay.toggled.connect(self.canvas.set_overlay); view_controls.addWidget(overlay); self.view_buttons.append(overlay)
        contrast = QCheckBox('Contrast')
        contrast.toggled.connect(self.canvas.set_contrast); view_controls.addWidget(contrast)
        self.zoom_label = QLabel()
        self.canvas.zoom_changed.connect(lambda factor: self.zoom_label.setText(f'{factor * 100:.0f}%'))
        view_controls.addWidget(self.zoom_label)
        view_controls.addStretch(); layout.addLayout(view_controls)
        edit = FlowLayout()
        self.undo_button = QPushButton('Undo'); self.undo_button.clicked.connect(self.canvas.undo); edit.addWidget(self.undo_button)
        self.delete_button = QPushButton('Delete Annotations'); self.delete_button.clicked.connect(self.canvas.delete_selected); edit.addWidget(self.delete_button)
        class_group = QWidget(); class_row = QHBoxLayout(class_group)
        class_row.setContentsMargins(0, 0, 0, 0)
        class_row.addWidget(QLabel('Selected class:'))
        self.classes = QComboBox(); self.classes.addItems(['Healthy', 'Dead'])
        self.classes.setPlaceholderText('Select cell outlines first')
        self.classes.setMinimumContentsLength(23)
        self.classes.setMinimumHeight(self.delete_button.sizeHint().height())
        self.classes.setToolTip('Change the class of selected cell outlines.')
        self.classes.activated.connect(self.canvas.reclassify)
        class_row.addWidget(self.classes); edit.addWidget(class_group)
        self.group_toggle = QCheckBox('Group')
        self.group_toggle.clicked.connect(self.canvas.set_group)
        edit.addWidget(self.group_toggle)
        layout.addLayout(edit)
        self.complete = QCheckBox('All visible cells and groups are annotated')
        self.complete.toggled.connect(self.mark_reviewed); layout.addWidget(self.complete)
        actions = QHBoxLayout()
        self.return_button = QPushButton('Request Corrections'); self.return_button.clicked.connect(self.reject_submission)
        self.approve_button = QPushButton('Approve Submission'); self.approve_button.clicked.connect(self.approve_and_merge)
        self.approve_button.setProperty('primary', True)
        self.action_buttons = [self.return_button, self.approve_button]
        actions.addWidget(self.return_button); actions.addStretch(); actions.addWidget(self.approve_button)
        layout.addLayout(actions)
        self.sub_list.currentItemChanged.connect(self.on_submission_selected)
        self.frame_list.currentItemChanged.connect(self.on_frame_selected)
        self.update_edit_actions()
        self.refresh_submissions(automatic=True)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, 'classes'):
            self.classes.setMinimumHeight(self.delete_button.sizeHint().height())

    def update_edit_actions(self):
        same_account = self.read_context() == self._session_context
        self.canvas.setEnabled(same_account and not self._operation)
        available = bool(self.current_image_filename) and not self._operation and same_account
        self.zoom_label.setVisible(bool(self.current_image_filename))
        for button in self.view_buttons: button.setEnabled(available)
        selected_items = [item for item in self.canvas.scene().selectedItems() if item in self.canvas.polygons]
        selected = bool(selected_items) and available
        self.delete_button.setEnabled(selected)
        self.classes.setEnabled(selected)
        categories = {self.canvas.boxes[int(item.data(0))]['class_id'] for item in selected_items
                      if int(item.data(0)) < len(self.canvas.boxes)}
        nuclear_selection=2 in categories
        self.classes.setEnabled(selected and not nuclear_selection)
        self.classes.setCurrentIndex(next(iter(categories)) if len(categories)==1 and not nuclear_selection else -1)
        self.classes.setPlaceholderText('Nuclear outline' if nuclear_selection else 'Mixed classes' if selected_items else 'Select cell outlines first')
        selected_boxes = [self.canvas.boxes[int(item.data(0))] for item in selected_items]
        self.group_toggle.setEnabled(selected and all(box['class_id'] in (0, 1) for box in selected_boxes))
        kinds = {object_type(box) for box in selected_boxes}
        self.group_toggle.blockSignals(True)
        self.group_toggle.setTristate(len(kinds) > 1)
        self.group_toggle.setCheckState(Qt.CheckState.PartiallyChecked if len(kinds) > 1 else
                                       Qt.CheckState.Checked if kinds == {'group'} else Qt.CheckState.Unchecked)
        self.group_toggle.blockSignals(False)
        self.undo_button.setEnabled(bool(self.canvas.history) and available)
        self.complete.setEnabled(available)
        self.return_button.setEnabled(bool(self.current_submission) and not self._loading and not self._operation and same_account)
        self.approve_button.setEnabled(bool(self.current_annotations) and self.reviewed_images == set(self.current_annotations) and not self._loading and not self._operation and same_account)

    def retry_load(self):
        action = self._retry_action
        self.clear_retry()
        if action: action()

    def clear_retry(self):
        self._retry_action = None
        self.retry_button.hide()
        self.error_details.clear()

    def offer_retry(self, action):
        self._retry_action = action
        self.retry_button.show()

    def save_current_edits(self):
        if self.current_image_filename:
            if self.canvas.boxes == getattr(self, '_displayed_boxes', None):
                self.current_annotations[self.current_image_filename] = copy.deepcopy(self._displayed_record)
                return
            record = dict(self.current_annotations[self.current_image_filename],
                          boxes=copy.deepcopy(self.canvas.boxes), coordinate_space='pixels')
            self.current_annotations[self.current_image_filename] = normalize_record(record, self.canvas.width_px, self.canvas.height_px)

    def edits_changed(self):
        self.save_current_edits()
        self.complete.setChecked(False)
        self.update_edit_actions()
        self.status.setText('Unapproved review edits')

    def mark_reviewed(self, checked):
        if self.current_image_filename:
            if checked: self.reviewed_images.add(self.current_image_filename)
            else: self.reviewed_images.discard(self.current_image_filename)
        self.update_edit_actions()

    def can_leave(self):
        if self._operation:
            self.status.setText('Finishing the review action…')
            return False
        self.save_current_edits()
        if self.current_annotations != self.reviewed_snapshot:
            return confirm_action(self, 'Discard review edits?',
                                  'These changes have not been approved. Leaving this review will discard them.',
                                  'Discard Edits')
        return True

    def run_read(self, action, completed):
        """Bound UI waiting for reads; late network results cannot replace a retry."""
        timer = QTimer(self)
        timer.setSingleShot(True)
        settled = False
        def finish(value, error):
            nonlocal settled
            if settled or sip.isdeleted(timer):
                return
            settled = True
            timer.stop()
            timer.deleteLater()
            completed(value, error)
        timer.timeout.connect(lambda: finish(None, TimeoutError('No response after 30 seconds. Retry the request.')))
        timer.start(30000)
        run_background(action, finish)
        return timer

    @staticmethod
    def read_context():
        return tempo_hub.token, tempo_hub.get_dataset_repo()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._loading:
            self.request_refresh()

    def request_refresh(self):
        self._refresh_pending = True
        self.update_edit_actions()
        if not self.isVisible() or self._loading or self._operation:
            return
        self.save_current_edits()
        if self.current_annotations != self.reviewed_snapshot:
            self.status.setText('Review updates available. Your edits remain open.')
            return
        self.refresh_submissions(automatic=True)

    def clear_selection(self):
        self._frame_generation += 1
        self.current_submission = self.current_image_filename = None
        self.current_annotations = {}; self.reviewed_snapshot = {}; self.reviewed_images.clear()
        self.frame_list.clear(); self.canvas.clear_image()
        self.image_title.clear()

    def refresh_submissions(self, automatic=False):
        if self._loading or not self.can_leave():
            return
        if self.current_annotations != self.reviewed_snapshot or self.read_context() != self._session_context:
            self.clear_selection()
        self._session_context = context = self.read_context()
        self._reload_selection = not automatic
        self._refresh_pending = False
        self._generation += 1; generation = self._generation
        self._loading = True; self.sub_list.setEnabled(False); self.refresh_button.setEnabled(False); self.update_edit_actions()
        self.clear_retry()
        self.status.setText('Loading submissions…')
        def ready(result, error):
            if generation != self._generation:
                return
            if context != self.read_context():
                self.invalidate_reads()
                self.request_refresh()
                return
            self.submissions_ready(result, error)
        self.run_read(tempo_hub.fetch_pending_submissions, ready)

    def submissions_ready(self, submissions, error):
        self._loading = False; self.sub_list.setEnabled(True); self.refresh_button.setEnabled(True)
        if error:
            self.status.setText('Submissions could not be loaded.')
            if not self.current_submission:
                self.image_title.clear()
            status_code = getattr(getattr(error, 'response', None), 'status_code', None)
            summary = ('Hugging Face returned a server error. Retry shortly.'
                       if isinstance(status_code, int) and status_code >= 500
                       else 'Check your connection and review access, then retry.')
            self.error_details.set_error(summary, error)
            self.offer_retry(self.refresh_submissions); return
        self.save_current_edits()
        if self.current_annotations != self.reviewed_snapshot:
            self._refresh_pending = True
            self.status.setText('Review updates available. Your edits remain open.')
            self.update_edit_actions()
            return
        selected = self.current_submission['submission_id'] if self.current_submission else None
        self.sub_list.blockSignals(True)
        self.sub_list.clear()
        match = None
        for sub in submissions:
            count = sub['image_count']
            name = tempo_hub.display_collaborator(sub['collaborator_id'], record=sub)
            item = QListWidgetItem(f"{name}\n{count} {'image' if count == 1 else 'images'}")
            item.setToolTip(sub['collaborator_id'])
            item.setData(Qt.ItemDataRole.UserRole, sub); self.sub_list.addItem(item)
            if sub['submission_id'] == selected:
                match = item
                self.current_submission = sub
        if match:
            self.sub_list.setCurrentItem(match)
        elif selected:
            self.clear_selection()
        self.sub_list.blockSignals(False)
        count = len(submissions)
        self.status.setText(f"{count} {'submission' if count == 1 else 'submissions'} awaiting review." if count else 'No submissions awaiting review.')
        if not self.current_submission:
            self.image_title.setText('Select a submission to review.' if submissions else '')
        self.update_edit_actions()
        if match and self._reload_selection:
            self.on_submission_selected(match, None, preserve_image=self.current_image_filename)
        if self._refresh_pending:
            self.request_refresh()

    def on_submission_selected(self, current, previous, preserve_image=None):
        if not current: return
        if not self.can_leave():
            self.sub_list.blockSignals(True); self.sub_list.setCurrentItem(previous); self.sub_list.blockSignals(False)
            return
        self._restore_view = (preserve_image, self.canvas.transform(),
                              self.canvas.horizontalScrollBar().value(), self.canvas.verticalScrollBar().value(),
                              (self.canvas.width_px, self.canvas.height_px)) if preserve_image else None
        self._generation += 1; generation = self._generation
        context = self.read_context()
        self._frame_generation += 1
        self.current_image_filename = None
        self.current_submission = current.data(Qt.ItemDataRole.UserRole)
        sid = self.current_submission['submission_id']
        self.current_annotations = {}; self.reviewed_snapshot = {}; self.reviewed_images.clear()
        self.frame_list.clear(); self.canvas.clear_image()
        self._loading = True; self.refresh_button.setEnabled(False); self.update_edit_actions()
        self.clear_retry()
        self.image_title.clear()
        self.status.setText('Loading annotations…')
        def ready(records, error):
            if generation != self._generation: return
            if context != self.read_context():
                self.invalidate_reads(); self.request_refresh(); return
            self._loading = False; self.refresh_button.setEnabled(True)
            if error:
                self.status.setText('Annotations could not be loaded.')
                self.error_details.set_error('Check your connection and choose Retry to load this submission.', error)
                self.offer_retry(lambda: self.on_submission_selected(self.sub_list.currentItem(), None))
                self.update_edit_actions(); return
            self.current_annotations = records
            self.reviewed_snapshot = copy.deepcopy(records)
            for name, record in sorted(records.items(), key=lambda pair: [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', pair[1].get('original_filename') or pair[0])]):
                item = QListWidgetItem(record.get('original_filename') or name)
                item.setData(Qt.ItemDataRole.UserRole, name); item.setToolTip(record.get('original_filename') or name); self.frame_list.addItem(item)
            self.status.setText(f"{len(records)} {'image' if len(records) == 1 else 'images'} to review.")
            selected_row = next((i for i in range(self.frame_list.count())
                                 if self.frame_list.item(i).data(Qt.ItemDataRole.UserRole) == preserve_image), 0)
            self.frame_list.setCurrentRow(selected_row); self.update_edit_actions()
            if self._refresh_pending:
                self.request_refresh()
        self.run_read(lambda: tempo_hub.fetch_submission_annotations(sid), ready)

    def on_frame_selected(self, current, previous):
        if not current: return
        self.save_current_edits()
        self.current_image_filename = None
        self._frame_generation += 1; generation = self._frame_generation
        context = self.read_context()
        name = current.data(Qt.ItemDataRole.UserRole)
        record = self.current_annotations[name]
        self.canvas.clear_image(); self.update_edit_actions()
        self.clear_retry()
        self.complete.blockSignals(True); self.complete.setChecked(False); self.complete.blockSignals(False)
        self.image_title.setText('Loading image…')
        def ready(path, error):
            if generation != self._frame_generation or context != self.read_context(): return
            if error:
                self.image_title.setText(record.get('original_filename') or name)
                self.error_details.set_error('The image could not be downloaded. Check your connection and choose Retry.', error)
                self.offer_retry(lambda: self.on_frame_selected(self.frame_list.currentItem(), None)); return
            try:
                self.canvas.set_image_and_masks(path, record)
                self.complete.setText('Visible nuclear outlines reviewed' if record.get('nuclear_only') else 'All visible cells and groups are annotated')
                restore = getattr(self, '_restore_view', None)
                if restore and restore[0] == name and restore[4] == (self.canvas.width_px, self.canvas.height_px):
                    self.canvas.setTransform(restore[1])
                    self.canvas.horizontalScrollBar().setValue(restore[2]); self.canvas.verticalScrollBar().setValue(restore[3])
                    self.canvas.zoom_changed.emit(self.canvas.transform().m11())
                self._restore_view = None
                self._displayed_boxes = copy.deepcopy(self.canvas.boxes)
                self._displayed_record = copy.deepcopy(record)
            except Exception as exc:
                self.image_title.setText(record.get('original_filename') or name)
                self.error_details.set_error('The image could not be displayed. Choose Retry to reload it; if it still fails, review the file details below.', exc)
                self.offer_retry(lambda: self.on_frame_selected(self.frame_list.currentItem(), None)); return
            self.current_image_filename = name
            self.image_title.setText(record.get('original_filename') or name)
            self.complete.blockSignals(True); self.complete.setChecked(name in self.reviewed_images); self.complete.blockSignals(False)
            self.update_edit_actions()
        self.run_read(lambda: tempo_hub.download_image(name), ready)

    def approve_and_merge(self):
        if not self.approve_button.isEnabled(): return
        self.save_current_edits()
        count = len(self.current_annotations)
        nuclear = any(record.get('nuclear_only') for record in self.current_annotations.values())
        consequence = (f'Nuclear outlines from {count} reviewed images will be included in training. Approved cell outlines stay locked.'
                       if nuclear else f'{count} reviewed images will be locked and included in the approved training dataset.')
        if not confirm_action(self, 'Approve submission?', consequence, 'Approve Submission'):
            return
        sid = self.current_submission['submission_id']
        records = copy.deepcopy(self.current_annotations); expected = copy.deepcopy(self.reviewed_snapshot)
        context = self.read_context()
        self.clear_retry()
        self._operation = True; self.sub_list.setEnabled(False); self.frame_list.setEnabled(False); self.canvas.setEnabled(False)
        self.refresh_button.setEnabled(False); self.update_edit_actions(); self.status.setText('Approving submission…')
        def ready(result, error):
            self._operation = False; self.sub_list.setEnabled(True); self.frame_list.setEnabled(True); self.canvas.setEnabled(True); self.refresh_button.setEnabled(True)
            if context != self.read_context():
                self.request_refresh(); return
            if error or not result[0]:
                self.status.setText('Approval did not finish.')
                self.error_details.set_error('Your review edits are still open. Review the details before retrying Approve Submission.', error or RuntimeError(result[1]))
                self.update_edit_actions(); return
            self.reviewed_snapshot = copy.deepcopy(self.current_annotations)
            self.submission_approved.emit(sid); self.review_completed.emit(); self.refresh_submissions()
        run_background(lambda: tempo_hub.merge_submission_to_master(sid, records, expected_annotations=expected), ready)

    def reject_submission(self):
        if not self.current_submission or not self.can_leave(): return
        feedback, ok = QInputDialog.getMultiLineText(self, 'Request Corrections', 'Describe the corrections needed:')
        if not ok or not feedback.strip(): return
        sid = self.current_submission['submission_id']
        context = self.read_context()
        self.clear_retry()
        self._operation = True; self.sub_list.setEnabled(False); self.frame_list.setEnabled(False); self.canvas.setEnabled(False)
        self.refresh_button.setEnabled(False); self.update_edit_actions(); self.status.setText('Sending correction request…')
        def ready(result, error):
            self._operation = False; self.sub_list.setEnabled(True); self.frame_list.setEnabled(True); self.canvas.setEnabled(True); self.refresh_button.setEnabled(True)
            if context != self.read_context():
                self.request_refresh(); return
            if error or not result[0]:
                self.status.setText('Correction request did not finish.')
                self.error_details.set_error('Review the details, then retry Request Corrections.', error or RuntimeError(result[1]))
                self.update_edit_actions(); return
            self.reviewed_snapshot = copy.deepcopy(self.current_annotations)
            self.review_completed.emit()
            self.refresh_submissions()
        run_background(lambda: tempo_hub.reject_submission(sid, feedback.strip()), ready)

    def reject(self):
        if self.can_leave():
            self.invalidate_reads()
            super().reject()

    def invalidate_reads(self):
        self._generation += 1
        self._frame_generation += 1
        self._loading = False
        self.sub_list.setEnabled(True)
        self.refresh_button.setEnabled(True)

    def closeEvent(self, event):
        if self.can_leave():
            self.invalidate_reads()
            event.accept()
        else: event.ignore()
