"""Create signed update metadata from an already-built, certified VPK package.

This tool never builds, installs, signs Windows executables, or publishes. Release
mode requires a certified repository pin and an explicit evidence JSON with
schema_version, source_commit, upstream_commit, updater_sha256, patch_sha256,
full_package_sha256, and a checks mapping containing every pinned required check set to true. Hashes
in the evidence bind certification records; they do not independently prove
that a native updater passed the checks. Review evidence before certifying a pin.

Patch specifications contain base_version, base_commit, base_inventory_sha256,
and file. Each base must match a signed previous index and its exact full package
in --base-package-dir. The certified native updater reconstructs every target;
publication requires a complete target inventory match. Test and certify this
engine on Windows before publishing. Host all assets before advertising the index.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile

from tempo_update_manifest import (ARCHITECTURE, MAX_ASSET, MAX_EXPANDED, MAX_FILES,
    MAX_METADATA, PACKAGE_ID, SCHEMA_VERSION, inventory_digest, load_verified_manifest,
    validate_path, verify_zip, verify_asset, FileRecord)

PIN = Path(__file__).resolve().parents[1] / 'installer' / 'velopack' / 'pin.json'
HASH = re.compile(r'^[0-9a-f]{64}$')


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _read_json(path, limit=MAX_METADATA):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError('Input JSON exceeds its size limit.')
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key.')
            result[key] = value
        return result
    return json.loads(path.read_bytes(), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON value.')))


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def inventory_package(path):
    """Return every regular ZIP member's path, size and hash; never extract."""
    result, seen, total = [], set(), 0
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES * 2:
            raise ValueError('Package has too many ZIP members.')
        for entry in entries:
            name = entry.filename.rstrip('/') if entry.is_dir() else entry.filename
            validate_path(name)
            folded = name.casefold()
            if folded in seen:
                raise ValueError('Package contains duplicate Windows paths.')
            seen.add(folded)
            if stat.S_ISLNK(entry.external_attr >> 16) or entry.external_attr & 0x400:
                raise ValueError('Package contains a link or reparse point.')
            if entry.is_dir():
                continue
            if len(result) >= MAX_FILES or entry.file_size < 0:
                raise ValueError('Package has too many files.')
            total += entry.file_size
            if total > MAX_EXPANDED:
                raise ValueError('Package expanded size is too large.')
            digest, count = hashlib.sha256(), 0
            with archive.open(entry) as stream:
                while block := stream.read(1024 * 1024):
                    count += len(block)
                    if count > entry.file_size:
                        raise ValueError('ZIP entry exceeds its declared size.')
                    digest.update(block)
            if count != entry.file_size:
                raise ValueError('ZIP entry is truncated.')
            result.append({'path': name, 'size': count, 'sha256': digest.hexdigest()})
    if not result:
        raise ValueError('Package is empty.')
    return sorted(result, key=lambda item: item['path'])


def _certified(pin_path, evidence_path, commit, full_package, native_updater, native_patch):
    pin, evidence = _read_json(pin_path), _read_json(evidence_path)
    if not isinstance(pin, dict) or not isinstance(evidence, dict):
        raise ValueError('Invalid native certification.')
    required = pin.get('required_checks')
    if (pin.get('schema_version') != 1 or pin.get('certification_status') != 'certified'
            or not isinstance(required, list) or not required or len(required) != len(set(required))
            or any(not isinstance(x, str) or not x for x in required)):
        raise ValueError('The native updater pin has not been certified.')
    if (evidence.get('schema_version') != 1 or evidence.get('source_commit') != commit
            or evidence.get('upstream_commit') != pin.get('upstream_commit')
            or not isinstance(pin.get('updater_sha256'), str) or not HASH.fullmatch(pin['updater_sha256'])
            or not isinstance(pin.get('patch_sha256'), str) or not HASH.fullmatch(pin['patch_sha256'])
            or evidence.get('updater_sha256') != pin['updater_sha256']
            or evidence.get('patch_sha256') != pin['patch_sha256']):
        raise ValueError('Native certification identity does not match the pinned updater.')
    if (_digest(full_package) != evidence.get('full_package_sha256')
            or (native_updater is not None and _digest(native_updater) != pin['updater_sha256'])
            or _digest(native_patch) != pin['patch_sha256']):
        raise ValueError('Certified package, updater, or patch bytes do not match evidence.')
    with zipfile.ZipFile(full_package) as archive:
        matches = [item for item in archive.infolist() if item.filename == 'lib/app/Squirrel.exe']
        if len(matches) != 1:
            raise ValueError('The full package must contain exactly one certified updater.')
        with archive.open(matches[0]) as stream:
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != pin['updater_sha256']:
            raise ValueError('Packaged updater does not match certified native updater.')
    checks = evidence.get('checks')
    if not isinstance(checks, dict) or any(checks.get(name) is not True for name in required):
        raise ValueError('Native certification is missing required checks.')


