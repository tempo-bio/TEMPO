"""Run blocking desktop operations outside the Qt GUI thread."""
import threading
import queue
from PyQt6.QtCore import QObject, QThread, pyqtSignal, Qt, QTimer, QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication, QDialog, QVBoxLayout, QLabel, QProgressBar

_background_tasks = set()
_background_application = None
_background_timer = None
_modal_tasks = set()


def has_active_tasks():
    """Read GUI-owned task state without touching hardware or waiting on work."""
    return bool(_modal_tasks) or any(not result.done_event.is_set()
                                     for result in tuple(_background_tasks))


def pending_task_titles():
    """Protected modal work remains registered until its result is applied."""
    return [dialog.windowTitle() for dialog in tuple(_modal_tasks)]


class QuitGuard(QObject):
    def eventFilter(self, target, event):
        if event.type() == QEvent.Type.Quit and _modal_tasks:
            event.ignore()
            for dialog in tuple(_modal_tasks):
                dialog.wait_status.setText('Waiting for this operation to finish before quitting. Its outcome has not been confirmed.')
            return True
        if event.type() == QEvent.Type.Quit and target is QApplication.instance():
            target._tempo_quit_requested = True
        return False


def install_quit_guard(application):
    if not hasattr(application, '_tempo_quit_guard'):
        application._tempo_quit_guard = QuitGuard(application)
        application.installEventFilter(application._tempo_quit_guard)


def release_background_signals():
    """Release GUI emitters before Qt/Python teardown; reads may still wait.

    Dashboard and write dialogs must settle protected writes before allowing
    Quit. Daemon reads only write to Python mailboxes, never to Qt objects.
    """
    from PyQt6 import sip
    global _background_timer
    if _background_timer is not None and not sip.isdeleted(_background_timer):
        _background_timer.stop()
        _background_timer.deleteLater()
    _background_timer = None
    for result in tuple(_background_tasks):
        if not sip.isdeleted(result):
            sip.delete(result)
    _background_tasks.clear()


def dispose_closed_views(*views):
    """Dispose already-closed widgets while Qt is still alive.

    Explicit disposal avoids Rosetta/SIP interpreter-exit cleanup cycles.
    This is called only after the close guards have settled protected work.
    """
    if any(view.isVisible() for view in views):
        raise RuntimeError('Cannot dispose a view before its close has completed.')
    if _modal_tasks:
        raise RuntimeError('Cannot dispose views while a protected operation is unsettled.')
    for view in views:
        workers = view.findChildren(QThread) + [getattr(view, 'worker', None)]
        capture = getattr(view, 'tab_capture', None)
        if capture is not None:
            workers += [getattr(capture, name, None) for name in ('acq_worker', 'scout_worker')]
        if any(worker is not None and not worker.wait(0) for worker in workers):
            raise RuntimeError('Cannot dispose a view while a worker thread is unsettled.')
    resources = []
    for view in views:
        # Graphics scenes do not own pyqtgraph's detached control menus.
        # Settle their ownership while every scene item is still alive.
        import sys
        if 'pyqtgraph' in sys.modules:
            from tempo_plot import own_plot_resources
            resources.extend(own_plot_resources(view))
        view.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    from PyQt6 import sip
    if any(not sip.isdeleted(resource) for resource in resources):
        raise RuntimeError('Closed plots still own live Qt control resources.')
    return dict(plot_resources_disposed=len(resources))


def dispose_application(application):
    """Destroy Qt after protected work settles, before interpreter cleanup."""
    from PyQt6 import sip
    global _background_application
    if _modal_tasks or any(widget.isVisible() for widget in application.topLevelWidgets()):
        raise RuntimeError('Cannot dispose the application before its windows and protected work close.')
    if any(not worker.wait(0) for worker in application.findChildren(QThread)):
        raise RuntimeError('Cannot dispose the application while a worker is unsettled.')
    release_background_signals()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    sip.delete(application)
    _background_application = None
    if QApplication.instance() is not None:
        raise RuntimeError('Qt application disposal did not complete.')


class BackgroundResult(QObject):
    completed = pyqtSignal(object, object)


def _background_work(action, mailbox, done):
    try:
        try:
            value, error = action(), None
        except Exception as exc:
            value, error = None, exc
        mailbox.put((value, error))
    finally:
        done.set()


def _deliver_background_results():
    from PyQt6 import sip
    for result in tuple(_background_tasks):
        if sip.isdeleted(result):
            _background_tasks.discard(result)
            continue
        try:
            value, error = result.mailbox.get_nowait()
        except queue.Empty:
            continue
        try:
            result.completed.emit(value, error)
        finally:
            _background_tasks.discard(result)
            result.deleteLater()


