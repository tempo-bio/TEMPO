"""Native fixture acceptance must reject success, termination, or partial apply."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tests.fixtures.velopack.run_fixture import assert_running_app_refused


class ProcessRefusalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'current').mkdir()
        self.version = self.root / 'current' / 'fixture-version.txt'
        self.version.write_text('1.0.1')
        self.refused = SimpleNamespace(returncode=1, stdout='Package process is still running')

    def test_nonzero_exit_requires_live_app_and_unchanged_installation(self):
        assert_running_app_refused(self.refused, self.root, app_alive=True)
        with self.assertRaisesRegex(AssertionError, 'terminated'):
            assert_running_app_refused(self.refused, self.root, app_alive=False)
        with self.assertRaisesRegex(AssertionError, 'accepted'):
            assert_running_app_refused(SimpleNamespace(returncode=0, stdout='applied'),
                                       self.root, app_alive=True)

    def test_refusal_cannot_hide_partial_replacement(self):
        self.version.write_text('1.0.2')
        with self.assertRaisesRegex(AssertionError, 'changed'):
            assert_running_app_refused(self.refused, self.root, app_alive=True)
        self.version.write_text('1.0.1')
        for path in (self.root / 'tempo-previous', self.root / 'tempo-update-pending.txt'):
            with self.subTest(path=path.name):
                path.touch()
                with self.assertRaisesRegex(AssertionError, 'replacing'):
                    assert_running_app_refused(self.refused, self.root, app_alive=True)
                path.unlink()


if __name__ == '__main__':
    unittest.main()
