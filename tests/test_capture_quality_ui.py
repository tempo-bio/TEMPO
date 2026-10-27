"""Offscreen decision edits use separate reasons and retain imported data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
import pandas as pd
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication,QLabel
from tempo_quality_ui import QualityReviewDialog
from tempo_quality import dataset_decisions
from tempo_analysis import read_results

APP=QApplication.instance() or QApplication([])


class QualityReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'results.csv'
        pd.DataFrame([dict(Recording_ID=name,Well_ID='A01',FOV_ID=str(i),Cell_ID='1',
                          Quality_Flags='focus_override',Quality_Included=True)
                      for i,name in enumerate(('one','two'))]).to_csv(self.path,index=False)
        self.original=self.path.read_bytes()

    def test_each_row_saves_its_own_reason_and_beta_disclaimer_is_visible(self):
        dialog=QualityReviewDialog(self.path,read_results(self.path));dialog.show();APP.processEvents()
        self.addCleanup(dialog.close)
        self.assertTrue(any('in beta' in label.text() and label.isVisible() for label in dialog.findChildren(QLabel)))
        for i,reason in enumerate(('Poor intracellular detail','Keep for sensitivity analysis')):
            checkbox=dialog.table.cellWidget(i,2)
            if i==0:QTest.mouseClick(checkbox,Qt.MouseButton.LeftButton)
            edit=dialog.table.cellWidget(i,3)
            QTest.mouseClick(edit,Qt.MouseButton.LeftButton);QTest.keyClicks(edit,reason)
        QTest.mouseClick(dialog.apply_button,Qt.MouseButton.LeftButton);APP.processEvents()
        records=dataset_decisions(self.path)['recordings']
        self.assertEqual(records['one']['decisions'][-1]['reason'],'Poor intracellular detail')
        self.assertFalse(records['one']['decisions'][-1]['included'])
        self.assertEqual(records['two']['decisions'][-1]['reason'],'Keep for sensitivity analysis')
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_cancel_does_not_save_choices(self):
        dialog=QualityReviewDialog(self.path,read_results(self.path))
        dialog.table.cellWidget(0,2).setChecked(False);dialog.reject()
        self.assertFalse(Path(str(self.path)+'.quality.json').exists())

    def test_changed_dataset_keeps_unsaved_edits_visible(self):
        dialog=QualityReviewDialog(self.path,read_results(self.path));dialog.show();APP.processEvents()
        self.addCleanup(dialog.close)
        dialog.table.cellWidget(0,3).setText('Changed')
        self.path.write_text(self.path.read_text()+'\n')
        # An existing decision binds subsequent writes to the reviewed source.
        from tempo_quality import decide_dataset
        self.path.write_bytes(self.original)
        decide_dataset(self.path,{'one':dict(included=True,reason='First')},'reviewer')
        self.path.write_text(self.path.read_text()+'\n')
        dialog.save()
        self.assertEqual(dialog.table.cellWidget(0,3).text(),'Changed')
        self.assertTrue(dialog.isVisible())
