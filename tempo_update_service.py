"""Authenticated update preparation, independent of Qt and native replacement.

Preparation never closes TEMPO or replaces installed files. Native activation
requires a separately certified installation owner; unsupported builds keep the
existing manual-installer route.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from urllib.error import HTTPError
from urllib.parse import urlsplit

from tempo_annotation_store import atomic_bytes
from tempo_update_lock import update_lock
from tempo_update_manifest import (MAX_METADATA, inventory_digest,
    load_verified_manifest, plan_transfer, verify_asset, verify_zip)

MAX_INDEX = 32 * 1024 * 1024
DOWNLOAD_HOSTS = frozenset(('github.com', 'release-assets.githubusercontent.com',
                           'objects.githubusercontent.com'))
FEEDS = {
    'stable': 'https://github.com/tempo-bio/TEMPO/releases/latest/download/tempo-update-index.json',
    'prerelease': 'https://github.com/tempo-bio/TEMPO/releases/download/updates-prerelease/tempo-update-index.json',
}


class UpdateCancelled(RuntimeError):
    pass


class FullDownloadRequired(RuntimeError):
    """A failed patch needs a newly accepted, larger full transfer."""


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate update metadata key.')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON constant.')))


def _write(path, value):
    atomic_bytes(path, json.dumps(value, sort_keys=True, allow_nan=False).encode('utf-8'))


def _check_cancel(cancel):
    if cancel and cancel():
        raise UpdateCancelled('Update cancelled. The installed application is unchanged.')


def _safe_directory(path):
    """Do not redirect update state through existing links/reparse points."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Update storage cannot contain links or reparse points.')
    return path


