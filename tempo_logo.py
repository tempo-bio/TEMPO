"""Native, clock-driven drawing of the approved TEMPO QRS animation."""
from functools import lru_cache
import math
from pathlib import Path
import sys

from PyQt6.QtCore import QEvent, QEasingCurve, QElapsedTimer, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QFontMetrics, QImage, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QShortcut, QPalette, QTextOption
from PyQt6.QtWidgets import QApplication, QDialog, QFrame, QLabel, QPlainTextEdit, QSizePolicy, QVBoxLayout, QWidget


_ASSETS = Path(__file__).resolve().parent / 'assets'
_VIEW = QRect(160, 425, 940, 410)
_PATCH = QRect(380, 470, 450, 330)
_EASE = QEasingCurve(QEasingCurve.Type.BezierSpline)
_EASE.addCubicBezierSegment(QPointF(.42, 0), QPointF(.58, 1), QPointF(1, 1))


def trace_interval(seconds):
    """Visible interval along the stroke: draw, hold, forward erase, empty."""
    phase = seconds % 3.0
    if phase < 1.35:
        return 0.0, _EASE.valueForProgress(phase / 1.35)
    if phase < 1.65:
        return 0.0, 1.0
    if phase < 2.8:
        return _EASE.valueForProgress((phase - 1.65) / 1.15), 1.0
    return 0.0, 0.0


@lru_cache(maxsize=1)
def _artwork():
    source = QImage(str(_ASSETS / 'tempo_icon.png'))
    base = QImage(str(_ASSETS / 'tempo_logo_base.png'))
    ready = not source.isNull() and not base.isNull()
    if base.isNull() and not source.isNull():
        base = source.copy(_VIEW)
    path = QPainterPath(QPointF(400, 695.5))
    path.lineTo(496, 695.5)
    path.cubicTo(520, 695.5, 527, 678, 537, 642)
    for x, y in ((558, 566), (612, 763), (658, 498), (685, 605)):
        path.lineTo(x, y)
    path.cubicTo(692, 631, 698, 639.5, 719, 639.5)
    path.lineTo(804, 639.5)
    return base, source.copy(_PATCH), path, ready


@lru_cache(maxsize=1)
def _sampled_path():
    # Qt before 6.10 lacks trimmed(). Equal arc-length samples retain smooth
    # pen motion on those installations without increasing the Qt requirement.
    path = _artwork()[2]
    count = math.ceil(path.length())
    return tuple(path.pointAtPercent(path.percentAtLength(path.length() * i / count))
                 for i in range(count + 1))


def _trimmed_path(path, start, end):
    if hasattr(path, 'trimmed'):
        return path.trimmed(start, end)
    points = _sampled_path()
    last = len(points) - 1
    first_distance, last_distance = start * last, end * last

    def point_at(distance):
        index = min(int(distance), last - 1)
        fraction = distance - index
        return points[index] * (1 - fraction) + points[index + 1] * fraction

    result = QPainterPath(point_at(first_distance))
    for index in range(math.floor(first_distance) + 1, math.ceil(last_distance)):
        result.lineTo(points[index])
    result.lineTo(point_at(last_distance))
    return result


