"""Reviewed group identity survives training, processing and separate summaries."""
import copy
import json
import os
from pathlib import Path
import tempfile
import types
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import cv2
import numpy as np
import pandas as pd
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication
from tempo_annotations import normalize_record, pixel_record, validate_annotations
from tempo_regions import model_class, annotation_class, region_counts, scout_qualifies
from tempo_analysis import read_results, summarize_cells
import tempo_cellpose as cellpose
import tempo_training as training

APP = QApplication.instance() or QApplication([])


class GroupMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def record(self):
        boxes = []
        for cid in range(4):
            x = .05 + cid * .24
            boxes.append(dict(annotation_class(cid), polygon=[[x,.1],[x+.15,.1],[x+.15,.7],[x,.7]]))
        return dict(status='approved', width=64, height=64, coordinate_space='normalized', boxes=boxes)

    def dataset(self):
        folder = self.root/'datasets/universal_images'; folder.mkdir(parents=True)
        records = {f'field-{i}.png': self.record() for i in range(3)}
        for name in records:
            cv2.imwrite(str(folder/name), np.full((64,64), 90, np.uint8))
        (folder/'universal_annotations.json').write_text(json.dumps(records))
        return training.build_dataset(self.root)

    def test_group_metadata_roundtrip_preserves_viability_and_rejects_nuclear_groups(self):
        record = self.record()
        pixels = pixel_record(record)
        restored = normalize_record(pixels, 64, 64)
        self.assertEqual(restored['boxes'], record['boxes'])
        self.assertEqual([model_class(box) for box in restored['boxes']], [0,1,2,3])
        validate_annotations({'image.png': restored})
        self.assertIsNone(model_class(dict(class_id=2,object_type='nucleus')))
        restored['boxes'][0]['class_id'] = 2
        restored['boxes'][0]['object_type'] = 'group'
        with self.assertRaisesRegex(ValueError, 'nucleus'):
            validate_annotations({'image.png': restored})

    def test_review_group_toggle_is_undoable_without_changing_healthy_dead_or_outline(self):
        from annotation_review_dialog import ReviewCanvas
        image = QImage(64,64,QImage.Format.Format_RGB32); image.fill(0)
        path = self.root/'image.png'; image.save(str(path))
        canvas = ReviewCanvas(); self.addCleanup(canvas.close)
        canvas.set_image_and_masks(str(path), self.record())
        before = copy.deepcopy(canvas.boxes)
        canvas.polygons[0].setSelected(True)
        canvas.set_group(True)
        self.assertEqual(canvas.boxes[0], dict(before[0], object_type='group'))
        canvas.reclassify(1)
        self.assertEqual(canvas.boxes[0]['object_type'], 'group')
        canvas.reclassify(2)
        self.assertEqual(canvas.boxes[0]['class_id'], 1)
        self.assertEqual(canvas.boxes[0]['object_type'], 'group')
        canvas.undo()
        self.assertEqual(canvas.boxes[0]['class_id'], 0)
        self.assertEqual(canvas.boxes[0]['object_type'], 'group')

    def test_compilation_keeps_group_outlines_as_distinct_model_targets(self):
        dataset = self.dataset()
        files = list((dataset/'cells/labels').rglob('*.txt'))
        self.assertEqual(len(files), 3)
        for path in files:
            self.assertEqual([int(line.split()[0]) for line in path.read_text().splitlines()], [0,1,2,3])
            split = path.parent.name
            masks = training.read_image_plane(dataset/'masks'/split/(path.stem+'.tif'), cv2.IMREAD_UNCHANGED)
            self.assertEqual(set(np.unique(masks)), {0,1,2,3,4})
            specialists = cellpose.specialist_labels([masks], [path])
            for name in ('healthy','dead','healthy_group','dead_group'):
                self.assertTrue(specialists[name][0].any(), name)
            self.assertFalse(np.any(specialists['healthy'][0] & specialists['healthy_group'][0]))

    def test_group_specialist_bundle_preserves_classes_and_rejects_missing_weight(self):
        paths = {}
        for name in cellpose.MODEL_CLASSES:
            path = self.root/(name+'.pt'); path.write_bytes(name.encode()); paths[name] = path
        target = self.root/'cellpose4_cardio.pt'
        cellpose.save_specialists(target, paths)
        with cellpose.checkpoint_paths(target) as recovered:
            self.assertEqual(set(recovered), set(paths))
            self.assertEqual(recovered['healthy_group'].read_bytes(), b'healthy_group')
        paths['healthy_group'].unlink()
        before = target.read_bytes()
        with self.assertRaises(ValueError):
            cellpose.save_specialists(target, paths)
        self.assertEqual(target.read_bytes(), before)

    def test_specialist_disagreement_about_cell_vs_group_does_not_produce_two_objects(self):
        left = np.zeros((32,32),np.uint16); left[3:15,4:20] = 1
        right = left.copy()
        instances = cellpose.merge_predictions({0:left,2:right})
        self.assertEqual(len(instances), 1)
        self.assertIsNone(instances[0].class_id)

    def test_cellpose_trains_four_categories_and_scores_group_classes_in_comparisons(self):
        import torch  # Load before patch.dict restores the fake worker modules.
        dataset = self.dataset()
        _, masks = training.load_cellpose_partition(dataset, 'train')
        fake = types.ModuleType('cellpose')
        trained = []
        def factory(**kwargs):
            name = Path(kwargs['pretrained_model']).stem
            return types.SimpleNamespace(net=object(), eval=lambda image,**kw:(masks[name][0],))
        def train(net, **kwargs):
            name = kwargs['model_name'].removeprefix('cellpose_')
            trained.append(name)
            self.assertTrue(kwargs['train_labels'][0].any())
            path = Path(kwargs['save_path'])/'weights'; path.write_bytes(name.encode())
            return str(path)
        fake.models = types.SimpleNamespace(CellposeModel=factory)
        fake.train = types.SimpleNamespace(train_seg=train)
        fake.utils = types.SimpleNamespace(diameters=lambda mask:(7.,np.array([7.])))
        with patch.dict(sys.modules,{'cellpose':fake}), patch.object(cellpose,'release_model_memory'):
            tx,ty=training.load_cellpose_partition(dataset,'train')
            vx,vy=training.load_cellpose_partition(dataset,'val')
            target=cellpose.train_specialists(tx,ty,vx,vy,self.root/'trained',1,1,'cpu')
            score = training.benchmark('cellpose',target,dataset,'cpu')
            legacy = self.root/'legacy.pt'
            with cellpose.checkpoint_paths(target) as paths:
                cellpose.save_specialists(legacy,{name:paths[name] for name in ('healthy','dead')})
            baseline = training.benchmark('cellpose',legacy,dataset,'cpu')
        self.assertEqual(trained,list(cellpose.MODEL_CLASSES))
        self.assertEqual(score['score'],1)
        self.assertEqual(score['class_scores']['healthy_group'],1)
        self.assertEqual(score['metric'],baseline['metric'])
        self.assertLess(baseline['score'],score['score'])

    def test_false_group_prediction_does_not_change_the_comparison_objective(self):
        image = np.zeros((32,32),np.uint8)
        mask = np.zeros((32,32),np.uint16); mask[3:15,4:20] = 1
        labels = {name:[mask.copy() if name == 'healthy' else np.zeros_like(mask)] for name in cellpose.MODEL_CLASSES}
        baseline = cellpose.merge_predictions({0:mask})
        candidate = baseline + cellpose.merge_predictions({2:mask})
        with patch.object(training,'load_cellpose_partition',return_value=([image],labels)):
            with patch.object(cellpose,'predict_batch',return_value=[baseline]):
                old = training.benchmark('cellpose','fixture',self.root,'cpu')
            with patch.object(cellpose,'predict_batch',return_value=[candidate]):
                new = training.benchmark('cellpose','fixture',self.root,'cpu')
        self.assertEqual(old['metric'],new['metric'])
        self.assertEqual(old['score'],1)
        self.assertLess(new['score'],old['score'])

    def test_nuclear_training_labels_do_not_reuse_cell_or_group_instances(self):
        dataset=self.dataset()
        _,cells=training.load_partition(dataset,'train')
        _,nuclei=training.load_nuclear_partition(dataset,'train')
        self.assertGreater(cells[0].max(),0)
        self.assertFalse(np.any(nuclei[0]>0))
        self.assertTrue(np.any(nuclei[0]<0))

    def test_stardist_predictions_have_nuclear_kind_without_viability(self):
        import yolo_inference_helper as helper
        import zipfile
        mask=np.zeros((32,32),np.uint16);mask[3:15,4:20]=1
        path=self.root/'stardist_nuclei.zip'
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr('model/tempo_nuclei.json',json.dumps({'preprocessing':{'profile':'P0'}}))
        model=types.SimpleNamespace(predict_instances=lambda image:(mask,{}))
        with patch.object(helper,'resolve_model_path',return_value=str(path)),patch.object(training,'configure_tensorflow'),patch.object(training,'unpack_stardist',return_value=model):
            result=helper.run_inference(np.zeros((32,32),np.uint8))
        self.assertTrue(result['success'],result)
        self.assertEqual(result['result_kind'],'nuclei')
        self.assertEqual(result['boxes'][0]['object_type'],'nucleus')
        self.assertNotIn('requires_viability_review',result['boxes'][0])

    def test_healthy_group_qualifies_scout_without_inventing_cell_count(self):
        counts = region_counts([annotation_class(2), annotation_class(3), annotation_class(1)])
        self.assertEqual(counts, dict(live_count=0,dead_count=1,healthy_groups=1,dead_groups=1))
        self.assertTrue(scout_qualifies(counts, 3))
        self.assertFalse(scout_qualifies(region_counts([annotation_class(3)]), 3))
        self.assertTrue(scout_qualifies(region_counts([annotation_class(0)]*3), 3))

    def test_unattended_detection_preserves_group_kind_in_processing_input(self):
        import tempo_backend
        box = dict(class_id=0, object_type='group', polygon=[[1,1],[20,1],[20,20],[1,20]])
        with patch('yolo_inference_helper.detect_cells_via_bridge', return_value=dict(success=True,boxes=[box])):
            cells = tempo_backend.run_yolo_inference('synthetic.avi')
        self.assertEqual(cells[0]['object_type'], 'group')
        self.assertEqual(cells[0]['id'], 'group_001')

    def results(self):
        path = self.root/'results.csv'
        pd.DataFrame([dict(Well_ID='A01',FOV_ID='FOV001',Recording_ID='recording',Cell_ID='cell_001',Object_Type='cell',Peak_Amplitude_au=10),
                      dict(Well_ID='A01',FOV_ID='FOV001',Recording_ID='recording',Cell_ID='group_001',Object_Type='group',Peak_Amplitude_au=100)]).to_csv(path,index=False)
        return path

    def test_analysis_never_merges_a_cell_and_group_with_the_same_legacy_identifier(self):
        data = read_results(self.results())
        data['Cell_ID'] = 'shared-id'
        means, _ = summarize_cells(data)
        self.assertEqual(len(means), 2)
        self.assertEqual(dict(zip(means.Object_Type,means.Peak_Amplitude_au)), {'cell':10,'group':100})
        data.loc[0,'Object_Type'] = 'clump'
        data.to_csv(self.root/'invalid.csv',index=False)
        with self.assertRaisesRegex(ValueError, 'Object_Type'):
            read_results(self.root/'invalid.csv')

    def test_ui_uses_separate_lists_and_averages_and_retains_legacy_cells(self):
        with patch('platform.machine', return_value='x86_64'):
            import tempo_gui as gui
        from tempo_tasks import dispose_closed_views
        view = gui.AnalyzerTab()
        self.addCleanup(lambda: dispose_closed_views(view)); self.addCleanup(view.close)
        view.load_csv(str(self.results()))
        self.assertEqual(len(view.current_well_data), 1)
        self.assertEqual(view.current_well_data.Object_Type.iloc[0], 'cell')
        self.assertIn('(1 cell)', view.lbl_target.text())
        self.assertEqual(view.lbl_amp.text(), '10.00')
        view.measurement_selector.setCurrentIndex(1)
        self.assertEqual(len(view.current_well_data), 1)
        self.assertIn('(1 group)', view.lbl_target.text())
        self.assertEqual(view.lbl_amp.text(), '100.00')
        self.assertIn('Group 001', view.cell_selector.itemText(1))
        view.cell_selector.setCurrentIndex(1)
        self.assertEqual(view.lbl_target.text(), 'Group 001')
        legacy = pd.read_csv(self.results()).iloc[:1].drop(columns='Object_Type')
        legacy.to_csv(self.root/'legacy.csv',index=False)
        view.load_csv(str(self.root/'legacy.csv'))
        self.assertEqual(view.current_well_data.Object_Type.iloc[0], 'cell')
        self.assertEqual(view.measurement_selector.currentData(), 'cell')

    def test_processing_writes_group_identity_into_real_synthetic_video_results(self):
        import tempo_backend
        path = self.root/'A01_FOV001.avi'
        writer = cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'MJPG'),20,(48,48))
        self.assertTrue(writer.isOpened())
        for index in range(40):
            writer.write(np.full((48,48,3),int(90+40*np.sin(index/3)),np.uint8))
        writer.release()
        cells = [dict(id='group_001',class_id=0,object_type='group',polygon=[[5,5],[40,5],[40,40],[5,40]])]
        with patch.object(tempo_backend,'PROCESSED_DIR',str(self.root/'processed')):
            result = tempo_backend.process_assay_result(str(path),cells)
        self.assertIn(result.outcome, ('success','no_beats'), result.error)
        data = read_results(result.results_csv)
        self.assertEqual(data.Object_Type.tolist(), ['group'])
        self.assertEqual(data.Cell_ID.tolist(), ['group_001'])
        self.assertTrue((Path(result.output_dir)/data.Trace_File.iloc[0]).is_file())
