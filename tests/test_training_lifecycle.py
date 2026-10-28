"""Synthetic dataset and fail-closed training/publishing regression coverage."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import cv2
import numpy as np
import torch
from tempo_training import build_dataset, instance_counts, APPROVAL_POLICY, select_approved_annotations, validate_training_dataset, load_partition
from tempo_training_groups import SPLIT_POLICY


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); (self.root/'datasets/images').mkdir(parents=True)
        self.records={}
        for i in range(12):
            name=f'{i}.png'
            cv2.imwrite(str(self.root/'datasets/images'/name),np.zeros((64,64),np.uint8))
            self.records[name]={'status':'approved','source_well':f'well-{i}','boxes':[
                {'class_id':0,'polygon':[[2,2],[12,2],[12,12],[2,12]]},
                {'class_id':1,'box':[20,20,10,10]},
                {'class_id':2,'polygon':[[40,40],[50,40],[50,50],[40,50]]}]}
        self.save()
    def save(self):
        (self.root/'datasets/universal_annotations.json').write_text(json.dumps(self.records))
    def test_labels_preserve_dead_boxes_exclude_background_and_separate_splits(self):
        dataset=build_dataset(self.root)
        assignments=json.loads((self.root/'datasets/training_splits.json').read_text())
        self.assertEqual(set(assignments.values()),{'train','val','test'})
        for path in (dataset/'detect/labels').glob('*/*.txt'):
            lines=path.read_text().splitlines()
            self.assertEqual([line.split()[0] for line in lines],['0','1'])
            self.assertTrue(all(len(line.split())==5 for line in lines))
        for path in (dataset/'masks').glob('*/*.tif'):
            mask=cv2.imread(str(path),cv2.IMREAD_UNCHANGED)
            self.assertEqual(set(np.unique(mask)),{0,1,2})
            self.assertEqual(mask[45,45],0)
        self.records['0.png']['boxes'][0]['class_id']=1; self.save()
        build_dataset(self.root)
        self.assertEqual(assignments,json.loads((self.root/'datasets/training_splits.json').read_text()))
    def test_unapproved_records_excluded(self):
        self.records['0.png']['status']='submitted'; self.save()
        result=build_dataset(self.root)
        self.assertNotIn('0.png',[r['name'] for r in json.loads((result/'dataset.json').read_text())['records']])
    def test_unclassified_native_outlines_cannot_become_training_labels(self):
        self.records['0.png']['boxes'][0]['requires_viability_review'] = True
        self.save()
        with self.assertRaisesRegex(ValueError, 'classify all unclassified'):
            build_dataset(self.root)
    def test_windows_imread_replacement_cannot_change_image_or_mask_planes(self):
        from ultralytics.utils.patches import imread as windows_imread
        # Exercise the real third-party replacement on every host, including TIFF.
        with patch.object(cv2, 'imread', windows_imread):
            dataset = build_dataset(self.root)
            images, masks = load_partition(dataset, 'train')
        self.assertTrue(images)
        for image, mask in zip(images, masks):
            self.assertEqual(image.shape, (64, 64))
            self.assertEqual(mask.shape, (64, 64))
            self.assertEqual(mask.dtype, np.uint16)
            self.assertEqual(set(np.unique(mask)), {0, 1, 2})
    def test_native_inference_keeps_grayscale_after_windows_yolo_import(self):
        from ultralytics.utils.patches import imread as windows_imread
        from yolo_inference_helper import run_inference
        weights = self.root/'cellpose4_cardio.pt'
        weights.write_bytes(b'fixture, never loaded')
        calls = []
        def evaluate(image, **kwargs):
            calls.append(image)
            self.assertEqual(image.shape, (64, 64))
            return (np.zeros(image.shape, dtype=np.uint16),)
        fake = types.ModuleType('cellpose')
        fake.models = types.SimpleNamespace(CellposeModel=lambda **kwargs: types.SimpleNamespace(eval=evaluate))
        with patch.object(cv2, 'imread', windows_imread), patch.dict('sys.modules', {'cellpose': fake}), \
             patch('yolo_inference_helper.resolve_model_path', return_value=str(weights)):
            result = run_inference(str(self.root/'datasets/images/0.png'), 'cellpose')
        self.assertTrue(result['success'], result)
        self.assertEqual(len(calls), 1)
    def test_missing_status_is_not_approval(self):
        for record in self.records.values():
            record.pop('status')
        with self.assertRaisesRegex(ValueError, 'No explicitly approved'):
            select_approved_annotations(self.records)
    def test_three_approved_images_replace_legacy_splits_without_changing_drafts(self):
        for name, record in self.records.items():
            if name not in ('0.png','1.png','2.png'):
                record.pop('status')
        self.save()
        original = (self.root/'datasets/universal_annotations.json').read_bytes()
        (self.root/'datasets/training_splits.json').write_text(json.dumps({n:'train' for n in self.records}))
        result = build_dataset(self.root)
        info = validate_training_dataset(result)
        self.assertEqual({r['name'] for r in info['records']}, {'0.png','1.png','2.png'})
        self.assertEqual({r['split'] for r in info['records']}, {'train','val','test'})
        self.assertEqual((self.root/'datasets/universal_annotations.json').read_bytes(), original)
        self.assertTrue((self.root/'datasets/training_splits.before-approval-filter.json').exists())
    def test_empty_approved_image_rejected(self):
        self.records['0.png']['boxes'] = []
        with self.assertRaisesRegex(ValueError, 'no cell annotations'):
            select_approved_annotations(self.records)
    def test_old_compiled_dataset_rejected(self):
        (self.root/'dataset.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'predates'):
            validate_training_dataset(self.root)
    def test_missing_image_is_failure(self):
        (self.root/'datasets/images/0.png').unlink()
        with self.assertRaises(ValueError): build_dataset(self.root)
    def test_instance_benchmark_matches_ids_independently(self):
        truth=np.zeros((10,10),np.uint16); truth[1:4,1:4]=1; truth[6:9,6:9]=2
        pred=truth*10
        self.assertEqual(instance_counts(truth,pred),(2,0,0))
        pred[pred==20]=0
        self.assertEqual(instance_counts(truth,pred),(1,0,1))


class InferenceGeometryTests(unittest.TestCase):
    def test_disconnected_mask_islands_never_become_bridges(self):
        from yolo_inference_helper import mask_polygon
        mask = np.zeros((100, 100), np.uint8)
        mask[20:60, 30:70] = 1
        mask[80:90, 80:90] = 1
        mask[2, 2] = 1
        polygon = np.array(mask_polygon(mask))
        self.assertEqual(polygon.min(axis=0).tolist(), [30, 20])
        self.assertEqual(polygon.max(axis=0).tolist(), [69, 59])
        self.assertEqual(len(polygon), 4)

    def test_contour_simplification_retains_area_and_thin_projection(self):
        from yolo_inference_helper import mask_polygon
        mask = np.zeros((240, 240), np.uint8)
        cv2.ellipse(mask, (100, 120), (65, 45), 0, 0, 360, 1, -1)
        mask[117:123, 150:220] = 1
        contour = max(cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0], key=cv2.contourArea)
        polygon = np.array(mask_polygon(mask), np.float32)
        self.assertLess(len(polygon), len(contour))
        self.assertLessEqual(abs(cv2.contourArea(polygon) / cv2.contourArea(contour) - 1), .01)
        self.assertEqual(polygon[:, 0].max(), 219)

    def test_empty_degenerate_and_scaled_masks(self):
        from yolo_inference_helper import mask_polygon
        mask = np.zeros((20, 20), np.uint8)
        self.assertIsNone(mask_polygon(mask))
        mask[5, 2:10] = 1
        self.assertIsNone(mask_polygon(mask))
        mask[2:10, 2:10] = 1
        polygon = np.array(mask_polygon(mask, (40, 60)))
        self.assertEqual(polygon.min(axis=0).tolist(), [6, 4])
        self.assertEqual(polygon.max(axis=0).tolist(), [27, 18])

    def test_yolo_inference_uses_bitmap_not_merged_polygon(self):
        from yolo_inference_helper import run_inference
        mask = torch.zeros((1, 100, 100))
        mask[0, 20:60, 30:70] = 1
        mask[0, 80:90, 80:90] = 1
        result = types.SimpleNamespace(masks=types.SimpleNamespace(data=mask), orig_shape=(100, 100),
            boxes=types.SimpleNamespace(cls=torch.tensor([1]), conf=torch.tensor([.9])))
        with patch('yolo_inference_helper.resolve_model_path', return_value=__file__), \
             patch('ultralytics.YOLO', return_value=lambda *args, **kwargs: [result]):
            response = run_inference('unused.png', 'yolo_seg')
        self.assertTrue(response['success'])
        self.assertEqual(response['boxes'][0]['class_id'], 1)
        self.assertEqual(response['boxes'][0]['box'], [30, 20, 39, 39])


class InstanceMetricTests(unittest.TestCase):
    @staticmethod
    def reference(truth, predicted):
        from scipy.optimize import linear_sum_assignment
        gt_ids = np.unique(truth); gt_ids = gt_ids[gt_ids > 0]
        pred_ids = np.unique(predicted); pred_ids = pred_ids[pred_ids > 0]
        overlap = np.zeros((len(gt_ids), len(pred_ids)))
        for i, a in enumerate(gt_ids):
            for j, b in enumerate(pred_ids):
                left, right = truth == a, predicted == b
                overlap[i, j] = np.count_nonzero(left & right) / max(1, np.count_nonzero(left | right))
        rows, cols = linear_sum_assignment(-(overlap >= .5).astype(float) - overlap * 1e-6)
        count = int(sum(overlap[i, j] >= .5 for i, j in zip(rows, cols)))
        return count, len(pred_ids) - count, len(gt_ids) - count

    def test_matches_previous_metric_on_random_masks_and_sparse_ids(self):
        rng = np.random.default_rng(735)
        ids = np.array([-1, 0, 1, 700, 2**40], dtype=np.int64)
        for trial in range(80):
            truth = rng.choice(ids, size=(18, 23))
            predicted = truth.copy() if trial % 2 else rng.choice(ids, size=truth.shape)
            changes = rng.random(truth.shape) < (trial % 10) / 10
            predicted[changes] = rng.choice(ids, size=np.count_nonzero(changes))
            with self.subTest(trial=trial):
                self.assertEqual(instance_counts(truth, predicted), self.reference(truth, predicted))

    def test_empty_masks_and_exact_half_overlap_preserve_matching_rule(self):
        truth = np.array([[1, 1, 2, 2]])
        examples = [(truth, np.ones_like(truth), (1, 0, 1)),
                    (np.ones_like(truth), truth, (1, 1, 0)),
                    (truth, np.zeros_like(truth), (0, 0, 2)),
                    (np.zeros_like(truth), truth, (0, 2, 0)),
                    (np.zeros_like(truth), np.zeros_like(truth), (0, 0, 0)),
                    (np.array([[1., np.nan]]), np.array([[1., 0.]]), (1, 0, 0))]
        for truth, predicted, expected in examples:
            with self.subTest(expected=expected):
                self.assertEqual(instance_counts(truth, predicted), expected)

    def test_crowded_predictions_on_full_image_do_not_require_per_pair_pixel_scans(self):
        rows, cols = np.indices((1024, 1024))
        truth = (rows // 128) * 8 + cols // 128 + 1
        predicted = (rows // 16) * 64 + cols // 16 + 1
        self.assertEqual(instance_counts(truth, predicted), (0, 4096, 64))

    def test_evaluation_reports_prediction_and_matching_separately(self):
        import tempo_training
        masks = [np.array([[1, 1], [0, 0]]), np.array([[0, 0], [2, 2]])]
        predictions = iter(masks)
        model = types.SimpleNamespace(predict_instances=lambda image: (next(predictions), {}))
        utils = types.ModuleType('csbdeep.utils'); utils.normalize = lambda x, *a: x
        output = io.StringIO()
        with patch('tempo_nuclear_training.profile_images', return_value=(masks, masks)), \
             patch.object(tempo_training, 'unpack_stardist', return_value=model), \
             patch.dict('sys.modules', {'csbdeep.utils': utils}), contextlib.redirect_stdout(output):
            self.assertEqual(tempo_training.benchmark('stardist', 'unused', 'unused', 'cpu'),
                             {'metric':'annotated_nuclear_recall_IoU50','score':1.0,'annotated_nuclei':2,'outside_false_positives':0,'unreviewed_inside_predictions':0})
        messages = output.getvalue()
        for index in (1, 2):
            self.assertLess(messages.index(f'predicting evaluation image {index}/2'),
                            messages.index(f'matching evaluation image {index}/2'))
            self.assertIn(f'image {index}/2 evaluated; 1 matched, 0 missed annotated nuclei', messages)


class SegmentationInitializationTests(unittest.TestCase):
    def test_boundary_loss_keeps_large_half_precision_projection_finite(self):
        import train_yolo
        devices = ['cpu'] + (['cuda'] if torch.cuda.is_available() else [])
        for device in devices:
            with self.subTest(device=device):
                coefficients = torch.full((1, 2), 400., dtype=torch.float16, device=device, requires_grad=True)
                prototypes = torch.full((2, 8, 8), 400., dtype=torch.float16, device=device, requires_grad=True)
                mask = torch.zeros(1, 8, 8, device=device)
                mask[:, 2:6, 2:6] = 1
                bounds = torch.tensor([[0., 0., 8., 8.]], device=device)
                with torch.autocast(device_type=device, dtype=torch.bfloat16):
                    loss = train_yolo.BoundaryAwareSegmentationLoss.single_mask_loss(
                        mask, coefficients, prototypes, bounds, torch.ones(1, device=device))
                self.assertEqual(loss.dtype, torch.float32)
                self.assertTrue(torch.isfinite(loss))
                self.assertGreater(loss.item(), torch.finfo(torch.float16).max)
                loss.backward()
                self.assertTrue(torch.isfinite(coefficients.grad).all())
                self.assertTrue(torch.isfinite(prototypes.grad).all())

    def test_boundary_loss_rejects_invalid_values_before_probability_bce(self):
        import train_yolo
        loss = train_yolo.BoundaryAwareSegmentationLoss.single_mask_loss
        mask = torch.zeros(1, 8, 8)
        coefficients = torch.ones(1, 2)
        prototypes = torch.ones(2, 8, 8)
        bounds = torch.tensor([[0., 0., 8., 8.]])
        with self.assertRaisesRegex(FloatingPointError, 'Non-finite segmentation logits'):
            loss(mask, coefficients * float('nan'), prototypes, bounds, torch.ones(1))
        with self.assertRaisesRegex(ValueError, 'zero-area'):
            loss(mask, coefficients, prototypes, bounds, torch.zeros(1))
        with self.assertRaisesRegex(ValueError, 'masks must contain'):
            loss(mask + 2, coefficients, prototypes, bounds, torch.ones(1))

    def test_retired_segmentation_worker_is_rejected(self):
        import train_yolo
        with patch('tempo_training.validate_training_dataset', return_value={}), patch.object(train_yolo,'YOLO') as constructor:
            with self.assertRaisesRegex(ValueError,'Retired'):
                train_yolo.train_single_model('yolo_seg')
            constructor.assert_not_called()

    def test_training_cannot_use_an_unrelated_cloud_account_or_write_saved_settings(self):
        import os
        import train_yolo
        class SavedSettings(dict):
            def __setitem__(self, key, value):
                raise AssertionError('Training tried to persist third-party settings')
        for fail in (False, True):
            settings = SavedSettings(tensorboard=True, sync=True, api_key='synthetic-account-key')
            original = dict(settings)
            with self.subTest(failure=fail), patch('ultralytics.utils.SETTINGS', settings), \
                 patch.dict(os.environ, {'ULTRALYTICS_API_KEY':'synthetic-environment-key'}):
                try:
                    with train_yolo.local_yolo_settings():
                        self.assertNotIn('ULTRALYTICS_API_KEY', os.environ)
                        self.assertEqual(settings['api_key'], '')
                        self.assertFalse(settings['sync'])
                        self.assertFalse(settings['tensorboard'])
                        if fail:
                            raise RuntimeError('Synthetic training failure')
                except RuntimeError:
                    self.assertTrue(fail)
                self.assertEqual(dict(settings), original)
                self.assertEqual(os.environ['ULTRALYTICS_API_KEY'], 'synthetic-environment-key')

    def test_cached_analytics_sender_stays_disabled_after_training_settings_restore(self):
        import importlib
        import train_yolo
        analytics = importlib.import_module('ultralytics.utils.events')
        sender = analytics.events
        cfg = types.SimpleNamespace(mode='predict', task='detect', model='synthetic.pt')
        with patch.object(sender, 'enabled', True), patch.object(sender, 't', 0), \
             patch.object(sender, 'events', []), patch.object(analytics, 'Thread') as transport:
            with train_yolo.local_yolo_settings():
                sender(cfg)
            sender(cfg)
            transport.assert_not_called()
            self.assertFalse(sender.events)

    def test_segmentation_training_loss_backpropagates_for_both_heads(self):
        import train_yolo
        from ultralytics.cfg import DEFAULT_CFG_DICT

        trainer = object.__new__(train_yolo.BoundaryAwareSegmentationTrainer)
        trainer.data = {'nc': 2, 'channels': 3}
        for cfg in ('yolo11n-seg.yaml', 'yolo26n-seg.yaml'):
            with self.subTest(cfg=cfg):
                model = trainer.get_model(cfg=cfg, verbose=False)
                model.args = types.SimpleNamespace(**DEFAULT_CFG_DICT)
                model.train()
                batch = {
                    'img': torch.rand(2, 3, 64, 64),
                    'batch_idx': torch.tensor([0., 1.]),
                    'cls': torch.tensor([[0.], [1.]]),
                    'bboxes': torch.tensor([[.5, .5, .5, .5], [.5, .5, .5, .5]]),
                    'masks': torch.zeros(2, 16, 16),
                    'sem_masks': torch.zeros(2, 16, 16),
                }
                batch['masks'][:, 4:12, 4:12] = 1
                batch['sem_masks'][1, 4:12, 4:12] = 1
                optimizer = torch.optim.SGD(model.parameters(), lr=1e-3)
                loss, _ = model(batch)
                self.assertTrue(torch.isfinite(loss).all())
                loss.sum().backward()
                gradients = [p.grad for p in model.parameters() if p.grad is not None]
                self.assertTrue(gradients)
                self.assertTrue(all(torch.isfinite(g).all() for g in gradients))
                optimizer.step()

    def test_loss_is_lazy_and_preserves_yolo26_branches(self):
        import copy
        import io
        import train_yolo
        from ultralytics.cfg import DEFAULT_CFG_DICT

        # Construct only: no trainer setup, dataset, training loop, or optimizer.
        trainer = object.__new__(train_yolo.BoundaryAwareSegmentationTrainer)
        trainer.data = {'nc': 2, 'channels': 3}
        model = trainer.get_model(cfg='yolo26n-seg.yaml', verbose=False)
        self.assertFalse(hasattr(model, 'args'))
        self.assertFalse(hasattr(model, 'criterion'))

        model.args = types.SimpleNamespace(**DEFAULT_CFG_DICT)
        criterion = model.init_criterion()
        self.assertIsInstance(criterion, train_yolo.loss_module.E2ELoss)
        self.assertIsInstance(criterion.one2many, train_yolo.BoundaryAwareSegmentationLoss)
        self.assertIsInstance(criterion.one2one, train_yolo.BoundaryAwareSegmentationLoss)

        # EMA copies and checkpoints must not retain a lambda bound to another model.
        copied = copy.deepcopy(model)
        self.assertIs(copied.init_criterion.__self__, copied)
        torch.save(copied, io.BytesIO())

    def test_standard_head_uses_boundary_loss_without_e2e_wrapper(self):
        import train_yolo
        from ultralytics.cfg import DEFAULT_CFG_DICT

        trainer = object.__new__(train_yolo.BoundaryAwareSegmentationTrainer)
        trainer.data = {'nc': 2, 'channels': 3}
        model = trainer.get_model(cfg='yolo11n-seg.yaml', verbose=False)
        model.args = types.SimpleNamespace(**DEFAULT_CFG_DICT)
        self.assertIsInstance(model.init_criterion(), train_yolo.BoundaryAwareSegmentationLoss)


class TrainingFailureTests(unittest.TestCase):
    def test_first_release_ignores_stock_weights_as_champion_before_and_after_download(self):
        import train_yolo
        for stock_exists in (False,True):
            with self.subTest(stock_exists=stock_exists),tempfile.TemporaryDirectory() as directory:
                root=Path(directory); (root/'runs').mkdir()
                (root/'runs/champion_baselines.json').write_text('{}')
                stock=root/'yolo26n-scout.pt'
                if stock_exists: stock.write_bytes(b'generic pretrained weights')
                save_dir=root/'candidate/training'; (save_dir/'weights').mkdir(parents=True)
                (save_dir/'weights/best.pt').write_bytes(b'candidate')
                def load(architecture):
                    stock.write_bytes(b'generic pretrained weights')
                    return types.SimpleNamespace(train=lambda **kwargs:types.SimpleNamespace(save_dir=save_dir))
                with patch.object(train_yolo,'BASE_DIR',directory),patch.object(train_yolo,'YOLO',side_effect=load), \
                     patch.object(train_yolo,'ensure_model_dependencies'),patch('tempo_training.validate_training_dataset',return_value={'validation_partition':'val','evaluation_partition':'test','preprocessing':{'profile':'P0'},'box_width':16}), \
                     patch('tempo_training.export_yolo_for_inference',side_effect=lambda source,destination,**kwargs:source), \
                     patch('tempo_training.benchmark',return_value={'score':0.1}) as benchmark:
                    result=train_yolo.train_single_model('nano_scout',dataset=root/'dataset',output=root/'candidate')
                self.assertIsNone(result['baseline_metrics'])
                self.assertIsNone(result['baseline_sha256'])
                self.assertEqual(benchmark.call_count,1)

    def test_verified_release_is_benchmarked_and_changed_champion_rejects_training(self):
        import train_yolo
        for changed in (False,True):
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as directory:
                root=Path(directory); (root/'runs').mkdir()
                champion=root/'stardist_nuclei.zip'; champion.write_bytes(b'champion')
                digest=hashlib.sha256(champion.read_bytes()).hexdigest()
                (root/'runs/champion_baselines.json').write_text(json.dumps({champion.name:digest}))
                if changed: champion.write_bytes(b'changed')
                artifact=root/'candidate.zip'; artifact.write_bytes(b'candidate')
                with patch.object(train_yolo,'BASE_DIR',directory),patch.object(train_yolo,'ensure_model_dependencies'), \
                     patch('tempo_training.validate_training_dataset',return_value={'validation_partition':'val','evaluation_partition':'test','preprocessing':{'profile':'P0'},'box_width':16}),patch('tempo_training.train_native',return_value=str(artifact)) as train, \
                     patch('tempo_training.benchmark',side_effect=[{'score':0.2},{'score':0.1}]) as benchmark:
                    if changed:
                        with self.assertRaisesRegex(ValueError,'differs from the recorded release'):
                            train_yolo.train_single_model('stardist',dataset=root/'dataset',output=root/'candidate')
                        train.assert_not_called(); benchmark.assert_not_called()
                    else:
                        result=train_yolo.train_single_model('stardist',dataset=root/'dataset',output=root/'candidate')
                        self.assertEqual(result['baseline_sha256'],digest)
                        self.assertEqual(result['baseline_metrics'],{'score':0.1})
                        self.assertEqual(benchmark.call_args.args[1],champion)

    def test_missing_dependency_does_not_auto_install_or_mutate_verified_environment(self):
        import train_yolo
        with patch('importlib.util.find_spec', side_effect=lambda name: None if name=='tensorflow' else object()), \
             patch.object(train_yolo.subprocess,'run') as command:
            with self.assertRaisesRegex(RuntimeError,'Missing training dependencies: tensorflow'):
                train_yolo.ensure_model_dependencies(['stardist'])
        command.assert_not_called()

    def test_segmentation_checkpoint_loads_in_fresh_process_without_training_class(self):
        import subprocess
        import sys
        repository=Path(__file__).resolve().parents[1]
        producer='''
import sys,torch
from train_yolo import BoundaryAwareSegmentationModel
from tempo_training import export_yolo_for_inference
BoundaryAwareSegmentationModel.__module__='__main__'
model=BoundaryAwareSegmentationModel('yolo26n-seg.yaml',nc=2,verbose=False).eval()
# Match a real training checkpoint: the active weights are in EMA.
torch.save({'model':None,'ema':model,'train_args':{'task':'segment'}},sys.argv[1])
export_yolo_for_inference(sys.argv[1],sys.argv[2])
raw=torch.load(sys.argv[1],weights_only=False)['ema']
portable=torch.load(sys.argv[2],weights_only=False)['model']
assert set(raw.state_dict())==set(portable.state_dict())
assert all(torch.equal(value,portable.state_dict()[name]) for name,value in raw.state_dict().items())
def equal(left,right):
    if isinstance(left,torch.Tensor):
        torch.testing.assert_close(left,right,rtol=0,atol=0)
    elif isinstance(left,dict):
        assert left.keys()==right.keys()
        for key in left: equal(left[key],right[key])
    elif isinstance(left,(list,tuple)):
        assert len(left)==len(right)
        for a,b in zip(left,right): equal(a,b)
    else: assert left==right
with torch.no_grad():
    inputs=torch.zeros(1,3,64,64)
    equal(raw(inputs),portable(inputs))
from pathlib import Path
before=Path(sys.argv[2]).read_bytes()
for destination in (sys.argv[1],sys.argv[2]):
    try: export_yolo_for_inference(sys.argv[1],destination)
    except ValueError: pass
    else: raise AssertionError('An existing checkpoint was overwritten')
assert Path(sys.argv[2]).read_bytes()==before
'''
        reader='''
import sys
from ultralytics import YOLO
model=YOLO(sys.argv[1])
assert model.task=='segment'
assert type(model.model).__module__=='ultralytics.nn.tasks'
assert 'train_yolo' not in sys.modules
'''
        with tempfile.TemporaryDirectory() as folder:
            raw,portable=[str(Path(folder)/name) for name in ('raw.pt','portable.pt')]
            saved=subprocess.run([sys.executable,'-c',producer,raw,portable],cwd=repository,
                                 capture_output=True,text=True,timeout=45)
            self.assertEqual(saved.returncode,0,saved.stderr)
            broken=subprocess.run([sys.executable,'-c',reader,raw],cwd=repository,
                                  capture_output=True,text=True,timeout=45)
            self.assertNotEqual(broken.returncode,0)
            self.assertIn('BoundaryAwareSegmentationModel',broken.stderr)
            loaded=subprocess.run([sys.executable,'-c',reader,portable],cwd=repository,
                                  capture_output=True,text=True,timeout=45)
            self.assertEqual(loaded.returncode,0,loaded.stderr)

    def test_fresh_runtime_requests_exact_yolo26_architectures(self):
        import train_yolo
        for kind, architecture in [('nano_scout', 'yolo26n.pt')]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                save_dir = root/'candidate/training'
                (save_dir/'weights').mkdir(parents=True)
                (save_dir/'weights/best.pt').write_bytes(b'checkpoint')
                model = types.SimpleNamespace(train=lambda **kwargs:types.SimpleNamespace(save_dir=save_dir))
                with patch.object(train_yolo, 'YOLO', return_value=model) as constructor, \
                     patch.object(train_yolo, 'BASE_DIR', directory), \
                     patch.object(train_yolo.os.path, 'exists', return_value=False), \
                    patch.object(train_yolo, 'ensure_model_dependencies'), \
                    patch('tempo_training.export_yolo_for_inference', side_effect=lambda source,destination,**kwargs:source), \
                    patch('tempo_training.validate_training_dataset',return_value={'validation_partition':'val','evaluation_partition':'test','preprocessing':{'profile':'P0'},'box_width':16}), \
                     patch('tempo_training.benchmark', return_value={'score':0.1}):
                    train_yolo.train_single_model(kind, dataset=root/'dataset', output=root/'candidate')
                constructor.assert_called_once_with(architecture)

    def test_failed_workers_propagate_and_no_champions_are_deployed(self):
        import train_yolo
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); dataset=root/'datasets/compiled'; dataset.mkdir(parents=True)
            (dataset/'dataset.json').write_text(json.dumps({'test_fingerprint':'mock','approval_policy':APPROVAL_POLICY, 'split_policy':SPLIT_POLICY}))
            with patch.object(train_yolo,'BASE_DIR',directory), patch.object(train_yolo,'ensure_yolo_dataset',return_value=dataset), patch.object(train_yolo.subprocess,'run',return_value=types.SimpleNamespace(returncode=1)), patch.object(train_yolo.torch.cuda,'is_available',return_value=False):
                self.assertFalse(train_yolo.train_parallel_pipeline(epochs=1))
            report=json.loads((root/'runs/training_report.json').read_text())
            self.assertFalse(report['success']); self.assertEqual(len(report['errors']),1)
            self.assertFalse(list(root.glob('*.pt')))



class NativeAdapterTests(unittest.TestCase):
    def test_cellpose_uses_dino_and_native_train_api(self):
        import sys
        import tempo_training
        calls={}
        def factory(**kwargs):
            calls['model']=kwargs
            return types.SimpleNamespace(net='native-cellpose-network')
        def train(net, **kwargs):
            calls['net']=net; calls['train']=kwargs
            path=Path(kwargs['save_path'])/'weights'
            path.write_bytes(b'cellpose')
            return str(path),[],[]
        fake=types.ModuleType('cellpose'); fake.models=types.SimpleNamespace(CellposeModel=factory); fake.train=types.SimpleNamespace(train_seg=train)
        labels = np.zeros((64, 64), dtype=np.uint16); labels[10:20, 10:20] = 1
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules,{'cellpose':fake}), patch.object(tempo_training,'load_cellpose_partition',return_value=([np.zeros((64,64))], {'healthy':[labels], 'dead':[labels]})):
            result=tempo_training.train_native('cellpose','unused',folder,1,2,'cpu')
            self.assertTrue(Path(result).is_file())
        self.assertEqual(calls['model']['pretrained_model'],'cpdino')
        self.assertEqual(calls['net'],'native-cellpose-network')
        self.assertEqual(calls['train']['n_epochs'],1)
        self.assertEqual(calls['train']['min_train_masks'],0)
        self.assertNotIn('bsize',calls['train'])

    def test_cellpose_sam_fallback_uses_library_tile_size(self):
        import sys
        import tempo_training
        attempted = []
        def factory(**kwargs):
            attempted.append(kwargs['pretrained_model'])
            if kwargs['pretrained_model'] == 'cpdino':
                raise ImportError('DINO extension unavailable')
            return types.SimpleNamespace(net=types.SimpleNamespace(backbone='sam_vitl'))
        def train(net, bsize=256, **kwargs):
            self.assertEqual(net.backbone, 'sam_vitl')
            self.assertEqual(bsize, 256)
            path = Path(kwargs['save_path'])/'checkpoint'
            path.write_bytes(b'SAM checkpoint')
            return str(path), [], []
        fake = types.ModuleType('cellpose')
        fake.models = types.SimpleNamespace(CellposeModel=factory)
        fake.train = types.SimpleNamespace(train_seg=train)
        labels = np.zeros((64, 64), dtype=np.uint16); labels[10:20, 10:20] = 1
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, {'cellpose':fake}), \
             patch.object(tempo_training, 'load_cellpose_partition', return_value=([np.zeros((64,64))], {'healthy':[labels], 'dead':[labels]})):
            result = tempo_training.train_native('cellpose', 'unused', folder, 1, 1, 'cpu')
            self.assertTrue(Path(result).is_file())
            details = json.loads((Path(folder)/'training_details.json').read_text())
            for stage in details['stages'].values():
                self.assertEqual(stage['pretrained_model'], 'cpsam')
                self.assertEqual(stage['backbone'], 'sam_vitl')
                self.assertEqual(stage['fallback']['error_type'], 'ImportError')
        self.assertEqual(attempted, ['cpdino', 'cpsam', 'cpdino', 'cpsam'])
    def test_stardist_uses_native_partial_nuclear_training_and_bundle(self):
        import sys
        import tempo_training
        calls=[]
        class Model:
            def __init__(self,config,name,basedir):
                self.path=Path(basedir)/name;self.path.mkdir();(self.path/'config.json').write_text('{}')
                self.callbacks=[]
                self.assert_partial=None
            def prepare_for_training(self):pass
            def train(self,images,labels,**kwargs):
                calls.append(labels[0].copy())
                (self.path/'weights_best.h5').write_bytes(b'native')
            def load_weights(self,name):calls.append(name)
        star=types.ModuleType('stardist.models');star.Config2D=lambda **kw:kw;star.StarDist2D=Model
        labels=np.full((64,64),-1,dtype=int);labels[:5,:5]=0;labels[10:20,10:20]=1
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);dataset=root/'dataset';dataset.mkdir()
            (dataset/'dataset.json').write_text(json.dumps(dict(preprocessing={'profile':'P0'},assisted=False)))
            with patch.dict(sys.modules,{'stardist.models':star}),patch.object(tempo_training,'configure_tensorflow'), \
                 patch('tempo_nuclear_training.profile_images',return_value=([np.zeros((64,64))],[labels])):
                result=tempo_training.train_native('stardist',dataset,root/'output',1,1,'cpu')
            import zipfile
            with zipfile.ZipFile(result) as archive:
                self.assertIn('model/weights_best.h5',archive.namelist())
                self.assertIn('model/tempo_nuclei.json',archive.namelist())
        np.testing.assert_array_equal(calls[0],labels)
        self.assertIn('weights_best.h5',calls[1:])


class PublicationTests(unittest.TestCase):
    def test_diagnostic_worker_uses_training_images_and_marks_candidate_unpublishable(self):
        import train_yolo
        import tempo_training
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); dataset=root/'dataset'; dataset.mkdir()
            (dataset/'dataset.json').write_text(json.dumps({'approval_policy':APPROVAL_POLICY,'format':'tempo-training-v3',
                'split_policy':SPLIT_POLICY,'purpose':'training-check'}))
            artifact=root/'candidate.zip';artifact.write_bytes(b'check candidate')
            with patch.object(train_yolo,'BASE_DIR',str(root)), patch.object(train_yolo,'ensure_model_dependencies'), \
                 patch.object(tempo_training,'train_native',return_value=str(artifact)) as train, \
                 patch.object(tempo_training,'benchmark',return_value={'metric':'instance_F1_IoU50','score':.5}) as evaluate:
                result=train_yolo.train_single_model('stardist',dataset=dataset,output=root/'check',epochs=1,training_check=True)
            self.assertEqual(train.call_args.kwargs['validation_split'],'train')
            self.assertEqual(evaluate.call_args.kwargs['split'],'train')
            self.assertFalse(result['publishable']);self.assertFalse(result['promote'])
            self.assertEqual(result['evaluation_partition'],'train')

    def test_diagnostic_candidates_cannot_publish_even_with_a_full_report_header(self):
        from tempo_training import MODEL_FILES, publish_champions
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'runs').mkdir()
            report=dict(success=True, origin='approved-master', approval_policy=APPROVAL_POLICY,
                        split_policy=SPLIT_POLICY, results={name:{'publishable':False} for name in MODEL_FILES})
            (root/'runs/training_report.json').write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError,'Training-check candidates'):
                publish_champions(root,'fixture/models','fake')

    def test_first_release_uses_remote_baseline_not_stock_yolo_hash(self):
        import hashlib
        from tempo_training import MODEL_FILES, publish_champions
        import huggingface_hub
        from huggingface_hub.errors import EntryNotFoundError
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'runs').mkdir(); (root/'datasets').mkdir()
            (root/'datasets/training_splits.json').write_text('{}')
            (root/'datasets/training_split_policy.json').write_text(json.dumps({'source_groups':{}}))
            results={}
            for name,filename in MODEL_FILES.items():
                artifact=root/filename; artifact.write_bytes(name.encode())
                results[name]={'artifact':str(artifact),'sha256':hashlib.sha256(name.encode()).hexdigest(),'baseline_sha256':'stock-yolo-hash','metrics':{'score':.8},'promote':True}
            report={'success':True,'origin':'approved-master','run_id':'run','results':results,'remote_baselines':{},'test_fingerprint':'heldout','approval_policy':APPROVAL_POLICY, 'split_policy':SPLIT_POLICY}
            (root/'runs/training_report.json').write_text(json.dumps(report))
            commits=[]
            api=types.SimpleNamespace(repo_info=lambda **kw:types.SimpleNamespace(sha='parent'),create_commit=lambda **kw:commits.append(kw))
            with patch.object(huggingface_hub,'HfApi',return_value=api),patch.object(huggingface_hub,'hf_hub_download',side_effect=EntryNotFoundError('missing')):
                self.assertEqual(publish_champions(root,'org/models','mock'),3)
            self.assertEqual(commits[0]['parent_commit'],'parent')
            self.assertEqual(len(commits[0]['operations']),9)
    def test_local_experiment_cannot_publish_as_approved_master(self):
        from tempo_training import publish_champions
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'runs').mkdir()
            (root/'runs/training_report.json').write_text(json.dumps({'success':True,'origin':'local','results':{}}))
            with self.assertRaises(ValueError): publish_champions(root,'org/models','mock')


if __name__=='__main__': unittest.main()
