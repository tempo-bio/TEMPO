"""Inline correction uses the shared polygon editor and preserves analysis revisions."""
import copy
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import cv2
import numpy as np
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt,QPointF
from PyQt6.QtTest import QTest
from tempo_recording import NativeWriter,sha256
from tempo_backend import process_assay_result
from tempo_analysis_io import reanalyze_outlines
from tempo_quality import decide_dataset

APP=QApplication.instance() or QApplication([])
with patch('platform.machine',return_value='x86_64'):import tempo_gui as gui


class OutlineCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'Assay_A01_FOV001.tif'
        writer=NativeWriter(self.source,100,sensor_bits=12)
        for index in range(12):writer.write(np.full((200,300),1000+index,np.uint16),camera_time_ms=index*10)
        writer.close()
        self.cells=[dict(id='cell_7',class_id=0,polygon=[[20,20],[100,20],[100,90],[20,90]]),
                    dict(id='cell_9',class_id=1,polygon=[[160,100],[240,100],[240,170],[160,170]])]

    def dialog(self):
        view=gui.AutoCellReviewDialog(str(self.source),cells_data=self.cells)
        view.show()
        for _ in range(8):APP.processEvents()
        self.addCleanup(view.close)
        return view

    def draw(self,view,points):
        for point in points:
            QTest.mouseClick(view.view.viewport(),Qt.MouseButton.LeftButton,pos=view.view.mapFromScene(QPointF(*point)))
        QTest.mouseClick(view.view.viewport(),Qt.MouseButton.RightButton,pos=view.view.mapFromScene(QPointF(*points[-1])))
        APP.processEvents()

    def test_add_replace_label_delete_and_undo_preserve_other_cell_identity(self):
        view=self.dialog();self.assertIsInstance(view.view,gui.AnnotationView)
        view.outline_mode.setCurrentIndex(1)
        self.draw(view,[(110,30),(145,30),(145,70),(110,70)])
        self.assertEqual(len(view.get_cells_data()),3)
        self.assertEqual([row['id'] for row in view.get_cells_data()][:2],['cell_7','cell_9'])
        view.table.selectRow(0);view.outline_mode.setCurrentIndex(2)
        replacement=[(25,25),(90,25),(90,85),(25,85)];self.draw(view,replacement)
        np.testing.assert_allclose(view.detected_cells[0]['polygon'],replacement,atol=1)
        self.assertEqual(view.detected_cells[0]['id'],'cell_7')
        view.table.selectRow(0);view.outline_class.setCurrentIndex(3)
        QTest.mouseClick(view.label_outline_button,Qt.MouseButton.LeftButton)
        self.assertEqual(view.get_cells_data()[0]['object_type'],'group')
        self.assertEqual(view.get_cells_data()[0]['class_id'],1)
        view.table.selectRow(1);QTest.mouseClick(view.delete_outline_button,Qt.MouseButton.LeftButton)
        self.assertNotIn('cell_9',[row['id'] for row in view.get_cells_data()])
        view.undo_outline_edit()
        self.assertIn('cell_9',[row['id'] for row in view.get_cells_data()])
        self.assertEqual(self.cells[0]['polygon'],[[20,20],[100,20],[100,90],[20,90]])

    def test_ctrl_click_selects_outline_without_changing_inclusion_or_starting_a_drawing(self):
        view=self.dialog()
        QTest.mouseClick(view.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.ControlModifier,
                         pos=view.view.mapFromScene(QPointF(50,50)))
        self.assertEqual(view.table.currentRow(),0)
        self.assertEqual(len(view.get_cells_data()),2)
        self.assertFalse(view.view.current_polygon_pts)

    def test_new_geometry_analysis_preserves_old_results_and_quality_decisions(self):
        original=process_assay_result(str(self.source),cells_data=self.cells,output_root=self.root/'processed',_native=True)
        self.assertTrue(original.resolved,original.error)
        before=sha256(Path(original.output_dir)/'processing.json')
        source_hash=sha256(self.source)
        decide_dataset(original.results_csv,{Path(original.output_dir).name:dict(included=False,reason='Review required')},'reviewer')
        corrected=copy.deepcopy(self.cells);corrected[0]['polygon']=[[25,25],[90,25],[90,85],[25,85]]
        result=reanalyze_outlines(original,corrected,output_root=self.root/'revisions',actor='reviewer')
        self.assertTrue(result.resolved,result.error)
        self.assertNotEqual(result.output_dir,original.output_dir)
        self.assertEqual(sha256(Path(original.output_dir)/'processing.json'),before)
        self.assertEqual(sha256(self.source),source_hash)
        saved=json.loads((Path(result.output_dir)/'processing.json').read_text())
        self.assertEqual(saved['cells'][0]['id'],'cell_7')
        self.assertEqual(saved['cells'][0]['polygon'],corrected[0]['polygon'])
        quality=json.loads((Path(result.output_dir)/'quality.json').read_text())
        self.assertFalse(quality['decisions'][-1]['included'])
        self.assertEqual(json.loads((Path(result.output_dir)/'revision.json').read_text())['kind'],'outline_correction')

    def test_source_change_is_rejected_before_creating_a_new_analysis(self):
        original=process_assay_result(str(self.source),cells_data=self.cells,output_root=self.root/'processed',_native=True)
        self.source.write_bytes(self.source.read_bytes()+b'changed')
        with self.assertRaises(ValueError):reanalyze_outlines(original,self.cells,output_root=self.root/'revisions',actor='reviewer')
        self.assertFalse((self.root/'revisions').exists())
