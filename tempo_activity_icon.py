"""Opt-in OS icon activity; native Qt icon APIs, no video or hardware polling."""
import math
import sys
import time

from PyQt6 import sip
from PyQt6.QtCore import QObject, QElapsedTimer, QProcess, QThread, QTimer, Qt
from PyQt6.QtGui import QIcon, QImage, QPainter, QPen, QPixmap

from tempo_logo import _ASSETS, _VIEW, _artwork, _trimmed_path, trace_interval


_CLOUD_PHASES = frozenset(('preparing', 'running', 'checking_runtime', 'preparing_dataset',
    'preparing_baselines', 'checking_training_steps', 'training', 'validating', 'publishing'))


def cloud_work_active(status, *, now=None):
    """A stale/missing heartbeat is not evidence that a remote job is running."""
    if not isinstance(status, dict) or status.get('phase') not in _CLOUD_PHASES:
        return False
    stamp = status.get('updated_at')
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
        return False
    age = (time.time() if now is None else now) - stamp
    return -5 <= age <= 120


def desktop_work_active(window):
    """Inspect existing acknowledgements and workers, including minimized work.

    No camera/serial calls, network requests, or thread waits occur here. A
    connected stimulator's heartbeat alone does not count as active output.
    """
    from tempo_tasks import has_active_tasks
    from PyQt6.QtWidgets import QApplication
    if has_active_tasks():
        return True
    capture = getattr(window, 'tab_capture', None)
    annotator = getattr(window, 'tab_annotator', None)
    workers = [getattr(window, 'worker', None), getattr(annotator, 'auto_worker', None)]
    if capture is not None:
        workers.extend(getattr(capture, key, None) for key in ('acq_worker', 'scout_worker', '_cleanup_worker'))
        if getattr(capture, 'is_recording', False):
            return True
        preview = getattr(capture, 'timer', None)
        if preview is not None and preview.isActive():
            return True
        tools = getattr(capture, 'capture_controls', None)
        if getattr(getattr(tools, 'session', None), 'active', False):
            return True
        if any(not future.done() for future, _ in getattr(tools, '_pending', ())):
            return True
        service = getattr(tools, 'service', None)
        status = getattr(service, 'status', None)
        if (getattr(service, 'connected', False) and status is not None
                and status.mode in ('PACING', 'CONSTANT_STIM', 'SQUARE_10HZ')):
            return True
    for top in QApplication.topLevelWidgets():
        workers.extend(top.findChildren(QThread))
        if any(process.state() != QProcess.ProcessState.NotRunning for process in top.findChildren(QProcess)):
            return True
    if any(isinstance(worker, QThread) and not sip.isdeleted(worker) and worker.isRunning()
           for worker in workers):
        return True
    maintainer = getattr(window, 'tab_maintainer', None)
    if maintainer is not None and getattr(maintainer, '_allowed', False):
        if getattr(maintainer, 'request', None) and cloud_work_active(getattr(maintainer, '_activity_training_status', {})):
            return True
        evaluation = getattr(maintainer, 'model_evaluation', None)
        payload = getattr(evaluation, 'payload', None)
        if payload and cloud_work_active(payload.get('state', {})):
            return True
    return False


