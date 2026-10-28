"""Application-wide interaction styling, including dynamically created dialogs."""
from pathlib import Path
import os
import sys
from html import escape
from PyQt6.QtCore import QObject, QEvent, Qt, QSize, QRectF, QPoint, QTimer, pyqtSignal
from PyQt6.QtGui import QPalette, QColor, QTextOption, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QAbstractButton, QCheckBox, QSlider, QDialog, QMessageBox, QTextEdit, QFrame, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QToolButton, QToolTip, QSizePolicy, QLayout, QGroupBox, QGridLayout, QFormLayout, QLineEdit, QTabWidget, QScrollArea, QProxyStyle, QStyle, QStyleOptionButton


class HelpController(QObject):
    """One bounded, widget-anchored help surface for labels and information icons."""
    def __init__(self, app):
        super().__init__(app)
        self.source = self.bubble = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.show_help)
        self.leave_timer = QTimer(self)
        self.leave_timer.setSingleShot(True)
        self.leave_timer.setInterval(160)
        self.leave_timer.timeout.connect(self.dismiss_if_outside)
        app.installEventFilter(self)

    def dismiss_if_outside(self):
        if self.source is not None and not getattr(self, 'keyboard_open', False) and not self.source.underMouse():
            if self.bubble is None or not self.bubble.underMouse():
                self.dismiss()

    def dismiss(self):
        from PyQt6 import sip
        self.timer.stop()
        self.leave_timer.stop()
        if self.bubble is not None and not sip.isdeleted(self.bubble):
            self.bubble.hide()
            self.bubble.deleteLater()
        self.source = self.bubble = None

    def show_help(self, source=None):
        from PyQt6 import sip
        keyboard_open = source is not None
        source = source or self.source
        if source is None or sip.isdeleted(source) or not source.isVisible():
            self.dismiss(); return
        self.dismiss()
        self.source = source
        self.keyboard_open = keyboard_open
        window = source.window()
        bubble = self.bubble = QFrame(window)
        bubble.setObjectName('tempoHelpPopup')
        bubble.setAccessibleName(source.accessibleName() or source.property('tempoHelpText'))
        bubble.setStyleSheet('#tempoHelpPopup { background: #253447; border: 1px solid #71859c; border-radius: 4px; }')
        layout = QVBoxLayout(bubble); layout.setContentsMargins(5, 5, 5, 5)
        scroll = QScrollArea(); scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        label = QLabel(source.property('tempoHelpText'))
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setFont(window.font()); label.setWordWrap(True)
        label.setStyleSheet('color: #edf2f7; background: transparent; border: none;')
        scroll.setWidget(label); layout.addWidget(scroll)
        bubble.help_scroll = scroll
        width = min(360, max(40, window.width() - 12))
        natural = label.fontMetrics().horizontalAdvance(label.text()) + 14
        width = min(width, max(100, natural))
        anchor = source.mapTo(window, QPoint(0, source.height()))
        top = source.mapTo(window, QPoint()).y()
        below, above = window.height() - anchor.y() - 12, top - 12
        height = min(max(30, label.heightForWidth(width - 14) + 14),
                     max(20, below, above), window.height() - 12)
        bubble.resize(width, height)
        y = anchor.y() + 6 if below >= height else top - height - 6
        bubble.move(max(6, min(anchor.x(), window.width() - width - 6)),
                    max(6, min(y, window.height() - height - 6)))
        bubble.show(); bubble.raise_()

    def eventFilter(self, obj, event):
        from PyQt6 import sip
        if self.source is not None and sip.isdeleted(self.source):
            self.dismiss()
        kind = event.type()
        if isinstance(obj, QWidget) and obj.property('tempoHelpText'):
            if kind == QEvent.Type.ToolTip:
                return True
            if kind == QEvent.Type.Enter and obj.isEnabled():
                self.dismiss(); self.source = obj; self.timer.start()
            elif kind == QEvent.Type.Leave and obj is self.source:
                self.leave_timer.start()
            elif kind in (QEvent.Type.Hide, QEvent.Type.FocusOut) and obj is self.source:
                self.dismiss()
            elif kind == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_F1):
                self.show_help(obj); return True
        if self.source is not None:
            if self.bubble is not None and isinstance(obj, QWidget) and (obj is self.bubble or self.bubble.isAncestorOf(obj)):
                if kind == QEvent.Type.Enter: self.leave_timer.stop()
                elif kind == QEvent.Type.Leave: self.leave_timer.start()
            if kind == QEvent.Type.KeyPress and self.bubble is not None and event.key() in (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                bar = self.bubble.help_scroll.verticalScrollBar()
                bar.setValue(bar.value() + (bar.pageStep() if event.key() == Qt.Key.Key_PageDown else -bar.pageStep()))
                return True
            if (kind == QEvent.Type.Wheel and not (self.bubble is not None and isinstance(obj, QWidget) and self.bubble.isAncestorOf(obj))) or (kind == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Tab)):
                self.dismiss()
            elif kind in (QEvent.Type.Hide, QEvent.Type.Resize, QEvent.Type.WindowDeactivate) and obj is self.source.window():
                self.dismiss()
            elif kind == QEvent.Type.MouseButtonPress and obj is not self.source and not (self.bubble is not None and isinstance(obj, QWidget) and (obj is self.bubble or self.bubble.isAncestorOf(obj))):
                self.dismiss()
        return False


def help_controller():
    app = QApplication.instance()
    if not hasattr(app, '_tempo_help'):
        app._tempo_help = HelpController(app)
    return app._tempo_help


def install_help(widget, details, panel=None, title=None):
    if panel is not None:
        panel.bind(widget, title or widget.text(), details)
        return
    widget.setProperty('tempoHelpText', details)
    widget.setToolTip(details)
    widget.setAccessibleDescription(details)
    widget.setMouseTracking(True)
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    help_controller()


