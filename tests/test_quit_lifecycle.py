"""Offscreen shutdown checks; no network, hardware, or research files are used.

The child uses the real splash and dashboard. Only external services, storage paths,
and update-probe results are substituted. Simulated reads are bounded and released
before interpreter teardown. Native desktop windows are forbidden in this harness.
"""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import platform
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def quit_probe(scenario):
    print('CHILD_STARTING', flush=True)
    import json
    import tempfile
    import threading
    from unittest.mock import Mock, patch
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    app = QApplication([])
    # Match tempo_gui's actual desktop entry point instead of Windows' default
    # style, which is unrelated to the shipped application's shutdown behavior.
    app.setStyle('Fusion')
    app.setQuitOnLastWindowClosed(False)
    from tempo_tasks import install_quit_guard, release_background_signals, dispose_closed_views, dispose_application
    install_quit_guard(app)
    print('QT_CREATED', flush=True)
    if scenario.startswith('setup_'):
        import setup_wizard as setup
        from PyQt6 import sip
        from tempo_tasks import TaskThread
        original = setup.SetupWizardDialog
        dialogs = []
        release = threading.Event()
        def wizard_factory():
            wizard = original()
            dialogs.append(wizard)
            if scenario == 'setup_busy_cancel':
                wizard.worker = TaskThread(lambda: release.wait(3), wizard)
                wizard.worker.start()
                def cancel():
                    assert wizard.worker.isRunning()
                    wizard.reject()
                    assert wizard.isVisible()
                    print('SETUP_CANCEL_DEFERRED', flush=True)
                    release.set()
                    def joined():
                        if not wizard.worker.wait(0):
                            QTimer.singleShot(10, joined)
                            return
                        print('SETUP_WORKER_JOINED', flush=True)
                        wizard.reject()
                    joined()
                QTimer.singleShot(30, cancel)
            else:
                QTimer.singleShot(30, wizard.accept if scenario == 'setup_accept' else wizard.reject)
            return wizard
        def launch(*args, **kwargs):
            assert QApplication.instance() is None
            assert sip.isdeleted(dialogs[0])
            assert args[0][1] == os.path.join(setup.BASE_DIR, 'tempo_gui.py')
            print('DESKTOP_LAUNCHED_AFTER_QT_DISPOSAL', flush=True)
        with patch.object(setup, 'run_background', return_value=None), \
             patch.object(setup, 'SetupWizardDialog', side_effect=wizard_factory), \
             patch.object(setup.subprocess, 'Popen', side_effect=launch) as launched:
            result = setup.run_setup_application(app)
        assert len(dialogs) == 1 and sip.isdeleted(dialogs[0])
        assert QApplication.instance() is None
        assert launched.call_count == (1 if scenario == 'setup_accept' else 0)
        print('CLOSE_ACCEPTED True', flush=True)
        print('QT_EXITED', result, flush=True)
        print('QT_APPLICATION_DISPOSED', flush=True)
        print('CHILD_MAIN_RETURNED', flush=True)
        return result
    with patch('platform.machine', return_value='x86_64'):
        import tempo_gui as gui
    print('GUI_IMPORTED', flush=True)
    import update_wizard as updates

    release_reads = threading.Event()
    read_threads = []
    def blocked():
        read_threads.append(threading.current_thread())
        print('PROBE_BLOCKED', flush=True)
        release_reads.wait(8)
    def failed():
        raise ConnectionError('simulated offline')

    software = blocked if scenario in ('software_stalled', 'both_stalled', 'early_close', 'quit_event', 'splash_quit') else lambda: (False, '')
    models = blocked if scenario in ('models_stalled', 'both_stalled', 'early_close', 'quit_event', 'splash_quit') else lambda: False
    if scenario == 'offline':
        software = models = failed
    with tempfile.TemporaryDirectory() as directory, \
         patch.object(gui.tempo_backend, 'UNIVERSAL_DATASET_DIR', directory), \
         patch.object(gui.tempo_backend, 'QUEUE_FILE', directory + '/queue.json'), \
         patch.object(gui.tempo_hub, 'token', None), \
         patch.object(gui.tempo_hub, 'get_collaborator_id', return_value='Quit Test'), \
         patch.object(gui.tempo_hub, 'cached_lock_manifest', return_value={'images': {}}), \
         patch.object(gui.tempo_hub, 'fetch_lock_manifest', return_value={'images': {}}), \
         patch.object(gui.tempo_hub, 'is_maintainer', return_value=False), \
         patch.object(updates, 'software_release', side_effect=software), \
         patch.object(updates, 'model_release_probe', side_effect=models):
        if scenario in ('splash_quit', 'startup_idle'):
            original_splash, original_dashboard = updates.SplashScreen, gui.TempoDashboard
            dashboards = []
            def splash_factory():
                splash = original_splash()
                if scenario == 'splash_quit':
                    QTimer.singleShot(20, app.quit)
                return splash
            def dashboard_factory():
                window = original_dashboard()
                dashboards.append(window)
                print('DASHBOARD_READY', flush=True)
                QTimer.singleShot(100, app.quit)
                return window
            QTimer.singleShot(5000, lambda: app.exit(73))
            try:
                with patch.object(updates, 'SplashScreen', side_effect=splash_factory), \
                     patch.object(gui, 'TempoDashboard', side_effect=dashboard_factory):
                    result = gui.run_desktop_application(app)
            finally:
                release_reads.set()
                for thread in read_threads:
                    thread.join(timeout=2)
                    assert not thread.is_alive()
            assert getattr(app, '_tempo_quit_requested', False)
            if scenario == 'splash_quit':
                assert not dashboards
                print('STARTUP_QUIT_ACCEPTED', flush=True)
            else:
                assert len(dashboards) == 1
            print('CLOSE_ACCEPTED True', flush=True)
            print('QT_EXITED', result, flush=True)
            dispose_application(app)
            print('QT_APPLICATION_DISPOSED', flush=True)
            print('CHILD_MAIN_RETURNED', flush=True)
            return result
        splash = updates.SplashScreen()
        if scenario == 'early_close':
            QTimer.singleShot(20, splash.reject)
        splash.exec()
        print('SPLASH_SETTLED', flush=True)
        window = gui.TempoDashboard()
        window.show()
        app.setQuitOnLastWindowClosed(True)
        print('DASHBOARD_READY', flush=True)
        if scenario == 'idle':
            try:
                dispose_application(app)
            except RuntimeError:
                assert window.isVisible()
            else:
                raise AssertionError('Application disposal bypassed a visible window.')
        processing_released = threading.Event()
        auxiliary_released = threading.Event()
        auxiliary_worker = None
        preview_camera = None
        if scenario == 'preview_quit':
            import numpy as np
            preview_camera = Mock()
            def read_frame():
                auxiliary_released.wait(2)
                return True, np.zeros((12, 12, 3), dtype=np.uint8)
            preview_camera.read.side_effect = read_frame
            window.tab_capture.cv_cap = preview_camera
            window.tab_capture.timer.start(1000)
            window.tab_capture.update_frame()
        if scenario == 'annotation_quit':
            from tempo_tasks import TaskThread
            auxiliary_worker = TaskThread(lambda: auxiliary_released.wait(2), window.tab_annotator)
            window.tab_annotator.auto_worker = auxiliary_worker
            auxiliary_worker.start()
        active = scenario in ('active_processing_cancel', 'active_processing_wait')
        if active:
            from tempo_processing import ProcessingResult
            def process(video_path, cancel=None, **kwargs):
                while not processing_released.wait(0.01):
                    if cancel.is_cancelled():
                        return ProcessingResult(video_path, outcome='cancelled')
                return ProcessingResult(video_path, outcome='no_beats')
            processing_patch = patch.object(gui.tempo_backend, 'process_assay_result', side_effect=process)
            processing_patch.start()
            window.lock_buttons()
            window.worker = gui.ProcessingWorker('single', filepath=directory + '/probe.avi')
            window.worker.result_signal.connect(window.record_processing_result)
            window.worker.state_signal.connect(window.job_status.setText)
            window.worker.finished.connect(window.unlock_buttons)
            window.worker.start()
        scenario_state = []
        def close_window():
            print('CLOSE_REQUESTED', flush=True)
            if scenario == 'credential_migration_quit' and not scenario_state:
                from tempo_secure_store import CredentialMigrationRequired
                from tempo_tasks import pending_task_titles
                def ensure(*, migrate=False):
                    if gui.tempo_hub._credentials_ready:
                        return
                    if not migrate:
                        raise CredentialMigrationRequired('Fixture migration required.')
                    assert auxiliary_released.wait(2)
                    gui.tempo_hub.token = 'hf_SYNTHETICShutdownFixture0123456789'
                def background(action, done):
                    try:
                        value = action()
                    except Exception as error:
                        done(None, error)
                    else:
                        done(value, None)
                def try_quit():
                    app.quit()
                    assert window.isVisible()
                    assert pending_task_titles() == ['Securing saved account…']
                    print('CREDENTIAL_QUIT_DEFERRED', flush=True)
                    QTimer.singleShot(100, auxiliary_released.set)
                with patch.object(gui.tempo_hub, '_credentials_ready', False), \
                     patch.object(gui.tempo_hub, 'ensure_credentials', side_effect=ensure), \
                     patch.object(gui.tempo_hub, 'authenticated_account', return_value='hf:fixture-account'), \
                     patch.object(gui.tempo_hub, 'get_account_display_name', return_value='fixture-researcher'), \
                     patch.object(gui, 'run_background', side_effect=background):
                    QTimer.singleShot(20, try_quit)
                    window.refresh_account_indicator()
                    assert not pending_task_titles()
                    assert 'fixture-researcher' in window.account_indicator.text()
                    print('CREDENTIAL_MIGRATION_CONFIRMED', flush=True)
                scenario_state.append('settled')
                QTimer.singleShot(100, app.quit)
                return
            if scenario == 'modal_write_quit' and not scenario_state:
                from tempo_tasks import run_task, pending_task_titles
                def try_quit():
                    app.quit()
                    assert window.isVisible()
                    assert pending_task_titles() == ['Protected write fixture']
                    print('PROTECTED_QUIT_DEFERRED', flush=True)
                    QTimer.singleShot(100, auxiliary_released.set)
                QTimer.singleShot(20, try_quit)
                value = run_task(window, 'Protected write fixture', lambda: auxiliary_released.wait(2) and 17)
                assert value == 17
                assert not pending_task_titles()
                print('WRITE_OUTCOME_CONFIRMED', flush=True)
                scenario_state.append('settled')
                QTimer.singleShot(100, app.quit)
                return
            if scenario == 'preview_quit':
                app.quit()
                assert window.isVisible()
                preview_camera.release.assert_not_called()
                print('CAMERA_QUIT_DEFERRED', flush=True)
                QTimer.singleShot(100, auxiliary_released.set)
                return
            if scenario in ('annotation_quit', 'sync_quit') and not scenario_state:
                if scenario == 'sync_quit':
                    window.tab_annotator._sync_busy = True
                app.quit()
                assert window.isVisible()
                print('APPLICATION_QUIT_DEFERRED', flush=True)
                def settle():
                    auxiliary_released.set()
                    if auxiliary_worker is not None and not auxiliary_worker.wait(0):
                        QTimer.singleShot(20, settle)
                        return
                    window.tab_annotator._sync_busy = False
                    QTimer.singleShot(0, app.quit)
                scenario_state.append('settling')
                QTimer.singleShot(100, settle)
                return
            if scenario == 'pending_write' and not scenario_state:
                window.tab_maintainer._pending_writes = 1
                assert not window.close()
                assert window.tabs.currentWidget() is window.tab_maintainer
                assert 'maintainer action' in window.statusBar().currentMessage()
                print('WRITE_CLOSE_DEFERRED', flush=True)
                window.tab_maintainer._pending_writes = 0
                QTimer.singleShot(100, close_window)
                scenario_state.append('settled')
                return
            if scenario == 'unsynced_saved':
                window.tab_annotator.universal_annotations = {'sample.png': {'boxes': [], 'sync_pending': True}}
                window.tab_annotator.save_universal_annotations()
            with patch.object(gui, 'confirm_action', return_value=scenario != 'active_processing_wait'):
                if active:
                    assert not window.close()
                    assert window.isVisible()
                    print('ACTIVE_CLOSE_DEFERRED', flush=True)
                    if scenario == 'active_processing_wait':
                        assert window.worker.isRunning()
                        assert not window.worker.cancel_token.is_cancelled()
                        print('PROCESSING_CONTINUES', flush=True)
                        processing_released.set()
                        QTimer.singleShot(300, window.close)
                    return
                if scenario == 'app_quit':
                    app.quit()
                elif scenario == 'quit_event':
                    app.closeAllWindows()
                else:
                    print('INITIAL_CLOSE_ACCEPTED', window.close(), flush=True)
        QTimer.singleShot(100, close_window)
        QTimer.singleShot(5000, lambda: app.exit(73))
        try:
            result = app.exec()
        finally:
            # Test stalls must never survive into Qt/SIP interpreter teardown.
            release_reads.set()
            processing_released.set()
            auxiliary_released.set()
            if auxiliary_worker is not None:
                assert auxiliary_worker.wait(2000)
            for thread in read_threads:
                thread.join(timeout=2)
                assert not thread.is_alive()
        if active:
            assert not window.worker.isRunning()
            processing_patch.stop()
        # Ordered camera/stimulation shutdown may defer the first close request.
        # Accept only the settled state; disposal below still rejects live views
        # and unfinished workers, and the five-second exit deadline is unchanged.
        print('CLOSE_ACCEPTED', not window.isVisible(), flush=True)
        if preview_camera is not None:
            preview_camera.release.assert_called_once()
            print('CAMERA_RELEASE_CONFIRMED', flush=True)
        if scenario == 'unsynced_saved':
            saved = json.loads(Path(window.tab_annotator.universal_json_path).read_text())
            assert saved['sample.png']['sync_pending'] is True
            print('LOCAL_DRAFT_PRESERVED', flush=True)
        print('QT_EXITED', result, flush=True)
        from tempo_tasks import dispose_closed_views
        dispose_closed_views(window, splash)
    dispose_application(app)
    print('QT_APPLICATION_DISPOSED', flush=True)
    print('CHILD_MAIN_RETURNED', flush=True)
    return result


