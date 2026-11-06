"""Install the optional, release-matched NVIDIA inference bundle for one user."""

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import urllib.request
from urllib.parse import urlsplit
import zipfile
import uuid

from tempo_config import get_base_dir, get_config_dir
from tempo_release import VERSION, build_identity
from tempo_process import run_model_worker
from tempo_nvidia_manifest import MAX_DESCRIPTOR, validate_descriptor


_MAX_EXPANDED = 16 * 1024**3


def runtime_root():
    return Path(get_config_dir()).parent / 'nvidia-runtime'


def release_descriptor():
    """Return only the descriptor pinned into the CPU executable at build time."""
    if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
        return None
    path = Path(get_base_dir()) / 'assets' / 'nvidia-runtime.json'
    try:
        if path.stat().st_size > MAX_DESCRIPTOR:
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    try:
        validate_descriptor(value, version=VERSION, commit=build_identity().get('commit'))
    except ValueError:
        return None
    return value


def _digest(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def _extract_checked(archive, target):
    """Reject traversal, links and decompression bombs before writing members."""
    target = Path(target)
    with zipfile.ZipFile(archive) as source:
        infos = source.infolist()
        if len(infos) > 30000:
            raise ValueError('NVIDIA runtime archive has too many files.')
        names = set()
        expanded = 0
        for info in infos:
            # ZipInfo normalizes host separators and truncates at NUL on read.
            # Validate the original archive name before trusting that result.
            name = info.orig_filename
            member = PurePosixPath(name)
            if (not name or name != info.filename or '\\' in name or '\0' in name
                    or name.startswith('/') or ':' in name
                    or member.is_absolute() or '..' in member.parts or len(member.parts) < 2
                    or member.parts[0] != 'TEMPO'
                    or name.rstrip('/') != member.as_posix()
                    or any(part.endswith((' ', '.')) for part in member.parts)):
                raise ValueError('NVIDIA runtime archive has an unsafe path.')
            key = name.rstrip('/').casefold()
            if key in names:
                raise ValueError('NVIDIA runtime archive has duplicate paths.')
            names.add(key)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError('NVIDIA runtime archive contains a link.')
            expanded += info.file_size
            if expanded > _MAX_EXPANDED:
                raise ValueError('NVIDIA runtime archive is too large when unpacked.')
        for info in infos:
            destination = target.joinpath(*PurePosixPath(info.filename).parts)
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open(info) as original, destination.open('xb') as output:
                shutil.copyfileobj(original, output, 1024 * 1024)


def _probe(executable, descriptor, timeout=120):
    with tempfile.TemporaryDirectory(prefix='tempo-nvidia-probe-') as folder:
        report = Path(folder) / 'probe.json'
        process = run_model_worker([str(executable), '--probe-cuda', str(report)], timeout=timeout)
        if process.returncode or not report.is_file() or report.stat().st_size > 16384:
            raise RuntimeError('NVIDIA runtime could not complete its GPU check.')
        result = json.loads(report.read_text(encoding='utf-8'))
        if (result.get('success') is not True or result.get('commit') != descriptor['commit']
                or result.get('app_version') != descriptor['app_version']
                or result.get('cuda_version') != '12.6'):
            raise RuntimeError('NVIDIA runtime did not pass its GPU check.')
        return result


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=path.stem + '-', suffix='.tmp', dir=path.parent)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _locked(root, action):
    from tempo_annotation_store import session_lock
    root.mkdir(parents=True, exist_ok=True)
    try:
        with session_lock(root/'install'):
            return action()
    except BlockingIOError:
        raise RuntimeError('Another TEMPO instance is installing NVIDIA support. Wait for it to finish, then retry.') from None


def stage_nvidia_runtime(descriptor, *, target_version, target_commit,
                         report=lambda received, total: None, open_url=None):
    """Stage and probe a release-pinned worker without changing the active marker.

    The caller supplies the descriptor authenticated for the target application.
    The returned receipt is JSON serializable and can be persisted for activation.
    """
    validate_descriptor(descriptor, version=target_version, commit=target_commit)
    root = runtime_root()
    return _locked(root, lambda: _stage_runtime(root, descriptor, report, open_url))


def activate_nvidia_runtime(receipt, descriptor, *, target_version, target_commit):
    """Select a staged worker after checking its target and complete inventory."""
    validate_descriptor(descriptor, version=target_version, commit=target_commit)
    root = runtime_root()
    return _locked(root, lambda: _activate_runtime(root, receipt, descriptor))


def install_nvidia_runtime(report=lambda received, total: None, *, descriptor=None, open_url=None):
    """Download, check, probe and atomically select the current release worker."""
    descriptor = descriptor or release_descriptor()
    if descriptor is None:
        raise RuntimeError('This build does not include an NVIDIA runtime download.')
    # The embedded descriptor normally supplies the commit. Explicit descriptors
    # are also used by the package installer and are validated for this version.
    validate_descriptor(descriptor, version=VERSION, commit=descriptor.get('commit'))
    root = runtime_root()
    def install():
        receipt = _stage_runtime(root, descriptor, report, open_url)
        _activate_runtime(root, receipt, descriptor)
        return True
    return _locked(root, install)


