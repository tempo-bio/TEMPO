"""Isolated, populated GUI checks for Model Evaluation; no native TEMPO launch."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from PyQt6.QtCore import Qt,QPoint,QRect
from PyQt6.QtGui import QImage,QFont
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication,QWidget
from tempo_ui import install_ui,InformationButton,help_controller
from tempo_tasks import dispose_closed_views
from model_evaluation import ModelEvaluationPage

APP=QApplication.instance() or QApplication([])
install_ui(APP)


def payload(phase='ready',count=30):
    descriptor=dict(id='custom-model-study',version='1',name='Custom model study',stages=[dict(id='calibrate',label='Calibration'),dict(id='measure',label='Measurement')],
        parameters=[dict(name='repeats',label='Repeats',type='integer',default=3,minimum=1,maximum=10)])
    plan=dict(id='a'*32,name='Saved comparison',gpu='A100',protocol=descriptor,parameters={'repeats':3},scope='Protocol-defined independent units',
        partitions=dict(train=count-4,val=2,test=2),manifest=dict(images={f'field-{i}.png':dict(split='train' if i<count-4 else 'val' if i<count-2 else 'test',unit=f'unit-{i}') for i in range(count)}))
    return dict(plan=plan,state=dict(phase=phase,updated_at=time.time()),reports=dict(calibrate={},measure={}),revision='pinned',intent={})


def completed_payload():
    data=payload('completed')
    data['reports']=dict(calibrate=dict(summary='Calibration completed.',tables=[]),measure=dict(summary='Custom comparison completed.',decision=dict(passed=True),
        tables=[dict(title='Latency',columns=['Condition','Milliseconds'],rows=[['Reference',12.],['Candidate',9.]]),
                dict(title='Agreement',columns=['Measurement','Value'],rows=[['Accuracy',.94],['Recall',.92]])],
        previews=[dict(name='field-28.png',views={'Reference':'results/reference.png','Candidate':'results/candidate.png'})]))
    return data


class ModelEvaluationUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.addCleanup(patch.stopall)
        self.parent=QWidget();self.addCleanup(self.parent.close)
        self.parent.run_write=lambda action,done:done(action(),None)
        self.view=ModelEvaluationPage(self.parent)
        self.view.setWindowFlags(Qt.WindowType.Window)
        self.addCleanup(dispose_closed_views,self.view)
        self.addCleanup(self.view.close)
        self.view.set_allowed(True);self.view.timer.stop()
        patch('model_evaluation.run_background',side_effect=lambda action,done:done(action(),None)).start()

    def test_declared_stages_and_tables_have_actionable_controls(self):
        self.view.show_payload(payload())
        self.assertEqual(self.view.primary.text(),'Run Stage')
        self.assertEqual(self.view.images.rowCount(),30)
        self.assertTrue(self.view.images.item(0,2).text().startswith('unit-'))
        complete=completed_payload();self.view.show_payload(complete)
        self.assertEqual(self.view.primary.text(),'Record Decision')
        self.assertEqual(len(self.view.result_tables),2)
        self.assertEqual(self.view.result_tables[0].item(1,1).text(),'9.000')
        self.assertEqual(self.view.preview_image.count(),1)

    def test_publication_is_separate_from_recording_a_decision(self):
        data=completed_payload()
        data['model_proposal']=dict(format='tempo-evaluated-model-release-v1',models={'cellpose':{'artifact':'models/cells.pt'}})
        self.view.show_payload(data);self.view.show();APP.processEvents()
        self.assertFalse(self.view.publish.isVisible())
        data['state']['adoption_recorded']=True;self.view.show_payload(data);APP.processEvents()
        self.assertEqual(self.view.primary.text(),'Decision Recorded');self.assertFalse(self.view.primary.isEnabled())
        self.assertTrue(self.view.publish.isVisible());self.assertTrue(self.view.publish.isEnabled())
        data['deployment']={'model_revision':'saved-release'};self.view.show_payload(data)
        self.assertEqual(self.view.publish.text(),'Models Published');self.assertFalse(self.view.publish.isEnabled())

    def test_small_nonzero_effects_are_not_displayed_as_zero(self):
        data=completed_payload();data['reports']['measure']['tables']=[dict(title='Effects',columns=['Low','High'],rows=[[.00016332681655260864,-.00016332681655260864]])]
        self.view.show_payload(data)
        table=self.view.result_tables[0]
        self.assertEqual(table.item(0,0).text(),'0.000163');self.assertEqual(table.item(0,1).text(),'-0.000163')

    def test_publication_preparation_has_no_short_read_timeout_and_requires_review(self):
        data=completed_payload();data['state']['adoption_recorded']=True
        data['model_proposal']={'models':{'cellpose':{'artifact':'models/cells.pt'}}}
        self.view.show_payload(data)
        prepared={'proposal':'fixture'}
        with patch('model_evaluation.adoption.prepare_adoption',return_value=prepared) as prepare, \
             patch('model_evaluation.adoption.adoption_summary',return_value='Publish evaluated models?'), \
             patch('model_evaluation.confirm_action',return_value=False), \
             patch('model_evaluation.adoption.publish_adoption') as publish, \
             patch.object(self.view,'read',side_effect=AssertionError('Large downloads cannot use the 30-second reader')):
            self.view.publish_models()
        prepare.assert_called_once();publish.assert_not_called();self.assertFalse(self.view._busy)

    def test_queue_uses_frozen_plan_without_manual_assignment_or_notebook_edits(self):
        self.view.show_payload(payload())
        with patch('model_evaluation.service.queue_evaluation',return_value={}) as queue,patch.object(self.view,'load_selected'):
            self.view.primary.click()
        queue.assert_called_once()
        self.assertEqual(queue.call_args.args[1:],('a'*32,'calibrate'))
        self.assertFalse(hasattr(self.view,'assign_role'))

    def test_reviewed_report_does_not_choose_execution_and_notebook_waits_for_queue(self):
        data=payload();data['reports']={'calibrate':dict(summary='Calibration completed.',tables=[])}
        self.view.show_payload(data)
        self.assertEqual(self.view.report_stage.currentData(),'calibrate')
        self.assertEqual(self.view.status.text(),'Ready · Measurement')
        callbacks=[]
        self.parent.run_write=lambda action,done:callbacks.append((action,done))
        queued=copy.deepcopy(data);queued['state'].update(phase='awaiting_colab',stage='measure')
        queued['intent']={'evaluation_id':data['plan']['id']}
        with patch('model_evaluation.service.queue_evaluation',return_value={}) as queue, \
             patch.object(self.view,'load_selected',side_effect=lambda:self.view.show_payload(queued)), \
             patch.object(self.view,'open_notebook') as opened:
            self.view.primary.click()
            self.assertFalse(self.view.primary.isEnabled());opened.assert_not_called()
            action,done=callbacks.pop();done(action(),None)
            queue.assert_called_once()
            self.assertEqual(queue.call_args.args[1:],('a'*32,'measure'))
            self.assertEqual(self.view.status.text(),'Awaiting Colab · Measurement')
            self.assertEqual(self.view.primary.text(),'Open Notebook')
            self.view.primary.click();opened.assert_called_once()

    def test_instructions_work_with_only_the_maintained_packaged_guides(self):
        import maintainer_tab
        import shutil
        docs=self.root/'packaged/docs';docs.mkdir(parents=True)
        for name in ('MODEL_EVALUATION_WORKFLOW.md','TRAINING_WORKFLOW.md'):
            shutil.copy2(Path(__file__).resolve().parents[1]/'docs'/name,docs/name)
        opened=[]
        def inspect(dialog):
            opened.append(dialog.reader.toPlainText())
        with patch.object(maintainer_tab,'get_base_dir',return_value=str(docs.parent)), \
             patch.object(maintainer_tab.TrainingWorkflowDialog,'exec',inspect):
            self.view.instructions()
        self.assertEqual(len(opened),1)
        self.assertIn('Run stages in Colab',opened[0])
        self.assertIn('Interruptions and recovery',opened[0])
        self.assertIn('Review results',opened[0])
        self.assertNotIn('Preparation contract',opened[0])
        self.assertNotIn('ignored files',opened[0])
        self.assertNotIn('tempo-evaluation-protocol-v1',opened[0])
        with self.assertRaises(ValueError):
            maintainer_tab.TrainingWorkflowDialog(source_name='MODEL_EVALUATION.md')

    def test_user_selection_updates_visible_settings_and_stage_preview(self):
        from tests.evaluation_fixtures import protocol
        import json
        path=protocol(self.root/'conditional');descriptor=json.loads(path.read_text())
        descriptor['parameters']=[dict(name='test',label='Test',type='choice',choices=['speed','quality'],default='speed'),
            dict(name='batch',label='Batch size',type='integer',default=4,visible_when={'test':'speed'}),
            dict(name='threshold',label='Threshold',type='number',default=.5,visible_when={'test':'quality'})]
        descriptor['stages']=[dict(id='speed',label='Speed measurement',when={'test':'speed'}),dict(id='quality',label='Quality measurement',when={'test':'quality'})]
        path.write_text(json.dumps(descriptor));self.view.load_protocol_file(path)
        self.view.resize(1100,750);self.view.show();APP.processEvents()
        self.assertFalse(self.view.parameter_widgets['batch'][1].isHidden());self.assertTrue(self.view.parameter_widgets['threshold'][1].isHidden())
        self.assertEqual(self.view.name.mapTo(self.view,QPoint()).x(),self.view.parameter_widgets['batch'][1].mapTo(self.view,QPoint()).x())
        self.assertIn('Speed measurement',self.view.summary.text())
        combo=self.view.parameter_widgets['test'][1];combo.setFocus();QTest.keyClick(combo,Qt.Key.Key_Down);APP.processEvents()
        self.assertTrue(self.view.parameter_widgets['batch'][1].isHidden());self.assertFalse(self.view.parameter_widgets['threshold'][1].isHidden())
        self.assertIn('Quality measurement',self.view.summary.text());self.assertNotIn('Speed measurement',self.view.summary.text())

    def test_common_run_summary_and_details_survive_different_scientific_results(self):
        from PyQt6.QtWidgets import QDialog,QTextBrowser
        import json
        data=completed_payload();data['reports']['measure']['tables']=[dict(title='Memory',columns=['Condition','MiB'],rows=[['Candidate',128.]])]
        data['reports']['measure']['execution']=dict(elapsed_seconds=45.2,gpu='Fixture GPU',attempt_id='fixture-attempt')
        self.view.show_payload(data)
        self.assertIn('45.2 s',self.view.result_run_summary.text());self.assertIn('Fixture GPU',self.view.result_run_summary.text())
        self.assertEqual(self.view.result_tables[0].horizontalHeaderItem(1).text(),'MiB')
        opened=[]
        with patch.object(QDialog,'exec',lambda dialog:opened.append(json.loads(dialog.findChild(QTextBrowser).toPlainText()))):
            self.view.run_details.click()
        self.assertEqual(opened[0]['protocol_version'],'1');self.assertEqual(opened[0]['execution']['attempt_id'],'fixture-attempt')

    def test_running_failed_and_stale_states_preserve_resume_and_cancel_actions(self):
        for phase in ('running','failed','cancelled','disconnected','awaiting_colab'):
            data=payload(phase)
            if phase=='running':data['state']['updated_at']=time.time()-180
            self.view.show_payload(data)
            self.assertEqual(self.view.cancel.isEnabled(),phase in ('running','awaiting_colab'))
            self.assertEqual(not self.view.recover.isHidden(),phase=='running')
            if phase in ('failed','cancelled','disconnected'):self.assertEqual(self.view.primary.text(),'Resume Stage')

    def test_instructions_use_a_button_and_dialog_instead_of_an_information_hover(self):
        self.view.resize(1100,800);self.view.show();APP.processEvents()
        self.assertIsNone(self.view.findChild(InformationButton))
        import maintainer_tab
        with patch.object(maintainer_tab.TrainingWorkflowDialog,'exec') as opened:
            QTest.mouseMove(self.view.instructions_button,self.view.instructions_button.rect().center());QTest.qWait(200)
            self.assertIsNone(help_controller().bubble);opened.assert_not_called()
            QTest.mouseClick(self.view.instructions_button,Qt.MouseButton.LeftButton)
            opened.assert_called_once()

    def test_operator_instructions_wrap_and_scroll_at_large_fonts(self):
        import maintainer_tab
        original=APP.font();self.addCleanup(APP.setFont,original);APP.setFont(QFont('Arial',24))
        dialog=maintainer_tab.TrainingWorkflowDialog(source_name='MODEL_EVALUATION_WORKFLOW.md',title='Model Evaluation')
        self.addCleanup(dispose_closed_views,dialog);self.addCleanup(dialog.close)
        dialog.resize(640,460);dialog.show()
        for _ in range(8):APP.processEvents()
        self.assertEqual(dialog.reader.horizontalScrollBar().maximum(),0)
        self.assertGreater(dialog.reader.verticalScrollBar().maximum(),0)

    def test_predictions_are_checksum_loaded_and_zoom_is_synchronized(self):
        self.view.show_payload(completed_payload())
        image=QImage(320,240,QImage.Format.Format_RGB32);image.fill(Qt.GlobalColor.gray)
        path=self.root/'preview.png';image.save(str(path))
        self.view.resize(1200,800);self.view.show()
        with patch('model_evaluation.service.load_evidence',return_value=[str(path),str(path)]) as load:
            self.view.tabs.setCurrentIndex(2);APP.processEvents()
        self.assertTrue(load.called)
        self.view.left.scale(1.25,1.25);self.view.left.moved.emit()
        self.assertEqual(self.view.left.transform(),self.view.right.transform())
        self.assertFalse(self.view.left.scene().itemsBoundingRect().isEmpty())

    def test_account_change_discards_late_results(self):
        callbacks=[]
        with patch('model_evaluation.run_background',side_effect=lambda action,done:callbacks.append(done)):
            self.view.read(lambda:completed_payload(),self.view.show_payload)
        self.view.set_allowed(False)
        callbacks[0](completed_payload(),None)
        self.assertIsNone(self.view.payload)
        self.assertFalse(self.view.isEnabled())

    def test_populated_empty_active_and_completed_geometry(self):
        output=os.environ.get('TEMPO_EVALUATION_SCREENS')
        if output:Path(output).mkdir(parents=True,exist_ok=True)
        image=QImage(320,240,QImage.Format.Format_RGB32);image.fill(Qt.GlobalColor.gray)
        from PyQt6.QtGui import QPainter,QColor,QPen
        painter=QPainter(image);painter.setPen(QPen(QColor('#28dc28'),2));painter.drawRect(20,30,180,80)
        painter.setPen(QPen(QColor('#9b51e0'),2))
        for x in (50,100,150):painter.drawEllipse(x,55,15,20)
        painter.setPen(QPen(QColor('#f03c3c'),2));painter.drawRect(230,130,50,70);painter.end()
        path=self.root/'preview.png';image.save(str(path))
        with patch('model_evaluation.service.load_evidence',return_value=[str(path),str(path)]):
            for state in ('empty','ready','running','completed'):
                data=None if state=='empty' else completed_payload() if state=='completed' else payload(state)
                if data:
                    self.view.evaluations.blockSignals(True);self.view.evaluations.clear()
                    self.view.evaluations.addItem(data['plan']['name'],data['plan']['id']);self.view.evaluations.blockSignals(False)
                self.view.show_payload(data)
                for font,width,height in ((12,1200,800),(12,1600,950),(18,960,620),(24,960,620)):
                    self.view.setStyleSheet(f'QWidget {{font: {font}pt Arial;}}');self.view.resize(width,height);self.view.show()
                    for tab in ((0,1,2) if state=='completed' else (0,)):
                        self.view.tabs.setCurrentIndex(tab)
                        for _ in range(10):APP.processEvents()
                        with self.subTest(state=state,font=font,width=width,tab=tab):
                            self.assertEqual(self.view.width(),width)
                            for button in (self.view.primary,self.view.create,self.view.save_notebook,self.view.refresh):
                                if button.isVisible():self.assertLessEqual(button.fontMetrics().horizontalAdvance(button.text())+12,button.width())
                            self.assertTrue(self.view.rect().contains(self.view.tabs.geometry()))
                            self.assertGreater(self.view.tabs.height(),80)
                            if output:self.view.grab().save(str(Path(output)/f'{state}-{width}-{font}-tab{tab}.png'))

    def test_maintainer_tab_exists_and_child_writes_use_its_shutdown_protection(self):
        import maintainer_tab
        with patch.object(maintainer_tab.QTimer,'singleShot'):
            owner=maintainer_tab.MaintainerTab(types.SimpleNamespace(universal_annotations={}))
        self.addCleanup(dispose_closed_views,owner);self.addCleanup(owner.close)
        self.assertIn('Model Evaluation',[owner.pages.tabText(index) for index in range(owner.pages.count())])
        self.assertNotIn('Experiments',[owner.pages.tabText(index) for index in range(owner.pages.count())])
        page=owner.model_evaluation;page.set_allowed(True);page.timer.stop();page.show_payload(payload())
        callbacks=[]
        with patch.object(maintainer_tab,'run_background',side_effect=lambda action,done:callbacks.append(done)):
            page.write(lambda:True,lambda _:None)
        self.assertTrue(owner.pending_work());self.assertTrue(page._busy)
        callbacks[0](True,None)
        self.assertFalse(owner.pending_work());self.assertFalse(page._busy)


if __name__=='__main__':unittest.main()
