"""Offscreen update dialog behavior with simulated network and installation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMainWindow

from tempo_tasks import dispose_closed_views, has_active_tasks
from tempo_ui import install_ui
from tempo_update_service import FullDownloadRequired, UpdateCancelled
from tempo_update_ui import PackagedSoftwareUpdateDialog

APP = QApplication.instance() or QApplication([])
install_ui(APP, theme_name='dark')


def until(predicate, timeout=3000):
    end = time.monotonic() + timeout / 1000
    while not predicate() and time.monotonic() < end:
        QTest.qWait(10)
    if not predicate():
        raise AssertionError('Dialog did not reach the expected state')


def coordinator():
    target = SimpleNamespace(version='1.0.2', release_notes='## What’s new\n\n- Sample release improvements.',
                             full=SimpleNamespace(size=250 * 1024 * 1024))
    plan = SimpleNamespace(target=target, assets=(SimpleNamespace(size=4508877),))
    obj = Mock()
    obj.plan = plan
    obj.version = '1.0.1'
    obj.component_required = False
    obj.component_download_size = 0
    obj.check.return_value = plan
    obj.download.return_value = {}
    obj.prepare_components.return_value = None
    obj.installation_consequences.return_value = None
    obj.request_install.return_value = False
    return obj


class UpdateDialogTests(unittest.TestCase):
    def setUp(self):
        self.old_font = APP.font()
        self.views = []

    def tearDown(self):
        for view in reversed(self.views):
            if view.isVisible():
                view.close()
            until(lambda: getattr(view, '_task', None) is None)
            dispose_closed_views(view)
        APP.setFont(self.old_font)

    def make(self, service, parent=None):
        dialog = PackagedSoftwareUpdateDialog(parent, coordinator=service)
        self.views.append(dialog)
        dialog.show()
        until(lambda: dialog._state != 'checking')
        return dialog

    def test_check_download_and_install_use_coordinator(self):
        service = coordinator()
        dialog = self.make(service)
        self.assertIn('TEMPO 1.0.2', dialog.log_view.toPlainText())
        self.assertIn('4.3 MB', dialog.log_view.toPlainText())
        QTest.mouseClick(dialog.btn_update, Qt.MouseButton.LeftButton)
        until(lambda: dialog._state == 'ready')
        service.download.assert_called_once()
        self.assertIn('Installed: 1.0.1', dialog.log_view.toPlainText())
        service.prepare_components.assert_not_called()
        QTest.mouseClick(dialog.btn_update, Qt.MouseButton.LeftButton)
        until(lambda: service.request_install.called)
        self.assertEqual(dialog._state, 'ready')
        service.installation_consequences.assert_called_once()

    def test_component_check_blocker_does_not_misdiagnose_a_connection_failure(self):
        service = coordinator()
        service.check.side_effect = ValueError('Invalid NVIDIA selection. Repair NVIDIA support or defer the update.')
        dialog = self.make(service)
        self.assertEqual(dialog._state, 'error')
        self.assertIn('Repair NVIDIA support or defer the update.', dialog.error_details.summary.text())
        self.assertNotIn('Check your connection', dialog.error_details.summary.text())

    def test_cancel_waits_for_worker_and_does_not_prepare_component(self):
        service = coordinator()
        started = threading.Event()
        def download(*, cancel, progress):
            started.set()
            while not cancel():
                time.sleep(.005)
            raise UpdateCancelled('Cancelled')
        service.download.side_effect = download
        dialog = self.make(service)
        QTest.mouseClick(dialog.btn_update, Qt.MouseButton.LeftButton)
        until(started.is_set)
        QTest.mouseClick(dialog.btn_update, Qt.MouseButton.LeftButton)
        until(lambda: dialog._state == 'available')
        service.prepare_components.assert_not_called()

    def test_full_fallback_requires_explicit_acceptance(self):
        service = coordinator()
        service.download.side_effect = [FullDownloadRequired('patch failed'), {}]
        dialog = self.make(service)
        dialog.btn_update.click()
        until(lambda: dialog._state == 'full_required')
        self.assertIn('250.0 MB', dialog.error_details.summary.text())
        service.use_full_download.assert_not_called()
        dialog.btn_update.click()
        until(lambda: dialog._state == 'ready')
        service.use_full_download.assert_called_once()

    def test_only_own_pending_close_action_is_cancelled(self):
        service = coordinator()
        parent = QMainWindow()
        parent.show()
        self.views.insert(0, parent)
        action = object()
        parent._close_action = None
        parent.cancel_close_action = Mock(side_effect=lambda: setattr(parent, '_close_action', None))
        def request(owner):
            owner._close_action = action
            return True
        service.request_install.side_effect = request
        dialog = self.make(service, parent)
        dialog.btn_update.click()
        until(lambda: dialog._state == 'ready')
        dialog.btn_update.click()
        until(lambda: dialog._state == 'waiting')
        parent._close_action = object()
        dialog.reject()
        parent.cancel_close_action.assert_not_called()

    def test_own_pending_close_action_is_cancelled_on_dismissal(self):
        service = coordinator()
        parent = QMainWindow()
        parent.show()
        self.views.insert(0, parent)
        parent._close_action = None
        parent.cancel_close_action = Mock(side_effect=lambda: setattr(parent, '_close_action', None))
        def request(owner):
            owner._close_action = object()
            return True
        service.request_install.side_effect = request
        dialog = self.make(service, parent)
        dialog.btn_update.click()
        until(lambda: dialog._state == 'ready')
        dialog.btn_update.click()
        until(lambda: dialog._state == 'waiting')
        dialog.reject()
        parent.cancel_close_action.assert_called_once()

    def test_cancelled_pending_handoff_returns_to_ready(self):
        service = coordinator()
        parent = QMainWindow()
        parent.show()
        self.views.insert(0, parent)
        parent._close_action = None
        def request(owner):
            owner._close_action = object()
            return True
        service.request_install.side_effect = request
        dialog = self.make(service, parent)
        dialog.btn_update.click()
        until(lambda: dialog._state == 'ready')
        dialog.btn_update.click()
        until(lambda: dialog._state == 'waiting')
        parent._close_action = None
        until(lambda: dialog._state == 'ready')
        self.assertTrue(parent.isVisible())
        self.assertTrue(dialog.isVisible())
        self.assertIn('Restart cancelled', dialog.status.text())

    def test_actual_parent_close_completes_sync_and_delayed_handoffs(self):
        class GuardedWindow(QMainWindow):
            def __init__(self, delayed):
                super().__init__()
                self._close_action = None
                self.allowed = not delayed
                self.handoffs = 0

            def request_close_action(self, action):
                self._close_action = action
                return self.close()

            def closeEvent(self, event):
                if not self.allowed:
                    event.ignore()
                    return
                action = self._close_action
                self._close_action = None
                if action is not None and not action():
                    event.ignore()
                    return
                super().closeEvent(event)

        for delayed in (False, True):
            with self.subTest(delayed=delayed):
                service = coordinator()
                parent = GuardedWindow(delayed)
                parent.show()
                self.views.insert(0, parent)
                def request(owner):
                    def handoff():
                        owner.handoffs += 1
                        return True
                    owner.request_close_action(handoff)
                    return owner._close_action is handoff or not owner.isVisible()
                service.request_install.side_effect = request
                dialog = self.make(service, parent)
                dialog.btn_update.click()
                until(lambda: dialog._state == 'ready')
                dialog.btn_update.click()
                if delayed:
                    until(lambda: dialog._state == 'waiting')
                    self.assertTrue(parent.isVisible())
                    self.assertTrue(dialog.isVisible())
                    parent.allowed = True
                    parent.close()
                until(lambda: not parent.isVisible() and not dialog.isVisible())
                self.assertEqual(parent.handoffs, 1)
                self.assertFalse(has_active_tasks())
                self.views.remove(dialog)
                self.views.remove(parent)
                dispose_closed_views(dialog, parent)

    def test_nvidia_preparation_finishes_before_dialog_closes(self):
        service = coordinator()
        service.component_required = True
        service.component_download_size = 3 * 1024 ** 3
        started, release = threading.Event(), threading.Event()
        def prepare(*, progress):
            progress(1, 4)
            started.set()
            release.wait(3)
        service.prepare_components.side_effect = prepare
        dialog = self.make(service)
        self.assertIn('3.000 GB for NVIDIA support', dialog.log_view.toPlainText())
        self.assertIn('Download: 3.004 GB', dialog.log_view.toPlainText())
        output = os.environ.get('TEMPO_UPDATE_PREVIEW_DIR')
        if output:
            dialog.grab().save(str(Path(output) / 'ordinary-nvidia-available.png'))
        dialog.btn_update.click()
        until(started.is_set)
        until(lambda: dialog._component_preparing)
        self.assertFalse(dialog.btn_update.isEnabled())
        self.assertTrue(has_active_tasks())
        dialog.close()
        self.assertTrue(dialog.isVisible())
        self.assertIn('Finishing NVIDIA', dialog.status.text())
        release.set()
        until(lambda: not dialog.isVisible())
        service.request_install.assert_not_called()

    def test_long_release_notes_scroll_without_hiding_actions(self):
        APP.setFont(QFont('Arial', 18))
        service = coordinator()
        service.component_required = True
        service.component_download_size = 3 * 1024 ** 3
        sections = [
            ('Analysis', 'Compare recordings with improved views of contraction timing and amplitude.'),
            ('Recording', 'Review capture settings and retain your selected configuration between sessions.'),
            ('Collaboration', 'Keep drafts and pending submissions available while work is interrupted.'),
            ('Reliability', 'Recover interrupted preparation and review actionable errors before retrying.'),
            ('Compatibility', 'Existing recordings and user-selected data locations remain available.'),
            ('More details', 'See the user guide for complete instructions and troubleshooting.'),
        ]
        service.plan.target.release_notes = '\n\n'.join(
            f'### {heading}\n\n{text}' for heading, text in sections)
        dialog = self.make(service)
        dialog.resize(620, 420)
        APP.processEvents()
        self.assertIn('Download: 3.004 GB', dialog.log_view.toPlainText())
        scrollbar = dialog.log_view.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        output = os.environ.get('TEMPO_UPDATE_PREVIEW_DIR')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            dialog.grab().save(str(Path(output) / 'large-font-long-start.png'))
        scrollbar.setValue(scrollbar.maximum())
        APP.processEvents()
        self.assertEqual(scrollbar.value(), scrollbar.maximum())
        cursor = QTextCursor(dialog.log_view.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.assertTrue(dialog.log_view.viewport().rect().contains(dialog.log_view.cursorRect(cursor).bottomRight()))
        for button in (dialog.retry, dialog.btn_update):
            self.assertTrue(button.isVisible())
            self.assertTrue(dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight())))
        if output:
            dialog.grab().save(str(Path(output) / 'large-font-long-end.png'))

    def test_real_state_renders_and_controls_fit(self):
        output = os.environ.get('TEMPO_UPDATE_PREVIEW_DIR')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
        for size, points, label in (((620, 420), 12, 'ordinary'), ((520, 360), 12, 'small'),
                                    ((1000, 600), 12, 'wide'),
                                    ((620, 420), 18, 'large-font')):
            APP.setFont(QFont('Arial', points))
            for state in ('available', 'downloading', 'ready', 'error'):
                with self.subTest(size=label, state=state):
                    service = coordinator()
                    started = threading.Event(); release = threading.Event()
                    def delayed(*, cancel, progress):
                        progress(42, 100); started.set()
                        while not release.wait(.005):
                            if cancel(): raise UpdateCancelled('Cancelled')
                    if state == 'downloading': service.download.side_effect = delayed
                    if state == 'error': service.download.side_effect = OSError('Not enough disk space. Free space and try again.')
                    dialog = self.make(service)
                    dialog.resize(*size)
                    if state != 'available':
                        dialog.btn_update.click()
                        if state == 'downloading':
                            until(started.is_set)
                            until(lambda: dialog.progress.maximum() == 100)
                            self.assertEqual(dialog.progress.value(), 42)
                            self.assertEqual(dialog.progress.format(), 'Downloading update: %p%')
                        else:
                            until(lambda: dialog._state == state)
                    QTest.qWait(50)
                    self.assertEqual((dialog.width(), dialog.height()), size)
                    for button in (dialog.retry, dialog.btn_update):
                        self.assertTrue(dialog.rect().contains(button.mapTo(dialog, QPoint(0, 0))))
                        self.assertTrue(dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight())))
                        self.assertGreaterEqual(button.width(), button.fontMetrics().horizontalAdvance(button.text()) + 10)
                    # Real click, pointer departure and keyboard focus changes.
                    QTest.mouseClick(dialog.log_view.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
                    QTest.mouseMove(dialog.btn_update, dialog.btn_update.rect().center())
                    QTest.mouseMove(dialog, QPoint(2, 2))
                    dialog.retry.setFocus(Qt.FocusReason.TabFocusReason)
                    APP.processEvents()
                    if output:
                        dialog.grab().save(str(Path(output)/f'{label}-{state}.png'))
                    release.set()
                    if state == 'downloading': until(lambda: dialog._task is None)
                    dialog.close()
                    self.views.remove(dialog)
                    dispose_closed_views(dialog)


if __name__ == '__main__':
    unittest.main()