class ContextHelpPanel(QGroupBox):
    """Keep explanations readable beside the values, including keyboard focus."""
    def __init__(self, parent=None):
        super().__init__('Measurement details', parent)
        layout = QVBoxLayout(self)
        self.topic = QLabel()
        self.topic.setTextFormat(Qt.TextFormat.PlainText)
        self.topic.setWordWrap(True)
        self.topic.setStyleSheet('font-weight: bold;')
        self.details = QLabel('Hover over any statistic label for information. You can also use Tab to focus a label.')
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for label in (self.topic, self.details):
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            layout.addWidget(label)
        self.topic.hide()
        layout.addStretch(1)
        self.setMinimumHeight(self.fontMetrics().lineSpacing() * 6)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.MinimumExpanding)

    def bind(self, widget, title, details):
        widget.setProperty('tempoHelpText', None)
        widget.setProperty('contextHelpTitle', title.rstrip(':'))
        widget.setProperty('contextHelpDetails', details)
        widget.setToolTip('')
        widget.setAccessibleDescription(details)
        widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        widget.setMouseTracking(True)
        widget.installEventFilter(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_text()

    def fit_text(self):
        width = max(40, self.width() - 28)
        height = max(0, self.details.heightForWidth(width))
        if not self.topic.isHidden():
            height += max(0, self.topic.heightForWidth(width)) + self.layout().spacing()
        self.setMinimumHeight(max(self.fontMetrics().lineSpacing() * 6, height + 52))

    def eventFilter(self, widget, event):
        if event.type() in (QEvent.Type.Enter, QEvent.Type.FocusIn):
            self.topic.setText(widget.property('contextHelpTitle'))
            self.topic.show()
            self.details.setText(widget.property('contextHelpDetails'))
            self.fit_text()
            self.updateGeometry()
        return event.type() == QEvent.Type.ToolTip


class ContainedToolsMenu(QFrame):
    """Keyboard-accessible tools overlay contained within its application window."""
    def __init__(self, anchor, actions):
        super().__init__(anchor.window())
        self.anchor = anchor
        self.setObjectName('tempoToolsMenu')
        self.setStyleSheet('#tempoToolsMenu { background: #253447; border: 1px solid #71859c; }')
        self.setAccessibleName('Application Tools')
        outer = QVBoxLayout(self); outer.setContentsMargins(4, 4, 4, 4)
        scroll = self.scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(); rows = QVBoxLayout(content); rows.setContentsMargins(0, 0, 0, 0)
        self.buttons = []
        for title, action in actions:
            button = QPushButton(title)
            self.buttons.append(button)
            button.clicked.connect(lambda _, callback=action: self.activate(callback))
            rows.addWidget(button)
        scroll.setWidget(content); outer.addWidget(scroll)
        self.hide()
        QApplication.instance().installEventFilter(self)

    def activate(self, callback):
        self.hide(); self.anchor.setFocus(); callback()

    def toggle(self):
        if self.isVisible():
            self.hide(); return
        self.setParent(self.anchor.window())
        window = self.window()
        point = self.anchor.mapTo(window, QPoint(0, self.anchor.height()))
        width = min(window.width() - 12, max(b.sizeHint().width() for b in self.buttons) + 24)
        natural = sum(b.sizeHint().height() for b in self.buttons) + 40
        below = window.height() - point.y() - 12
        above = point.y() - self.anchor.height() - 12
        height = min(natural, max(below, above), window.height() - 12)
        y = point.y() + 6 if below >= height else point.y() - self.anchor.height() - height - 6
        self.setGeometry(max(6, min(point.x(), window.width() - width - 6)), max(6, y), width, height)
        self.show(); self.raise_(); self.buttons[0].setFocus()

    def eventFilter(self, obj, event):
        if self.isVisible():
            if obj in self.buttons and event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Home, Qt.Key.Key_End):
                index = self.buttons.index(obj)
                if event.key() == Qt.Key.Key_Home: index = 0
                elif event.key() == Qt.Key.Key_End: index = len(self.buttons) - 1
                else: index = (index + (1 if event.key() == Qt.Key.Key_Down else -1)) % len(self.buttons)
                self.buttons[index].setFocus()
                self.scroll.ensureWidgetVisible(self.buttons[index], 0, 0)
                return True
            if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self.hide(); self.anchor.setFocus(); return True
            if event.type() == QEvent.Type.MouseButtonPress and isinstance(obj, QWidget) and obj is not self.anchor and obj is not self and not self.isAncestorOf(obj):
                self.hide()
            if event.type() in (QEvent.Type.Resize, QEvent.Type.WindowDeactivate, QEvent.Type.Hide) and obj is self.window():
                self.hide()
        return False


class InformationButton(QToolButton):
    """Optional feature context, available by hover or keyboard activation."""
    def __init__(self, topic, details, parent=None):
        super().__init__(parent)
        self.setText('i')
        self.setAccessibleName(f'{topic} information')
        self.setAccessibleDescription(details)
        self.setToolTip('<qt>' + escape(details).replace('\n', '<br>') + '</qt>')
        install_help(self, details)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setMouseTracking(True)
        self.clicked.connect(self.show_information)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.resize_for_font()

    def resize_for_font(self):
        side = max(20, self.fontMetrics().height() + 4)
        self.setFixedSize(side, side)
        self.updateGeometry()

    def sizeHint(self):
        side = max(20, self.fontMetrics().height() + 4)
        return QSize(side, side)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        circle = QRectF(self.rect()).adjusted(2,2,-2,-2)
        painter.setPen(QPen(QColor('#71859c'),1))
        painter.setBrush(QColor('#253447') if self.underMouse() else QColor(Qt.GlobalColor.transparent))
        painter.drawEllipse(circle)
        font = self.font(); font.setItalic(False); font.setBold(True)
        painter.setFont(font); painter.setPen(QColor('#edf2f7' if self.underMouse() else '#a9bacb'))
        painter.drawText(circle,Qt.AlignmentFlag.AlignCenter,'i')

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        if QToolTip.text() == self.toolTip():
            QToolTip.hideText()
        super().leaveEvent(event)
        self.update()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space,Qt.Key.Key_Return,Qt.Key.Key_Enter):
            self.show_information(); event.accept()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() in (Qt.Key.Key_Space,Qt.Key.Key_Return,Qt.Key.Key_Enter):
            event.accept()
        else:
            super().keyReleaseEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self.resize_for_font()

    def show_information(self):
        help_controller().show_help(self)


