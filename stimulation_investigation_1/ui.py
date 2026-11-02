"""Acquisition's collection workspace; the Stimulation panel keeps output ownership."""
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QShortcut, QKeySequence
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLineEdit, QTextEdit, QLabel, QPushButton, QComboBox, QScrollArea, QTabWidget,
    QListWidget, QListWidgetItem, QSpinBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QFileDialog, QMessageBox, QSizePolicy, QDialog, QTextBrowser)

from tempo_ui import ErrorDetails, FlowRow, InformationButton
from .storage import Investigation, CONDITION_FIELDS, SCOPE_FIELDS, now
from .capture import RawCapture, MODE_NAMES


def plain_label(text=''):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return label


def scrolled(widget):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(widget)
    return scroll


class InvestigationWorkspace(QWidget):
    capture_changed = pyqtSignal()

    def __init__(self, tools, parent=None):
        super().__init__(parent)
        self.tools = tools
        self.investigation = Investigation()
        self.selected_condition = None
        self.scope_record = self.arduino_record = None
        self.capture = None
        self.dirty = False
        self.loading = False
        self.sidebar = QWidget()
        self.condition_widgets = {}
        self.scope_widgets = {}
        self._build_sidebar()
        self._build_center()
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.load_conditions()

    @property
    def capturing(self):
        return self.capture is not None and not self.capture.done

    def _build_sidebar(self):
        layout = QVBoxLayout(self.sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.conditions = QComboBox()
        self.conditions.setAccessibleName('Experimental condition')
        self.conditions.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.conditions.setMinimumContentsLength(8)
        self.conditions.currentIndexChanged.connect(self.select_condition)
        layout.addWidget(self.conditions)
        self.setup_form = QWidget()
        self.setup_form.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        body = QVBoxLayout(self.setup_form)
        body.setContentsMargins(6, 0, 6, 8)
        groups = (
            ('Condition', ('name', 'well', 'medium', 'fill_volume_ul')),
            ('Recorded signal', ('commanded_voltage_v', 'frequency_hz', 'pulse_width_ms', 'output')),
            ('Electrodes', ('electrode_material', 'electrode_dimensions', 'electrode_position_well')),
            ('Sensing wires', ('wire_material', 'wire_dimensions', 'wire_position_well', 'wire_position_electrodes')),
            ('Notes', ('notes',)),
        )
        labels = dict(CONDITION_FIELDS)
        for title, keys in groups:
            box = QGroupBox(title)
            form = QFormLayout(box)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            for key in keys:
                multiline = key.endswith('dimensions') or 'position_' in key or key == 'notes'
                edit = QTextEdit() if multiline else QLineEdit()
                if multiline:
                    edit.setAcceptRichText(False)
                    edit.setMinimumHeight(edit.fontMetrics().lineSpacing() * 2 + 16)
                    edit.setMaximumHeight(edit.fontMetrics().lineSpacing() * 3 + 16)
                edit.setMinimumWidth(0)
                edit.setAccessibleName(title + ' · ' + labels[key])
                edit.textChanged.connect(lambda *_, key=key: self.condition_edited(key))
                self.condition_widgets[key] = edit
                label = plain_label(labels[key] if key != 'notes' else '')
                label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
                form.addRow(label, edit)
            if title == 'Recorded signal':
                self.copy_settings_button = QPushButton('Copy settings')
                self.copy_settings_button.clicked.connect(self.copy_settings)
                form.addRow(self.copy_settings_button)
            body.addWidget(box)
        body.addStretch()
        self.setup_scroll = scrolled(self.setup_form)
        layout.addWidget(self.setup_scroll, 1)
        actions = FlowRow()
        self.add_condition_button = QPushButton('Add condition')
        self.add_condition_button.clicked.connect(self.add_condition)
        self.duplicate_button = QPushButton('Duplicate')
        self.duplicate_button.clicked.connect(self.duplicate_condition)
        actions.flow.addWidget(self.add_condition_button)
        actions.flow.addWidget(self.duplicate_button)
        layout.addWidget(actions)

    def _build_center(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        header = FlowRow()
        self.file_label = plain_label('New investigation')
        layout.addWidget(self.file_label)
        self.new_button = QPushButton('New…'); self.new_button.clicked.connect(self.new_investigation)
        self.open_button = QPushButton('Open…'); self.open_button.clicked.connect(self.open_investigation)
        self.export_button = QPushButton('Export…'); self.export_button.clicked.connect(self.export_investigation)
        self.guide_button = QPushButton('Guide…'); self.guide_button.clicked.connect(self.show_guide)
        for button in (self.new_button, self.open_button, self.export_button, self.guide_button):
            header.flow.addWidget(button)
        layout.addWidget(header)
        self.empty = plain_label('Add a condition to record measurements.')
        layout.addWidget(self.empty)
        self.empty_space = QWidget()
        self.empty_space.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.empty_space, 1)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._scope_page(), 'Oscilloscope')
        self.tabs.addTab(self._arduino_page(), 'Arduino')
        layout.addWidget(self.tabs, 1)
        self.error = ErrorDetails()
        layout.addWidget(self.error)
        footer = FlowRow()
        self.unsaved = QLabel('Unsaved changes')
        self.save_button = QPushButton('Save investigation')
        self.save_button.setProperty('primary', True)
        self.save_button.clicked.connect(self.save)
        footer.flow.addWidget(self.unsaved)
        footer.flow.addWidget(self.save_button)
        layout.addWidget(footer)

    def _scope_page(self):
        pane = QWidget(); layout = QVBoxLayout(pane)
        top = QHBoxLayout()
        top.addWidget(QLabel('Oscilloscope records')); top.addStretch()
        self.add_scope_button = QPushButton('Add record')
        self.add_scope_button.clicked.connect(self.add_scope_record)
        top.addWidget(self.add_scope_button); layout.addLayout(top)
        self.scope_list = QListWidget()
        self.scope_list.setAccessibleName('Oscilloscope records')
        self.scope_list.setMaximumHeight(self.fontMetrics().lineSpacing() * 3 + 22)
        self.scope_list.currentRowChanged.connect(self.select_scope_record)
        layout.addWidget(self.scope_list)
        self.scope_snapshot = QPushButton('Recorded setup…')
        self.scope_snapshot.clicked.connect(lambda: self.show_recorded_setup(self.scope_record))
        layout.addWidget(self.scope_snapshot, 0, Qt.AlignmentFlag.AlignLeft)
        self.scope_empty = plain_label('No oscilloscope records for this condition.')
        layout.addWidget(self.scope_empty)
        self.scope_empty_space = QWidget()
        self.scope_empty_space.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.scope_empty_space, 1)
        content = QWidget(); body = QVBoxLayout(content)
        self.scope_form = QFormLayout()
        self.scope_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.scope_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.scope_label = QLineEdit()
        self.scope_label.setAccessibleName('Oscilloscope record label')
        self.scope_label.textChanged.connect(self.scope_edited)
        self.scope_form.addRow('Record', self.scope_label)
        for key, title in SCOPE_FIELDS:
            edit = QLineEdit(); edit.setMinimumWidth(0); edit.setAccessibleName(title)
            edit.textChanged.connect(self.scope_edited)
            self.scope_widgets[key] = edit
            self.scope_form.addRow(title, edit)
        self.scope_notes = QTextEdit(); self.scope_notes.setAcceptRichText(False)
        self.scope_notes.setMinimumHeight(self.fontMetrics().lineSpacing() * 3 + 16)
        self.scope_notes.textChanged.connect(self.scope_edited)
        self.scope_form.addRow('Notes', self.scope_notes)
        body.addLayout(self.scope_form)
        self.scope_attachments = QListWidget()
        self.scope_attachments.setMaximumHeight(self.fontMetrics().lineSpacing() * 3 + 16)
        self.scope_attachments.itemActivated.connect(self.open_scope_attachment)
        body.addWidget(self.scope_attachments)
        self.attach_button = QPushButton('Attach file…'); self.attach_button.clicked.connect(self.attach_file)
        row = QHBoxLayout(); row.addWidget(self.attach_button); row.addStretch(); body.addLayout(row)
        body.addStretch()
        self.scope_scroll = scrolled(content)
        layout.addWidget(self.scope_scroll, 1)
        return pane

    def _arduino_page(self):
        pane = QWidget(); layout = QVBoxLayout(pane)
        self.device_status = plain_label()
        layout.addWidget(self.device_status)
        self.install_button = QPushButton('Install development firmware…')
        self.install_button.clicked.connect(lambda: self.tools.open_investigation_firmware())
        layout.addWidget(self.install_button, 0, Qt.AlignmentFlag.AlignLeft)
        config = QFormLayout()
        config.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        config.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.readings = QSpinBox(); self.readings.setRange(1, 64); self.readings.setValue(64)
        self.readings.setAccessibleName('Number of raw readings')
        self.interval = QSpinBox(); self.interval.setRange(100, 1000000); self.interval.setValue(500)
        self.interval.setSuffix(' µs'); self.interval.setAccessibleName('Requested reading interval')
        self.trigger = QComboBox(); self.trigger.addItem('Immediate', False); self.trigger.addItem('Next pulse', True)
        config.addRow('Readings', self.readings); config.addRow('Requested interval', self.interval); config.addRow('Trigger', self.trigger)
        layout.addLayout(config)
        capture_row = FlowRow()
        self.record_button = QPushButton('Record Arduino')
        self.record_button.setProperty('primary', True)
        self.record_button.clicked.connect(self.record_arduino)
        capture_row.flow.addWidget(self.record_button)
        self.raw_info = InformationButton('Raw Arduino readings',
            'A1 and A2 are sampled sequentially. Values are raw 14-bit ADC counts. '
            'Each reading retains device timestamps and output state before and after conversion. '
            'Requested intervals are not guaranteed sample timing. Recording does not start or stop stimulation.')
        capture_row.flow.addWidget(self.raw_info)
        layout.addWidget(capture_row)
        self.capture_state = plain_label()
        layout.addWidget(self.capture_state)
        self.arduino_list = QComboBox()
        self.arduino_list.setAccessibleName('Arduino records')
        self.arduino_list.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.arduino_list.setMinimumContentsLength(8)
        self.arduino_list.currentIndexChanged.connect(self.select_arduino_record)
        layout.addWidget(self.arduino_list)
        self.arduino_snapshot = QPushButton('Recorded setup…')
        self.arduino_snapshot.clicked.connect(lambda: self.show_recorded_setup(self.arduino_record))
        layout.addWidget(self.arduino_snapshot, 0, Qt.AlignmentFlag.AlignLeft)
        self.arduino_empty = plain_label('No Arduino records for this condition.')
        layout.addWidget(self.arduino_empty)
        self.raw_table = QTableWidget(0, 7)
        self.raw_table.setHorizontalHeaderLabels(['Input', 'Raw count', 'Start (µs)', 'End (µs)', 'Mode before', 'High before', 'High after'])
        self.raw_table.verticalHeader().hide()
        self.raw_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.raw_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.raw_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.raw_table, 1)
        self.arduino_notes = QTextEdit(); self.arduino_notes.setAcceptRichText(False)
        self.arduino_notes.setAccessibleName('Arduino record notes')
        self.arduino_notes.setMaximumHeight(self.fontMetrics().lineSpacing() * 4 + 16)
        self.arduino_notes.textChanged.connect(self.arduino_edited)
        self.arduino_notes_label = QLabel('Capture notes')
        layout.addWidget(self.arduino_notes_label); layout.addWidget(self.arduino_notes)
        self.arduino_empty_space = QWidget()
        self.arduino_empty_space.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.arduino_empty_space, 1)
        return pane

    def changed(self):
        if self.loading: return
        self.dirty = True
        self._quit_discarded = False
        self.unsaved.show(); self.save_button.setEnabled(True)

    def mark_saved(self):
        self.dirty = False
        self.unsaved.hide(); self.save_button.setEnabled(False)
        self.file_label.setText(self.investigation.path.name if self.investigation.path else 'New investigation')

    def report(self, exc):
        self.error.set_error(str(exc), exc)

    def add_condition(self):
        condition = self.investigation.add_condition('New condition')
        self.load_conditions(condition['id']); self.changed()

    def duplicate_condition(self):
        if self.selected_condition is None: return
        condition = self.investigation.duplicate_condition(self.selected_condition['id'])
        self.load_conditions(condition['id']); self.changed()

    def load_conditions(self, selected=None):
        self.conditions.blockSignals(True)
        self.conditions.clear()
        for condition in self.investigation.document['conditions']:
            self.conditions.addItem(condition['name'] or 'Unnamed condition', condition['id'])
        index = self.conditions.findData(selected) if selected else 0
        self.conditions.setCurrentIndex(max(0, index))
        self.conditions.blockSignals(False)
        self.select_condition()
        self.unsaved.setVisible(self.dirty); self.save_button.setEnabled(self.dirty)

    def select_condition(self, *_):
        identifier = self.conditions.currentData()
        self.selected_condition = self.investigation.condition(identifier) if identifier else None
        self.loading = True
        for key, edit in self.condition_widgets.items():
            text = self.selected_condition[key] if self.selected_condition else ''
            edit.setPlainText(text) if isinstance(edit, QTextEdit) else edit.setText(text)
        self.loading = False
        self.setup_form.setEnabled(self.selected_condition is not None)
        self.setup_scroll.setVisible(self.selected_condition is not None)
        self.conditions.setVisible(self.conditions.count() > 0)
        self.duplicate_button.setEnabled(self.selected_condition is not None and not self.capturing)
        self.tabs.setVisible(self.selected_condition is not None)
        self.empty.setVisible(self.selected_condition is None)
        self.empty_space.setVisible(self.selected_condition is None)
        self.refresh_records()

    def condition_edited(self, key):
        if self.loading or self.selected_condition is None: return
        edit = self.condition_widgets[key]
        self.selected_condition[key] = edit.toPlainText() if isinstance(edit, QTextEdit) else edit.text()
        if key == 'name':
            self.conditions.setItemText(self.conditions.currentIndex(), self.selected_condition[key] or 'Unnamed condition')
        self.changed()

    def copy_settings(self):
        if self.selected_condition is None: return
        status = self.tools.service.status if self.tools.service and self.tools.service.connected else None
        if status is None:
            self.report(ValueError('Connect the Arduino to copy its acknowledged settings.')); return
        parameters = status.parameters
        for key, value in (('commanded_voltage_v', parameters.amplitude_v), ('frequency_hz', parameters.frequency_hz),
                           ('pulse_width_ms', parameters.pulse_width_ms), ('output', parameters.channel + 1)):
            self.condition_widgets[key].setText(str(value))

    def records(self, instrument):
        identifier = self.selected_condition['id'] if self.selected_condition else None
        return [r for r in self.investigation.document['records']
                if r['condition_id'] == identifier and r['instrument'] == instrument]

    def refresh_records(self, scope_id=None, arduino_id=None):
        scope_id = scope_id or (self.scope_record['id'] if self.scope_record else None)
        arduino_id = arduino_id or (self.arduino_record['id'] if self.arduino_record else None)
        self.scope_list.blockSignals(True); self.scope_list.clear()
        for record in self.records('oscilloscope'):
            item = QListWidgetItem(record['label'] or 'Untitled record')
            item.setData(Qt.ItemDataRole.UserRole, record['id']); self.scope_list.addItem(item)
        selected = next((i for i in range(self.scope_list.count()) if self.scope_list.item(i).data(Qt.ItemDataRole.UserRole) == scope_id), self.scope_list.count() - 1)
        self.scope_list.setCurrentRow(selected); self.scope_list.blockSignals(False)
        self.select_scope_record()
        self.arduino_list.blockSignals(True); self.arduino_list.clear()
        for record in self.records('arduino'):
            self.arduino_list.addItem(record['label'] or record['created_at'], record['id'])
        index = self.arduino_list.findData(arduino_id)
        self.arduino_list.setCurrentIndex(index if index >= 0 else self.arduino_list.count() - 1)
        self.arduino_list.blockSignals(False); self.select_arduino_record()

    def add_scope_record(self):
        if self.selected_condition is None: return
        record = self.investigation.add_record(self.selected_condition['id'], 'oscilloscope', 'New record')
        self.refresh_records(scope_id=record['id']); self.changed(); self.scope_label.setFocus()

    def select_scope_record(self, *_):
        item = self.scope_list.currentItem()
        identifier = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.scope_record = next((r for r in self.records('oscilloscope') if r['id'] == identifier), None)
        self.loading = True
        self.scope_label.setText(self.scope_record['label'] if self.scope_record else '')
        for key, edit in self.scope_widgets.items():
            edit.setText(self.scope_record['values'].get(key, '') if self.scope_record else '')
        self.scope_notes.setPlainText(self.scope_record['notes'] if self.scope_record else '')
        self.loading = False
        self.scope_empty.setVisible(self.scope_record is None)
        self.scope_empty_space.setVisible(self.scope_record is None)
        self.scope_scroll.setVisible(self.scope_record is not None)
        self.scope_snapshot.setVisible(self.scope_record is not None)
        self.scope_list.setVisible(self.scope_list.count() > 0)
        self.refresh_attachments()

    def scope_edited(self):
        if self.loading or self.scope_record is None: return
        self.scope_record.update(label=self.scope_label.text(), notes=self.scope_notes.toPlainText(),
                                 values={key: edit.text() for key, edit in self.scope_widgets.items()})
        item = self.scope_list.currentItem()
        if item: item.setText(self.scope_record['label'] or 'Untitled record')
        self.changed()

    def refresh_attachments(self):
        self.scope_attachments.clear()
        if self.scope_record:
            for attachment in self.scope_record['attachments']:
                self.scope_attachments.addItem(attachment['name'])
        self.scope_attachments.setVisible(self.scope_attachments.count() > 0)

    def attach_file(self):
        if self.scope_record is None or not self.ensure_saved(): return
        filename, _ = QFileDialog.getOpenFileName(self, 'Attach oscilloscope file')
        if not filename: return
        try:
            self.investigation.attach(self.scope_record, filename)
            self.changed(); self.refresh_attachments()
        except Exception as exc: self.report(exc)

    def open_scope_attachment(self, item):
        if self.scope_record is None: return
        try:
            attachment = self.scope_record['attachments'][self.scope_attachments.row(item)]
            path = self.investigation.attachment_path(attachment)
            if not path.is_file(): raise FileNotFoundError('Attachment missing: ' + attachment['name'])
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as exc: self.report(exc)

    def select_arduino_record(self, *_):
        identifier = self.arduino_list.currentData()
        self.arduino_record = next((r for r in self.records('arduino') if r['id'] == identifier), None)
        self.loading = True
        self.arduino_notes.setPlainText(self.arduino_record['notes'] if self.arduino_record else '')
        self.loading = False
        self.arduino_notes.setEnabled(self.arduino_record is not None)
        self.arduino_notes.setVisible(self.arduino_record is not None)
        self.arduino_notes_label.setVisible(self.arduino_record is not None)
        self.arduino_empty_space.setVisible(self.arduino_record is None)
        self.arduino_list.setVisible(self.arduino_list.count() > 0)
        self.arduino_snapshot.setVisible(self.arduino_record is not None)
        self.arduino_empty.setVisible(self.arduino_record is None)
        self.refresh_raw_table()

    def refresh_raw_table(self):
        capture = (self.arduino_record or {}).get('capture') or {}
        rows = capture.get('samples', [])
        self.arduino_empty_space.setVisible(not rows)
        self.raw_table.setRowCount(len(rows))
        for row_index, sample in enumerate(rows):
            values = [f'A{sample["input"]}', str(sample['raw_count']), str(sample['started_us']),
                      str(sample['finished_us']), MODE_NAMES[sample['mode_before']],
                      str(sample['high_before']), str(sample['high_after'])]
            for column, value in enumerate(values):
                self.raw_table.setItem(row_index, column, QTableWidgetItem(value))
        self.raw_table.setVisible(bool(rows))
        if self.capturing:
            self.capture_state.setText('Waiting for next pulse…' if getattr(self.capture, 'device_state', '') == 'armed' else 'Recording Arduino…')
        else:
            labels = {'complete': 'Capture complete', 'cancelled': 'Capture cancelled',
                      'interrupted': 'Capture interrupted', 'recording': 'Capture incomplete'}
            self.capture_state.setText(labels.get(capture.get('state'), ''))

    def arduino_edited(self):
        if self.loading or self.arduino_record is None: return
        self.arduino_record['notes'] = self.arduino_notes.toPlainText(); self.changed()

    def show_recorded_setup(self, record):
        if record is None: return
        import json
        dialog = QDialog(self)
        dialog.setWindowTitle('TEMPO · Recorded Setup')
        dialog.resize(560, 580)
        layout = QVBoxLayout(dialog)
        text = QTextEdit(); text.setReadOnly(True)
        snapshot = record['condition_snapshot']
        lines = [record['label'] or record['created_at'], '']
        for key, title in CONDITION_FIELDS:
            if snapshot.get(key):
                if key.startswith('electrode_'): title = 'Stimulation electrodes · ' + title
                elif key.startswith('wire_'): title = 'Sensing wires · ' + title
                lines.extend([title, snapshot[key], ''])
        if record.get('capture'):
            capture = {key: value for key, value in record['capture'].items() if key != 'samples'}
            lines.extend(['Arduino capture', json.dumps(capture, ensure_ascii=False, indent=2)])
        text.setPlainText('\n'.join(lines)); layout.addWidget(text)
        QShortcut(QKeySequence.StandardKey.Close, dialog, activated=dialog.close)
        self._snapshot_dialog = dialog; dialog.show()

    def show_guide(self):
        dialog = QDialog(self); dialog.setWindowTitle('TEMPO · Stimulation Investigation 1 Guide')
        dialog.resize(700, 660)
        layout = QVBoxLayout(dialog)
        guide = QTextBrowser()
        try:
            path = Path(__file__).resolve().parent.parent / 'docs' / 'STIMULATION_INVESTIGATION_1.md'
            guide.setMarkdown(path.read_text(encoding='utf-8'))
        except OSError as exc:
            self.report(exc); return
        layout.addWidget(guide)
        QShortcut(QKeySequence.StandardKey.Close, dialog, activated=dialog.close)
        self._guide_dialog = dialog; dialog.show()

    def record_arduino(self):
        if self.capturing:
            self.capture.cancel(); self.record_button.setText('Cancelling…'); return
        if self.selected_condition is None or not self.ensure_saved(): return
        try:
            self.error.clear()
            self.capture = RawCapture(self.investigation, self.selected_condition['id'], self.tools.service,
                readings=self.readings.value(), interval_us=self.interval.value(),
                next_pulse=self.trigger.currentData())
            self.refresh_records(arduino_id=self.capture.record['id'])
            self.mark_saved(); self.capture_changed.emit(); self.refresh_device()
        except Exception as exc:
            self.changed(); self.refresh_records(); self.report(exc)

    def poll(self):
        if self.capturing:
            try:
                saved = self.investigation._saved_bytes
                self.capture.tick()
                if self.investigation._saved_bytes != saved:
                    self.mark_saved()
            except Exception as exc:
                if self.capture.save_error: self.changed()
                self.report(exc)
            self.refresh_raw_table()
            if self.capture.done: self.capture_changed.emit()
        self.refresh_device()

    def refresh_device(self):
        service = self.tools.service
        connected = service is not None and service.connected
        supported = connected and service.investigation_protocol == 1
        self.device_status.setText('Connect the Arduino in Stimulation.' if not connected else
                                  ('Development firmware required for Arduino recording.' if not supported else ''))
        self.device_status.setVisible(bool(self.device_status.text()))
        self.install_button.setVisible(not supported)
        self.install_button.setEnabled(not self.capturing and not self.tools.firmware_busy())
        self.record_button.setEnabled(self.capturing or (supported and self.selected_condition is not None))
        if self.capturing:
            self.record_button.setText('Cancelling…' if self.capture.cancel_requested else 'Cancel capture')
        else:
            self.record_button.setText('Record Arduino')
        for edit in (self.readings, self.interval, self.trigger, self.conditions, self.add_condition_button,
                     self.duplicate_button, self.setup_form, self.new_button, self.open_button):
            edit.setEnabled(not self.capturing)
        self.setup_form.setEnabled(not self.capturing and self.selected_condition is not None)
        self.duplicate_button.setEnabled(not self.capturing and self.selected_condition is not None)

    def ensure_saved(self):
        return self.save() if self.dirty or self.investigation.path is None else True

    def save(self):
        destination = None
        if self.investigation.path is None:
            filename, _ = QFileDialog.getSaveFileName(self, 'Save investigation', 'stimulation-investigation-1.json', 'Investigation (*.json)')
            if not filename: return False
            destination = filename
        try:
            self.investigation.save(destination)
            self.error.clear(); self.mark_saved(); return True
        except Exception as exc:
            self.changed(); self.report(exc); return False

    def prepare_close(self, confirm_once=False):
        if self.capturing:
            self.report(ValueError('Cancel Arduino capture before closing TEMPO.')); return False
        if not self.dirty: return True
        if confirm_once and getattr(self, '_quit_discarded', False): return True
        answer = QMessageBox.question(self, 'TEMPO · Unsaved investigation', 'Save changes to this investigation?',
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save)
        if answer == QMessageBox.StandardButton.Cancel: return False
        if answer == QMessageBox.StandardButton.Discard and confirm_once:
            self._quit_discarded = True
        return self.save() if answer == QMessageBox.StandardButton.Save else True

    def new_investigation(self):
        if not self.prepare_close(): return
        self.investigation = Investigation(); self.scope_record = self.arduino_record = None
        self.capture = None; self.dirty = False; self.load_conditions(); self.mark_saved(); self.error.clear()

    def open_investigation(self):
        if not self.prepare_close(): return
        filename, _ = QFileDialog.getOpenFileName(self, 'Open investigation', '', 'Investigation (*.json)')
        if not filename: return
        try:
            investigation = Investigation.open(filename)
            recovered = False
            for record in investigation.document['records']:
                capture = record.get('capture')
                if capture and capture['state'] == 'recording':
                    capture.update(state='interrupted', finished_at=now(), error='TEMPO closed before capture completion.')
                    recovered = True
            self.investigation = investigation; self.scope_record = self.arduino_record = None
            self.capture = None; self.dirty = recovered; self.load_conditions()
            self.file_label.setText(investigation.path.name); self.error.clear()
        except Exception as exc: self.report(exc)

    def export_investigation(self):
        filename, _ = QFileDialog.getSaveFileName(self, 'Export investigation', 'stimulation-investigation-1.zip', 'ZIP archive (*.zip)')
        if not filename: return
        try:
            self.investigation.export(filename); self.error.clear()
        except Exception as exc: self.report(exc)
