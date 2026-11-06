import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import tempo_update_manifest as m

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
KEY = Ed25519PrivateKey.generate()
PUBLIC = KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def asset(version, name, data=b'x', size=None):
    return {'name': name, 'url': f'https://github.com/tempo-bio/TEMPO/releases/download/v{version}/{name}',
            'size': len(data) if size is None else size, 'sha256': sha(data)}


def payload(version='1.0.2', commit='a'*40):
    return {'schema_version': 1, 'package_id': m.PACKAGE_ID, 'version': version, 'commit': commit,
            'architecture': 'win-x64', 'channel': 'stable', 'sequence': 1,
            'expires_at': '2027-01-01T00:00:00Z', 'full': asset(version, 'full.zip', size=100),
            'inventory': [{'path': 'dir/TEMPO.exe', 'size': 3, 'sha256': sha(b'app')}], 'patches': [],
            'minimum_updater_version': '1.2.161', 'storage_schema_version': 1}


def signed(obj):
    encoded = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    envelope = {'signed': obj, 'signatures': [{'key_id': 'test', 'signature': base64.b64encode(KEY.sign(encoded)).decode()}]}
    return json.dumps(envelope).encode()


def load(obj, min_sequence=0, channel='stable'):
    return m.load_verified_manifest(signed(obj), {'test': PUBLIC}, now=NOW, min_sequence=min_sequence, expected_channel=channel)