class OnOffSwitch(QAbstractButton):
    """An immediate binary request; callers separately display acknowledged state."""
    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setAccessibleName(text)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        height = max(28, self.fontMetrics().height() + 10)
        if not self.text(): return QSize(round(max(20,height-6)*1.8)+6,height)
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + height * 2 + 14, height)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        height = max(20, self.height() - 6)
        track = QRectF(3, 3, height * 1.8, height)
        color = '#338c73' if self.isChecked() else '#536172'
        if not self.isEnabled(): color = '#3c4651'
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color)); painter.drawRoundedRect(track,height/2,height/2)
        diameter = height - 6
        x = track.right() - diameter - 3 if self.isChecked() else track.left() + 3
        painter.setBrush(QColor('#f2f6fa' if self.isEnabled() else '#8b949e'))
        painter.drawEllipse(QRectF(x,track.top()+3,diameter,diameter))
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()) if self.isEnabled() else QColor('#8b949e'))
        if self.text(): painter.drawText(QRectF(track.right()+9,0,self.width()-track.right()-9,self.height()),Qt.AlignmentFlag.AlignVCenter,self.text())


class SlidingChoice(QSlider):
    """Two exposed choices beneath a transparent, draggable selection overlay."""
    currentIndexChanged = pyqtSignal(int)

    def __init__(self, labels, values, parent=None):
        super().__init__(Qt.Orientation.Horizontal,parent)
        self.labels, self.values = tuple(labels), tuple(values)
        if len(self.labels) != 2 or len(self.values) != 2:
            raise ValueError('SlidingChoice requires exactly two choices.')
        self.setRange(0,1); self.setSingleStep(1); self.setPageStep(1)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self._drag_position = None
        self.valueChanged.connect(self._changed)

    def _changed(self, value):
        self.setAccessibleDescription(f'{self.labels[value]}. Left/Right selects a mode; click or drag either choice.')
        self.currentIndexChanged.emit(value)
        self.update()

    def currentData(self): return self.values[self.value()]
    def currentIndex(self): return self.value()
    def setCurrentIndex(self, index): self.setValue(index)
    def findData(self, value): return self.values.index(value) if value in self.values else -1

    def sizeHint(self):
        return QSize(2*max(self.fontMetrics().horizontalAdvance(label) for label in self.labels)+36,
                     max(30,self.fontMetrics().height()+14))

    def minimumSizeHint(self): return self.sizeHint()

    def paintEvent(self,event):
        painter=QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cell=(self.width()-4)/2
        position=self.value() if self._drag_position is None else self._drag_position
        overlay=QRectF(2+cell*position,2,cell,self.height()-4)
        painter.setPen(QPen(QColor(124,228,186,130 if self.isEnabled() else 55),1))
        painter.setBrush(QColor(124,228,186,24 if self.isEnabled() else 10))
        painter.drawRoundedRect(overlay,4,4)
        painter.setFont(self.font())
        for index,label in enumerate(self.labels):
            painter.setPen(QColor('#edf2f7' if self.isEnabled() else '#99a3b0'))
            painter.drawText(QRectF(2+index*cell,0,cell,self.height()),Qt.AlignmentFlag.AlignCenter,label)

    def mousePressEvent(self,event):
        if event.button()!=Qt.MouseButton.LeftButton: event.ignore(); return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._press_x=event.position().x(); self._start_position=float(self.value())
        self._drag_position=self._start_position; self._dragged=False
        self.update(); event.accept()

    def mouseMoveEvent(self,event):
        if self._drag_position is None: return
        delta=event.position().x()-self._press_x
        if abs(delta)>3: self._dragged=True
        self._drag_position=max(0.,min(1.,self._start_position+delta/max(1,(self.width()-4)/2)))
        self.update(); event.accept()

    def mouseReleaseEvent(self,event):
        if event.button()!=Qt.MouseButton.LeftButton or self._drag_position is None: return
        choice=(1 if self._drag_position>=.5 else 0) if self._dragged else int(event.position().x()>=self.width()/2)
        self._drag_position=None
        self.setValue(choice); self.update(); event.accept()


class InformationSlot(QWidget):
    """Reserve real layout space around an icon embedded in a tab."""
    def __init__(self, information, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3,3,3,3)
        layout.addWidget(information,0,Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed,QSizePolicy.Policy.Fixed)


class FilePickerRow(QWidget):
    """A file field and its action share a baseline and content-sized height."""
    def __init__(self, field, button, parent=None):
        super().__init__(parent)
        self.field, self.button = field, button
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0,0,0,0); layout.setSpacing(8)
        layout.addWidget(field,1); layout.addWidget(button)
        button.setStyleSheet('QPushButton { padding: 4px 10px; }')
        self.sync_geometry()

    def sync_geometry(self):
        self.field.ensurePolished(); self.button.ensurePolished()
        height = max(self.field.sizeHint().height(), self.field.fontMetrics().height()+10)
        self.field.setFixedHeight(height); self.button.setFixedHeight(height)
        self.button.setFixedWidth(self.button.fontMetrics().horizontalAdvance(self.button.text())+22)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange,QEvent.Type.StyleChange):
            self.sync_geometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.sync_geometry()


