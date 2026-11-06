"""Small frozen Velopack lifecycle fixture; contains no TEMPO user data."""
import velopack
velopack.App().set_auto_apply_on_startup(False).run()

import os
from pathlib import Path
import sys
import time
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
