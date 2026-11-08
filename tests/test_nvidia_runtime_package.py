"""The optional worker archive must match the tested build and pinned descriptor."""
import hashlib
import copy
import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from scripts.package_nvidia_runtime import package, verify_descriptor, verify_published
from tempo_nvidia_manifest import MAX_PART
from tempo_release import VERSION


class NvidiaRuntimePackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root/'bundle'
        (self.bundle/'_internal/torch/lib').mkdir(parents=True)
        (self.bundle/'TEMPO.exe').write_bytes(b'tested executable')
        (self.bundle/'_internal/torch/lib/torch_cuda.dll').write_bytes(b'CUDA A')
        (self.bundle/'_internal/torch/lib/c10_cuda.dll').write_bytes(b'CUDA B')
        self.provenance = self.root/'build-provenance.json'
        self.commit = 'a'*40
        self.provenance.write_text(json.dumps({'commit': self.commit, 'profile':'nvidia-cu126'}))
        self.output = self.root/'output'

    def package(self):
        return package(self.bundle, self.output, tag=f'v{VERSION}', provenance=self.provenance, part_size=256)

    def verify(self):
        return verify_descriptor(self.output/'nvidia-runtime.json', commit=self.commit)

    def test_archive_identity_and_tamper_rejection(self):
        descriptor = self.package()
        self.assertGreater(len(descriptor['parts']), 1)
        self.assertEqual(self.verify(), descriptor)
        self.assertFalse(list(self.output.glob('*.zip')))
        data = b''.join((self.output/part['url'].rsplit('/', 1)[-1]).read_bytes()
                        for part in descriptor['parts'])
        self.assertEqual(len(data), descriptor['archive_size'])
        self.assertEqual(hashlib.sha256(data).hexdigest(), descriptor['archive_sha256'])
        part = self.output/descriptor['parts'][0]['url'].rsplit('/', 1)[-1]
        part.write_bytes(part.read_bytes()[:-1]+b'X')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.verify()

    def test_missing_and_undeclared_release_assets_are_rejected(self):
        descriptor = self.package()
        part = self.output/descriptor['parts'][0]['url'].rsplit('/', 1)[-1]
        backup = part.read_bytes()
        part.unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.verify()
        part.write_bytes(backup)
        (self.output/f'TEMPO_NVIDIA_Runtime_v{VERSION}.zip').write_bytes(b'oversized old-style archive')
        with self.assertRaisesRegex(ValueError, 'undeclared'):
            self.verify()

    def test_publisher_rejects_oversized_parts_and_mismatched_runtime(self):
        with self.assertRaisesRegex(ValueError, '1 GiB'):
            package(self.bundle, self.output, tag=f'v{VERSION}', provenance=self.provenance, part_size=MAX_PART+1)
        self.provenance.write_text(json.dumps({'commit':self.commit, 'profile':'cpu'}))
        with self.assertRaisesRegex(ValueError, 'wrong profile'):
            self.package()

    def test_package_failure_leaves_no_publishable_partial_assets(self):
        with patch('scripts.package_nvidia_runtime.MAX_ARCHIVE', 300):
            with self.assertRaisesRegex(ValueError, 'limit'):
                self.package()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_part_order_and_combined_hash_are_verified(self):
        descriptor = self.package()
        manifest = self.output/'nvidia-runtime.json'
        descriptor['parts'].reverse()
        manifest.write_text(json.dumps(descriptor))
        with self.assertRaisesRegex(ValueError, 'order'):
            self.verify()
        descriptor['parts'].reverse()
        descriptor['archive_sha256'] = 'f'*64
        manifest.write_text(json.dumps(descriptor))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.verify()

    def test_worker_executable_is_checked_even_with_consistent_archive_checksums(self):
        descriptor = self.package()
        descriptor['executable_sha256'] = 'f'*64
        (self.output/'nvidia-runtime.json').write_text(json.dumps(descriptor))
        with self.assertRaisesRegex(ValueError, 'executable hash'):
            self.verify()

    def test_zip64_directory_survives_part_boundaries(self):
        # Exercise the >4 GiB ZIP layout with a lowered threshold, without
        # allocating multi-gigabyte files in each preflight run.
        with patch('zipfile.ZIP64_LIMIT', 64):
            descriptor = self.package()
            self.assertEqual(self.verify(), descriptor)

    def test_workflow_package_and_verification_commands_without_installed_dependencies(self):
        output = self.root/'cli-output'
        command = [sys.executable, '-S', '-m', 'scripts.package_nvidia_runtime']
        options = dict(cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
        packaged = subprocess.run([*command, '--bundle', str(self.bundle), '--output', str(output),
                                  '--provenance', str(self.provenance), '--release-tag', f'v{VERSION}'], **options)
        self.assertEqual(packaged.returncode, 0, packaged.stderr)
        verified = subprocess.run([*command, '--verify-descriptor', str(output/'nvidia-runtime.json'),
                                   '--commit', self.commit], **options)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(packaged.stdout), json.loads(verified.stdout))

    def test_publication_requires_all_exact_uploaded_parts_and_matching_source_tag(self):
        descriptor = self.package()
        release = dict(draft=False, prerelease=True, tag_name=f'v{VERSION}', assets=[dict(
            name=part['url'].rsplit('/', 1)[-1], size=part['size'], state='uploaded',
            digest='sha256:'+part['sha256'], browser_download_url=part['url']) for part in descriptor['parts']])
        source = {'sha':self.commit}
        requested = []
        def opener(request, timeout):
            self.assertEqual(timeout, 30)
            requested.append(request.full_url)
            self.assertTrue(request.full_url.startswith('https://api.github.com/repos/tempo-bio/TEMPO/'))
            value = release if '/releases/tags/' in request.full_url else source
            return io.BytesIO(json.dumps(value).encode())
        manifest = self.output/'nvidia-runtime.json'
        self.assertTrue(verify_published(manifest, commit=self.commit, open_url=opener)['success'])
        self.assertEqual(len(requested), 2)
        original = copy.deepcopy(release)
        for failure in ('missing', 'hash', 'size', 'url', 'pending', 'duplicate', 'oversized', 'draft', 'tag'):
            release = copy.deepcopy(original)
            if failure == 'missing':release['assets'].pop()
            if failure == 'hash':release['assets'][0]['digest'] = 'sha256:'+'f'*64
            if failure == 'size':release['assets'][0]['size'] += 1
            if failure == 'url':release['assets'][0]['browser_download_url'] += '?different'
            if failure == 'pending':release['assets'][0]['state'] = 'new'
            if failure == 'duplicate':release['assets'].append(release['assets'][0])
            if failure == 'oversized':release['assets'][0]['size'] = 2*1024**3
            if failure == 'draft':release['draft'] = True
            if failure == 'tag':release['tag_name'] = 'v1.0.0'
            with self.subTest(failure=failure), self.assertRaises(ValueError):
                verify_published(manifest, commit=self.commit, open_url=opener)
        release = original
        source['sha'] = 'b'*40
        with self.assertRaisesRegex(ValueError, 'source revision'):
            verify_published(manifest, commit=self.commit, open_url=opener)


if __name__ == '__main__':
    unittest.main()