class ErrorDetails(QWidget):
    """Keep recovery instructions visible and technical failure details optional."""
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.summary = QLabel(); self.summary.setWordWrap(True)
        self.summary.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.toggle = QPushButton('Show Details'); self.toggle.setCheckable(True)
        self.details = QTextEdit(); self.details.setReadOnly(True); self.details.setMaximumHeight(160)
        self.details.setAccessibleName('Technical error details')
        self.details.setTabChangesFocus(True)
        layout.addWidget(self.summary)
        layout.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.details)
        self.toggle.toggled.connect(self.details.setVisible)
        self.toggle.toggled.connect(lambda shown: self.toggle.setText('Hide Details' if shown else 'Show Details'))
        self.clear()

    def clear(self):
        self.toggle.setChecked(False); self.details.hide()
        self.summary.clear(); self.details.clear(); self.hide()

    def set_error(self, summary, error, *, secrets=()):
        from tempo_credentials import redact_credentials
        self.clear()
        self.summary.setText(redact_credentials(summary, *secrets))
        self.details.setPlainText(f'{type(error).__name__}: {redact_credentials(error, *secrets)}')
        self.show()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.summary.setMinimumHeight(max(0, self.summary.heightForWidth(self.summary.width())))


class FilenameLabel(QTextEdit):
    """Selectable filenames wrap even when they contain no spaces."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty('filename', True)
        self.setReadOnly(True)
        self.setTabChangesFocus(True)
        self.setAcceptRichText(False)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(0)
        self.document().documentLayout().documentSizeChanged.connect(
            lambda size: self.setFixedHeight(min(100, max(20, int(size.height()) + 4))))

    def setText(self, text):
        self.setPlainText(text)


def confirm_action(parent, title, message, action):
    """Name the consequence on the button rather than using Yes/No."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(title)
    box.setInformativeText(message)
    proceed = box.addButton(action, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.exec()
    return box.clickedButton() is proceed

def format_model_metric(record):
    """Readable scores without inventing a zero when a result is absent."""
    import math
    names = {'mAP50': 'Mean average precision (IoU 0.50)',
             'instance_F1_IoU50': 'Instance F1 (IoU 0.50)',
             'cell_class_F1_IoU50': 'Cell detection and class F1 (IoU 0.50)',
             'object_class_F1_IoU50': 'Cell/group detection and class F1 (IoU 0.50)'}
    if not isinstance(record, dict):
        return 'Test score unavailable'
    metric = names.get(record.get('metric'), record.get('metric') or 'Test score')
    score = record.get('score')
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
        return f'{metric}: unavailable'
    text = f'{metric}: {score:.4f}'
    class_scores = record.get('class_scores', {})
    if record.get('metric') == 'cell_class_F1_IoU50' and isinstance(class_scores, dict) and all(
            isinstance(class_scores.get(name), (int, float)) and not isinstance(class_scores.get(name), bool)
            and math.isfinite(class_scores[name]) for name in ('healthy', 'dead')):
        text += f'\nHealthy: {class_scores["healthy"]:.4f} · Dead: {class_scores["dead"]:.4f}'
    if record.get('metric') == 'object_class_F1_IoU50' and isinstance(class_scores, dict):
        labels = {'healthy': 'Healthy cells', 'dead': 'Dead cells',
                  'healthy_group': 'Healthy groups', 'dead_group': 'Dead groups'}
        values = [f'{label}: {class_scores[name]:.4f}' for name,label in labels.items()
                  if isinstance(class_scores.get(name), (int,float)) and not isinstance(class_scores[name], bool)
                  and math.isfinite(class_scores[name])]
        if values:
            text += '\n' + ' · '.join(values)
    return text


UI_REVISION = 'presentation-2026-10-14-r11'
ACTION_GREEN = '#3EA685'
DESCRIPTION_BLUE = '#79C0FF'
GUIDANCE_MINT = '#7CE4BA'

APP_STYLE = '''
QWidget { outline: none; }
QPushButton { background: #333333; color: #edf2f7; border: 1px solid #526174;
 border-radius: 4px; padding: 6px 10px; }
QPushButton:hover { background: #354b61; border-color: #8db5d9; }
QPushButton:pressed { background: #1b2938; }
QPushButton, QToolButton { outline: none; }
QToolButton { border: 1px solid #526174; }
#tempoToolsMenu QPushButton { background: #2f4052; outline: none; }
#tempoToolsMenu QPushButton:hover { background: #354b61; border-color: #8db5d9; }
#tempoToolsMenu QPushButton:pressed { background: #1b2938; border-color: #526174; }
QPushButton[primary="true"]:enabled { background: ACTION_GREEN_TOKEN; border-color: ACTION_GREEN_TOKEN; color: white; }
QPushButton[plateWell="true"] { padding: 0; min-height: 26px; max-height: 26px; font-size: 11px; }
QPushButton:disabled { color: #99a3b0; background: #252b34; border-color: #39414c; }
QLineEdit, QTextEdit, QListWidget, QTableWidget, QSpinBox, QDoubleSpinBox, QComboBox {
 background: #111923; color: #edf2f7; border: 1px solid #465467; border-radius: 3px; padding: 4px; }
QLineEdit:disabled, QTextEdit:disabled, QListWidget:disabled, QTableWidget:disabled,
QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {
 background: #252b34; color: #99a3b0; border-color: #39414c; }
QTextEdit[filename="true"] { background: transparent; border: none; padding: 0; }
QAbstractItemView::item:selected { background: #315776; color: #ffffff; }
QGroupBox { border: 1px solid #3c4959; border-radius: 4px; margin-top: 9px; padding-top: 4px; }
QGroupBox::title { subcontrol-origin: margin; left: 9px; padding: 0 4px; }
QTabWidget::pane { border: 1px solid #3c4959; }
QTabBar::tab { padding: 7px 10px; background: #222e3d; color: #d6dfe9; }
QTabBar::tab:selected { background: #315776; color: #ffffff; }
QProgressBar { background: #252e3a; border: 1px solid #465467; border-radius: 3px; color: #edf2f7; text-align: center; min-height: 18px; }
QProgressBar::chunk { background: #357f70; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #8495a9; border-radius: 3px; background: #111923; }
QCheckBox::indicator:hover { border-color: #91c9f7; }
QCheckBox::indicator:checked { background: #26675c; image: url("CHECKBOX_IMAGE"); }
QCheckBox::indicator:disabled { border-color: #526174; background: #252b34; }
QToolTip { background: #253447; color: #ffffff; border: 1px solid #71859c; padding: 5px; }
'''.replace('ACTION_GREEN_TOKEN', ACTION_GREEN).replace('CHECKBOX_IMAGE', (Path(__file__).parent / 'assets' / 'checkbox-check.svg').as_posix())
for color in ('#2D9CDB', ACTION_GREEN, '#F2994A', '#E74C3C', '#1A5276', '#0088CC',
              '#9B51E0', '#E67E22', '#AA00FF', '#4A5568', '#253447'):
    APP_STYLE += f'''
QPushButton[actionColor="{color}"]:enabled {{ background: {color}; color: white; border-color: {color}; }}
QPushButton[actionColor="{color}"]:enabled:hover {{ background: {QColor(color).lighter(112).name()}; }}
QPushButton[actionColor="{color}"]:enabled:pressed {{ background: {QColor(color).darker(115).name()}; }}
'''


def set_button_color(button, color):
    button.setProperty('actionColor', color)
    button.style().unpolish(button)
    button.style().polish(button)

DIALOG_STYLE = ''  # Dialogs inherit the same application theme as the workspace.


class FocusNeutralStyle(QProxyStyle):
    """Suppress focus decoration without changing focus or activation behavior."""
    @staticmethod
    def paint_option(option):
        copied = type(option)(option)
        copied.state &= ~QStyle.StateFlag.State_HasFocus
        if isinstance(copied, QStyleOptionButton):
            copied.features &= ~(QStyleOptionButton.ButtonFeature.DefaultButton |
                                 QStyleOptionButton.ButtonFeature.AutoDefaultButton)
        return copied

    def drawPrimitive(self, element, option, painter, widget=None):
        if element != QStyle.PrimitiveElement.PE_FrameFocusRect:
            super().drawPrimitive(element, self.paint_option(option), painter, widget)

    def drawControl(self, element, option, painter, widget=None):
        super().drawControl(element, self.paint_option(option), painter, widget)

    def drawComplexControl(self, control, option, painter, widget=None):
        super().drawComplexControl(control, self.paint_option(option), painter, widget)


class InteractionStyle(QObject):
    def eventFilter(self, obj, event):
        if isinstance(obj, QGroupBox) and event.type() in (QEvent.Type.Polish, QEvent.Type.Show, QEvent.Type.FontChange):
            # Fixed nine-pixel margins overlap 18-point titles. Keep ordinary
            # spacing compact and reserve the extra height a larger title needs.
            margin = max(9, obj.fontMetrics().height() - 9)
            if obj.property('tempoTitleMargin') != margin:
                obj.setProperty('tempoTitleMargin', margin)
                if not hasattr(obj, '_tempo_group_base_style'):
                    obj._tempo_group_base_style = obj.styleSheet()
                obj.setStyleSheet(obj._tempo_group_base_style + f'\nQGroupBox {{ margin-top: {margin}px; }}')
        if event.type() in (QEvent.Type.Polish, QEvent.Type.Show, QEvent.Type.EnabledChange):
            if isinstance(obj, QAbstractButton):
                obj.setCursor(Qt.CursorShape.PointingHandCursor if obj.isEnabled() and not isinstance(obj,InformationButton) else Qt.CursorShape.ArrowCursor)
            if isinstance(obj, QDialog) and event.type() != QEvent.Type.EnabledChange:
                if not obj.property('tempoStyled'):
                    obj.setStyleSheet(DIALOG_STYLE + obj.styleSheet())
                    obj.setProperty('tempoStyled', True)
                if isinstance(obj, QMessageBox):
                    if obj.icon() in (QMessageBox.Icon.Information, QMessageBox.Icon.Question):
                        obj.setIcon(QMessageBox.Icon.NoIcon)
                    if not obj.windowTitle().startswith('TEMPO'):
                        obj.setWindowTitle('TEMPO · ' + obj.windowTitle())
        return False


def prepare_offscreen_fonts(app):
    """The Windows offscreen plugin needs explicit system-font registration."""
    if sys.platform != 'win32' or app.platformName() != 'offscreen':
        return {}
    if hasattr(app, '_tempo_font_evidence'):
        return app._tempo_font_evidence
    import hashlib
    from PyQt6.QtGui import QFontDatabase, QFont, QRawFont
    font_root = Path(os.environ['SystemRoot'])/'Fonts'
    files = ('arial.ttf','arialbd.ttf','ariali.ttf','arialbi.ttf',
             'segoeui.ttf','segoeuib.ttf','segoeuii.ttf','segoeuiz.ttf','seguisym.ttf')
    loaded = {}
    for filename in files:
        path = font_root/filename
        if not path.is_file() or QFontDatabase.addApplicationFont(str(path)) < 0:
            raise RuntimeError(f'Cannot register required Windows fixture font: {path}')
        loaded[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    font = QFont('Segoe UI', app.font().pointSize())
    raw = QRawFont.fromFont(font)
    if not all(raw.supportsCharacter(character) for character in 'TEMPO αµ−…'):
        raise RuntimeError('Windows fixture font cannot render required text.')
    if not QRawFont.fromFont(QFont('Segoe UI Symbol',font.pointSize())).supportsCharacter('▦'):
        raise RuntimeError('Windows fixture font cannot render the plate-layout icon.')
    app.setFont(font)
    app._tempo_font_evidence = dict(family=font.family(), files=loaded)
    return app._tempo_font_evidence


def install_ui(app=None):
    app = app or QApplication.instance()
    if app is None:
        return
    prepare_offscreen_fonts(app)
    if hasattr(app, '_tempo_interactions'):
        return
    app._tempo_style = FocusNeutralStyle(app.style().objectName() or 'Fusion')
    app.setStyle(app._tempo_style)
    app._tempo_interactions = InteractionStyle(app)
    app.installEventFilter(app._tempo_interactions)
    palette = QPalette()
    for role, color in [(QPalette.ColorRole.Window, '#19212c'),
                        (QPalette.ColorRole.WindowText, '#edf2f7'),
                        (QPalette.ColorRole.Base, '#111923'),
                        (QPalette.ColorRole.AlternateBase, '#202c3a'),
                        (QPalette.ColorRole.Text, '#edf2f7'),
                        (QPalette.ColorRole.Button, '#29384a'),
                        (QPalette.ColorRole.ButtonText, '#edf2f7'),
                        (QPalette.ColorRole.Highlight, '#315776'),
                        (QPalette.ColorRole.HighlightedText, '#ffffff')]:
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor('#99a3b0'))
    app.setPalette(palette)
    app.setStyleSheet(APP_STYLE)
    app.setProperty('tempoUiRevision', UI_REVISION)
    app.setProperty('tempoUiSource', str(Path(__file__).resolve()))


class WrappingCheckBox(QCheckBox):
    """Native checkbox with a wrapping label and one full-width hit target."""
    def __init__(self,text,parent=None):
        super().__init__('',parent)
        self.label=QLabel(text,self);self.label.setWordWrap(True)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAccessibleName(text)
        policy=QSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True);self.setSizePolicy(policy)

    def text(self):return self.label.text()

    def hitButton(self,position):return self.rect().contains(position)

    def heightForWidth(self,width):
        return max(super().sizeHint().height(),self.label.heightForWidth(max(1,width-28))+4)

    def sizeHint(self):
        from PyQt6.QtCore import QSize
        width=min(300,self.label.sizeHint().width()+28)
        return QSize(width,self.heightForWidth(width))

    def minimumSizeHint(self):
        from PyQt6.QtCore import QSize
        return QSize(80,super().sizeHint().height())

    def resizeEvent(self,event):
        self.label.setGeometry(28,0,max(1,self.width()-28),self.height())
        super().resizeEvent(event)


