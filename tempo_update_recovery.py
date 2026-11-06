"""Offline, explicit recovery of an armed Windows update transaction.

The caller must ensure all application and helper processes have exited before
calling these functions. Recovery never starts an executable or modifies user
stores. A packaged companion must embed a nonempty trust store before release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

from tempo_update_lock import update_lock
from tempo_update_manifest import (FileRecord, load_verified_manifest, verify_directory,
                                   verify_zip, validate_path)
from tempo_update_migration import _safe

# Populated by the separately certified companion build, never from CLI input.
EMBEDDED_TRUSTED_KEYS = {}
_FORMAT = 'tempo-update-recovery-v1'
_PREFIX = 'lib/app/'


def _digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _atomic(path, data):
    path = _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.tempo-repair-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_write(path, obj):
    _atomic(path, json.dumps(obj, sort_keys=True, indent=2).encode('utf-8'))


def _validated_release(data, trusted_keys, channel, *, offline=False):
    # Previously armed, signed packages remain usable for offline repair after
    # feed expiry; this bypass is never used to arm a fresh transaction.
    now = datetime(1970, 1, 1, tzinfo=timezone.utc) if offline else datetime.now(timezone.utc)
    return load_verified_manifest(data, trusted_keys, now=now, min_sequence=0, expected_channel=channel)


def _schema(release):
    value = json.loads(release.signed_payload).get('storage_schema_version', 1)
    if type(value) is not int or value < 1:
        raise ValueError('Invalid signed storage schema version.')
    return value


def _application_inventory(release):
    entries = [FileRecord(item.path[len(_PREFIX):], item.size, item.sha256)
               for item in release.inventory if item.path.startswith(_PREFIX)]
    if not entries:
        raise ValueError('Signed inventory contains no application payload.')
    for item in entries:
        validate_path(item.path)
    return tuple(entries)


def _verify_app(root, release):
    root = _safe(root, missing_ok=False)
    if not root.is_dir():
        raise ValueError('Application directory is missing.')
    verify_directory(root, _application_inventory(release))


def _store_copy(source, destination):
    source = _safe(source, missing_ok=False)
    destination = _safe(destination)
    if destination.exists():
        if not destination.is_file() or _digest(source) != _digest(destination):
            raise FileExistsError('Recovery cache already contains different data.')
        return
    fd, temp = tempfile.mkstemp(prefix='.tempo-package-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as out, open(source, 'rb') as inp:
            shutil.copyfileobj(inp, out, 1024 * 1024)
            out.flush(); os.fsync(out.fileno())
        if _digest(temp) != _digest(source):
            raise ValueError('Recovery package copy failed verification.')
        os.link(temp, destination)
    finally:
        os.unlink(temp)


def _validate_locations(root, external, packages_dir, expected_root):
    root = _safe(root)
    external = _safe(external)
    packages_dir = _safe(packages_dir)
    expected_root = _safe(expected_root)
    if root != expected_root or root == external or root in external.parents or external in root.parents:
        raise ValueError('Recovery root is not the expected isolated installation.')
    if packages_dir == root or root in packages_dir.parents or external in packages_dir.parents:
        raise ValueError('Update lock must be outside the application and recovery cache.')
    return root, external, packages_dir


def arm_recovery(root, external_recovery_dir, base_release, base_package, target_release, target_package,
                 *, trusted_keys, packages_dir, expected_root, nvidia_marker=None):
    """Copy verified signed base/target into an external, offline recovery cache.

    Returns the record path. Both releases must be freshly authenticated; repeat
    use of a cache with differing contents is rejected. ``expected_root`` is the
    installation path independently selected by the coordinator, not the record.
    """
    root, external, packages = _validate_locations(root, external_recovery_dir, packages_dir, expected_root)
    base = _validated_release(base_release.envelope, trusted_keys, base_release.channel)
    target = _validated_release(target_release.envelope, trusted_keys, target_release.channel)
    if (base.version, base.commit) != (base_release.version, base_release.commit) or (target.version, target.commit) != (target_release.version, target_release.commit):
        raise ValueError('Release identity mismatch.')
    if base.channel != target.channel or base.version == target.version:
        raise ValueError('Incompatible recovery release pair.')
    _application_inventory(base); _application_inventory(target)
    with update_lock(packages):
        _verify_app(root / 'current', base)
        for release, package in ((base, base_package), (target, target_package)):
            verify_zip(_safe(package, missing_ok=False), release.inventory)
        if nvidia_marker is not None:
            encoded_marker = json.dumps(nvidia_marker, sort_keys=True, allow_nan=False)
            if not isinstance(nvidia_marker, dict) or len(encoded_marker.encode()) > 4096:
                raise ValueError('Invalid NVIDIA selection marker.')
        external.mkdir(parents=True, exist_ok=True)
        record_path = external / 'recovery.json'
        if record_path.exists():
            raise FileExistsError('A recovery transaction already exists; retain it for repair.')
        for name, package in (('base.nupkg', base_package), ('target.nupkg', target_package)):
            _store_copy(package, external / name)
        for name, release in (('base.json', base), ('target.json', target)):
            path = external / name
            if path.exists() and path.read_bytes() != release.envelope:
                raise FileExistsError('A different signed recovery record already exists.')
            if not path.exists():
                _atomic(path, release.envelope)
        record = {'format': _FORMAT, 'root': str(root), 'packages_dir': str(packages),
                  'channel': base.channel, 'base': [base.version, base.commit, _schema(base)],
                  'target': [target.version, target.commit, _schema(target)],
                  'base_sha256': _digest(external / 'base.nupkg'),
                  'target_sha256': _digest(external / 'target.nupkg'),
                  'nvidia_marker': nvidia_marker, 'state': 'armed'}
        _json_write(record_path, record)
        return record_path


def _read_record(path, expected_root, packages_dir):
    path = _safe(path, missing_ok=False)
    if path.name != 'recovery.json' or path.stat().st_size > 65536:
        raise ValueError('Invalid recovery record path or size.')
    record = json.loads(path.read_text(encoding='utf-8'))
    fields = {'format', 'root', 'packages_dir', 'channel', 'base', 'target', 'base_sha256',
              'target_sha256', 'nvidia_marker', 'state'}
    if not isinstance(record, dict) or set(record) != fields or record['format'] != _FORMAT:
        raise ValueError('Invalid recovery record.')
    root, external, packages = _validate_locations(record['root'], path.parent, packages_dir, expected_root)
    if str(packages) != record['packages_dir'] or record['channel'] not in ('stable', 'prerelease'):
        raise ValueError('Recovery lock or channel mismatch.')
    return path, record, root, external, packages


def _load_pair(external, record, keys):
    releases = []
    for name in ('base', 'target'):
        meta = _safe(external / (name + '.json'), missing_ok=False)
        package = _safe(external / (name + '.nupkg'), missing_ok=False)
        if meta.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('Oversized recovery metadata.')
        release = _validated_release(meta.read_bytes(), keys, record['channel'], offline=True)
        if [release.version, release.commit, _schema(release)] != record[name]:
            raise ValueError('Recovery release identity changed.')
        if _digest(package) != record[name + '_sha256']:
            raise ValueError('Recovery package bytes changed.')
        verify_zip(package, release.inventory)
        releases.append((release, package))
    return releases


def _stage(root, release, package):
    stage = _safe(root / 'tempo-repair-stage')
    if stage.exists():
        _verify_app(stage, release)
        return stage
    temporary = Path(tempfile.mkdtemp(prefix='.tempo-repair-stage-', dir=root))
    try:
        with zipfile.ZipFile(package) as archive:
            for item in _application_inventory(release):
                destination = _safe(temporary / item.path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(_PREFIX + item.path) as source, open(destination, 'xb') as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
                    target.flush(); os.fsync(target.fileno())
        _verify_app(temporary, release)
        os.rename(temporary, stage)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

    return stage


def recover(record_path, trusted_keys, *, mode, expected_root, packages_dir):
    """Explicitly install a verified target or compatible previous version.

    A nonempty quarantine is never deleted or overwritten. Repeated calls after
    an interrupted swap resume if the verified stage and quarantine remain.
    """
    if mode not in ('forward', 'previous'):
        raise ValueError('Recovery mode must be forward or previous.')
    path, record, root, external, packages = _read_record(record_path, expected_root, packages_dir)
    with update_lock(packages):
        path, record, root, external, packages = _read_record(path, expected_root, packages_dir)
        (base, base_package), (target, target_package) = _load_pair(external, record, trusted_keys)
        if mode == 'previous' and _schema(base) != _schema(target):
            raise ValueError('Previous application is incompatible with the newer data schema.')
        release, package = (base, base_package) if mode == 'previous' else (target, target_package)
        current, quarantine = _safe(root / 'current'), _safe(root / 'tempo-repair-quarantine')
        if quarantine.exists() and current.exists():
            _verify_app(current, release)
            record['state'] = 'recovered_' + mode; _json_write(path, record)
            return record['state']
        stage = _stage(root, release, package)
        record['state'] = 'swapping_' + mode; _json_write(path, record)
        if current.exists():
            if quarantine.exists():
                raise FileExistsError('Existing quarantine requires manual review.')
            os.rename(current, quarantine)
        try:
            os.rename(stage, current)
        except OSError:
            if not current.exists() and quarantine.exists():
                try:
                    os.rename(quarantine, current)
                except OSError:
                    pass  # Preserve both paths and journal for independent repair.
            raise
        _verify_app(current, release)
        record['state'] = 'recovered_' + mode; _json_write(path, record)
        return record['state']


def confirm(record_path, trusted_keys, *, running_version, running_commit, expected_root, packages_dir):
    """Verify running target and prior payload before removing updater backup.

    Caller must have independently observed desktop readiness and excluded all
    concurrent installation changes. Quarantined recovery data is never deleted.
    """
    path, record, root, external, packages = _read_record(record_path, expected_root, packages_dir)
    with update_lock(packages):
        path, record, root, external, packages = _read_record(path, expected_root, packages_dir)
        (base, _), (target, _) = _load_pair(external, record, trusted_keys)
        if (running_version, running_commit) != (target.version, target.commit):
            raise ValueError('Running version does not match the signed target.')
        _verify_app(root / 'current', target)
        previous, marker = _safe(root / 'tempo-previous'), _safe(root / 'tempo-update-pending.txt')
        if previous.exists():
            _verify_app(previous, base)
        if marker.exists():
            expected = f'TEMPO update pending\nprevious={base.version}\ntarget={target.version}\n'
            if marker.read_text(encoding='utf-8') != expected:
                raise ValueError('Updater marker does not match the recovery record.')
        record['state'] = 'confirming'; _json_write(path, record)
        if previous.exists():
            shutil.rmtree(previous)
        if marker.exists():
            marker.unlink()
        record['state'] = 'confirmed'; _json_write(path, record)
        return 'confirmed'


def main():
    parser = argparse.ArgumentParser(description='Repair an already armed TEMPO installation.')
    parser.add_argument('--mode', required=True, choices=('forward', 'previous'))
    args = parser.parse_args()
    if sys.platform != 'win32' or not EMBEDDED_TRUSTED_KEYS:
        raise SystemExit('Repair companion has no certified embedded trust keys.')
    local = Path(os.environ['LOCALAPPDATA'])
    root = local / 'TEMPO.Bio.Desktop'
    packages = local / 'TEMPO' / 'updates' / 'packages'
    record = local / 'TEMPO' / 'updates' / 'recovery' / 'recovery.json'
    print(recover(record, EMBEDDED_TRUSTED_KEYS, mode=args.mode, expected_root=root, packages_dir=packages))


if __name__ == '__main__':
    main()
