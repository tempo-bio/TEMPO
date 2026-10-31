"""Bounded release downloads and seeking across real part boundaries."""
import copy
import io
from pathlib import Path
import tempfile
import unittest

from tempo_nvidia_manifest import JoinedFiles, MAX_ARCHIVE, MAX_PART, validate_descriptor


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.url = 'https://github.com/tempo-bio/TEMPO/releases/download/v1.0.1/TEMPO_NVIDIA_Runtime_v1.0.1.zip'
        self.value = dict(schema_version=2, profile='nvidia-cu126', app_version='1.0.1',
                          commit='a'*40, executable='TEMPO/TEMPO.exe',
                          archive_size=6, archive_sha256='b'*64, executable_sha256='c'*64,
                          parts=[dict(url=self.url+'.part001', size=3, sha256='d'*64),
                                 dict(url=self.url+'.part002', size=3, sha256='e'*64)])

    def validate(self, value):
        return validate_descriptor(value, version='1.0.1', commit='a'*40)

    def test_current_and_legacy_descriptor_identity(self):
        self.assertEqual(self.validate(self.value), self.value)
        legacy = dict(self.value, schema_version=1, archive_url=self.url)
        legacy.pop('parts')
        self.assertEqual(self.validate(legacy), legacy)
        with self.assertRaises(ValueError):
            validate_descriptor(legacy, version='1.0.1', commit='a'*40, allow_legacy=False)

    def test_unpublishable_or_ambiguous_manifests_are_rejected(self):
        changes = [dict(archive_size=True), dict(schema_version=True), dict(commit='b'*40),
                   dict(archive_size=MAX_ARCHIVE+1), dict(parts=[]), dict(parts=None),
                   dict(parts=self.value['parts']*5), dict(parts=list(reversed(self.value['parts']))),
                   dict(parts=[self.value['parts'][0]]*2), dict(archive_size=7)]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(dict(self.value, **change))
        for change in (dict(size=True), dict(size=MAX_PART+1), dict(size=0), dict(sha256='bad'),
                       dict(url=self.url+'.part001?token=private'), dict(url=self.url.replace('v1.0.1/', 'v1.0.0/')+'.part001'),
                       dict(url='https://example.com/runtime.part001'), dict(url=self.url+'.part002')):
            value = copy.deepcopy(self.value)
            value['parts'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(value)

    def test_6gb_manifest_requires_no_6gb_test_fixture(self):
        value = copy.deepcopy(self.value)
        value['archive_size'] = 6 * MAX_PART
        value['parts'] = [dict(url=f'{self.url}.part{index:03d}', size=MAX_PART, sha256='d'*64)
                          for index in range(1, 7)]
        self.assertEqual(self.validate(value)['archive_size'], 6 * MAX_PART)
        value['parts'][0]['size'] += 1
        with self.assertRaises(ValueError):
            self.validate(value)

    def test_joined_files_read_seek_and_close_across_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder)/str(index) for index in range(3)]
            for path, data in zip(paths, (b'abc', b'defg', b'hij')):
                path.write_bytes(data)
            with JoinedFiles(paths) as stream:
                self.assertEqual(stream.read(5), b'abcde')
                self.assertEqual(stream.seek(-3, io.SEEK_END), 7)
                self.assertEqual(stream.read(), b'hij')
                stream.seek(2)
                self.assertEqual(stream.read(6), b'cdefgh')
                self.assertEqual(stream.seek(-6, io.SEEK_CUR), 2)
                self.assertEqual(stream.read(1), b'c')
                stream.seek(20)
                self.assertEqual(stream.read(), b'')
                with self.assertRaises(ValueError):
                    stream.seek(-1)
                with self.assertRaises(ValueError):
                    stream.seek(0, 999)
            with self.assertRaises(ValueError):
                stream.read(1)
            for path in paths:
                path.unlink()
