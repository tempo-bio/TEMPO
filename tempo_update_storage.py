"""Stable packaged collaboration storage with an explicit publication point.

Callers must establish that no other TEMPO process or worker can write any
involved store while preparing migration. The migration lock serializes this
operation only; it is not a lock on existing collaboration writers.
"""
import hashlib
import json
import os
from pathlib import Path
import stat

from tempo_annotation_store import session_lock
from tempo_update_migration import (_atomic_json, _hash, _safe, inventory_legacy,
                                    migrate_collaboration)

_POINTER_FORMAT = 'tempo-collaboration-pointer-v1'
_POINTER_NAME = 'collaboration-location.json'


def _pointer(profile_root):
    return _safe(Path(profile_root) / 'config' / _POINTER_NAME)


def _read_pointer(profile_root):
    path = _pointer(profile_root)
    if not path.exists():
        return None
    try:
        if not path.is_file() or path.stat().st_size > 65536:
            raise ValueError('Invalid collaboration storage pointer.')
        value = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or set(value) != {'format', 'target', 'sources'}
                or value['format'] != _POINTER_FORMAT or value['target'] != 'collaboration'
                or not isinstance(value['sources'], list)
                or not all(isinstance(item, str) and item and os.path.isabs(item)
                           for item in value['sources'])):
            raise ValueError('Invalid collaboration storage pointer.')
        target = _safe(Path(profile_root) / 'collaboration')
        if not target.is_dir():
            raise ValueError('Published collaboration storage is unavailable.')
        return value
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('Invalid collaboration storage pointer.') from error


def collaboration_base(bundle_root, profile_root):
    """Select the published stable root, or the legacy root before publication."""
    if _read_pointer(profile_root) is None:
        return Path(bundle_root) / 'datasets'
    return Path(profile_root) / 'collaboration'


def _inventory_store(root, cancel):
    """Compare complete directory contents before merging multiple stores."""
    root = _safe(root, missing_ok=False)
    if not root.is_dir():
        raise ValueError('Collaboration storage is unavailable.')
    entries = {}
    for directory, dirs, files in os.walk(root, followlinks=False,
                                           onerror=lambda error: (_ for _ in ()).throw(error)):
        dirs.sort(); files.sort()
        for name in dirs + files:
            if cancel and cancel():
                return None
            path = _safe(Path(directory) / name, missing_ok=False)
            mode = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(mode):
                entries[relative] = ('directory',)
            elif stat.S_ISREG(mode):
                entries[relative] = ('file', path.stat().st_size, _hash(path))
            else:
                raise ValueError(f'Unsupported dataset entry: {path}')
    return entries


def prepare_packaged_storage(bundle_root, profile_root, legacy_roots=(), *, cancel=None):
    """Inventory and migrate legacy stores, then atomically publish selection.

    Returns status complete, conflict, or cancelled with inventories and migration
    details. Original stores and model fallbacks are never removed or modified.
    Caller must exclude concurrent writers throughout the operation.
    """
    bundle = _safe(bundle_root)
    profile = _safe(profile_root)
    lock_path = profile / 'updates' / 'migration'
    _safe(lock_path.parent).mkdir(parents=True, exist_ok=True)
    with session_lock(lock_path):
        existing = _read_pointer(profile)
        if existing is not None:
            return {'status': 'complete', 'pointer': existing, 'already_published': True,
                    'inventories': [], 'migrations': []}
        roots = []
        for root in (bundle, *legacy_roots):
            root = _safe(root)
            if root not in roots:
                roots.append(root)
        inventories = [dict(root=str(root), **inventory_legacy(root)) for root in roots]
        result = {'status': 'in_progress', 'inventories': inventories, 'migrations': [],
                  'conflicts': []}
        addons = [addon for inventory in inventories for addon in inventory['addons']]
        if addons:
            result.update(status='conflict', conflicts=[{'kind': 'addon_requires_review', **addon}
                                                         for addon in addons])
            return result
        stores = [Path(item['datasets']) for item in inventories if item['datasets'] is not None]
        fingerprints = []
        for source in stores:
            fingerprint = _inventory_store(source, cancel)
            if fingerprint is None:
                result['status'] = 'cancelled'
                return result
            fingerprints.append(fingerprint)
        if fingerprints and any(value != fingerprints[0] for value in fingerprints[1:]):
            result.update(status='conflict', conflicts=[{'kind': 'different_stores',
                                                         'sources': [str(p) for p in stores]}])
            return result
        destination = _safe(profile / 'collaboration')
        journal_root = _safe(profile / 'updates' / 'migration-receipts')
        for source in stores:
            key = hashlib.sha256(os.fsencode(str(source))).hexdigest()
            migration = migrate_collaboration(source, destination,
                receipt_path=journal_root / (key + '.json'), cancel=cancel)
            result['migrations'].append(migration)
            if migration['status'] != 'complete':
                result.update(status=migration['status'], conflicts=migration['conflicts'])
                return result
        if cancel and cancel():
            result['status'] = 'cancelled'
            return result
        destination.mkdir(parents=True, exist_ok=True)
        final_inventory = _inventory_store(destination, cancel)
        if final_inventory is None:
            result['status'] = 'cancelled'
            return result
        for source, original in zip(stores, fingerprints):
            latest = _inventory_store(source, cancel)
            if latest is None:
                result['status'] = 'cancelled'
                return result
            if latest != original:
                result.update(status='conflict', conflicts=[{'kind': 'source_changed',
                                                             'source': str(source)}])
                return result
        if final_inventory != (fingerprints[0] if fingerprints else {}):
            result.update(status='conflict', conflicts=[{'kind': 'destination_differs',
                                                         'destination': str(destination)}])
            return result
        pointer = {'format': _POINTER_FORMAT, 'target': 'collaboration',
                   'sources': [str(path) for path in stores]}
        _atomic_json(_pointer(profile), pointer)
        result.update(status='complete', pointer=pointer, already_published=False)
        return result
