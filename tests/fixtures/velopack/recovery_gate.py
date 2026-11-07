"""Require native scenario evidence; this is not a production certificate."""
import json
from pathlib import Path
import sys

REQUIRED = {'normal', 'guarded_handoff', 'before_old_rename', 'after_old_rename', 'restore_failure',
            'abrupt_after_old', 'running_process', 'hook_cleanup', 'legacy_writer_guard', 'wait_timeout',
            'independent_repair', 'offline_forward', 'offline_previous',
            'health_confirmation', 'two_successive_updates'}
REQUIRED |= {'uninstall_running_process', 'uninstall_rename_blocked', 'uninstall_reinstall', 'repaired_owner_next_update'}
REQUIRED |= {'installation_lock', 'application_lock', 'setup_repair', 'setup_running_process', 'setup_pending_refused', 'damaged_owner'}
REQUIRED.add('inherited_lock_identity')
PRODUCTION_REQUIRED = REQUIRED - {'before_old_rename', 'after_old_rename', 'restore_failure', 'abrupt_after_old',
    'independent_repair', 'offline_previous', 'offline_forward'}


def main(path, *, production=False):
    report = json.loads(Path(path).read_text(encoding='utf-8'))
    if report.get('diagnostic_only'):
        raise SystemExit('Handle diagnostics change timing and cannot satisfy native acceptance.')
    if report.get('error'):
        raise SystemExit('Windows recovery fixture reported an error.')
    if report.get('production') is not production:
        raise SystemExit('Native report has the wrong production/fixture mode.')
    required = PRODUCTION_REQUIRED if production else REQUIRED
    scenarios = report.get('scenarios', {})
    missing = sorted(name for name in required if scenarios.get(name) is not True)
    if missing:
        raise SystemExit('Windows recovery gate incomplete: ' + ', '.join(missing))
    print('All native fixture scenarios passed; separate certification is still required.')


if __name__ == '__main__':
    main(sys.argv[1], production='--production' in sys.argv[2:])
