"""Cloud monitoring failures with simulated time and no remote services."""
import contextlib
from datetime import datetime, timezone
from email.utils import format_datetime
import io
import threading
import types
import unittest
from unittest.mock import patch

from tempo_cloud_runner import sync_error_details, sync_retry_delay, watch_training_status


class ServerError(RuntimeError):
    def __init__(self, message, code=429, headers=None):
        super().__init__(message)
        self.response = types.SimpleNamespace(status_code=code, headers=headers or {})


class Clock:
    def __init__(self, end):
        self.now, self.end = 0, end

    def wait(self, interval):
        self.now += interval
        return self.is_set()

    def is_set(self):
        return self.now > self.end


class CloudStatusTests(unittest.TestCase):
    def test_details_include_http_code_and_message_and_redact_credentials(self):
        with patch.dict('os.environ', {'HF_TOKEN': 'private-test-token'}):
            message = sync_error_details(ServerError('Denied private-test-token hf_ABCDEFGHIJK', 403))
        self.assertIn('HTTP 403', message)
        self.assertIn('Denied', message)
        self.assertNotIn('private-test-token', message)
        self.assertNotIn('hf_ABCDEFGHIJK', message)
        self.assertIn('[redacted]', message)

    def test_retry_delay_handles_server_seconds_dates_invalid_values_and_bounds(self):
        self.assertEqual(sync_retry_delay(ConnectionError(), 1), 20)
        self.assertEqual(sync_retry_delay(ConnectionError(), 1000), 300)
        self.assertEqual(sync_retry_delay(ServerError('', headers={'Retry-After': '600'}), 2), 600)
        self.assertEqual(sync_retry_delay(ServerError('', headers={'RateLimit': '"api";r=0;t=240'}), 1), 240)
        self.assertEqual(sync_retry_delay(ServerError('', headers={'RateLimit': '"api";r=1;t=240'}), 1), 20)
        date = format_datetime(datetime.fromtimestamp(1120, timezone.utc), usegmt=True)
        with patch('tempo_cloud_runner.time.time', return_value=1000):
            self.assertEqual(sync_retry_delay(ServerError('', headers={'Retry-After': date}), 1), 120)
        for value in ('invalid', 'NaN', 'inf', '-5'):
            with self.subTest(value=value):
                self.assertEqual(sync_retry_delay(ServerError('', headers={'Retry-After': value}), 1), 20)
        self.assertEqual(sync_retry_delay(ServerError('You can retry this action in about 1 hour.'),1),3600)
        self.assertEqual(sync_retry_delay(ServerError('Retry in 12 minutes.'),1),720)
        self.assertEqual(sync_retry_delay(ServerError('Commit limit',headers={'Retry-After':'30'}),1,commit_limit=True),30)
        self.assertEqual(sync_retry_delay(ServerError('Commit limit'),1,commit_limit=True),3600)

    def test_status_write_backoff_does_not_skip_cancellation_and_recovers(self):
        clock = Clock(120)
        cancelled = threading.Event()
        reads, writes = [], []
        output = io.StringIO()
        def read():
            reads.append(clock.now)
            return {'cancel': clock.now >= 20}
        def write():
            writes.append(clock.now)
            if len(writes) == 1:
                raise ServerError('Too many writes', headers={'Retry-After': '90'})
        with patch('tempo_cloud_runner.time.monotonic', side_effect=lambda: clock.now), \
             contextlib.redirect_stdout(output):
            watch_training_status(read_control=read, upload_status=write, cancelled=cancelled, done=clock)
        self.assertEqual(reads, list(range(10, 121, 10)))
        self.assertEqual(writes, [10, 100])
        self.assertTrue(cancelled.is_set())
        self.assertIn('HTTP 429', output.getvalue())
        self.assertIn('Too many writes', output.getvalue())
        self.assertIn('Retrying in 90s', output.getvalue())
        self.assertIn('Status uploads resumed', output.getvalue())

    def test_control_backoff_does_not_block_status_and_reports_cancellation_limit(self):
        clock = Clock(50)
        reads, writes = [], []
        output = io.StringIO()
        def read():
            reads.append(clock.now)
            if len(reads) == 1:
                raise ServerError('Access denied', 403)
            return {}
        with patch('tempo_cloud_runner.time.monotonic', side_effect=lambda: clock.now), \
             contextlib.redirect_stdout(output):
            watch_training_status(read_control=read, upload_status=lambda: writes.append(clock.now),
                                  cancelled=threading.Event(), done=clock)
        self.assertEqual(reads, [10, 30, 40, 50])
        self.assertEqual(writes, [10])
        self.assertIn('Use Colab to stop execution', output.getvalue())
        self.assertIn('Cancellation checks resumed', output.getvalue())

    def test_persistent_outage_has_increasing_retry_spacing(self):
        clock = Clock(160)
        attempts = []
        def write():
            attempts.append(clock.now)
            raise ConnectionError('Network unavailable')
        with patch('tempo_cloud_runner.time.monotonic', side_effect=lambda: clock.now), \
             contextlib.redirect_stdout(io.StringIO()):
            watch_training_status(read_control=lambda: {}, upload_status=write,
                                  cancelled=threading.Event(), done=clock)
        self.assertEqual(attempts, [10, 30, 70, 150])

    def test_hour_of_progress_reserves_commit_capacity_and_keeps_cancellation_live(self):
        clock=Clock(3600);reads=[];writes=[]
        with patch('tempo_cloud_runner.time.monotonic',side_effect=lambda:clock.now):
            watch_training_status(read_control=lambda:reads.append(clock.now) or {},
                upload_status=lambda:writes.append(clock.now),cancelled=threading.Event(),done=clock)
        self.assertEqual(len(reads),360)
        self.assertEqual(len(writes),60)


if __name__ == '__main__':
    unittest.main()
