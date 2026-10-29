"""Bounded, revision-checked local annotation writes shared by GUI and checkout."""
import json
import copy
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

_locks = {}
_guard = threading.Lock()


class AnnotationSessionConflict(OSError):
    """A newer local session contains changes that cannot be merged safely."""


def _merge_session_annotations(base, proposed, current, normalize):
    missing = object()
    result = {}
    for name in sorted(set(base) | set(proposed) | set(current)):
        values = [records.get(name, missing) for records in (base, proposed, current)]
        before, local, saved = [normalize(name, value) if value is not missing else missing for value in values]
        if local == before:
            chosen = saved
        elif saved == before or local == saved:
            chosen = local
        else:
            raise AnnotationSessionConflict(
                'Annotations changed in another operation. Close other TEMPO windows, then choose '
                'Reload Saved Annotations. Your current edits remain in this window.')
        if chosen is not missing:
            result[name] = copy.deepcopy(chosen)
    return result


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def read_session(path):
    try:
        with open(path, encoding='utf-8') as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ValueError('Saved checkout session could not be read; its contents were preserved.') from exc
    if (not isinstance(value, dict) or not isinstance(value.get('annotations', {}), dict)
            or any(not isinstance(name, str) or not isinstance(record, dict)
                   or not isinstance(record.get('boxes', []), list)
                   for name, record in value.get('annotations', {}).items())
            or not isinstance(value.get('claimed_images', []), list)
            or any(not isinstance(name, str) or not name for name in value.get('claimed_images', []))
            or type(value.get('session_revision', 0)) is not int or value.get('session_revision', 0) < 0):
        raise ValueError('Saved checkout session is malformed; its contents were preserved.')
    return value


@contextmanager
def session_lock(path):
    """Do not block the GUI waiting for another process to finish saving."""
    key = str(Path(path).resolve())
    Path(key).parent.mkdir(parents=True, exist_ok=True)
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise BlockingIOError('Another operation is saving annotations. Retry after it finishes.')
    try:
        with open(key+'.lock', 'a+b') as stream:
            if os.name == 'nt':
                import msvcrt
                if not stream.tell():
                    stream.write(b'\0'); stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                if os.name == 'nt':
                    stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)
    finally:
        lock.release()


def update_session(path, transform, *, expected_revision=None, annotations_path=None,
                   base_session=None, normalize_record=None):
    """Commit a complete draft snapshot; restore earlier files on handled failure."""
    with session_lock(path):
        current = read_session(path)
        revision = current.get('session_revision', 0)
        stale = expected_revision is not None and expected_revision != revision
        if stale and base_session is None:
            raise AnnotationSessionConflict('Annotations changed in another operation. Reload the saved session before retrying; no files were overwritten.')
        updated = transform(copy.deepcopy(current) if stale else current)
        if stale:
            updated = dict(current, **updated)
            if (current.get('dataset_repo') not in (None, updated.get('dataset_repo'))
                    or current.get('collaborator_id') not in (None, base_session.get('collaborator_id'), updated.get('collaborator_id'))):
                raise AnnotationSessionConflict('The saved annotation session now belongs to another account or dataset. Close other TEMPO windows, then choose Reload Saved Annotations.')
            normalize = normalize_record or (lambda name, record: copy.deepcopy(record))
            updated['annotations'] = _merge_session_annotations(
                base_session.get('annotations', {}), updated.get('annotations', {}),
                current.get('annotations', {}), normalize)
            # Apply this window's claim changes without discarding independent
            # checkouts made after its baseline was loaded.
            before = set(base_session.get('claimed_images', []))
            proposed = set(updated.get('claimed_images', []))
            removed = before-proposed
            added = proposed-before
            updated['claimed_images'] = list(dict.fromkeys(
                [name for name in current.get('claimed_images', []) if name not in removed]
                + [name for name in updated.get('claimed_images', []) if name in added]))
        updated['session_revision'] = revision + 1
        files = {Path(path):json.dumps(updated, indent=2, allow_nan=False).encode('utf-8')}
        if annotations_path is not None:
            files = {Path(annotations_path):json.dumps(updated['annotations'], indent=2, allow_nan=False).encode('utf-8'), **files}
        previous = {target:target.read_bytes() if target.exists() else None for target in files}
        written = []
        try:
            for target, data in files.items():
                atomic_bytes(target, data)
                written.append(target)
        except BaseException as error:
            rollback_errors = []
            for target in reversed(written):
                try:
                    if previous[target] is None:
                        target.unlink()
                    else:
                        atomic_bytes(target, previous[target])
                except BaseException as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if rollback_errors:
                rescue = Path(path).parent/'annotation_recovery'/uuid.uuid4().hex
                for target, data in previous.items():
                    if data is not None:
                        atomic_bytes(rescue/target.name, data)
                raise OSError(f'Annotation save and rollback failed. Recovery copies: {rescue}. '
                              + '; '.join(rollback_errors)) from error
            raise
        return updated
