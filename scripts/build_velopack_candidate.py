"""Package the complete disabled Windows app for isolated acceptance, never publication.

Three same-source packaging revisions exercise native replacement mechanics.
Their ephemeral signatures are trusted only by the disposable repair companion;
they are not release signatures or evidence of two distinct application releases.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.build_update_repair import build as build_repair
from scripts.package_windows_updates import _canonical, _digest, inventory_package
from scripts.windows_release_checks import isolated_environment, run_logged
from tempo_update_manifest import (ARCHITECTURE, PACKAGE_ID, FileRecord,
                                   load_verified_manifest, validate_path, verify_zip)
from tempo_update_recovery import _application_inventory
from tempo_legacy_migration import SUPPORTED_LEGACY

ROOT = Path(__file__).resolve().parents[1]
VPK_SHA256 = '2b56ce117f803fc70c103cb423bd040e395e40370f6ff6e10818e9ff9c26a323'
VERSIONS = ('1.0.2-velopack.1', '1.0.2-velopack.2', '1.0.2-velopack.3')
TITLE = 'TEMPO Velopack Candidate'


def file_record(path, name=None):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError('Expected an ordinary artifact file: ' + str(path))
    return dict(path=validate_path(name or path.name), size=path.stat().st_size, sha256=_digest(path))


def folder_inventory(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Payload root must be an ordinary directory, not a link.')
    result = []
    for directory, dirs, files in os.walk(root, followlinks=False,
            onerror=lambda error: (_ for _ in ()).throw(error)):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if (path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400
                    or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                raise ValueError('Payload contains a link or unsupported file.')
        for name in files:
            path = Path(directory) / name
            result.append(file_record(path, path.relative_to(root).as_posix()))
    if not result:
        raise ValueError('Payload is empty.')
    return sorted(result, key=lambda item: item['path'])


def check_payload(package, source_inventory, updater_hash, commit, *, release_config=None):
    """Require every built file, including source/notices/workers, byte for byte."""
    inventory = inventory_package(package)
    actual = {item['path']: item for item in inventory}
    for item in source_inventory:
        name = 'lib/app/' + item['path']
        if actual.get(name) != dict(item, path=name):
            raise ValueError('Packaged payload differs from tested folder: ' + item['path'])
    if actual.get('lib/app/Squirrel.exe', {}).get('sha256') != updater_hash:
        raise ValueError('Package does not contain the exact production-feature updater.')
    with zipfile.ZipFile(package) as archive:
        provenance = json.loads(archive.read('lib/app/_internal/assets/build-provenance.json'))
        config = json.loads(archive.read('lib/app/_internal/assets/tempo-updates.json'))
    if provenance.get('commit') != commit or provenance.get('profile') != 'cpu':
        raise ValueError('Expected the tested CPU application at the exact source commit.')
    if release_config is None:
        disabled = json.loads((ROOT / 'installer/tempo-updates.json').read_bytes())
        if config != disabled or config.get('enabled') is not False or config.get('trusted_keys'):
            raise ValueError('Acceptance packages must retain the disabled, empty-trust configuration.')
    elif config != release_config:
        raise ValueError('Package trust differs from the reviewed release configuration.')
    return inventory


def signed_fixture(package, inventory, version, commit, sequence, key, *, patches=()):
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    asset = file_record(package)
    asset['name'] = asset.pop('path')
    asset['url'] = f'https://github.com/tempo-bio/TEMPO/releases/download/v{version}/{asset["name"]}'
    obj = dict(schema_version=1, package_id=PACKAGE_ID, version=version, commit=commit,
               architecture=ARCHITECTURE, channel='prerelease', sequence=sequence,
               expires_at=(datetime.now(timezone.utc) + timedelta(days=14)).strftime('%Y-%m-%dT%H:%M:%SZ'),
               full=asset, inventory=inventory, patches=list(patches), minimum_updater_version='1.2.161',
               storage_schema_version=1)
    envelope = dict(signed=obj, signatures=[dict(key_id='acceptance-only',
        signature=base64.b64encode(key.sign(_canonical(obj))).decode('ascii'))])
    release = load_verified_manifest(_canonical(envelope), {'acceptance-only': public},
                                    now=datetime.now(timezone.utc), min_sequence=0, expected_channel='prerelease')
    verify_zip(package, release.inventory)
    return release


def build(bundle, vpk_archive, updater, native_evidence, output, commit, *, setup):
    if sys.platform != 'win32':
        raise ValueError('Full-app acceptance candidates must be built on Windows.')
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('A full source commit is required.')
    if any(name.startswith(('VPK_', 'TEMPO_UPDATE_')) for name in os.environ):
        raise ValueError('Remove VPK and update-override environment variables before candidate packaging.')
    bundle, output = Path(bundle).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Use an absent output directory; candidate artifacts are immutable.')
    if _digest(vpk_archive) != VPK_SHA256:
        raise ValueError('Packager checksum mismatch.')
    updater_hash = _digest(updater)
    setup_hash = _digest(setup)
    evidence = json.loads(Path(native_evidence).read_bytes())
    pin = json.loads((ROOT / 'installer/velopack/pin.json').read_bytes())
    if (evidence.get('source_commit') != commit or evidence.get('updater_sha256') != updater_hash
            or evidence.get('setup_sha256') != setup_hash
            or evidence.get('upstream_commit') != pin['upstream_commit']
            or evidence.get('patch_sha256') != pin['patch_sha256']
            or evidence.get('fixture_passed') is not True
            or evidence.get('production_passed') is not True
            or _digest(ROOT / 'installer/velopack/0001-tempo-recovery.patch') != pin['patch_sha256']):
        raise ValueError('Native candidate identity/evidence does not match this source revision.')
    source_inventory = folder_inventory(bundle)
    output.mkdir(parents=True)
    contract = dict(schema_version=1, purpose='isolated-full-app-acceptance', release_certified=False,
                    source_commit=commit, package_id=PACKAGE_ID, title=TITLE,
                    same_source_packaging_revisions=True, updater_sha256=updater_hash,
                    setup_sha256=setup_hash,
                    packager_sha256=VPK_SHA256, native_evidence=evidence,
                    bundle_inventory=source_inventory, revisions=[])
    contract['legacy_source'] = dict(SUPPORTED_LEGACY['1.0.1'], version='1.0.1',
        url='https://github.com/tempo-bio/TEMPO/releases/download/v1.0.1/TEMPO_Setup_v1.0.1.exe')
    key = Ed25519PrivateKey.generate()  # Never saved or exported.
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    keys_path = output / 'acceptance-public-keys.json'
    keys_path.write_bytes(_canonical({'acceptance-only': base64.b64encode(public).decode('ascii')}))
    with tempfile.TemporaryDirectory(prefix='tempo-vpk-') as temporary:
        work = Path(temporary)
        hooks = ('--veloapp-install', '--veloapp-updated', '--veloapp-obsolete', '--veloapp-uninstall')
        for hook in hooks:
            environment = isolated_environment(work / hook.removeprefix('--'))
            if run_logged([str(bundle / 'TEMPO.exe'), hook, VERSIONS[0]],
                          output / (hook.removeprefix('--') + '.log'), 20,
                          cwd=work, env=environment):
                raise RuntimeError('The real frozen lifecycle entrypoint failed: ' + hook)
        contract['frozen_lifecycle_probes'] = list(hooks)
        vpk = work / 'vpk'
        with zipfile.ZipFile(vpk_archive) as archive:
            archive.extractall(vpk)  # Exact hash-pinned upstream archive only.
        shutil.copyfile(updater, vpk / 'vendor/update_x64.exe')
        shutil.copyfile(setup, vpk / 'vendor/setup_x64.exe')
        repair = build_repair(keys_path, updater, output, work / 'repair')
        contract['repair'] = file_record(repair)
        command = ['dotnet', str(vpk / 'tools/net8.0/any/vpk.dll')]
        releases = work / 'releases'
        for sequence, version in enumerate(VERSIONS, 1):
            args = command + ['[win]', 'pack', '--packId', PACKAGE_ID, '--packTitle', TITLE,
                '--packVersion', version, '--packDir', str(bundle), '--mainExe', 'TEMPO.exe',
                '--runtime', ARCHITECTURE, '--outputDir', str(releases), '--noPortable',
                '--noDefaultExclude', '--exclude', '(?!)', '--skip-updates', '--yes', '--legacyConsole']
            if run_logged(args, output / f'pack-{sequence}.log', 1800):
                raise RuntimeError('Full-app packaging or real frozen lifecycle check failed.')
            fulls = list(releases.glob(f'*{version}-full.nupkg'))
            setups = list(releases.glob('*Setup.exe'))
            if len(fulls) != 1 or len(setups) != 1:
                raise ValueError('Packager produced ambiguous artifacts.')
            full = output / fulls[0].name
            # Keep VPK's exact prior full for the next delta. Temp and output
            # directories can be on different volumes; a hard link is unsafe.
            shutil.copyfile(fulls[0], full)
            setup = output / f'TEMPO-Velopack-{version}-Setup.exe'
            shutil.copyfile(setups[0], setup)
            inventory = check_payload(full, source_inventory, updater_hash, commit)
            release = signed_fixture(full, inventory, version, commit, sequence, key)
            meta = output / f'acceptance-{version}.json'
            meta.write_bytes(release.envelope)
            row = dict(version=version, full=file_record(full), installer=file_record(setup),
                       manifest=file_record(meta), installed_inventory=[vars(item) for item in _application_inventory(release)],
                       expanded_bytes=sum(item['size'] for item in inventory))
            if sequence > 1:
                deltas = list(releases.glob(f'*{version}-delta.nupkg'))
                if len(deltas) != 1:
                    raise ValueError('Expected exactly one native delta for this candidate.')
                delta = output / deltas[0].name
                shutil.copyfile(deltas[0], delta)
                reconstructed = work / f'reconstructed-{sequence}.nupkg'
                previous = output / contract['revisions'][-1]['full']['path']
                if run_logged([str(Path(updater).resolve()), 'patch', '--old', str(previous),
                               '--delta', str(delta), '--output', str(reconstructed)],
                              output / f'reconstruct-{sequence}.log', 900):
                    raise RuntimeError('Production-feature delta reconstruction failed.')
                verify_zip(reconstructed, tuple(FileRecord(**item) for item in inventory))
                row['delta'] = file_record(delta)
                row['delta_reconstructed_inventory_match'] = True
                reconstructed.unlink()
            contract['revisions'].append(row)
    if folder_inventory(bundle) != source_inventory:
        raise ValueError('Packager changed the independently tested application folder.')
    for name in ('verification-contract.json', 'build-provenance.json'):
        shutil.copyfile(ROOT / 'Output' / name, output / name)
    for name in ('verify_velopack_install.ps1', 'windows_installer_helpers.ps1'):
        shutil.copyfile(ROOT / 'scripts' / name, output / name)
    # Completion marker is last; failed builds cannot be mistaken for a usable set.
    (output / 'candidate-contract.json').write_bytes(_canonical(contract))
    return contract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'vpk-archive', 'updater', 'setup', 'native-evidence', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    build(args.bundle, args.vpk_archive, args.updater, args.native_evidence, args.output, args.commit, setup=args.setup)
    print('Full-app candidate prepared. No release certified, signed for users, or published.')


if __name__ == '__main__':
    main()