class FlowLayout(QLayout):
    """Place related control groups left to right, wrapping at available width."""
    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self.items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item):
        self.items.append(item)

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        from PyQt6.QtCore import QRect
        return self._arrange(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        from PyQt6.QtCore import QSize
        size = QSize()
        for item in self.items:
            if item.isEmpty():
                continue
            minimum = item.minimumSize()
            minimum.setWidth(minimum.width() + self._text_overhang(item))
            size = size.expandedTo(minimum)
        return size

    @staticmethod
    def _text_overhang(item):
        widget = item.widget()
        if not isinstance(widget, QPushButton):
            return 0
        # Qt's button hint uses advance widths. Arial's painted ink can extend
        # one pixel further, including on disabled Windows controls.
        text = widget.text().replace('&&', '\0').replace('&', '').replace('\0', '&')
        metrics = widget.fontMetrics()
        ink = metrics.boundingRect(text)
        left = max(0, -ink.left())
        right = max(0, ink.left() + ink.width() - metrics.horizontalAdvance(text))
        # Button text is centered by its advances; reserve both sides so a
        # one-sided glyph bearing cannot be clipped at the opposite edge.
        return 2 * max(left, right)

    def _arrange(self, rect, measure):
        from PyQt6.QtCore import QRect
        x, y, height = rect.x(), rect.y(), 0
        row = []
        def place_row():
            if not measure:
                for item, left, width, item_height in row:
                    item.setGeometry(QRect(left, y + (height - item_height) // 2, width, item_height))
        for item in self.items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            hint.setWidth(hint.width() + self._text_overhang(item))
            width = min(hint.width(), rect.width())
            if x > rect.x() and x + width > rect.right() + 1:
                place_row()
                row = []
                x = rect.x(); y += height + self.spacing(); height = 0
            item_height = item.heightForWidth(width) if item.hasHeightForWidth() else hint.height()
            row.append((item, x, width, item_height))
            x += width + self.spacing()
            height = max(height, item_height)
        place_row()
        return y - rect.y() + height


class FlowRow(QWidget):
    """Keep a wrapping toolbar at its actual content height after each resize."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.flow = FlowLayout(self)
        self.flow.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def minimumSizeHint(self):
        return self.flow.minimumSize()

    def sizeHint(self):
        return QSize(self.flow.minimumSize().width(), self.flow.heightForWidth(self.width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.sync_height()

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.FontChange, QEvent.Type.StyleChange):
            if hasattr(self, 'flow'):
                self.sync_height()
        return result

    def sync_height(self):
        height = self.flow.heightForWidth(self.width())
        if self.height() != height or self.minimumHeight() != height:
            self.setFixedHeight(height)


class SettingRow(QWidget):
    """Shared label column, adjacent input, and guidance beside it when space permits."""
    def __init__(self, label, control, label_width, parent=None, information=None):
        super().__init__(parent)
        from PyQt6.QtWidgets import QGridLayout
        self.label, self.control, self.label_width = label, control, label_width
        self.control_widget = control
        if information is not None:
            self.control_widget = QWidget()
            pair = QHBoxLayout(self.control_widget)
            pair.setContentsMargins(0,0,0,0); pair.setSpacing(6)
            pair.addWidget(control); pair.addWidget(information)
            self.control_widget.setSizePolicy(QSizePolicy.Policy.Maximum,QSizePolicy.Policy.Fixed)
        self.row = QGridLayout(self)
        self.row.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setHorizontalSpacing(12)
        self.row.setVerticalSpacing(4)
        self.row.addWidget(label, 0, 0)
        self.row.addWidget(self.control_widget, 0, 1)
        self.row.setColumnStretch(2, 1)
        self.notes = None
        self.arrangement = None
        self.stacked = None
        label.setWordWrap(True)
        control.setSizePolicy(QSizePolicy.Policy.Expanding if isinstance(control, (QLineEdit,FilePickerRow)) else QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def setNotes(self, notes):
        self.notes = notes
        self.arrangement = None
        self.arrange()

    def arrange(self):
        width = max(0, self.contentsRect().width())
        control_width = max(self.control_widget.minimumWidth(), self.control_widget.sizeHint().width())
        stacked = width < self.label_width + control_width + 12
        beside = not stacked and not isinstance(self.control, (QLineEdit,FilePickerRow)) and width >= self.label_width + control_width + 380
        arrangement = (stacked, beside)
        if arrangement != self.arrangement:
            self.arrangement = arrangement
            self.stacked = stacked
            self.row.removeWidget(self.control_widget)
            if self.notes is not None:
                self.row.removeWidget(self.notes)
            if stacked:
                self.row.addWidget(self.control_widget, 1, 0, 1, 3)
            elif isinstance(self.control, (QLineEdit,FilePickerRow)):
                self.row.addWidget(self.control_widget, 0, 1, 1, 2)
            else:
                self.row.addWidget(self.control_widget, 0, 1)
            if self.notes is not None:
                if beside:
                    self.row.addWidget(self.notes, 0, 2)
                else:
                    self.row.addWidget(self.notes, 2 if stacked else 1, 0, 1, 3)
        self.label.setFixedWidth(min(width, self.label_width))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.arrange()


class SelectorRow(QWidget):
    """Two related selectors share a row, or stack together when their text needs it."""
    def __init__(self, first_label, first, second_label, second, parent=None):
        super().__init__(parent)
        self.first_label, self.first = first_label, first
        self.second_label, self.second = second_label, second
        self.grid = QGridLayout(self)
        # The horizontal arrangement must not prevent a later narrow resize.
        self.grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(12); self.grid.setVerticalSpacing(6)
        self.grid.addWidget(first_label, 0, 0)
        self.stacked = None
        for label in (first_label, second_label):
            label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        for selector in (first, second):
            selector.currentTextChanged.connect(lambda _: self.reflow())
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def minimumSizeHint(self):
        first_width, second_width = self.textWidths()
        return QSize(max(self.first_label.sizeHint().width() + first_width,
                         self.second_label.sizeHint().width() + second_width) + 12,
                     max(w.sizeHint().height() for w in
                         (self.first_label, self.first, self.second_label, self.second)))

    def textWidths(self):
        first_width = max(self.first.minimumSizeHint().width(), self.first.fontMetrics().horizontalAdvance(self.first.currentText()) + 40)
        second_width = max(self.second.minimumSizeHint().width(), min(400, self.second.fontMetrics().horizontalAdvance(self.second.currentText()) + 40))
        return first_width, second_width

    def needsStack(self, width):
        first_width, second_width = self.textWidths()
        return width < self.first_label.sizeHint().width() + first_width + self.second_label.sizeHint().width() + second_width + 36

    def hasHeightForWidth(self):
        return False

    def heightForWidth(self, width):
        row_height = max(w.sizeHint().height() for w in (self.first_label,self.first,self.second_label,self.second))
        return row_height * (2 if self.needsStack(width) else 1) + (6 if self.needsStack(width) else 0)

    def reflow(self):
        stacked = self.needsStack(self.width())
        if stacked != self.stacked:
            self.stacked = stacked
            for widget in (self.first,self.second_label,self.second): self.grid.removeWidget(widget)
            self.grid.addWidget(self.first,0,1,1,3 if stacked else 1)
            self.grid.addWidget(self.second_label,1 if stacked else 0,0 if stacked else 2)
            self.grid.addWidget(self.second,1 if stacked else 0,1 if stacked else 3,1,3 if stacked else 1)
            self.grid.setColumnStretch(1,1)
            self.grid.setColumnStretch(3,0 if stacked else 1)
        first_width, second_width = self.textWidths()
        self.grid.setColumnMinimumWidth(1,0 if stacked else first_width)
        self.grid.setColumnMinimumWidth(3,0 if stacked else second_width)
        self.setFixedHeight(self.heightForWidth(self.width()))
        self.updateGeometry()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.reflow()


class CompactWorkspace(QWidget):
    """Scroll only below the content minimum; an expanding plot yields spare height."""
    def hasHeightForWidth(self):
        return self.layout() is not None and self.layout().hasHeightForWidth()

    def heightForWidth(self, width):
        layout = self.layout()
        return layout.minimumHeightForWidth(width) if layout is not None else -1

    def sync_minimum_height(self):
        layout = self.layout()
        if layout is None:
            return
        height = (layout.minimumHeightForWidth(self.width()) if layout.hasHeightForWidth()
                  else layout.minimumSize().height())
        if height >= 0 and self.minimumHeight() != height:
            self.setMinimumHeight(height)

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest:
            self.sync_minimum_height()
        return result

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.sync_minimum_height()


class ContentTabs(QTabWidget):
    """A compact panel sizes to the active page, including wrapped empty-state copy."""
    def __init__(self, parent=None):
        super().__init__(parent)
        policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.currentChanged.connect(lambda _: self.updateGeometry())

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        page = self.currentWidget()
        body = 0
        if page is not None and page.layout() is not None:
            layout = page.layout()
            body = layout.totalHeightForWidth(max(0, width - 4)) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
        return max(0, body) + self.tabBar().sizeHint().height() + 4

    def sizeHint(self):
        return QSize(300, self.heightForWidth(max(300, self.width())))

    def minimumSizeHint(self):
        return QSize(0, self.heightForWidth(max(0, self.width())))


def statistic_description(text):
    """Definitions shared by the mean and dispersion label tooltips."""
    definitions = {
        'Amplitude (a.u.):': 'a.u. means arbitrary units. Amplitude measures the peak change in image intensity above baseline; it is not a force or distance measurement.',
        'Time-to-Peak (ms):': 'Time from contraction onset to the peak signal.',
        'Relaxation (ms):': 'Time from peak contraction to the end of relaxation.',
        'Beat interval (ms):': 'Time between consecutive detected contraction peaks.',
        'CT50 (ms):': 'Contraction time to reach 50% of peak amplitude.',
        'CT90 (ms):': 'Contraction time to reach 90% of peak amplitude.',
        'RT50 (ms):': 'Relaxation time from the peak to a 50% decrease in amplitude.',
        'RT90 (ms):': 'Relaxation time from the peak to a 90% decrease in amplitude.',
        'Contraction (a.u./ms):': 'Maximum rate of intensity increase during contraction. a.u./ms means arbitrary intensity units per millisecond; this is a signal-change rate, not physical movement speed.',
        'Relaxation (a.u./ms):': 'Maximum rate of intensity decrease during relaxation. a.u./ms means arbitrary intensity units per millisecond; this is a signal-change rate, not physical movement speed.',
        'Fractional Anisotropy (α):': 'Directional preference of motion: zero is isotropic; larger values indicate stronger alignment.',
        'Contraction Angle (θ):': 'Dominant motion orientation, measured axially from 0 to 180 degrees.',
        'Vector Coherence:': 'Agreement among local motion directions; larger values indicate more consistent motion.',
    }
    return definitions.get(text, '')


class StatisticsTabs(QTabWidget):
    """Keep metric groups at their natural height; spare height stays below them."""
    def set_forms(self, forms):
        self.forms = forms

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.sync_columns()

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest:
            self.sync_columns()
        return result

    def sync_columns(self):
        for forms in getattr(self, 'forms', ()):
            for form in forms:
                for row in range(form.rowCount()):
                    form.setRowMinimumHeight(row, 0)
            labels = [form.itemAt(row, QFormLayout.ItemRole.LabelRole).widget()
                      for form in forms for row in range(form.rowCount())]
            desired = max(label.fontMetrics().horizontalAdvance(label.text()) for label in labels) + 4
            values = [form.itemAt(row, QFormLayout.ItemRole.FieldRole).widget()
                      for form in forms for row in range(form.rowCount())]
            value_width = max(value.fontMetrics().horizontalAdvance(value.text()) for value in values) + 2
            for form in forms:
                available = form.parentWidget().width() - value_width - 28
                form.setColumnMinimumWidth(0, max(120, min(desired, available)))
                form.setColumnMinimumWidth(1, value_width)


class MetricLabel(QLabel):
    """Reserve the wrapped text height at the actual allocated column width."""
    def resizeEvent(self, event):
        super().resizeEvent(event)
        geometry = (self.width(), self.font().key())
        if geometry != getattr(self, '_wrapped_geometry', None):
            self._wrapped_geometry = geometry
            # QLabel includes its current minimum in heightForWidth. Clear the
            # previous narrow-column height before measuring a wider column.
            self.setMinimumHeight(0)
            self.setMinimumHeight(max(0, self.heightForWidth(self.width())))


class MetricForm(QGridLayout):
    """Two real grid columns: names cannot collapse underneath their values."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnStretch(2, 1)
        self.setHorizontalSpacing(10)

    def addRow(self, text, value):
        row = self.rowCount() if self.count() else 0
        label = MetricLabel(text)
        label.setWordWrap(True)
        self.addWidget(label, row, 0)
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.addWidget(value, row, 1)

    def itemAt(self, index, role=None):
        if role is None:
            return super().itemAt(index)
        return self.itemAtPosition(index, 0 if role == QFormLayout.ItemRole.LabelRole else 1)


class BusySpinner(QWidget):
    """A quiet activity indicator that reserves the same space when idle."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._angle = 0
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.advance)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleName('Status refresh')
        self.setAccessibleDescription('Idle')
        self.resize_for_font()

    def resize_for_font(self):
        side = max(14, min(22, round(self.fontMetrics().height() * .7)))
        self.setFixedSize(side, side)

    def set_busy(self, busy):
        self._busy = bool(busy)
        self.setAccessibleDescription('Refreshing' if self._busy else 'Idle')
        if self._busy and self.isVisible():
            self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def advance(self):
        self._angle = (self._angle + 12) % 360
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        if self._busy:
            self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange):
            self.resize_for_font()

    def paintEvent(self, event):
        if not self._busy:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        circle = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        track = self.palette().color(QPalette.ColorRole.Text); track.setAlpha(35)
        arc = self.palette().color(QPalette.ColorRole.Text); arc.setAlpha(145)
        painter.setPen(QPen(track, 1.5)); painter.drawEllipse(circle)
        painter.setPen(QPen(arc, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(circle, -self._angle * 16, 100 * 16)


class InlineNotice(QLabel):
    """Empty feedback occupies no row; useful messages remain persistent."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.hide()

    def setText(self, text):
        super().setText(text)
        self.setVisible(bool(text))

    def clear(self):
        super().clear()
        self.hide()


class PreviewStatusLabel(InlineNotice):
    """Use a single status line whenever the preview header has enough room."""
    def sizeHint(self):
        hint = super().sizeHint()
        lines = self.text().splitlines()
        hint.setWidth(max((self.fontMetrics().horizontalAdvance(line) for line in lines), default=0))
        return hint
