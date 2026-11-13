"""Assemble one stable-channel installer from the reviewed Windows build artifacts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import zipfile

from scripts.build_velopack_candidate import (ROOT, VPK_SHA256, check_payload,
    file_record, folder_inventory)
from scripts.configure_windows_updates import candidate, stage
from scripts.package_windows_updates import COMMIT, _canonical, _digest, reviewed_native_pin
from scripts.windows_release_checks import run_logged, validate_report
from tempo_release import VERSION
from tempo_update_manifest import ARCHITECTURE, PACKAGE_ID, FileRecord
from tempo_update_recovery import _application_inventory


def build(bundle, build_contract, verification, runtime_report, vpk_archive, setup, output,
          *, tested_updater=None, **review):
    if sys.platform != 'win32':
        raise ValueError('The release installer must be assembled on Windows.')
    bundle, output = Path(bundle).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Use a new output directory for immutable release artifacts.')
    if bundle == output or bundle in output.parents:
        raise ValueError('Release outputs must be outside the verified input bundle.')
    contract = json.loads(Path(build_contract).read_bytes())
    commit = contract.get('source_commit')
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise ValueError('The full application build commit is required.')
    if (contract.get('purpose') != 'isolated-full-app-acceptance'
            or contract.get('packager_sha256') != VPK_SHA256
            or _digest(vpk_archive) != VPK_SHA256
            or contract.get('updater_sha256') != _digest(tested_updater or review['native_updater'])
            or contract.get('setup_sha256') != _digest(setup)):
        raise ValueError('Use the exact packager and native tools reviewed by the full Windows build.')
    if folder_inventory(bundle) != contract.get('bundle_inventory'):
        raise ValueError('The CPU bundle differs from the verified Windows build.')
    identity_path = bundle / '_internal' / 'assets' / 'build-provenance.json'
    identity = json.loads(identity_path.read_bytes())
    verified = json.loads(Path(verification).read_bytes())
    report = json.loads(Path(runtime_report).read_bytes())
    validate_report(report, commit=commit)
    if (identity.get('profile') != 'cpu' or identity.get('commit') != commit
            or identity.get('version') != VERSION or verified.get('commit') != commit
            or verified.get('version') != VERSION or verified.get('schema_version') != report['schema_version']
            or verified.get('required_checks') != report['required_checks']
            or verified.get('lock_sha256') != identity.get('lock_sha256')):
        raise ValueError('CPU provenance, frozen diagnostics and the verification contract must agree.')
    review = dict(review, source_commit=commit, channel='stable')
    config = candidate(**review)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.tempo-release-', dir=output.parent) as directory:
        work = Path(directory)
        payload = shutil.copytree(bundle, work / 'TEMPO')
        config_path = work / 'tempo-updates.json'
        config_path.write_bytes(_canonical(config))
        stage(payload, config_path, **review)
        staged_inventory = folder_inventory(payload)
        vpk = work / 'vpk'
        with zipfile.ZipFile(vpk_archive) as archive:
            archive.extractall(vpk)  # The complete upstream archive is hash pinned.
        shutil.copyfile(review['native_updater'], vpk / 'vendor/update_x64.exe')
        shutil.copyfile(setup, vpk / 'vendor/setup_x64.exe')
        packages = work / 'packages'
        result = work / 'release'
        result.mkdir()
        command = ['dotnet', str(vpk / 'tools/net8.0/any/vpk.dll'), '[win]', 'pack',
                   '--packId', PACKAGE_ID, '--packTitle', 'TEMPO', '--packVersion', VERSION,
                   '--packDir', str(payload), '--mainExe', 'TEMPO.exe', '--runtime', ARCHITECTURE,
                   '--outputDir', str(packages), '--noPortable', '--noDefaultExclude', '--exclude', '(?!)',
                   '--skip-updates', '--yes', '--legacyConsole']
        if run_logged(command, result / 'pack.log', 1800):
            raise RuntimeError('Stable release packaging failed.')
        fulls, installers = list(packages.glob(f'*{VERSION}-full.nupkg')), list(packages.glob('*Setup.exe'))
        if len(fulls) != 1 or len(installers) != 1:
            raise ValueError('Expected one full package and one installer for the release.')
        full = shutil.copyfile(fulls[0], result / fulls[0].name)
        installer = shutil.copyfile(installers[0], result / f'TEMPO_Setup_v{VERSION}.exe')
        if folder_inventory(payload) != staged_inventory:
            raise ValueError('The packager changed the staged application folder.')
        inventory = check_payload(full, staged_inventory, config['updater_sha256'], commit,
                                  release_config=config)
        installed = _application_inventory(SimpleNamespace(inventory=tuple(FileRecord(**row) for row in inventory)))
        repair = shutil.copyfile(payload / 'TEMPORepair.exe', result / 'TEMPORepair.exe')
        shutil.copyfile(identity_path, result / 'build-provenance.json')
        shutil.copyfile(verification, result / 'verification-contract.json')
        shutil.copyfile(runtime_report, result / 'portable-verification.json')
        for name in ('verify_velopack_install.ps1', 'windows_installer_helpers.ps1'):
            shutil.copyfile(ROOT / 'scripts' / name, result / name)
        release = dict(schema_version=1, purpose='stable-prerelease', source_commit=commit,
                       package_id=PACKAGE_ID, title='TEMPO', release_certified=False,
                       updater_sha256=config['updater_sha256'], repair=file_record(repair),
                       revisions=[dict(version=VERSION, installer=file_record(installer), full=file_record(full),
                                       installed_inventory=[vars(row) for row in installed])])
        (result / 'release-contract.json').write_bytes(_canonical(release))
        result.rename(output)
    return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'build-contract', 'verification', 'runtime-report', 'vpk-archive',
                 'setup', 'output', 'public-keys', 'native-updater', 'repair-executable', 'certification'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--native-pin', type=Path)
    parser.add_argument('--tested-updater', type=Path,
                        help='Exact updater used by the current build contract when shipping a retained updater')
    args = vars(parser.parse_args())
    args['pin_path'] = reviewed_native_pin(args.pop('native_pin'))
    build(**args)
    print('Stable-channel files assembled for Windows verification.')


if __name__ == '__main__':
    main()
