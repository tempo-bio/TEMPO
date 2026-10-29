"""Optional NVIDIA payload must be verified before it can become active."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

import tempo_nvidia_setup as setup
import tempo_nvidia_runtime as runtime
from tempo_release import VERSION


class _Response(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


def _archive(*, path='TEMPO/TEMPO.exe', payload=b'fixed-worker'):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr(path, payload)
    return output.getvalue()


class NvidiaSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.url = 'https://github.com/tempo-bio/TEMPO/releases/download/v1.0.1/TEMPO_NVIDIA_Runtime_v1.0.1.zip'

    def descriptor(self, data):
        return dict(schema_version=1, profile='nvidia-cu126', app_version=VERSION,
                    commit='a' * 40, archive_url=self.url,
                    archive_sha256=hashlib.sha256(data).hexdigest(), archive_size=len(data),
                    executable='TEMPO/TEMPO.exe',
                    executable_sha256=hashlib.sha256(b'fixed-worker').hexdigest())

    def test_verified_payload_is_activated_only_after_cuda_probe(self):
        archive = _archive()
        descriptor = self.descriptor(archive)
        snapshots = []
        with patch.object(setup, 'runtime_root', return_value=self.root), \
             patch.object(setup, '_probe', return_value={'success': True}) as probe:
            self.assertTrue(setup.install_nvidia_runtime(
                lambda received, total: snapshots.append((received, total)),
                descriptor=descriptor, open_url=lambda request, timeout: _Response(archive, self.url)))
        self.assertEqual(snapshots[-1], (len(archive), len(archive)))
        self.assertEqual(probe.call_count, 1)
        active = json.loads((self.root/'active.json').read_text())
        self.assertEqual(active['archive_sha256'], descriptor['archive_sha256'])
        self.assertEqual((self.root/active['bundle']/active['executable']).read_bytes(), b'fixed-worker')

    def test_activation_is_readable_by_the_worker_selector_for_the_same_release(self):
        archive = _archive()
        descriptor = self.descriptor(archive)
        with patch.object(setup, 'runtime_root', return_value=self.root), \
             patch.object(setup, '_probe', return_value={'success': True}):
            setup.install_nvidia_runtime(descriptor=descriptor,
                open_url=lambda request, timeout: _Response(archive, self.url))
        with patch.object(runtime, 'runtime_root', return_value=self.root), \
             patch.object(runtime.sys, 'platform', 'win32'), \
             patch.object(runtime.sys, 'frozen', True, create=True), \
             patch.object(setup, 'release_descriptor', return_value=descriptor), \
             patch('tempo_release.build_identity', return_value={'commit': 'a' * 40}):
            self.assertEqual(runtime.active_executable().read_bytes(), b'fixed-worker')
            with patch.object(setup, 'release_descriptor', return_value={**descriptor, 'archive_sha256': 'b' * 64}):
                self.assertIsNone(runtime.active_executable())

    def test_download_and_probe_failures_leave_prior_runtime_active(self):
        old = {'schema_version': 1, 'version': 'previous'}
        (self.root/'active.json').write_text(json.dumps(old))
        archive = _archive()
        descriptor = self.descriptor(archive)
        with patch.object(setup, 'runtime_root', return_value=self.root):
            with self.assertRaisesRegex(ValueError, 'checksum'):
                setup.install_nvidia_runtime(descriptor=descriptor,
                    open_url=lambda request, timeout: _Response(archive[:-1] + b'X', self.url))
            with patch.object(setup, '_probe', side_effect=RuntimeError('no GPU')):
                with self.assertRaisesRegex(RuntimeError, 'no GPU'):
                    setup.install_nvidia_runtime(descriptor=descriptor,
                        open_url=lambda request, timeout: _Response(archive, self.url))
        self.assertEqual(json.loads((self.root/'active.json').read_text()), old)

    def test_archive_rejects_traversal_links_and_duplicate_casefolded_members(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('../escape', 'TEMPO/../../escape', '/TEMPO/TEMPO.exe', 'TEMPO\\bad.exe'):
                file = root/'archive.zip'
                file.write_bytes(_archive(path=name))
                with self.assertRaises(ValueError):
                    setup._extract_checked(file, root/'out')
            file.write_bytes(_archive(path='TEMPO/TEMPO.exe'))
            with zipfile.ZipFile(file, 'a') as out:
                out.writestr('TEMPO/tempo.EXE', b'other')
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                setup._extract_checked(file, root/'out')

    def test_descriptor_requires_matching_embedded_release_and_origin(self):
        descriptor = self.descriptor(_archive())
        assets = self.root/'assets'
        assets.mkdir()
        path = assets/'nvidia-runtime.json'
        path.write_text(json.dumps(descriptor))
        with patch.object(setup.sys, 'platform', 'win32'), \
             patch.object(setup.sys, 'frozen', True, create=True), \
             patch.object(setup, 'get_base_dir', return_value=str(self.root)), \
             patch.object(setup, 'build_identity', return_value={'commit': 'a' * 40}):
            self.assertEqual(setup.release_descriptor(), descriptor)
            path.write_text(json.dumps({**descriptor, 'archive_url': 'https://example.com/payload.zip'}))
            self.assertIsNone(setup.release_descriptor())
            path.write_text(json.dumps({**descriptor, 'commit': 'b' * 40}))
            self.assertIsNone(setup.release_descriptor())


if __name__ == '__main__':
    unittest.main()
