"""Non-destructive foundations for migrating legacy packaged user data.

The caller must prevent concurrent writers and select the new data root only
when the result is complete. This module never removes a legacy installation.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

_MODEL_NAMES = ('yolo26n-scout.pt', 'stardist_nuclei.zip', 'cellpose4_cardio.pt',
                'yolo26s-seg.pt', 'stardist_cardio.zip')
_FORMAT = 'tempo-collaboration-migration-v1'


def _safe(path, *, missing_ok=True):
    """Reject links and Windows reparse points in existing path components."""
    path = Path(os.path.abspath(os.fspath(path)))
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing_ok:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'Link or reparse point is not allowed: {part}')
    return path


def _hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path, payload):
    _safe(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe(path)
    fd, tmp = tempfile.mkstemp(prefix='.tempo-migration-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _raise_walk(error):
    raise error


def discover_addons(install_root):
    """Report add-on receipt files for manual compatibility assessment.

    Receipt contents are intentionally not trusted or interpreted as permission
    to remove a package. Presence always requires an explicit assessment.
    """
    root = _safe(install_root) / '.tempo-packages'
    if not root.exists():
        return []
    _safe(root, missing_ok=False)
    if not root.is_dir():
        raise ValueError(f'Invalid add-on directory: {root}')
    result = []
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=_raise_walk):
        for name in dirs + files:
            _safe(Path(directory) / name, missing_ok=False)
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.lower() == '.json':
                result.append({'path': str(path), 'compatibility': 'requires_review'})
    # An unrecognized or partial package directory must not be treated as absent.
    if not result:
        # The published overlay remover deletes its own child, leaving this
        # empty parent. Only exact restoration of its reviewed before-files
        # distinguishes that state from an unknown partial installation.
        if not any(root.iterdir()):
            from tempo_update_addons import reviewed_base_restored
            if reviewed_base_restored(_safe(install_root)):
                return []
        result.append({'path': str(root), 'compatibility': 'requires_review'})
    return result


def inventory_legacy(install_root):
    """Locate legacy datasets, local model fallbacks, and add-on evidence."""
    root = _safe(install_root)
    layouts = (root, _safe(root / '_internal'))
    stores, models, addons = [], [], []
    for layout in layouts:
        datasets = _safe(layout / 'datasets')
        if datasets.is_dir():
            stores.append(str(datasets))
        for name in _MODEL_NAMES:
            path = _safe(layout / name)
            if path.is_file():
                models.append(str(path))
        addons.extend(discover_addons(layout))
    return {'datasets': stores[0] if stores else None, 'dataset_roots': stores,
            'models': models, 'addons': addons}


def migrate_collaboration(legacy_root, destination_root, *, receipt_path=None, cancel=None):
    """Copy dataset contents with verified, non-overwriting publication.

    Returns status complete, conflict, or cancelled and a per-file inventory.
    A receipt, if supplied, is an atomic progress journal, not a lock. Reruns
    verify all files rather than trusting it. The caller must hold a shared
    application/migration lock and prevent all writes to both roots throughout.
    Existing links, special files and reparse points cause an error.
    """
    source = _safe(legacy_root, missing_ok=False)
    target = _safe(destination_root)
    if not source.is_dir():
        raise ValueError('Legacy datasets must be a directory')
    if source == target or source in target.parents or target in source.parents:
        raise ValueError('Legacy and destination roots must not overlap')
    receipt = _safe(receipt_path) if receipt_path is not None else None
    if receipt and (receipt == source or source in receipt.parents or receipt == target or target in receipt.parents):
        raise ValueError('Receipt must be outside both dataset roots')
    if receipt and receipt.exists():
        with open(receipt, encoding='utf-8') as stream:
            prior = json.load(stream)
        if (not isinstance(prior, dict) or prior.get('format') != _FORMAT or
                prior.get('source') != str(source) or prior.get('destination') != str(target)):
            raise ValueError('Receipt belongs to another migration or is invalid')
    result = {'format': _FORMAT, 'source': str(source), 'destination': str(target),
              'status': 'in_progress', 'files': {}, 'conflicts': []}
    def save():
        if receipt:
            _atomic_json(receipt, result)
    def cancelled():
        return bool(cancel and cancel())
    save()
    target.mkdir(parents=True, exist_ok=True)
    for directory, dirs, files in os.walk(source, followlinks=False, onerror=_raise_walk):
        dirs.sort(); files.sort()
        relative = Path(directory).relative_to(source)
        for name in dirs + files:
            item = _safe(Path(directory) / name, missing_ok=False)
            mode = item.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError(f'Unsupported dataset entry: {item}')
        dest_dir = _safe(target / relative)
        if dest_dir.exists() and not dest_dir.is_dir():
            result['conflicts'].append(relative.as_posix())
            dirs[:] = []
            save(); continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            if cancelled():
                result['status'] = 'cancelled'; save(); return result
            original = _safe(Path(directory) / name, missing_ok=False)
            dest = _safe(dest_dir / name)
            rel = (relative / name).as_posix()
            expected = _hash(original)
            if dest.exists():
                if not dest.is_file() or _hash(dest) != expected:
                    result['conflicts'].append(rel)
                else:
                    result['files'][rel] = expected
                save(); continue
            fd, temp = tempfile.mkstemp(prefix='.tempo-copy-', dir=dest_dir)
            try:
                with os.fdopen(fd, 'wb') as out, open(original, 'rb') as inp:
                    for chunk in iter(lambda: inp.read(1024 * 1024), b''):
                        if cancelled():
                            result['status'] = 'cancelled'; save(); return result
                        out.write(chunk)
                    out.flush(); os.fsync(out.fileno())
                if _hash(temp) != expected or _hash(original) != expected:
                    raise OSError(f'Legacy file changed or copy verification failed: {original}')
                _safe(dest)
                try:
                    os.link(temp, dest)  # atomic create; never replace another writer's file
                except FileExistsError:
                    _safe(dest, missing_ok=False)
                    if not dest.is_file() or _hash(dest) != expected:
                        result['conflicts'].append(rel); save(); continue
                if _hash(dest) != expected:
                    raise OSError(f'Published file failed verification: {dest}')
                result['files'][rel] = expected
                save()
            finally:
                os.unlink(temp)
    result['status'] = 'conflict' if result['conflicts'] else 'complete'
    save()
    return result
