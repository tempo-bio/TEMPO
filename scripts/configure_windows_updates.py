"""Prepare a certified, public-only Windows update candidate; never publish it."""
import argparse
import base64
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.build_update_repair import read_keys
from scripts.package_windows_updates import COMMIT, HASH, PIN, REQUIRED_CHECKS, _digest, _read_json

PATCH = ROOT / 'installer/velopack/0001-tempo-recovery.patch'


def candidate(public_keys, native_updater, repair_executable, certification, source_commit,
              *, channel='stable', pin_path=PIN, patch_path=PATCH):
    """Bind public trust and exact tools to reviewed native evidence, before packaging."""
    pin, evidence = _read_json(pin_path), _read_json(certification)
    required = pin.get('required_checks') if isinstance(pin, dict) else None
    if (not isinstance(pin, dict) or pin.get('schema_version') != 1
            or pin.get('certification_status') != 'certified'
            or not isinstance(required, list) or any(not isinstance(x, str) or not x for x in required)
            or len(required) != len(set(required)) or not REQUIRED_CHECKS.issubset(required)
            or not isinstance(pin.get('upstream_commit'), str) or not COMMIT.fullmatch(pin['upstream_commit'])
            or not isinstance(pin.get('updater_sha256'), str) or not HASH.fullmatch(pin['updater_sha256'])
            or not isinstance(pin.get('patch_sha256'), str) or not HASH.fullmatch(pin['patch_sha256'])):
        raise ValueError('The native updater pin has not been certified.')
    if not isinstance(source_commit, str) or not COMMIT.fullmatch(source_commit):
        raise ValueError('A full source commit is required.')
    repair_hash = _digest(repair_executable)
    if (not isinstance(evidence, dict) or evidence.get('schema_version') != 1
            or evidence.get('source_commit') != source_commit
            or evidence.get('upstream_commit') != pin['upstream_commit']
            or evidence.get('patch_sha256') != pin['patch_sha256']
            or evidence.get('updater_sha256') != pin['updater_sha256']
            or evidence.get('repair_sha256') != repair_hash
            or evidence.get('public_keys_sha256') != _digest(public_keys)
            or _digest(native_updater) != pin['updater_sha256']
            or _digest(patch_path) != pin['patch_sha256']):
        raise ValueError('Native evidence or tool bytes do not match the certified identities.')
    checks = evidence.get('checks')
    if not isinstance(checks, dict) or any(checks.get(name) is not True for name in required):
        raise ValueError('Native certification is missing required checks.')
    if channel not in ('stable', 'prerelease'):
        raise ValueError('Invalid channel.')
    keys = read_keys(public_keys)
    return {'schema_version': 1, 'enabled': True, 'package_id': 'TEMPO.Bio.Desktop',
            'channel': channel, 'trusted_keys': {key: base64.b64encode(value).decode('ascii')
                                                  for key, value in keys.items()},
            'native_recovery_certified': True, 'updater_sha256': pin['updater_sha256'],
            'repair_sha256': repair_hash}


def validate_config(config, **kwargs):
    expected = candidate(**kwargs)
    if _read_json(config, limit=65536) != expected:
        raise ValueError('Update configuration does not match public keys and certified evidence.')
    return expected


def stage(bundle, config, **kwargs):
    expected = validate_config(config, **kwargs)
    bundle = Path(bundle)
    if not (bundle / 'TEMPO.exe').is_file() or not (bundle / '_internal' / 'assets').is_dir():
        raise ValueError('Expected an existing PyInstaller TEMPO folder.')
    if any(path.is_symlink() for path in (bundle, bundle / '_internal', bundle / '_internal' / 'assets')):
        raise ValueError('Bundle paths must not be links.')
    config_dest = bundle / '_internal' / 'assets' / 'tempo-updates.json'
    repair_dest = bundle / 'TEMPORepair.exe'
    if config_dest.is_symlink() or repair_dest.is_symlink():
        raise ValueError('Bundle outputs must not be links.')
    shutil.copyfile(kwargs['repair_executable'], repair_dest)
    if _digest(repair_dest) != expected['repair_sha256']:
        raise ValueError('Copied repair executable failed verification.')
    config_dest.write_text(json.dumps(expected, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-keys', type=Path, required=True)
    parser.add_argument('--native-updater', type=Path, required=True)
    parser.add_argument('--repair-executable', type=Path, required=True)
    parser.add_argument('--certification', type=Path, required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--channel', choices=('stable', 'prerelease'), default='stable')
    parser.add_argument('--output', type=Path, required=True, help='configuration file named tempo-updates.json')
    parser.add_argument('--bundle', type=Path, help='also stage into this existing PyInstaller folder')
    args = parser.parse_args()
    if args.output.name != 'tempo-updates.json':
        parser.error('--output must be named tempo-updates.json')
    options = dict(public_keys=args.public_keys, native_updater=args.native_updater,
                   repair_executable=args.repair_executable, certification=args.certification,
                   source_commit=args.source_commit, channel=args.channel)
    config = candidate(**options)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(config, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    if args.bundle:
        stage(args.bundle, args.output, **options)
    print('Candidate configuration prepared; this does not publish or certify a release.')


if __name__ == '__main__':
    main()