class QuitProcessTests(unittest.TestCase):
    def test_first_run_cancellation_disposes_qt_without_launching_desktop(self):
        result = self.run_scenario('setup_cancel')
        self.assertNotIn('DESKTOP_LAUNCHED_AFTER_QT_DISPOSAL', result.stdout)

    def test_first_run_completion_disposes_qt_before_launching_desktop(self):
        result = self.run_scenario('setup_accept')
        self.assertIn('DESKTOP_LAUNCHED_AFTER_QT_DISPOSAL', result.stdout)

    def test_first_run_cancellation_waits_for_requirements_worker(self):
        result = self.run_scenario('setup_busy_cancel')
        self.assertIn('SETUP_CANCEL_DEFERRED', result.stdout)
        self.assertIn('SETUP_WORKER_JOINED', result.stdout)
        self.assertNotIn('DESKTOP_LAUNCHED_AFTER_QT_DISPOSAL', result.stdout)

    def test_setup_and_capture_fixtures_exit_without_sip_cleanup_crash(self):
        command = [sys.executable, '-X', 'faulthandler', '-m', 'unittest',
                   'tests.test_setup_model_updates', 'tests.test_capture_workflow']
        if sys.platform == 'darwin':
            command = ['arch', '-' + platform.machine(), *command]
        result = subprocess.run(command, cwd=ROOT,
                                env=dict(os.environ, QT_QPA_PLATFORM='offscreen', HF_HUB_OFFLINE='1'),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stderr, r'Ran [1-9][0-9]* tests')
        self.assertNotIn('Fatal Python error', result.stderr)

    def run_scenario(self, scenario):
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONUNBUFFERED='1', PYTHONFAULTHANDLER='1')
        command = [sys.executable, str(Path(__file__).resolve()), '--probe', scenario]
        if sys.platform == 'darwin':
            # Universal Python executables otherwise relaunch as ARM even when
            # this suite runs under Rosetta with Intel-only Qt dependencies.
            command = ['arch', '-' + platform.machine(), *command]
        try:
            result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                    text=True, timeout=60)
        except subprocess.TimeoutExpired as exc:
            self.fail(f'{scenario}: child exceeded the startup/exit deadline. '
                      f'Last lifecycle markers: {exc.stdout!r}. Stderr: {exc.stderr!r}')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('CLOSE_ACCEPTED True', result.stdout)
        self.assertIn('QT_EXITED 0', result.stdout)
        self.assertIn('QT_APPLICATION_DISPOSED', result.stdout)
        self.assertIn('CHILD_MAIN_RETURNED', result.stdout)
        self.assertNotIn('QThread: Destroyed while thread', result.stderr)
        return result

    def test_idle_process_exits(self):
        self.run_scenario('idle')

    def test_stalled_software_probe_cannot_hold_process_open(self):
        self.run_scenario('software_stalled')

    def test_stalled_model_probe_cannot_hold_process_open(self):
        self.run_scenario('models_stalled')

    def test_both_stalled_probes_cannot_hold_process_open(self):
        self.run_scenario('both_stalled')

    def test_early_splash_dismissal_and_close_exits_with_stalled_checks(self):
        self.run_scenario('early_close')

    def test_application_close_all_windows_exits_with_stalled_checks(self):
        self.run_scenario('quit_event')

    def test_application_quit_closes_dashboard_and_exits(self):
        self.run_scenario('app_quit')

    def test_application_quit_during_splash_never_opens_dashboard(self):
        result = self.run_scenario('splash_quit')
        self.assertIn('STARTUP_QUIT_ACCEPTED', result.stdout)
        self.assertNotIn('DASHBOARD_READY', result.stdout)

    def test_production_startup_event_loop_opens_one_dashboard_and_exits(self):
        result = self.run_scenario('startup_idle')
        self.assertEqual(result.stdout.count('DASHBOARD_READY'), 1)

    def test_application_quit_waits_for_annotation_worker(self):
        result = self.run_scenario('annotation_quit')
        self.assertIn('APPLICATION_QUIT_DEFERRED', result.stdout)

    def test_application_quit_waits_for_cloud_sync_outcome(self):
        result = self.run_scenario('sync_quit')
        self.assertIn('APPLICATION_QUIT_DEFERRED', result.stdout)

    def test_application_quit_waits_for_protected_modal_write_result(self):
        result = self.run_scenario('modal_write_quit')
        self.assertIn('PROTECTED_QUIT_DEFERRED', result.stdout)
        self.assertIn('WRITE_OUTCOME_CONFIRMED', result.stdout)

    def test_application_quit_waits_for_credential_migration_and_readback(self):
        result = self.run_scenario('credential_migration_quit')
        self.assertIn('CREDENTIAL_QUIT_DEFERRED', result.stdout)
        self.assertIn('CREDENTIAL_MIGRATION_CONFIRMED', result.stdout)

    def test_application_quit_waits_for_preview_then_releases_camera(self):
        result = self.run_scenario('preview_quit')
        self.assertIn('CAMERA_QUIT_DEFERRED', result.stdout)
        self.assertIn('CAMERA_RELEASE_CONFIRMED', result.stdout)

    def test_offline_probes_do_not_prevent_exit(self):
        self.run_scenario('offline')

    def test_unsynced_saved_draft_survives_close(self):
        result = self.run_scenario('unsynced_saved')
        self.assertIn('LOCAL_DRAFT_PRESERVED', result.stdout)

    def test_quit_cooperatively_cancels_and_waits_for_processing_thread(self):
        result = self.run_scenario('active_processing_cancel')
        self.assertIn('ACTIVE_CLOSE_DEFERRED', result.stdout)

    def test_declining_cancellation_keeps_processing_and_window_alive(self):
        result = self.run_scenario('active_processing_wait')
        self.assertIn('PROCESSING_CONTINUES', result.stdout)

    def test_pending_write_blocks_close_and_surfaces_maintainer_until_settled(self):
        result = self.run_scenario('pending_write')
        self.assertIn('WRITE_CLOSE_DEFERRED', result.stdout)


