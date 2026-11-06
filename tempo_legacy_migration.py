"""Explicit prepare/finish migration from reviewed Inno installations.

Preparation leaves the old installation and active storage unchanged. The user
removes the old installation through Windows Settings only after preparation
returns successfully. Finish requires its registration and executable to be
gone, then publishes the verified external copies. No legacy code is executed.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from tempo_model_catalog import INSTALLABLE_MODEL_FILES
from tempo_update_lock import application_lock, startup_guard
from tempo_update_migration import _atomic_json, _hash, _safe, migrate_collaboration, inventory_legacy
from tempo_update_storage import _inventory_store, _read_pointer, prepare_packaged_storage

FORMAT = 'tempo-legacy-upgrade-v1'
MAX_RECORD = 32 * 1024 * 1024
SUPPORTED_LEGACY = {
    '1.0.1': {
        'commit': 'b386e96984c581bf8e8afd4679cd1500552386bc',
        'installer_sha256': '5f462548ef2c086140c91b207c6b67679f45b72f35f4432dee727107536381a4',
        'executable_sha256': 'f9daa9029e7949ec052853abe6c0d2d59c664c2b36e1fc2155f59913e5fdb030',
        'provenance_sha256': 'afb218ece211f40c7bd644841e3db45806769a2ff28c595b8e074257ac048ebe',
    },
}
# Exact descriptor bytes extracted from the reviewed published CPU installer.
# Keep this separate from the existing CPU identity to preserve pending journals.
SUPPORTED_LEGACY_NVIDIA = {
    '1.0.1': '4cd48cb0b5dc40f0fb59ff8fa66bcda8c46b68d147b46f4ab326817ffc6d369a',
}


def _read(path):
    path = _safe(path, missing_ok=False)
    if not path.is_file() or path.stat().st_size > MAX_RECORD:
        raise ValueError('Invalid legacy migration record.')
    def unique(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate legacy migration metadata key.')
            value[key] = item
        return value
    return json.loads(path.read_bytes(), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid migration JSON constant.')))


def _transaction(profile):
    return _safe(Path(profile) / 'updates' / 'legacy-migration')


def _footprint(root):
    value = _inventory_store(root, None)
    return json.loads(json.dumps(value, sort_keys=True))


def _mutable(root):
    result = {}
    if (root / 'datasets').exists():
        result['datasets'] = _footprint(root / 'datasets')
    for name in sorted(INSTALLABLE_MODEL_FILES):
        path = _safe(root / name)
        if path.exists():
            if not path.is_file():
                raise ValueError('Invalid legacy model: ' + str(path))
            result[name] = [path.stat().st_size, _hash(path)]
    return result


def _identify(root, supported):
    exe = _safe(root / 'TEMPO.exe', missing_ok=False)
    provenance = _safe(root / '_internal' / 'assets' / 'build-provenance.json', missing_ok=False)
    identity = (_hash(exe), _hash(provenance))
    matches = [(version, item) for version, item in supported.items()
               if identity == (item['executable_sha256'], item['provenance_sha256'])]
    if len(matches) != 1:
        raise ValueError('This legacy executable and provenance are not a reviewed migration source.')
    version, item = matches[0]
    if _read(provenance).get('commit') != item['commit']:
        raise ValueError('Legacy source commit differs from reviewed provenance.')
    return version, item


@contextmanager
def legacy_writer_guard(root, executable_sha256=None, *, hash_executables=False):
    """Deny new executable opens while excluding already-running legacy writers.

    This is Windows-only. Denying executable reads prevents another legacy
    instance from starting during the copy, despite not knowing our app lease.
    Only query access is requested for process inspection; no process is killed.
    """
    if os.name != 'nt':
        raise RuntimeError('Legacy installation migration must run on Windows.')
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                 wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD)]
    psapi.EnumProcesses.restype = wintypes.BOOL
    handles = []
    locked_files = {}
    root = _safe(root)
    try:
        for directory, dirs, files in os.walk(root, followlinks=False,
                onerror=lambda error: (_ for _ in ()).throw(error)):
            for name in dirs + files:
                _safe(Path(directory) / name, missing_ok=False)
            for name in files:
                path = Path(directory) / name
                if path.suffix.lower() != '.exe':
                    continue
                handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
                if handle == wintypes.HANDLE(-1).value:
                    raise BlockingIOError('Close the old TEMPO installation and its workers before migrating.')
                handles.append(handle)
                if hash_executables or (path == root / 'TEMPO.exe' and executable_sha256 is not None):
                    digest, buffer = hashlib.sha256(), ctypes.create_string_buffer(1024 * 1024)
                    count = wintypes.DWORD()
                    size = 0
                    while True:
                        if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
                            raise ctypes.WinError(ctypes.get_last_error())
                        if not count.value:
                            break
                        size += count.value
                        digest.update(buffer.raw[:count.value])
                    locked_files[path.relative_to(root).as_posix()] = [size, digest.hexdigest()]
                    if path == root / 'TEMPO.exe' and executable_sha256 is not None and digest.hexdigest() != executable_sha256:
                        raise ValueError('Legacy executable changed before migration acquired its lock.')
        # EnumProcesses truncates silently if the buffer is full; grow until complete.
        capacity = 4096
        while True:
            pids, count = (wintypes.DWORD * capacity)(), wintypes.DWORD()
            if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(count)):
                raise ctypes.WinError(ctypes.get_last_error())
            if count.value < ctypes.sizeof(pids):
                break
            capacity *= 2
            if capacity > 262144:
                raise RuntimeError('Process inspection exceeded its limit.')
        canonical = os.path.normcase(os.path.realpath(root))
        for pid in pids[:count.value // ctypes.sizeof(wintypes.DWORD)]:
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:  # Other users' protected/system processes are not package writers.
                continue
            try:
                buffer, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                    image = os.path.normcase(os.path.realpath(buffer.value))
                    if image == canonical or image.startswith(canonical + os.sep):
                        raise BlockingIOError('The old TEMPO installation still has a running process. Close it before migrating.')
            finally:
                kernel.CloseHandle(handle)
        yield locked_files
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def _registrations(root, registry, *, retired=False):
    from tempo_update_bootstrap import find_registered_legacy
    entries = find_registered_legacy(registry)
    if retired and entries:
        raise ValueError('The old installation is still registered. Remove it through Windows Settings before finishing migration.')
    if not retired and not entries:
        raise ValueError('This old installation is not registered. Keep its files and review it as an orphaned installation before migration.')
    canonical = lambda path: os.path.normcase(os.path.realpath(_safe(path)))
    if any(not item['location'] or canonical(item['location']) != canonical(root) for item in entries):
        raise ValueError('Multiple or unresolved old installations require review before migration.')


def _validate_target(folder, keys, target_root, *, offline=False):
    from tempo_update_recovery import _validated_release, _verify_app
    from tempo_update_manifest import verify_zip
    envelope = _read(folder / 'target.json')
    channel = envelope.get('signed', {}).get('channel')
    release = _validated_release((folder / 'target.json').read_bytes(), keys, channel, offline=offline)
    verify_zip(folder / 'target.nupkg', release.inventory)
    _verify_app(target_root / 'current', release)
    return release


def _record(profile, supported):
    folder = _transaction(profile)
    record = _read(folder / 'migration.json')
    fields = {'format', 'legacy_root', 'target_root', 'version', 'identity', 'stores', 'selected', 'model_root'}
    if (not isinstance(record, dict) or set(record) not in (fields, fields | {'nvidia'}) or record['format'] != FORMAT
            or not isinstance(record['version'], str) or not isinstance(record['identity'], dict)
            or not isinstance(record['stores'], dict)
            or not all(isinstance(store, dict) for store in record['stores'].values())
            or not isinstance(record['model_root'], str)
            or (record['selected'] is not None and not isinstance(record['selected'], dict))
            or supported.get(record['version']) != record['identity']
            or set(record['stores']) != {'flat', 'internal'}):
        raise ValueError('Invalid or unreviewed legacy migration record.')
    root, target = _safe(record['legacy_root']), _safe(record['target_root'])
    if target != _safe(Path(profile).parent / 'TEMPO.Bio.Desktop'):
        raise ValueError('Migration target is not the supported per-user installation.')
    if root == target or root in target.parents or target in root.parents:
        raise ValueError('Legacy and target application locations overlap.')
    if root == Path(profile) or root in Path(profile).parents or Path(profile) in root.parents:
        raise ValueError('Legacy installation overlaps personal storage.')
    return folder, record, root, target


def _legacy_nvidia_descriptor(path, version, identity, supported_nvidia):
    from tempo_nvidia_manifest import validate_descriptor
    expected = supported_nvidia.get(version)
    if expected is None or _hash(_safe(path, missing_ok=False)) != expected:
        raise ValueError('The old NVIDIA descriptor is not a reviewed migration source. Keep the old installation.')
    value = _read(path)
    validate_descriptor(value, version=version, commit=identity['commit'], allow_legacy=False)
    return value


@contextmanager
def _prepare_nvidia(root, profile, release, package, version, identity, *, enabled, supported_nvidia):
    """Probe before the exclusive app lease; retain locks on old external workers.

    The target probe is itself a modern worker with a shared application lease.
    Holding startup/exclusive leases across it would prevent it from starting.
    Legacy main/worker executable guards exclude writers which lack that lease.
    """
    from tempo_update_components import (read_runtime_marker, _valid_marker, _verify_retained,
                                         packaged_descriptor, verify_staged_runtime)
    from tempo_annotation_store import session_lock
    runtime = _safe(profile / 'nvidia-runtime')
    marker = read_runtime_marker(runtime)
    if marker is None:
        yield None
        return
    if not enabled:
        raise ValueError('This installation needs matching NVIDIA migration. Keep the old installation, or prepare its matching worker with --prepare-nvidia.')
    source_path = root / '_internal' / 'assets' / 'nvidia-runtime.json'
    source = _legacy_nvidia_descriptor(source_path, version, identity, supported_nvidia)
    if not _valid_marker(marker, source):
        raise ValueError('Existing NVIDIA selection does not match the reviewed old release. Keep the old installation.')
    target = packaged_descriptor(release, package)
    with legacy_writer_guard(runtime / marker['bundle'], hash_executables=True) as locked:
        with startup_guard(profile), application_lock(profile):
            pass  # Exclude an active modern app before downloading either pair.
        from tempo_nvidia_setup import runtime_root, retain_nvidia_archive
        if _safe(runtime_root()) != runtime:
            raise ValueError('NVIDIA staging does not belong to this personal profile.')
        # The published v1.0.1 installer discarded its downloaded ZIP. Obtain
        # the pinned old archive when absent so every old library is verifiable
        # and an offline old pair remains available after uninstall.
        retain_nvidia_archive(source, marker)
        _verify_retained(runtime, marker, source, locked_files=locked)
        cached = _transaction(profile) / 'migration.json'
        receipt = None
        if cached.exists():
            previous = _read(cached).get('nvidia')
            if isinstance(previous, dict):
                receipt = previous.get('receipt')
        if receipt is None:
            from tempo_nvidia_setup import stage_nvidia_runtime
            receipt = stage_nvidia_runtime(target, target_version=release.version, target_commit=release.commit)
        with session_lock(runtime / 'install'):
            # A concurrent installer or a changed cached receipt cannot replace
            # the captured old/target pair or authorize retirement.
            _verify_retained(runtime, marker, source, locked_files=locked)
            verify_staged_runtime(receipt, target, runtime_root=runtime, previous_marker=marker)
            yield dict(legacy_descriptor_sha256=supported_nvidia[version],
                       previous_marker=marker, receipt=receipt)


@contextmanager
def _finish_nvidia(folder, record, release, profile, supported_nvidia):
    from tempo_update_components import (read_runtime_marker, _valid_marker, _verify_retained,
                                         packaged_descriptor, verify_staged_runtime)
    from tempo_annotation_store import session_lock
    runtime = _safe(profile / 'nvidia-runtime')
    component = record.get('nvidia')
    if component is None:
        if read_runtime_marker(runtime) is not None:
            raise ValueError('NVIDIA selection changed; component migration requires review.')
        yield lambda: None
        return
    if (not isinstance(component, dict) or set(component) != {'legacy_descriptor_sha256', 'previous_marker', 'receipt'}
            or component['legacy_descriptor_sha256'] != supported_nvidia.get(record['version'])):
        raise ValueError('Invalid legacy NVIDIA migration record.')
    source = _legacy_nvidia_descriptor(folder / 'legacy-nvidia-descriptor.json', record['version'],
                                       record['identity'], supported_nvidia)
    marker, receipt = component['previous_marker'], component['receipt']
    if not _valid_marker(marker, source):
        raise ValueError('Retained NVIDIA selection differs from the reviewed old release.')
    target = packaged_descriptor(release, folder / 'target.nupkg')
    with legacy_writer_guard(runtime / marker['bundle'], hash_executables=True) as locked, session_lock(runtime / 'install'):
        _verify_retained(runtime, marker, source, locked_files=locked)
        selected = verify_staged_runtime(receipt, target, runtime_root=runtime,
                                          previous_marker=marker, allow_activated=True)
        def activate():
            if read_runtime_marker(runtime) != selected:
                _atomic_json(runtime / 'active.json', selected)
        yield activate


def completed_migration(profile, legacy_root=None):
    """Read a compact completion proof; never use it to authorize old code."""
    folder = _transaction(profile)
    if not (folder / 'completed.json').exists():
        return False
    value = _read(folder / 'completed.json')
    if (not isinstance(value, dict) or set(value) != {'format', 'legacy_root', 'record_sha256'}
            or value['format'] != FORMAT or _hash(_safe(folder / 'migration.json')) != value['record_sha256']):
        raise ValueError('Legacy migration completion proof is invalid.')
    root = _safe(value['legacy_root'])
    if _read_pointer(profile) is None:
        raise ValueError('Published legacy collaboration storage is unavailable. Finish migration before starting.')
    return (legacy_root is None or root == _safe(legacy_root)) and not (root / 'TEMPO.exe').exists()


def _expected_collaboration(record, target):
    inventories = [item['datasets'] for item in record['stores'].values() if 'datasets' in item]
    for path in inventory_legacy(target / 'current' / '_internal')['dataset_roots']:
        inventories.append(_footprint(Path(path)))
    if inventories and any(value != inventories[0] for value in inventories[1:]):
        raise ValueError('Legacy collaboration stores differ. Review both copies before uninstalling.')
    models = {}
    for store in record['stores'].values():
        for name, value in store.items():
            if name != 'datasets':
                if name in models and models[name] != value:
                    raise ValueError('Legacy model locations contain different models. Review both copies before uninstalling.')
                models[name] = value
    return inventories[0] if inventories else {}


def _check_destination(profile, expected):
    published = _read_pointer(profile)
    destination = _safe(profile / 'collaboration')
    if not destination.exists():
        return
    actual = _footprint(destination)
    if ((published is not None and actual != expected)
            or any(expected.get(name) != value for name, value in actual.items())):
        raise ValueError('Existing collaboration work differs from the prepared migration. No active data was replaced.')


def _model_destination(profile):
    from tempo_model_catalog import installed_model_dir
    from tempo_config import tempo_preferences
    if getattr(tempo_preferences, 'load_error', ''):
        raise ValueError('Resolve the personal preferences load error before migrating model locations.')
    root = _safe(installed_model_dir())
    if _safe(profile / 'models') not in root.parents:
        raise ValueError('Model storage does not belong to this personal profile.')
    return root


def _relocate_dataset_references(selected, legacy_root, profile, footprint, model_paths):
    """Remap exact references to verified migrated files, preserving external paths.

    Raw snapshots remain untouched. JSON values and the generated YOLO YAML
    `path` field are the supported persisted reference formats. Absolute JSON
    identifiers or unsupported YAML references require review before uninstall.
    """
    roots = list(dict.fromkeys([legacy_root / 'datasets', legacy_root / '_internal' / 'datasets',
                              Path(os.path.realpath(legacy_root)) / 'datasets',
                              Path(os.path.realpath(legacy_root)) / '_internal' / 'datasets']))
    names = {os.path.normcase(os.path.normpath(name)): name for name in footprint}
    def mapped(value):
        if not isinstance(value, str) or not os.path.isabs(value):
            return value
        normalized = os.path.normcase(os.path.normpath(value))
        if normalized in model_paths:
            return str(model_paths[normalized])
        for source in roots:
            prefix = os.path.normcase(os.path.normpath(source))
            if normalized == prefix or normalized.startswith(prefix + os.sep):
                relative = os.path.relpath(os.path.normpath(value), source)
                key = names.get(os.path.normcase(os.path.normpath(relative)))
                if relative != '.' and key is None:
                    raise ValueError('A saved dataset reference has no verified migrated file: ' + value)
                return str(profile / 'collaboration' / (key or '.'))
        return value
    def transform(value):
        if isinstance(value, str):
            return mapped(value)
        if isinstance(value, list):
            return [transform(item) for item in value]
        if isinstance(value, dict):
            if any(mapped(key) != key for key in value):
                raise ValueError('A legacy JSON file uses absolute dataset paths as identifiers. Review it before migration.')
            return {key: transform(item) for key, item in value.items()}
        return value
    from tempo_annotation_store import atomic_bytes
    for path in selected.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in ('.json', '.yaml', '.yml'):
            continue
        if path.stat().st_size > MAX_RECORD:
            raise ValueError('A dataset metadata file exceeds the supported migration size: ' + str(path))
        if path.suffix.lower() == '.json':
            # Scientific records retain the legacy JSON reader's numeric
            # behavior, including existing NaN values; migration metadata is
            # parsed separately with the strict reader above.
            original = json.loads(path.read_bytes())
            updated = transform(original)
            if updated != original:
                atomic_bytes(path, json.dumps(updated, indent=2, ensure_ascii=False).encode('utf-8'))
        else:
            lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
            updated = []
            for line in lines:
                if path.name == 'data.yaml' and line.startswith('path:'):
                    raw = line[5:].strip()
                    try:
                        value = json.loads(raw)
                    except json.JSONDecodeError:
                        value = raw
                    replacement = mapped(value)
                    if replacement != value:
                        line = 'path: ' + json.dumps(replacement, ensure_ascii=False) + '\n'
                # Other absolute application-store YAML references are not
                # rewritten by guessing a schema.
                variants = [str(root) for root in roots] + [str(root).replace('\\', '/') for root in roots]
                if any(variant.casefold() in line.casefold() or
                       json.dumps(variant)[1:-1].casefold() in line.casefold() for variant in variants):
                    raise ValueError('Unsupported absolute dataset reference in YAML: ' + str(path))
                updated.append(line)
            if updated != lines:
                atomic_bytes(path, ''.join(updated).encode('utf-8'))


def prepare_legacy_migration(root, profile, installer, release, package, target_root, *,
                             trusted_keys, registry=None, supported=SUPPORTED_LEGACY,
                             prepare_nvidia=False, supported_nvidia=SUPPORTED_LEGACY_NVIDIA):
    """Stage verified personal data and offline installers; never retire software."""
    from tempo_update_recovery import _store_copy, _validated_release, _verify_app
    from tempo_update_manifest import verify_zip
    root, profile, target = _safe(root), _safe(profile), _safe(target_root)
    if target != _safe(profile.parent / 'TEMPO.Bio.Desktop'):
        raise ValueError('Migration target is not the supported per-user installation.')
    if (root == profile or root in profile.parents or profile in root.parents
            or root == target or root in target.parents or target in root.parents):
        raise ValueError('Migration locations overlap.')
    if inventory_legacy(root)['addons']:
        raise ValueError('Remove the optional package using its verified procedure or defer migration. Its records were retained.')
    version, identity = _identify(root, supported)
    installer = _safe(installer, missing_ok=False)
    if _hash(installer) != identity['installer_sha256']:
        raise ValueError('Keep the exact reviewed old installer available for offline recovery.')
    checked = _validated_release(release.envelope, trusted_keys, release.channel)
    if checked != release:
        raise ValueError('Target release differs from its signed envelope.')
    verify_zip(package, release.inventory)
    _verify_app(target / 'current', release)
    _registrations(root, registry)
    with legacy_writer_guard(root, identity['executable_sha256']), \
            _prepare_nvidia(root, profile, release, package, version, identity,
                            enabled=prepare_nvidia, supported_nvidia=supported_nvidia) as nvidia, \
            startup_guard(profile), application_lock(profile):
        _registrations(root, registry)
        inventory = inventory_legacy(root)
        if inventory['addons']:
            raise ValueError('Remove the optional package using its verified procedure or defer migration. Its records were retained.')
        folder = _transaction(profile)
        folder.mkdir(parents=True, exist_ok=True)
        stores = {label: _mutable(source) for label, source in
                  (('flat', root), ('internal', root / '_internal'))}
        record = dict(format=FORMAT, legacy_root=str(root), target_root=str(target),
                      version=version, identity=identity, stores=stores, selected=None,
                      model_root=str(_model_destination(profile)))
        if nvidia is not None:
            record['nvidia'] = nvidia
        if len(json.dumps(record).encode('utf-8')) > MAX_RECORD:
            raise ValueError('Migration inventory exceeds its supported size. Keep the old installation.')
        expected_collaboration = _expected_collaboration(record, target)
        if (folder / 'migration.json').exists():
            previous = _read(folder / 'migration.json')
            if {**previous, 'selected': None} != record:
                raise ValueError('Old data changed after preparation. Both copies were retained; review the existing migration before retrying.')
            if previous.get('selected') is not None and (folder / 'prepared.json').exists():
                proof = dict(format=FORMAT, legacy_root=str(root), record_sha256=_hash(folder / 'migration.json'))
                if (_read(folder / 'prepared.json') == proof and
                        all(_mutable(folder / label) == store for label, store in stores.items()) and
                        _footprint(folder / 'selected' / 'datasets') == previous['selected'] and
                        (folder / 'target.json').read_bytes() == release.envelope and
                        _hash(folder / 'legacy-installer.exe') == identity['installer_sha256']):
                    _validate_target(folder, trusted_keys, target)
                    if nvidia is not None:
                        _legacy_nvidia_descriptor(folder / 'legacy-nvidia-descriptor.json', version, identity, supported_nvidia)
                    _check_destination(profile, previous['selected'])
                    return {'status': 'prepared', 'record': str(folder / 'migration.json'),
                            'next_action': 'Remove the old installation through Windows Settings, then finish migration.'}
        required = installer.stat().st_size + Path(package).stat().st_size
        for source in (root, root / '_internal'):
            for name in _mutable(source):
                path = source / name
                required += 3 * (sum(p.stat().st_size for p in path.rglob('*') if p.is_file())
                                 if path.is_dir() else path.stat().st_size)
        if shutil.disk_usage(profile).free < required:
            raise OSError('Insufficient space for verified migration copies and offline recovery.')
        _atomic_json(folder / 'migration.json', record)
        _store_copy(installer, folder / 'legacy-installer.exe')
        _store_copy(package, folder / 'target.nupkg')
        if nvidia is not None:
            _store_copy(root / '_internal' / 'assets' / 'nvidia-runtime.json', folder / 'legacy-nvidia-descriptor.json')
        if (folder / 'target.json').exists() and (folder / 'target.json').read_bytes() != release.envelope:
            raise ValueError('A different target was already prepared.')
        from tempo_annotation_store import atomic_bytes
        atomic_bytes(folder / 'target.json', release.envelope)
        for label, source in (('flat', root), ('internal', root / '_internal')):
            snapshot = _safe(folder / label)
            snapshot.mkdir(exist_ok=True)
            if 'datasets' in stores[label]:
                result = migrate_collaboration(source / 'datasets', snapshot / 'datasets')
                if result['status'] != 'complete':
                    raise ValueError('Legacy snapshot differs from a previous attempt. Original files were retained.')
            for name in stores[label]:
                if name != 'datasets':
                    _store_copy(source / name, snapshot / name)
            if _mutable(source) != stores[label] or _mutable(snapshot) != stores[label]:
                raise ValueError('Legacy data changed during preparation. Keep the old installation.')
        selected = _safe(folder / 'selected')
        if selected.exists():
            # A partial derived copy may contain edits; retain it rather than
            # overwriting it when resuming from the immutable raw snapshots.
            os.rename(selected, folder / ('selected-interrupted-' + uuid.uuid4().hex))
        (selected / 'datasets').mkdir(parents=True)
        for source in (folder / 'flat' / 'datasets', folder / 'internal' / 'datasets',
                       target / 'current' / '_internal' / 'datasets'):
            if source.exists():
                result = migrate_collaboration(source, selected / 'datasets')
                if result['status'] != 'complete':
                    raise ValueError('Legacy stores conflict; review them before uninstalling.')
        if _footprint(selected / 'datasets') != expected_collaboration:
            raise ValueError('Prepared collaboration copy differs from its verified sources.')
        models = {os.path.normcase(os.path.normpath(candidate)): folder / label / name
                  for label, source in (('flat', root), ('internal', root / '_internal'))
                  for name in stores[label] if name != 'datasets'
                  for candidate in (source / name, Path(os.path.realpath(source / name)))}
        _relocate_dataset_references(selected / 'datasets', root, profile, expected_collaboration, models)
        record['selected'] = _footprint(selected / 'datasets')
        if len(json.dumps(record).encode('utf-8')) > MAX_RECORD:
            raise ValueError('Prepared migration inventory exceeds its supported size. Keep the old installation.')
        _check_destination(profile, record['selected'])
        _atomic_json(folder / 'migration.json', record)
        _atomic_json(folder / 'prepared.json', dict(format=FORMAT, legacy_root=str(root),
                     record_sha256=_hash(folder / 'migration.json')))
        return {'status': 'prepared', 'record': str(folder / 'migration.json'),
                'next_action': 'Remove the old installation through Windows Settings, then finish migration.'}


def finish_legacy_migration(profile, *, trusted_keys, registry=None, supported=SUPPORTED_LEGACY,
                            supported_nvidia=SUPPORTED_LEGACY_NVIDIA):
    """Publish only after legacy retirement; leave snapshots and installers intact."""
    from tempo_update_bootstrap import _preserve_models
    profile = _safe(profile)
    with startup_guard(profile), application_lock(profile):
        folder, record, root, target = _record(profile, supported)
        _registrations(root, registry, retired=True)
        if (root / 'TEMPO.exe').exists():
            raise ValueError('The old executable is still present. Finish removing that installation before publishing migrated data.')
        if completed_migration(profile):
            return {'status': 'complete', 'already_published': True}
        prepared = _read(folder / 'prepared.json')
        if prepared != dict(format=FORMAT, legacy_root=str(root), record_sha256=_hash(folder / 'migration.json')):
            raise ValueError('Legacy preparation is incomplete or changed. Do not publish its data.')
        if _hash(folder / 'legacy-installer.exe') != record['identity']['installer_sha256']:
            raise ValueError('Offline legacy installer failed verification.')
        release = _validate_target(folder, trusted_keys, target, offline=True)
        _expected_collaboration(record, target)
        expected_collaboration = record['selected']
        if not isinstance(expected_collaboration, dict) or _footprint(folder / 'selected' / 'datasets') != expected_collaboration:
            raise ValueError('The prepared collaboration copy changed. Original snapshots were retained.')
        with legacy_writer_guard(root), _finish_nvidia(folder, record, release, profile, supported_nvidia) as activate_nvidia:
            for label, source in (('flat', root), ('internal', root / '_internal')):
                if _mutable(folder / label) != record['stores'][label]:
                    raise ValueError('A migration snapshot changed. The original installation backup was retained.')
                # An uninstaller may remove original files; surviving files must match.
                for name, value in _mutable(source).items():
                    expected = record['stores'][label].get(name)
                    if name == 'datasets' and expected is not None:
                        if any(expected.get(key) != item for key, item in value.items()):
                            raise ValueError('Old collaboration data changed after preparation; review both copies.')
                    elif expected != value:
                        raise ValueError('Old local models changed after preparation; review both copies.')
            snapshots = [folder / 'flat', folder / 'internal']
            _check_destination(profile, expected_collaboration)
            model_root = _model_destination(profile)
            if str(model_root) != record['model_root']:
                raise ValueError('The selected model repository changed after preparation. Review both model locations before finishing.')
            for snapshot in snapshots:
                _preserve_models(snapshot, model_root)
            # Publish collaboration last. A stopped activation/publication is
            # pending, blocks desktop startup, and is resumable with both pairs.
            activate_nvidia()
            result = prepare_packaged_storage(folder / 'selected', profile)
            if result['status'] != 'complete':
                raise ValueError('Migration publication has a data conflict. Verified snapshots were retained.')
            _atomic_json(folder / 'completed.json', dict(format=FORMAT, legacy_root=str(root),
                         record_sha256=_hash(folder / 'migration.json')))
            return {'status': 'complete', 'already_published': False}