def _asset(path, version):
    from urllib.parse import quote
    path = Path(path)
    name = validate_path(path.name)
    size = path.stat().st_size
    if not 0 < size <= MAX_ASSET or not path.is_file() or path.is_symlink():
        raise ValueError('Invalid update asset.')
    return {'name': name, 'size': size, 'sha256': _digest(path),
            'url': f'https://github.com/tempo-bio/TEMPO/releases/download/v{version}/{quote(name, safe="-._~")}'}


def _trusted(path):
    if path is None:
        return {}
    obj = _read_json(path, 65536)
    if not isinstance(obj, dict) or not obj:
        raise ValueError('Invalid trusted key map.')
    result = {}
    for name, value in obj.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError('Invalid trusted key.')
        key = base64.b64decode(value, validate=True)
        if len(key) != 32:
            raise ValueError('Invalid trusted key length.')
        result[name] = key
    return result


def package(*, full_package, version, commit, channel, sequence, expires_at, signing_key,
            key_id, output, certification, pin_path=PIN, previous_index=None,
            trusted_keys=None, patch_spec=None, nvidia_descriptor=None, now=None,
            native_updater=None, native_patch=None, release_notes=None,
            base_package_dir=None, storage_schema_version=1):
    """Build authenticated output after evidence checks; never overwrite differing files."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if not isinstance(key_id, str) or not key_id or len(key_id) > 128:
        raise ValueError('A bounded key ID is required.')
    if native_patch is None:
        raise ValueError('The certified native patch file is required.')
    if patch_spec is not None and (not previous_index or not base_package_dir or not native_updater):
        raise ValueError('Patch publication requires a previous index, base package directory, and certified native updater.')
    _certified(pin_path, certification, commit, full_package, native_updater, native_patch)
    private = serialization.load_pem_private_key(Path(signing_key).read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError('Signing key must be an Ed25519 PEM private key.')
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    now = now or datetime.now(timezone.utc)
    trust = _trusted(trusted_keys)
    if key_id in trust and trust[key_id] != public:
        raise ValueError('Signing key ID conflicts with trusted key map.')
    trust[key_id] = public
    previous, bases = [], {}
    if previous_index:
        if trusted_keys is None:
            raise ValueError('A previous index requires an explicit trusted key map.')
        index = _read_json(previous_index, 32 * 1024 * 1024)
        if not isinstance(index, dict) or set(index) != {'releases'} or not isinstance(index['releases'], list) or not 1 <= len(index['releases']) <= 19:
            raise ValueError('Invalid previous release index.')
        versions, sequences = set(), set()
        for envelope in index['releases']:
            raw = _canonical(envelope)
            release = load_verified_manifest(raw, trust, now=now, min_sequence=0, expected_channel=channel)
            identity = (release.version, release.commit, inventory_digest(release.inventory))
            if identity in bases or release.version in versions or release.sequence in sequences or release.sequence >= sequence:
                raise ValueError('Previous release is duplicated or not older.')
            versions.add(release.version)
            sequences.add(release.sequence)
            bases[identity] = release
            previous.append(envelope)
    if previous_index and version in versions:
        raise ValueError('Target version already exists in the previous index.')
    full_package = Path(full_package)
    files = [full_package]
    payload = {'schema_version': SCHEMA_VERSION, 'package_id': PACKAGE_ID, 'version': version,
               'commit': commit, 'architecture': ARCHITECTURE, 'channel': channel,
               'sequence': sequence, 'expires_at': expires_at, 'full': _asset(full_package, version),
               'inventory': inventory_package(full_package), 'patches': [],
               'storage_schema_version': storage_schema_version}
    if release_notes is not None:
        path = Path(release_notes)
        if path.stat().st_size > 65536:
            raise ValueError('Release notes exceed 64 KiB.')
        payload['release_notes'] = path.read_text(encoding='utf-8')
    if nvidia_descriptor is not None:
        payload['nvidia_descriptor'] = _read_json(nvidia_descriptor, 16384)
    target_inventory = tuple(FileRecord(**record) for record in payload['inventory'])
    verify_zip(full_package, target_inventory)
    if patch_spec is not None:
        specs = _read_json(patch_spec)
        if not isinstance(specs, list) or not 1 <= len(specs) <= 1000:
            raise ValueError('Invalid patch specifications.')
        base_dir = Path(base_package_dir)
        if not base_dir.is_dir() or base_dir.is_symlink():
            raise ValueError('Invalid base package directory.')
        expected_updater_hash = _read_json(pin_path)['updater_sha256']
        with tempfile.TemporaryDirectory(prefix='tempo-patch-check-') as temp:
            for index, item in enumerate(specs):
                if not isinstance(item, dict) or set(item) != {'base_version', 'base_commit', 'base_inventory_sha256', 'file'}:
                    raise ValueError('Invalid patch specification.')
                identity = (item['base_version'], item['base_commit'], item['base_inventory_sha256'])
                base = bases.get(identity)
                if base is None:
                    raise ValueError('Patch base is not a verified previous release.')
                base_path = base_dir / base.full.name
                if base_path.is_symlink() or not base_path.is_file():
                    raise ValueError('Verified base package is missing or unsafe.')
                verify_asset(base_path, base.full)
                verify_zip(base_path, base.inventory)
                path = Path(item['file'])
                delta = _asset(path, version)
                if Path(native_updater).is_symlink() or _digest(native_updater) != expected_updater_hash:
                    raise ValueError('Certified patch engine changed.')
                reconstructed = Path(temp) / f'target-{index}.nupkg'
                command = [str(Path(native_updater).resolve()), 'patch', '--old', str(base_path.resolve()),
                           '--delta', str(path.resolve()), '--output', str(reconstructed)]
                try:
                    subprocess.run(command, check=True, timeout=900, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except (subprocess.SubprocessError, OSError) as exc:
                    raise ValueError('Patch reconstruction failed or timed out.') from exc
                if not reconstructed.is_file() or reconstructed.is_symlink():
                    raise ValueError('Patch reconstruction did not produce a target.')
                verify_zip(reconstructed, target_inventory)
                # Detect source changes during reconstruction before signing it.
                if _digest(path) != delta['sha256'] or path.stat().st_size != delta['size']:
                    raise ValueError('Patch changed during reconstruction.')
                verify_asset(base_path, base.full)
                files.append(path)
                payload['patches'].append({'base_version': identity[0], 'base_commit': identity[1],
                                           'base_inventory_sha256': identity[2], 'target_version': version,
                                           'target_commit': commit, 'asset': delta})
    envelope = {'signed': payload, 'signatures': [{'key_id': key_id,
                'signature': base64.b64encode(private.sign(_canonical(payload))).decode('ascii')}]}
    raw = _canonical(envelope)
    release = load_verified_manifest(raw, trust, now=now, min_sequence=sequence, expected_channel=channel)
    verify_zip(full_package, release.inventory)
    assets = [payload['full']] + [p['asset'] for p in payload['patches']]
    names = [a['name'].casefold() for a in assets]
    metadata_name = f'tempo-update-v{version}.json'
    reserved = {'tempo-update-index.json', metadata_name.casefold(), 'tempo-update-sha256.json'}
    if len(set(names)) != len(names) or set(names) & reserved:
        raise ValueError('Output asset filenames collide.')
    outputs = {metadata_name: raw + b'\n', 'tempo-update-index.json': _canonical({'releases': previous + [envelope]}) + b'\n',
               'tempo-update-sha256.json': _canonical({a['name']: a['sha256'] for a in assets}) + b'\n'}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.update-package-', dir=output) as temp:
        staging = Path(temp)
        for src, asset in zip(files, assets):
            dest = staging / asset['name']
            with src.open('rb') as original, dest.open('xb') as copy:
                shutil.copyfileobj(original, copy, 1024 * 1024)
            if dest.stat().st_size != asset['size'] or _digest(dest) != asset['sha256']:
                raise ValueError('Source asset changed while packaging.')
        for name, data in outputs.items():
            (staging / name).write_bytes(data)
        for path in staging.iterdir():
            dest = output / path.name
            if dest.exists() and (not dest.is_file() or dest.is_symlink() or _digest(dest) != _digest(path)):
                raise ValueError('Output already contains a different artifact: ' + path.name)
        created = []
        try:
            for path in staging.iterdir():
                dest = output / path.name
                if not dest.exists():
                    os.link(path, dest)  # Atomic creation; never overwrite a racing writer.
                    created.append(dest)
        except BaseException:
            for dest in created:
                dest.unlink(missing_ok=True)
            raise
    return release


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--inventory-only', action='store_true')
    parser.add_argument('--version')
    parser.add_argument('--commit')
    parser.add_argument('--channel', choices=('stable', 'prerelease'))
    parser.add_argument('--sequence', type=int)
    parser.add_argument('--expires-at')
    parser.add_argument('--signing-key', type=Path)
    parser.add_argument('--key-id')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--certification', type=Path)
    parser.add_argument('--native-updater', type=Path)
    parser.add_argument('--native-patch', type=Path, default=PIN.parent/'0001-tempo-recovery.patch')
    parser.add_argument('--previous-index', type=Path)
    parser.add_argument('--trusted-keys', type=Path)
    parser.add_argument('--patch-spec', type=Path)
    parser.add_argument('--base-package-dir', type=Path)
    parser.add_argument('--storage-schema-version', type=int, default=1)
    parser.add_argument('--nvidia-descriptor', type=Path)
    parser.add_argument('--release-notes', type=Path)
    args = parser.parse_args(argv)
    if args.inventory_only:
        print(json.dumps(inventory_package(args.package), indent=2))
        return
    required = ('version', 'commit', 'channel', 'sequence', 'expires_at', 'signing_key', 'key_id', 'output', 'certification')
    missing = [name for name in required if getattr(args, name) is None]
    if missing:
        parser.error('Release output requires: ' + ', '.join('--'+x.replace('_', '-') for x in missing))
    result = package(full_package=args.package, version=args.version, commit=args.commit,
                     channel=args.channel, sequence=args.sequence, expires_at=args.expires_at,
                     signing_key=args.signing_key, key_id=args.key_id, output=args.output,
                     certification=args.certification, previous_index=args.previous_index,
                     trusted_keys=args.trusted_keys, patch_spec=args.patch_spec,
                     nvidia_descriptor=args.nvidia_descriptor, native_updater=args.native_updater,
                     native_patch=args.native_patch, release_notes=args.release_notes,
                     base_package_dir=args.base_package_dir, storage_schema_version=args.storage_schema_version)
    print(json.dumps({'version': result.version, 'commit': result.commit, 'output': str(args.output)}))


if __name__ == '__main__':
    main()
