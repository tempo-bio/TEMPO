"""Exercise the published v1.0.3 installer and the exact v1.0.4 Windows delta."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from scripts.build_update_repair import read_keys
from scripts.build_velopack_candidate import file_record
from scripts.package_windows_updates import inventory_package
from scripts.verify_windows_update_trial import installed_trial, run_windows, trial_directory
from tempo_update_manifest import FileRecord, load_verified_manifest, verify_asset, verify_zip
from tempo_update_recovery import _verify_app, _verify_owner


def verify(base_dir, target_dir, evidence):
    if sys.platform != 'win32' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('Use an isolated GitHub Actions Windows runner.')
    base_dir, target_dir, evidence = (path.resolve() for path in (base_dir, target_dir, evidence))
    evidence.mkdir(parents=True, exist_ok=True)
    keys = read_keys(target_dir / 'stable-public-keys.json')
    previous = load_verified_manifest((base_dir / 'tempo-update-v1.0.3.json').read_bytes(), keys,
        now=datetime.now(timezone.utc), expected_channel='stable', min_sequence=0)
    base = base_dir / previous.full.name
    verify_asset(base, previous.full)
    contract = json.loads((target_dir / 'release-contract.json').read_bytes())
    revision, = contract['revisions']
    target = target_dir / revision['full']['path']
    delta = target_dir / 'TEMPO.Bio.Desktop-1.0.4-delta.nupkg'
    verification = json.loads((target_dir / 'delta-verification.json').read_bytes())
    if (previous.version != '1.0.3' or revision['version'] != '1.0.4'
            or verification['reconstructed_inventory_matches'] is not True
            or verification['base'] != file_record(base)
            or verification['target'] != revision['full'] or verification['target'] != file_record(target)
            or verification['delta'] != file_record(delta)
            or verification['patcher'] != file_record(target_dir / 'update.exe')):
        raise ValueError('The update inputs differ from the verified Windows delta.')

    installer = base_dir / 'TEMPO_Setup_v1.0.3.exe'
    installer_identity = file_record(installer)
    if installer_identity['sha256'] != '7072d1ac82c9aa75f93bce3b8365944b24f51871aa991e0109e7c0e83c9d326b':
        raise ValueError('Use the exact published v1.0.3 installer.')
    repair = target_dir / 'TEMPORepair.exe'
    if file_record(repair) != contract['repair']:
        raise ValueError('Repair companion differs from the release contract.')

    with trial_directory(evidence.parent, 'release-delta-') as work:
        (work / 'trial-setup.exe').hardlink_to(installer)
        (work / 'TEMPORepair.exe').hardlink_to(repair)
        trial = {'installer': installer_identity, 'repair': contract['repair']}
        with installed_trial(work, trial, work / 'profile') as (root, profile, env):
            _verify_app(root / 'current', previous)
            native = _verify_owner(root, previous)
            reconstructed = work / 'reconstructed.nupkg'
            run_windows([native, 'patch', '--old', base, '--delta', delta, '--output', reconstructed], env)
            verify_zip(reconstructed, tuple(FileRecord(**row) for row in inventory_package(target)))
            log = evidence / 'native-apply.log'
            run_windows([native, 'apply', '--silent', '--norestart', '--rootDir', root,
                         '--package', reconstructed, '--log', log], env)
            for item in revision['installed_inventory']:
                if file_record(root / 'current' / item['path'], item['path']) != item:
                    raise ValueError('Installed update differs from the target inventory.')
            if file_record(native, 'update.exe') != verification['patcher']:
                raise ValueError('The installed updater changed during replacement.')
            report = evidence / 'target-runtime.json'
            run_windows([root / 'current' / 'TEMPO.exe', '--verify-installation', report], env, timeout=900)
            result = json.loads(report.read_bytes())
            if result.get('success') is not True or result.get('version') != revision['version']:
                raise ValueError('The updated application failed its runtime checks.')
            if (profile / 'trial-recording.txt').read_bytes() != b'Test recording outside the installation':
                raise ValueError('The update changed the test recording.')
            # The direct native apply leaves a backup. The runner profile is
            # disposable and the backup is retained separately before uninstall.
            previous_dir = root / 'tempo-previous'
            if previous_dir.exists():
                previous_dir.rename(work / 'retained-previous')
            (root / 'tempo-update-pending.txt').unlink(missing_ok=True)
    (evidence / 'delta-upgrade.json').write_text(json.dumps({
        'success': True, 'base_installer': installer_identity, 'target_commit': contract['source_commit'],
        'delta_verification': verification, 'preserved_recording': True,
        'updated_runtime_verified': True,
    }, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base', 'target', 'evidence'):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    verify(args.base, args.target, args.evidence)
