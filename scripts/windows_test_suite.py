"""Discover the full suite and reject unreviewed Windows skips."""
import argparse
import faulthandler
import json
from pathlib import Path
import sys
import unittest
from scripts.windows_packager import start_stack_sampler

# Only reviewed platform exclusions are allowed; every other skip fails the gate.
WINDOWS_SKIPS = {
    'test_native_signal_names_the_failed_stage': 'POSIX signal test',
    'test_rosetta_bridge_returns_native_output_identity': 'Rosetta bridge is macOS-specific',
    'test_rosetta_cancellation_reaches_native_child': 'Native bridge is macOS-specific',
    'test_desktop_requirements_check_does_not_install_or_probe_nvidia': 'Checks the non-Windows requirements path',
    'test_missing_desktop_package_fails_requirements_check': 'Checks the non-Windows requirements path',
    'test_dangling_private_link_reports_failure_without_migration_or_recursion': 'Creating Windows symlinks may require administrator privileges.',
    'test_actual_sketch_watchdog_and_output_state_machine': 'Host C++ compiler unavailable; Arduino board compile remains a separate check.',
}


class ImmediateFailures(unittest.TextTestResult):
    """Emit a failure's traceback before later tests can stall or crash."""
    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.stream.writeln('\nFAILURE DETAILS: '+test.id())
        self.stream.writeln(self._exc_info_to_string(error, test))

    def addError(self, test, error):
        super().addError(test, error)
        self.stream.writeln('\nERROR DETAILS: '+test.id())
        self.stream.writeln(self._exc_info_to_string(error, test))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    faulthandler.enable()
    stopped,sampler = start_stack_sampler(label='Test suite')
    try:
        if sys.platform == 'win32':
            from PyQt6.QtWidgets import QApplication
            from tempo_ui import prepare_offscreen_fonts
            application = QApplication.instance() or QApplication([])
            fonts = prepare_offscreen_fonts(application)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.with_name('test-fonts.json').write_text(json.dumps(fonts, indent=2)+'\n', encoding='utf-8')
        suite = unittest.defaultTestLoader.discover('tests')
        result = unittest.TextTestRunner(verbosity=2, resultclass=ImmediateFailures).run(suite)
    finally:
        stopped.set()
        sampler.join(timeout=5)
    skipped = [dict(test=test.id(), reason=reason) for test, reason in result.skipped]
    unexpected = [s for s in skipped if WINDOWS_SKIPS.get(s['test'].rsplit('.', 1)[-1]) != s['reason']] if sys.platform == 'win32' else []
    data = dict(tests=result.testsRun, failures=[t.id() for t, _ in result.failures],
                errors=[t.id() for t, _ in result.errors], skipped=skipped, unexpected_skips=unexpected,
                success=result.wasSuccessful() and not unexpected and result.testsRun > 0)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2)+'\n', encoding='utf-8')
    if unexpected: print('Unreviewed Windows skips:', unexpected, flush=True)
    return 0 if data['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
