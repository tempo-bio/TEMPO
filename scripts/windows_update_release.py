"""Retain and verify exact Windows release sets; never upload or advertise them.

A release-set signature binds installers, offline repair, update packages,
matching Git source and reviewed evidence. Preparing a local set does not prove
durable storage. Reverify a separately recovered copy using the printed digest.
Publication checks are read-only and require an independently supplied trust map.
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
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import quote, urlsplit
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from scripts.build_update_repair import read_keys
from scripts.configure_windows_updates import candidate
from scripts.package_windows_updates import PIN, _canonical, _certified, _digest, _read_json, reviewed_native_pin
from tempo_nvidia_manifest import JoinedFiles
from tempo_update_manifest import (ARCHITECTURE, PACKAGE_ID, inventory_digest,
    load_verified_manifest, validate_path, verify_asset, verify_zip, _version)
from tempo_update_migration import _safe
from tempo_update_service import FEEDS, DOWNLOAD_HOSTS

ROOT = Path(__file__).resolve().parents[1]
CATALOG = 'tempo-release-set.json'
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_PUBLIC = 2 * 1024**3  # Release assets must be strictly below this limit.
ROLES = frozenset({'installer', 'repair', 'updater', 'patch', 'certification',
    'native_certification', 'acceptance', 'provenance', 'verification_contract', 'dependency_sources'})
CHECKS = frozenset({'fresh_install', 'legacy_cpu_migration', 'two_successive_releases',
    'skipped_release', 'delta_full_fallback', 'interrupted_replacement', 'offline_recovery',
    'state_preservation', 'uninstall_repair', 'concurrent_process_refusal',
    'hardware_shutdown', 'optional_package_transition', 'code_signing_review',
    'source_regressions', 'repository_privacy', 'dependency_sources', 'frozen_runtime', 'interface_review',
    'trust_bootstrap_and_rotation'})


def _file(path):
    path = _safe(path, missing_ok=False)
    if not path.is_file():
        raise ValueError('Release input must be an ordinary file.')
    return path


def _name(value):
    validate_path(value)
    if '/' in value:
        raise ValueError('Expected a single release asset filename.')
    return value


def _load_release(path, keys, channel, now):
    return load_verified_manifest(_file(path).read_bytes(), keys, now=now,
                                  min_sequence=0, expected_channel=channel)


def _index(path, keys, channel, now):
    data = _read_json(_file(path), 32 * 1024**2)
    if not isinstance(data, dict) or set(data) != {'releases'} or not isinstance(data['releases'], list) or not 1 <= len(data['releases']) <= 20:
        raise ValueError('Invalid release-set update index.')
    releases = [load_verified_manifest(_canonical(item), keys, now=now,
                min_sequence=0, expected_channel=channel) for item in data['releases']]
    if len({r.version for r in releases}) != len(releases) or len({r.sequence for r in releases}) != len(releases):
        raise ValueError('Duplicate release identity or sequence in update index.')
    return releases


def _source_archive(repository, commit, output, *, prefix='TEMPO/'):
    actual = subprocess.check_output(['git', 'rev-parse', '--verify', commit + '^{commit}'],
                                     cwd=repository, text=True).strip()
    if actual != commit:
        raise ValueError('The matching full source commit is unavailable.')
    subprocess.run(['git', 'archive', '--format=tar', '--prefix=' + prefix,
                    '--output=' + str(output), commit], cwd=repository, check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)


def _acceptance(path, artifacts, release):
    value = _read_json(path)
    hashes = {role: _digest(artifacts[role]) for role in ('installer', 'repair', 'updater')}
    hashes['full_package'] = release.full.sha256
    required = CHECKS | ({'legacy_nvidia_migration', 'matching_worker_inference'} if release.nvidia_descriptor else set())
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int or value.get('schema_version') != 1
            or value.get('release_certified') is not True or value.get('source_commit') != release.commit
            or value.get('version') != release.version or value.get('artifact_sha256') != hashes
            or not isinstance(value.get('checks'), dict)
            or any(value['checks'].get(name) is not True for name in required)):
        raise ValueError('Reviewed full-app acceptance for these exact artifacts is incomplete. Candidate checks cannot certify a release.')
    signing = value.get('code_signing')
    if (not isinstance(signing, dict) or signing.get('mode') not in ('authenticode', 'unsigned-approved')
            or not isinstance(signing.get('decision_reference'), str)
            or not signing['decision_reference'].strip() or not 1 <= len(signing['decision_reference']) <= 2048):
        raise ValueError('An explicit reviewed Windows code-signing decision is required.')
    evidence = value.get('evidence')
    if not isinstance(evidence, dict):
        raise ValueError('Acceptance requires retained, hash-bound evidence for each check.')
    for check in required:
        item = evidence.get(check)
        role = 'report_' + check
        if (not isinstance(item, dict) or set(item) != {'sha256'} or role not in artifacts
                or _digest(artifacts[role]) != item['sha256']):
            raise ValueError('Acceptance evidence is missing or changed: ' + check)
    return required


def _payload(full, artifacts, expected, release):
    with zipfile.ZipFile(full) as package:
        config = json.loads(package.read('lib/app/_internal/assets/tempo-updates.json'))
        if config != expected:
            raise ValueError('Packaged trust/configuration differs from the certified release configuration.')
        bindings = {'repair': 'lib/app/TEMPORepair.exe',
                    'provenance': 'lib/app/_internal/assets/build-provenance.json',
                    'dependency_sources': 'lib/app/_internal/third-party-sources/dependency-sources.json'}
        for role, name in bindings.items():
            record = next((r for r in release.inventory if r.path == name), None)
            if record is None or record.sha256 != _digest(artifacts[role]):
                raise ValueError('Packaged artifact differs from release inputs: ' + role)
    provenance = _read_json(artifacts['provenance'])
    contract = _read_json(artifacts['verification_contract'])
    if provenance.get('commit') != release.commit or provenance.get('profile') != 'cpu' or contract.get('commit') != release.commit:
        raise ValueError('Build provenance/verification contract differs from the signed CPU source revision.')


def prepare(*, manifest, index, package_dir, artifacts, public_keys, signing_key, key_id,
            output, dependency_cache, native_repository, repository=ROOT, pin_path=PIN, previous_index=None, base_package_dir=None,
            channel='stable', now=None):
    """Create an immutable signed set after verifying reviewed release inputs.

    Artifacts maps the required ROLES and report_<check> roles to local files.
    This function never creates acceptance claims, changes trust, runs a Windows
    binary or configures storage retention. The private key is never copied.
    """
    now = now or datetime.now(timezone.utc)
    output = _safe(output)
    if output.exists():
        raise ValueError('Release sets are immutable. Use an absent output directory.')
    keys = read_keys(public_keys)
    private = serialization.load_pem_private_key(_file(signing_key).read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError('Release-set signing requires an Ed25519 private key.')
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if keys.get(key_id) != public:
        raise ValueError('The signing key is not in the independently supplied public trust map.')
    release = _load_release(manifest, keys, channel, now)
    if not isinstance(artifacts, dict) or not ROLES <= set(artifacts) or any(
            not isinstance(role, str) or (role not in ROLES and not re.fullmatch('report_[a-z][a-z0-9_]{0,80}', role)) for role in artifacts):
        raise ValueError('Release-set artifact roles are incomplete or unknown.')
    artifacts = {role: _file(path) for role, path in artifacts.items()}
    private_path = _file(signing_key)
    private_hash = _digest(private_path)
    if any(os.path.samefile(path, private_path) or _digest(path) == private_hash for path in artifacts.values()):
        raise ValueError('Private signing material cannot be retained as a release artifact.')
    full = _file(Path(package_dir) / release.full.name)
    verify_asset(full, release.full); verify_zip(full, release.inventory)
    _certified(pin_path, artifacts['certification'], release.commit, full, artifacts['updater'], artifacts['patch'])
    expected = candidate(public_keys, artifacts['updater'], artifacts['repair'],
        artifacts['native_certification'], release.commit, channel=channel,
        pin_path=pin_path, patch_path=artifacts['patch'])
    _payload(full, artifacts, expected, release)
    required = _acceptance(artifacts['acceptance'], artifacts, release)
    releases = _index(index, keys, channel, now)
    if not releases or max(releases, key=lambda r: r.sequence).signed_payload != release.signed_payload:
        raise ValueError('Update index does not advertise this exact release at the highest sequence.')
    bases = _index(previous_index, keys, channel, EPOCH) if previous_index else []
    if any(base.sequence >= release.sequence or _version(base.version) >= _version(release.version) for base in bases):
        raise ValueError('Release version and sequence must advance beyond the authenticated previous index.')
    if release.patches and base_package_dir is None:
        raise ValueError('Every advertised patch requires its exact retained full base.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.release-set-', dir=output.parent) as temp:
        staging = Path(temp)
        files, public_names = [], {CATALOG.casefold(), 'tempo-update-index.json'}
        def retain(source, role, *, name=None, version=None, publish=False, feed=False):
            source = _file(source)
            name = _name(name or source.name)
            group = 'feed' if feed else ('public' if publish else 'private/' + role)
            destination = staging / group / name
            if destination.exists():
                raise ValueError('Release-set artifact paths collide.')
            if publish and (name.casefold() in public_names or not 0 < source.stat().st_size < MAX_PUBLIC):
                raise ValueError('Published asset name collides or exceeds the release size limit.')
            if publish:
                public_names.add(name.casefold())
            destination.parent.mkdir(parents=True, exist_ok=True)
            before = _digest(source)
            shutil.copyfile(source, destination)
            if _digest(destination) != before or _digest(source) != before:
                raise ValueError('Release input changed while retaining it.')
            url = (FEEDS[channel] if feed else
                f'https://github.com/tempo-bio/TEMPO/releases/download/v{version or release.version}/{quote(name, safe="-._~")}') if publish or feed else None
            files.append(dict(role=role, path=destination.relative_to(staging).as_posix(),
                              size=destination.stat().st_size, sha256=before, url=url, feed=feed))
        for role in sorted(artifacts):
            retain(artifacts[role], role, publish=role in ('installer', 'repair', 'dependency_sources', 'patch'))
        retain(public_keys, 'public_keys')
        retain(pin_path, 'native_pin', name='velopack-pin.json', publish=True)
        retain(manifest, 'release_manifest', name=f'tempo-update-v{release.version}.json', publish=True)
        retain(index, 'update_index', name='tempo-update-index.json', feed=True)
        if previous_index:
            retain(previous_index, 'previous_index')
        retain(full, 'full_package', publish=True)
        sources_retained = {}
        def retain_sources(index_path, lock_name):
            from scripts.prepare_dependency_sources import validate, retain_source
            source_index = _read_json(index_path)
            lock = staging / 'source-validation.lock'
            lock.write_bytes(subprocess.check_output(['git', 'show', release.commit + ':' + lock_name], cwd=repository))
            validate(source_index, lock)
            lock.unlink()
            for source in source_index['sources']:
                name = _name(source['filename'])
                path = _file(Path(dependency_cache) / name)
                retain_source(source, _safe(dependency_cache), fetch=False)
                identity = (source['size'], source['sha256'])
                if name in sources_retained:
                    if sources_retained[name] != identity:
                        raise ValueError('Dependency source archives collide between build profiles.')
                    continue
                retain(path, 'dependency_archive', publish=True)
                sources_retained[name] = identity
        retain_sources(artifacts['dependency_sources'], 'requirements-windows.lock')
        for patch in release.patches:
            path = _file(Path(package_dir) / patch.asset.name)
            verify_asset(path, patch.asset)
            retain(path, 'delta', publish=True)
            matches = [base for base in bases if (base.version, base.commit, inventory_digest(base.inventory)) ==
                       (patch.base_version, patch.base_commit, patch.base_inventory_sha256)]
            if len(matches) != 1:
                raise ValueError('Advertised delta base is not an authenticated retained release.')
            base = matches[0]
            path = _file(Path(base_package_dir) / base.full.name)
            verify_asset(path, base.full); verify_zip(path, base.inventory)
            retain(path, 'previous_full', version=base.version, publish=True)
            metadata = _file(Path(base_package_dir) / f'tempo-update-v{base.version}.json')
            original = _load_release(metadata, keys, channel, EPOCH)
            if original.signed_payload != base.signed_payload:
                raise ValueError('Retained previous manifest differs from its authenticated index entry.')
            retain(metadata, 'previous_manifest', version=base.version, publish=True)
        if release.nvidia_descriptor is not None:
            from tempo_update_components import packaged_descriptor
            from scripts.package_nvidia_runtime import _verify_archive
            descriptor = packaged_descriptor(release, full)
            metadata = _file(Path(package_dir) / 'nvidia-runtime.json')
            if _read_json(metadata, 16384) != descriptor:
                raise ValueError('Original NVIDIA descriptor differs from signed component metadata.')
            retain(metadata, 'nvidia_descriptor', publish=True)
            paths = []
            for part in descriptor['parts']:
                name = part['url'].rsplit('/', 1)[-1]
                path = _file(Path(package_dir) / name)
                if path.stat().st_size != part['size'] or _digest(path) != part['sha256']:
                    raise ValueError('NVIDIA part differs from signed target metadata.')
                retain(path, 'nvidia_part', publish=True); paths.append(path)
            with JoinedFiles(paths) as archive:
                digest = hashlib.sha256()
                for block in iter(lambda: archive.read(1024**2), b''):
                    digest.update(block)
                if digest.hexdigest() != descriptor['archive_sha256']:
                    raise ValueError('Combined NVIDIA archive differs from the signed target.')
                archive.seek(0); _verify_archive(archive, descriptor)
                archive.seek(0)
                with zipfile.ZipFile(archive) as package:
                    name = 'TEMPO/_internal/third-party-sources/dependency-sources.json'
                    if package.getinfo(name).file_size > 16 * 1024**2:
                        raise ValueError('NVIDIA source index is oversized.')
                    metadata = staging / 'dependency-sources-nvidia.json'
                    metadata.write_bytes(package.read(name))
                retain_sources(metadata, 'requirements-windows-nvidia.lock')
                retain(metadata, 'nvidia_dependency_sources', publish=True); metadata.unlink()
        source = staging / f'TEMPO-v{release.version}-source.tar'
        _source_archive(repository, release.commit, source)
        retain(source, 'source_archive', publish=True); source.unlink()
        source = staging / ('Velopack-' + _read_json(pin_path)['upstream_commit'] + '-source.tar')
        _source_archive(native_repository, _read_json(pin_path)['upstream_commit'], source, prefix='Velopack/')
        retain(source, 'native_source_archive', publish=True); source.unlink()
        signed = dict(schema_version=1, purpose='tempo-windows-release-set', package_id=PACKAGE_ID,
            architecture=ARCHITECTURE, version=release.version, commit=release.commit,
            channel=release.channel, sequence=release.sequence, files=files)
        envelope = dict(signed=signed, signatures=[dict(key_id=key_id,
            signature=base64.b64encode(private.sign(_canonical(signed))).decode('ascii'))])
        (staging / CATALOG).write_bytes(_canonical(envelope) + b'\n')
        verify(staging, public_keys, now=now)
        output.mkdir()  # Exclusive creation also rejects a racing empty directory.
        for child in staging.iterdir():
            if child.name != CATALOG:
                child.rename(output / child.name)
        os.link(staging / CATALOG, output / CATALOG)  # Completion marker last, never overwrite.
    return dict(status='prepared', catalog_sha256=_digest(output / CATALOG),
                version=release.version, commit=release.commit, publication_performed=False,
                durable_storage_verified=False)


def verify(root, public_keys, *, expected_sha256=None, now=None):
    """Verify a recovered set using external trust; historical sets may be expired."""
    root = _safe(root, missing_ok=False)
    path = _file(root / CATALOG)
    if expected_sha256 is not None and _digest(path) != expected_sha256:
        raise ValueError('Release-set catalog differs from the independently retained digest.')
    keys = read_keys(public_keys)
    envelope = _read_json(path)
    if not isinstance(envelope, dict) or set(envelope) != {'signed', 'signatures'}:
        raise ValueError('Invalid release-set envelope.')
    signatures = envelope['signatures']
    if not isinstance(signatures, list) or not 1 <= len(signatures) <= 16:
        raise ValueError('Invalid release-set signatures.')
    valid = False
    for signature in signatures:
        if (not isinstance(signature, dict) or set(signature) != {'key_id', 'signature'}
                or not isinstance(signature['key_id'], str) or not isinstance(signature['signature'], str)):
            raise ValueError('Invalid release-set signature entry.')
        key = keys.get(signature['key_id'])
        if key is not None:
            try:
                Ed25519PublicKey.from_public_bytes(key).verify(base64.b64decode(signature['signature'], validate=True), _canonical(envelope['signed']))
                valid = True
            except InvalidSignature:
                pass
    if not valid:
        raise ValueError('No independently trusted release-set signature verified.')
    value = envelope['signed']
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'purpose', 'package_id',
            'architecture', 'version', 'commit', 'channel', 'sequence', 'files'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['purpose'] != 'tempo-windows-release-set'
            or value['package_id'] != PACKAGE_ID or value['architecture'] != ARCHITECTURE
            or not isinstance(value['files'], list) or not 1 <= len(value['files']) <= 4096):
        raise ValueError('Invalid release-set identity or file inventory.')
    seen, roles = set(), {}
    for item in value['files']:
        if not isinstance(item, dict) or set(item) != {'role', 'path', 'size', 'sha256', 'url', 'feed'}:
            raise ValueError('Invalid release-set file record.')
        if (not isinstance(item['role'], str) or type(item['feed']) is not bool
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])
                or (item['url'] is not None and not isinstance(item['url'], str))):
            raise ValueError('Invalid release-set file role or identity.')
        name = validate_path(item['path'])
        if name.casefold() in seen:
            raise ValueError('Duplicate release-set Windows path.')
        seen.add(name.casefold())
        file = _file(root / name)
        if type(item['size']) is not int or item['size'] < 1 or file.stat().st_size != item['size'] or _digest(file) != item['sha256']:
            raise ValueError('Retained release artifact is missing or changed: ' + item['role'])
        roles.setdefault(item['role'], []).append(file)
    actual = set()
    for directory, dirs, names in os.walk(root, followlinks=False,
            onerror=lambda error: (_ for _ in ()).throw(error)):
        for name in dirs + names:
            _safe(Path(directory) / name, missing_ok=False)
        actual.update((Path(directory) / name).relative_to(root).as_posix() for name in names)
    if actual != {item['path'] for item in value['files']} | {CATALOG}:
        raise ValueError('Recovered release set contains missing or unexpected files.')
    required = ROLES | {'public_keys', 'native_pin', 'release_manifest', 'update_index', 'full_package', 'source_archive', 'native_source_archive'}
    if any(len(roles.get(role, [])) != 1 for role in required):
        raise ValueError('Retained release-set roles are incomplete or ambiguous.')
    release = _load_release(roles['release_manifest'][0], keys, value['channel'], now or EPOCH)
    if (release.version, release.commit, release.sequence) != (value['version'], value['commit'], value['sequence']):
        raise ValueError('Retained release identity differs from its signed manifest.')
    verify_asset(roles['full_package'][0], release.full); verify_zip(roles['full_package'][0], release.inventory)
    artifacts = {role: paths[0] for role, paths in roles.items() if len(paths) == 1}
    _certified(artifacts['native_pin'], artifacts['certification'], release.commit,
               artifacts['full_package'], artifacts['updater'], artifacts['patch'])
    expected = candidate(artifacts['public_keys'], artifacts['updater'], artifacts['repair'],
        artifacts['native_certification'], release.commit, channel=release.channel,
        pin_path=artifacts['native_pin'], patch_path=artifacts['patch'])
    _payload(artifacts['full_package'], artifacts, expected, release)
    _acceptance(artifacts['acceptance'], artifacts, release)
    if max(_index(artifacts['update_index'], keys, release.channel, now or EPOCH),
           key=lambda r: r.sequence).signed_payload != release.signed_payload:
        raise ValueError('Retained update index differs from the release-set target.')
    return value


def _response(opener, url):
    response = opener(urllib.request.Request(url, headers={'User-Agent': 'TEMPO-release-verification'}), timeout=30)
    parsed = urlsplit(response.geturl())
    allowed = {'api.github.com'} if url.startswith('https://api.github.com/') else DOWNLOAD_HOSTS
    if (parsed.scheme != 'https' or parsed.hostname not in allowed or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        response.close()
        raise ValueError('Release verification redirected outside trusted HTTPS hosts.')
    return response


def check_publication(root, public_keys, expected_sha256, *, include_feed=False, opener=None, now=None):
    """Read public bytes/tag only; assets are checked before optional feed readback."""
    now = now or datetime.now(timezone.utc)
    value = verify(root, public_keys, expected_sha256=expected_sha256, now=now)
    opener = opener or urllib.request.urlopen
    def api(resource):
        with _response(opener, 'https://api.github.com/repos/tempo-bio/TEMPO/' + resource) as response:
            raw = response.read(4 * 1024**2 + 1)
        if len(raw) > 4 * 1024**2:
            raise ValueError('Release metadata exceeds its size limit.')
        return json.loads(raw)
    tag = 'v' + value['version']
    release = api('releases/tags/' + quote(tag, safe=''))
    if release.get('draft') is not False or release.get('tag_name') != tag:
        raise ValueError('The versioned public release is unavailable.')
    if not include_feed and release.get('prerelease') is not True:
        raise ValueError('Prepare and verify assets as a public prerelease before advertising the feed.')
    if include_feed:
        if value['channel'] == 'stable':
            latest = api('releases/latest')
            if release.get('prerelease') is not False or latest.get('tag_name') != tag:
                raise ValueError('Stable feed verification requires this exact release to be latest and stable.')
        elif release.get('prerelease') is not True:
            raise ValueError('Prerelease feed verification requires the matching prerelease flag.')
    if api('commits/' + quote(tag, safe='')).get('sha') != value['commit']:
        raise ValueError('The public release tag differs from the certified full source commit.')
    keys = read_keys(public_keys)
    for item in value['files']:
        if item['role'] == 'previous_manifest':
            base = _load_release(Path(root) / item['path'], keys, value['channel'], EPOCH)
            if api('commits/' + quote('v' + base.version, safe='')).get('sha') != base.commit:
                raise ValueError('A retained base release tag differs from its authenticated source commit.')
    verified = []
    catalog = _file(Path(root) / CATALOG)
    remote_catalog = dict(role='release_catalog', path=CATALOG, size=catalog.stat().st_size,
        sha256=_digest(catalog), feed=False,
        url=f'https://github.com/tempo-bio/TEMPO/releases/download/{tag}/{CATALOG}')
    for item in sorted(value['files'] + [remote_catalog], key=lambda f: f['feed']):
        if item['url'] is None or (item['feed'] and not include_feed):
            continue
        if item['feed']:
            if item['url'] != FEEDS[value['channel']]:
                raise ValueError('Unexpected release feed URL.')
        elif not item['url'].startswith('https://github.com/tempo-bio/TEMPO/releases/download/'):
            raise ValueError('Unexpected public release asset URL.')
        digest, count, deadline = hashlib.sha256(), 0, time.monotonic() + 900
        with _response(opener, item['url']) as response:
            while block := response.read(1024**2):
                count += len(block)
                if count > item['size'] or time.monotonic() > deadline:
                    raise ValueError('Served release artifact exceeded its size or verification deadline.')
                digest.update(block)
        if count != item['size'] or digest.hexdigest() != item['sha256']:
            raise ValueError('Served release artifact differs from retained bytes: ' + item['role'])
        verified.append(item['path'])
    return dict(status='verified', commit=value['commit'], verified_assets=verified,
                feed_verified=include_feed, publication_performed=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    create = modes.add_parser('prepare')
    for name in ('manifest', 'index', 'package-dir', 'artifacts', 'public-keys', 'signing-key', 'output', 'dependency-cache', 'native-repository'):
        create.add_argument('--' + name, required=True, type=Path)
    create.add_argument('--key-id', required=True)
    create.add_argument('--native-pin', type=Path)
    create.add_argument('--channel', choices=('stable', 'prerelease'), default='stable')
    create.add_argument('--previous-index', type=Path)
    create.add_argument('--base-package-dir', type=Path)
    for name in ('verify', 'check-publication'):
        command = modes.add_parser(name)
        command.add_argument('--root', required=True, type=Path)
        command.add_argument('--public-keys', required=True, type=Path)
        command.add_argument('--catalog-sha256', required=True)
        if name == 'check-publication':
            command.add_argument('--include-feed', action='store_true')
    args = vars(parser.parse_args())
    mode = args.pop('mode')
    if mode == 'prepare':
        args['artifacts'] = _read_json(args['artifacts'])
        args['pin_path'] = reviewed_native_pin(args.pop('native_pin'))
        result = prepare(**args)
    elif mode == 'verify':
        expected = args.pop('catalog_sha256')
        result = verify(**args, expected_sha256=expected)
        result = dict(status='verified', commit=result['commit'], publication_performed=False)
    else:
        expected = args.pop('catalog_sha256')
        result = check_publication(**args, expected_sha256=expected)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