class StartupProbeResultTests(unittest.TestCase):
    def setUp(self):
        from PyQt6.QtWidgets import QApplication
        from unittest.mock import patch
        import update_wizard
        self.app = QApplication.instance() or QApplication([])
        self.pending = []
        self.patch = patch.object(update_wizard, 'run_background', side_effect=lambda action, done: self.pending.append(done))
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.check = update_wizard.StartupUpdateCheck()
        self.addCleanup(self.check.deadline.stop)
        self.results = []
        self.check.check_finished.connect(lambda found, details: self.results.append((found, details)))
        self.check.start()

    def test_completed_checks_report_both_updates_once(self):
        self.pending[0]((True, 'New software'), None)
        self.assertFalse(self.results)
        self.pending[1]((None, None), None)
        self.pending[2](True, None)
        self.assertEqual(len(self.results), 1)
        self.assertTrue(self.results[0][0])
        self.assertIn('New software', self.results[0][1])
        self.assertIn('Model weights update available', self.results[0][1])
        self.assertFalse(self.check.deadline.isActive())

    def test_deadline_retains_completed_result_ignores_late_response(self):
        self.pending[0]((True, 'New software'), None)
        self.check.finish()
        original = list(self.results)
        self.pending[1]((None, None), None)
        self.check.finish()
        self.assertEqual(self.results, original)
        self.assertEqual(original, [(True, 'New software')])

    def test_errors_do_not_advertise_updates(self):
        for done in self.pending:
            done(None, ConnectionError('offline'))
        self.assertEqual(self.results, [(False, '')])

    def test_start_is_idempotent(self):
        self.check.start()
        self.assertEqual(len(self.pending), 2)

    def test_dismissing_splash_ignores_late_workspace_changes_without_advertising_updates(self):
        from unittest.mock import patch
        import update_wizard
        self.check.abandon()
        with patch.object(update_wizard, 'apply_shared_settings') as apply:
            self.pending[1]((update_wizard.tempo_hub.token, {'settings': 'late response'}), None)
        apply.assert_not_called()
        self.assertEqual(self.results, [])
        self.assertFalse(self.check.deadline.isActive())


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        sys.path.insert(0, str(ROOT))
        raise SystemExit(quit_probe(sys.argv[2]))
    unittest.main()
