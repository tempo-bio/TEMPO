import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.configure_windows_updates import candidate, stage
from scripts.package_windows_updates import REQUIRED_CHECKS


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class UpdateConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.keys = self.root / 'keys.json'
        self.keys.write_text(json.dumps({'release': base64.b64encode(bytes(range(32))).decode()}))
        self.updater = self.root / 'Update.exe'
        self.updater.write_bytes(b'updater')
        self.repair = self.root / 'TEMPORepair.exe'
        self.repair.write_bytes(b'repair')
        self.patch = self.root / 'patch'
        self.patch.write_bytes(b'patch')
        self.pin = self.root / 'pin.json'
        self.pin_data = {'schema_version': 1, 'certification_status': 'certified',
                         'upstream_commit': 'a'*40, 'updater_sha256': digest(self.updater),
                         'patch_sha256': digest(self.patch), 'required_checks': sorted(REQUIRED_CHECKS)}
        self.pin.write_text(json.dumps(self.pin_data))
        self.cert = self.root / 'cert.json'
        self.cert_data = {'schema_version': 1, 'source_commit': 'b'*40,
                          'upstream_commit': 'a'*40, 'updater_sha256': digest(self.updater),
                          'patch_sha256': digest(self.patch), 'repair_sha256': digest(self.repair),
                          'public_keys_sha256': digest(self.keys),
                          'checks': {name: True for name in REQUIRED_CHECKS}}
        self.cert.write_text(json.dumps(self.cert_data))
        self.options = dict(public_keys=self.keys, native_updater=self.updater,
                            repair_executable=self.repair, certification=self.cert,
                            source_commit='b'*40, pin_path=self.pin, patch_path=self.patch)

    def test_candidate_binds_validated_public_configuration(self):
        result = candidate(**self.options)
        self.assertTrue(result['enabled'])
        self.assertEqual(result['repair_sha256'], digest(self.repair))
        self.assertEqual(result['trusted_keys'], json.loads(self.keys.read_text()))

    def test_uncertified_or_missing_required_check_rejected(self):
        self.pin_data['certification_status'] = 'unverified'
        self.pin.write_text(json.dumps(self.pin_data))
        with self.assertRaises(ValueError):
            candidate(**self.options)
        self.pin_data['certification_status'] = 'certified'
        self.pin.write_text(json.dumps(self.pin_data))
        self.cert_data['checks'].pop(next(iter(REQUIRED_CHECKS)))
        self.cert.write_text(json.dumps(self.cert_data))
        with self.assertRaises(ValueError):
            candidate(**self.options)

    def test_mismatched_bytes_or_source_rejected(self):
        for path in (self.keys, self.updater, self.repair, self.patch):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b'x')
                with self.assertRaises(ValueError):
                    candidate(**self.options)
                path.write_bytes(original)
        with self.assertRaises(ValueError):
            candidate(**{**self.options, 'source_commit': 'c'*40})

    def test_stage_validates_before_enabling_and_copies_repair(self):
        bundle = self.root / 'TEMPO'
        assets = bundle / '_internal' / 'assets'
        assets.mkdir(parents=True)
        (bundle / 'TEMPO.exe').write_bytes(b'app')
        destination = assets / 'tempo-updates.json'
        destination.write_text('{"enabled": false}')
        config = self.root / 'tempo-updates.json'
        config.write_text(json.dumps(candidate(**self.options)))
        self.repair.write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            stage(bundle, config, **self.options)
        self.assertFalse(json.loads(destination.read_text())['enabled'])
        self.assertFalse((bundle / 'TEMPORepair.exe').exists())
        self.repair.write_bytes(b'repair')
        stage(bundle, config, **self.options)
        self.assertTrue(json.loads(destination.read_text())['enabled'])
        self.assertEqual((bundle / 'TEMPORepair.exe').read_bytes(), b'repair')
        value = json.loads(config.read_text())
        value['trusted_keys'] = {'wrong': base64.b64encode(bytes(32)).decode()}
        config.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            stage(bundle, config, **self.options)


if __name__ == '__main__':
    unittest.main()