class UpdateManifestTests(unittest.TestCase):
    def test_signature_freshness_and_identity(self):
        obj = payload()
        self.assertEqual(load(obj).commit, 'a'*40)
        envelope = json.loads(signed(obj))
        envelope['signed']['commit'] = 'b'*40
        with self.assertRaisesRegex(ValueError, 'signature'):
            m.load_verified_manifest(json.dumps(envelope).encode(), {'test': PUBLIC}, now=NOW, min_sequence=0, expected_channel='stable')
        with self.assertRaisesRegex(ValueError, 'Stale'):
            load(obj, min_sequence=2)
        with self.assertRaisesRegex(ValueError, 'channel'):
            load(obj, channel='prerelease')
        obj['expires_at'] = '2025-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'Expired'):
            load(obj)

    def test_compatibility_fields_must_be_signed(self):
        for field in ('minimum_updater_version', 'storage_schema_version'):
            obj = payload()
            del obj[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                load(obj)

    def test_unsafe_paths(self):
        for path in ['../x', 'C:/x', 'dir\\file', 'dir/CON.txt', 'file:stream', 'x.', '/abs', 'x//y', 'Lpt1/foo']:
            with self.subTest(path=path):
                obj = payload()
                obj['inventory'][0]['path'] = path
                with self.assertRaises(ValueError):
                    load(obj)

    def test_collisions_and_asset_origin(self):
        obj = payload()
        obj['inventory'].append(dict(obj['inventory'][0], path='DIR/tempo.EXE'))
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            load(obj)
        obj = payload()
        obj['full']['url'] = obj['full']['url'].replace('github.com', 'evil.example')
        with self.assertRaisesRegex(ValueError, 'URL'):
            load(obj)

    def test_inventory_directory_zip_and_tampering(self):
        release = load(payload())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'root'
            (root/'dir').mkdir(parents=True)
            (root/'dir'/'TEMPO.exe').write_bytes(b'app')
            m.verify_directory(root, release.inventory)
            archive = Path(temp)/'payload.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('dir/TEMPO.exe', b'app')
            m.verify_zip(archive, release.inventory)
            (root/'dir'/'TEMPO.exe').write_bytes(b'bad')
            with self.assertRaises(ValueError):
                m.verify_directory(root, release.inventory)
            (root/'dir'/'TEMPO.exe').unlink()
            try:
                os.symlink(temp, root/'dir'/'TEMPO.exe')
            except OSError:
                pass  # Creating symlinks can require Windows developer mode.
            else:
                with self.assertRaisesRegex(ValueError, 'Reparse'):
                    m.verify_directory(root, release.inventory)
            with zipfile.ZipFile(archive, 'a') as z:
                z.writestr('extra', b'x')
            with self.assertRaisesRegex(ValueError, 'Unexpected'):
                m.verify_zip(archive, release.inventory)

    def test_cached_asset_is_rechecked(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'asset'
            path.write_bytes(b'x')
            record = m.Asset('asset', 'unused', 1, sha(b'x'))
            m.verify_asset(path, record)
            path.write_bytes(b'y')
            with self.assertRaises(ValueError):
                m.verify_asset(path, record)

    def test_release_notes_and_updater_compatibility(self):
        obj = payload()
        self.assertEqual(load(obj).release_notes, '')
        obj['release_notes'] = 'Scientific improvement.'
        obj['minimum_updater_version'] = '1.2.161'
        self.assertEqual(load(obj).release_notes, 'Scientific improvement.')
        obj['minimum_updater_version'] = '1.2.162'
        with self.assertRaisesRegex(ValueError, 'newer updater'):
            load(obj)
        obj['minimum_updater_version'] = '1.2.161'
        obj['release_notes'] = 'x' * 65537
        with self.assertRaisesRegex(ValueError, 'release notes'):
            load(obj)

    def test_authenticated_nvidia_descriptor(self):
        obj = payload()
        version, commit = obj['version'], obj['commit']
        base = f'https://github.com/tempo-bio/TEMPO/releases/download/v{version}/TEMPO_NVIDIA_Runtime_v{version}.zip'
        descriptor = {'schema_version': 2, 'profile': 'nvidia-cu126', 'app_version': version,
                      'commit': commit, 'executable': 'TEMPO/TEMPO.exe', 'archive_size': 1,
                      'archive_sha256': sha(b'x'), 'executable_sha256': sha(b'x'),
                      'parts': [{'url': base+'.part001', 'size': 1, 'sha256': sha(b'x')}]}
        obj['nvidia_descriptor'] = descriptor
        self.assertEqual(json.loads(load(obj).nvidia_descriptor), descriptor)
        descriptor['commit'] = 'b'*40
        with self.assertRaises(ValueError):
            load(obj)

    def test_two_link_chain_requires_matching_intermediate_inventory(self):
        first = payload('1.0.2', 'a'*40)
        base = 'c'*64
        first['patches'] = [{'base_version':'1.0.1', 'base_commit':'b'*40, 'base_inventory_sha256':base,
                            'target_version':'1.0.2', 'target_commit':'a'*40,
                            'asset':asset('1.0.2', 'one.zip', size=20)}]
        first_release = load(first)
        second = payload('1.0.3', 'd'*40)
        second['patches'] = [{'base_version':'1.0.2', 'base_commit':'a'*40,
                              'base_inventory_sha256':m.inventory_digest(first_release.inventory),
                              'target_version':'1.0.3', 'target_commit':'d'*40,
                              'asset':asset('1.0.3', 'two.zip', size=20)}]
        args = dict(installed_version='1.0.1', installed_commit='b'*40,
                    installed_inventory_sha256=base, base_verified=True, channel='stable')
        self.assertEqual(len(m.plan_transfer([first_release, load(second)], **args).assets), 2)
        second['patches'][0]['base_inventory_sha256'] = 'e'*64
        self.assertEqual(m.plan_transfer([first_release, load(second)], **args).kind, 'full')

    def test_transfer_policy_and_identity(self):
        obj = payload()
        base_inventory = 'c'*64
        obj['patches'] = [{'base_version':'1.0.1', 'base_commit':'b'*40, 'base_inventory_sha256':base_inventory,
                           'target_version':'1.0.2', 'target_commit':'a'*40, 'asset':asset('1.0.2', 'patch.zip', size=20)}]
        release = load(obj)
        args = dict(installed_version='1.0.1', installed_commit='b'*40, installed_inventory_sha256=base_inventory, channel='stable')
        self.assertEqual(m.plan_transfer([release], base_verified=True, **args).kind, 'patch')
        self.assertEqual(m.plan_transfer([release], base_verified=False, **args).kind, 'full')
        self.assertEqual(m.plan_transfer([release], base_verified=True, **{**args, 'installed_commit':'d'*40}).kind, 'full')
        obj['patches'][0]['asset']['size'] = 75
        self.assertEqual(m.plan_transfer([load(obj)], base_verified=True, **args).kind, 'full')
        with self.assertRaisesRegex(ValueError, 'downgrade'):
            m.plan_transfer([release], base_verified=True, **{**args, 'installed_version':'1.0.3'})


if __name__ == '__main__':
    unittest.main()
