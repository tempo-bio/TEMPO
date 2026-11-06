"""Early, fail-closed storage preparation for packaged Windows startup.

The caller must hold the exclusive application lease until this function returns,
then hold a desktop lease for its lifetime. Older Inno applications do not honor
that lease, so their registered or default-location evidence blocks migration.
"""
import os
from pathlib import Path
import tempfile

from tempo_update_migration import _hash, _safe, inventory_legacy
from tempo_model_catalog import INSTALLABLE_MODEL_FILES
from tempo_update_storage import prepare_packaged_storage

_INNO_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{D8C8E192-3B48-4FE2-9653-532F99277A81}_is1'


class StorageBootstrapError(RuntimeError):
    """Starting could select an incomplete or independently writable store."""


def find_registered_legacy(registry=None):
    """Find the installer's exact identity in both user/machine registry views.

    Identity establishes a potential competing writer, not provenance. Missing
    keys are ordinary; inaccessible registry views must not be treated as empty.
    """
    if registry is None:
        import winreg as registry
    found = []
    for hive, name in ((registry.HKEY_CURRENT_USER, 'current user'),
                       (registry.HKEY_LOCAL_MACHINE, 'local machine')):
        for view, label in ((registry.KEY_WOW64_32KEY, '32-bit'),
                            (registry.KEY_WOW64_64KEY, '64-bit')):
            try:
                with registry.OpenKey(hive, _INNO_KEY, 0, registry.KEY_READ | view) as key:
                    try:
                        location, value_type = registry.QueryValueEx(key, 'InstallLocation')
                    except FileNotFoundError:
                        location = ''
                    if not isinstance(location, str):
                        location = ''
                    found.append({'scope': name, 'view': label, 'location': location})
            except FileNotFoundError:
                continue
            except OSError as error:
                raise StorageBootstrapError(
                    'Could not check for an older TEMPO installation. '
                    'Resolve the registry access error and try again.') from error
    return found


def _preserve_models(bundle_root, model_root):
    """Preserve installed models; copy verified local fallbacks only when absent."""
    bundle, destination = _safe(bundle_root), _safe(model_root)
    inventory = {}
    for name in sorted(INSTALLABLE_MODEL_FILES):
        source = _safe(bundle / name)
        target = _safe(destination / name)
        if target.exists():
            if not target.is_file():
                raise ValueError(f'Invalid installed model: {target}')
            inventory[name] = _hash(target)
            continue
        if not source.exists():
            continue
        if not source.is_file():
            raise ValueError(f'Invalid local model: {source}')
        expected = _hash(source)
        destination.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.tempo-model-', dir=destination)
        try:
            with os.fdopen(fd, 'wb') as out, source.open('rb') as inp:
                for chunk in iter(lambda: inp.read(1024 * 1024), b''):
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if _hash(temporary) != expected or _hash(source) != expected:
                raise OSError(f'Local model changed during copying: {source}')
            _safe(target)
            try:
                os.link(temporary, target)
            except FileExistsError:
                # A stable installation that won the race remains authoritative.
                pass
            if not _safe(target, missing_ok=False).is_file():
                raise ValueError(f'Invalid installed model: {target}')
            inventory[name] = _hash(target)
        finally:
            os.unlink(temporary)
    return inventory


def bootstrap_packaged_storage(bundle_root, profile_root, *, registry=None,
                               legacy_default_root=None, model_root=None, cancel=None):
    """Publish stable collaboration storage only when competing writers are excluded.

    The legacy registry identity is intentionally never used as authorization to
    copy files or remove software. Custom registered paths are blocked even if
    missing or unverifiable; unregistered evidence at the default location is
    also blocked. An expert must verify and migrate such installations separately.
    """
    registered = find_registered_legacy(registry)
    if registered:
        locations = sorted({item['location'] for item in registered if item['location']})
        detail = ' Detected location: ' + ', '.join(locations) + '.' if locations else ''
        raise StorageBootstrapError(
            'An older TEMPO installation is registered. Close it and use a verified '
            'migration procedure before starting this installation; its data has '
            'not been moved or deleted.' + detail)
    profile = Path(profile_root)
    legacy = Path(legacy_default_root) if legacy_default_root is not None else profile.parent / 'Programs' / 'TEMPO'
    try:
        inventory = inventory_legacy(legacy)
        evidence = ((legacy / 'unins000.exe').exists() or (legacy / 'TEMPO.exe').exists()
                    or inventory['datasets'] is not None or inventory['models'] or inventory['addons'])
    except (OSError, ValueError) as error:
        raise StorageBootstrapError('Could not inspect the older TEMPO location. '
                                    'Resolve the access problem before starting.') from error
    if evidence:
        raise StorageBootstrapError(
            f'Possible older TEMPO data or installation found at {legacy}. '
            'Its origin and active writers cannot be verified. Use a verified '
            'migration procedure before starting; the original files are unchanged.')
    try:
        if model_root is None:
            from tempo_model_catalog import installed_model_dir
            model_root = installed_model_dir()
        models = _preserve_models(bundle_root, model_root)
        result = prepare_packaged_storage(bundle_root, profile_root, cancel=cancel)
    except (OSError, ValueError) as error:
        raise StorageBootstrapError(
            'TEMPO could not prepare its collaboration storage. The existing '
            f'files were retained. Details: {error}') from error
    if result['status'] != 'complete':
        raise StorageBootstrapError(
            'TEMPO could not safely complete storage migration '
            f"({result['status']}: {result.get('conflicts', [])}). "
            'Existing files were retained; resolve the conflict before starting.')
    result['models'] = models
    return result
