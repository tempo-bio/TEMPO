"""Processing contracts, cooperative cancellation, and durable queue transactions."""
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
import json
import os
from pathlib import Path
import tempfile
import threading

SCHEMA_VERSION = 4
TERMINAL_OUTCOMES = {'success', 'no_cells', 'no_beats'}

class ProcessingCancelled(Exception):
    pass

class CancellationToken:
    def __init__(self, path=None):
        self.path = path
        self.event = threading.Event()

    def cancel(self):
        self.event.set()
        if self.path:
            Path(self.path).touch()

    def is_cancelled(self):
        return self.event.is_set() or bool(self.path and Path(self.path).exists())

    def check(self):
        if self.is_cancelled():
            raise ProcessingCancelled('Processing cancelled at a safe boundary.')

@dataclass
class ProcessingResult:
    input_path: str
    output_dir: str = ''
    outcome: str = 'failure'
    completed_stages: list = field(default_factory=list)
    error: str = ''
    results_csv: str = ''
    schema_version: int = SCHEMA_VERSION

    @property
    def resolved(self):
        return self.outcome in TERMINAL_OUTCOMES

    def to_dict(self):
        return asdict(self)


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

_locks = {}
_locks_guard = threading.Lock()

@contextmanager
def file_lock(path, blocking=True):
    """A separate lock inode survives atomic replacement of the data file."""
    path = os.path.abspath(path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with _locks_guard:
        lock = _locks.setdefault(path, threading.RLock())
    if not lock.acquire(blocking=blocking):
        raise BlockingIOError('The entry is in use by another operation.')
    try:
        with open(path + '.lock', 'a+b') as stream:
            if os.name == 'nt':
                import msvcrt
                if not stream.tell():
                    stream.write(b'\0'); stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    if not blocking:
                        raise BlockingIOError('The entry is in use by another operation.') from exc
                    raise
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield
            finally:
                if os.name == 'nt':
                    stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)
    finally:
        lock.release()


def read_queue(path):
    if not Path(path).exists():
        return []
    value = json.loads(Path(path).read_text())
    if not isinstance(value, list) or any(not isinstance(p, str) or not p.strip() for p in value):
        raise ValueError('Expected a list of nonempty video paths.')
    return value


def edit_queue(path, edit):
    with file_lock(path):
        before = read_queue(path)
        after = edit(list(before))
        if after != before:
            atomic_write(path, after)
        return after


def readable_recording(path):
    return Path(path).is_file() and os.access(path, os.R_OK)


def read_result_history(path):
    if not Path(path).exists():
        return []
    document = json.loads(Path(path).read_text())
    if not isinstance(document, dict) or not isinstance(document.get('results'), list):
        raise ValueError('Processing history is malformed. Existing history will be preserved.')
    return [ProcessingResult(**record) for record in document['results']]


def record_result(path, result):
    with file_lock(path):
        results = read_result_history(path)
        results = [previous for previous in results if not (
            previous.input_path == result.input_path and previous.output_dir == result.output_dir)]
        results.append(result)
        atomic_write(path, {'schema_version': SCHEMA_VERSION,
                            'results': [previous.to_dict() for previous in results]})
