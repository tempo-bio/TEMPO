"""Read-only overlay assessment and non-destructive pre-removal snapshots.

A local receipt never authorizes replacement or removal. Only the reviewed
published overlay contract can establish that its original removal procedure
applies. This module never executes a package, changes firmware or removes files.
"""
from pathlib import Path
import shutil
import uuid

from tempo_legacy_migration import SUPPORTED_LEGACY, _read
from tempo_update_migration import _safe, _hash, _atomic_json, migrate_collaboration
from tempo_update_storage import _inventory_store

REVIEWED_OVERLAY = {
    'package': 'stimulation-investigation-1', 'version': '0.1.0',
    'archive_sha256': '86420ab3d47f8ecb8488c9b8aa81029a39c13e9cfe1d72ea52f30b3cbfbdfcda',
    'source_sha256': '3306e392812679ec810ace21a1fa29247e2a54c7db862936ac3f9c4c63b38b50',
    'provenance_sha256': SUPPORTED_LEGACY['1.0.1']['provenance_sha256'],
    'files': [
        dict(path='TEMPO.exe', before_sha256=SUPPORTED_LEGACY['1.0.1']['executable_sha256'],
             after_sha256='2385cf29a7726946ef7a94f5cc4cb19e2ac90d771abb0cdfa42db53cc4fe8d75', size=92990155),
        dict(path='_internal/firmware/tempo_pacer/tempo_pacer.ino',
             before_sha256='6772deb09b78bf565a798311301762dd5cf48c66763e5d4ab50e03a9cefbe5cc',
             after_sha256='60e4bfc7bfd3203250573e4de138313d1e630b356ad0c730d31e96fd27fc8f6d', size=9671),
        dict(path='_internal/firmware/tempo_pacer/investigation_1.h', before_sha256=None,
             after_sha256='e6fdc2757a117b6c3f06ab10c80f67e429a2681d397356633d68a72d84dbe31a', size=5301),
        dict(path='_internal/docs/STIMULATION_INVESTIGATION_1.md', before_sha256=None,
             after_sha256='1f8db1524a4f8429170a1ad4dedd8b1e46f763a3de3900d1955d583cb3cfe0ad', size=8878),
    ],
}


