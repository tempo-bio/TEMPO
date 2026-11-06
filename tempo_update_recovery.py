"""Offline, explicit recovery of an armed Windows update transaction.

The caller must ensure all application and helper processes have exited before
calling these functions. Recovery never starts an executable or modifies user
research stores. Explicit previous-version repair may restore its verified
NVIDIA selection. A packaged companion must embed a nonempty trust store.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone

from tempo_update_lock import application_lock, startup_guard, update_lock
from tempo_update_manifest import (FileRecord, load_verified_manifest, verify_directory,
                                   verify_zip, validate_path)
from tempo_update_migration import _safe

# Populated by the separately certified companion build, never from CLI input.
EMBEDDED_TRUSTED_KEYS = {}
EMBEDDED_UPDATER_SHA256 = None
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
    value = json.loads(release.signed_payload)['storage_schema_version']
    if type(value) is not int or value < 1:
        raise ValueError('Invalid signed storage schema version.')
    return value


def _application_inventory(release):
    """Project the signed ZIP inventory onto Velopack's installed current tree.

    The pinned native extractor excludes its Squirrel.exe updater and obsolete
    execution stubs. Those bytes stay authenticated in the full package; they
    are installation-owner files, not files installed under current.
    """
    updaters = [item for item in release.inventory if item.path.endswith('Squirrel.exe')]
    if updaters and (len(updaters) != 1 or updaters[0].path != _PREFIX + 'Squirrel.exe'):
        raise ValueError('Ambiguous native updater layout in signed package.')
    entries = [FileRecord(item.path[len(_PREFIX):], item.size, item.sha256)
               for item in release.inventory if item.path.startswith(_PREFIX)
               and item.path != _PREFIX + 'Squirrel.exe'
               and not item.path.endswith('_ExecutionStub.exe')]
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


def _owner_record(release):
    records = [item for item in release.inventory if item.path.endswith('Squirrel.exe')]
    if len(records) != 1 or records[0].path != _PREFIX + 'Squirrel.exe':
        raise ValueError('Signed release must contain one canonical native updater.')
    return records[0]


def _owner_location(root):
    owner = _safe(root / 'Update.exe')
    if owner.exists() and not owner.is_file():
        raise ValueError('Native updater location is not an ordinary file.')
    return owner


def _verify_owner(root, release):
    record = _owner_record(release)
    owner = _owner_location(root)
    if (not owner.is_file() or owner.stat().st_size != record.size or _digest(owner) != record.sha256):
        raise ValueError('Native updater does not match the signed application release.')
    return owner


def _restore_owner(root, release, package):
    """Restore authenticated owner bytes without executing or discarding old bytes.

    A stopped restoration remains retryable from the retained signed package.
    Exclusive hard-link creation prevents overwriting a concurrently created owner.
    """
    record = _owner_record(release)
    owner = _owner_location(root)
    if owner.is_file() and owner.stat().st_size == record.size and _digest(owner) == record.sha256:
        return
    fd, temporary = tempfile.mkstemp(prefix='.tempo-repair-owner-', dir=root)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as destination, zipfile.ZipFile(package) as archive:
            with archive.open(record.path) as source:
                shutil.copyfileobj(source, destination, 1024 * 1024)
            destination.flush(); os.fsync(destination.fileno())
        if temporary.stat().st_size != record.size or _digest(temporary) != record.sha256:
            raise ValueError('Restored native updater differs from the signed package.')
        if owner.exists():
            quarantine = _safe(root / ('tempo-repair-owner-quarantine-' + uuid.uuid4().hex + '.exe'))
            os.link(owner, quarantine)
            owner.unlink()
        try:
            os.link(temporary, owner)
        except FileExistsError:
            _verify_owner(root, release)
        _verify_owner(root, release)
    finally:
        temporary.unlink(missing_ok=True)


def _retain_native_pending(root):
    """Retain obsolete native transaction state after explicit verified repair.

    A damaged marker cannot authorize deletion and must not block the next
    update. The verified offline pair remains outside the application directory.
    Repeated calls resume after either rename without overwriting prior copies.
    """
    for name, directory in (('tempo-previous', True), ('tempo-update-pending.txt', False)):
        source = _safe(root / name)
        if not source.exists():
            continue
        if (directory and not source.is_dir()) or (not directory and not source.is_file()):
            raise ValueError('Unexpected native transaction path; retain it for review.')
        destination = _safe(root / ('tempo-repair-retained-' + name + '-' + uuid.uuid4().hex))
        os.rename(source, destination)


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
    _owner_record(base); _owner_record(target)
    with update_lock(packages):
        _verify_app(root / 'current', base)
        _verify_owner(root, base)
        for release, package in ((base, base_package), (target, target_package)):
            verify_zip(_safe(package, missing_ok=False), release.inventory)
        if nvidia_marker is not None:
            encoded_marker = json.dumps(nvidia_marker, sort_keys=True, allow_nan=False)
            if not isinstance(nvidia_marker, dict) or len(encoded_marker.encode()) > 4096:
                raise ValueError('Invalid NVIDIA selection marker.')
        external.mkdir(parents=True, exist_ok=True)
        record_path = external / 'recovery.json'
        if record_path.exists():
            _, previous, _, _, _ = _read_record(record_path, expected_root, packages)
            (old_base, _), (old_target, _) = _load_pair(external, previous, trusted_keys)
            if old_base == base and old_target == target and previous['state'] == 'armed':
                return record_path
            if previous['state'] != 'confirmed' or (old_target.version, old_target.commit) != (base.version, base.commit):
                raise FileExistsError('A recovery transaction already exists; retain it for repair.')
            # Keep previously confirmed offline material outside the next journal.
            history = _safe(external.with_name('recovery-history'))
            history.mkdir(exist_ok=True)
            os.rename(external, history / (old_target.version + '-' + old_target.commit))
            external.mkdir()
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
        _owner_record(release); _owner_location(root)
        current, quarantine = _safe(root / 'current'), _safe(root / 'tempo-repair-quarantine')
        if current.exists():
            try:
                _verify_app(current, release)
            except ValueError:
                # An explicit repair in the other direction retains both the
                # original quarantine and the presently installed payload.
                if quarantine.exists():
                    quarantine = _safe(root / ('tempo-repair-quarantine-' + uuid.uuid4().hex))
            else:
                _restore_owner(root, release, package)
                _retain_native_pending(root)
                if mode == 'previous':
                    from tempo_update_components import restore_previous_runtime
                    restore_previous_runtime(record, base, target,
                        runtime_root=packages.parent.parent / 'nvidia-runtime')
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
        _restore_owner(root, release, package)
        _retain_native_pending(root)
        if mode == 'previous':
            from tempo_update_components import restore_previous_runtime
            restore_previous_runtime(record, base, target,
                runtime_root=packages.parent.parent / 'nvidia-runtime')
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
        (base, _), (target, package) = _load_pair(external, record, trusted_keys)
        if (running_version, running_commit) != (target.version, target.commit):
            raise ValueError('Running version does not match the signed target.')
        _verify_app(root / 'current', target)
        _verify_owner(root, target)
        from tempo_update_components import verify_selected_runtime
        receipt = None
        if record['nvidia_marker'] is not None:
            receipt_path = _safe(packages.parent / 'prepared-nvidia.json', missing_ok=False)
            if receipt_path.stat().st_size > 65536:
                raise ValueError('Invalid staged NVIDIA receipt.')
            receipt = json.loads(receipt_path.read_bytes())
        verify_selected_runtime(target, package, receipt, runtime_root=packages.parent.parent / 'nvidia-runtime',
                                previous_marker=record['nvidia_marker'])
        previous, marker = _safe(root / 'tempo-previous'), _safe(root / 'tempo-update-pending.txt')
        if previous.exists():
            if record['state'] == 'confirming':
                # A stopped cleanup may already have removed part of this
                # verified backup. Every remaining byte must still be signed;
                # unexpected data or links remain a recoverable conflict.
                remaining = tuple(item for item in _application_inventory(base)
                                  if _safe(previous / item.path).exists())
                if remaining:
                    verify_directory(previous, remaining)
                else:
                    for directory, dirs, files in os.walk(previous, followlinks=False,
                            onerror=lambda error: (_ for _ in ()).throw(error)):
                        for name in dirs + files:
                            _safe(Path(directory) / name, missing_ok=False)
                        if files:
                            raise ValueError('Unexpected data remains in the previous application backup.')
            else:
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


def _wait_for_application(profile):
    deadline = time.monotonic() + 60
    with startup_guard(profile):
        while True:
            lease = application_lock(profile)
            try:
                lease.__enter__()
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('TEMPO or a worker is still running. Finish its work before updating.')
                time.sleep(.1)
    return lease


def apply_pending(record_path, trusted_keys, *, expected_root, packages_dir):
    """Apply only an armed transaction, after all desktop/worker leases settle.

    The separate companion owns the replacement lifetime. It never force-stops
    an application and does not automatically launch an older application.
    """
    path, record, root, external, packages = _read_record(record_path, expected_root, packages_dir)
    if record['state'] != 'armed':
        raise ValueError('This transaction requires explicit repair rather than another apply.')
    lease = _wait_for_application(packages.parent.parent)
    try:
        with update_lock(packages):
            _, record, root, external, packages = _read_record(path, expected_root, packages_dir)
            if record['state'] != 'armed':
                raise ValueError('Update transaction changed while waiting for application exit.')
            (base, _), (target, package) = _load_pair(external, record, trusted_keys)
            _validated_release(target.envelope, trusted_keys, target.channel)
            _verify_app(root / 'current', base)
            from tempo_update_components import verify_component_transition
            receipt = None
            if record['nvidia_marker'] is not None:
                receipt_path = _safe(packages.parent / 'prepared-nvidia.json', missing_ok=False)
                if receipt_path.stat().st_size > 65536:
                    raise ValueError('Invalid staged NVIDIA receipt.')
                receipt = json.loads(receipt_path.read_bytes())
            verify_component_transition(base, target, package, receipt,
                runtime_root=packages.parent.parent / 'nvidia-runtime', expected_marker=record['nvidia_marker'])
            updater = _safe(root / 'Update.exe', missing_ok=False)
            hashes = [item.sha256 for item in base.inventory if item.path == _PREFIX + 'Squirrel.exe']
            if len(hashes) != 1 or _digest(updater) != hashes[0]:
                raise ValueError('Native updater does not match the signed installed release.')
            record['state'] = 'installing'; _json_write(path, record)
        # Native apply acquires the same package lock. Keep the application
        # lease, but release the package lock before starting its owner.
        with open(external / 'native-apply.log', 'ab') as log:
            result = subprocess.run([str(updater), 'apply', '--norestart',
                '--package', str(package), '--rootDir', str(root), '--packageDir', str(packages)],
                stdout=log, stderr=log, check=False)
        with update_lock(packages):
            if result.returncode:
                record['state'] = 'recovery_required'; _json_write(path, record)
                raise RuntimeError('Installation did not finish. Run TEMPORepair to repair forward or choose the compatible previous version.')
            _verify_app(root / 'current', target)
            _verify_owner(root, target)
            record['state'] = 'awaiting_confirmation'; _json_write(path, record)
    finally:
        lease.__exit__(None, None, None)
    # Start only the verified target after releasing the application lease.
    subprocess.Popen([str(root / 'current' / 'TEMPO.exe')], cwd=str(root / 'current'), close_fds=True)
    return 'awaiting_confirmation'


def uninstall_application(root, profile, trusted_keys):
    """Launch the installation owner only after every TEMPO lease is released."""
    lease = _wait_for_application(profile)
    try:
        from tempo_update_service import read_configuration
        configuration = read_configuration(_safe(Path(root) / 'current' / '_internal', missing_ok=False))
        import base64
        if configuration is None or {
                key: base64.b64decode(value, validate=True)
                for key, value in configuration['trusted_keys'].items()} != trusted_keys:
            raise ValueError('Uninstall trust configuration changed. Use the original installer to repair it.')
        updater = _safe(Path(root) / 'Update.exe', missing_ok=False)
        if (not EMBEDDED_UPDATER_SHA256 or _digest(updater) != EMBEDDED_UPDATER_SHA256
                or configuration['updater_sha256'] != EMBEDDED_UPDATER_SHA256):
            raise ValueError('The installation owner failed its certified checksum.')
        result = subprocess.run([str(updater), 'uninstall', '--rootDir', str(root)], check=False)
        if result.returncode:
            raise RuntimeError('The uninstaller did not finish. User data was retained; retry from Windows settings.')
        return 'uninstalled'
    finally:
        lease.__exit__(None, None, None)


def _main():
    parser = argparse.ArgumentParser(description='Repair or migrate a TEMPO installation.')
    parser.add_argument('--mode', default='forward', choices=('apply', 'confirm', 'forward', 'previous', 'uninstall',
                                                            'prepare-migration', 'finish-migration', 'snapshot-packages'))
    parser.add_argument('--version')
    parser.add_argument('--commit')
    parser.add_argument('--legacy-root', type=Path)
    parser.add_argument('--legacy-installer', type=Path)
    parser.add_argument('--target-manifest', type=Path)
    parser.add_argument('--target-package', type=Path)
    parser.add_argument('--prepare-nvidia', action='store_true',
                        help='download and probe the signed target worker before preparing legacy retirement')
    parser.add_argument('--show-errors', action='store_true', help='show actionable failures when launched by the desktop')
    args = parser.parse_args()
    if args.prepare_nvidia and args.mode != 'prepare-migration':
        parser.error('--prepare-nvidia applies only to prepare-migration')
    if args.mode == 'snapshot-packages' and args.legacy_root is None:
        parser.error('snapshot-packages requires --legacy-root')
    if args.mode == 'prepare-migration' and any(getattr(args, name) is None for name in
            ('legacy_root', 'legacy_installer', 'target_manifest', 'target_package')):
        parser.error('prepare-migration requires --legacy-root, --legacy-installer, --target-manifest and --target-package')
    if sys.platform != 'win32' or not EMBEDDED_TRUSTED_KEYS:
        raise SystemExit('Repair companion has no certified embedded trust keys.')
    local = Path(os.environ['LOCALAPPDATA'])
    root = local / 'TEMPO.Bio.Desktop'
    packages = local / 'TEMPO' / 'updates' / 'packages'
    record = local / 'TEMPO' / 'updates' / 'recovery' / 'recovery.json'
    try:
        if args.mode == 'snapshot-packages':
            from tempo_update_addons import snapshot_addons
            result = snapshot_addons(args.legacy_root, local / 'TEMPO')
        elif args.mode == 'prepare-migration':
            from tempo_legacy_migration import prepare_legacy_migration, _read
            channel = _read(args.target_manifest).get('signed', {}).get('channel')
            target_release = _validated_release(args.target_manifest.read_bytes(), EMBEDDED_TRUSTED_KEYS, channel)
            result = prepare_legacy_migration(args.legacy_root, local / 'TEMPO', args.legacy_installer,
                target_release, args.target_package, root, trusted_keys=EMBEDDED_TRUSTED_KEYS,
                prepare_nvidia=args.prepare_nvidia)
        elif args.mode == 'finish-migration':
            from tempo_legacy_migration import finish_legacy_migration
            result = finish_legacy_migration(local / 'TEMPO', trusted_keys=EMBEDDED_TRUSTED_KEYS)
        elif args.mode == 'uninstall':
            result = uninstall_application(root, local / 'TEMPO', EMBEDDED_TRUSTED_KEYS)
        elif args.mode == 'apply':
            result = apply_pending(record, EMBEDDED_TRUSTED_KEYS, expected_root=root, packages_dir=packages)
        elif args.mode == 'confirm':
            result = confirm(record, EMBEDDED_TRUSTED_KEYS, running_version=args.version,
                             running_commit=args.commit, expected_root=root, packages_dir=packages)
        else:
            with startup_guard(local / 'TEMPO'), application_lock(local / 'TEMPO'):
                result = recover(record, EMBEDDED_TRUSTED_KEYS, mode=args.mode, expected_root=root, packages_dir=packages)
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        if args.show_errors:
            import ctypes
            if args.mode == 'snapshot-packages':
                message = str(error) + '\n\nThe package snapshot did not finish. Keep the old installation, receipts and backups; no package was removed.'
                title = 'TEMPO package recovery needs attention'
            elif args.mode in ('prepare-migration', 'finish-migration'):
                message = str(error) + '\n\nMigration did not finish. Keep the old installation until preparation succeeds. Verified copies were retained for review.'
                title = 'TEMPO migration needs attention'
            elif args.mode == 'uninstall':
                message = str(error) + '\n\nThe uninstaller did not finish. Use Windows Settings to retry or the original installer to repair the installation.'
                title = 'TEMPO uninstall needs attention'
            else:
                message = (str(error) + '\n\nThe verified packages were retained. Run "' +
                           sys.executable + '" --mode forward to repair the update, '
                           'or use --mode previous to request a compatible previous version. '
                           'Diagnostics: ' + str(record.parent / 'native-apply.log'))
                title = 'TEMPO update needs attention'
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
        raise SystemExit(1) from error
    print(json.dumps(result, indent=2) if isinstance(result, dict) else result)


def main():
    _main()


if __name__ == '__main__':
    main()
