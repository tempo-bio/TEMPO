"""Class retention, independent fine-tuning, bundle compatibility and abstention."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

import cv2
import numpy as np

import tempo_cellpose as adapter
import tempo_training
from yolo_inference_helper import run_inference


class CellposeSpecialistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.healthy = np.zeros((32, 32), np.uint16)
        self.dead = np.zeros_like(self.healthy)
        self.healthy[3:10, 4:11] = 1
        self.dead[18:25, 20:27] = 1

    def bundle(self):
        checkpoints = {}
        for name in adapter.CLASSES:
            path = self.root / (name + '.pt')
            path.write_bytes(name.encode())
            checkpoints[name] = path
        target = self.root / 'cellpose4_cardio.pt'
        adapter.save_specialists(target, checkpoints)
        return target

    def partition(self, name, targets):
        images = self.root / 'cells/images' / name
        labels = self.root / 'cells/labels' / name
        masks = self.root / 'masks' / name
        for path in (images, labels, masks):
            path.mkdir(parents=True, exist_ok=True)
        for index, classes in enumerate(targets):
            mask = np.zeros((32, 32), np.uint16)
            rows = []
            for instance_id, class_id in enumerate(classes, 1):
                foreground = self.healthy if class_id == 0 else self.dead
                mask[foreground > 0] = instance_id
                y, x = np.nonzero(foreground)
                points = [(x.min(), y.min()), (x.max(), y.min()), (x.max(), y.max()), (x.min(), y.max())]
                rows.append(str(class_id) + ' ' + ' '.join(str(coordinate / 31) for point in points for coordinate in point))
            cv2.imwrite(str(images / f'{index}.png'), np.full((32, 32), 100, np.uint8))
            cv2.imwrite(str(masks / f'{index}.tif'), mask)
            (labels / f'{index}.txt').write_text('\n'.join(rows))

    def test_class_labels_preserve_other_class_as_background_and_negative_images(self):
        self.partition('train', [(0, 1), (1,), (0,)])
        images, labels = tempo_training.load_cellpose_partition(self.root, 'train')
        self.assertEqual(len(images), 3)
        np.testing.assert_array_equal(labels['healthy'][0], self.healthy)
        np.testing.assert_array_equal(labels['dead'][0], self.dead)
        self.assertFalse(labels['healthy'][1].any())
        self.assertFalse(labels['dead'][2].any())
        self.assertEqual(labels['dead'][1].max(), 1)

    def test_missing_or_unmapped_class_labels_fail_closed(self):
        file = self.root / 'labels.txt'
        file.write_text('0 0.1 0.1 0.3 0.1 0.3 0.3\n')
        with self.assertRaisesRegex(ValueError, 'no matching class'):
            adapter.specialist_labels([self.healthy + self.dead * 2], [file])
        file.write_text('4\n')
        with self.assertRaisesRegex(ValueError, 'Unknown Cellpose class'):
            adapter.specialist_labels([self.healthy], [file])
        with self.assertRaises(ValueError):
            adapter.specialist_labels([self.healthy], [])

    def test_other_class_overlap_cannot_erase_a_training_cell(self):
        file = self.root / 'overlap.txt'
        polygon = '0.1 0.1 0.3 0.1 0.3 0.3 0.1 0.3'
        file.write_text('0 ' + polygon + '\n1 ' + polygon + '\n')
        # The combined instance mask contains only the later dead label.
        mask = np.zeros((32, 32), np.uint16); mask[3:10, 3:10] = 2
        labels = adapter.specialist_labels([mask], [file])
        self.assertEqual(labels['healthy'][0].max(), 1)
        self.assertEqual(labels['dead'][0].max(), 1)
        np.testing.assert_array_equal(labels['healthy'][0], labels['dead'][0])

    def test_independent_training_retains_negatives_and_saves_a_complete_pair(self):
        self.partition('train', [(0, 1), (1,), (0,)])
        self.partition('val', [(0, 1)])
        networks, calls = [], []
        def factory(**kwargs):
            self.assertEqual(kwargs['pretrained_model'], 'cpdino')
            net = types.SimpleNamespace(state='pretrained', backbone='dino')
            networks.append(net)
            return types.SimpleNamespace(net=net)
        def train(net, **kwargs):
            self.assertEqual(net.state, 'pretrained')
            self.assertEqual(kwargs['min_train_masks'], 0)
            self.assertEqual(len(kwargs['train_data']), 3)
            calls.append(kwargs)
            net.state = 'trained'
            path = Path(kwargs['save_path']) / 'checkpoint'
            path.write_bytes(kwargs['model_name'].encode())
            return str(path), [], []
        fake = types.ModuleType('cellpose')
        fake.models = types.SimpleNamespace(CellposeModel=factory)
        fake.train = types.SimpleNamespace(train_seg=train)
        fake.utils = types.SimpleNamespace(diameters=lambda mask: (7., np.array([7.])))
        with patch.dict('sys.modules', {'cellpose': fake}), patch('tempo_progress.report') as progress:
            path = tempo_training.train_native('cellpose', self.root, self.root / 'output', 3, 2, 'cpu')
        self.assertEqual(len(networks), 2)
        self.assertIsNot(networks[0], networks[1])
        np.testing.assert_array_equal(calls[0]['train_labels'][0], self.healthy)
        np.testing.assert_array_equal(calls[1]['train_labels'][0], self.dead)
        self.assertFalse(calls[0]['train_labels'][1].any())
        self.assertFalse(calls[1]['train_labels'][2].any())
        self.assertEqual(calls[0]['n_epochs'], 3)
        self.assertEqual(calls[1]['n_epochs'], 3)
        with adapter.checkpoint_paths(path) as paths:
            self.assertEqual(set(paths), {'healthy', 'dead'})
            self.assertNotEqual(paths['healthy'].read_bytes(), paths['dead'].read_bytes())
        metadata = json.loads((self.root / 'output/training_details.json').read_text())
        self.assertEqual(metadata['stages']['healthy']['training_cells'], 2)
        self.assertEqual(metadata['stages']['dead']['training_cells'], 2)
        self.assertTrue(any(call.args == ('cellpose', 'training_dead', 3, 6) for call in progress.call_args_list))

    def test_missing_training_class_never_loads_weights_or_publishes_half_a_pair(self):
        self.partition('train', [(1,)])
        self.partition('val', [(0, 1)])
        with patch('cellpose.models.CellposeModel') as factory:
            with self.assertRaisesRegex(ValueError, 'missing healthy'):
                tempo_training.train_native('cellpose', self.root, self.root / 'output', 1, 1, 'cpu')
        factory.assert_not_called()
        self.assertFalse((self.root / 'output/cellpose4_cardio.pt').exists())

    def test_second_stage_failure_preserves_an_existing_bundle(self):
        target = self.bundle()
        previous = target.read_bytes()
        fake = types.ModuleType('cellpose')
        fake.models = types.SimpleNamespace(CellposeModel=lambda **kwargs: types.SimpleNamespace(net=object()))
        attempts = []
        def train(net, **kwargs):
            attempts.append(kwargs['model_name'])
            if len(attempts) == 2:
                raise RuntimeError('dead stage failed')
            path = Path(kwargs['save_path']) / 'checkpoint'
            path.write_bytes(b'healthy stage')
            return str(path)
        fake.train = types.SimpleNamespace(train_seg=train)
        labels = {'healthy': [self.healthy], 'dead': [self.dead]}
        with patch.dict('sys.modules', {'cellpose': fake}), self.assertRaisesRegex(RuntimeError, 'dead stage failed'):
            adapter.train_specialists([self.healthy], labels, [self.healthy], labels, self.root, 1, 1, 'cpu')
        self.assertEqual(target.read_bytes(), previous)

    def test_bundle_extraction_is_scoped_and_legacy_torch_zip_remains_legacy(self):
        target = self.bundle()
        with adapter.checkpoint_paths(target) as paths:
            extracted = paths['healthy']
            self.assertEqual(extracted.read_bytes(), b'healthy')
        self.assertFalse(extracted.exists())
        import torch
        legacy = self.root / 'legacy.pt'
        torch.save({'model': 'legacy fixture'}, legacy)
        with adapter.checkpoint_paths(legacy) as paths:
            self.assertEqual(paths, {'unclassified': legacy})

    def test_damaged_bundle_never_falls_back_to_healthy_or_legacy(self):
        target = self.root / 'damaged.pt'
        for members in ({adapter.MANIFEST: '{}', 'healthy.pt': 'healthy'},
                        {adapter.MANIFEST: '{}', 'healthy.pt': 'healthy', 'dead.pt': 'dead', '../escape.pt': 'bad'},
                        {adapter.MANIFEST: '{"format":"unknown"}', 'healthy.pt': 'healthy', 'dead.pt': 'dead'}):
            with zipfile.ZipFile(target, 'w') as archive:
                for name, data in members.items():
                    archive.writestr(name, data)
            with self.assertRaises(ValueError):
                with adapter.checkpoint_paths(target):
                    pass
        self.assertFalse((self.root.parent / 'escape.pt').exists())

    def test_distinct_predictions_keep_classes_and_identical_predictions_abstain(self):
        instances = adapter.merge_specialists(self.healthy, self.dead)
        self.assertEqual([instance.class_id for instance in instances], [0, 1])
        np.testing.assert_array_equal(adapter.instance_image(instances, self.healthy.shape, 0) > 0, self.healthy > 0)
        merged = adapter.merge_specialists(self.healthy, self.healthy)
        self.assertEqual(len(merged), 1)
        self.assertIsNone(merged[0].class_id)
        self.assertEqual(merged[0].mask.sum(), self.healthy.sum())

    def test_containment_and_one_to_many_conflicts_have_no_confident_duplicate(self):
        healthy = np.zeros((32, 32), np.uint16)
        dead = np.zeros_like(healthy)
        healthy[3:15, 3:20] = 1
        dead[4:8, 4:9] = 1
        dead[10:14, 12:18] = 2
        dead[25:29, 25:29] = 3
        instances = adapter.merge_specialists(healthy, dead)
        self.assertEqual(len(instances), 2)
        self.assertEqual([instance.class_id for instance in instances], [None, 1])
        self.assertEqual(instances[0].mask.sum(), 12 * 17)

    def test_minor_overlap_sparse_ids_and_foreground_only_masks(self):
        healthy = self.healthy * 700
        dead = np.zeros_like(healthy)
        dead[8:15, 9:16] = 500
        self.assertEqual([i.class_id for i in adapter.merge_specialists(healthy, dead)], [0, 1])
        all_foreground = np.ones((3, 3), np.uint16)
        instances = adapter.merge_specialists(all_foreground, np.zeros_like(all_foreground))
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].mask.sum(), 9)
        self.assertEqual(adapter.merge_specialists(np.zeros_like(healthy), np.zeros_like(healthy)), [])

    def test_prediction_loads_one_network_at_a_time_and_preserves_absolute_geometry(self):
        target = self.bundle()
        active, maximum = [0], [0]
        calls = []
        owner = self
        class Model:
            def __init__(self, **kwargs):
                self.name = Path(kwargs['pretrained_model']).read_bytes().decode()
                calls.append(self.name)
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
            def eval(self, image, **kwargs):
                return (owner.healthy if self.name == 'healthy' else owner.dead,)
            def __del__(self):
                active[0] -= 1
        fake = types.ModuleType('cellpose')
        fake.models = types.SimpleNamespace(CellposeModel=Model)
        with patch.dict('sys.modules', {'cellpose': fake}), \
             patch('yolo_inference_helper.resolve_model_path', return_value=str(target)), \
             contextlib.redirect_stdout(io.StringIO()):
            response = run_inference(np.zeros((32, 32), np.uint8), 'cellpose')
        self.assertTrue(response['success'], response)
        self.assertEqual(calls, ['healthy', 'dead'])
        self.assertEqual(maximum[0], 1)
        self.assertEqual([b['class_id'] for b in response['boxes']], [0, 1])
        self.assertTrue(all(not box.get('requires_viability_review') for box in response['boxes']))
        points = np.asarray(response['boxes'][1]['polygon'])
        self.assertEqual(points.min(axis=0).tolist(), [20, 18])
        self.assertEqual(points.max(axis=0).tolist(), [26, 24])

    def test_cpu_prediction_avoids_emulated_bfloat16_for_both_specialists(self):
        import torch
        target = self.bundle()
        owner = self
        for device in ('cpu', torch.device('cpu:0'), 'cuda:1', 'mps'):
            with self.subTest(device=str(device)):
                calls = []
                class Model:
                    def __init__(self, **kwargs):
                        calls.append(kwargs)
                        self.name = Path(kwargs['pretrained_model']).read_bytes().decode()
                    def eval(self, image, **kwargs):
                        return (owner.healthy if self.name == 'healthy' else owner.dead,)
                fake = types.ModuleType('cellpose')
                fake.models = types.SimpleNamespace(CellposeModel=Model)
                with patch.dict('sys.modules', {'cellpose': fake}), \
                     patch.object(adapter, 'release_model_memory'):
                    result = adapter.predict_batch(target, [self.healthy], device)
                self.assertEqual(len(calls), 2)
                for call in calls:
                    self.assertEqual(call['device'], torch.device(device))
                    self.assertEqual(call['use_bfloat16'], torch.device(device).type != 'cpu')
                self.assertEqual([instance.class_id for instance in result[0]], [0, 1])

    def test_benchmark_penalizes_wrong_classes_despite_perfect_outlines(self):
        self.partition('test', [(0, 1)])
        correct = adapter.merge_specialists(self.healthy, self.dead)
        wrong = adapter.merge_specialists(self.dead, self.healthy)
        with patch.object(adapter, 'predict_batch', return_value=[correct]):
            metrics = tempo_training.benchmark('cellpose', 'unused', self.root, 'cpu')
        self.assertEqual(metrics['score'], 1)
        self.assertEqual(metrics['class_scores'], {'healthy': 1, 'dead': 1})
        with patch.object(adapter, 'predict_batch', return_value=[wrong]):
            metrics = tempo_training.benchmark('cellpose', 'unused', self.root, 'cpu')
        self.assertEqual(metrics['score'], 0)
        self.assertEqual(metrics['segmentation_score'], 1)

    def test_inference_conflicts_require_review_instead_of_claiming_healthy(self):
        from tempo_annotations import normalize_record, validate_annotations
        target = self.bundle()
        fake = types.ModuleType('cellpose')
        fake.models = types.SimpleNamespace(CellposeModel=lambda **kwargs: types.SimpleNamespace(
            eval=lambda image, **kwargs: (self.healthy.copy(),)))
        with patch.dict('sys.modules', {'cellpose': fake}), \
             patch('yolo_inference_helper.resolve_model_path', return_value=str(target)), \
             contextlib.redirect_stdout(io.StringIO()):
            response = run_inference(np.zeros((32, 32), np.uint8), 'cellpose')
        self.assertTrue(response['success'], response)
        self.assertEqual(len(response['boxes']), 1)
        self.assertTrue(response['boxes'][0]['requires_viability_review'])
        record = normalize_record({'boxes': response['boxes']}, 32, 32)
        with self.assertRaisesRegex(ValueError, 'classify all unclassified'):
            validate_annotations({'image.png': record})

    def test_legacy_outlines_get_segmentation_credit_but_never_healthy_credit(self):
        self.partition('test', [(0, 1)])
        legacy = adapter.unclassified_instances(self.healthy + self.dead * 2)
        with patch.object(adapter, 'predict_batch', return_value=[legacy]):
            metrics = tempo_training.benchmark('cellpose', 'unused', self.root, 'cpu')
        self.assertEqual(metrics['score'], 0)
        self.assertEqual(metrics['segmentation_score'], 1)
        self.assertEqual(metrics['unclassified_predictions'], 2)

    def test_native_cellpose_preprocessing_and_loss_keep_negative_examples(self):
        import torch
        from cellpose.train import _process_train_test, _loss_fn_seg
        images = [np.zeros((32, 32), np.uint8), np.ones((32, 32), np.uint8)]
        labels = [self.healthy.copy(), np.zeros_like(self.healthy)]
        from cellpose import utils
        original = utils.diameters
        with adapter.negative_image_diameters(labels):
            result = _process_train_test(train_data=images, train_labels=labels,
                                         min_train_masks=0, device=torch.device('cpu'))
        self.assertIs(utils.diameters, original)
        self.assertEqual(len(result[0]), 2)
        self.assertEqual(len(result[1]), 2)
        self.assertFalse(result[1][1].any())
        self.assertTrue(np.isfinite(result[5]).all())
        self.assertGreater(result[5][1], 0)
        prediction = torch.zeros((1, 3, 8, 8), requires_grad=True)
        loss = _loss_fn_seg(torch.zeros_like(prediction), prediction, torch.device('cpu'))
        loss.backward()
        self.assertTrue(torch.isfinite(loss))
        self.assertTrue((prediction.grad[:, -1] > 0).all())

    def test_incomplete_evaluation_cannot_report_success(self):
        self.partition('test', [(0, 1)])
        with patch.object(adapter, 'predict_batch', return_value=[]), self.assertRaisesRegex(ValueError, 'Every Cellpose'):
            tempo_training.benchmark('cellpose', 'unused', self.root, 'cpu')

    def test_negative_diameter_override_is_restored_after_failure(self):
        from cellpose import utils
        original = utils.diameters
        with self.assertRaisesRegex(RuntimeError, 'training stopped'):
            with adapter.negative_image_diameters([self.healthy]):
                diameter, sizes = utils.diameters(np.zeros_like(self.healthy))
                self.assertTrue(np.isfinite(diameter))
                self.assertEqual(len(sizes), 0)
                raise RuntimeError('training stopped')
        self.assertIs(utils.diameters, original)


if __name__ == '__main__':
    unittest.main()