def read_configuration(bundle_root):
    """Read build-owned public trust configuration; missing configuration is off."""
    path = Path(bundle_root) / 'assets' / 'tempo-updates.json'
    if not path.is_file():
        return None
    if path.stat().st_size > 65536:
        raise ValueError('Update configuration is oversized.')
    value = _json(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError('Invalid update configuration.')
    if value.get('schema_version') != 1 or value.get('enabled') is not True:
        return None
    if value.get('package_id') != 'TEMPO.Bio.Desktop' or value.get('channel') not in FEEDS:
        raise ValueError('Unsupported update configuration.')
    keys = value.get('trusted_keys')
    if not isinstance(keys, dict) or not keys:
        raise ValueError('No release verification keys are configured.')
    for key, encoded in keys.items():
        if not isinstance(key, str) or not isinstance(encoded, str) or len(base64.b64decode(encoded, validate=True)) != 32:
            raise ValueError('Invalid release verification key.')
    # Enabling a download UI is not permission to use an uncertified installer.
    if value.get('native_recovery_certified') is not True:
        raise ValueError('Windows update recovery has not been certified for this build.')
    import re
    for field in ('updater_sha256', 'repair_sha256'):
        if not isinstance(value.get(field), str) or not re.fullmatch('[0-9a-f]{64}', value[field]):
            raise ValueError('Certified native updater and repair identities are required.')
    return value


class UpdateService:
    def __init__(self, root, trusted_keys, *, channel='stable', opener=None, clock=None):
        if channel not in FEEDS or not trusted_keys:
            raise ValueError('A supported channel and trusted release keys are required.')
        self.root = _safe_directory(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.keys = dict(trusted_keys)
        self.channel = channel
        self.opener = opener or urllib.request.urlopen
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.state = 'idle'
        self.releases = ()
        self.envelopes = {}
        self._issued = None

    def _state(self, state, **details):
        self.state = state
        _write(self.root / 'transaction.json', dict(details, schema_version=1, state=state))

    def _response(self, url, *, timeout=15):
        response = self.opener(urllib.request.Request(url, headers={'User-Agent': 'TEMPO-Updater'}), timeout=timeout)
        final = urlsplit(response.geturl())
        if final.scheme != 'https' or final.hostname not in DOWNLOAD_HOSTS or final.username or final.password or final.port not in (None, 443):
            response.close()
            raise ValueError('Update download redirected outside the trusted HTTPS hosts.')
        return response

    def _fetch_index(self, *, cancel=None, timeout=15):
        _check_cancel(cancel)
        deadline = time.monotonic() + 2 * timeout
        blocks, received = [], 0
        try:
            with self._response(FEEDS[self.channel], timeout=timeout) as response:
                while block := response.read(65536):
                    _check_cancel(cancel)
                    received += len(block)
                    if received > MAX_INDEX or time.monotonic() > deadline:
                        raise ValueError('Update index exceeded its size or time limit.')
                    blocks.append(block)
        except HTTPError as error:
            if error.code == 404:
                raise ValueError('The update feed is unavailable. Check again later.') from error
            raise
        index = _json(b''.join(blocks))
        if not isinstance(index, dict) or set(index) != {'releases'} or not isinstance(index['releases'], list) or not 1 <= len(index['releases']) <= 20:
            raise ValueError('Invalid update release index.')
        releases, envelopes, versions, sequences = [], {}, set(), set()
        for envelope in index['releases']:
            _check_cancel(cancel)
            encoded = json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(encoded) > MAX_METADATA:
                raise ValueError('Release metadata exceeded its size limit.')
            release = load_verified_manifest(encoded, self.keys, now=self.clock(), min_sequence=0, expected_channel=self.channel)
            identity = (release.version, release.commit)
            if identity in envelopes or release.version in versions or release.sequence in sequences:
                raise ValueError('Duplicate update release identity, version, or sequence.')
            releases.append(release)
            envelopes[identity] = encoded
            versions.add(release.version)
            sequences.add(release.sequence)
        return tuple(releases), envelopes

    def _check_watermark(self, releases):
        watermark = self.root / ('sequence-' + self.channel + '.json')
        try:
            previous = _json(watermark.read_bytes())
        except FileNotFoundError:
            previous = {'sequence': 0}
        if not isinstance(previous, dict) or type(previous.get('sequence')) is not int or previous['sequence'] < 0:
            raise ValueError('Invalid update sequence watermark.')
        sequence = previous['sequence']
        newest = max(releases, key=lambda release: release.sequence)
        if newest.sequence < sequence:
            raise ValueError('The update index is older than the last verified index.')
        fingerprint = hashlib.sha256(newest.signed_payload).hexdigest()
        if newest.sequence == sequence and sequence and previous.get('sha256') != fingerprint:
            raise ValueError('Update sequence identity changed.')
        return newest, watermark, fingerprint

    def probe(self, *, installed_version, installed_commit, timeout):
        """Check availability at launch without opening an update transaction."""
        releases, _ = self._fetch_index(timeout=timeout)
        newest, _, _ = self._check_watermark(releases)
        try:
            plan = plan_transfer(releases, installed_version=installed_version,
                                 installed_commit=installed_commit, installed_inventory_sha256='0' * 64,
                                 base_verified=False, channel=self.channel)
        except ValueError as exc:
            if str(exc) != 'Automatic downgrade or reinstall is not permitted.':
                raise
            return None
        if plan.target != newest:
            raise ValueError('Newest release sequence does not match version ordering.')
        return newest.version

    def check(self, *, cancel=None):
        with update_lock(self.root / 'packages'):
            self.releases, self.envelopes, self._issued = (), {}, None
            self._state('checking')
            try:
                releases, envelopes = self._fetch_index(cancel=cancel)
                newest, watermark, fingerprint = self._check_watermark(releases)
                _check_cancel(cancel)
                _write(watermark, {'sequence': newest.sequence, 'sha256': fingerprint})
                self.releases, self.envelopes = releases, envelopes
                self._state('available', channel=self.channel)
                return self.releases
            except UpdateCancelled:
                self._state('cancelled')
                raise
            except Exception:
                self._state('failed')
                raise

    def _authenticate(self, release):
        """Never trust constructible record fields without its signed envelope."""
        checked = load_verified_manifest(release.envelope, self.keys, now=self.clock(),
                                         min_sequence=0, expected_channel=self.channel)
        if checked != release:
            raise ValueError('Release differs from its signed envelope.')
        return checked

    def _plan_locked(self, *, installed_version, installed_commit, base_release, base_package, force_full):
        verified, identity = False, '0' * 64
        releases = tuple(self._authenticate(r) for r in self.releases)
        if not releases:
            raise ValueError('Check and authenticate releases before planning.')
        watermark = _json((self.root / ('sequence-' + self.channel + '.json')).read_bytes())
        newest = max(releases, key=lambda r: r.sequence)
        if (not isinstance(watermark, dict) or type(watermark.get('sequence')) is not int
                or newest.sequence != watermark['sequence']
                or hashlib.sha256(newest.signed_payload).hexdigest() != watermark.get('sha256')):
            raise ValueError('Authenticated releases do not match the trusted sequence.')
        if base_release is not None:
            base_release = self._authenticate(base_release)
            if (base_release.version, base_release.commit) != (installed_version, installed_commit):
                raise ValueError('The saved base does not match the installed application.')
        if base_release is not None and base_package is not None and not force_full:
            try:
                verify_zip(_safe_directory(base_package), base_release.inventory)
                verified = True
                identity = inventory_digest(base_release.inventory)
            except (OSError, ValueError, zipfile.BadZipFile):
                verified = False
        result = plan_transfer(releases, installed_version=installed_version,
            installed_commit=installed_commit, installed_inventory_sha256=identity,
            base_verified=verified, channel=self.channel)
        if result.target != newest:
            raise ValueError('Newest release sequence does not match version ordering.')
        return result

    def plan(self, *, installed_version, installed_commit, base_release=None, base_package=None, force_full=False):
        with update_lock(self.root / 'packages'):
            self._issued = None
            result = self._plan_locked(installed_version=installed_version, installed_commit=installed_commit,
                                       base_release=base_release, base_package=base_package, force_full=force_full)
            self._issued = (result, installed_version, installed_commit, base_release,
                            str(Path(base_package).absolute()) if base_package is not None else None, force_full)
            return result

    def recovery_package_path(self, release):
        return self.root / 'packages' / 'recovery-base' / release.full.name

    def _download(self, asset, *, cancel, progress, cache=None):
        cache = _safe_directory(cache if cache is not None else self.root / 'packages')
        cache.mkdir(exist_ok=True)
        path = cache / asset.name
        _safe_directory(path)
        _check_cancel(cancel)
        if path.exists():
            try:
                verify_asset(path, asset)
                _check_cancel(cancel)
                progress(asset.size, asset.size)
                return path
            except (ValueError, OSError):
                path.unlink()  # Only this service's named cache entry is removed.
        fd, temporary = tempfile.mkstemp(prefix='.download-', dir=cache)
        received, started = 0, time.monotonic()
        try:
            with os.fdopen(fd, 'wb') as output, self._response(asset.url) as response:
                while block := response.read(1024 * 1024):
                    _check_cancel(cancel)
                    received += len(block)
                    if received > asset.size or time.monotonic() - started > 1800:
                        raise ValueError('Update exceeded its declared size or download deadline.')
                    output.write(block)
                    progress(received, asset.size)
                output.flush()
                os.fsync(output.fileno())
            _check_cancel(cancel)
            verify_asset(temporary, asset)
            os.replace(temporary, path)
            return path
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def prepare(self, plan, *, base_release=None, base_package=None, patcher=None,
                patcher_sha256=None, cancel=None, progress=lambda received, total: None,
                report_stage=lambda stage: None):
        """Download and verify a target; never execute native apply or modify the app."""
        with update_lock(self.root / 'packages'):
            try:
                report_stage('checking_base')
                _check_cancel(cancel)
                if self._issued is None:
                    raise ValueError('An authenticated plan is required.')
                issued, version, commit, expected_base, expected_path, force_full = self._issued
                current_path = str(Path(base_package).absolute()) if base_package is not None else None
                if plan != issued or base_release != expected_base or current_path != expected_path:
                    raise ValueError('Transfer plan or base changed after planning.')
                fresh = self._plan_locked(installed_version=version, installed_commit=commit,
                                          base_release=base_release, base_package=base_package, force_full=force_full)
                if fresh != issued:
                    raise ValueError('The verified base or selected transfer changed.')
                # Both reconstruction and later native staging need full-sized storage.
                expanded = sum(item.size for item in plan.target.inventory)
                required = 2 * expanded + plan.target.full.size + sum(a.size for a in plan.assets)
                fetch_base = base_release is not None and base_package is None
                if base_release is not None:
                    required += base_release.full.size  # The offline recovery copy.
                if fetch_base:
                    if base_release not in self.releases:
                        raise ValueError('The signed update index does not contain the installed recovery base.')
                    required += base_release.full.size  # The verified download.
                if shutil.disk_usage(self.root).free < required:
                    raise OSError('Insufficient free space to prepare and recover this update.')
                self._state('downloading', version=plan.target.version, commit=plan.target.commit)
                total, completed, files = sum(a.size for a in plan.assets), 0, []
                if fetch_base:
                    total += base_release.full.size
                    report_stage('downloading_recovery')
                    base_package = self._download(base_release.full, cancel=cancel,
                        progress=lambda received, _size: progress(received, total),
                        cache=self.recovery_package_path(base_release).parent)
                    report_stage('verifying_recovery')
                    verify_zip(base_package, base_release.inventory)
                    completed = base_release.full.size
                report_stage('downloading_update')
                for asset in plan.assets:
                    try:
                        files.append(self._download(asset, cancel=cancel,
                            progress=lambda received, _size, offset=completed: progress(offset + received, total)))
                    except (OSError, ValueError, HTTPException) as exc:
                        if plan.kind == 'patch':
                            raise FullDownloadRequired('The incremental download failed. Accept the full download or try later.') from exc
                        raise
                    completed += asset.size
                self._state('verifying', version=plan.target.version, commit=plan.target.commit)
                if plan.kind == 'patch':
                    output = None
                    try:
                        if base_package is None or base_release is None or patcher is None:
                            raise ValueError('No verified base or patch engine is available.')
                        report_stage('checking_base')
                        verify_zip(base_package, base_release.inventory)
                        patcher = _safe_directory(patcher)
                        actual = hashlib.sha256(patcher.read_bytes()).hexdigest()
                        if not patcher_sha256 or actual != patcher_sha256:
                            raise ValueError('The native patch engine identity could not be verified.')
                        output = self.root / 'packages' / ('prepared-' + plan.target.full.sha256 + '.nupkg')
                        output.unlink(missing_ok=True)
                        command = [str(patcher), 'patch', '--old', str(Path(base_package).resolve())]
                        for path in files:
                            command += ['--delta', str(path.resolve())]
                        command += ['--output', str(output.resolve())]
                        options = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)} if os.name == 'nt' else {'start_new_session': True}
                        report_stage('rebuilding_update')
                        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
                        started = time.monotonic()
                        try:
                            while process.poll() is None:
                                _check_cancel(cancel)
                                if time.monotonic() - started > 900:
                                    raise TimeoutError('Update reconstruction timed out.')
                                try:
                                    process.wait(timeout=.1)
                                except subprocess.TimeoutExpired:
                                    pass
                            if process.returncode:
                                raise ValueError('Update reconstruction failed.')
                        finally:
                            if process.poll() is None:
                                from tempo_process import stop_tree
                                stop_tree(process)
                        report_stage('verifying_update')
                        verify_zip(output, plan.target.inventory)
                    except UpdateCancelled:
                        if output is not None:
                            output.unlink(missing_ok=True)
                        raise
                    except (OSError, ValueError, TimeoutError, zipfile.BadZipFile) as exc:
                        if output is not None:
                            output.unlink(missing_ok=True)
                        raise FullDownloadRequired('The incremental update could not be prepared. Accept the full download or defer installation.') from exc
                elif plan.kind == 'full':
                    if len(files) != 1 or plan.assets != (plan.target.full,):
                        raise ValueError('Invalid full transfer plan.')
                    output = files[0]
                    report_stage('verifying_update')
                    verify_zip(output, plan.target.inventory)
                else:
                    raise ValueError('Unknown update transfer kind.')
                _check_cancel(cancel)
                identity = inventory_digest(plan.target.inventory)
                record = dict(schema_version=1, version=plan.target.version,
                              commit=plan.target.commit, inventory_sha256=identity,
                              package=str(output.resolve()), channel=self.channel)
                atomic_bytes(self.root / 'prepared-release.json', self.envelopes[(plan.target.version, plan.target.commit)])
                _write(self.root / 'prepared.json', record)
                self._state('ready', **record)
                return record
            except UpdateCancelled:
                self._state('cancelled')
                raise
            except Exception:
                self._state('failed')
                raise

    def verified_prepared(self):
        """Reauthenticate and recheck persisted preparation before any handoff."""
        with update_lock(self.root / 'packages'):
            return self._verified_prepared_locked()

    def _verified_prepared_locked(self):
        record = _json((self.root / 'prepared.json').read_bytes())
        envelope = (self.root / 'prepared-release.json').read_bytes()
        release = load_verified_manifest(envelope, self.keys, now=self.clock(), min_sequence=0, expected_channel=self.channel)
        expected = (release.version, release.commit, inventory_digest(release.inventory))
        if not isinstance(record, dict) or record.get('schema_version') != 1 or record.get('channel') != self.channel or (record.get('version'), record.get('commit'), record.get('inventory_sha256')) != expected:
            raise ValueError('Prepared release identity changed.')
        watermark_path = self.root / ('sequence-' + self.channel + '.json')
        watermark = _json(watermark_path.read_bytes())
        if (not isinstance(watermark, dict) or type(watermark.get('sequence')) is not int
                or release.sequence != watermark['sequence']
                or watermark.get('sha256') != hashlib.sha256(release.signed_payload).hexdigest()):
            raise ValueError('Prepared release is stale or differs from the trusted sequence.')
        path = _safe_directory(record['package'])
        if path.parent != self.root / 'packages':
            raise ValueError('Prepared package leaves update storage.')
        verify_zip(path, release.inventory)
        return release, path
