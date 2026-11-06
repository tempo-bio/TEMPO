"""Offline recovery restores only verified, retained NVIDIA worker selections."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import zipfile

from tempo_update_components import (restore_previous_runtime, read_runtime_marker,
    packaged_descriptor, verify_component_transition, activate_staged_runtime, verify_selected_runtime)
from tempo_update_manifest import FileRecord


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

    def target_package(self, *, embedded=None):
        data = embedded or self.target.nvidia_descriptor
        path = self.root / 'target.nupkg'
        with zipfile.ZipFile(path, 'w') as source:
            source.writestr('lib/app/_internal/assets/nvidia-runtime.json', data)
        self.target.inventory = (FileRecord('lib/app/_internal/assets/nvidia-runtime.json',
                                            len(data), hashlib.sha256(data).hexdigest()),)
        return path

    def staged(self):
        _, selected, archive = fixture('1.0.2', 'b'*40, b'target')
        (self.root / (selected['bundle'] + '.zip')).write_bytes(archive)
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            source.extractall(self.root / selected['bundle'])
        return dict(schema_version=1, descriptor=json.loads(self.target.nvidia_descriptor),
                    bundle=selected['bundle'], previous_marker=self.previous)

    def test_signed_descriptor_must_agree_with_the_descriptor_inside_target_package(self):
        expected = json.loads(self.target.nvidia_descriptor)
        self.assertEqual(packaged_descriptor(self.target, self.target_package()), expected)
        other = dict(expected, archive_sha256='c'*64)
        package = self.target_package(embedded=json.dumps(other).encode())
        with self.assertRaisesRegex(ValueError, 'differs from signed component metadata'):
            packaged_descriptor(self.target, package)

    def test_transition_rechecks_both_complete_runtimes_and_activates_idempotently(self):
        package, receipt = self.target_package(), self.staged()
        self.set_marker(self.previous)
        self.assertEqual(verify_component_transition(self.base, self.target, package, receipt,
            runtime_root=self.root, expected_marker=self.previous), self.previous)
        self.assertEqual(read_runtime_marker(self.root), self.previous)
        descriptor = json.loads(self.target.nvidia_descriptor)
        self.assertTrue(activate_staged_runtime(receipt, descriptor,
            runtime_root=self.root, previous_marker=self.previous))
        self.assertFalse(activate_staged_runtime(receipt, descriptor,
            runtime_root=self.root, previous_marker=self.previous))
        self.assertEqual(read_runtime_marker(self.root), self.selected)
        (self.root / self.selected['bundle'] / 'TEMPO/_internal/library.dll').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, 'inventory differs'):
            activate_staged_runtime(receipt, descriptor, runtime_root=self.root, previous_marker=self.previous)

    def test_transition_refuses_changed_selection_and_corrupt_retained_base(self):
        package, receipt = self.target_package(), self.staged()
        with self.assertRaisesRegex(ValueError, 'selection changed'):
            verify_component_transition(self.base, self.target, package, receipt,
                runtime_root=self.root, expected_marker=self.previous)
        self.set_marker(self.previous)
        before = self.marker_path.read_bytes()
        (self.root / self.previous['bundle'] / 'TEMPO/_internal/library.dll').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, 'inventory differs'):
            verify_component_transition(self.base, self.target, package, receipt,
                runtime_root=self.root, expected_marker=self.previous)
        self.assertEqual(self.marker_path.read_bytes(), before)

    def test_confirmation_requires_the_selected_verified_target_not_only_a_prepared_receipt(self):
        package, receipt = self.target_package(), self.staged()
        self.set_marker(self.previous)
        with self.assertRaisesRegex(ValueError, 'has not been selected'):
            verify_selected_runtime(self.target, package, receipt, runtime_root=self.root, previous_marker=self.previous)
        self.set_marker(self.selected)
        verify_selected_runtime(self.target, package, receipt, runtime_root=self.root, previous_marker=self.previous)
        (self.root / self.selected['bundle'] / 'TEMPO/_internal/library.dll').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'inventory differs'):
            verify_selected_runtime(self.target, package, receipt, runtime_root=self.root, previous_marker=self.previous)

    def test_invalid_duplicate_or_linked_marker_is_never_a_cpu_selection(self):
        for data in ('broken', 'null', '{"schema_version":1,"schema_version":1}'):
            self.marker_path.write_text(data)
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, 'Invalid NVIDIA selection'):
                read_runtime_marker(self.root)
        self.marker_path.unlink()
        try:
            self.marker_path.symlink_to(self.root / 'missing.json')
        except (OSError, NotImplementedError):
            self.skipTest('Symbolic links are unavailable')
        with self.assertRaisesRegex(ValueError, 'Link or reparse point'):
            read_runtime_marker(self.root)


if __name__ == '__main__':
    unittest.main()
