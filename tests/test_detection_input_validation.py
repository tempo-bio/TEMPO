"""Malformed detections and cell geometry cannot create misleading processing outputs."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import cv2
import numpy as np
import tempo_backend as backend
from masking_engine import process_cell_masks


class DetectionInputValidationTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root/'A01_FOV001.avi'
        writer = cv2.VideoWriter(str(self.video), cv2.VideoWriter_fourcc(*'MJPG'), 20, (32,32))
        self.assertTrue(writer.isOpened())
        for i in range(5): writer.write(np.full((32,32,3), 40+i, dtype=np.uint8))
        writer.release()
        self.cell = {'id': 'cell_1', 'polygon': [[2,2],[20,2],[20,20],[2,20]]}

    def test_duplicate_cell_ids_reject_before_extraction(self):
        with self.assertRaisesRegex(ValueError,'Duplicate cell'):
            process_cell_masks(str(self.video),str(self.root),[self.cell,copy.deepcopy(self.cell)])

    def test_invalid_cell_ids_and_polygons_do_not_create_outputs(self):
        cases = [dict(self.cell,id='../escaped'), dict(self.cell,id='..\\escaped'),
                 dict(self.cell,id=''), dict(self.cell,polygon=[[1,1],[2,2],[3,3]]),
                 dict(self.cell,polygon=[[1,1],[2,2],[float('nan'),3]]),
                 dict(self.cell,polygon=[[1,1],[2,2]])]
        for cell in cases:
            with self.subTest(cell=cell), patch('masking_engine.cv2.VideoCapture') as capture:
                with self.assertRaises(ValueError):
                    process_cell_masks(str(self.video),str(self.root),[cell],export_debug_avis=True)
                capture.assert_not_called()
        self.assertEqual(list(self.root.glob('masked*')),[])

    def test_valid_distinct_cells_each_receive_one_frame_per_input_frame(self):
        cells = [self.cell,dict(self.cell,id='cell_2')]
        result = process_cell_masks(str(self.video),str(self.root),cells)
        self.assertEqual({name:len(frames) for name,frames in result.cell_frames.items()},
                         {'cell_1':5,'cell_2':5})

    def test_malformed_detection_response_is_failure_not_completed_no_cells(self):
        for packet in ({'success':True}, {'success':True,'boxes':None},
                       {'success':True,'boxes':{}}, {'success':True,'boxes':'bad'}):
            with self.subTest(packet=packet), patch('yolo_inference_helper.detect_cells_via_bridge',return_value=packet):
                with self.assertRaisesRegex(ValueError,'detection response'):
                    backend.run_yolo_inference(str(self.video))

    def test_valid_empty_detection_remains_no_cells(self):
        with patch('yolo_inference_helper.detect_cells_via_bridge',return_value={'success':True,'boxes':[]}):
            self.assertEqual(backend.run_yolo_inference(str(self.video)),[])

    def test_non_boolean_success_cannot_resolve_an_empty_detection(self):
        for value in ('false',1,None):
            with self.subTest(value=value), patch('yolo_inference_helper.detect_cells_via_bridge',
                    return_value={'success':value,'boxes':[]}):
                with self.assertRaises(RuntimeError):
                    backend.run_yolo_inference(str(self.video))

    def test_malformed_detection_keeps_recording_queued(self):
        queue=self.root/'queue.json'; queue.write_text(json.dumps([str(self.video)]))
        with patch.object(backend,'QUEUE_FILE',str(queue)), \
             patch.object(backend,'PROCESSED_DIR',str(self.root/'outputs')), \
             patch.object(backend,'processing_provenance',return_value={}), \
             patch('yolo_inference_helper.detect_cells_via_bridge',return_value={'success':True}):
            result=backend.process_entire_queue()
        self.assertEqual(result['failed'],1)
        self.assertEqual(json.loads(queue.read_text()),[str(self.video)])
        self.assertIn('detection response',result['results'][0].error)

    def test_invalid_supplied_cells_fail_before_output_creation(self):
        with patch.object(backend,'processing_provenance',return_value={}), \
             patch.object(backend,'run_yolo_inference') as detect:
            result = backend.process_assay_result(str(self.video),cells_data={},output_root=str(self.root/'outputs'),_native=True)
        self.assertEqual(result.outcome,'failure')
        self.assertEqual(result.output_dir,'')
        detect.assert_not_called()
        self.assertFalse((self.root/'outputs').exists())


if __name__ == "__main__":
    unittest.main()
