"""Verify and restore an authenticated NVIDIA selection during repair.

This module has no GUI or application-configuration imports. Callers must hold
an exclusive application lease and supply already authenticated releases.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import zipfile

from tempo_annotation_store import session_lock
from tempo_nvidia_manifest import validate_descriptor
from tempo_update_migration import _safe

_MAX_EXPANDED = 16 * 1024 ** 3


def read_runtime_marker(runtime_root):
    """An unreadable/broken selection is a blocker, never evidence of CPU opt-out."""
    path = _safe(Path(runtime_root) / 'active.json')
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 4096:
        raise ValueError('Invalid NVIDIA selection. Repair NVIDIA support or defer the update.')
    def unique(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError('Duplicate NVIDIA selection field.')
            result[name] = value
        return result
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid NVIDIA selection value.')))
    except (ValueError, UnicodeError) as error:
        raise ValueError('Invalid NVIDIA selection. Repair NVIDIA support or defer the update.') from error
    if not isinstance(value, dict):
        raise ValueError('Invalid NVIDIA selection. Repair NVIDIA support or defer the update.')
    return value


def packaged_descriptor(release, package):
    """Bind signed component metadata to the CPU payload that will select it.

    Callers authenticate the release and verify the complete package separately.
    Read only the bounded descriptor member here, verifying its signed identity.
    """
    descriptor = _descriptor(release)
    name = 'lib/app/_internal/assets/nvidia-runtime.json'
    records = [item for item in release.inventory if item.path == name]
    if len(records) != 1 or records[0].size > 16384:
        raise ValueError('Signed application payload has no bounded NVIDIA descriptor.')
    with zipfile.ZipFile(_safe(package, missing_ok=False)) as source:
        members = [item for item in source.infolist() if item.filename == name]
        if len(members) != 1 or members[0].file_size != records[0].size:
            raise ValueError('Packaged NVIDIA descriptor differs from its signed inventory.')
        data = source.read(members[0])
    if hashlib.sha256(data).hexdigest() != records[0].sha256 or json.loads(data) != descriptor:
        raise ValueError('Packaged NVIDIA descriptor differs from signed component metadata.')
    return descriptor


def staged_marker(receipt, descriptor):
    if (not isinstance(receipt, dict) or set(receipt) != {
            'schema_version', 'descriptor', 'bundle', 'previous_marker'}
            or type(receipt['schema_version']) is not int or receipt['schema_version'] != 1
            or receipt['descriptor'] != descriptor):
        raise ValueError('Staged NVIDIA receipt differs from the signed target.')
    marker = dict(schema_version=1, version=descriptor['app_version'], commit=descriptor['commit'],
                  bundle=receipt['bundle'], executable=descriptor['executable'],
                  executable_sha256=descriptor['executable_sha256'], archive_sha256=descriptor['archive_sha256'])
    if not _valid_marker(marker, descriptor):
        raise ValueError('Staged NVIDIA receipt has an invalid bundle path.')
    return marker


def verify_staged_runtime(receipt, descriptor, *, runtime_root, previous_marker, allow_activated=False):
    """Verify selection and every staged byte; caller holds the runtime install lock."""
    root = _safe(runtime_root)
    marker = staged_marker(receipt, descriptor)
    if receipt['previous_marker'] != previous_marker:
        raise ValueError('NVIDIA selection differs from the prepared transaction.')
    current = read_runtime_marker(root)
    if current != previous_marker and not (allow_activated and current == marker):
        raise ValueError('NVIDIA selection changed after preparation. Retain both runtimes for review.')
    _verify_retained(root, marker, descriptor)
    return marker


def activate_staged_runtime(receipt, descriptor, *, runtime_root, previous_marker):
    """Select a fully verified target, including an interrupted activation retry."""
    root = _safe(runtime_root)
    with session_lock(root / 'install'):
        marker = verify_staged_runtime(receipt, descriptor, runtime_root=root,
                                       previous_marker=previous_marker, allow_activated=True)
        if read_runtime_marker(root) == marker:
            return False
        from tempo_update_migration import _atomic_json
        _atomic_json(root / 'active.json', marker)
        return True


def verify_component_transition(base, target, package, receipt, *, runtime_root, expected_marker):
    """Recheck the old/target pair before handoff and again after application exit."""
    root = _safe(runtime_root)
    with session_lock(root / 'install'):
        marker = read_runtime_marker(root)
        if marker != expected_marker:
            raise ValueError('NVIDIA selection changed after update preparation.')
        if marker is None:
            if receipt is not None:
                raise ValueError('NVIDIA staging does not match the CPU selection.')
            return None
        descriptor = _descriptor(base)
        if not _valid_marker(marker, descriptor):
            raise ValueError('Existing NVIDIA selection differs from the signed base release. Repair support or defer the update.')
        _verify_retained(root, marker, descriptor)
        descriptor = packaged_descriptor(target, package)
        verify_staged_runtime(receipt, descriptor, runtime_root=root, previous_marker=marker)
        return marker


def verify_selected_runtime(target, package, receipt, *, runtime_root, previous_marker):
    """Independent confirmation requires the target worker to be selected and intact."""
    root = _safe(runtime_root)
    with session_lock(root / 'install'):
        if previous_marker is None:
            if read_runtime_marker(root) is not None:
                raise ValueError('NVIDIA selection changed during the CPU update.')
            return
        descriptor = packaged_descriptor(target, package)
        marker = verify_staged_runtime(receipt, descriptor, runtime_root=root,
                                       previous_marker=previous_marker, allow_activated=True)
        if read_runtime_marker(root) != marker:
            raise ValueError('The target NVIDIA worker has not been selected. Keep the recovery packages.')


def _descriptor(release):
    if release.nvidia_descriptor is None:
        raise ValueError('Signed NVIDIA descriptor is unavailable; repair forward.')
    try:
        value = json.loads(release.nvidia_descriptor)
        validate_descriptor(value, version=release.version, commit=release.commit, allow_legacy=False)
        return value
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValueError('Signed NVIDIA descriptor cannot be verified; repair forward.') from error


def _valid_marker(marker, descriptor):
    if not isinstance(marker, dict) or set(marker) != {
            'schema_version', 'version', 'commit', 'bundle', 'executable',
            'executable_sha256', 'archive_sha256'}:
        return False
    prefix = f"{descriptor['commit']}-{descriptor['archive_sha256'][:12]}"
    return (type(marker['schema_version']) is int and marker['schema_version'] == 1
            and marker['version'] == descriptor['app_version']
            and marker['commit'] == descriptor['commit']
            and marker['executable'] == descriptor['executable']
            and marker['executable_sha256'] == descriptor['executable_sha256']
            and marker['archive_sha256'] == descriptor['archive_sha256']
            and isinstance(marker['bundle'], str)
            and re.fullmatch(re.escape(prefix) + r'(?:-[0-9a-f]{32})?', marker['bundle']) is not None)


def _digest(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(block)
    return digest.hexdigest()


def _verify_retained(root, marker, descriptor, *, locked_files=None):
    bundle = _safe(root / marker['bundle'], missing_ok=False)
    archive = _safe(root / (marker['bundle'] + '.zip'), missing_ok=False)
    if not bundle.is_dir() or not archive.is_file() or archive.stat().st_size != descriptor['archive_size']:
        raise ValueError('Retained NVIDIA runtime is missing or invalid; repair forward.')
    with archive.open('rb') as stream:
        if _digest(stream) != descriptor['archive_sha256']:
            raise ValueError('Retained NVIDIA archive checksum failed; repair forward.')
    actual = set()
    def fail(error):
        raise error
    for directory, dirs, files in os.walk(bundle, followlinks=False, onerror=fail):
        for name in dirs + files:
            path = _safe(Path(directory) / name, missing_ok=False)
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError('Invalid NVIDIA runtime entry; repair forward.')
            if stat.S_ISREG(mode):
                actual.add(path.relative_to(bundle).as_posix())
    expected, names, expanded = set(), set(), 0
    with zipfile.ZipFile(archive) as source:
        infos = source.infolist()
        if len(infos) > 30000:
            raise ValueError('NVIDIA archive contains too many files.')
        for info in infos:
            name = info.orig_filename
            member = PurePosixPath(name)
            if (not name or name != info.filename or '\\' in name or '\0' in name
                    or name.startswith('/') or ':' in name or member.is_absolute()
                    or '..' in member.parts or len(member.parts) < 2 or member.parts[0] != 'TEMPO'
                    or name.rstrip('/') != member.as_posix()
                    or any(part.endswith((' ', '.')) for part in member.parts)
                    or stat.S_ISLNK(info.external_attr >> 16)):
                raise ValueError('NVIDIA archive has an unsafe path.')
            key = name.rstrip('/').casefold()
            if key in names:
                raise ValueError('NVIDIA archive contains duplicate paths.')
            names.add(key)
            expanded += info.file_size
            if expanded > _MAX_EXPANDED:
                raise ValueError('NVIDIA archive is too large.')
            if info.is_dir():
                continue
            expected.add(name)
            path = _safe(bundle.joinpath(*member.parts), missing_ok=False)
            if not path.is_file() or path.stat().st_size != info.file_size:
                raise ValueError('Retained NVIDIA runtime inventory differs; repair forward.')
            with source.open(info) as original:
                original_hash = _digest(original)
            locked = (locked_files or {}).get(name)
            if locked is None:
                with path.open('rb') as installed:
                    installed_hash = _digest(installed)
            else:
                # Windows share-mode-zero executable guards deny even our own
                # second open. The guard hashes through its owned read handle.
                if locked[0] != info.file_size:
                    raise ValueError('Retained NVIDIA runtime inventory differs; repair forward.')
                installed_hash = locked[1]
            if original_hash != installed_hash:
                raise ValueError('Retained NVIDIA runtime inventory differs; repair forward.')
            if name == descriptor['executable'] and original_hash != descriptor['executable_sha256']:
                raise ValueError('Retained NVIDIA executable checksum failed; repair forward.')
    if expected != actual or descriptor['executable'] not in expected:
        raise ValueError('Retained NVIDIA runtime inventory differs; repair forward.')


def restore_previous_runtime(record, base_release, target_release, *, runtime_root):
    """Restore only the captured, signed base selection; reject unrelated changes."""
    root = _safe(runtime_root)
    marker_path = _safe(root / 'active.json')
    previous = record.get('nvidia_marker')
    root.mkdir(parents=True, exist_ok=True)
    with session_lock(root / 'install'):
        marker_path = _safe(marker_path)
        current = None
        if marker_path.exists():
            if not marker_path.is_file() or marker_path.stat().st_size > 4096:
                raise ValueError('Invalid NVIDIA selection; repair forward.')
            current = json.loads(marker_path.read_bytes())
            if not isinstance(current, dict):
                raise ValueError('Invalid NVIDIA selection; repair forward.')
        if previous is None:
            if current is not None:
                raise ValueError('NVIDIA selection changed; repair forward.')
            return False
        descriptor = _descriptor(base_release)
        if not _valid_marker(previous, descriptor):
            raise ValueError('Previous NVIDIA selection does not match the signed release; repair forward.')
        if current != previous:
            if not _valid_marker(current, _descriptor(target_release)):
                raise ValueError('NVIDIA selection changed; repair forward.')
        _verify_retained(root, previous, descriptor)
        data = json.dumps(previous, sort_keys=True, allow_nan=False).encode('utf-8')
        fd, temporary = tempfile.mkstemp(prefix='active-', suffix='.tmp', dir=root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _safe(marker_path)
            os.replace(temporary, marker_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return True
