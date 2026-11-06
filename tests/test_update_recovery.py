import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import tempo_update_manifest as manifest
import tempo_update_recovery as recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.install = self.root / 'app'
        self.current = self.install / 'current'
        self.current.mkdir(parents=True)
        (self.current / 'TEMPO.exe').write_bytes(b'base')
        self.cache = self.root / 'profile' / 'recovery'
        self.packages = self.root / 'profile' / 'packages'
        self.key = Ed25519PrivateKey.generate()
        self.keys = {'test': self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)}
        self.base, self.base_zip = self.release('1.0.1', b'base', 'a')
        self.target, self.target_zip = self.release('1.0.2', b'target', 'b')

    def release(self, version, body, commit):
        archive = self.root / (version + '.zip')
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('lib/app/TEMPO.exe', body)
            z.writestr('metadata.xml', b'meta')
        inv = [{'path': p, 'size': len(b), 'sha256': hashlib.sha256(b).hexdigest()}
               for p, b in [('lib/app/TEMPO.exe', body), ('metadata.xml', b'meta')]]
        content = archive.read_bytes()
        obj = {'schema_version': 1, 'package_id': manifest.PACKAGE_ID, 'version': version,
               'commit': commit * 40, 'architecture': 'win-x64', 'channel': 'stable',
               'sequence': 1, 'expires_at': '2099-01-01T00:00:00Z',
               'full': {'name': 'full.zip', 'url': f'https://github.com/tempo-bio/TEMPO/releases/download/v{version}/full.zip',
                        'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()},
               'inventory': inv, 'patches': []}
        signed = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        envelope = json.dumps({'signed': obj, 'signatures': [{'key_id': 'test', 'signature': base64.b64encode(self.key.sign(signed)).decode()}]}).encode()
        release = manifest.load_verified_manifest(envelope, self.keys, now=datetime.now(timezone.utc), min_sequence=0, expected_channel='stable')
        return release, archive

    def arm(self):
        return recovery.arm_recovery(self.install, self.cache, self.base, self.base_zip, self.target, self.target_zip,
                                     trusted_keys=self.keys, expected_root=self.install, packages_dir=self.packages)

    def recover(self, path, mode='forward'):
        return recovery.recover(path, self.keys, mode=mode, expected_root=self.install, packages_dir=self.packages)

    def test_forward_preserves_old_current_and_is_repeatable(self):
        path = self.arm()
        self.assertEqual(self.recover(path), 'recovered_forward')
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'target')
        self.assertEqual((self.install / 'tempo-repair-quarantine' / 'TEMPO.exe').read_bytes(), b'base')
        self.assertEqual(self.recover(path), 'recovered_forward')

    def test_missing_current_and_interrupted_swap(self):
        path = self.arm()
        self.current.rename(self.install / 'tempo-repair-quarantine')
        self.assertEqual(self.recover(path), 'recovered_forward')
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'target')
        self.assertEqual((self.install / 'tempo-repair-quarantine' / 'TEMPO.exe').read_bytes(), b'base')

    def test_corruption_or_wrong_trust_does_not_replace_application(self):
        path = self.arm()
        (self.cache / 'target.nupkg').write_bytes(b'broken')
        with self.assertRaises(ValueError):
            self.recover(path)
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'base')
        with self.assertRaises(ValueError):
            recovery.recover(path, {'other': b'x'*32}, mode='forward', expected_root=self.install, packages_dir=self.packages)
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'base')

    def test_rename_failure_restores_current(self):
        path = self.arm()
        original = recovery.os.rename
        def rename(src, dst):
            if Path(src).name == 'tempo-repair-stage':
                raise OSError('injected rename failure')
            return original(src, dst)
        with patch.object(recovery.os, 'rename', side_effect=rename), self.assertRaises(OSError):
            self.recover(path)
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'base')
        self.assertEqual(self.recover(path), 'recovered_forward')

    def test_confirm_verifies_target_and_preserves_incompatible_previous(self):
        path = self.arm()
        previous = self.install / 'tempo-previous'
        self.current.rename(previous)
        self.current.mkdir()
        (self.current / 'TEMPO.exe').write_bytes(b'target')
        (self.install / 'tempo-update-pending.txt').write_text('TEMPO update pending\nprevious=1.0.1\ntarget=1.0.2\n')
        with self.assertRaises(ValueError):
            recovery.confirm(path, self.keys, running_version='1.0.1', running_commit='a'*40,
                             expected_root=self.install, packages_dir=self.packages)
        self.assertTrue(previous.exists())
        result = recovery.confirm(path, self.keys, running_version='1.0.2', running_commit='b'*40,
                                  expected_root=self.install, packages_dir=self.packages)
        self.assertEqual(result, 'confirmed')
        self.assertFalse(previous.exists())
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'target')

    def test_unexpected_root_rejected(self):
        path = self.arm()
        with self.assertRaises(ValueError):
            recovery.recover(path, self.keys, mode='forward', expected_root=self.root / 'other', packages_dir=self.packages)


if __name__ == '__main__':
    unittest.main()
