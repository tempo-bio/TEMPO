"""Assemble private signed Windows update tests from three complete CPU builds."""

import argparse
import base64
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.build_update_repair import build as build_repair
from scripts.build_velopack_candidate import VPK_SHA256, check_payload, file_record, folder_inventory, signed_fixture
from scripts.package_windows_updates import COMMIT, _asset, _canonical, _digest
from scripts.prepare_windows_update_trial import VERSIONS
from scripts.windows_release_checks import run_logged
from tempo_update_manifest import ARCHITECTURE, PACKAGE_ID, FileRecord, inventory_digest, verify_zip

NATIVE_SHA256 = {
    'update.exe': 'fc83f917c99689e5d642e0617fb641fb23fa9e4af9853aa8c45bf2dcdc3f5373',
    'setup.exe': '22066a774a216d564355adbdaa74b5788779384dfdb66f24a3be3030eaea16c3',
}


def inspect_bundles(bundles):
    """Require independent executable and source identities and a dependency change."""
    identities = []
    executables = set()
    for bundle, version in zip(bundles, VERSIONS, strict=True):
        identity = json.loads((bundle / '_internal/assets/build-provenance.json').read_bytes())
        if (identity.get('version') != version or identity.get('profile') != 'cpu'
                or not isinstance(identity.get('commit'), str) or not COMMIT.fullmatch(identity['commit'])
                or not isinstance(identity.get('packages'), dict) or not identity['packages']):
            raise ValueError(f'Expected the complete CPU build of {version}.')
        if (bundle / '_internal/assets/nvidia-runtime.json').exists():
            raise ValueError('The isolated CPU trial must not advertise a public NVIDIA runtime.')
        identity['executable_sha256'] = file_record(bundle / 'TEMPO.exe')['sha256']
        executables.add(identity['executable_sha256'])
        identities.append(identity)
    if len(executables) != 3 or len({item['commit'] for item in identities}) != 3:
        raise ValueError('Trial versions must come from three distinct source builds.')
    if all(item['packages'] == identities[0]['packages'] for item in identities[1:]):
        raise ValueError('Include a dependency change in at least one trial build.')
    return identities


def patch_record(base, target_version, delta):
    return dict(base_version=base.version, base_commit=base.commit,
                base_inventory_sha256=inventory_digest(base.inventory), target_version=target_version,
                asset=_asset(delta, target_version))


def write_indexes(output, releases, package_b, inventory_b, key):
    first, second, third = releases
    if len(second.patches) != 1:
        raise ValueError('The fallback tests require one verified A-to-B delta.')
    invalid = output / 'invalid-trial-delta.nupkg'
    invalid.write_bytes(b'Authenticated test input that Velopack cannot reconstruct.')
    patch = patch_record(first, second.version, invalid)
    bad_second = signed_fixture(package_b, inventory_b, second.version, second.commit, second.sequence, key,
                               patches=[dict(patch, target_commit=second.commit)])
    indexes = {
        'first-update': (first, second),
        'next-update': (second, third),
        'skip-version': (first, third),
        'invalid-reconstruction': (first, bad_second),
    }
    for name, entries in indexes.items():
        (output / f'{name}.json').write_bytes(_canonical(
            {'releases': [json.loads(item.envelope) for item in entries]}))


