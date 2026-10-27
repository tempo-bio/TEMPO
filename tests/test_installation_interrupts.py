"""Handled interrupts must restore installations or retain recovery backups."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from tempo_hub import TempoHub
from tempo_model_catalog import MODEL_FILES
from tempo_training import build_dataset


class InstallationInterruptTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hub = TempoHub.__new__(TempoHub); self.hub.token = None
        self.hub.get_models_repo = lambda: 'synthetic/models'
        self.source = self.root/'candidate.bin'; self.source.write_bytes(b'candidate')
        self.first_model=MODEL_FILES['nano_scout'];self.second_model=MODEL_FILES['cellpose']
        self.manifest = {'revision': 'synthetic', 'models': {
            n: {'sha256': self.hub.calculate_sha256(self.source)} for n in (self.first_model,self.second_model)}}
        for name in self.manifest['models']: (self.root/name).write_bytes(('old '+name).encode())

    def dataset(self):
        images = self.root/'datasets/images'; images.mkdir(parents=True)
        records = {}
        for n in range(3):
            name = f'{n}.png'; self.assertTrue(cv2.imwrite(str(images/name), np.zeros((32,32), np.uint8)))
            records[name] = {'status': 'approved', 'source_well':f'well-{n}', 'boxes': [
                {'class_id': 0, 'polygon': [[1,1], [20,1], [20,20]]}]}
        source = self.root/'datasets/universal_annotations.json'; source.write_text(json.dumps(records))
        output = build_dataset(self.root)
        previous = self.snapshot(output)
        records['0.png']['boxes'][0]['class_id'] = 1; source.write_text(json.dumps(records))
        return output, previous

    @staticmethod
    def snapshot(folder):
        return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob('*') if p.is_file()}

    def test_model_interrupt_before_or_after_replace_restores_old_weights(self):
        replace = os.replace
        for after in (False, True):
            interrupted = []
            for name in self.manifest['models']: (self.root/name).write_bytes(('old '+name).encode())
            def stop(source, target):
                if Path(target) == self.root/(self.first_model if after else self.second_model) and not interrupted:
                    interrupted.append(True)
                    if after: replace(source, target)
                    raise KeyboardInterrupt('simulated stop')
                return replace(source, target)
            with self.subTest(after_replace=after), patch('tempo_hub.hf_hub_download', return_value=str(self.source)), \
                 patch('tempo_hub.os.replace', side_effect=stop):
                with self.assertRaises(KeyboardInterrupt): self.hub.download_model_release(self.manifest, self.root)
                for name in self.manifest['models']:
                    self.assertEqual((self.root/name).read_bytes(), ('old '+name).encode())
                self.assertFalse(list(self.root.glob('.model-update-*')))

    def assert_dataset_interrupt(self, point):
        output, previous = self.dataset(); replace = os.replace; interrupted = []
        split = self.root/'datasets/training_splits.json'; policy = self.root/'datasets/training_split_policy.json'
        old_split, old_policy = split.read_bytes(), policy.read_bytes()
        def stop(source, target):
            selected = (Path(target) == split if point == 'metadata' else
                        Path(target) == output if point == 'output' else Path(target).name == 'compiled.previous')
            if selected and not interrupted:
                interrupted.append(True)
                if point != 'metadata': replace(source, target)
                raise KeyboardInterrupt('simulated stop')
            return replace(source, target)
        with patch('tempo_training.os.replace', side_effect=stop):
            with self.assertRaises(KeyboardInterrupt): build_dataset(self.root)
        self.assertEqual(self.snapshot(output), previous)
        self.assertEqual(split.read_bytes(), old_split); self.assertEqual(policy.read_bytes(), old_policy)
        self.assertFalse(list((self.root/'datasets').glob('.dataset-install-*')))

    def test_dataset_interrupt_before_metadata_restores_output(self):
        self.assert_dataset_interrupt('metadata')

    def test_dataset_interrupt_after_output_replace_restores_output(self):
        self.assert_dataset_interrupt('output')

    def test_dataset_interrupt_after_prior_output_moves_restores_output(self):
        self.assert_dataset_interrupt('backup')

    def test_interrupted_model_rollback_retains_recovery_backup(self):
        replace = os.replace
        def fail(source, target):
            if Path(source).parent.name == 'downloads' and Path(target).name == self.second_model: raise OSError('install failed')
            if Path(source).name == self.first_model+'.previous': raise KeyboardInterrupt('rollback interrupted')
            return replace(source, target)
        with patch('tempo_hub.hf_hub_download', return_value=str(self.source)), \
             patch('tempo_hub.os.replace', side_effect=fail):
            with self.assertRaises(BaseException) as failure:
                self.hub.download_model_release(self.manifest, self.root)
        self.assertIsInstance(failure.exception, RuntimeError)
        self.assertRegex(str(failure.exception), 'backups.*retained')
        backup = list(self.root.glob('.model-update-*/backups/'+self.first_model+'.previous'))
        self.assertEqual(len(backup), 1); self.assertEqual(backup[0].read_bytes(), ('old '+self.first_model).encode())

    def test_interrupted_dataset_rollback_retains_recovery_backup(self):
        output, previous = self.dataset(); replace = os.replace
        def fail(source, target):
            if Path(source).name == 'training_split_policy.json': raise OSError('install failed')
            if Path(source).name == 'compiled.previous': raise KeyboardInterrupt('rollback interrupted')
            return replace(source, target)
        with patch('tempo_training.os.replace', side_effect=fail):
            with self.assertRaises(BaseException) as failure: build_dataset(self.root)
        self.assertIsInstance(failure.exception, RuntimeError)
        self.assertRegex(str(failure.exception), 'backups.*retained')
        backup = list((self.root/'datasets').glob('.dataset-install-*/compiled.previous'))
        self.assertEqual(len(backup), 1); self.assertEqual(self.snapshot(backup[0]), previous)


if __name__ == '__main__':
    unittest.main()
