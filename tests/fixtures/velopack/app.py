"""Small frozen Velopack lifecycle fixture; contains no TEMPO user data."""
import os
from pathlib import Path
import subprocess
import sys
import time

# Test-only hook: leave an identifiable helper running after the hook exits.
# This exercises cleanup separately from the pre-hook running-app check.
if '--veloapp-obsolete' in sys.argv and os.environ.get('TEMPO_FIXTURE_HOOK_CHILD'):
    child = subprocess.Popen([sys.executable, '--hold'], close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    Path(os.environ['TEMPO_FIXTURE_CHILD_PID']).write_text(str(child.pid), encoding='utf-8')
    ready = Path(os.environ['TEMPO_FIXTURE_READY'])
    deadline = time.monotonic() + 10
    while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    if not ready.exists():
        raise RuntimeError('Hook helper did not acquire its application lease')

import velopack
velopack.App().set_auto_apply_on_startup(False).run()

from tempo_update_lock import application_lock

# Model the desktop's shared lease for the complete application lifetime.
with application_lock(Path(os.environ['LOCALAPPDATA']) / 'TEMPO', shared=True):
    version = (Path(sys.executable).parent / 'fixture-version.txt').read_text(encoding='utf-8').strip()
    if '--hold' in sys.argv:
        Path(os.environ['TEMPO_FIXTURE_READY']).write_text('ready', encoding='utf-8')
        stop = Path(os.environ['TEMPO_FIXTURE_STOP'])
        deadline = time.monotonic() + 120
        while not stop.exists() and time.monotonic() < deadline:
            time.sleep(.1)
    elif '--probe' in sys.argv:
        print(version, flush=True)
    elif os.environ.get('TEMPO_FIXTURE_LAUNCH'):
        Path(os.environ['TEMPO_FIXTURE_LAUNCH']).write_text(version, encoding='utf-8')