def run_background(action, completed):
    """Keep workers alive independently of the view, without a modal dialog."""
    global _background_application, _background_timer
    application = QApplication.instance()
    if application is None or QThread.currentThread() != application.thread():
        raise RuntimeError('Background desktop tasks must start on the GUI thread.')
    if application is not None and application is not _background_application:
        application.aboutToQuit.connect(release_background_signals)
        _background_application = application
    install_quit_guard(application)
    if _background_timer is None:
        _background_timer = QTimer(application)
        _background_timer.setInterval(20)
        _background_timer.timeout.connect(_deliver_background_results)
        _background_timer.start()
    result = BackgroundResult()
    result.mailbox = queue.SimpleQueue()
    result.done_event = threading.Event()
    _background_tasks.add(result)
    result.completed.connect(completed)
    # The worker receives no QObject or GUI callback. Late results cannot enter SIP.
    threading.Thread(target=_background_work, args=(action, result.mailbox, result.done_event), daemon=True).start()
    return result


class TaskThread(QThread):
    completed = pyqtSignal(object, object)

    def __init__(self, action, parent=None):
        super().__init__(parent)
        self.action = action

    def run(self):
        try:
            self.completed.emit(self.action(), None)
        except Exception as exc:
            self.completed.emit(None, exc)


class TaskDialog(QDialog):
    def __init__(self, parent, title, action):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        layout = QVBoxLayout(self)
        self.wait_status = QLabel(title)
        self.wait_status.setWordWrap(True)
        layout.addWidget(self.wait_status)
        self.wait_notice = QTimer(self)
        self.wait_notice.setSingleShot(True)
        self.wait_notice.timeout.connect(lambda: self.wait_status.setText(title + '\nStill waiting for the operation. Closing is unavailable until its outcome is known.'))
        self.wait_notice.start(10000)
        progress = QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(progress)
        self.result_value = self.error = None
        self.worker = TaskThread(action, self)
        self.worker.completed.connect(self.done_work)
        self.worker.finished.connect(self.finish_when_joined)
        self.join_poll = QTimer(self)
        self.join_poll.setInterval(20)
        self.join_poll.timeout.connect(self.finish_when_joined)

    def finish_when_joined(self):
        if not self.worker.wait(0):
            self.join_poll.start()
            return
        self.join_poll.stop()
        self.wait_notice.stop()
        _modal_tasks.discard(self)
        self.accept()

    def done(self, result):
        if self in _modal_tasks:
            return
        super().done(result)

    def reject(self):
        if self not in _modal_tasks and self.worker.wait(0):
            super().reject()

    def closeEvent(self, event):
        if self in _modal_tasks or not self.worker.wait(0):
            event.ignore()
        else:
            super().closeEvent(event)

    def done_work(self, value, error):
        self.result_value, self.error = value, error


def run_task(parent, title, action):
    dialog = TaskDialog(parent, title, action)
    install_quit_guard(QApplication.instance())
    _modal_tasks.add(dialog)
    dialog.worker.start()
    dialog.exec()
    if dialog in _modal_tasks or not dialog.worker.wait(0):
        raise RuntimeError('The operation remains active; its outcome is not confirmed.')
    try:
        if dialog.error:
            raise dialog.error
        return dialog.result_value
    finally:
        dialog.deleteLater()


def run_inline_task(parent, title, action, completed, *, wait_status):
    """Protect an embedded operation while leaving its parent's help usable.

    Reuse the modal task's quit guard and joined-thread lifecycle without
    opening a second window. Apply the outcome only after the worker exits.
    """
    dialog = TaskDialog(parent, title, action)
    dialog.wait_status = wait_status
    def deliver(_):
        try:
            completed(dialog.result_value, dialog.error)
        finally:
            dialog.deleteLater()
    dialog.finished.connect(deliver)
    install_quit_guard(QApplication.instance())
    _modal_tasks.add(dialog)
    dialog.worker.start()
    return dialog


class ReadTaskDialog(QDialog):
    """A read can stop waiting without claiming the underlying request stopped."""
    def __init__(self, parent, title, action, timeout_ms=15000):
        super().__init__(parent)
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QPushButton
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(title))
        stop = QPushButton('Stop Waiting')
        layout.addWidget(stop)
        stop.clicked.connect(self.reject)
        self.result_value = None
        self.error = None
        self.settled = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.timed_out)
        self.timer.start(timeout_ms)
        run_background(action, self.received)

    def received(self, value, error):
        if self.settled:
            return
        self.settled = True
        self.timer.stop()
        self.result_value, self.error = value, error
        self.accept()

    def timed_out(self):
        self.error = TimeoutError('The request did not finish in time. Try again.')
        self.reject()

    def reject(self):
        if self.settled:
            return
        self.settled = True
        self.timer.stop()
        self.error = self.error or RuntimeError('Stopped waiting for the read. No late result will be applied.')
        super().reject()


def run_read_task(parent, title, action, timeout_ms=15000):
    dialog = ReadTaskDialog(parent, title, action, timeout_ms)
    dialog.exec()
    try:
        if dialog.error:
            raise dialog.error
        return dialog.result_value
    finally:
        dialog.deleteLater()
