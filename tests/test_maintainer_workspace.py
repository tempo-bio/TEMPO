"""Maintainer UI and measured progress checks; no remote calls or GPU work."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import time
import types
import unittest
from unittest.mock import patch
from PyQt6.QtWidgets import QApplication, QPushButton, QMessageBox
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtGui import QFont
import maintainer_tab
from tempo_ui import install_ui

from tempo_tasks import dispose_closed_views


def close_fixture(view):
    view.close()
    dispose_closed_views(view)


APP=QApplication.instance() or QApplication([])
install_ui(APP)


class TrainingWorkflowTests(unittest.TestCase):
    def dialog(self):
        view = maintainer_tab.TrainingWorkflowDialog()
        self.addCleanup(close_fixture, view)
        return view

    def test_reader_uses_packaged_document_and_scrolls_at_large_fonts(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'docs/TRAINING_WORKFLOW.md'
            source.parent.mkdir(); source.write_text('# Fixture workflow\n\n' + 'Read the full workflow.\n\n' * 70)
            with patch.object(maintainer_tab, 'get_base_dir', return_value=directory):
                view = self.dialog()
        self.assertIn('Fixture workflow', view.reader.toPlainText())
        original = APP.font(); self.addCleanup(APP.setFont, original)
        APP.setFont(QFont('Arial',24)); view.resize(640,460); view.show(); APP.processEvents()
        self.assertEqual(view.size().width(),640)
        self.assertEqual(view.size().height(),460)
        self.assertGreater(view.reader.verticalScrollBar().maximum(),0)
        self.assertEqual(view.reader.horizontalScrollBar().maximum(),0)
        QTest.keyClick(view.reader, Qt.Key.Key_PageDown)
        APP.processEvents()
        self.assertGreater(view.reader.verticalScrollBar().value(),0)
        QTest.keyClick(view.reader, Qt.Key.Key_Escape)
        self.assertFalse(view.isVisible())

    def test_maintained_guide_distinguishes_notebook_stages_and_preserved_holdouts(self):
        text = self.dialog().reader.toPlainText()
        for phrase in ('Run all','system\'s default browser',
                       'approximately every 10 seconds','approximately every 15 seconds',
                       'First model saved','Submission has no minimum image, cell or class count'):
            self.assertIn(phrase,text)


class MaintainerTests(unittest.TestCase):
    def test_cloud_notebook_edit_action_requests_its_settings_editor(self):
        from unittest.mock import Mock
        self.view.pages.setEnabled(True)
        self.view.pages.setCurrentWidget(self.view.cloud_scroll)
        requested = Mock(); self.view.notebook_settings_requested.connect(requested)
        self.view.edit_notebook.click()
        requested.assert_called_once_with()
        from PyQt6.QtWidgets import QLabel
        self.assertIn('Notebook URL', [label.text() for label in self.view.cloud.findChildren(QLabel)])
        self.assertNotIn('Notebook URL (optional)', [label.text() for label in self.view.cloud.findChildren(QLabel)])

    def test_training_tab_opens_workflow_without_preparing_a_run(self):
        self.view.pages.setEnabled(True); self.view.pages.setCurrentWidget(self.view.cloud_scroll)
        with patch.object(maintainer_tab.TrainingWorkflowDialog,'exec') as opened:
            self.view.training_workflow.click()
        opened.assert_called_once()
        self.assertFalse(self.view.pending_work())

    def test_unavailable_workflow_is_an_error_without_starting_training(self):
        with patch.object(maintainer_tab,'get_base_dir',return_value='/missing-tempo-fixture'):
            self.view.open_training_workflow()
        self.assertIn('Training workflow could not be opened',self.view.cloud_error.summary.text())
        self.assertFalse(self.view.pending_work())

    def test_pending_write_remains_guarded_until_callback_is_settled(self):
        callbacks = []
        with patch.object(maintainer_tab, 'run_background', side_effect=lambda action, done: callbacks.append(done)):
            self.view.run_write(lambda: True, lambda value, error: None)
        self.assertTrue(self.view.pending_work())
        callbacks[0](True, None)
        self.assertFalse(self.view.pending_work())

    def test_local_approval_failure_clears_stale_success_and_stays_inline(self):
        from unittest.mock import Mock
        from PyQt6.QtWidgets import QListWidgetItem
        self.view.pages.setEnabled(True)
        item = QListWidgetItem('image.png'); item.setData(Qt.ItemDataRole.UserRole, 'image.png')
        self.view.local_list.blockSignals(True)
        self.view.local_list.addItem(item); self.view.local_list.setCurrentItem(item)
        self.view.approve_image.setEnabled(True)
        self.view.image_action_status.setText('1 images approved.')
        self.view.annotator.approve_selected_images = Mock(side_effect=ConnectionError('offline'))
        with patch.object(self.view, 'refresh_local') as refresh:
            self.view.approve_local()
        self.assertEqual(self.view.image_action_status.text(), '')
        self.assertIn('offline', self.view.image_error.details.toPlainText())
        refresh.assert_not_called()

    def test_notebook_opening_depends_on_url_not_run_preparation(self):
        self.view.pages.setEnabled(True)
        self.view.url.setText('');self.assertTrue(self.view.reopen.isEnabled())
        self.view.url.setText('https://colab.research.google.com/drive/fixture')
        self.assertTrue(self.view.reopen.isEnabled())
        for phase in ('training','completed','failed','disconnected'):
            self.view.remote_ready(({'id':'fixture'}, {'phase':phase,'updated_at':time.time()}, {'images':{}}),None)
            self.assertTrue(self.view.reopen.isEnabled())
        self.assertFalse(hasattr(self.view,'start'))
        self.assertFalse(hasattr(self.view,'gpu'))
        self.assertFalse(hasattr(self.view,'epochs'))

    def test_blank_url_opens_colab_without_saving_or_starting_training(self):
        self.view.pages.setEnabled(True)
        self.view.url.setText('  ')
        self.view.remote_ready(({'id':'run'},{'phase':'disconnected'},None),None)
        with patch.object(maintainer_tab.webbrowser,'open',return_value=True) as browser, \
             patch.object(maintainer_tab.tempo_preferences,'save') as save, \
             patch.object(maintainer_tab.tempo_preferences,'set') as set_preference, \
             patch('subprocess.Popen') as process:
            self.view.reopen.click()
        browser.assert_called_once_with(maintainer_tab.COLAB_HOME)
        save.assert_not_called();set_preference.assert_not_called();process.assert_not_called()
        self.assertIn('Choose a saved notebook',self.view.cloud_notice.text())

    def test_invalid_url_shows_reason_instead_of_disabling_open_notebook(self):
        self.view.pages.setEnabled(True)
        self.view.url.setText('https://example.com/notebook')
        self.assertTrue(self.view.reopen.isEnabled())
        with patch.object(maintainer_tab.webbrowser,'open') as browser:
            self.view.reopen.click()
        browser.assert_not_called()
        self.assertIn('Enter a Colab notebook URL',self.view.cloud_error.summary.text())

    def test_opening_shared_notebook_does_not_attempt_a_local_save(self):
        self.view.url.setText('https://colab.research.google.com/drive/fixture')
        with patch.object(maintainer_tab.webbrowser,'open',return_value=True) as browser, \
             patch.object(maintainer_tab.tempo_preferences,'save',return_value=False) as save, \
             patch.object(maintainer_tab.tempo_preferences,'set'):
            self.view.open_colab()
        browser.assert_called_once()
        save.assert_not_called()
        self.assertTrue(self.view.cloud_error.isHidden())
        self.assertTrue(self.view.url.isReadOnly())

    def test_opening_notebook_never_changes_administrator_managed_settings(self):
        self.view.url.setText('https://colab.research.google.com/drive/fixture')
        with patch.object(maintainer_tab.webbrowser,'open',return_value=True) as browser, \
             patch.object(maintainer_tab.tempo_preferences,'set',side_effect=OSError('Preferences unavailable')) as save:
            self.view.open_colab()
        browser.assert_called_once()
        save.assert_not_called()
        self.assertTrue(self.view.cloud_error.isHidden())

    def test_reservations_use_remote_original_names_and_report_search_matches(self):
        manifest = {'images': {
            'a.png': {'status': 'locked', 'original_filename': 'field_10.png', 'claimed_by': 'Alice'},
            'z.png': {'status': 'locked', 'original_filename': 'field_2.png', 'claimed_by': 'Bob'},
        }}
        self.view.remote_ready(({}, {}, manifest), None)
        self.assertEqual(self.view.leases.item(0, 0).text(), 'field_2.png')
        self.view.lease_search.setText(' Bob ')
        self.assertEqual(self.view.lease_summary.text(), '1 of 2 reservations match.')
        self.view.remote_ready(({}, {}, manifest), None)
        self.assertEqual(self.view.lease_summary.text(), '1 of 2 reservations match.')
        self.view.lease_search.setText('missing')
        self.assertEqual(self.view.lease_summary.text(), '0 of 2 reservations match.')

    def test_cached_other_owner_reservation_disables_local_approval(self):
        self.view.annotator.universal_annotations = {'image.png': {'boxes': [{}]}}
        self.view.annotator.collaborator_id = 'Alice'
        self.view.annotator.lock_manifest = {'images': {'image.png': {
            'status': 'locked', 'claimed_by': 'Bob', 'expires_at': '2099-01-01T00:00:00+00:00'}}}
        self.view.refresh_local()
        with patch.object(self.view.local_preview, 'set_image_and_masks'):
            self.view.local_list.setCurrentRow(0)
        self.assertFalse(self.view.approve_image.isEnabled())
        self.assertIn('not reserved by you', self.view.image_status.text())
        self.assertIn('Reserved by: Bob', self.view.image_status.text())
        self.view.annotator.lock_manifest['images']['image.png']['expires_at'] = '2000-01-01T00:00:00+00:00'
        with patch.object(self.view.local_preview, 'set_image_and_masks'):
            self.view.preview_local(self.view.local_list.currentItem())
        self.assertIn('Last reserved by: Bob', self.view.image_status.text())

    def test_local_draft_owner_uses_shared_profile_with_id_only_on_hover(self):
        actor = 'hf:foreign-draft-owner'
        self.view.annotator.universal_annotations = {'image.png': {'boxes': [{}], 'draft_owner': actor}}
        self.view.annotator.lock_manifest = dict(images={}, collaborators={actor: dict(profile_name='Cell Mechanics Lab')})
        self.view.refresh_local()
        with patch.object(self.view.local_preview, 'set_image_and_masks'):
            self.view.local_list.setCurrentRow(0)
        self.assertIn('Local draft by: Cell Mechanics Lab', self.view.image_status.text())
        self.assertNotIn(actor, self.view.image_status.text())
        self.assertEqual(self.view.image_status.toolTip(), actor)
        self.view.preview_local(None)
        self.assertEqual(self.view.image_status.toolTip(), '')

    def test_approved_images_remain_in_image_inventory(self):
        self.view.annotator.universal_annotations = {
            'stored.png': {'original_filename':'image.png','status':'approved','boxes':[{}]},
            'draft.png': {'boxes':[{}]},
        }
        self.view.refresh_local()
        self.assertEqual(self.view.local_list.count(), 2)
        self.assertEqual(self.view.local_list.item(1).text().splitlines()[0], 'image.png')
        self.view.image_filter.setCurrentIndex(2)
        self.assertEqual(self.view.local_list.count(), 1)

    def test_failed_run_overrides_last_epoch_display(self):
        self.view.remote_ready(({'id':'r'}, {'phase':'failed','models':{
            'cellpose':{'phase':'training','epoch':12,'total':150}}}, {'images':{}}),None)
        label,bar=self.view.models['cellpose']
        self.assertIn('failed',label.text().lower())
        self.assertNotIn('training',label.text())
        self.assertEqual(bar.format(),'Failed')
        self.assertIn('No result reported', self.view.models['stardist'][0].text())

    def test_pool_uses_original_filename_in_tooltip(self):
        self.view.remote_ready(({}, {}, {'images': {'internal.png': {
            'original_filename': 'field_57.png', 'status': 'approved'}}}), None)
        self.assertEqual(self.view.pool_list.item(0).toolTip(), 'field_57.png')
        self.assertEqual(self.view.pool_list.item(0).text().splitlines()[0], 'field_57.png')

    def test_pool_orders_original_names_naturally_with_local_fallback(self):
        self.view.annotator.universal_annotations = {'a.png': {'original_filename': 'field_10.png'}}
        self.view.remote_ready(({}, {}, {'images': {
            'a.png': {'status': 'approved'},
            'z.png': {'original_filename': 'field_2.png', 'status': 'approved'}}}), None)
        self.assertEqual([self.view.pool_list.item(i).toolTip() for i in range(2)],
                         ['field_2.png', 'field_10.png'])

    def test_preview_failure_disables_approval_and_retry_clears_error(self):
        self.view.pages.setEnabled(True)
        self.view.annotator.universal_annotations = {'image.png': {'boxes': [{}]}}
        self.view.refresh_local()
        with patch.object(self.view.local_preview, 'set_image_and_masks', side_effect=OSError('missing image')):
            self.view.local_list.setCurrentRow(0)
        self.assertFalse(self.view.approve_image.isEnabled())
        self.assertIn('missing image', self.view.image_error.details.toPlainText())
        with patch.object(self.view.local_preview, 'set_image_and_masks'):
            self.view.preview_local(self.view.local_list.currentItem())
        self.assertTrue(self.view.image_error.isHidden())

    def test_missing_metric_is_unavailable_and_does_not_hide_failure(self):
        self.view.remote_ready(({'id':'r'}, {'phase':'failed','error':'Validation failed',
            'metrics':{'stardist':{'metric':'instance_F1_IoU50','score':None}}}, {'images':{}}),None)
        text=self.view.details.toPlainText()
        self.assertIn('Validation failed',text)
        self.assertIn('unavailable',text)
        self.assertNotIn('0.0000',text)

    def test_validation_is_not_displayed_as_epoch_completion(self):
        self.view.remote_ready(({'id':'r'}, {'phase':'training','models':{
            'cellpose':{'phase':'validating','epoch':150,'total':150}}}, {'images':{}}),None)
        label,bar=self.view.models['cellpose']
        self.assertEqual(bar.maximum(),0)
        self.assertEqual(bar.format(),'Validating')

    def test_pool_upload_cancel_has_no_remote_effect(self):
        with patch.object(maintainer_tab.QFileDialog,'getOpenFileNames',return_value=(['image.png'],'')), \
             patch.object(maintainer_tab,'QImage') as image, \
             patch.object(maintainer_tab,'confirm_action',return_value=False), \
             patch.object(maintainer_tab.tempo_hub,'upload_raw_images') as upload:
            image.return_value.isNull.return_value=False
            self.view.upload_pool_images()
        upload.assert_not_called()

    def test_pool_rejects_duplicate_names_before_upload(self):
        with patch.object(maintainer_tab.QFileDialog,'getOpenFileNames',return_value=(['one/image.png','two/image.png'],'')), \
             patch.object(maintainer_tab.tempo_hub,'upload_raw_images') as upload:
            self.view.upload_pool_images()
        upload.assert_not_called()
        self.assertIn('same filename',self.view.pool_status.text())

    def test_pool_refresh_preserves_action_error(self):
        self.view.pool_status.setText('Upload failed: offline')
        self.view.remote_ready(({}, {}, {'images':{'image.png':{'status':'available'}}}),None)
        self.assertEqual(self.view.pool_list.count(),1)
        self.assertEqual(self.view.pool_status.text(),'Upload failed: offline')

    def test_upload_error_details_survive_inventory_refresh(self):
        self.view.pages.setEnabled(True)
        with patch.object(maintainer_tab.QFileDialog, 'getOpenFileNames', return_value=(['image.png'], '')), \
             patch.object(maintainer_tab, 'QImage') as image, \
             patch.object(maintainer_tab, 'confirm_action', return_value=True), \
             patch.object(maintainer_tab, 'run_background', side_effect=lambda action, done: done(None, ConnectionError('offline'))):
            image.return_value.isNull.return_value = False
            self.view.upload_pool_images()
        self.assertTrue(self.view.upload_images_button.isEnabled())
        self.assertTrue(self.view.pool_error.details.isHidden())
        self.view.remote_ready(({}, {}, {'images': {}}), None)
        self.assertIn('already have uploaded', self.view.pool_error.summary.text())
        self.view.pool_error.toggle.click()
        self.assertIn('offline', self.view.pool_error.details.toPlainText())

    def test_remote_submission_is_not_offered_for_local_approval(self):
        self.view.annotator.universal_annotations={'image.png':{'boxes':[{}]}}
        self.view._remote_images={'image.png':{'status':'submitted'}}
        self.view.refresh_local()
        with patch.object(self.view.local_preview,'set_image_and_masks'):
            self.view.local_list.setCurrentRow(0)
        self.assertIn('Awaiting review',self.view.local_list.item(0).text())
        self.assertFalse(self.view.approve_image.isEnabled())
        self.view.image_filter.setCurrentIndex(1)
        self.assertEqual(self.view.local_list.count(),0)

    def test_backend_access_failure_is_distinguishable_without_granting_access(self):
        from tempo_hub import TempoHub
        def offline(**kwargs): raise ConnectionError('offline')
        hub=types.SimpleNamespace(api=types.SimpleNamespace(whoami=offline),token='test')
        self.assertFalse(TempoHub.is_maintainer(hub))
        with self.assertRaises(ConnectionError): TempoHub.is_maintainer(hub,raise_on_error=True)

    def test_workspace_requests_error_reporting_for_access_check(self):
        pending=[]
        with patch.object(maintainer_tab,'run_background',side_effect=lambda action,done:pending.append(action)), \
             patch.object(maintainer_tab.tempo_hub,'is_maintainer',return_value=False) as check:
            self.view.verify_access(); pending.pop()()
        check.assert_called_once_with(raise_on_error=True)

    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(maintainer_tab.QTimer,'singleShot').start()
        self.view=maintainer_tab.MaintainerTab(types.SimpleNamespace(universal_annotations={}))
        self.addCleanup(close_fixture, self.view)

    def test_real_epochs_and_completed_models_are_separate_from_publication(self):
        self.view.remote_ready(({'id':'run'},{'phase':'training','gpu':'A100','updated_at':time.time(),
            'models':{'nano_scout':{'phase':'training','epoch':3,'total':150},'stardist':{'phase':'training'},'yolo_seg':{'phase':'completed'}}}, {'images':{}}),None)
        label,bar=self.view.models['nano_scout']
        self.assertEqual(bar.value(),3); self.assertEqual(bar.maximum(),150)
        self.assertIn('epoch 3/150',label.text())
        self.assertEqual(self.view.models['stardist'][1].maximum(),0)
        self.assertIn('training',self.view.phase.text().lower())

    def test_reservations_have_columns_and_exclude_approved_images(self):
        self.view.remote_ready(({}, {}, {'images':{
            'approved.png':{'status':'approved'},
            'available.png':{'status':'available'},
            'image.png':{'status':'locked','claimed_by':'A','expires_at':'2030-01-01T00:00:00+00:00'},
        }}),None)
        self.assertEqual(self.view.leases.rowCount(),1)
        self.assertEqual(self.view.leases.item(0,0).text(),'image.png')
        self.assertEqual(self.view.leases.item(0,1).text(),'A')
        self.view.lease_search.setText('not present')
        self.assertTrue(self.view.leases.isRowHidden(0))

    def test_access_check_error_keeps_retry_available(self):
        signals=[]; self.view.access_checked.connect(signals.append)
        self.view.access_ready(False, ConnectionError('offline'))
        self.assertEqual(signals,[True])
        self.assertFalse(self.view.pages.isEnabled())
        self.assertFalse(self.view.retry_access.isHidden())
        self.assertNotIn('published',self.view.notice.text())
        self.assertTrue(self.view.access_error.details.isHidden())
        self.view.access_error.toggle.click()
        self.assertIn('offline', self.view.access_error.details.toPlainText())
        self.view.verify_access()
        self.assertTrue(self.view.access_error.isHidden())

    def test_failed_and_stale_runs_not_reported_complete(self):
        self.view.remote_ready(({'id':'run'},{'phase':'training','updated_at':time.time()-180,'models':{}},{'images':{}}),None)
        self.assertIn('last heartbeat',self.view.phase.text())
        self.view.remote_ready(({'id':'run'},{'phase':'failed','error':'GPU unavailable','models':{}},{'images':{}}),None)
        self.assertIn('GPU unavailable',self.view.details.toPlainText())
        self.assertFalse(self.view.stop.isEnabled())

    def test_buttons_show_pointer_and_disabled_buttons_do_not(self):
        button=QPushButton('Action'); self.addCleanup(button.close)
        button.show(); APP.processEvents()
        self.assertEqual(button.cursor().shape(),Qt.CursorShape.PointingHandCursor)
        button.setEnabled(False); APP.processEvents()
        self.assertEqual(button.cursor().shape(),Qt.CursorShape.ArrowCursor)

    def test_disconnected_request_has_no_active_controls(self):
        self.view.remote_ready(({'id':'run'},{'phase':'disconnected','updated_at':time.time()-180,
            'models':{'stardist':{'phase':'training'}}},{'images':{}}),None)
        self.assertFalse(self.view.stop.isEnabled())
        self.assertFalse(self.view.reset_stale.isEnabled())
        self.assertNotIn('last heartbeat',self.view.phase.text())
        self.assertEqual(self.view.models['stardist'][1].maximum(),1)

    def test_clear_disconnected_applies_confirmed_result_without_another_fetch(self):
        self.view.request={'id':'run'}
        def run(action,done):
            try: result=action()
            except Exception as error: done(None,error)
            else: done(result,None)
        with patch.object(maintainer_tab,'confirm_action',return_value=True), \
             patch.object(maintainer_tab,'run_background',side_effect=run), \
             patch.object(maintainer_tab,'resolve_disconnected_run') as resolve, \
             patch.object(self.view,'refresh_remote') as refresh:
            resolve.side_effect = ValueError('Colab reported within the last two minutes.')
            self.view.clear_disconnected()
            self.assertIn('last two minutes',self.view.cloud_error.details.toPlainText())
            refresh.assert_called_once()
            refresh.reset_mock(); resolve.side_effect = None
            resolve.return_value = dict(request={'id':'run'},status={'phase':'disconnected','updated_at':time.time()},outcome='cleared')
            self.view.clear_disconnected()
            self.assertIn('Run record cleared',self.view.cloud_notice.text())
            self.assertFalse(self.view.reset_stale.isEnabled())
            self.assertFalse(self.view.stop.isEnabled())
            self.assertFalse(self.view.pending_work())
            self.assertIn('Disconnected',self.view.phase.text())
            refresh.assert_not_called()

    def test_status_timeout_allows_retry_and_ignores_late_response(self):
        callbacks = []
        self.view._allowed = True
        self.view.pages.setEnabled(True)
        self.view.pages.setCurrentWidget(self.view.cloud_scroll)
        with patch.object(maintainer_tab,'run_background',side_effect=lambda action,done:callbacks.append(done)):
            self.view.refresh_status.click()
            self.assertTrue(self.view._polling)
            self.assertEqual(self.view.refresh_status.text(),'Refresh Status')
            self.assertTrue(self.view.refresh_status.isEnabled())
            self.assertEqual(self.view.refresh_spinner.accessibleDescription(),'Refreshing')
            self.assertTrue(self.view.poll_deadline.isActive())
            self.view.status_read_timed_out()
            self.assertFalse(self.view._polling)
            self.assertEqual(self.view.refresh_spinner.accessibleDescription(),'Idle')
            self.assertTrue(self.view.refresh_status.isEnabled())
            self.assertIn('30 seconds',self.view.status_error.details.toPlainText())
            self.view.refresh_status.click()
            callbacks[0](({'id':'old'},{'phase':'training'},None),None)
            self.assertTrue(self.view._polling)
            self.assertIsNone(self.view.request)
            callbacks[1](({'id':'new'},{'phase':'failed'},None),None)
            self.assertEqual(self.view.request['id'],'new')
            self.assertFalse(self.view._polling)
            self.assertTrue(self.view.status_error.isHidden())

    def test_cloud_refresh_reads_training_only_without_flashing_feedback(self):
        self.view._allowed = True; self.view.pages.setEnabled(True)
        self.view.pages.setCurrentWidget(self.view.cloud_scroll)
        calls = []
        def read(name,*args):
            calls.append(name)
            if name == 'training/request.json': return {'id':'run'}
            if name == 'training/run/status.json': return {'phase':'training','updated_at':100}
            raise AssertionError('Cloud refresh must not depend on annotation reads')
        with patch.object(maintainer_tab,'run_background',side_effect=lambda action,done:done(action(),None)), \
             patch.object(maintainer_tab.tempo_hub,'_head',return_value='head'), \
             patch.object(maintainer_tab.tempo_hub,'_read',side_effect=read):
            self.view.refresh_status.click(); self.view.refresh_status.click()
        self.assertEqual(calls,['training/request.json','training/run/status.json']*2)
        self.assertFalse(hasattr(self.view,'refresh_feedback'))
        self.assertEqual(self.view.refresh_status.text(),'Refresh Status')
        self.assertTrue(self.view.refresh_status.isEnabled())
        self.assertEqual(self.view.refresh_spinner.accessibleDescription(),'Idle')

    def test_training_header_stays_at_top_across_run_states_and_fonts(self):
        self.view.pages.setEnabled(True); self.view.pages.setCurrentWidget(self.view.cloud_scroll)
        self.view.notice.clear()
        self.view.resize(900,680); self.view.show()
        for points in (12,18,24):
            self.view.setFont(QFont('Arial',points))
            positions=[]
            for phase in ('training','checking_training_steps','disconnected','completed'):
                self.view.remote_ready(({'id':'run'},{'phase':phase,'updated_at':time.time(),'gpu':'NVIDIA L4'},None),None)
                APP.processEvents()
                positions.append(self.view.training_workflow.y())
                self.assertEqual(self.view.training_workflow.y(),self.view.cloud.layout().contentsMargins().top())
            self.assertEqual(len(set(positions)),1)
            self.assertTrue(self.view.notice.isHidden())

    def test_real_spinner_animation_and_stable_button_geometry_during_refresh(self):
        callbacks=[]
        self.view._allowed=True;self.view.pages.setEnabled(True)
        self.view.pages.setCurrentWidget(self.view.cloud_scroll)
        self.view.notice.clear();self.view.resize(1000,760);self.view.show();APP.processEvents()
        button_bounds=self.view.refresh_status.geometry()
        spinner_bounds=self.view.refresh_spinner.geometry()
        with patch.object(maintainer_tab,'run_background',side_effect=lambda action,done:callbacks.append(done)):
            self.view.refresh_remote()
            self.assertTrue(self.view.refresh_spinner.timer.isActive())
            before=self.view.refresh_spinner.grab().toImage()
            QTest.qWait(180)
            after=self.view.refresh_spinner.grab().toImage()
            self.assertNotEqual(before,after)
            self.assertEqual(self.view.refresh_status.geometry(),button_bounds)
            self.assertEqual(self.view.refresh_spinner.geometry(),spinner_bounds)
            self.view.refresh_remote(manual=True);self.view.refresh_remote(manual=True)
            self.assertEqual(len(callbacks),1)
            callbacks[0](({}, {}, None),None)
            APP.processEvents()
            self.assertFalse(self.view.refresh_spinner.timer.isActive())
            self.assertEqual(self.view.refresh_status.geometry(),button_bounds)
            self.assertEqual(self.view.refresh_spinner.geometry(),spinner_bounds)
            self.assertEqual(self.view.refresh_status.text(),'Refresh Status')

    def test_hidden_spinner_suspends_animation_and_resumes_on_show(self):
        self.view.pages.setEnabled(True);self.view.pages.setCurrentWidget(self.view.cloud_scroll)
        self.view.show();APP.processEvents()
        self.view.refresh_spinner.set_busy(True)
        self.assertTrue(self.view.refresh_spinner.timer.isActive())
        self.view.hide();APP.processEvents()
        self.assertFalse(self.view.refresh_spinner.timer.isActive())
        self.view.show();APP.processEvents()
        self.assertTrue(self.view.refresh_spinner.timer.isActive())
        self.view.refresh_spinner.set_busy(False)
        self.assertFalse(self.view.refresh_spinner.timer.isActive())

    def test_recovery_write_remains_protected_and_late_poll_cannot_restore_active_run(self):
        callbacks = []
        self.view._allowed = True
        self.view.request = {'id':'run'}
        with patch.object(maintainer_tab,'run_background',side_effect=lambda action,done:callbacks.append(done)), \
             patch.object(maintainer_tab,'confirm_action',return_value=True):
            self.view.refresh_remote()
            self.view.clear_disconnected()
            self.assertTrue(self.view.pending_work())
            self.assertFalse(self.view.reset_stale.isEnabled())
            self.assertEqual(self.view.reset_stale.text(),'Resolving…')
            callbacks[1](dict(request={'id':'run'},status={'phase':'disconnected'},outcome='cleared'),None)
            callbacks[0](({'id':'run'},{'phase':'training'},None),None)
            self.assertIn('Disconnected',self.view.phase.text())
            self.assertFalse(self.view.pending_work())

    def test_http_rejection_is_visible_and_redacted_without_expanding_details(self):
        error = RuntimeError('Secret fixture-private-token')
        error.response = types.SimpleNamespace(status_code=429)
        self.view.request = {'id':'run'}
        with patch.object(maintainer_tab.tempo_hub,'token','fixture-private-token'), \
             patch.object(maintainer_tab,'confirm_action',return_value=True), \
             patch.object(maintainer_tab,'run_background',side_effect=lambda action,done:done(None,error)):
            self.view.clear_disconnected()
        self.assertIn('HTTP 429',self.view.cloud_error.summary.text())
        self.assertNotIn('fixture-private-token',self.view.cloud_error.details.toPlainText())
        self.assertFalse(self.view.pending_work())

    def test_account_change_discards_delayed_status_result(self):
        callbacks = []
        self.view._allowed = True
        with patch.object(maintainer_tab,'run_background',side_effect=lambda action,done:callbacks.append(done)), \
             patch.object(maintainer_tab.tempo_hub,'token','first-account'), \
             patch.object(self.view,'verify_access') as verify:
            self.view.refresh_remote()
            with patch.object(maintainer_tab.tempo_hub,'token','second-account'):
                callbacks[0](({'id':'foreign'},{'phase':'training'},None),None)
            verify.assert_called_once()
        self.assertIsNone(self.view.request)

    def test_poll_recovery_preserves_cloud_action_error(self):
        self.view.request = {'id': 'run'}
        with patch.object(maintainer_tab, 'run_background', side_effect=lambda action, done: done(None, ConnectionError('cancel offline'))):
            self.view.cancel_cloud()
        self.view.remote_ready(None, ConnectionError('poll offline'))
        self.assertIn('poll offline', self.view.status_error.details.toPlainText())
        self.view.remote_ready(({'id': 'run'}, {'phase': 'training'}, {'images': {}}), None)
        self.assertTrue(self.view.status_error.isHidden())
        self.assertFalse(self.view.cloud_error.isHidden())
        self.assertIn('cancel offline', self.view.cloud_error.details.toPlainText())
        self.assertIn('interrupt the cell in Colab', self.view.cloud_error.summary.text())

    def test_cloud_publication_notice_does_not_overwrite_image_feedback(self):
        self.view.image_action_status.setText('Image approved.')
        self.view.remote_ready(({'id': 'run'}, {'phase': 'completed'}, {'images': {}}), None)
        self.assertEqual(self.view.cloud_notice.text(), 'Run completed.')
        self.assertEqual(self.view.image_action_status.text(), 'Image approved.')
        self.assertNotIn('published', self.view.notice.text())
        self.view.remote_ready(({'id': 'next'}, {'phase': 'awaiting_colab'}, {'images': {}}), None)
        self.assertEqual(self.view.cloud_notice.text(), '')

    def test_normal_notice_is_branded_without_error_icon(self):
        box=QMessageBox(QMessageBox.Icon.Information,'Saved','Annotation saved')
        self.addCleanup(box.close);box.show();APP.processEvents()
        self.assertEqual(box.icon(),QMessageBox.Icon.NoIcon)
        self.assertEqual(box.text(),'Annotation saved')

    def test_nonmaintainer_cannot_enable_workspace(self):
        self.view.access_ready(False,None)
        self.assertFalse(self.view.pages.isEnabled())

    def test_open_notebook_uses_default_browser_without_starting_process(self):
        self.view.url.setText('https://colab.research.google.com/drive/fixture')
        with patch.object(maintainer_tab.webbrowser,'open',return_value=True) as browser, \
             patch('subprocess.Popen') as process,patch.object(maintainer_tab.tempo_preferences,'save',return_value=True), \
             patch.object(maintainer_tab.tempo_preferences,'set'):
            self.view.open_colab()
        browser.assert_called_once_with(self.view.url.text());process.assert_not_called()

    def test_completed_run_reports_zero_improvements_without_claiming_replacement(self):
        self.view.remote_ready(({'id':'run'},{'phase':'completed','updated_count':0,'updated_at':time.time()}, {'images':{}}),None)
        self.assertEqual(self.view.cloud_notice.text(),'Current models updated: 0.')
        self.assertFalse(self.view.stop.isEnabled())

    def test_updated_model_comparison_is_labelled_as_previous_score(self):
        new = {'metric':'mAP50','score':.0911}
        previous = {'metric':'mAP50','score':.0206}
        self.view.remote_ready(({'id':'run'},{'phase':'completed','updated_count':1,
            'metrics':{'nano_scout':new},'model_outcomes':{'nano_scout':{
                'outcome':'Updated','baseline_metrics':previous}}},None),None)
        text = self.view.details.toPlainText()
        self.assertIn('0.0911',text)
        self.assertIn('Previous model score: Mean average precision (IoU 0.50): 0.0206',text)
        self.assertNotIn('Current score:',text)

    def test_cellpose_specialist_stages_and_class_scores_are_visible(self):
        self.view.remote_ready(({'id': 'run'}, {'phase': 'training', 'updated_at': time.time(),
            'models': {'cellpose': {'phase': 'training_dead', 'epoch': 151, 'total': 300}}}, {'images': {}}), None)
        label, bar = self.view.models['cellpose']
        self.assertIn('Training dead', label.text())
        self.assertEqual(bar.value(), 151)
        self.assertEqual(bar.maximum(), 300)
        self.view.remote_ready(({'id': 'run'}, {'phase': 'completed', 'metrics': {'cellpose': {
            'metric': 'cell_class_F1_IoU50', 'score': .7, 'class_scores': {'healthy': .5, 'dead': .9}}}}, None), None)
        text = self.view.details.toPlainText()
        self.assertIn('Cell detection and class F1', text)
        self.assertIn('Healthy: 0.5000', text)
        self.assertIn('Dead: 0.9000', text)

    def test_stale_status_and_reopened_progress_require_no_browser_access(self):
        self.view.pages.setEnabled(True)
        with patch.object(maintainer_tab.webbrowser,'open',side_effect=AssertionError('No browser read')):
            self.view.remote_ready(({'id':'run'},{'phase':'training','updated_at':time.time()-70,
                'models':{'cellpose':{'phase':'training','epoch':10,'total':150}}},{'images':{}}),None)
        self.assertIn('No status update',self.view.connection_status.text())
        self.assertIn('epoch 10/150',self.view.models['cellpose'][0].text())
        self.assertFalse(self.view.reset_stale.isEnabled())
        self.view.remote_ready(({'id':'run'},{'phase':'training','updated_at':time.time()-130},{'images':{}}),None)
        self.assertTrue(self.view.reset_stale.isEnabled())

    def test_notebook_url_rejects_unrelated_or_local_targets(self):
        for value in ['file:///tmp/test','http://colab.research.google.com/drive/a','https://example.com/drive/a']:
            with self.assertRaises(ValueError):maintainer_tab.colab_url(value)
