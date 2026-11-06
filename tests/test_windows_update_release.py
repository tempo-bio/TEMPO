"""Release retention/publication preflight must never trust candidate assertions."""
import base64
from datetime import datetime, timezone
import io
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import windows_update_release as retention
from scripts.configure_windows_updates import candidate
from scripts.package_windows_updates import REQUIRED_CHECKS, _canonical, _digest, package
from tempo_update_manifest import inventory_digest


class Response(io.BytesIO):
    def __init__(self, value, url):
        super().__init__(value)
        self.url = url

    def geturl(self):
        return self.url


class WindowsReleaseSetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repository = self.root / 'repository'
        self.repository.mkdir()
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=self.repository, stderr=subprocess.DEVNULL).decode().strip()
        git('init', '-q')
        (self.repository / 'tempo.py').write_text('# isolated release-set source fixture\n')
        self.cache = self.root / 'source-cache'
        self.cache.mkdir()
        source = b'isolated dependency source fixture'
        source_path = self.cache / 'fixture-1.0.tar.gz'
        source_path.write_bytes(source)
        source_url = 'https://example.invalid/fixture-1.0.tar.gz'
        lock = f'fixture @ {source_url} --hash=sha256:{_digest(source_path)}\n'.encode()
        for name in ('requirements-windows.lock', 'requirements-windows-nvidia.lock'):
            (self.repository / name).write_bytes(lock)
        self.source_index = dict(schema=1, windows_lock_sha256=hashlib.sha256(lock).hexdigest(), sources=[dict(
            id='fixture', filename=source_path.name, url=source_url, sha256=_digest(source_path), size=len(source),
            version='1.0', license='MIT', basis='isolated unit fixture', required_by={'fixture': '1.0'})])
        git('add', 'tempo.py', 'requirements-windows.lock', 'requirements-windows-nvidia.lock')
        git('-c', 'user.name=Release fixture', '-c', 'user.email=fixture'+'@'+'example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '-qm', 'release-set fixture')
        self.commit = git('rev-parse', 'HEAD')
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.key = Ed25519PrivateKey.generate()
        self.private = self.root / 'private.pem'
        self.private.write_bytes(self.key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.keys = self.put('public-keys.json', _canonical({'release': base64.b64encode(self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode('ascii')}))
        self.artifacts = {role: self.put(role + '.bin', role.encode()) for role in retention.ROLES}
        self.artifacts['installer'] = self.put('TEMPO-Setup.exe', b'installer fixture')
        self.artifacts['repair'] = self.put('TEMPORepair.exe', b'repair fixture')
        self.artifacts['updater'] = self.put('Update.exe', b'updater fixture')
        self.artifacts['patch'] = self.put('native.patch', b'patch fixture')
        self.artifacts['provenance'] = self.put('build-provenance.json', _canonical({'commit': self.commit, 'profile': 'cpu'}))
        self.artifacts['verification_contract'] = self.put('verification-contract.json', _canonical({'commit': self.commit}))
        self.artifacts['dependency_sources'] = self.put('dependency-sources.json', _canonical(self.source_index))
        pin = dict(schema_version=1, certification_status='certified', upstream_commit=self.commit,
            updater_sha256=_digest(self.artifacts['updater']), patch_sha256=_digest(self.artifacts['patch']),
            required_checks=sorted(REQUIRED_CHECKS))
        self.pin = self.put('pin.json', _canonical(pin))
        native = dict(schema_version=1, source_commit=self.commit, upstream_commit=pin['upstream_commit'],
            updater_sha256=pin['updater_sha256'], patch_sha256=pin['patch_sha256'],
            repair_sha256=_digest(self.artifacts['repair']), public_keys_sha256=_digest(self.keys),
            checks={name: True for name in REQUIRED_CHECKS})
        self.artifacts['native_certification'] = self.put('native-certification.json', _canonical(native))
        config = candidate(self.keys, self.artifacts['updater'], self.artifacts['repair'], self.artifacts['native_certification'],
            self.commit, pin_path=self.pin, patch_path=self.artifacts['patch'])
        full = self.root / 'TEMPO-1.0.2-full.nupkg'
        with zipfile.ZipFile(full, 'w') as source:
            source.writestr('lib/app/TEMPO.exe', b'frozen app fixture')
            source.writestr('lib/app/Squirrel.exe', self.artifacts['updater'].read_bytes())
            source.writestr('lib/app/TEMPORepair.exe', self.artifacts['repair'].read_bytes())
            source.writestr('lib/app/_internal/assets/build-provenance.json', self.artifacts['provenance'].read_bytes())
            source.writestr('lib/app/_internal/assets/tempo-updates.json', _canonical(config))
            source.writestr('lib/app/_internal/third-party-sources/dependency-sources.json', self.artifacts['dependency_sources'].read_bytes())
        self.artifacts['certification'] = self.put('package-certification.json', _canonical(dict(native, full_package_sha256=_digest(full))))
        self.packages = self.root / 'packages'
        self.release = package(full_package=full, version='1.0.2', commit=self.commit, channel='stable',
            sequence=1, expires_at='2027-01-01T00:00:00Z', signing_key=self.private, key_id='release',
            output=self.packages, certification=self.artifacts['certification'], pin_path=self.pin,
            native_updater=self.artifacts['updater'], native_patch=self.artifacts['patch'],
            now=self.now, minimum_updater_version='1.2.161', storage_schema_version=1)
        # Synthetic claims exist only inside this isolated unit fixture. They are
        # never evidence that a real installer or physical hardware was accepted.
        acceptance = dict(schema_version=1, release_certified=True, version='1.0.2', source_commit=self.commit,
            artifact_sha256={role: _digest(self.artifacts[role]) for role in ('installer', 'repair', 'updater')},
            checks={name: True for name in retention.CHECKS}, evidence={},
            code_signing=dict(mode='unsigned-approved', decision_reference='unit-fixture-only'))
        acceptance['artifact_sha256']['full_package'] = self.release.full.sha256
        for name in retention.CHECKS:
            report = self.put('report-' + name + '.json', _canonical({'fixture': name}))
            self.artifacts['report_' + name] = report
            acceptance['evidence'][name] = {'sha256': _digest(report)}
        self.artifacts['acceptance'] = self.put('acceptance.json', _canonical(acceptance))
        self.options = dict(manifest=self.packages / 'tempo-update-v1.0.2.json',
            index=self.packages / 'tempo-update-index.json', package_dir=self.packages,
            artifacts=self.artifacts, public_keys=self.keys, signing_key=self.private, key_id='release',
            output=self.root / 'release-set', repository=self.repository, native_repository=self.repository,
            dependency_cache=self.cache, pin_path=self.pin, now=self.now)

    def put(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def prepare(self, **kwargs):
        return retention.prepare(**dict(self.options, **kwargs))

    def test_retains_signed_exact_artifacts_and_git_source_without_private_key(self):
        (self.repository / 'untracked-private.txt').write_bytes(b'local-untracked-fixture')
        result = self.prepare()
        output = self.options['output']
        self.assertFalse(result['publication_performed'])
        self.assertFalse(result['durable_storage_verified'])
        value = retention.verify(output, self.keys, expected_sha256=result['catalog_sha256'])
        self.assertEqual(value['commit'], self.commit)
        source = next(item for item in value['files'] if item['role'] == 'source_archive')
        actual = subprocess.check_output(['git', 'get-tar-commit-id'], input=(output / source['path']).read_bytes()).decode().strip()
        self.assertEqual(actual, self.commit)
        self.assertNotIn(b'local-untracked-fixture', (output / source['path']).read_bytes())
        self.assertFalse(any(b'PRIVATE KEY' in path.read_bytes() for path in output.rglob('*') if path.is_file()))
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.prepare()

    def test_unverified_native_pin_and_candidate_only_claims_cannot_prepare_release_set(self):
        pin = json.loads(self.pin.read_bytes())
        pin['certification_status'] = 'unverified'
        self.pin.write_bytes(_canonical(pin))
        with self.assertRaisesRegex(ValueError, 'not been certified'):
            self.prepare()
        self.assertFalse(self.options['output'].exists())
        pin['certification_status'] = 'certified'; self.pin.write_bytes(_canonical(pin))
        acceptance = json.loads(self.artifacts['acceptance'].read_bytes())
        acceptance['release_certified'] = False
        self.artifacts['acceptance'].write_bytes(_canonical(acceptance))
        with self.assertRaisesRegex(ValueError, 'Candidate checks cannot certify'):
            self.prepare()
        self.assertFalse(self.options['output'].exists())

    def test_missing_hardware_evidence_and_changed_installer_or_reports_block(self):
        acceptance = self.artifacts['acceptance']
        original = acceptance.read_bytes()
        value = json.loads(original); value['checks']['hardware_shutdown'] = False
        acceptance.write_bytes(_canonical(value))
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.prepare()
        acceptance.write_bytes(original)
        self.artifacts['report_hardware_shutdown'].write_bytes(b'changed report')
        with self.assertRaisesRegex(ValueError, 'evidence is missing or changed'):
            self.prepare()
        self.assertFalse(self.options['output'].exists())

    def test_recovered_copy_needs_external_trust_and_exact_digest_and_bytes(self):
        result = self.prepare()
        recovered = self.root / 'recovered'
        shutil.copytree(self.options['output'], recovered)
        retention.verify(recovered, self.keys, expected_sha256=result['catalog_sha256'])
        wrong = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        keys = self.put('unrelated-keys.json', _canonical({'release': base64.b64encode(wrong).decode()}))
        with self.assertRaisesRegex(ValueError, 'trusted release-set signature'):
            retention.verify(recovered, keys)
        with self.assertRaisesRegex(ValueError, 'independently retained digest'):
            retention.verify(recovered, self.keys, expected_sha256='f'*64)
        (recovered / 'public/TEMPO-Setup.exe').write_bytes(b'changed installer')
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            retention.verify(recovered, self.keys, expected_sha256=result['catalog_sha256'])

    def test_recovered_set_rejects_extra_files_and_altered_catalog_signatures(self):
        result = self.prepare()
        output = self.options['output']
        extra = output / 'public/unexpected.exe'
        extra.write_bytes(b'not signed')
        with self.assertRaisesRegex(ValueError, 'unexpected files'):
            retention.verify(output, self.keys, expected_sha256=result['catalog_sha256'])
        extra.unlink()
        path = output / retention.CATALOG
        envelope = json.loads(path.read_bytes())
        envelope['signed']['version'] = '1.0.3'
        path.write_bytes(_canonical(envelope))
        with self.assertRaisesRegex(ValueError, 'trusted release-set signature'):
            retention.verify(output, self.keys)

    def test_expired_recovery_material_is_verifiable_but_cannot_pass_publication(self):
        result = self.prepare()
        retention.verify(self.options['output'], self.keys)
        with self.assertRaisesRegex(ValueError, 'Expired release metadata'):
            retention.check_publication(self.options['output'], self.keys, result['catalog_sha256'],
                now=datetime(2030, 1, 1, tzinfo=timezone.utc), opener=lambda *a, **k: self.fail('Expired feed reached network'))

    def publication_opener(self, *, changed_role=None, redirect=False):
        value = retention.verify(self.options['output'], self.keys)
        assets = {item['url']: (self.options['output'] / item['path']).read_bytes()
                  for item in value['files'] if item['url'] is not None}
        assets['https://github.com/tempo-bio/TEMPO/releases/download/v1.0.2/' + retention.CATALOG] = (self.options['output'] / retention.CATALOG).read_bytes()
        changed = next((item['url'] for item in value['files'] if item['role'] == changed_role), None)
        self.requested = []
        self.live_stable = False
        def opener(request, timeout):
            url = request.full_url
            self.requested.append(url)
            if '/releases/tags/' in url:
                return Response(_canonical(dict(draft=False, prerelease=not self.live_stable, tag_name='v1.0.2')), url)
            if url.endswith('/releases/latest'):
                return Response(_canonical(dict(tag_name='v1.0.2')), url)
            if '/commits/' in url:
                return Response(_canonical(dict(sha=self.commit)), url)
            data = b'changed public bytes' if url == changed else assets[url]
            return Response(data, 'https://untrusted.example/file' if redirect else url)
        return opener

    def test_readonly_publication_check_verifies_assets_then_feed_last(self):
        result = self.prepare()
        opener = self.publication_opener()
        args = (self.options['output'], self.keys, result['catalog_sha256'])
        before = retention.check_publication(*args, opener=opener, now=self.now)
        self.assertFalse(before['feed_verified'])
        self.assertFalse(before['publication_performed'])
        self.assertNotIn(retention.FEEDS['stable'], self.requested)
        self.live_stable = True
        after = retention.check_publication(*args, opener=opener, now=self.now, include_feed=True)
        self.assertTrue(after['feed_verified'])
        self.assertEqual(self.requested[-1], retention.FEEDS['stable'])

    def test_corrupt_served_installer_or_redirect_prevents_feed_check(self):
        result = self.prepare()
        args = (self.options['output'], self.keys, result['catalog_sha256'])
        for change in ({'changed_role': 'installer'}, {'redirect': True}):
            with self.subTest(change=change):
                opener = self.publication_opener(**change)
                self.live_stable = True
                with self.assertRaises(ValueError):
                    retention.check_publication(*args, opener=opener, now=self.now, include_feed=True)
                self.assertNotIn(retention.FEEDS['stable'], self.requested)

    def test_patch_set_requires_and_preserves_exact_authenticated_previous_full(self):
        full = self.root / self.release.full.name
        base = self.root / 'TEMPO-1.0.1-full.nupkg'
        shutil.copyfile(full, base)
        base_output = self.root / 'base-release'
        args = dict(commit=self.commit, channel='stable', sequence=1, expires_at='2027-01-01T00:00:00Z',
            signing_key=self.private, key_id='release', pin_path=self.pin,
            certification=self.artifacts['certification'], native_updater=self.artifacts['updater'],
            native_patch=self.artifacts['patch'], now=self.now, minimum_updater_version='1.2.161', storage_schema_version=1)
        previous = package(full_package=base, version='1.0.1', output=base_output, **args)
        delta = self.put('TEMPO-1.0.2-delta.nupkg', b'isolated delta fixture')
        spec = self.put('patch-spec.json', _canonical([dict(base_version=previous.version, base_commit=previous.commit,
            base_inventory_sha256=inventory_digest(previous.inventory), file=str(delta))]))
        output = self.root / 'patch-release'
        def reconstruct(command, **kwargs):
            shutil.copyfile(full, command[command.index('--output') + 1])
        with patch('scripts.package_windows_updates.subprocess.run', side_effect=reconstruct):
            self.release = package(full_package=full, version='1.0.2', output=output,
                **dict(args, sequence=2, previous_index=base_output / 'tempo-update-index.json',
                       trusted_keys=self.keys, patch_spec=spec, base_package_dir=base_output))
        self.options.update(manifest=output / 'tempo-update-v1.0.2.json', index=output / 'tempo-update-index.json',
                            package_dir=output, previous_index=base_output / 'tempo-update-index.json', base_package_dir=base_output)
        retained_base = base_output / base.name
        original = retained_base.read_bytes()
        retained_base.write_bytes(b'rebuilt or changed old full')
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.options['output'].exists())
        retained_base.write_bytes(original)
        result = self.prepare()
        value = retention.verify(self.options['output'], self.keys, expected_sha256=result['catalog_sha256'])
        record = next(item for item in value['files'] if item['role'] == 'previous_full')
        self.assertEqual((self.options['output'] / record['path']).read_bytes(), original)
        self.assertEqual(sum(item['role'] == 'delta' for item in value['files']), 1)

    def test_nvidia_requires_hardware_evidence_and_retains_all_signed_parts(self):
        from tests.test_update_components import fixture
        worker, marker, data = fixture('1.0.2', self.commit, b'isolated NVIDIA worker fixture')
        descriptor = json.loads(worker.nvidia_descriptor)
        stream = io.BytesIO(data)
        with zipfile.ZipFile(stream, 'a') as runtime:
            runtime.writestr('TEMPO/_internal/third-party-sources/dependency-sources.json', _canonical(self.source_index))
        data = stream.getvalue()
        descriptor.update(archive_size=len(data), archive_sha256=hashlib.sha256(data).hexdigest())
        descriptor['parts'][0].update(size=len(data), sha256=descriptor['archive_sha256'])
        worker.nvidia_descriptor = _canonical(descriptor)
        full = self.root / 'TEMPO-1.0.2-gpu-full.nupkg'
        shutil.copyfile(self.packages / self.release.full.name, full)
        with zipfile.ZipFile(full, 'a') as archive:
            archive.writestr('lib/app/_internal/assets/nvidia-runtime.json', worker.nvidia_descriptor)
        cert = json.loads(self.artifacts['certification'].read_bytes())
        cert['full_package_sha256'] = _digest(full)
        self.artifacts['certification'].write_bytes(_canonical(cert))
        descriptor_file = self.put('gpu-descriptor.json', worker.nvidia_descriptor)
        output = self.root / 'gpu-release'
        self.release = package(full_package=full, version='1.0.2', commit=self.commit, channel='stable', sequence=1,
            expires_at='2027-01-01T00:00:00Z', signing_key=self.private, key_id='release', output=output,
            pin_path=self.pin, certification=self.artifacts['certification'], native_updater=self.artifacts['updater'],
            native_patch=self.artifacts['patch'], now=self.now, minimum_updater_version='1.2.161',
            storage_schema_version=1, nvidia_descriptor=descriptor_file)
        self.options.update(manifest=output / 'tempo-update-v1.0.2.json', index=output / 'tempo-update-index.json', package_dir=output)
        (output / 'nvidia-runtime.json').write_bytes(worker.nvidia_descriptor + b'\n')
        acceptance = json.loads(self.artifacts['acceptance'].read_bytes())
        acceptance['artifact_sha256']['full_package'] = self.release.full.sha256
        self.artifacts['acceptance'].write_bytes(_canonical(acceptance))
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.prepare()
        for name in ('legacy_nvidia_migration', 'matching_worker_inference'):
            acceptance['checks'][name] = True
            report = self.put('report-' + name + '.json', _canonical({'fixture': name}))
            self.artifacts['report_' + name] = report
            acceptance['evidence'][name] = {'sha256': _digest(report)}
        self.artifacts['acceptance'].write_bytes(_canonical(acceptance))
        with self.assertRaises(FileNotFoundError):
            self.prepare()
        self.assertFalse(self.options['output'].exists())
        part = output / descriptor['parts'][0]['url'].rsplit('/', 1)[-1]
        part.write_bytes(data)
        result = self.prepare()
        value = retention.verify(self.options['output'], self.keys, expected_sha256=result['catalog_sha256'])
        record = next(item for item in value['files'] if item['role'] == 'nvidia_part')
        self.assertEqual((self.options['output'] / record['path']).read_bytes(), data)

    def test_missing_dependency_source_bytes_are_not_replaced_with_a_url_only_index(self):
        source = next(self.cache.iterdir())
        source.unlink()
        with self.assertRaises(FileNotFoundError):
            self.prepare()
        self.assertFalse(self.options['output'].exists())

    def test_previous_index_cannot_authorize_a_reused_version_or_sequence(self):
        with self.assertRaisesRegex(ValueError, 'must advance'):
            self.prepare(previous_index=self.options['index'])
        self.assertFalse(self.options['output'].exists())

    def test_untrusted_new_signer_and_private_key_artifact_alias_are_rejected(self):
        other = Ed25519PrivateKey.generate()
        private = self.put('other-private.pem', other.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        with self.assertRaisesRegex(ValueError, 'independently supplied public trust map'):
            self.prepare(signing_key=private)
        alias = self.root / 'report-private-alias.bin'
        shutil.copyfile(self.private, alias)
        self.artifacts['report_hardware_shutdown'] = alias
        with self.assertRaisesRegex(ValueError, 'Private signing material'):
            self.prepare()
        self.assertFalse(self.options['output'].exists())


if __name__ == '__main__':
    unittest.main()
