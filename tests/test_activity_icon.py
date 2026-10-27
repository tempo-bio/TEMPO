"""Native icon dispatch, saved opt-in, cached animation, and real activity gates."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QCoreApplication, QEvent, QThread
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QWidget

import tempo_activity_icon as activity
import tempo_config as config
import tempo_tasks as tasks

APP = QApplication.instance() or QApplication([])


class ActivityIconTests(unittest.TestCase):
    def setUp(self):
        self.window = QWidget()
        self.window.setWindowIcon(QIcon(str(activity._ASSETS / 'tempo_icon.png')))
        self.addCleanup(self.dispose)

    def dispose(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def controller(self, busy=False, platform='darwin'):
        self.busy = busy
        target = SimpleNamespace(supported=True, platform=platform, apply=Mock(), restore=Mock())
        controller = activity.ActivityIconController(APP, self.window, target=target, probe=lambda: self.busy)
        self.addCleanup(controller.shutdown)
        return controller, target

    def test_both_platforms_default_to_zero_timers_zero_frames_and_no_icon_updates(self):
        self.assertIs(config.DEFAULT_PREFERENCES['appearance']['animate_activity_icon']['value'], False)
        preferences = copy.deepcopy(config.DEFAULT_PREFERENCES)
        with patch.object(config.tempo_preferences, '_preferences', preferences):
            for platform in ('darwin', 'win32'):
                controller, target = self.controller(busy=True, platform=platform)
                with patch.object(activity, 'IconFrames') as frames:
                    controller.apply_preferences()
                    controller.poll()
                    controller.tick()
                self.assertFalse(controller.enabled)
                self.assertFalse(controller.poll_timer.isActive())
                self.assertFalse(controller.animation_timer.isActive())
                self.assertIsNone(controller.frames)
                frames.assert_not_called()
                target.apply.assert_not_called()

    def test_enabled_idle_avoids_rendering_then_animates_minimized_work_and_restores(self):
        controller, target = self.controller()
        controller.clock = SimpleNamespace(start=Mock(), nsecsElapsed=lambda: 0)
        controller.set_enabled(True)
        self.assertTrue(controller.poll_timer.isActive())
        self.assertFalse(controller.animation_timer.isActive())
        self.assertIsNone(controller.frames)
        self.window.showMinimized()
        self.busy = True
        controller.poll()
        self.assertTrue(controller.animation_timer.isActive())
        target.apply.assert_called_once()
        controller.tick()  # Same clock frame cannot trigger another OS update.
        target.apply.assert_called_once()
        self.busy = False
        controller.poll()
        self.assertFalse(controller.animation_timer.isActive())
        target.restore.assert_called()
        controller.set_enabled(False)
        self.assertFalse(controller.poll_timer.isActive())
        self.assertIsNone(controller.frames)

    def test_disabling_or_quitting_restores_icon_and_releases_frames(self):
        controller, target = self.controller(busy=True, platform='win32')
        controller.set_enabled(True)
        self.assertIsNotNone(controller.frames)
        controller.shutdown()
        self.assertFalse(controller.enabled)
        self.assertFalse(controller.poll_timer.isActive())
        self.assertFalse(controller.animation_timer.isActive())
        self.assertIsNone(controller.frames)
        target.restore.assert_called()

    def test_activity_observer_failure_cannot_escape_into_application_work(self):
        controller, target = self.controller()
        controller.probe = Mock(side_effect=RuntimeError('Unavailable activity source'))
        with self.assertLogs('tempo_activity_icon', level='ERROR'):
            controller.set_enabled(True)
        self.assertFalse(controller.enabled)
        self.assertFalse(controller.poll_timer.isActive())
        self.assertFalse(controller.animation_timer.isActive())
        target.apply.assert_not_called()

    def test_native_routes_use_dock_application_icon_or_explicit_windows_icon(self):
        icon = QIcon(str(activity._ASSETS / 'tempo_icon.png'))
        for platform in ('darwin', 'win32'):
            target = activity.NativeIconTarget(APP, self.window, platform=platform)
            with patch.object(APP, 'setWindowIcon') as dock, patch.object(self.window, 'setWindowIcon') as taskbar:
                target.apply(icon)
                target.restore()
                if platform == 'darwin':
                    self.assertEqual(dock.call_count, 2)
                    taskbar.assert_not_called()
                    self.assertEqual(dock.call_args.args[0].cacheKey(), target.original_app.cacheKey())
                else:
                    self.assertEqual(taskbar.call_count, 2)
                    self.assertEqual(dock.call_count, 2)
                    self.assertEqual(dock.call_args.args[0].cacheKey(), target.original_app.cacheKey())
                    self.assertEqual(taskbar.call_args.args[0].cacheKey(), target.original_window.cacheKey())

    def test_icons_are_square_transparent_keep_static_letters_and_have_bounded_cache(self):
        for size in (128, 256):
            frames = activity.IconFrames(size)
            empty = frames.at(0).pixmap(size, size).toImage()
            full = frames.at(18).pixmap(size, size).toImage()
            self.assertEqual(empty.size(), full.size())
            self.assertEqual(empty.pixelColor(0, 0).alpha(), 0)
            peak = (round(658 * size / 1254), round(498 * size / 1254))
            self.assertLess(empty.pixelColor(*peak).green(), 80)
            self.assertGreater(full.pixelColor(*peak).green(), 170)
            for x, y, width, height in ((219, 551, 131, 157), (872, 548, 164, 164),
                                         (373, 551, 115, 90), (737, 551, 122, 60)):
                region = tuple(round(value * size / 1254) for value in (x, y, width, height))
                self.assertEqual(empty.copy(*region), full.copy(*region))
            for index in range(72):
                frames.at(index)
            self.assertEqual(len(frames.frames), 36)
            self.assertIs(frames.at(1), frames.at(37))
        self.assertEqual(activity.IconFrames.index_at(1.45), activity.IconFrames.index_at(1.6))
        self.assertEqual(activity.IconFrames.index_at(0), activity.IconFrames.index_at(2.95))

    def test_pacing_requires_acknowledged_active_output_not_just_a_connection(self):
        tools = SimpleNamespace(service=SimpleNamespace(connected=True, status=None))
        self.window.tab_capture = SimpleNamespace(capture_controls=tools)
        with patch.object(tasks, 'has_active_tasks', return_value=False), patch.object(APP, 'topLevelWidgets', return_value=[self.window]):
            self.assertFalse(activity.desktop_work_active(self.window))
            for mode, active in [('REST_DISCHARGE', False), ('DISCONNECTED_HIZ', False),
                                 ('PACING', True), ('CONSTANT_STIM', True), ('SQUARE_10HZ', True)]:
                tools.service.status = SimpleNamespace(mode=mode)
                self.assertEqual(activity.desktop_work_active(self.window), active)
            tools.service.connected = False
            self.assertFalse(activity.desktop_work_active(self.window))
            tools.session = SimpleNamespace(active=True)
            self.assertTrue(activity.desktop_work_active(self.window))
            tools.session.active = False
            from concurrent.futures import Future
            future = Future()
            tools._pending = [(future, None)]
            self.assertTrue(activity.desktop_work_active(self.window))
            future.set_result(None)
            self.assertFalse(activity.desktop_work_active(self.window))

    def test_actual_local_workers_and_background_reads_count_until_finished(self):
        worker = QThread(self.window)
        self.window.worker = worker
        with patch.object(tasks, 'has_active_tasks', return_value=False), patch.object(APP, 'topLevelWidgets', return_value=[self.window]):
            with patch.object(worker, 'isRunning', return_value=True):
                self.assertTrue(activity.desktop_work_active(self.window))
            self.assertFalse(activity.desktop_work_active(self.window))
        done = threading.Event()
        result = SimpleNamespace(done_event=done)
        with patch.object(tasks, '_background_tasks', [result]), patch.object(tasks, '_modal_tasks', set()):
            self.assertTrue(tasks.has_active_tasks())
            done.set()
            self.assertFalse(tasks.has_active_tasks())

    def test_cloud_training_counts_fresh_active_states_and_stops_for_waiting_or_stale(self):
        now = time.time()
        maintainer = SimpleNamespace(_allowed=True, request={'id': 'run'},
            _activity_training_status={'phase': 'training', 'updated_at': now}, model_evaluation=None)
        self.window.tab_maintainer = maintainer
        with patch.object(tasks, 'has_active_tasks', return_value=False), patch.object(APP, 'topLevelWidgets', return_value=[self.window]):
            self.assertTrue(activity.desktop_work_active(self.window))
            for phase in ('awaiting_colab', 'ready', 'completed', 'failed', 'cancelled', 'disconnected'):
                maintainer._activity_training_status = {'phase': phase, 'updated_at': now}
                self.assertFalse(activity.desktop_work_active(self.window))
            maintainer._activity_training_status = {'phase': 'training', 'updated_at': now - 121}
            self.assertFalse(activity.desktop_work_active(self.window))
            maintainer.model_evaluation = SimpleNamespace(payload={'state': {'phase': 'running', 'updated_at': now}})
            self.assertTrue(activity.desktop_work_active(self.window))
            maintainer._allowed = False
            self.assertFalse(activity.desktop_work_active(self.window))
        for stamp in (None, True, 'now', float('nan'), float('inf'), now + 300):
            self.assertFalse(activity.cloud_work_active({'phase': 'training', 'updated_at': stamp}, now=now))


if __name__ == '__main__':
    unittest.main()
