"""Measured model progress shared by isolated cloud workers (no ML imports)."""
import json
import os
import tempfile
import time
from pathlib import Path


def report(model, phase, epoch=None, total=None):
    directory = os.environ.get('TEMPO_PROGRESS_DIR')
    if not directory:
        return
    path = Path(directory); path.mkdir(parents=True, exist_ok=True)
    value = dict(model=model, phase=phase, epoch=epoch, total=total, updated_at=time.time())
    fd, temporary = tempfile.mkstemp(dir=path)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
        os.replace(temporary, path/(model+'.json'))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def snapshots(directory):
    result = {}
    for path in Path(directory).glob('*.json'):
        try:
            result[path.stem] = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
    return result
