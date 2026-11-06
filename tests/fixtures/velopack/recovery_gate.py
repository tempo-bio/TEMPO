"""Require native scenario evidence; this is not a production certificate."""
import json
from pathlib import Path
import sys

REQUIRED = {'normal', 'guarded_handoff', 'before_old_rename', 'after_old_rename', 'restore_failure',
            'abrupt_after_old', 'running_process', 'hook_cleanup', 'legacy_writer_guard', 'wait_timeout',
            'independent_repair', 'offline_forward', 'offline_previous',
            'health_confirmation', 'two_successive_updates'}
REQUIRED |= {'uninstall_running_process', 'uninstall_rename_blocked', 'uninstall_reinstall', 'repaired_owner_next_update'}


def main(path):
    report = json.loads(Path(path).read_text(encoding='utf-8'))
    if report.get('error'):
        raise SystemExit('Windows recovery fixture reported an error.')
    scenarios = report.get('scenarios', {})
    missing = sorted(name for name in REQUIRED if scenarios.get(name) is not True)
    if missing:
        raise SystemExit('Windows recovery gate incomplete: ' + ', '.join(missing))
    print('All native fixture scenarios passed; separate certification is still required.')


if __name__ == '__main__':
    main(sys.argv[1])
