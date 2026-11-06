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


def _verify_retained(root, marker, descriptor):
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
            with source.open(info) as original, path.open('rb') as installed:
                original_hash, installed_hash = _digest(original), _digest(installed)
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
