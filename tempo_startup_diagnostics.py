"""Opt-in startup stages and Python locations, without credentials or payloads."""
import atexit
import _thread
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import threading
import time

_recorder = None


def progress_locks():
    """Inspect already-created locks without acquiring or constructing one."""
    def describe(lock):
        result = dict(kind=type(lock).__name__, lock_id=id(lock))
        if isinstance(lock, _thread.RLock):
            # Built-in RLock repr contains only lock state, owner ID and count.
            state = re.search(r'owner=(\d+) count=(\d+)', repr(lock))
            if state:
                result.update(owner_thread_id=int(state[1]), count=int(state[2]))
        elif type(lock).__module__ == 'tqdm.std' and type(lock).__name__ == 'TqdmDefaultWriteLock':
            result['components'] = [describe(item) for item in vars(lock).get('locks', [])[:2]]
        return result
    results = []
    for name in ('tqdm.std', 'tqdm.auto', 'tqdm.asyncio', 'huggingface_hub.utils.tqdm'):
        module = sys.modules.get(name)
        cls = vars(module).get('tqdm') if module is not None else None
        if not isinstance(cls, type):
            continue
        for base in cls.__mro__:
            lock = vars(base).get('_lock')
            if lock is not None:
                results.append(dict(module=name, lock=describe(lock)))
                break
    return results


class StartupRecorder:
    def __init__(self, path, interval=10):
        self.path = Path(path)
        self.started = time.monotonic()
        self.events = []
        self.snapshots = []
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.interval = interval
        self.thread = None

    def record(self, stage, *, status=None, count=None, error=None):
        # Callers pass static stage names and numeric metadata, never values,
        # request URLs, headers, response bodies, or exception messages.
        entry = dict(stage=stage, elapsed_s=round(time.monotonic()-self.started, 3))
        if status is not None:
            entry['http_status'] = int(status)
        if count is not None:
            entry['count'] = int(count)
        if error is not None:
            entry['error_type'] = type(error).__name__
        with self.lock:
            self.events.append(entry)
            self.flush()

    def snapshot(self):
        locations = []
        for ident, frame in sys._current_frames().items():
            stack = []
            while frame is not None and len(stack) < 35:
                stack.append(dict(file=os.path.basename(frame.f_code.co_filename),
                                  function=frame.f_code.co_name, line=frame.f_lineno))
                frame = frame.f_back
            locations.append(dict(thread_id=ident, stack=stack))
        with self.lock:
            self.snapshots.append(dict(elapsed_s=round(time.monotonic()-self.started, 3),
                                       threads=locations, progress_locks=progress_locks()))
            self.flush()

    def flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='tempo-startup.', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                json.dump(dict(schema_version=1, events=self.events, snapshots=self.snapshots), output)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def watch(self):
        # Bound diagnostic work even when startup never reaches the dashboard.
        for _ in range(60):
            if self.stopped.wait(self.interval):
                return
            try:
                self.snapshot()
            except OSError:
                return

    def start(self):
        self.record('process.started')
        self.thread = threading.Thread(target=self.watch, name='startup-diagnostics', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()


def start_from_arguments(arguments):
    global _recorder
    if len(arguments) == 3 and arguments[1] == '--diagnose-startup':
        _recorder = StartupRecorder(arguments[2])
        _recorder.start()
        atexit.register(_recorder.stop)
        del arguments[1:]


def record(stage, **metadata):
    if _recorder is not None:
        try:
            _recorder.record(stage, **metadata)
        except OSError:
            # An unavailable report destination cannot change application work.
            pass


def finish():
    if _recorder is not None:
        _recorder.stop()