def _digest_if_present(path):
    path = _safe(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError('Package recovery path is not a file: ' + str(path))
    return _hash(path)


def reviewed_base_restored(install_root):
    """Recognize the empty parent left by the reviewed original package remover."""
    root = _safe(install_root)
    try:
        if _digest_if_present(root / '_internal/assets/build-provenance.json') != REVIEWED_OVERLAY['provenance_sha256']:
            return False
        return all(_digest_if_present(root / item['path']) == item['before_sha256'] for item in REVIEWED_OVERLAY['files'])
    except (OSError, ValueError):
        return False


def _assess_known(root, package_root):
    review = REVIEWED_OVERLAY
    result = dict(path=str(package_root), package=review['package'], version=review['version'],
                  status='requires_review', reason='Unrecognized or incomplete receipt.',
                  release_compatible=False, removal_available=False,
                  published_archive_sha256=review['archive_sha256'])
    try:
        receipt = _read(package_root / 'receipt.json')
        if (not isinstance(receipt, dict) or type(receipt.get('schema')) is not int or receipt['schema'] != 1
                or receipt.get('package') != review['package'] or receipt.get('version') != review['version']
                or receipt.get('source_sha256') != review['source_sha256']
                or receipt.get('files') != review['files'] or receipt.get('state') not in
                ('installed', 'installing', 'rolled-back', 'removing')):
            return result
        if (_digest_if_present(root / '_internal/assets/build-provenance.json') != review['provenance_sha256']):
            result['reason'] = 'The base installation differs from the reviewed v1.0.1 source.'
            return result
        for item in review['files']:
            current = _digest_if_present(root / item['path'])
            allowed = {item['after_sha256']}
            if receipt['state'] != 'installed':
                allowed.add(item['before_sha256'])
            if current not in allowed:
                result['reason'] = 'Application files changed or removal is incomplete: ' + item['path']
                return result
            if item['before_sha256'] is not None and _digest_if_present(package_root / 'backup' / item['path']) != item['before_sha256']:
                result['reason'] = 'An original package backup is missing or changed: ' + item['path']
                return result
        result.update(status='removal_required', removal_available=True, receipt_state=receipt['state'],
                      reason='Snapshot recovery material, then use the original verified package remover or defer migration.')
    except (OSError, ValueError):
        # Unreadable state remains a blocker. Its raw evidence may still be
        # retained by the snapshot command, or an access error reported there.
        pass
    return result


def assess_addons(install_root):
    """Distinguish a reviewed removal route from modified/unknown/partial state."""
    root = _safe(install_root)
    result = []
    for label, layout in (('flat', root), ('internal', root / '_internal')):
        packages = _safe(layout / '.tempo-packages')
        if not packages.exists():
            continue
        if not packages.is_dir():
            raise ValueError('Invalid optional-package directory: ' + str(packages))
        children = sorted(packages.iterdir())
        if not children:
            restored = label == 'flat' and reviewed_base_restored(root)
            result.append(dict(path=str(packages), status='base_restored' if restored else 'requires_review',
                               reason='Reviewed original base restored.' if restored else 'Empty package recovery directory.',
                               release_compatible=restored, removal_available=False))
        for child in children:
            child = _safe(child)
            if label == 'flat' and child.is_dir() and child.name == REVIEWED_OVERLAY['package']:
                result.append(_assess_known(root, child))
            else:
                result.append(dict(path=str(child), status='requires_review', reason='Unknown package or historical layout.',
                                   release_compatible=False, removal_available=False))
    return result


def snapshot_addons(install_root, profile_root):
    """Retain all receipt/backup trees and reviewed overlay files outside uninstall.

    Copying is not removal authorization. Unknown receipts cannot name arbitrary
    paths to read. Every copied tree is verified against before/after inventories.
    A failed copy has no completion marker and remains available for inspection.
    """
    root, profile = _safe(install_root), _safe(profile_root)
    if root == profile or root in profile.parents or profile in root.parents:
        raise ValueError('Package recovery snapshots must be outside the installation.')
    assessment = assess_addons(root)
    if not assessment:
        return dict(status='absent', snapshots=[])
    sources = {}
    for label, layout in (('flat', root), ('internal', root / '_internal')):
        packages = _safe(layout / '.tempo-packages')
        if packages.exists():
            sources[label] = (packages, _inventory_store(packages, None))
    current = {}
    if (root / '.tempo-packages' / REVIEWED_OVERLAY['package']).exists():
        for item in REVIEWED_OVERLAY['files']:
            path = _safe(root / item['path'])
            if path.exists():
                current[item['path']] = [path.stat().st_size, _hash(path)]
    required = sum(entry[1] for _, values in sources.values() for entry in values.values() if entry[0] == 'file')
    required += sum(entry[0] for entry in current.values())
    profile.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(profile).free < required:
        raise OSError('Insufficient space for optional-package recovery snapshots. Keep the old installation.')
    destination = _safe(profile / 'updates' / 'package-snapshots' / uuid.uuid4().hex)
    destination.mkdir(parents=True)
    for label, (source, inventory) in sources.items():
        result = migrate_collaboration(source, destination / label)
        if (result['status'] != 'complete' or _inventory_store(source, None) != inventory
                or _inventory_store(destination / label, None) != inventory):
            raise ValueError('Package recovery material changed during copying. Keep both copies for review.')
    from tempo_update_recovery import _store_copy
    for name, expected in current.items():
        source = _safe(root / name)
        target = _safe(destination / 'application' / name)
        target.parent.mkdir(parents=True, exist_ok=True)
        _store_copy(source, target)
        if [source.stat().st_size, _hash(source)] != expected or [target.stat().st_size, _hash(target)] != expected:
            raise ValueError('Application overlay changed during copying. Snapshot is incomplete.')
    record = dict(schema_version=1, source_root=str(root), assessment=assessment,
                  packages={label: values for label, (_, values) in sources.items()}, application=current)
    # Read back the complete source trees again before recording completion.
    if any(_inventory_store(source, None) != inventory for source, inventory in sources.values()):
        raise ValueError('Package recovery material changed during copying. Snapshot is incomplete.')
    if any([_safe(root / name).stat().st_size, _hash(root / name)] != value for name, value in current.items()):
        raise ValueError('Application overlay changed during copying. Snapshot is incomplete.')
    _atomic_json(destination / 'package-snapshot.json', record)
    return dict(status='snapshotted', snapshot=str(destination), assessment=assessment)