def _bundle_matches(expected, installed):
    if not installed.is_dir() or any(path.is_symlink() for path in installed.rglob('*')):
        return False
    expected_files = {path.relative_to(expected) for path in expected.rglob('*') if path.is_file()}
    actual_files = {path.relative_to(installed) for path in installed.rglob('*') if path.is_file()}
    return expected_files == actual_files and all(
        (expected/name).stat().st_size == (installed/name).stat().st_size
        and _digest(expected/name) == _digest(installed/name) for name in expected_files)


def _stage_runtime(root, descriptor, report, open_url):
    opener = open_url or urllib.request.urlopen
    with tempfile.TemporaryDirectory(prefix='nvidia-download-', dir=root) as folder:
        staging = Path(folder)
        archive = staging / 'runtime.zip'
        received = 0
        digest = hashlib.sha256()
        parts = descriptor.get('parts') if descriptor['schema_version'] == 2 else [dict(
            url=descriptor['archive_url'], size=descriptor['archive_size'], sha256=descriptor['archive_sha256'])]
        with archive.open('wb') as output:
            for part in parts:
                request = urllib.request.Request(part['url'], headers={'User-Agent': 'TEMPO/' + VERSION})
                part_received = 0
                part_digest = hashlib.sha256()
                with opener(request, timeout=30) as response:
                    final_url = urlsplit(response.geturl())
                    if final_url.scheme != 'https' or final_url.hostname not in (
                            'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
                        raise ValueError('NVIDIA runtime download redirected outside GitHub.')
                    while block := response.read(1024 * 1024):
                        part_received += len(block)
                        if part_received > part['size']:
                            raise ValueError('NVIDIA runtime download exceeded its declared size.')
                        received += len(block)
                        digest.update(block)
                        part_digest.update(block)
                        output.write(block)
                        report(received, descriptor['archive_size'])
                if part_received != part['size'] or part_digest.hexdigest() != part['sha256']:
                    raise ValueError('NVIDIA runtime download failed its release checksum.')
        if received != descriptor['archive_size'] or digest.hexdigest() != descriptor['archive_sha256']:
            raise ValueError('NVIDIA runtime download failed its release checksum.')
        unpacked = staging / 'unpacked'
        _extract_checked(archive, unpacked)
        executable = unpacked / 'TEMPO' / 'TEMPO.exe'
        if not executable.is_file() or _digest(executable) != descriptor['executable_sha256']:
            raise ValueError('NVIDIA runtime executable failed its release checksum.')
        _probe(executable, descriptor)
        bundle = f"{descriptor['commit']}-{descriptor['archive_sha256'][:12]}"
        destination = root / bundle
        if destination.exists() and not _bundle_matches(unpacked, destination):
            # Never replace a bundle which may still be in use by the old app.
            bundle += '-' + uuid.uuid4().hex
            destination = root / bundle
        if not destination.exists():
            os.replace(unpacked, destination)
        archive_path = root / (bundle + '.zip')
        os.replace(archive, archive_path)
        marker_path = root / 'active.json'
        previous = None
        if marker_path.exists():
            if marker_path.stat().st_size > 4096:
                raise ValueError('Existing NVIDIA runtime marker is invalid.')
            previous = json.loads(marker_path.read_text(encoding='utf-8'))
        return {'schema_version': 1, 'descriptor': descriptor, 'bundle': bundle,
                'previous_marker': previous}


def _activate_runtime(root, receipt, descriptor):
    if not isinstance(receipt, dict) or receipt.get('schema_version') != 1 or receipt.get('descriptor') != descriptor:
        raise ValueError('Staged NVIDIA runtime does not match the target release.')
    bundle = receipt.get('bundle')
    prefix = f"{descriptor['commit']}-{descriptor['archive_sha256'][:12]}"
    if not isinstance(bundle, str) or not re.fullmatch(re.escape(prefix) + r'(?:-[0-9a-f]{32})?', bundle):
        raise ValueError('Staged NVIDIA runtime has an invalid path.')
    marker_path = root / 'active.json'
    current = None
    if marker_path.exists():
        if marker_path.stat().st_size > 4096:
            raise ValueError('Existing NVIDIA runtime marker is invalid.')
        current = json.loads(marker_path.read_text(encoding='utf-8'))
    if current != receipt.get('previous_marker'):
        raise ValueError('NVIDIA runtime selection changed after staging.')
    destination = root / bundle
    archive = root / (bundle + '.zip')
    if (archive.is_symlink() or not archive.is_file()
            or archive.stat().st_size != descriptor['archive_size']
            or _digest(archive) != descriptor['archive_sha256']):
        raise ValueError('Staged NVIDIA runtime archive failed its release checksum.')
    with tempfile.TemporaryDirectory(prefix='nvidia-verify-', dir=root) as folder:
        expected = Path(folder) / 'unpacked'
        _extract_checked(archive, expected)
        if (destination.is_symlink() or not _bundle_matches(expected, destination)
                or _digest(expected / descriptor['executable']) != descriptor['executable_sha256']):
            raise ValueError('Staged NVIDIA runtime inventory failed verification.')
    _atomic_json(root / 'active.json', {
        'schema_version': 1, 'version': descriptor['app_version'],
        'commit': descriptor['commit'], 'bundle': bundle,
        'executable': descriptor['executable'],
        'executable_sha256': descriptor['executable_sha256'],
        'archive_sha256': descriptor['archive_sha256'],
    })
    return True
