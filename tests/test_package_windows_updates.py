import base64
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from scripts.package_windows_updates import inventory_package, package
from tempo_update_manifest import inventory_digest, load_verified_manifest


class PackageWindowsUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.full = self.root/'full.nupkg'
        with zipfile.ZipFile(self.full, 'w') as archive:
            archive.writestr('TEMPO.nuspec', b'metadata')
            archive.writestr('lib/app/TEMPO.exe', b'app')
            archive.writestr('lib/app/Squirrel.exe', b'updater')
        self.key = Ed25519PrivateKey.generate()
        self.key_path = self.root/'private.pem'
        self.key_path.write_bytes(self.key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.updater = self.root/'updater.exe'
        self.updater.write_bytes(b'updater')
        self.patch = self.root/'native.patch'
        self.patch.write_bytes(b'patch')
        updater_hash = hashlib.sha256(self.updater.read_bytes()).hexdigest()
        patch_hash = hashlib.sha256(self.patch.read_bytes()).hexdigest()
        self.pin = self.root/'pin.json'
        self.pin.write_text(json.dumps({'schema_version':1, 'certification_status':'certified',
            'upstream_commit':'c'*40, 'updater_sha256':updater_hash, 'patch_sha256':patch_hash,
            'required_checks':['lifecycle', 'recovery']}))
        self.cert = self.root/'evidence.json'
        self.cert.write_text(json.dumps({'schema_version':1, 'source_commit':'a'*40,
            'upstream_commit':'c'*40, 'updater_sha256':updater_hash, 'patch_sha256':patch_hash,
            'full_package_sha256':hashlib.sha256(self.full.read_bytes()).hexdigest(),
            'checks':{'lifecycle':True, 'recovery':True}}))
        self.args = dict(full_package=self.full, version='1.0.2', commit='a'*40, channel='stable',
            sequence=1, expires_at='2027-01-01T00:00:00Z', signing_key=self.key_path, key_id='key',
            output=self.root/'output', certification=self.cert, pin_path=self.pin,
            now=datetime(2026,1,1,tzinfo=timezone.utc), native_updater=self.updater, native_patch=self.patch)

    def test_signs_exact_inventory_and_preserves_private_key(self):
        release = package(**self.args)
        root = self.args['output']
        index = json.loads((root/'tempo-update-index.json').read_bytes())
        self.assertEqual(len(index['releases']), 1)
        encoded = json.dumps(index['releases'][0]).encode()
        verified = load_verified_manifest(encoded, {'key':self.public}, now=self.args['now'],
                                          min_sequence=1, expected_channel='stable')
        self.assertEqual(release.inventory, verified.inventory)
        self.assertEqual({r.path for r in verified.inventory}, {'TEMPO.nuspec', 'lib/app/TEMPO.exe', 'lib/app/Squirrel.exe'})
        self.assertEqual((root/'full.nupkg').read_bytes(), self.full.read_bytes())
        self.assertNotIn(b'PRIVATE KEY', b''.join(path.read_bytes() for path in root.iterdir()))
        package(**self.args)  # Identical results are safe to regenerate.
        (root/'full.nupkg').write_bytes(b'other')
        with self.assertRaisesRegex(ValueError, 'different artifact'):
            package(**self.args)
        self.assertEqual((root/'full.nupkg').read_bytes(), b'other')

    def test_release_notes_are_signed(self):
        notes = self.root/'notes.txt'
        notes.write_text('Release details', encoding='utf-8')
        release = package(**dict(self.args, release_notes=notes))
        self.assertEqual(release.release_notes, 'Release details')
        notes.write_text('x' * 65537, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '64 KiB'):
            package(**dict(self.args, release_notes=notes, output=self.root/'bad'))
        self.assertFalse((self.root/'bad').exists())

    def test_failed_certification_produces_no_release(self):
        evidence = json.loads(self.cert.read_bytes())
        evidence['checks']['recovery'] = False
        self.cert.write_text(json.dumps(evidence))
        with self.assertRaisesRegex(ValueError, 'checks'):
            package(**self.args)
        self.assertFalse(self.args['output'].exists())

    def test_unverified_pin_is_rejected(self):
        pin = json.loads(self.pin.read_bytes())
        pin['certification_status'] = 'unverified'
        self.pin.write_text(json.dumps(pin))
        with self.assertRaisesRegex(ValueError, 'certified'):
            package(**self.args)
        self.assertFalse(self.args['output'].exists())

    def test_zip_rejects_unsafe_or_colliding_paths(self):
        for names in [('CON.txt',), ('../escape',), ('lib/A', 'lib/a')]:
            with self.subTest(names=names):
                archive = self.root/'unsafe.zip'
                with zipfile.ZipFile(archive, 'w') as z:
                    for name in names:
                        z.writestr(name, b'x')
                with self.assertRaises(ValueError):
                    inventory_package(archive)

    def test_patch_publication_is_blocked_without_reconstruction(self):
        prev_output = self.root/'prev_output'
        prev_args = dict(self.args, version='1.0.1', sequence=1, output=prev_output)
        prev_release = package(**prev_args)
        prev_index = prev_output/'tempo-update-index.json'

        trusted_keys_file = self.root/'trusted.json'
        trusted_keys_file.write_text(json.dumps({'key': base64.b64encode(self.public).decode('ascii')}))

        delta = self.root/'delta.nupkg'
        delta.write_bytes(b'delta')
        spec = self.root/'patches.json'
        spec.write_text(json.dumps([{
            'base_version': '1.0.1',
            'base_commit': 'a'*40,
            'base_inventory_sha256': inventory_digest(prev_release.inventory),
            'file': str(delta),
        }]))
        with self.assertRaisesRegex(ValueError, 'reconstruction'):
            package(**dict(self.args, sequence=2, patch_spec=spec, previous_index=prev_index,
                           base_package_dir=prev_output, trusted_keys=trusted_keys_file,
                           native_updater=self.updater))
        self.assertFalse(self.args['output'].exists())

    def test_packaged_updater_must_match_certified_bytes(self):
        pin = json.loads(self.pin.read_bytes())
        pin['updater_sha256'] = 'f'*64
        self.pin.write_text(json.dumps(pin))
        evidence = json.loads(self.cert.read_bytes())
        evidence['updater_sha256'] = 'f'*64
        self.cert.write_text(json.dumps(evidence))
        with self.assertRaises(ValueError):
            package(**dict(self.args, native_updater=None))
        self.assertFalse(self.args['output'].exists())


if __name__ == '__main__':
    unittest.main()