class AnimatedTempoLogo(QWidget):
    """Static letters plus one moving reveal mask; no movie or frame sequence.

    The reviewed underlay restores the complete white E/P and removes the
    PNG's baked-in pulse shadow. The colored stroke still uses source pixels.
    Only the small stroke patch is composited each frame, at display resolution.
    """

    def __init__(self, parent=None, *, width=320, version_text=None):
        super().__init__(parent)
        self._preferred_width = width
        self.setAccessibleName('TEMPO')
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setMaximumSize(width, math.ceil(width * _VIEW.height() / _VIEW.width()))
        self._base, self._source, self._path, self._ready = _artwork()
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self.update)
        self._buffer_key = None
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._timer.stop)
        self.version_label = None
        if version_text is not None:
            self.version_label = QLabel(version_text, self)
            self.version_label.setTextFormat(Qt.TextFormat.PlainText)
            self.version_label.setAccessibleName('TEMPO version ' + version_text)
            self._position_version()

    def sizeHint(self):
        return QSize(self._preferred_width, math.ceil(self._preferred_width * 410 / 940))

    def minimumSizeHint(self):
        return QSize(120, 53)

    def showEvent(self, event):
        super().showEvent(event)
        self._clock.start()
        if self._ready:
            self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _target_rect(self):
        scale = min(self.width() / _VIEW.width(), self.height() / _VIEW.height())
        width, height = _VIEW.width() * scale, _VIEW.height() * scale
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def _position_version(self):
        label = getattr(self, 'version_label', None)
        target = self._target_rect()
        if label is None or target.isEmpty():
            return
        palette = label.palette()
        palette.setColor(QPalette.ColorRole.WindowText, QApplication.palette().color(QPalette.ColorRole.Window))
        label.setPalette(palette)
        # Fit the quiet lower-right margin below the O, clear of the QRS.
        font = self.font()
        metrics = QFontMetrics(font)
        scale = min(1.0, max(1, target.height() * .30 - 10) / metrics.height(),
                    max(1, target.width() * .40 - 8) / max(1, metrics.horizontalAdvance(label.text())))
        if font.pixelSize() > 0:
            font.setPixelSize(max(1, math.floor(font.pixelSize() * scale)))
        else:
            font.setPointSizeF(max(1, font.pointSizeF() * scale))
        label.setFont(font)
        label.adjustSize()
        label.move(round(target.right()) - 8 - label.width(), round(target.bottom()) - 6 - label.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_version()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.PaletteChange):
            self._position_version()

    def _trace_frame(self, scale, ratio, start, end):
        key = (scale, ratio)
        if self._buffer_key != key:
            width = max(1, math.ceil(_PATCH.width() * scale * ratio))
            height = max(1, math.ceil(_PATCH.height() * scale * ratio))
            self._frame = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
            self._mask = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
            self._scaled_source = self._source.scaled(width, height,
                Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self._buffer_key = key
        self._mask.fill(Qt.GlobalColor.transparent)
        pen = QPainter(self._mask)
        pen.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen.scale(self._mask.width() / _PATCH.width(), self._mask.height() / _PATCH.height())
        pen.translate(-_PATCH.x(), -_PATCH.y())
        pen.setPen(QPen(Qt.GlobalColor.white, 34, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        pen.drawPath(_trimmed_path(self._path, start, end))
        pen.end()
        self._frame.fill(Qt.GlobalColor.transparent)
        painter = QPainter(self._frame)
        painter.drawImage(0, 0, self._scaled_source)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, self._mask)
        painter.end()
        return self._frame

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        target = self._target_rect()
        painter.drawImage(target, self._base)
        seconds = self._clock.nsecsElapsed() / 1_000_000_000 if self._clock.isValid() else 0.0
        start, end = trace_interval(seconds)
        if not self._ready or end - start <= 1e-6 or target.isEmpty():
            return
        scale = target.width() / _VIEW.width()
        frame = self._trace_frame(scale, self.devicePixelRatioF(), start, end)
        patch_target = QRectF(target.x() + (_PATCH.x() - _VIEW.x()) * scale,
                              target.y() + (_PATCH.y() - _VIEW.y()) * scale,
                              _PATCH.width() * scale, _PATCH.height() * scale)
        painter.drawImage(patch_target, frame)


class AnimatedTempoWordmark(AnimatedTempoLogo):
    """Transparent vector lettering for onboarding, using the same QRS timing."""

    def __init__(self, parent=None, *, width=148):
        super().__init__(parent, width=width)
        self.setMaximumSize(width, math.ceil(width * 330 / 860))
        self._ready = True
        self._letters = QPainterPath()
        # Complete white T/E/P remain under the animated trace.
        for x, y, w, h in ((219.5, 551.5, 131, 23.5), (271, 575, 27.5, 132.5),
                           (373, 551.5, 27, 156), (400, 551.5, 87, 23.5),
                           (400, 616, 74, 23), (400, 684, 74, 23.5)):
            self._letters.addRect(x, y, w, h)
        p = QPainterPath(QPointF(737, 707.5))
        p.lineTo(737, 551.5); p.lineTo(804, 551.5)
        p.cubicTo(835, 551.5, 859, 573, 859, 601)
        p.cubicTo(859, 630, 836, 651.5, 804, 651.5)
        p.lineTo(765, 651.5); p.lineTo(765, 707.5); p.closeSubpath()
        hole = QPainterPath(QPointF(765, 575))
        hole.lineTo(765, 628.5); hole.lineTo(804, 628.5)
        hole.cubicTo(821, 628.5, 832, 618, 832, 602)
        hole.cubicTo(832, 586, 821, 575, 804, 575); hole.closeSubpath()
        self._letters = self._letters.united(p.subtracted(hole))
        outer, inner = QPainterPath(), QPainterPath()
        outer.addEllipse(QPointF(953.5, 629.5), 81.5, 80.5)
        inner.addEllipse(QPointF(953.5, 629.5), 53.5, 56.5)
        self._letters = self._letters.united(outer.subtracted(inner))

    def sizeHint(self):
        return QSize(self._preferred_width, math.ceil(self._preferred_width * 330 / 860))

    def minimumSizeHint(self):
        return QSize(100, 39)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scale = min(self.width() / 860, self.height() / 330)
        painter.translate((self.width() - 860 * scale) / 2, (self.height() - 330 * scale) / 2)
        painter.scale(scale, scale)
        painter.translate(-200, -470)
        painter.fillPath(self._letters, QColor('#f9fbfd'))
        seconds = self._clock.nsecsElapsed() / 1e9 if self._clock.isValid() else 0
        start, end = trace_interval(seconds)
        if end - start > 1e-6:
            gradient = QLinearGradient(400, 0, 804, 0)
            for position, color in ((0, '#f9fcfd'), (.22, '#05f3ff'), (.39, '#09f7fb'),
                                    (.53, '#1ef7d3'), (.64, '#20f7b3'), (.75, '#27f7bc'), (1, '#f9fcfd')):
                gradient.setColorAt(position, QColor(color))
            painter.setPen(QPen(gradient, 23.5, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(_trimmed_path(self._path, start, end))


class AboutTempoDialog(QDialog):
    """Application identity with a centered logo and selectable runtime details."""

    def __init__(self, parent=None, *, application_path):
        super().__init__(parent)
        from PyQt6.QtCore import QT_VERSION_STR
        from tempo_release import VERSION
        from tempo_ui import UI_REVISION
        self.setWindowTitle('About TEMPO')
        self.resize(620, 360)
        self.setMinimumSize(360, 260)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        version = VERSION.rsplit('.', 1)[0] if len(VERSION.split('.')) == 3 and VERSION.endswith('.0') else VERSION
        self.logo = AnimatedTempoLogo(self, version_text='v' + version)
        layout.addWidget(self.logo, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.details = QPlainTextEdit(self)
        self.details.setReadOnly(True)
        self.details.setFrameShape(QFrame.Shape.NoFrame)
        self.details.setTabChangesFocus(True)
        self.details.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.details.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        palette = self.details.palette()
        palette.setColor(QPalette.ColorRole.Base, self.palette().color(QPalette.ColorRole.Window))
        self.details.setPalette(palette)
        self.details.setAccessibleName('Application details')
        self.details.setPlainText(
            f'Interface: {UI_REVISION}\nRunning from: {Path(application_path).resolve()}\n'
            f'Python: {sys.executable}\nQt: {QT_VERSION_STR}\nGraphics: {QApplication.platformName()}')
        layout.addWidget(self.details, 1)
        self._close_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Close), self)
        self._close_shortcut.activated.connect(self.reject)
