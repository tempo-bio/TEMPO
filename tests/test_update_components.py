"""Offline recovery restores only verified, retained NVIDIA worker selections."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import zipfile

from tempo_update_components import restore_previous_runtime


def fixture(version, commit, payload):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as out:
        out.writestr('TEMPO/TEMPO.exe', payload)
        out.writestr('TEMPO/_internal/library.dll', b'library-' + payload)
    data = stream.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    url = f'https://github.com/tempo-bio/TEMPO/releases/download/v{version}/TEMPO_NVIDIA_Runtime_v{version}.zip.part001'
    desc = dict(schema_version=2, profile='nvidia-cu126', app_version=version, commit=commit,
                archive_size=len(data), archive_sha256=digest, executable='TEMPO/TEMPO.exe',
                executable_sha256=hashlib.sha256(payload).hexdigest(),
                parts=[dict(url=url, size=len(data), sha256=digest)])
    marker = dict(schema_version=1, version=version, commit=commit,
                  bundle=f'{commit}-{digest[:12]}', executable=desc['executable'],
                  executable_sha256=desc['executable_sha256'], archive_sha256=digest)
    release = SimpleNamespace(version=version, commit=commit, nvidia_descriptor=json.dumps(desc).encode())
    return release, marker, data


class ComponentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base, self.previous, archive = fixture('1.0.1', 'a'*40, b'base')
        self.target, self.selected, _ = fixture('1.0.2', 'b'*40, b'target')
        (self.root / (self.previous['bundle'] + '.zip')).write_bytes(archive)
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            source.extractall(self.root / self.previous['bundle'])
        self.marker_path = self.root / 'active.json'
        self.set_marker(self.selected)

    def set_marker(self, value):
        self.marker_path.write_text(json.dumps(value))

    def restore(self):
        return restore_previous_runtime({'nvidia_marker': self.previous}, self.base, self.target, runtime_root=self.root)

    def test_restores_previous_and_is_idempotent(self):
        self.assertTrue(self.restore())
        self.assertEqual(json.loads(self.marker_path.read_text()), self.previous)
        self.assertTrue(self.restore())
        self.assertEqual(json.loads(self.marker_path.read_text()), self.previous)

    def test_unrelated_marker_is_untouched(self):
        unrelated = dict(self.selected, commit='c'*40)
        self.set_marker(unrelated)
        before = self.marker_path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'selection changed'):
            self.restore()
        self.assertEqual(self.marker_path.read_bytes(), before)

    def test_corrupt_archive_or_inventory_never_activates(self):
        archive = self.root / (self.previous['bundle'] + '.zip')
        original = archive.read_bytes()
        archive.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        before = self.marker_path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.restore()
        self.assertEqual(self.marker_path.read_bytes(), before)
        archive.write_bytes(original)
        (self.root / self.previous['bundle'] / 'TEMPO/_internal/library.dll').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'inventory differs'):
            self.restore()
        self.assertEqual(self.marker_path.read_bytes(), before)

    def test_extra_or_linked_runtime_file_blocks_restore(self):
        root = self.root / self.previous['bundle'] / 'TEMPO'
        extra = root / 'unexpected.dll'
        extra.write_bytes(b'extra')
        before = self.marker_path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'inventory differs'):
            self.restore()
        self.assertEqual(self.marker_path.read_bytes(), before)
        extra.unlink()
        library = root / '_internal/library.dll'
        library.unlink()
        external = self.root / 'outside.dll'
        external.write_bytes(b'library-base')
        try:
            library.symlink_to(external)
        except (OSError, NotImplementedError):
            self.skipTest('Symbolic links are unavailable')
        with self.assertRaisesRegex(ValueError, 'Link or reparse point'):
            self.restore()
        self.assertEqual(self.marker_path.read_bytes(), before)

    def test_forged_previous_and_legacy_descriptor_rejected(self):
        before = self.marker_path.read_bytes()
        self.previous['bundle'] = '../elsewhere'
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.restore()
        self.assertEqual(self.marker_path.read_bytes(), before)
        self.previous['bundle'] = 'a'*40 + '-' + self.previous['archive_sha256'][:12]
        descriptor = json.loads(self.base.nvidia_descriptor)
        descriptor['schema_version'] = 1
        self.base.nvidia_descriptor = json.dumps(descriptor).encode()
        with self.assertRaisesRegex(ValueError, 'repair forward'):
            self.restore()
        self.assertEqual(self.marker_path.read_bytes(), before)

    def test_cpu_marker_must_remain_absent(self):
        record = {'nvidia_marker': None}
        with self.assertRaisesRegex(ValueError, 'selection changed'):
            restore_previous_runtime(record, self.base, self.target, runtime_root=self.root)
        self.marker_path.unlink()
        self.assertFalse(restore_previous_runtime(record, self.base, self.target, runtime_root=self.root))
        self.assertFalse(self.marker_path.exists())


if __name__ == '__main__':
    unittest.main()
