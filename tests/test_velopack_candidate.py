"""Full-app candidate packaging must preserve exact tested bytes and disabled trust."""
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import shutil
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.build_velopack_candidate import (ROOT, VERSIONS, check_payload,
    file_record, folder_inventory, signed_fixture)
from scripts import build_velopack_candidate as candidate
from scripts import build_velopack_release as release_builder
from scripts.package_windows_updates import _digest
from tempo_release import REPORT_SCHEMA, REQUIRED_CHECKS, VERSION
from tempo_update_manifest import load_verified_manifest
from cryptography.hazmat.primitives import serialization


class VelopackCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        self.source = {
            'TEMPO.exe': b'frozen application',
            '_internal/assets/build-provenance.json': json.dumps({'commit': 'a'*40, 'profile': 'cpu'}).encode(),
            '_internal/assets/tempo-updates.json': (ROOT / 'installer/tempo-updates.json').read_bytes(),
            '_internal/third-party-licenses/index.json': b'notice inventory',
            '_internal/third-party-sources/dependency-sources.json': b'source inventory',
            '_internal/docs/GETTING_STARTED.md': b'user instructions',
        }
        for name, data in self.source.items():
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.inventory = folder_inventory(self.bundle)
        updater = self.root / 'Update.exe'
        updater.write_bytes(b'native updater')
        self.updater_hash = _digest(updater)
        self.package = self.root / 'TEMPO-test-full.nupkg'
        self.write_package()

    def write_package(self, changes=None, omit=()):
        values = {'lib/app/' + name: data for name, data in self.source.items()}
        values['lib/app/Squirrel.exe'] = b'native updater'
        values['lib/app/sq.version'] = b'version metadata'
        values.update(changes or {})
        with zipfile.ZipFile(self.package, 'w') as archive:
            for name, data in values.items():
                if name not in omit:
                    archive.writestr(name, data)

    def check(self, **kwargs):
        return check_payload(self.package, self.inventory, self.updater_hash, 'a'*40, **kwargs)

    def test_complete_payload_and_repair_manifests_bind_every_byte(self):
        inventory = self.check()
        key = Ed25519PrivateKey.generate()
        release = signed_fixture(self.package, inventory, VERSIONS[0], 'a'*40, 1, key)
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        verified = load_verified_manifest(release.envelope, {'acceptance-only': public},
            now=datetime.now(timezone.utc), min_sequence=0, expected_channel='prerelease')
        self.assertEqual(verified.full.sha256, _digest(self.package))
        self.assertEqual(verified.commit, 'a'*40)
        self.assertEqual(verified.inventory, release.inventory)
        other = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        with self.assertRaisesRegex(ValueError, 'trusted signature'):
            load_verified_manifest(release.envelope, {'acceptance-only': other},
                now=datetime.now(timezone.utc), min_sequence=0, expected_channel='prerelease')

    def test_notices_sources_docs_and_app_cannot_disappear_or_change(self):
        for name in self.source:
            with self.subTest(name=name):
                self.write_package(omit=('lib/app/' + name,))
                with self.assertRaisesRegex(ValueError, 'differs from tested folder'):
                    self.check()
                self.write_package({'lib/app/' + name: b'changed'})
                with self.assertRaisesRegex(ValueError, 'differs from tested folder'):
                    self.check()

    def test_native_updater_and_provenance_must_match(self):
        self.write_package({'lib/app/Squirrel.exe': b'other updater'})
        with self.assertRaisesRegex(ValueError, 'exact production-feature updater'):
            self.check()
        for identity in ({'commit': 'b'*40, 'profile': 'cpu'}, {'commit': 'a'*40, 'profile': 'nvidia-cu126'}):
            name = '_internal/assets/build-provenance.json'
            data = json.dumps(identity).encode()
            (self.bundle / name).write_bytes(data)
            self.inventory = folder_inventory(self.bundle)
            self.write_package({'lib/app/' + name: data})
            with self.assertRaisesRegex(ValueError, 'tested CPU application'):
                self.check()

    def test_enabled_or_new_trust_configuration_cannot_enter_acceptance_packages(self):
        name = '_internal/assets/tempo-updates.json'
        disabled = json.loads(self.source[name])
        for change in ({'enabled': True}, {'trusted_keys': {'unexpected': 'key'}}):
            data = json.dumps(dict(disabled, **change)).encode()
            (self.bundle / name).write_bytes(data)
            self.inventory = folder_inventory(self.bundle)
            self.write_package({'lib/app/' + name: data})
            with self.assertRaisesRegex(ValueError, 'disabled, empty-trust'):
                self.check()

    def test_source_links_are_rejected(self):
        (self.bundle / 'linked-file').symlink_to(self.bundle / 'TEMPO.exe')
        with self.assertRaisesRegex(ValueError, 'link'):
            folder_inventory(self.bundle)
        with self.assertRaisesRegex(ValueError, 'ordinary artifact'):
            file_record(self.bundle / 'linked-file')

    def test_release_assembly_keeps_the_tested_bundle_and_enables_reviewed_stable_trust(self):
        project, options = self.prepare_builder()
        commit, archive, setup, updater = (options[name] for name in ('commit', 'vpk_archive', 'setup', 'updater'))
        identity = dict(version=VERSION, commit=commit, profile='cpu', lock_sha256='a' * 64)
        (self.bundle / '_internal/assets/build-provenance.json').write_text(json.dumps(identity))
        verification = self.root / 'verification.json'
        expected = dict(version=VERSION, commit=commit, schema_version=REPORT_SCHEMA,
                        lock_sha256=identity['lock_sha256'], required_checks=list(REQUIRED_CHECKS))
        verification.write_text(json.dumps(expected))
        report = self.root / 'runtime.json'
        report.write_text(json.dumps(dict(expected, success=True, frozen=True, checks=list(REQUIRED_CHECKS),
                                          runtime_details=dict.fromkeys(REQUIRED_CHECKS, {}))))
        build_contract = self.root / 'build-contract.json'
        build_contract.write_text(json.dumps(dict(purpose='isolated-full-app-acceptance', source_commit=commit,
            packager_sha256=_digest(archive), updater_sha256=_digest(updater), setup_sha256=_digest(setup),
            bundle_inventory=folder_inventory(self.bundle))))
        keys = self.root / 'keys.json'
        keys.write_text(json.dumps({'stable': base64.b64encode(b'x' * 32).decode()}))
        repair = self.root / 'TEMPORepair.exe'
        repair.write_bytes(b'reviewed repair')
        pin_path = project / 'installer/velopack/pin.json'
        pin = dict(json.loads(pin_path.read_bytes()), certification_status='certified', updater_sha256=_digest(updater))
        pin_path.write_text(json.dumps(pin))
        evidence = self.root / 'evidence.json'
        evidence.write_text(json.dumps(dict(schema_version=1, source_commit=commit,
            upstream_commit=pin['upstream_commit'], patch_sha256=pin['patch_sha256'], updater_sha256=_digest(updater),
            repair_sha256=_digest(repair), public_keys_sha256=_digest(keys),
            checks=dict.fromkeys(pin['required_checks'], True))))
        output = self.root / 'release'

        def pack(command, log, timeout):
            self.assertEqual(command[command.index('--packVersion') + 1], VERSION)
            folder = Path(command[command.index('--packDir') + 1])
            packages = Path(command[command.index('--outputDir') + 1])
            packages.mkdir()
            with zipfile.ZipFile(packages / f'TEMPO-{VERSION}-full.nupkg', 'w') as package:
                for row in folder_inventory(folder):
                    package.write(folder / row['path'], 'lib/app/' + row['path'])
                package.writestr('lib/app/Squirrel.exe', updater.read_bytes())
            (packages / 'TEMPO-Setup.exe').write_bytes(b'setup bytes')
            return 0

        with patch.object(release_builder.sys, 'platform', 'win32'), \
             patch.object(release_builder, 'VPK_SHA256', _digest(archive)), \
             patch.object(release_builder, 'run_logged', side_effect=pack):
            assembled = release_builder.build(self.bundle, build_contract, verification, report, archive, setup,
                output, public_keys=keys, native_updater=updater, repair_executable=repair,
                certification=evidence, pin_path=pin_path, patch_path=pin_path.with_name('0001-tempo-recovery.patch'))
        revision = assembled['revisions'][0]
        self.assertEqual(revision['installer']['path'], f'TEMPO_Setup_v{VERSION}.exe')
        self.assertFalse(assembled['release_certified'])
        with zipfile.ZipFile(output / revision['full']['path']) as package:
            config = json.loads(package.read('lib/app/_internal/assets/tempo-updates.json'))
            self.assertTrue(config['enabled'])
            self.assertEqual(config['channel'], 'stable')
            self.assertEqual(config['trusted_keys'], json.loads(keys.read_bytes()))
        self.assertEqual((self.bundle / '_internal/assets/tempo-updates.json').read_bytes(),
                         self.source['_internal/assets/tempo-updates.json'])

    def prepare_builder(self):
        project = self.root / 'project'
        for name in ('installer/tempo-updates.json', 'installer/velopack/pin.json',
                     'installer/velopack/0001-tempo-recovery.patch',
                     'scripts/verify_velopack_install.ps1', 'scripts/windows_installer_helpers.ps1'):
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        (project / 'Output').mkdir()
        for name in ('verification-contract.json', 'build-provenance.json'):
            (project / 'Output' / name).write_text('{"commit":"' + 'a'*40 + '"}')
        archive = self.root / 'vpk.nupkg'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('vendor/update_x64.exe', b'vendor updater')
        setup = self.root / 'setup.exe'
        setup.write_bytes(b'reviewed setup helper')
        pin = json.loads((project / 'installer/velopack/pin.json').read_bytes())
        evidence = self.root / 'native-candidate.json'
        evidence.write_text(json.dumps(dict(source_commit='a'*40, updater_sha256=self.updater_hash,
            upstream_commit=pin['upstream_commit'], patch_sha256=pin['patch_sha256'], fixture_passed=True,
            production_passed=True, setup_sha256=_digest(setup))))
        options = dict(setup=setup, bundle=self.bundle, vpk_archive=archive, updater=self.root / 'Update.exe',
                       native_evidence=evidence, output=self.root / 'candidate', commit='a'*40)
        return project, options

    def test_builder_binds_all_revisions_and_checks_deltas_before_completion_marker(self):
        project, options = self.prepare_builder()
        commands = []
        def companion(keys, updater, dist, work):
            self.assertEqual(set(json.loads(keys.read_bytes())), {'acceptance-only'})
            path = Path(dist) / 'TEMPORepair.exe'
            path.write_bytes(b'independent companion')
            return path
        def run(command, log, timeout, **kwargs):
            commands.append(command)
            if command[1].startswith('--veloapp-'):
                self.assertEqual(command[0], str(self.bundle.resolve() / 'TEMPO.exe'))
                self.assertEqual(kwargs['env']['QT_QPA_PLATFORM'], 'offscreen')
            elif 'pack' in command:
                self.assertNotIn('--skipVeloAppCheck', command)
                self.assertIn('--noDefaultExclude', command)
                version = command[command.index('--packVersion') + 1]
                releases = Path(command[command.index('--outputDir') + 1])
                releases.mkdir(exist_ok=True)
                self.write_package({'lib/app/sq.version': version.encode()})
                shutil.copyfile(self.package, releases / f'TEMPO-{version}-full.nupkg')
                (releases / 'TEMPO-Setup.exe').write_bytes(version.encode())
                if version != VERSIONS[0]:
                    (releases / f'TEMPO-{version}-delta.nupkg').write_bytes(b'delta')
            else:
                self.assertEqual(command[1], 'patch')
                shutil.copyfile(self.package, command[command.index('--output') + 1])
            return 0
        with patch.object(candidate.sys, 'platform', 'win32'), patch.object(candidate, 'ROOT', project), \
             patch.object(candidate, 'VPK_SHA256', _digest(options['vpk_archive'])), \
             patch.object(candidate, 'build_repair', side_effect=companion), \
             patch.object(candidate, 'run_logged', side_effect=run), \
             patch.object(candidate, 'isolated_environment', return_value={'QT_QPA_PLATFORM': 'offscreen'}), \
             patch.dict(candidate.os.environ, {}, clear=True):
            contract = candidate.build(**options)
        self.assertTrue((options['output'] / 'candidate-contract.json').is_file())
        self.assertFalse(contract['release_certified'])
        self.assertTrue(contract['same_source_packaging_revisions'])
        self.assertEqual(len(commands), 9)
        self.assertEqual(len(contract['frozen_lifecycle_probes']), 4)
        for index, row in enumerate(contract['revisions']):
            self.assertEqual(row['version'], VERSIONS[index])
            self.assertEqual(row['full']['sha256'], _digest(options['output'] / row['full']['path']))
            self.assertEqual(row['installer']['sha256'], _digest(options['output'] / row['installer']['path']))
            if index:
                self.assertTrue(row['delta_reconstructed_inventory_match'])
        self.assertFalse(any(p.suffix in ('.pem', '.key') for p in options['output'].iterdir()))

    def test_native_mismatch_or_lifecycle_bypass_environment_fails_before_output(self):
        project, options = self.prepare_builder()
        for changes in ({'VPK_SKIP_VELO_APP_CHECK': '1'}, {'TEMPO_UPDATE_CONFIG': 'override.json'}):
            with self.subTest(env=changes), patch.object(candidate.sys, 'platform', 'win32'), \
                 patch.dict(candidate.os.environ, changes, clear=True):
                with self.assertRaisesRegex(ValueError, 'environment variables'):
                    candidate.build(**options)
                self.assertFalse(options['output'].exists())
        with patch.object(candidate.sys, 'platform', 'win32'), patch.object(candidate, 'ROOT', project), \
             patch.object(candidate, 'VPK_SHA256', _digest(options['vpk_archive'])), \
             patch.dict(candidate.os.environ, {}, clear=True):
            (self.root / 'Update.exe').write_bytes(b'changed native bytes')
            with self.assertRaisesRegex(ValueError, 'identity/evidence'):
                candidate.build(**options)
        self.assertFalse(options['output'].exists())

    def test_failed_frozen_hook_cannot_produce_a_completed_candidate(self):
        project, options = self.prepare_builder()
        with patch.object(candidate.sys, 'platform', 'win32'), patch.object(candidate, 'ROOT', project), \
             patch.object(candidate, 'VPK_SHA256', _digest(options['vpk_archive'])), \
             patch.object(candidate, 'isolated_environment', return_value={}), \
             patch.object(candidate, 'run_logged', return_value=1), \
             patch.object(candidate, 'build_repair') as companion, \
             patch.dict(candidate.os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'frozen lifecycle entrypoint'):
                candidate.build(**options)
        companion.assert_not_called()
        self.assertFalse((options['output'] / 'candidate-contract.json').exists())

    def test_builder_rejects_unexercised_or_changed_setup(self):
        project, options = self.prepare_builder()
        evidence_path = options['native_evidence']
        original = json.loads(evidence_path.read_bytes())
        for field, value in (('production_passed', False), ('setup_sha256', '0'*64)):
            evidence_path.write_text(json.dumps(dict(original, **{field: value})))
            with self.subTest(field=field), patch.object(candidate.sys, 'platform', 'win32'), \
                 patch.object(candidate, 'ROOT', project), \
                 patch.object(candidate, 'VPK_SHA256', _digest(options['vpk_archive'])), \
                 patch.dict(candidate.os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, 'identity/evidence'):
                    candidate.build(**options)
                self.assertFalse(options['output'].exists())


if __name__ == '__main__':
    unittest.main()
