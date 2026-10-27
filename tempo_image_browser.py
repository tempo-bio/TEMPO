"""Read-only shared-image browsing, separate from the editable annotation session."""
from collections import OrderedDict
import copy
import re

from PyQt6 import sip
from PyQt6.QtCore import Qt, QSize, QRect, QRectF, QPointF, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap, QIcon, QColor, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QListWidget,
                            QListWidgetItem, QSpinBox, QStyledItemDelegate,
                            QStyleOptionViewItem, QStyle, QCheckBox)
from tempo_annotations import normalize_record, pixel_record
from tempo_hub import expired
from tempo_ui import FlowRow, ErrorDetails, InlineNotice


def preview_thumbnail(payload):
    """Decode off the GUI thread; only the small QImage reaches the icon cache."""
    image = QImage(payload['path'])
    if image.isNull():
        raise ValueError('The shared image could not be read.')
    payload = dict(payload)
    payload['record'] = pixel_record(normalize_record(payload['record'], image.width(), image.height()))
    thumb = image.scaled(112, 84, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    thumb = thumb.convertToFormat(QImage.Format.Format_ARGB32)
    painter = QPainter(thumb)
    painter.scale(thumb.width() / image.width(), thumb.height() / image.height())
    colors = ('#00e060', '#ff5050', '#498cff')
    for box in payload['record'].get('boxes', []):
        pen = QPen(QColor(colors[box['class_id']]), 2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        points = box.get('polygon')
        if points:
            painter.drawPolygon(QPolygonF([QPointF(*point) for point in points]))
        elif 'box' in box:
            painter.drawRect(QRectF(*box['box']))
    painter.end()
    return payload, thumb


class BatchImageList(QListWidget):
    """Checkbox clicks change the batch without changing the focused image."""
    def __init__(self):
        super().__init__()
        self.setItemDelegate(QStyledItemDelegate(self))
        self._checkbox_pressed = False

    def checkbox_rect(self, item):
        option = QStyleOptionViewItem()
        option.initFrom(self)
        index = self.indexFromItem(item)
        self.itemDelegate().initStyleOption(option, index)
        option.rect = self.visualItemRect(item)
        option.decorationSize = self.iconSize()
        return self.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, self)

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if (event.button() == Qt.MouseButton.LeftButton and item
                and item.flags() & Qt.ItemFlag.ItemIsUserCheckable
                and self.checkbox_rect(item).contains(event.position().toPoint())):
            self._checkbox_pressed = True
            item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._checkbox_pressed:
            self._checkbox_pressed = False
            event.accept()
            return
        super().mouseReleaseEvent(event)


class SharedImageBrowser(QWidget):
    preview_ready = pyqtSignal(object)
    preview_loading = pyqtSignal(str)
    reserve_requested = pyqtSignal(list)
    selection_changed = pyqtSignal()
    # The parent can veto focus changes while an edit cannot be saved.
    can_change_image = lambda self: True
    open_editable_image = lambda self, name: False
    local_previews = lambda self: {}
    can_reserve_images = lambda self: True

    def __init__(self, hub, background, parent=None):
        super().__init__(parent)
        self.hub, self.background = hub, background
        self.catalog = None
        self.generation = 0
        self.cache = OrderedDict()
        self.pending = []
        self.busy = False
        self.focused_name = None
        self.items = {}
        self.local_entries = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        heading = FlowRow()
        heading.flow.addWidget(QLabel('Shared Images'))
        self.refresh = QPushButton('Refresh')
        self.refresh.clicked.connect(self.load)
        heading.flow.addWidget(self.refresh)
        layout.addWidget(heading)
        self.filter_row = FlowRow()
        self.filters = {}
        for category, text in (('available', 'Available'), ('approved', 'Approved'), ('mine', 'Reserved by Me')):
            checkbox = QCheckBox(text)
            self.filters[category] = checkbox
            self.filter_row.flow.addWidget(checkbox)
            checkbox.toggled.connect(self.apply_filters)
        self.hidden_selection = InlineNotice()
        self.filter_row.flow.addWidget(self.hidden_selection)
        layout.addWidget(self.filter_row)
        self.status = InlineNotice()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.error = ErrorDetails()
        layout.addWidget(self.error)
        self.images = BatchImageList()
        self.images.setIconSize(QSize(112, 84))
        self.images.setWordWrap(True)
        self.images.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.images.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.images.currentItemChanged.connect(self.focus_image)
        self.images.itemChanged.connect(self.update_actions)
        self.images.verticalScrollBar().valueChanged.connect(self.queue_visible)
        layout.addWidget(self.images, 1)
        selection = FlowRow()
        self.count = QSpinBox()
        self.count.setRange(1, 50)
        self.count.setValue(10)
        selection.flow.addWidget(self.count)
        self.select_first = QPushButton('Select First N Available')
        self.select_first.clicked.connect(self.select_first_available)
        selection.flow.addWidget(self.select_first)
        layout.addWidget(selection)
        actions = FlowRow()
        self.reserve_image = QPushButton('Reserve Image')
        self.reserve_image.clicked.connect(lambda: self.reserve_requested.emit([self.focused_name]))
        self.reserve_selected = QPushButton('Reserve Selected')
        self.reserve_selected.clicked.connect(lambda: self.reserve_requested.emit(self.checked_names()))
        actions.flow.addWidget(self.reserve_image)
        actions.flow.addWidget(self.reserve_selected)
        layout.addWidget(actions)
        self.reservation_status = InlineNotice()
        layout.addWidget(self.reservation_status)
        self.update_actions()

    def invalidate(self):
        self.generation += 1
        self.catalog = None
        self.pending.clear()
        self.cache.clear()
        self.busy = False
        self.focused_name = None
        self.images.clear()
        self.items.clear()
        self.local_entries.clear()
        self.refresh.setEnabled(True)
        self.status.clear()
        self.error.clear()
        self.update_actions()

    def hideEvent(self, event):
        self.generation += 1
        self.pending.clear()
        self.busy = False
        super().hideEvent(event)

    def load(self):
        if not self.can_change_image():
            return
        checked, focused = set(self.checked_names()), self.focused_name
        self.invalidate()
        generation, token, repo = self.generation, self.hub.token, self.hub.get_dataset_repo()
        self.status.setText('Loading shared images…')
        self.error.clear()
        self.refresh.setEnabled(False)
        def ready(catalog, error):
            if not self.is_current(generation, token, repo):
                return
            self.refresh.setEnabled(True)
            if error:
                self.status.setText('Shared images could not be loaded.')
                self.error.set_error('Check your account and connection, then refresh.', error)
                return
            self.catalog = catalog
            self.account = self.hub.get_collaborator_id()
            self.local_entries = {name: payload for name, payload in self.local_previews().items()
                                  if name not in catalog['manifest'].get('images', {})}
            entries = dict(catalog['manifest'].get('images', {}))
            entries.update({name: payload['reservation'] for name, payload in self.local_entries.items()})
            key = lambda pair: [int(p) if p.isdigit() else p.lower() for p in re.split(r'(\d+)', pair[1].get('original_filename') or pair[0])]
            for name, reservation in sorted(entries.items(), key=key):
                state = 'Local image' if name in self.local_entries else self.state_text(reservation)
                boxes = (self.local_entries[name]['record'] if name in self.local_entries
                         else catalog['records'].get(name, {})).get('boxes', [])
                count = len(boxes)
                item = QListWidgetItem(f"{reservation.get('original_filename') or name}\n{state} · {count} "
                                       f"{'annotation' if count == 1 else 'annotations'}")
                item.setData(Qt.ItemDataRole.UserRole, name)
                item.setToolTip(reservation.get('original_filename') or name)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                if self.eligible(name):
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked if name in checked else Qt.CheckState.Unchecked)
                item.setSizeHint(QSize(0, max(90, 3 * self.fontMetrics().lineSpacing() + 12)))
                self.items[name] = item
                self.images.addItem(item)
            self.size_rows()
            self.apply_filters()
            if focused in self.items:
                self.images.setCurrentItem(self.items[focused])
            self.queue_visible()
        self.background(self.hub.fetch_image_catalog, ready)

    def is_current(self, generation, token, repo):
        return (not sip.isdeleted(self) and generation == self.generation
                and token == self.hub.token and repo == self.hub.get_dataset_repo())

    def catalog_summary(self):
        visible = sum(not item.isHidden() for item in self.items.values())
        if self.items and visible != len(self.items):
            return f'{visible} of {len(self.items)} images'
        local = len(self.local_entries)
        shared = len(self.items) - local
        parts = [f'{shared} shared {"image" if shared == 1 else "images"}'] if shared or not local else []
        if local:
            parts.append(f'{local} local {"image" if local == 1 else "images"}')
        return ' · '.join(parts)

    def state_text(self, record):
        state = record.get('status', 'available')
        if state == 'approved':
            return 'Approved'
        if state in ('locked', 'submitted') and expired(record):
            return 'Expired · available'
        if record.get('claimed_by') == self.account and state in ('locked', 'submitted'):
            return 'Reserved by you' + (' · awaiting review' if state == 'submitted' else '')
        if state in ('locked', 'submitted'):
            return 'Reserved by another account' + (' · awaiting review' if state == 'submitted' else '')
        return 'Available'

    def category(self, name):
        if name in self.local_entries:
            return 'local'
        record = self.catalog['manifest'].get('images', {}).get(name, {})
        state = record.get('status', 'available')
        if state == 'approved':
            return 'approved'
        if state in ('locked', 'submitted') and not expired(record):
            return 'mine' if record.get('claimed_by') == self.account else 'others'
        return 'available'

    def apply_filters(self, *_):
        selected = {category for category, checkbox in self.filters.items() if checkbox.isChecked()}
        # Hiding a row must not navigate away from the current preview or clear
        # its independent batch checkbox.
        self.images.blockSignals(True)
        try:
            for name, item in self.items.items():
                item.setHidden(bool(selected) and self.category(name) not in selected)
        finally:
            self.images.blockSignals(False)
        if self.catalog:
            self.status.setText(self.catalog_summary())
        self.update_actions()
        self.queue_visible()

    def eligible(self, name):
        if not self.catalog or not name:
            return False
        record = self.catalog['manifest'].get('images', {}).get(name, {})
        state = record.get('status')
        return state == 'available' or (state in ('locked', 'submitted') and
                (expired(record) or record.get('claimed_by') == self.account))

    def checked_names(self):
        return [self.images.item(row).data(Qt.ItemDataRole.UserRole)
                for row in range(self.images.count())
                if self.images.item(row).checkState() == Qt.CheckState.Checked]

    def select_first_available(self):
        remaining = self.count.value()
        for row in range(self.images.count()):
            item = self.images.item(row)
            name = item.data(Qt.ItemDataRole.UserRole)
            if not item.isHidden() and self.eligible(name):
                item.setCheckState(Qt.CheckState.Checked if remaining > 0 else Qt.CheckState.Unchecked)
                remaining -= 1

    def update_actions(self, *_):
        reservable = self.can_reserve_images()
        self.reserve_image.setEnabled(reservable and self.eligible(self.focused_name))
        self.reserve_selected.setEnabled(reservable and bool(self.checked_names()))
        self.select_first.setEnabled(any(not item.isHidden() and self.eligible(name)
                                        for name, item in self.items.items()))
        hidden = sum(item.isHidden() and item.checkState() == Qt.CheckState.Checked
                     for item in self.items.values())
        self.hidden_selection.setText(f'{hidden} selected {"image" if hidden == 1 else "images"} hidden' if hidden else '')
        self.selection_changed.emit()

    def focus_image(self, item, previous=None):
        if not item:
            return
        if not self.can_change_image():
            self.images.blockSignals(True)
            self.images.setCurrentItem(previous)
            self.images.blockSignals(False)
            return
        self.focused_name = item.data(Qt.ItemDataRole.UserRole)
        self.update_actions()
        if self.open_editable_image(self.focused_name):
            return
        if self.focused_name in self.cache:
            self.cache.move_to_end(self.focused_name)
            self.preview_ready.emit(copy.deepcopy(self.cache[self.focused_name][0]))
        else:
            self.status.setText('Loading image preview…')
            self.preview_loading.emit(self.focused_name)
            self.pending = [self.focused_name] + [n for n in self.pending if n != self.focused_name]
            self.pump()

    def queue_visible(self, *_):
        if not self.catalog or not self.isVisible():
            return
        for row in range(self.images.count()):
            item = self.images.item(row)
            if not item.isHidden() and self.images.visualItemRect(item).intersects(self.images.viewport().rect()):
                name = item.data(Qt.ItemDataRole.UserRole)
                if name not in self.cache and name not in self.pending:
                    self.pending.append(name)
        self.pump()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self.size_rows)
        self.queue_visible()

    def size_rows(self):
        width = max(40, self.images.viewport().width() - self.images.iconSize().width() - 42)
        metrics = self.images.fontMetrics()
        for item in self.items.values():
            bounds = metrics.boundingRect(QRect(0, 0, width, 100000), Qt.TextFlag.TextWordWrap, item.text())
            item.setSizeHint(QSize(0, max(90, bounds.height() + 16)))

    def pump(self):
        if self.busy or not self.pending or not self.catalog:
            return
        name = self.pending.pop(0)
        catalog = self.catalog
        generation, token, repo = self.generation, self.hub.token, self.hub.get_dataset_repo()
        self.busy = True
        def ready(result, error):
            if not self.is_current(generation, token, repo):
                return
            self.busy = False
            if error:
                if name == self.focused_name:
                    self.status.setText('Image preview unavailable.')
                    self.error.set_error('Select the image again to retry, or refresh the browser.', error)
            else:
                payload, thumb = result
                self.cache[name] = (payload, thumb)
                self.items[name].setIcon(QIcon(QPixmap.fromImage(thumb)))
                while len(self.cache) > 64:
                    old, _ = self.cache.popitem(last=False)
                    self.items[old].setIcon(QIcon())
                if name == self.focused_name:
                    self.status.setText(self.catalog_summary())
                    self.error.clear()
                    self.preview_ready.emit(copy.deepcopy(payload))
            self.pump()
        local = copy.deepcopy(self.local_entries.get(name))
        self.background(lambda: preview_thumbnail(local if local is not None else self.hub.fetch_image_preview(name, catalog)), ready)