def build(bundles, vpk_archive, native, output):
    if sys.platform != 'win32':
        raise ValueError('The trial packages must be assembled on Windows.')
    bundles = [Path(path).resolve() for path in bundles]
    vpk_archive, native, output = (Path(path).resolve() for path in (vpk_archive, native, output))
    if output.exists() or any(bundle in output.parents for bundle in bundles):
        raise ValueError('Use an absent output directory outside the input builds.')
    identities = inspect_bundles(bundles)
    if _digest(vpk_archive) != VPK_SHA256:
        raise ValueError('Velopack packager checksum mismatch.')
    for name, sha256 in NATIVE_SHA256.items():
        if _digest(native / name) != sha256:
            raise ValueError(f'The v1.0.2 native tool checksum does not match for {name}.')

    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    trusted = {'acceptance-only': base64.b64encode(public).decode('ascii')}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.tempo-update-trial-', dir=output.parent) as temporary:
        work = Path(temporary)
        result = work / 'result'
        result.mkdir()
        keys_path = result / 'trial-public-keys.json'
        keys_path.write_bytes(_canonical(trusted))
        repair = build_repair(keys_path, native / 'update.exe', result, work / 'repair')
        config = dict(schema_version=1, enabled=True, package_id=PACKAGE_ID, channel='prerelease',
                      trusted_keys=trusted, native_recovery_certified=True,
                      updater_sha256=NATIVE_SHA256['update.exe'], repair_sha256=_digest(repair))
        vpk = work / 'vpk'
        with zipfile.ZipFile(vpk_archive) as archive:
            archive.extractall(vpk)
        for name in NATIVE_SHA256:
            shutil.copyfile(native / name, vpk / 'vendor' / name.replace('.exe', '_x64.exe'))
        package_dir = work / 'packages'
        releases, rows = [], []
        previous_full = None
        for number, (bundle, identity) in enumerate(zip(bundles, identities, strict=True), 1):
            version, commit = identity['version'], identity['commit']
            payload = shutil.copytree(bundle, work / 'TEMPO')
            (payload / '_internal/assets/tempo-updates.json').write_bytes(_canonical(config))
            shutil.copyfile(repair, payload / 'TEMPORepair.exe')
            expected = folder_inventory(payload)
            command = ['dotnet', str(vpk / 'tools/net8.0/any/vpk.dll'), '[win]', 'pack',
                       '--packId', PACKAGE_ID, '--packTitle', 'TEMPO Update Trial',
                       '--packVersion', version, '--packDir', str(payload), '--mainExe', 'TEMPO.exe',
                       '--runtime', ARCHITECTURE, '--outputDir', str(package_dir), '--noPortable',
                       '--noDefaultExclude', '--exclude', '(?!)', '--skip-updates', '--yes', '--legacyConsole']
            if run_logged(command, result / f'pack-{number}.log', 1800):
                raise RuntimeError(f'Velopack could not package {version}.')
            full, = package_dir.glob(f'*-{version}-full.nupkg')
            setup, = package_dir.glob('*Setup.exe')
            if number == 1:
                shutil.copyfile(setup, result / 'trial-setup.exe')
            setup.unlink()
            inventory = check_payload(full, expected, NATIVE_SHA256['update.exe'], commit, release_config=config)
            shutil.rmtree(payload)
            patches = []
            if releases:
                delta, = package_dir.glob(f'*-{version}-delta.nupkg')
                rebuilt = work / 'reconstructed.nupkg'
                if run_logged([str(native / 'update.exe'), 'patch', '--old', str(previous_full),
                               '--delta', str(delta), '--output', str(rebuilt)],
                              result / f'patch-{number}.log', 900):
                    raise RuntimeError(f'Velopack could not reconstruct {version}.')
                verify_zip(rebuilt, tuple(FileRecord(**item) for item in inventory))
                rebuilt.unlink()
                (result / delta.name).hardlink_to(delta)
                patches.append(dict(patch_record(releases[-1], version, delta), target_commit=commit))
            release = signed_fixture(full, inventory, version, commit, number, key, patches=patches)
            (result / full.name).hardlink_to(full)
            releases.append(release)
            rows.append({'version': version, 'commit': commit, 'full': file_record(full),
                         'executable_sha256': identity['executable_sha256'], 'dependencies': identity['packages']})
            previous_full = full
            if number == 2:
                package_b, inventory_b = full, inventory
        write_indexes(result, releases, package_b, inventory_b, key)
        (result / 'trial-contract.json').write_bytes(_canonical({
            'schema_version': 1, 'release_certified': False, 'purpose': 'isolated-signed-update-trial',
            'revisions': rows, 'installer': file_record(result / 'trial-setup.exe'),
            'repair': file_record(repair), 'updater_sha256': NATIVE_SHA256['update.exe'],
        }))
        result.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, nargs=3, required=True)
    parser.add_argument('--vpk-archive', type=Path, required=True)
    parser.add_argument('--native', type=Path, required=True, help='Directory with the tested setup.exe and update.exe')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.bundle, args.vpk_archive, args.native, args.output)


if __name__ == '__main__':
    main()
