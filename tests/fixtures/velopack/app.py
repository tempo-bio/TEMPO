"""Small frozen Velopack lifecycle fixture; contains no TEMPO user data."""
import os
from contextlib import nullcontext
from pathlib import Path
import subprocess
import sys
import time

if any(name in os.environ for name in ('TEMPO_INSTALLATION_LOCK_HANDLE', 'TEMPO_APPLICATION_LOCK_HANDLE')):
    raise RuntimeError('Native lease environment leaked into application or lifecycle hook')

# Test-only hook: leave an identifiable helper running after the hook exits.
# This exercises cleanup separately from the pre-hook running-app check.
if '--veloapp-obsolete' in sys.argv and os.environ.get('TEMPO_FIXTURE_HOOK_CHILD'):
    child = subprocess.Popen([sys.executable, '--unleased-hold'], close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    Path(os.environ['TEMPO_FIXTURE_CHILD_PID']).write_text(str(child.pid), encoding='utf-8')
    ready = Path(os.environ['TEMPO_FIXTURE_READY'])
    deadline = time.monotonic() + 10
    while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    if not ready.exists():
        raise RuntimeError('Uncooperative hook helper did not signal readiness')

import velopack
from tempo_update_lock import application_lock, installation_lock, update_lock
lifecycle = any(arg.startswith('--veloapp-') for arg in sys.argv[1:])
if lifecycle:
    velopack.App().set_auto_apply_on_startup(False).run()
elif '--unleased-hold' not in sys.argv:
    with application_lock(Path(os.environ['LOCALAPPDATA']) / 'TEMPO'), \
            installation_lock(Path(sys.executable).parent.parent), \
            update_lock(Path(sys.executable).parent.parent / 'packages'):
        velopack.App().set_auto_apply_on_startup(False).run()

# Model the desktop's shared lease for the complete application lifetime.
with (nullcontext() if '--unleased-hold' in sys.argv else
      application_lock(Path(os.environ['LOCALAPPDATA']) / 'TEMPO', shared=True)):
    version = (Path(sys.executable).parent / 'fixture-version.txt').read_text(encoding='utf-8').strip()
    if '--hold' in sys.argv or '--unleased-hold' in sys.argv:
        Path(os.environ['TEMPO_FIXTURE_READY']).write_text('ready', encoding='utf-8')
        stop = Path(os.environ['TEMPO_FIXTURE_STOP'])
        deadline = time.monotonic() + 120
        while not stop.exists() and time.monotonic() < deadline:
            time.sleep(.1)
    elif '--probe' in sys.argv:
        print(version, flush=True)
    elif os.environ.get('TEMPO_FIXTURE_LAUNCH'):
        Path(os.environ['TEMPO_FIXTURE_LAUNCH']).write_text(version, encoding='utf-8')
