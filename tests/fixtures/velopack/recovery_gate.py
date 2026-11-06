"""Require native scenario evidence; this is not a production certificate."""
import json
from pathlib import Path
import sys

REQUIRED = {'normal', 'before_old_rename', 'after_old_rename', 'restore_failure',
            'abrupt_after_old', 'running_process', 'wait_timeout',
            'independent_repair', 'health_confirmation', 'two_successive_updates'}


def main(path):
    report = json.loads(Path(path).read_text(encoding='utf-8'))
    scenarios = report.get('scenarios', {})
    missing = sorted(name for name in REQUIRED if scenarios.get(name) is not True)
    if missing:
        raise SystemExit('Windows recovery gate incomplete: ' + ', '.join(missing))
    print('All native fixture scenarios passed; separate certification is still required.')


if __name__ == '__main__':
    main(sys.argv[1])
