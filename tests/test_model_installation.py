"""Model names, manifests, checksums and staging protect installed weights."""
import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tempo_hub import TempoHub


class ModelInstallationTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hub = TempoHub.__new__(TempoHub)
        self.hub.token = None; self.hub.get_models_repo = lambda: 'synthetic/models'
        self.source = self.root/'candidate.bin'; self.source.write_bytes(b'candidate')
        self.digest = hashlib.sha256(b'candidate').hexdigest()
        self.manifest = {'revision': 'synthetic', 'models': {'yolo26n-scout.pt': {'sha256': self.digest}}}

    def test_nonportable_or_colliding_names_reject_before_download(self):
        for names in (('yolo26n-scout.pt', 'YOLO26N-SCOUT.pt'), ('..\\yolo26n-scout.pt',), ('..',), ('CON.pt',),
                      ('yolo26n-scout.pt.',), ('model:stream.pt',), ('',)):
            manifest = dict(self.manifest, models={n: {'sha256': self.digest} for n in names})
            with self.subTest(names=names), patch('tempo_hub.hf_hub_download', return_value=str(self.source)) as download:
                with self.assertRaises(ValueError):
                    self.hub.download_model_release(manifest, self.root)
                download.assert_not_called()
        self.assertEqual(list(self.root.glob('.model-update-*')), [])

    def test_manifest_read_rejects_bad_shapes_and_names(self):
        self.hub.token = 'fixture-token'
        self.hub.api = Mock(); self.hub.api.repo_info.return_value = SimpleNamespace(sha='synthetic')
        for raw in (None, [], {'models': []}, {'models': {'yolo26n-scout.pt': None}},
                    {'models': {'yolo26n-scout.pt': {'sha256': 123}}},
                    {'models': {'yolo26n-scout.pt': {'sha256': self.digest}, 'YOLO26N-SCOUT.pt': {'sha256': self.digest}}}):
            self.hub._read = Mock(return_value=copy.deepcopy(raw))
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.hub.model_manifest()

    def test_verified_local_sources_use_the_same_installer_without_redownload(self):
        with patch('tempo_hub.hf_hub_download',side_effect=AssertionError('Already staged weights must not be downloaded')):
            self.assertEqual(self.hub.download_model_release(self.manifest,self.root,
                             local_sources={'yolo26n-scout.pt':self.source}),1)
        self.assertEqual((self.root/'yolo26n-scout.pt').read_bytes(),b'candidate')

    def test_cached_source_reports_verification_then_installation_without_fake_transfer(self):
        events = []
        self.hub.download_model_release(self.manifest, self.root,
            local_sources={'yolo26n-scout.pt': self.source}, progress=events.append)
        self.assertEqual([event.stage for event in events],
                         ['checking', 'verifying', 'verified', 'installing', 'complete'])
        self.assertEqual((self.root/'yolo26n-scout.pt').read_bytes(), b'candidate')

    def test_checksum_failure_reports_failure_and_preserves_existing_model(self):
        target = self.root/'yolo26n-scout.pt'
        target.write_bytes(b'installed model')
        self.source.write_bytes(b'wrong download')
        events = []
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.hub.download_model_release(self.manifest, self.root,
                local_sources={'yolo26n-scout.pt': self.source}, progress=events.append)
        self.assertEqual(events[-1].stage, 'failed')
        self.assertNotIn('complete', [event.stage for event in events])
        self.assertEqual(target.read_bytes(), b'installed model')

    def test_broken_progress_callback_cannot_interrupt_model_installation(self):
        self.assertEqual(self.hub.download_model_release(self.manifest, self.root,
            local_sources={'yolo26n-scout.pt': self.source},
            progress=Mock(side_effect=RuntimeError('Display unavailable'))), 1)

    def test_wrong_local_source_cannot_replace_incumbent(self):
        target=self.root/'yolo26n-scout.pt';target.write_bytes(b'incumbent')
        self.source.write_bytes(b'wrong checkpoint')
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):
            self.hub.download_model_release(self.manifest,self.root,local_sources={'yolo26n-scout.pt':self.source})
        self.assertEqual(target.read_bytes(),b'incumbent')

    def test_manifest_read_requires_authentication_before_remote_access(self):
        self.hub.api = Mock()
        self.hub._read = Mock()
        with self.assertRaisesRegex(RuntimeError, 'authenticated online connection'):
            self.hub.model_manifest()
        self.hub.api.repo_info.assert_not_called()
        self.hub._read.assert_not_called()

    def test_uppercase_checksum_is_supported_without_mutating_manifest(self):
        self.manifest['models']['yolo26n-scout.pt']['sha256'] = self.digest.upper()
        original = copy.deepcopy(self.manifest)
        with patch('tempo_hub.hf_hub_download', return_value=str(self.source)):
            self.assertEqual(self.hub.download_model_release(self.manifest, self.root), 1)
        self.assertEqual((self.root/'yolo26n-scout.pt').read_bytes(), b'candidate')
        self.assertEqual(self.manifest, original)

    def test_unregistered_files_reject_before_any_install_or_download(self):
        for name in ('tempo_gui.py', 'tempo_config.py', 'model.pt',
                     'yolo26n-scout.pt.previous', 'preferences.json'):
            manifest = dict(self.manifest, models={name: {'sha256': self.digest}})
            with self.subTest(name=name), patch('tempo_hub.hf_hub_download') as download:
                with self.assertRaises(ValueError):
                    self.hub.download_model_release(manifest, self.root)
                download.assert_not_called()
        self.assertFalse(list(self.root.glob('.model-update-*')))

    def test_install_failure_restores_all_old_weights_and_removes_staging(self):
        for name in ('yolo26n-scout.pt', 'stardist_nuclei.zip'): (self.root/name).write_bytes(('old '+name).encode())
        manifest = dict(self.manifest, models={n: {'sha256': self.digest} for n in ('yolo26n-scout.pt', 'stardist_nuclei.zip')})
        replace = os.replace
        def fail_second(source, target):
            if Path(target) == self.root/'stardist_nuclei.zip': raise OSError('simulated installation failure')
            return replace(source, target)
        with patch('tempo_hub.hf_hub_download', return_value=str(self.source)), \
             patch('tempo_hub.os.replace', side_effect=fail_second):
            with self.assertRaises(OSError): self.hub.download_model_release(manifest, self.root)
        for name in ('yolo26n-scout.pt', 'stardist_nuclei.zip'): self.assertEqual((self.root/name).read_bytes(), ('old '+name).encode())
        self.assertFalse(list(self.root.glob('.model-update-*')))

    def test_failed_model_rollback_retains_previous_weight_backup(self):
        hub = TempoHub.__new__(TempoHub)
        hub.token = None; hub.get_models_repo = lambda: 'synthetic/models'
        first, second = 'yolo26n-scout.pt', 'stardist_nuclei.zip'
        (self.root/first).write_bytes(b'old-first')
        (self.root/second).write_bytes(b'old-second')
        source = self.root/'candidate.bin'; source.write_bytes(b'new')
        manifest = {'revision': 'synthetic', 'models': {
            name: {'sha256': hub.calculate_sha256(source)} for name in (first, second)}}
        original_replace = os.replace
        def replace(old, new):
            if Path(new) == self.root/second or str(old).endswith('.previous'):
                raise OSError('simulated model in use')
            return original_replace(old, new)
        with patch('tempo_hub.hf_hub_download', return_value=str(source)), \
             patch('tempo_hub.os.replace', side_effect=replace):
            with self.assertRaisesRegex(RuntimeError, 'backups.*retained'):
                hub.download_model_release(manifest, str(self.root))
        backups = list(self.root.glob('.model-update-*/backups/yolo26n-scout.pt.previous'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'old-first')
        self.assertEqual((self.root/second).read_bytes(), b'old-second')

    def test_failed_model_rollback_still_restores_other_models(self):
        hub = TempoHub.__new__(TempoHub)
        hub.token = None; hub.get_models_repo = lambda: 'synthetic/models'
        names = ('yolo26n-scout.pt', 'stardist_nuclei.zip', 'cellpose4_cardio.pt')
        for name in names:
            (self.root/name).write_bytes(('old-'+name).encode())
        source = self.root/'candidate.bin'; source.write_bytes(b'new')
        manifest = {'revision': 'synthetic', 'models': {
            name: {'sha256': hub.calculate_sha256(source)} for name in names}}
        original_replace = os.replace
        def replace(old, new):
            if Path(new) == self.root/'cellpose4_cardio.pt' or str(old).endswith('yolo26n-scout.pt.previous'):
                raise OSError('simulated model in use')
            return original_replace(old, new)
        with patch('tempo_hub.hf_hub_download', return_value=str(source)), \
             patch('tempo_hub.os.replace', side_effect=replace):
            with self.assertRaisesRegex(RuntimeError, 'backups.*retained'):
                hub.download_model_release(manifest, str(self.root))
        self.assertEqual((self.root/'stardist_nuclei.zip').read_bytes(), b'old-stardist_nuclei.zip')
        self.assertEqual((self.root/'cellpose4_cardio.pt').read_bytes(), b'old-cellpose4_cardio.pt')
        self.assertEqual(len(list(self.root.glob('.model-update-*/backups/yolo26n-scout.pt.previous'))), 1)


if __name__ == "__main__":
    unittest.main()
