"""Install the optional, release-matched NVIDIA inference bundle for one user."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
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


_HASH = re.compile(r'^[0-9a-f]{64}$')
_ASSET = re.compile(r'^https://github\.com/tempo-bio/TEMPO/releases/download/v[0-9]+\.[0-9]+\.[0-9]+/TEMPO_NVIDIA_Runtime_v[0-9]+\.[0-9]+\.[0-9]+\.zip$')
_MAX_ARCHIVE = 8 * 1024**3
_MAX_EXPANDED = 16 * 1024**3


def runtime_root():
    return Path(get_config_dir()).parent / 'nvidia-runtime'


def release_descriptor():
    """Return only the descriptor pinned into the CPU executable at build time."""
    if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
        return None
    path = Path(get_base_dir()) / 'assets' / 'nvidia-runtime.json'
    try:
        if path.stat().st_size > 4096:
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or value.get('profile') != 'nvidia-cu126'
            or value.get('app_version') != VERSION
            or not isinstance(value.get('commit'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', value['commit'])
            or value.get('commit') != build_identity().get('commit')
            or not isinstance(value.get('archive_url'), str)
            or not _ASSET.fullmatch(value['archive_url'])
            or value['archive_url'].split('/download/', 1)[-1].split('/', 1)[0] != 'v' + VERSION
            or type(value.get('archive_size')) is not int
            or not 0 < value['archive_size'] <= _MAX_ARCHIVE
            or value.get('executable') != 'TEMPO/TEMPO.exe'
            or not all(isinstance(value.get(key), str) and _HASH.fullmatch(value[key])
                       for key in ('archive_sha256', 'executable_sha256'))):
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


def install_nvidia_runtime(report=lambda received, total: None, *, descriptor=None, open_url=None):
    """Download, check, probe and then atomically select the optional worker."""
    descriptor = descriptor or release_descriptor()
    if descriptor is None:
        raise RuntimeError('This build does not include an NVIDIA runtime download.')
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    from tempo_annotation_store import session_lock
    try:
        with session_lock(root/'install'):
            return _install_runtime(root, descriptor, report, open_url)
    except BlockingIOError:
        raise RuntimeError('Another TEMPO instance is installing NVIDIA support. Wait for it to finish, then retry.') from None


def _bundle_matches(expected, installed):
    if not installed.is_dir() or any(path.is_symlink() for path in installed.rglob('*')):
        return False
    expected_files = {path.relative_to(expected) for path in expected.rglob('*') if path.is_file()}
    actual_files = {path.relative_to(installed) for path in installed.rglob('*') if path.is_file()}
    return expected_files == actual_files and all(
        (expected/name).stat().st_size == (installed/name).stat().st_size
        and _digest(expected/name) == _digest(installed/name) for name in expected_files)


def _install_runtime(root, descriptor, report, open_url):
    opener = open_url or urllib.request.urlopen
    with tempfile.TemporaryDirectory(prefix='nvidia-download-', dir=root) as folder:
        staging = Path(folder)
        archive = staging / 'runtime.zip'
        request = urllib.request.Request(descriptor['archive_url'], headers={'User-Agent': 'TEMPO/' + VERSION})
        received = 0
        digest = hashlib.sha256()
        with opener(request, timeout=30) as response, archive.open('wb') as output:
            final_url = urlsplit(response.geturl())
            if final_url.scheme != 'https' or final_url.hostname not in (
                    'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
                raise ValueError('NVIDIA runtime download redirected outside GitHub.')
            while block := response.read(1024 * 1024):
                received += len(block)
                if received > descriptor['archive_size']:
                    raise ValueError('NVIDIA runtime download exceeded its declared size.')
                digest.update(block)
                output.write(block)
                report(received, descriptor['archive_size'])
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
        backup = None
        if destination.exists():
            if not _bundle_matches(unpacked, destination):
                backup = root/(bundle+'.repair-backup-'+uuid.uuid4().hex)
                os.replace(destination, backup)
                try:
                    os.replace(unpacked, destination)
                except OSError as error:
                    try:
                        os.replace(backup, destination)
                    except OSError:
                        raise RuntimeError(f'NVIDIA repair could not restore the previous folder. Recovery copy: {backup}') from error
                    raise RuntimeError('NVIDIA repair could not replace files. Close other TEMPO instances and retry.') from error
        else:
            os.replace(unpacked, destination)
        _atomic_json(root / 'active.json', {
            'schema_version': 1, 'version': descriptor['app_version'],
            'commit': descriptor['commit'], 'bundle': bundle,
            'executable': descriptor['executable'],
            'executable_sha256': descriptor['executable_sha256'],
            'archive_sha256': descriptor['archive_sha256'],
        })
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
    return True
