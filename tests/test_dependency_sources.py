"""Dependency source downloads fail safely and stay matched to Windows builds."""
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.prepare_dependency_sources import (
    MANIFEST, ROOT, HTTPSRedirects, check_inventory, retain_source, validate, write_index,
)
import tempo_cloud
import tempo_release


class DependencySourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.lock = self.root/'requirements.lock'
        self.lock.write_text('example @ https://example.org/example-1.0-py3-none-any.whl --hash=sha256:'
                             + 'a'*64 + '\n')
        self.data = b'Synthetic upstream source archive'
        self.source = dict(id='example', version='1.0', license='LGPL-3.0', required_by={'example':'1.0'},
                           basis='Synthetic maintained test fixture', filename='example-1.0.tar.gz',
                           url='https://example.org/example-1.0.tar.gz', size=len(self.data),
                           sha256=hashlib.sha256(self.data).hexdigest())
        self.manifest = dict(schema=1, windows_lock_sha256=hashlib.sha256(self.lock.read_bytes()).hexdigest(),
                             sources=[self.source])

    def test_reviewed_manifest_matches_actual_windows_lock_and_copyleft_inventory(self):
        manifest = json.loads(MANIFEST.read_text())
        versions, covered = validate(manifest, ROOT/'requirements-windows.lock')
        inventory = {'packages':[dict(name=name, version=versions[name], declared_license='GPL-3.0')
                                  for name in covered]}
        check_inventory(inventory, manifest)
        output = self.root/'reviewed-index'
        write_index(manifest, output)
        self.assertEqual((output/'dependency-sources.json').read_bytes(), MANIFEST.read_bytes())

    def test_dependency_change_invalidates_source_review(self):
        self.lock.write_text(self.lock.read_text().replace('example-1.0-', 'example-2.0-'))
        with self.assertRaisesRegex(ValueError, 'dependencies changed'):
            validate(self.manifest, self.lock)

    def test_windows_checkout_preserves_every_profile_lock_and_source_manifest(self):
        from scripts.windows_build_profile import PROFILES
        repository = self.root/'repository'
        checkout = self.root/'windows checkout'
        repository.mkdir()
        files = {'.gitattributes'} | {name for profile in PROFILES.values()
                                      for name in (profile.lock, profile.source_manifest)}
        for name in files:
            destination = repository/name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT/name, destination)
        def git(*arguments):
            subprocess.run(['git', '-C', str(repository), '-c', 'core.autocrlf=true',
                            *arguments], check=True, capture_output=True)
        git('init', '--quiet')
        git('add', '.')
        git('checkout-index', '--all', '--prefix='+checkout.as_posix()+'/')
        for profile in PROFILES.values():
            with self.subTest(profile=profile.key):
                for name in (profile.lock, profile.source_manifest):
                    self.assertEqual((checkout/name).read_bytes(), (ROOT/name).read_bytes())
                validate(json.loads((checkout/profile.source_manifest).read_text()),
                         checkout/profile.lock)

    def test_source_cannot_claim_a_different_package_version(self):
        self.source['required_by']['example'] = '2.0'
        with self.assertRaisesRegex(ValueError, 'locked dependency'):
            validate(self.manifest, self.lock)

    def test_inventory_rejects_new_copyleft_package_or_changed_version(self):
        with self.assertRaisesRegex(ValueError, 'Missing dependency license notices'):
            check_inventory({'packages': [], 'packages_without_license_files': ['example']}, self.manifest)
        for name, version in (('unreviewed', '1.0'), ('example', '2.0')):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Unreviewed'):
                check_inventory({'packages':[dict(name=name, version=version,
                                                 declared_license='LGPL-3.0')]}, self.manifest)

    def test_manifest_rejects_filename_escape_credentials_and_duplicate_archives(self):
        for field, value in (('filename', '../outside.tar.gz'),
                             ('url', 'http://example.org/source.tar.gz'),
                             ('url', 'https://example.org/source.tar.gz?credential=synthetic'),
                             ('sha256', 'not-a-checksum'), ('size', -1)):
            manifest = copy.deepcopy(self.manifest)
            manifest['sources'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(manifest, self.lock)
        manifest = copy.deepcopy(self.manifest)
        manifest['sources'].append(copy.deepcopy(self.source))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            validate(manifest, self.lock)

    def test_verified_download_is_retained_and_offline_recheck_needs_no_network(self):
        opener = Mock(return_value=io.BytesIO(self.data))
        result = retain_source(self.source, self.root/'cache', fetch=True, opener=opener)
        self.assertEqual(result['sha256'], self.source['sha256'])
        opener.assert_called_once()
        offline = Mock(side_effect=AssertionError('Offline check attempted a download'))
        self.assertEqual(retain_source(self.source, self.root/'cache', opener=offline), result)
        self.assertEqual(list((self.root/'cache').iterdir()), [self.root/'cache'/self.source['filename']])

    def test_corrupt_or_oversized_download_leaves_no_archive_or_scratch_file(self):
        for data in (b'bad', b'x'*(self.source['size']+1)):
            cache = self.root/'cache'
            with self.subTest(size=len(data)), self.assertRaisesRegex(ValueError, 'checksum|size'):
                retain_source(self.source, cache, fetch=True, opener=lambda *a, **k:io.BytesIO(data))
            self.assertEqual(list(cache.iterdir()), [])

    def test_corrupt_existing_cache_is_preserved_and_reported_without_refetch(self):
        cache = self.root/'cache'
        cache.mkdir()
        target = cache/self.source['filename']
        target.write_bytes(b'Corrupt synthetic archive')
        opener = Mock()
        with self.assertRaisesRegex(ValueError, 'Cached source checksum'):
            retain_source(self.source, cache, fetch=True, opener=opener)
        opener.assert_not_called()
        self.assertEqual(target.read_bytes(), b'Corrupt synthetic archive')

    def test_missing_cache_cannot_be_certified_offline(self):
        with self.assertRaisesRegex(ValueError, 'Missing source archive'):
            retain_source(self.source, self.root/'cache')

    def test_download_redirect_cannot_downgrade_https(self):
        with self.assertRaisesRegex(ValueError, 'Unsafe source download redirect'):
            HTTPSRedirects().redirect_request(None, None, 302, '', {}, 'http://example.org/source')

    def test_generated_index_retains_exact_manifest_and_source_checksums(self):
        output = self.root/'index'
        output.mkdir()
        (output/'local-preferences.json').write_text('Synthetic local preferences')
        files = write_index(self.manifest, output)
        self.assertEqual({path.name for path in files},
                         {'dependency-sources.json', 'DEPENDENCY_SOURCES.md', 'SHA256SUMS.txt'})
        self.assertNotIn(output/'local-preferences.json', files)
        self.assertEqual(json.loads((output/'dependency-sources.json').read_text()), self.manifest)
        self.assertIn(self.source['url'], (output/'DEPENDENCY_SOURCES.md').read_text())
        self.assertEqual((output/'SHA256SUMS.txt').read_text(),
                         self.source['sha256'] + '  example-1.0.tar.gz\n')

    def test_packaged_source_index_must_match_build_identity(self):
        root = self.root/'payload'
        (root/'assets').mkdir(parents=True)
        (root/'docs').mkdir()
        for name in ('tempo_icon.ico', 'tempo_icon.png', 'tempo_shell_icon.png', 'tempo_logo_base.png', 'verification_add.onnx'):
            (root/'assets'/name).write_bytes(b'Synthetic required asset')
        (root/'docs/TRAINING_WORKFLOW.md').write_text('Synthetic workflow')
        for relative in ('docs/STIMULATION_SETUP.md', 'firmware/tempo_pacer/tempo_pacer.ino'):
            path = root/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Synthetic required setup resource')
        (root/'LICENSE').write_text('Synthetic project license')
        (root/'third-party-sources').mkdir()
        manifest = root/'third-party-sources/dependency-sources.json'
        manifest.write_text('{}')
        identity = dict(lock_sha256='synthetic-lock',
                        project_license_sha256=hashlib.sha256((root/'LICENSE').read_bytes()).hexdigest(),
                        dependency_sources_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())
        with patch.object(tempo_cloud, 'BUNDLE_FILES', ()), \
             patch.object(tempo_release.sys, '_MEIPASS', str(root), create=True), \
             patch.object(tempo_release.sys, 'frozen', True, create=True), \
             patch.object(tempo_release, 'build_identity', return_value=identity), \
             patch.object(tempo_release, 'verify_bundled_cpp_runtime', return_value={}), \
             patch.object(tempo_release, 'verify_wheel_libraries', return_value={}):
            self.assertEqual(tempo_release.probe_action('bundled payload')['dependency_sources']['sha256'],
                             identity['dependency_sources_sha256'])
            logo = root/'assets/tempo_logo_base.png'
            logo.unlink()
            with self.assertRaisesRegex(RuntimeError, 'tempo_logo_base.png'):
                tempo_release.probe_action('bundled payload')
            logo.write_bytes(b'Synthetic required asset')
            manifest.write_text('{"changed":true}')
            with self.assertRaisesRegex(RuntimeError, 'source index differs'):
                tempo_release.probe_action('bundled payload')
            manifest.unlink()
            with self.assertRaisesRegex(RuntimeError, 'source index is missing'):
                tempo_release.probe_action('bundled payload')


if __name__ == '__main__':
    unittest.main()
