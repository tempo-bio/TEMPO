"""Optional NVIDIA payload must be verified before it can become active."""
import hashlib
import io
import json
import stat
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
        # ZipInfo's constructor replaces the host separator on Windows.
        # Preserve the raw archive name so unsafe-path fixtures stay unsafe.
        info = zipfile.ZipInfo()
        info.filename = path
        archive.writestr(info, payload)
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

    def multipart(self, data):
        descriptor = self.descriptor(data)
        descriptor.update(schema_version=2, parts=[])
        descriptor.pop('archive_url')
        payloads = {}
        size = max(1, (len(data)+2)//3)
        for index, start in enumerate(range(0, len(data), size), 1):
            content = data[start:start+size]
            url = f'{self.url}.part{index:03d}'
            descriptor['parts'].append(dict(url=url, size=len(content), sha256=hashlib.sha256(content).hexdigest()))
            payloads[url] = content
        return descriptor, payloads

    def test_multipart_download_is_combined_verified_and_activated_with_aggregate_progress(self):
        data = _archive()
        descriptor, payloads = self.multipart(data)
        requested, progress = [], []
        def opener(request, timeout):
            self.assertEqual(timeout, 30)
            requested.append(request.full_url)
            return _Response(payloads[request.full_url], request.full_url)
        with patch.object(setup, 'runtime_root', return_value=self.root), \
             patch.object(setup, '_probe', return_value={'success':True}) as probe:
            self.assertTrue(setup.install_nvidia_runtime(lambda *value: progress.append(value),
                descriptor=descriptor, open_url=opener))
        self.assertEqual(requested, [part['url'] for part in descriptor['parts']])
        self.assertEqual(progress[-1], (len(data), len(data)))
        self.assertEqual([value[0] for value in progress], sorted(value[0] for value in progress))
        self.assertTrue(all(value[1] == len(data) for value in progress))
        probe.assert_called_once()
        marker = json.loads((self.root/'active.json').read_text())
        executable = self.root/marker['bundle']/marker['executable']
        self.assertEqual(executable.read_bytes(), b'fixed-worker')
        self.assertFalse(list(self.root.glob('nvidia-download-*')))
        with patch.object(runtime, 'runtime_root', return_value=self.root), \
             patch.object(runtime.sys, 'platform', 'win32'), \
             patch.object(runtime.sys, 'frozen', True, create=True), \
             patch.object(setup, 'release_descriptor', return_value=descriptor), \
             patch('tempo_release.build_identity', return_value={'commit':'a'*40}):
            self.assertEqual(runtime.active_executable(), executable.resolve())

    def test_multipart_transfer_failures_preserve_existing_activation_and_remove_partial_downloads(self):
        from urllib.error import HTTPError
        data = _archive()
        descriptor, payloads = self.multipart(data)
        target = descriptor['parts'][1]['url']
        old = '{"previous":"active"}'
        for failure in ('short', 'long', 'corrupt', 'missing', 'timeout', 'redirect'):
            (self.root/'active.json').write_text(old)
            def opener(request, timeout):
                content, url = payloads[request.full_url], request.full_url
                if request.full_url == target:
                    if failure == 'short': content = content[:-1]
                    if failure == 'long': content += b'extra'
                    if failure == 'corrupt': content = content[:-1]+bytes([content[-1]^255])
                    if failure == 'missing': raise HTTPError(url, 404, 'missing', {}, None)
                    if failure == 'timeout': raise TimeoutError('download stalled')
                    if failure == 'redirect': url = 'https://example.com/worker'
                return _Response(content, url)
            with self.subTest(failure=failure), patch.object(setup, 'runtime_root', return_value=self.root), \
                 patch.object(setup, '_probe') as probe:
                with self.assertRaises((ValueError, HTTPError, TimeoutError)):
                    setup.install_nvidia_runtime(descriptor=descriptor, open_url=opener)
                probe.assert_not_called()
                self.assertEqual((self.root/'active.json').read_text(), old)
                self.assertFalse(list(self.root.glob('nvidia-download-*')))

    def test_packager_assets_install_without_manual_reassembly(self):
        from scripts.package_nvidia_runtime import package, verify_descriptor
        bundle = self.root/'bundle'
        (bundle/'_internal/torch/lib').mkdir(parents=True)
        (bundle/'TEMPO.exe').write_bytes(b'fixed-worker')
        (bundle/'_internal/torch/lib/torch_cuda.dll').write_bytes(b'fixture CUDA A')
        (bundle/'_internal/torch/lib/c10_cuda.dll').write_bytes(b'fixture CUDA B')
        provenance = self.root/'provenance.json'
        provenance.write_text(json.dumps(dict(commit='a'*40, profile='nvidia-cu126')))
        output = self.root/'release-assets'
        descriptor = package(bundle, output, tag='v'+VERSION, provenance=provenance, part_size=256)
        self.assertEqual(verify_descriptor(output/'nvidia-runtime.json', commit='a'*40), descriptor)
        requested = []
        def opener(request, timeout):
            requested.append(request.full_url)
            path = output/request.full_url.rsplit('/', 1)[-1]
            return _Response(path.read_bytes(), request.full_url)
        with patch.object(setup, 'runtime_root', return_value=self.root/'installed'), \
             patch.object(setup, '_probe', return_value={'success':True}):
            self.assertTrue(setup.install_nvidia_runtime(descriptor=descriptor, open_url=opener))
        self.assertEqual(requested, [part['url'] for part in descriptor['parts']])
        marker = json.loads((self.root/'installed/active.json').read_text())
        self.assertEqual((self.root/'installed'/marker['bundle']/'TEMPO/_internal/torch/lib/torch_cuda.dll').read_bytes(), b'fixture CUDA A')

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

    def test_gpu_check_uses_owned_headless_worker_and_validates_its_report(self):
        from types import SimpleNamespace
        descriptor = {'commit':'a'*40, 'app_version':VERSION}
        result = dict(success=True, commit='a'*40, app_version=VERSION, cuda_version='12.6')
        def launch(command, **options):
            self.assertEqual(command[:2], ['fixture-worker.exe', '--probe-cuda'])
            self.assertEqual(options, {'timeout':120})
            Path(command[-1]).write_text(json.dumps(result))
            return SimpleNamespace(returncode=0)
        with patch.object(setup, 'run_model_worker', side_effect=launch):
            self.assertEqual(setup._probe(Path('fixture-worker.exe'), descriptor), result)
            result['commit'] = 'b'*40
            with self.assertRaisesRegex(RuntimeError, 'did not pass'):
                setup._probe(Path('fixture-worker.exe'), descriptor)

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

    def test_reinstall_repairs_support_libraries_even_when_executable_is_unchanged(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('TEMPO/TEMPO.exe', b'fixed-worker')
            archive.writestr('TEMPO/_internal/support.dll', b'valid-library')
        data = output.getvalue(); descriptor = self.descriptor(data)
        with patch.object(setup, 'runtime_root', return_value=self.root), \
             patch.object(setup, '_probe', return_value={'success': True}):
            install = lambda: setup.install_nvidia_runtime(descriptor=descriptor,
                open_url=lambda *a, **k: _Response(data, self.url))
            install()
            state = json.loads((self.root/'active.json').read_text())
            library = self.root/state['bundle']/'TEMPO/_internal/support.dll'
            library.write_bytes(b'corrupted-library')
            install()
        self.assertEqual(library.read_bytes(), b'valid-library')
        self.assertEqual(json.loads((self.root/'active.json').read_text()), state)
        self.assertFalse(list(self.root.glob('*.repair-backup-*')))

    def test_failed_repair_restores_previous_bundle_and_activation(self):
        data = _archive(); descriptor = self.descriptor(data)
        bundle = descriptor['commit']+'-'+descriptor['archive_sha256'][:12]
        installed = self.root/bundle/'TEMPO/TEMPO.exe'
        installed.parent.mkdir(parents=True); installed.write_bytes(b'old-damaged-executable')
        (self.root/'active.json').write_text('{"previous":true}')
        original = setup.os.replace
        def replace(source, target):
            if Path(source).name == 'unpacked':
                raise PermissionError('in use')
            return original(source, target)
        with patch.object(setup, 'runtime_root', return_value=self.root), \
             patch.object(setup, '_probe', return_value={'success': True}), \
             patch.object(setup.os, 'replace', side_effect=replace):
            with self.assertRaisesRegex(RuntimeError, 'Close other TEMPO'):
                setup.install_nvidia_runtime(descriptor=descriptor,
                    open_url=lambda *a, **k: _Response(data, self.url))
        self.assertEqual(installed.read_bytes(), b'old-damaged-executable')
        self.assertEqual(json.loads((self.root/'active.json').read_text()), {'previous':True})

    def test_archive_rejects_traversal_links_and_duplicate_casefolded_members(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('../escape', 'TEMPO/../../escape', '/TEMPO/TEMPO.exe', 'TEMPO\\bad.exe',
                         'TEMPO/./bad.exe', 'TEMPO//bad.exe', 'TEMPO/bad.exe.', 'TEMPO/bad.exe '):
                file = root/'archive.zip'
                file.write_bytes(_archive(path=name))
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'unsafe path'):
                    setup._extract_checked(file, root/'out')
            # Emulate ZipInfo's Windows read normalization on every test host.
            info = zipfile.ZipInfo('TEMPO/bad.exe')
            info.orig_filename = 'TEMPO\\bad.exe'
            with patch.object(zipfile.ZipFile, 'infolist', return_value=[info]):
                with self.assertRaisesRegex(ValueError, 'unsafe path'):
                    setup._extract_checked(file, root/'out')
            with zipfile.ZipFile(file, 'w') as out:
                link = zipfile.ZipInfo('TEMPO/link')
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                out.writestr(link, '../outside')
            with self.assertRaisesRegex(ValueError, 'link'):
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
            multipart, _ = self.multipart(_archive())
            path.write_text(json.dumps(multipart))
            self.assertEqual(setup.release_descriptor(), multipart)
            multipart['parts'][0]['url'] = 'https://example.com/part001'
            path.write_text(json.dumps(multipart))
            self.assertIsNone(setup.release_descriptor())


if __name__ == '__main__':
    unittest.main()
