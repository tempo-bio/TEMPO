"""Signed Windows update metadata and bounded, Qt-free content verification.

Schema 1 signs the UTF-8 JSON representation of ``signed`` using sorted keys,
compact separators, and no ASCII escaping. Trusted Ed25519 public keys are raw
32-byte values supplied by the caller; this module contains no trust anchors.
Signatures use standard base64. The signed inventory identity is SHA-256 of the
same canonical JSON encoding of its path-sorted array of inventory objects.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import quote
import zipfile

SCHEMA_VERSION = 1
PACKAGE_ID = 'TEMPO.Bio.Desktop'
ARCHITECTURE = 'win-x64'
MAX_METADATA = 16 * 1024 * 1024
MAX_FILES = 30000
MAX_EXPANDED = 32 * 1024**3
MAX_ASSET = 16 * 1024**3
MAX_PATCHES = 1000
_HASH = re.compile(r'^[0-9a-f]{64}$')
_COMMIT = re.compile(r'^[0-9a-f]{40}$')
_SEMVER = re.compile(r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$')
_DEVICE = re.compile(r'^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$', re.I)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError('Unexpected or missing metadata fields.')


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Invalid metadata size or count.')
    return value


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError('Invalid SHA-256 digest.')
    return value


def _commit(value):
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise ValueError('Invalid source revision.')
    return value


def _version(value):
    match = _SEMVER.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError('Invalid semantic version.')
    pre = match.group(4)
    if pre and any(p.isdigit() and len(p) > 1 and p[0] == '0' for p in pre.split('.')):
        raise ValueError('Invalid semantic version prerelease identifier.')
    # Numeric identifiers sort before nonnumeric; a release sorts after prereleases.
    return tuple(int(match.group(i)) for i in (1, 2, 3)) + (
        (1,) if pre is None else (0, *((0, int(p)) if p.isdigit() else (1, p) for p in pre.split('.'))),)


def validate_path(path):
    """Return a canonical relative Windows file path or raise ValueError."""
    if not isinstance(path, str) or not path or len(path) > 1024 or '\\' in path:
        raise ValueError('Invalid inventory path.')
    for part in path.split('/'):
        if (not part or len(part) > 255 or part in ('.', '..') or part[-1] in ' .'
                or _DEVICE.fullmatch(part) or any(ord(c) < 32 or c in '<>:"|?*' for c in part)):
            raise ValueError('Unsafe Windows path.')
    return path


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class FileRecord:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Patch:
    base_version: str
    base_commit: str
    base_inventory_sha256: str
    target_version: str
    target_commit: str
    asset: Asset


@dataclass(frozen=True)
class Release:
    version: str
    commit: str
    channel: str
    sequence: int
    expires_at: datetime
    full: Asset
    inventory: tuple[FileRecord, ...]
    patches: tuple[Patch, ...]
    signed_payload: bytes
    envelope: bytes
    nvidia_descriptor: bytes | None
    release_notes: str
    minimum_updater_version: str
    storage_schema_version: int


@dataclass(frozen=True)
class TransferPlan:
    kind: str
    assets: tuple[Asset, ...]
    target: Release


def _asset(obj, version):
    _fields(obj, ('name', 'url', 'size', 'sha256'))
    name = validate_path(obj['name'])
    if '/' in name or name in ('.', '..'):
        raise ValueError('Asset name must be a single filename.')
    expected = f'https://github.com/tempo-bio/TEMPO/releases/download/v{version}/{quote(name, safe="-._~")}'
    if not isinstance(obj['url'], str) or obj['url'] != expected:
        raise ValueError('Asset URL does not match its release and filename.')
    return Asset(name, expected, _integer(obj['size'], 1, MAX_ASSET), _hash(obj['sha256']))


def _inventory(items):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_FILES:
        raise ValueError('Invalid inventory count.')
    records, seen, total = [], set(), 0
    for item in items:
        _fields(item, ('path', 'size', 'sha256'))
        path = validate_path(item['path'])
        folded = path.casefold()
        if folded in seen:
            raise ValueError('Duplicate inventory path.')
        seen.add(folded)
        size = _integer(item['size'], 0, MAX_EXPANDED)
        total += size
        if total > MAX_EXPANDED:
            raise ValueError('Inventory exceeds expanded size limit.')
        records.append(FileRecord(path, size, _hash(item['sha256'])))
    # A file cannot also be a parent of another file, including on Windows.
    if any('/'.join(p.split('/')[:i]).casefold() in seen for p in (r.path for r in records)
           for i in range(1, len(p.split('/')))):
        raise ValueError('Inventory contains a file/directory collision.')
    return tuple(sorted(records, key=lambda r: r.path))


def inventory_digest(inventory):
    """Return the canonical SHA-256 identity of a validated file inventory."""
    records = _inventory([{'path': r.path, 'size': r.size, 'sha256': r.sha256} for r in inventory])
    return hashlib.sha256(_canonical([{'path': r.path, 'size': r.size, 'sha256': r.sha256} for r in records])).hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key.')
        result[key] = value
    return result


def load_verified_manifest(envelope_bytes, trusted_keys, *, now, min_sequence, expected_channel):
    """Authenticate and validate schema 1. ``now`` must be timezone-aware UTC.

    ``trusted_keys`` maps key IDs to raw 32-byte Ed25519 public keys. At least
    one signature must verify. The caller persists the highest trusted sequence
    and passes it as ``min_sequence`` on later calls; equal sequence is allowed.
    This function does not download assets or establish signed metadata freshness.
    """
    if not isinstance(envelope_bytes, bytes) or len(envelope_bytes) > MAX_METADATA:
        raise ValueError('Invalid metadata size.')
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError('An aware current time is required.')
    _integer(min_sequence, 0, 2**63 - 1)
    if expected_channel not in ('stable', 'prerelease'):
        raise ValueError('Invalid expected channel.')
    try:
        envelope = json.loads(envelope_bytes.decode('utf-8'), object_pairs_hook=_unique_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON constant.')))
        _fields(envelope, ('signed', 'signatures'))
        signatures = envelope['signatures']
        if not isinstance(signatures, list) or not 1 <= len(signatures) <= 16:
            raise ValueError('Invalid signatures.')
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        encoded = _canonical(envelope['signed'])
        valid = False
        for entry in signatures:
            _fields(entry, ('key_id', 'signature'))
            if not isinstance(entry['key_id'], str) or not isinstance(entry['signature'], str):
                raise ValueError('Invalid signature entry.')
            signature = base64.b64decode(entry['signature'], validate=True)
            key = trusted_keys.get(entry['key_id'])
            if key is not None:
                try:
                    Ed25519PublicKey.from_public_bytes(key).verify(signature, encoded)
                    valid = True
                except InvalidSignature:
                    pass
        if not valid:
            raise ValueError('No trusted signature verified.')
        obj = envelope['signed']
        required = {'schema_version', 'package_id', 'version', 'commit', 'architecture', 'channel',
                    'sequence', 'expires_at', 'full', 'inventory', 'patches',
                    'minimum_updater_version', 'storage_schema_version'}
        optional = {'nvidia_descriptor', 'release_notes'}
        if not isinstance(obj, dict) or not required <= set(obj) or set(obj) - required - optional:
            raise ValueError('Unexpected or missing metadata fields.')
        if type(obj['schema_version']) is not int or obj['schema_version'] != SCHEMA_VERSION or obj['package_id'] != PACKAGE_ID or obj['architecture'] != ARCHITECTURE:
            raise ValueError('Incompatible release identity.')
        version = obj['version']
        _version(version)
        if obj['channel'] != expected_channel or (obj['channel'] == 'stable' and '-' in version):
            raise ValueError('Incompatible release channel.')
        seq = _integer(obj['sequence'], 1, 2**63 - 1)
        if seq < min_sequence:
            raise ValueError('Stale release sequence.')
        if not isinstance(obj['expires_at'], str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', obj['expires_at']):
            raise ValueError('Invalid expiration.')
        expires = datetime.strptime(obj['expires_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        if now.astimezone(timezone.utc) >= expires:
            raise ValueError('Expired release metadata.')
        commit = _commit(obj['commit'])
        full = _asset(obj['full'], version)
        inventory = _inventory(obj['inventory'])
        raw_patches = obj['patches']
        if not isinstance(raw_patches, list) or len(raw_patches) > MAX_PATCHES:
            raise ValueError('Invalid patch count.')
        patches = []
        seen = set()
        for item in raw_patches:
            _fields(item, ('base_version', 'base_commit', 'base_inventory_sha256', 'target_version', 'target_commit', 'asset'))
            bv, tv = item['base_version'], item['target_version']
            if _version(bv) >= _version(tv) or tv != version or item['target_commit'] != commit:
                raise ValueError('Invalid patch endpoints.')
            patch = Patch(bv, _commit(item['base_commit']), _hash(item['base_inventory_sha256']), tv, commit, _asset(item['asset'], version))
            identity = (patch.base_version, patch.base_commit, patch.base_inventory_sha256)
            if identity in seen:
                raise ValueError('Duplicate patch base.')
            seen.add(identity)
            patches.append(patch)
        nvidia = obj.get('nvidia_descriptor')
        if nvidia is not None:
            from tempo_nvidia_manifest import validate_descriptor
            validate_descriptor(nvidia, version=version, commit=commit, allow_legacy=False)
            nvidia = _canonical(nvidia)
        elif 'nvidia_descriptor' in obj:
            raise ValueError('Invalid NVIDIA descriptor.')
        notes = obj.get('release_notes', '')
        if not isinstance(notes, str) or len(notes.encode('utf-8')) > 65536:
            raise ValueError('Invalid or oversized release notes.')
        minimum = obj['minimum_updater_version']
        if _version(minimum) > _version('1.2.161'):
            raise ValueError('Release requires a newer updater.')
        storage = _integer(obj['storage_schema_version'], 1, 2**31 - 1)
        return Release(version, commit, obj['channel'], seq, expires, full, inventory, tuple(patches),
                       encoded, envelope_bytes, nvidia, notes, minimum, storage)
    except (UnicodeError, json.JSONDecodeError, TypeError, KeyError, OverflowError) as exc:
        raise ValueError('Malformed release metadata.') from exc


def _verify_stream(stream, size, digest):
    h, count = hashlib.sha256(), 0
    while True:
        block = stream.read(1024 * 1024)
        if not block:
            break
        count += len(block)
        if count > size:
            raise ValueError('File exceeds declared size.')
        h.update(block)
    if count != size or h.hexdigest() != digest:
        raise ValueError('File does not match signed inventory.')


def verify_asset(path, asset):
    """Verify downloaded or cached asset bytes, including their exact size."""
    with open(path, 'rb') as stream:
        _verify_stream(stream, asset.size, asset.sha256)


def _is_link(path):
    info = os.lstat(path)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def verify_directory(root, inventory):
    """Verify every regular file and reject unexpected files and reparse points.

    Caller must hold exclusive ownership of the tree during verification and
    subsequent use. This path-based check alone cannot prevent concurrent swaps.
    """
    records = _inventory([{'path': r.path, 'size': r.size, 'sha256': r.sha256} for r in inventory])
    root = Path(root)
    # Reject links in the root and all ancestors; Windows junctions are reparse points.
    parents = (root, *root.absolute().parents) if os.name == 'nt' else (root,)
    for parent in parents:
        if _is_link(parent):
            raise ValueError('Reparse point in verification path.')
    expected = {r.path: r for r in records}
    found = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if _is_link(path):
                raise ValueError('Reparse point in inventory tree.')
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            record = expected.get(relative)
            if record is None or not stat.S_ISREG(os.lstat(path).st_mode):
                raise ValueError('Unexpected inventory file: ' + relative)
            with path.open('rb') as stream:
                _verify_stream(stream, record.size, record.sha256)
            found.add(relative)
    if found != set(expected):
        missing = sorted(set(expected) - found)
        raise ValueError('Missing inventory file: ' + ', '.join(missing[:10]) +
                         (f' ({len(missing)} files missing)' if len(missing) > 10 else ''))


def verify_zip(path, inventory):
    """Stream and verify ZIP members without extracting; reject links and extras."""
    records = _inventory([{'path': r.path, 'size': r.size, 'sha256': r.sha256} for r in inventory])
    expected = {r.path: r for r in records}
    seen = set()
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES * 2:
            raise ValueError('ZIP has too many members.')
        for entry in entries:
            name = entry.filename.rstrip('/') if entry.is_dir() else entry.filename
            validate_path(name)
            key = name.casefold()
            if key in seen:
                raise ValueError('Duplicate ZIP path.')
            seen.add(key)
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or (entry.external_attr & 0x400):
                raise ValueError('ZIP contains a link or reparse point.')
            if entry.is_dir():
                if not any(p.startswith(name + '/') for p in expected):
                    raise ValueError('Unexpected ZIP directory.')
                continue
            record = expected.get(name)
            if record is None or entry.file_size != record.size:
                raise ValueError('Unexpected ZIP file.')
            with archive.open(entry) as stream:
                _verify_stream(stream, record.size, record.sha256)
        if {r.path.casefold() for r in records} - seen:
            raise ValueError('Missing ZIP file.')


def plan_transfer(manifests, *, installed_version, installed_commit, installed_inventory_sha256,
                  base_verified, channel, max_patches=4):
    """Choose an upgrade to the latest authenticated Release, or raise ValueError.

    Only pass manifests freshly authenticated by load_verified_manifest. A true
    base_verified asserts that the caller just verified the base inventory under
    its transaction lock. Returned assets still require verify_asset on every use.
    Missing/uneconomic chains use the full target. No cache is trusted here.
    """
    _version(installed_version)
    _commit(installed_commit)
    _hash(installed_inventory_sha256)
    _integer(max_patches, 0, 4)
    if type(base_verified) is not bool or channel not in ('stable', 'prerelease') or not manifests:
        raise ValueError('Invalid transfer planning input.')
    if any(not isinstance(m, Release) or m.channel != channel for m in manifests):
        raise ValueError('Wrong release channel.')
    target = max(manifests, key=lambda m: _version(m.version))
    if _version(target.version) <= _version(installed_version):
        raise ValueError('Automatic downgrade or reinstall is not permitted.')
    if not base_verified or max_patches == 0:
        return TransferPlan('full', (target.full,), target)
    # Dynamic search over strictly increasing releases; choose cheapest chain.
    candidates = {(installed_version, installed_commit, installed_inventory_sha256, 0): (0, ())}
    for release in sorted(manifests, key=lambda m: _version(m.version)):
        for patch in release.patches:
            base = (patch.base_version, patch.base_commit, patch.base_inventory_sha256)
            for count in range(max_patches):
                prior = candidates.get((*base, count))
                if prior is None:
                    continue
                total, chain = prior[0] + patch.asset.size, prior[1] + (patch.asset,)
                identity = (release.version, release.commit, inventory_digest(release.inventory), count + 1)
                if identity not in candidates or total < candidates[identity][0]:
                    candidates[identity] = (total, chain)
    choices = [candidates[k] for k in candidates if k[:3] ==
               (target.version, target.commit, inventory_digest(target.inventory))]
    choice = min(choices, default=None, key=lambda item: item[0])
    if choice and choice[0] * 4 < target.full.size * 3:
        return TransferPlan('patch', choice[1], target)
    return TransferPlan('full', (target.full,), target)
