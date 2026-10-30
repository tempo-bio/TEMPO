"""Measured Hub bytes, truthful GUI state, and delivery on the GUI thread."""
import io
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
import unittest
from unittest.mock import patch

from tests.hub_http_fixture import httpx
from huggingface_hub.file_download import http_get
from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication
from tempo_model_transfer import ModelTransferProgress, ModelTransferReporter
from tempo_model_progress import ModelProgressWidget
from tempo_tasks import run_task
from tests.qt_lifecycle import close_and_dispose

APP = QApplication.instance() or QApplication([])


class TransferContractTests(unittest.TestCase):
    def test_real_xet_reporter_supports_aggregated_progress_without_console_tqdm(self):
        from types import SimpleNamespace
        from huggingface_hub.utils import _xet_progress_reporting as xet
        reporter_type = getattr(xet, 'XetDownloadProgressReporter', None)
        if reporter_type is None:
            self.skipTest('Hub 2 Xet reporter is unavailable in this older local SDK')
        events = []
        progress = ModelTransferReporter(events.append, ['fixture.pt']).download_class('fixture.pt')
        with patch('tqdm.std.tqdm.__new__', side_effect=AssertionError('Console progress constructed')):
            with reporter_type(reconstruction_desc='fixture', transfer_desc='fixture', total=100,
                               log_level=20, tqdm_class=progress) as report:
                report.update_progress(SimpleNamespace(total_bytes_completed=60,
                    total_transfer_bytes_completed=30, total_bytes_completion_rate=60,
                    total_transfer_bytes_completion_rate=30, total_bytes=100))
                self.assertIs(report.transfer_bar, report.reconstruction_bar)
        self.assertEqual(events[-1].received, 60)
        self.assertEqual(events[-1].transferred, 30)
        self.assertEqual(events[-1].total, 100)

    def test_real_hub_http_transfer_reports_bytes_without_constructing_tqdm(self):
        data = b'model checkpoint bytes'
        for known_size in (True, False):
            with self.subTest(known_size=known_size):
                events = []
                reporter = ModelTransferReporter(events.append, ['yolo26n-scout.pt'])
                def respond(request):
                    response = httpx.Response(200, content=data)
                    if not known_size:
                        del response.headers['content-length']
                    return response
                with httpx.Client(transport=httpx.MockTransport(respond)) as client, \
                     patch('huggingface_hub.utils._http.get_session', return_value=client), \
                     patch('tqdm.std.tqdm.__new__', side_effect=AssertionError('Console progress was constructed')):
                    output = io.BytesIO()
                    http_get('https://fixture.invalid/model', output,
                             tqdm_class=reporter.download_class('yolo26n-scout.pt'))
                self.assertEqual(output.getvalue(), data)
                self.assertEqual(events[-1].received, len(data))
                self.assertEqual(events[-1].total, len(data) if known_size else None)
                self.assertTrue(all(event.stage == 'downloading' for event in events))


class ProgressWidgetTests(unittest.TestCase):
    def view(self):
        view = ModelProgressWidget()
        self.addCleanup(close_and_dispose, view)
        return view

    def test_measured_bytes_and_wait_state_do_not_claim_success(self):
        with patch('tempo_model_progress.time.monotonic', return_value=100):
            view = self.view()
            view.update_progress(ModelTransferProgress('downloading', 'stardist_nuclei.zip', 2, 3, 48_000_000, 160_000_000))
        with patch('tempo_model_progress.time.monotonic', return_value=115):
            view.refresh_time()
        self.assertEqual(view.bar.value(), 300)
        self.assertIn('48.0 MB of 160.0 MB', view.transfer.text())
        self.assertIn('Waiting for data', view.elapsed.text())
        self.assertEqual(view.rows['stardist_nuclei.zip'].text(), 'Downloading')
        view.update_progress(ModelTransferProgress('verifying', 'stardist_nuclei.zip'))
        self.assertEqual(view.rows['stardist_nuclei.zip'].text(), 'Verifying')
        self.assertEqual(view.bar.maximum(), 0)
        self.assertNotIn('Installed', [row.text() for row in view.rows.values()])

    def test_unknown_length_and_failure_never_show_fabricated_completion(self):
        view = self.view()
        view.update_progress(ModelTransferProgress('downloading', 'yolo26n-scout.pt', 1, 3, 1200))
        self.assertEqual(view.bar.maximum(), 0)
        self.assertIn('1.2 KB', view.transfer.text())
        self.assertNotIn(' of ', view.transfer.text())
        view.update_progress(ModelTransferProgress('failed'))
        self.assertFalse(view.timer.isActive())
        self.assertTrue(view.bar.isHidden())
        self.assertTrue(all(row.text() == 'Check again' for row in view.rows.values()))

    def test_worker_progress_is_delivered_on_gui_thread_before_task_returns(self):
        threads, seen = [], []
        class ObservedProgress(ModelProgressWidget):
            def update_progress(self, event):
                threads.append(QThread.currentThread() == APP.thread())
                seen.append(event.stage)
                super().update_progress(event)
        view = ObservedProgress()
        def action(report):
            self.assertNotEqual(threading.current_thread(), threading.main_thread())
            report(ModelTransferProgress('downloading', 'yolo26n-scout.pt', 1, 3, 3, 4))
            report(ModelTransferProgress('complete'))
            return 7
        self.assertEqual(run_task(None, 'Install models', action, progress_widget=view), 7)
        self.assertEqual(seen, ['downloading', 'complete'])
        self.assertTrue(all(threads))
