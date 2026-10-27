"""Compilation failures must retain the prior dataset and holdout assignments."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from tempo_training import build_dataset


class TrainingDatasetTransactionTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); images = self.root/'datasets/images'; images.mkdir(parents=True)
        self.records = {}
        for number in range(3):
            name = f'{number}.png'
            self.assertTrue(cv2.imwrite(str(images/name), np.zeros((32,32), np.uint8)))
            self.records[name] = {'status': 'approved', 'source_well':f'well-{number}', 'boxes': [
                {'class_id': 0, 'polygon': [[2,2], [20,2], [20,20], [2,20]]}]}
        self.source = self.root/'datasets/universal_annotations.json'; self.save()
        self.output = build_dataset(self.root)
        self.previous = self.snapshot()
        self.splits = self.root/'datasets/training_splits.json'
        self.policy = self.root/'datasets/training_split_policy.json'
        self.old_splits, self.old_policy = self.splits.read_bytes(), self.policy.read_bytes()
        self.records['0.png']['boxes'][0]['class_id'] = 1; self.save()

    def save(self):
        self.source.write_text(json.dumps(self.records))

    def snapshot(self):
        return {str(p.relative_to(self.output)): p.read_bytes() for p in self.output.rglob('*') if p.is_file()}

    def assert_preserved(self):
        self.assertTrue(self.output.is_dir())
        self.assertEqual(self.snapshot(), self.previous)
        self.assertEqual(self.splits.read_bytes(), self.old_splits)
        self.assertEqual(self.policy.read_bytes(), self.old_policy)

    def test_dataset_publication_failure_retains_prior_dataset(self):
        rename, replace = Path.rename, os.replace
        failed = []
        def intercept(source, target, action):
            if Path(target) == self.output and not failed:
                failed.append(True); raise OSError('dataset publication failed')
            return action(source, target)
        with patch.object(Path, 'rename', lambda s,t: intercept(s,t,rename)), \
             patch('tempo_training.os.replace', lambda s,t: intercept(s,t,replace)):
            with self.assertRaises(OSError): build_dataset(self.root)
        self.assert_preserved()

    def test_partial_split_write_or_publication_failure_restores_dataset_and_splits(self):
        write, replace = Path.write_text, os.replace
        failed = []
        def write_failure(path, text, *args, **kwargs):
            if path == self.splits:
                write(path, '{partial'); raise OSError('split write failed')
            return write(path, text, *args, **kwargs)
        def replace_failure(source, target):
            if Path(target) == self.splits and not failed:
                failed.append(True); raise OSError('split publication failed')
            return replace(source, target)
        with patch.object(Path, 'write_text', write_failure), \
             patch('tempo_training.os.replace', replace_failure):
            with self.assertRaises(OSError): build_dataset(self.root)
        self.assert_preserved()

    def test_image_encoding_failure_cannot_publish_an_incomplete_dataset(self):
        encode = cv2.imwrite
        def fail_train_image(path, image):
            return False if '/images/train/' in str(path).replace('\\', '/') else encode(path, image)
        with patch('tempo_training.cv2.imwrite', side_effect=fail_train_image):
            with self.assertRaises(OSError): build_dataset(self.root)
        self.assert_preserved()

    def test_last_metadata_publication_failure_restores_every_installed_target(self):
        replace = os.replace; failed = []
        def fail_policy(source, target):
            if Path(target) == self.policy and not failed:
                failed.append(True); raise OSError('policy publication failed')
            return replace(source, target)
        with patch('tempo_training.os.replace', side_effect=fail_policy):
            with self.assertRaises(OSError): build_dataset(self.root)
        self.assert_preserved()

    def test_failed_rollback_retains_recoverable_prior_dataset(self):
        replace = os.replace
        def fail_output(source, target):
            if Path(target) == self.output: raise OSError('output directory in use')
            return replace(source, target)
        with patch('tempo_training.os.replace', side_effect=fail_output):
            with self.assertRaisesRegex(RuntimeError, 'backups.*retained'):
                build_dataset(self.root)
        backups = list((self.root/'datasets').glob('.dataset-install-*/compiled.previous'))
        self.assertEqual(len(backups), 1)
        self.assertEqual({str(p.relative_to(backups[0])): p.read_bytes()
                          for p in backups[0].rglob('*') if p.is_file()}, self.previous)

    def test_invalid_geometry_leaves_prior_dataset_and_draft_intact(self):
        self.records['0.png']['boxes'][0]['polygon'][1][0] = float('nan'); self.save()
        original = self.source.read_bytes()
        with self.assertRaises(ValueError): build_dataset(self.root)
        self.assert_preserved(); self.assertEqual(self.source.read_bytes(), original)

    def test_successful_rebuild_preserves_holdouts_and_removes_temporary_backups(self):
        result = build_dataset(self.root)
        self.assertEqual(result, self.output)
        self.assertNotEqual(self.snapshot(), self.previous)
        self.assertEqual(self.splits.read_bytes(), self.old_splits)
        self.assertFalse(list((self.root/'datasets').glob('compiled-*')))
        self.assertFalse(list((self.root/'datasets').glob('.dataset-install-*')))


if __name__ == '__main__':
    unittest.main()
