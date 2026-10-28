"""Real scout model and trainer checks; CPU only, no downloaded weights or remote writes."""
import contextlib
import io
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np
import torch
from ultralytics import YOLO
from ultralytics.cfg import DEFAULT_CFG_DICT
from ultralytics.utils import SETTINGS
from tempo_scout_training import CentroidDetectionTrainer, ScoutModel
from tempo_training_runtime import check_scout_training


class ScoutTrainingTests(unittest.TestCase):
    def setUp(self):
        from tempo_training_runtime import disable_ultralytics_analytics
        disable_ultralytics_analytics()
        settings_folder = tempfile.TemporaryDirectory(); self.addCleanup(settings_folder.cleanup)
        settings_file = patch.object(SETTINGS, 'file_path', Path(settings_folder.name)/'settings.json')
        settings_file.start(); self.addCleanup(settings_file.stop)
        # These fixtures fit local weights; package-version queries are unrelated.
        updates = patch('ultralytics.utils.checks.check_pip_update_available', return_value=False)
        updates.start(); self.addCleanup(updates.stop)
        previous = torch.get_num_threads(); torch.set_num_threads(2)
        self.addCleanup(torch.set_num_threads, previous)

    def test_actual_yolo26_forward_backward_reaches_both_detection_branches(self):
        result = check_scout_training()
        self.assertEqual(result['criterion'], 'E2ELoss')
        self.assertEqual(result['scoring'], 'center_class_F1_radius12')
        self.assertTrue(np.isfinite(result['loss']).all())

    def test_scout_scores_populated_predictions_and_partial_group_annotations(self):
        from tempo_evaluation_metrics import benchmark_scout
        def box(cid, center, width=2):
            x,y = center
            return SimpleNamespace(cls=torch.tensor(cid),xyxy=torch.tensor([[x-width/2,y-width/2,x+width/2,y+width/2]]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for subdir in ('images','unknown','labels'):(root/'detect'/subdir/'val').mkdir(parents=True)
            records = []
            for name, centers, groups in (
                    ('a_group', [[10,10],[60,60]], [[[.5,.5],[.9,.5],[.9,.9],[.5,.9]]]),
                    ('b_countable', [[10,10],[20,20],[30,30]], [])):
                cv2.imwrite(str(root/'detect/images/val'/(name+'.png')),np.zeros((100,100),np.uint8))
                (root/'detect/unknown/val'/(name+'.json')).write_text(json.dumps(dict(
                    centers=[dict(class_id=0,center=center) for center in centers],groups=groups)))
                (root/'detect/labels/val'/(name+'.txt')).write_text('2 .7 .7 .4 .4\n' if groups else '')
                records.append(dict(stem=name,name=name+'.png',preparation=name))
            (root/'dataset.json').write_text(json.dumps(dict(records=records)))
            responses = [SimpleNamespace(boxes=[box(0,[10,10]),box(0,[60,60]),box(0,[70,70]),box(2,[70,70],40)]),
                         SimpleNamespace(boxes=[box(0,[10,10]),box(0,[20,20]),box(0,[30,30]),box(1,[90,90])])]
            model = SimpleNamespace(model=SimpleNamespace(),predict=lambda *args,**kwargs:[responses.pop(0)])
            with patch('ultralytics.YOLO',return_value=model):
                scores = benchmark_scout('fixture.pt',root,'val','cpu')
            self.assertEqual(scores['matches'],dict(tp=5,fp=1,fn=0))
            self.assertAlmostEqual(scores['score'],10/11)
            self.assertEqual(scores['group_recall'],1.)
            self.assertEqual(scores['field_sensitivity'],1.)
            self.assertEqual(scores['count_mae'],1.)
            self.assertIsNone(scores['per_image'][0]['count_error'])
            self.assertEqual(scores['per_image'][1]['count_error'],1)

    def test_unknown_groups_suppress_only_negative_cell_labels_in_both_real_branches(self):
        model = ScoutModel('yolo26n.yaml', nc=4, verbose=False)
        model.args = SimpleNamespace(**DEFAULT_CFG_DICT)
        batch = dict(img=torch.rand(2,3,64,64), batch_idx=torch.tensor([0,1]),
            cls=torch.tensor([[0.],[2.]]), bboxes=torch.tensor([[.5,.5,.3,.3],[.5,.5,.5,.5]]),
            unknown_cells=torch.ones(2,64,64,dtype=torch.bool))
        predictions = model(batch['img'])
        for branch in ('one2many', 'one2one'):
            predictions[branch]['scores'].retain_grad()
        loss, _ = model.loss(batch, predictions)
        loss.sum().backward()
        for branch in ('one2many', 'one2one'):
            gradient = predictions[branch]['scores'].grad
            self.assertTrue(torch.isfinite(gradient).all())
            self.assertEqual(torch.count_nonzero(gradient[:,1]).item(), 0)
            self.assertGreater(torch.count_nonzero(gradient[:,0]).item(), 0)
            self.assertGreater(torch.count_nonzero(gradient[:,2:]).item(), 0)
        # The per-call ignore region must not leak into later fully supervised batches.
        batch.pop('unknown_cells')
        predictions = model(batch['img'])
        for branch in ('one2many', 'one2one'):
            predictions[branch]['scores'].retain_grad()
        loss, _ = model.loss(batch, predictions)
        loss.sum().backward()
        for branch in ('one2many', 'one2one'):
            self.assertGreater(torch.count_nonzero(predictions[branch]['scores'].grad[:,1]).item(), 0)

    def test_one_epoch_trainer_validation_export_and_reload(self):
        from tempo_training import export_yolo_for_inference
        from tempo_evaluation_metrics import benchmark_scout
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            for split, count in (('train', 2), ('val', 1)):
                for subdir in ('detect/images', 'detect/labels', 'detect/unknown', 'raw/images'):
                    (root/subdir/split).mkdir(parents=True)
                for index in range(count):
                    name = str(index)
                    image = np.random.default_rng(index).integers(0, 255, (64, 64, 3), dtype=np.uint8)
                    cv2.imwrite(str(root/'detect/images'/split/(name+'.png')), image)
                    cv2.imwrite(str(root/'raw/images'/split/(name+'.png')), image)
                    (root/'detect/labels'/split/(name+'.txt')).write_text('0 .3 .3 .1 .1\n2 .7 .7 .4 .4\n')
                    (root/'detect/unknown'/split/(name+'.json')).write_text(json.dumps(dict(shape=[64, 64],
                        centers=[dict(class_id=0,center=[19.2,19.2])],
                        groups=[[[.5,.5],[.9,.5],[.9,.9],[.5,.9]]])))
            config = root/'data.yaml'
            config.write_text('path: '+(root/'detect').as_posix()+'\ntrain: images/train\nval: images/val\nnames: [healthy, dead, healthy_group, dead_group]\n')
            from tempo_preprocessing import metadata
            preprocessing = metadata('P0')
            (root/'dataset.json').write_text(json.dumps(dict(records=[dict(stem='0',name='field.png',preparation='field-0')],
                preprocessing=preprocessing,box_width=16)))
            with patch.dict(SETTINGS, {key:False for key in SETTINGS if key in
                    ('tensorboard','wandb','mlflow','clearml','comet','dvc','neptune','raytune','hub')}), \
                 patch('ultralytics.data.utils.check_font'):
                model = YOLO('yolo26n.yaml')
                model.train(trainer=CentroidDetectionTrainer, data=str(config), epochs=1,
                    batch=2, imgsz=64, device='cpu', workers=0, amp=False, plots=False,
                    pretrained=False, project=str(root/'run'), name='scout', exist_ok=True,
                    verbose=False, deterministic=True)
                source = Path(model.trainer.save_dir)/'weights/best.pt'
                self.assertTrue(source.is_file())
                artifact = export_yolo_for_inference(source, root/'scout.pt', metadata=dict(
                    role='scout',format='centroid-v1',preprocessing=preprocessing,box_width=16))
                loaded = YOLO(artifact)
                result = loaded.predict(np.zeros((64,64,3),np.uint8), imgsz=64, device='cpu', verbose=False)
                self.assertEqual(len(result), 1)
                self.assertTrue(source.is_file())
                scores = benchmark_scout(artifact, root, 'val', 'cpu')
                self.assertTrue(np.isfinite(scores['score']))
                self.assertEqual(scores['metric'], 'center_class_F1_radius12')
                self.assertEqual(len(scores['per_image']), 1)
                self.assertGreaterEqual(scores['per_image'][0]['latency_ms'], 0)
                self.assertEqual(set(scores['radius_scores']), {'6','12','18'})
                json.dumps(scores, allow_nan=False)

    def test_tempo_worker_runs_from_approved_annotations_through_saved_scoring_result(self):
        import train_yolo
        from tempo_training import build_dataset, MODEL_FILES
        from matplotlib import font_manager
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory); images = root/'datasets/images'; images.mkdir(parents=True)
            records = {}
            assignments = dict(field0='train', field1='train', field2='val', field3='test')
            for index in range(4):
                name = f'field{index}.png'
                image = np.random.default_rng(index).integers(0,255,(96,96),dtype=np.uint8)
                cv2.imwrite(str(images/name),image)
                records[name] = dict(status='approved',width=96,height=96,coordinate_space='normalized',
                    image_id='sha256:'+hashlib.sha256((images/name).read_bytes()).hexdigest(),boxes=[
                        dict(class_id=0,polygon=[[.1,.1],[.3,.1],[.3,.3],[.1,.3]]),
                        dict(class_id=1,polygon=[[.4,.1],[.6,.1],[.6,.3],[.4,.3]]),
                        dict(class_id=0,object_type='group',polygon=[[.5,.5],[.9,.5],[.9,.9],[.5,.9]])])
            (root/'datasets/universal_annotations.json').write_text(json.dumps(records))
            (root/'datasets/architecture_validation.json').write_text(json.dumps(dict(
                profile='P0',scout_profile='gaussian1-clahe2',nuclear_assistance_passed=False)))
            dataset = build_dataset(root,origin='experiment',assisted=False,
                source_groups={name:name for name in records},
                split_assignments={name:assignments[Path(name).stem] for name in records})
            # The fixture supplies local random starting weights for the same architecture.
            # Fitting, export, TEMPO scoring and result serialization all run unmocked.
            stock = YOLO('yolo26n.yaml')
            torch.save(dict(model=stock.model,train_args={}),root/'yolo26n.pt')
            font = font_manager.findfont('DejaVu Sans')
            with patch.object(train_yolo,'BASE_DIR',str(root)), \
                 patch('ultralytics.data.utils.check_font',return_value=font), \
                 patch('ultralytics.utils.plotting.check_font',return_value=font), \
                 patch.dict(SETTINGS,{key:False for key in SETTINGS if key in
                     ('tensorboard','wandb','mlflow','clearml','comet','dvc','neptune','raytune','hub')}):
                result = train_yolo.train_single_model('nano_scout',dataset=dataset,
                    output=root/'candidate',epochs=1,batch=2,workers=0,device='cpu')
            self.assertEqual(json.loads((root/'candidate/result.json').read_text()), result)
            self.assertEqual(Path(result['artifact']).name, MODEL_FILES['nano_scout'])
            self.assertEqual(hashlib.sha256(Path(result['artifact']).read_bytes()).hexdigest(),result['sha256'])
            self.assertFalse(result['publishable'])
            self.assertEqual(result['scout_metadata']['preprocessing']['profile'],'gaussian1-clahe2')
            self.assertEqual(result['evaluation_partition'],'test')
            self.assertEqual(len(result['metrics']['per_image']),1)
            self.assertTrue(np.isfinite(result['metrics']['score']))
            self.assertGreaterEqual(result['metrics']['per_image'][0]['latency_ms'],0)
            json.dumps(result, allow_nan=False)
            from tempo_training_runtime import resume_scout_candidate
            info = json.loads((dataset/'dataset.json').read_text())
            info=dict(info,preprocessing=info['scout_preprocessing'])
            original_hash = result['sha256']
            (root/'candidate/result.json').unlink()
            with patch.object(train_yolo,'YOLO',side_effect=AssertionError('Completed fitting repeated')), \
                 patch('tempo_training.export_yolo_for_inference',side_effect=AssertionError('Completed export repeated')):
                with patch('tempo_training.benchmark',side_effect=RuntimeError('Injected scoring interruption')):
                    with self.assertRaisesRegex(RuntimeError,'scoring interruption'):
                        train_yolo.train_single_model('nano_scout',dataset=dataset,output=root/'candidate',epochs=1,batch=2,workers=0,device='cpu')
                recovered = train_yolo.train_single_model('nano_scout',dataset=dataset,
                    output=root/'candidate',epochs=1,batch=2,workers=0,device='cpu')
            self.assertEqual(recovered['sha256'],original_hash)
            self.assertEqual(recovered['metrics']['matches'],result['metrics']['matches'])
            # The same native final export also covers recovery of pre-receipt engines.
            (root/'candidate/trained_candidate.json').unlink()
            adopted = resume_scout_candidate(root/'candidate',dataset,info,1,2,None,allow_legacy=True)
            self.assertEqual(adopted,result['artifact'])
            self.assertTrue((root/'candidate/trained_candidate.json').is_file())
            self.assertEqual(json.loads((root/'candidate/trained_candidate.json').read_text())['provenance']['kind'],'legacy-final-export')
            changed = dataset/'changed-input.txt'; changed.write_text('changed inputs')
            with self.assertRaisesRegex(ValueError,'training inputs'):
                resume_scout_candidate(root/'candidate',dataset,info,1,2,None)
            self.assertEqual(hashlib.sha256(Path(result['artifact']).read_bytes()).hexdigest(),original_hash)


if __name__ == '__main__':
    unittest.main()