class IconFrames:
    """Lazily render at most 36 small native icons for a three-second cycle."""
    FPS = 12
    COUNT = 3 * FPS

    @classmethod
    def index_at(cls, seconds):
        index = int(seconds * cls.FPS) % cls.COUNT
        start, end = trace_interval(index / cls.FPS)
        if end - start <= 1e-6:
            return 0
        if start == 0 and end == 1:
            return 17  # Reuse the full stroke during the hold; no OS redraws.
        return index

    def __init__(self, size):
        base, _, self.path, ready = _artwork()
        source = QImage(str(_ASSETS / 'tempo_icon.png'))
        if not ready or source.isNull():
            raise RuntimeError('The TEMPO logo assets are missing.')
        empty = source.copy()
        painter = QPainter(empty)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.drawImage(_VIEW.topLeft(), base)
        painter.end()
        self.size = size
        self.scale = size / source.width()
        self.base = empty.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        self.source = source.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
        self.frames = {}

    def at(self, index):
        index %= self.COUNT
        if index not in self.frames:
            image = self.base.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            start, end = trace_interval(index / self.FPS)
            if end - start > 1e-6:
                mask = QImage(self.size, self.size, QImage.Format.Format_ARGB32_Premultiplied)
                mask.fill(Qt.GlobalColor.transparent)
                painter = QPainter(mask)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.scale(self.scale, self.scale)
                painter.setPen(QPen(Qt.GlobalColor.white, 34, Qt.PenStyle.SolidLine,
                                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                painter.drawPath(_trimmed_path(self.path, start, end))
                painter.end()
                trace = self.source.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                painter = QPainter(trace)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
                painter.drawImage(0, 0, mask)
                painter.end()
                painter = QPainter(image)
                painter.drawImage(0, 0, trace)
                painter.end()
            self.frames[index] = QIcon(QPixmap.fromImage(image))
        return self.frames[index]


class NativeIconTarget:
    """Qt's Cocoa adapter updates NSApp; its Windows adapter sends WM_SETICON.

    Update the explicit dashboard icon on Windows, where an application default
    does not override the icon already assigned to the window. Qt owns and frees
    the native icon handles. Executable/installer shortcut assets stay static.
    """
    def __init__(self, app, window, platform=None):
        self.app, self.window = app, window
        self.platform = sys.platform if platform is None else platform
        self.original_app = QIcon(app.windowIcon())
        self.original_window = QIcon(window.windowIcon())
        self.applied = False

    @property
    def supported(self):
        return self.platform in ('darwin', 'win32')

    def apply(self, icon):
        if not self.supported:
            return
        # Also update inherited dialog icons so a busy modal window does not
        # supply the old icon to a Windows taskbar group.
        self.app.setWindowIcon(icon)
        if self.platform == 'win32' and not sip.isdeleted(self.window):
            self.window.setWindowIcon(icon)
        self.applied = True

    def restore(self):
        if not self.applied:
            return
        self.app.setWindowIcon(self.original_app)
        if self.platform == 'win32' and not sip.isdeleted(self.window):
            self.window.setWindowIcon(self.original_window)
        self.applied = False


class ActivityIconController(QObject):
    """No timers or frame allocation until the saved opt-in is enabled."""
    def __init__(self, app, window, *, target=None, probe=None):
        super().__init__(window)
        self.target = target if target is not None else NativeIconTarget(app, window)
        self.probe = probe if probe is not None else lambda: desktop_work_active(window)
        self.enabled = False
        self.frames = None
        self.clock = QElapsedTimer()
        self.last_frame = None
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(250)
        self.poll_timer.timeout.connect(self.poll)
        self.animation_timer = QTimer(self)
        self.animation_timer.setInterval(84)
        self.animation_timer.timeout.connect(self.tick)
        app.aboutToQuit.connect(self.shutdown)
        window.destroyed.connect(self.shutdown)

    def apply_preferences(self):
        from tempo_config import tempo_preferences
        self.set_enabled(tempo_preferences.get('appearance', 'animate_activity_icon', False) is True)

    def set_enabled(self, enabled):
        self.enabled = bool(enabled) and self.target.supported
        if self.enabled:
            self.poll_timer.start()
            self.poll()
        else:
            self.poll_timer.stop()
            self.stop_animation()
            self.frames = None

    def poll(self):
        if not self.enabled:
            return
        try:
            busy = self.probe()
        except Exception:
            self._disable_after_error()
            return
        if busy:
            if not self.animation_timer.isActive():
                self.clock.start()
                self.animation_timer.start()
                self.tick()
        else:
            self.stop_animation()

    def tick(self):
        if not self.enabled:
            return
        try:
            if self.frames is None:
                self.frames = IconFrames(256 if self.target.platform == 'darwin' else 128)
            index = IconFrames.index_at(self.clock.nsecsElapsed() / 1_000_000_000)
            if index != self.last_frame:
                self.target.apply(self.frames.at(index))
                self.last_frame = index
        except Exception:
            self._disable_after_error()

    def _disable_after_error(self):
        # This optional decoration must never interrupt pacing or training.
        import logging
        logging.getLogger(__name__).exception('App icon animation stopped')
        self.set_enabled(False)

    def stop_animation(self):
        self.animation_timer.stop()
        self.last_frame = None
        self.target.restore()

    def shutdown(self):
        self.set_enabled(False)
