"""Small frozen Velopack lifecycle fixture; contains no TEMPO user data."""
import velopack
velopack.App().set_auto_apply_on_startup(False).run()

import os
from pathlib import Path
import sys
import time

if '--hold' in sys.argv:
    Path(os.environ['TEMPO_FIXTURE_READY']).write_text('ready', encoding='utf-8')
    time.sleep(120)
elif '--probe' in sys.argv:
    print((Path(sys.executable).parent / 'fixture-version.txt').read_text(encoding='utf-8').strip(), flush=True)
