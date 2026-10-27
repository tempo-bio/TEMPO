"""Bounded GUI fixtures for shared previews, contained help, account and settings states."""
import copy
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from PyQt6.QtCore import Qt, QPoint, QRect
from PyQt6.QtGui import QFont, QImage, QColor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QFormLayout, QPushButton
from tests.presentation_fixtures import write_dataset, write_historical_dataset
from tempo_ui import install_ui, help_controller
from tempo_image_browser import SharedImageBrowser

from tempo_tasks import dispose_closed_views


def close_fixture(view):
    view.close()
    dispose_closed_views(view)


APP = QApplication.instance() or QApplication([])
with patch('platform.machine',return_value='x86_64'):
    import tempo_gui as gui
install_ui(APP)


def settle(view, width=1100, height=760):
    view.resize(width,height); view.show()
    for _ in range(10): APP.processEvents()
    QTest.qWait(20)


class ImageBrowserAndSettingsTests(unittest.TestCase):

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.addCleanup(help_controller().dismiss)

    def browser(self, names=90):
        image=QImage(320,240,QImage.Format.Format_RGB32); image.fill(QColor('#687787'))
        image.save(str(self.root/'preview.png'))
        manifest={'images':{f'image{n}.png':{'status':'available'} for n in range(names)}}
        manifest['images']['image0.png']={'status':'approved'}
        record={'width':320,'height':240,'coordinate_space':'pixels','boxes':[
            {'class_id':0,'polygon':[[30,30],[120,30],[120,100],[30,100]]}]}
        catalog={'manifest':manifest,'records':{n:record for n in manifest['images']},
                 'revision':'one','dataset_repo':'fixture/data'}
        hub=SimpleNamespace(token='one',get_dataset_repo=lambda:'fixture/data',
            get_collaborator_id=lambda:'alice',fetch_image_catalog=lambda:copy.deepcopy(catalog),
            fetch_image_preview=lambda name,snapshot:dict(name=name,path=str(self.root/'preview.png'),
                record=copy.deepcopy(record),reservation=snapshot['manifest']['images'][name],revision=snapshot['revision']))
        jobs=[]
        browser=SharedImageBrowser(hub,lambda action,done:jobs.append((action,done)))
        self.addCleanup(close_fixture, browser)
        return browser,hub,jobs

    def complete(self,jobs):
        action,done=jobs.pop(0); done(action(),None)

    def test_focus_and_checkboxes_are_separate_and_first_n_replaces_selection(self):
        browser,hub,jobs=self.browser()
        settle(browser,450,620); browser.load(); self.complete(jobs)
        self.assertEqual(browser.checked_names(),[])
        browser.items['image8.png'].setCheckState(Qt.CheckState.Checked)
        browser.images.setCurrentItem(browser.items['image5.png'])
        self.assertEqual(browser.checked_names(),['image8.png'])
        browser.count.setValue(3); browser.select_first_available()
        self.assertEqual(browser.checked_names(),['image1.png','image2.png','image3.png'])
        self.assertEqual(browser.focused_name,'image5.png')
        self.assertGreater(browser.images.verticalScrollBar().maximum(),0)
        self.assertFalse(browser.items['image0.png'].flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def test_real_checkbox_click_keeps_preview_focus(self):
        browser,hub,jobs=self.browser(8)
        settle(browser,450,620); browser.load(); self.complete(jobs)
        browser.images.setCurrentItem(browser.items['image1.png'])
        target=browser.items['image2.png']
        browser.images.scrollToItem(target); APP.processEvents()
        rect=browser.images.checkbox_rect(target)
        self.assertTrue(browser.images.viewport().rect().contains(rect))
        QTest.mouseClick(browser.images.viewport(),Qt.MouseButton.LeftButton,pos=rect.center())
        self.assertEqual(browser.focused_name,'image1.png')
        self.assertEqual(browser.checked_names(),['image2.png'])

    def test_rapid_focus_and_account_switch_discard_stale_preview_results(self):
        browser,hub,jobs=self.browser(4); browser.load(); self.complete(jobs)
        results=[]; browser.preview_ready.connect(results.append)
        browser.images.setCurrentItem(browser.items['image1.png'])
        browser.images.setCurrentItem(browser.items['image2.png'])
        self.complete(jobs)
        self.assertEqual(results,[])
        self.complete(jobs)
        self.assertEqual([r['name'] for r in results],['image2.png'])
        browser.images.setCurrentItem(browser.items['image3.png'])
        hub.token='other'; self.complete(jobs)
        self.assertEqual(len(results),1)
        self.assertNotIn('image3.png',browser.cache)

    def test_account_switch_during_catalog_loading_keeps_refresh_available(self):
        browser,hub,jobs=self.browser(4)
        browser.load()
        self.assertFalse(browser.refresh.isEnabled())
        hub.token='other';browser.invalidate()
        self.assertTrue(browser.refresh.isEnabled())
        self.complete(jobs)
        self.assertIsNone(browser.catalog)
        self.assertEqual(browser.images.count(),0)

    def test_thumbnail_cache_is_bounded_and_errors_can_be_retried(self):
        browser,hub,jobs=self.browser(70); browser.load(); self.complete(jobs)
        for row in range(70):
            browser.images.setCurrentRow(row)
            while jobs: self.complete(jobs)
        self.assertLessEqual(len(browser.cache),64)
        self.assertTrue(browser.items['image0.png'].icon().isNull())
        browser.images.setCurrentRow(0)
        action,done=jobs.pop(0); done(None,ConnectionError('offline'))
        self.assertIn('offline',browser.error.details.toPlainText())
        browser.focus_image(browser.images.currentItem())
        self.complete(jobs)
        self.assertIn('image0.png',browser.cache)
        self.assertTrue(browser.error.isHidden())

    def test_statistics_controls_help_and_axes_at_all_review_sizes(self):
        original=APP.font()
        try:
            for size in (12,18,24):
                APP.setFont(QFont('Arial',size)); view=gui.AnalyzerTab(); self.addCleanup(close_fixture, view)
                view.load_csv(str(write_historical_dataset(self.root)))
                view.field_selector.setCurrentIndex(1); view.cell_selector.setCurrentIndex(1)
                self.assertEqual(view.lbl_target.text(),'Cell 001')
                self.assertEqual(view.plot_item.getAxis('bottom').labelText,'X')
                self.assertEqual(view.plot_item.getAxis('left').labelUnits,'px')
                self.assertFalse(view.plot_item.getViewBox().state['yInverted'])
                for width,height in ((960,620),(1100,760),(1700,950)):
                    settle(view,width,height)
                    heights=[control.height() for control in view.navigation_controls]
                    self.assertEqual(len(set(heights)),1)
                    self.assertEqual(view.plate_toggle.text(),'Plate Navigator')
                    label=view.form_core.itemAt(0,QFormLayout.ItemRole.LabelRole).widget()
                    value=view.form_core.itemAt(0,QFormLayout.ItemRole.FieldRole).widget()
                    self.assertLessEqual(value.x()-label.geometry().right()-1,24)
                    self.assertLessEqual(label.height(),label.heightForWidth(label.width())+2)
                    view.inspector_scroll.ensureWidgetVisible(label,0,0); APP.processEvents()
                    QTest.mouseMove(view,QPoint(5,5)); APP.processEvents()
                    QTest.mouseMove(label,label.rect().center()); QTest.qWait(180); APP.processEvents()
                    self.assertIsNone(help_controller().bubble)
                    panel = view.statistics_help
                    self.assertIn('Amplitude', panel.topic.text())
                    self.assertIn('arbitrary units', panel.details.text())
                    self.assertGreaterEqual(panel.details.height(), panel.details.heightForWidth(panel.details.width()))
                    view.inspector_scroll.ensureWidgetVisible(panel.details,0,0); APP.processEvents()
                    viewport=view.inspector_scroll.viewport()
                    text_rect=QRect(panel.details.mapTo(viewport,QPoint()),panel.details.size())
                    if text_rect.height() <= viewport.height():
                        self.assertTrue(viewport.rect().contains(text_rect), (size,width,height,viewport.rect(),text_rect))
                    else:
                        bar=view.inspector_scroll.verticalScrollBar()
                        bar.setValue(bar.maximum()); APP.processEvents()
                        self.assertLessEqual(panel.details.mapTo(viewport,QPoint(0,panel.details.height())).y(),viewport.height())
                        top=panel.details.mapTo(viewport,QPoint()).y()
                        bar.setValue(bar.value()+top); APP.processEvents()
                        self.assertGreaterEqual(panel.details.mapTo(viewport,QPoint()).y(),0)
                    QTest.mouseMove(view,QPoint(5,5)); APP.processEvents()
                    self.assertIsNone(help_controller().bubble)
                view.close()
        finally: APP.setFont(original)

    def test_settings_compare_saved_snapshot_revert_save_failures_and_contain_menu(self):
        view=gui.SettingsTab(); self.addCleanup(close_fixture, view)
        widget=view.widgets[('capture','target_fps')]
        original=widget.value()
        self.assertEqual(view.lbl_save_status.text(),'No changes')
        widget.setValue(original+1); self.assertEqual(view.lbl_save_status.text(),'Unsaved changes')
        widget.setValue(original); self.assertEqual(view.lbl_save_status.text(),'No changes')
        widget.setValue(original+1)
        with patch.object(gui.tempo_preferences,'save_to_disk',return_value=False):view.save_preferences()
        self.assertEqual(widget.value(),original+1)
        self.assertIn('could not be saved',view.lbl_save_status.text())
        with patch.object(gui.tempo_preferences,'save_to_disk',return_value=True),patch.object(gui.tempo_preferences,'set'):
            view.save_preferences()
        self.assertEqual(view.lbl_save_status.text(),'Settings saved')
        widget.setValue(original); self.assertEqual(view.lbl_save_status.text(),'Unsaved changes')
        widget.setValue(original+1); self.assertEqual(view.lbl_save_status.text(),'No changes')
        for size in (12,18,24):
            view.setFont(QFont('Arial',size))
            for width,height in ((960,620),(1100,760),(1700,950)):
                settle(view,width,height); view.btn_tools.click(); APP.processEvents()
                menu=view.tools_menu
                self.assertTrue(menu.isVisible())
                self.assertTrue(view.rect().contains(menu.geometry()))
                self.assertTrue(menu.buttons[0].hasFocus())
                QTest.keyClick(menu.buttons[0],Qt.Key.Key_Escape)
                self.assertFalse(menu.isVisible())

    def test_invalid_and_incompatible_field_placements_disable_full_well(self):
        for kind in ('invalid','incompatible','partial'):
            path=write_dataset(self.root,True)
            placement=self.root/'recording-2.spatial.json'
            document=json.loads(placement.read_text())
            if kind=='partial': placement.unlink()
            else:
                if kind=='invalid': document['pixel_to_well']=[[0,0,0],[0,0,0]]
                else: document['coordinate_system']='other-origin'
                placement.write_text(json.dumps(document))
            view=gui.AnalyzerTab(); self.addCleanup(close_fixture, view); view.load_csv(str(path))
            self.assertTrue(view.glw.isHidden())
            self.assertEqual(view.well_cells.rowCount(),12)
            self.assertEqual(view.cell_selector.count(),13)
            self.assertIn('Full-well map unavailable',view.map_notice.text())
            view.field_selector.setCurrentIndex(2)
            self.assertEqual(len(view.scatter.points()),6)
            view.close()

    def test_account_states_reject_old_identity_and_open_setup(self):
        from PyQt6.QtWidgets import QMainWindow
        class Host(gui.TempoDashboard):
            def __init__(self):
                QMainWindow.__init__(self)
                self.account_indicator=QPushButton(self); self._account_generation=0
            def closeEvent(self,event):
                QMainWindow.closeEvent(self,event)
        view=Host(); self.addCleanup(close_fixture, view)
        jobs=[]
        with patch.object(gui,'run_background',side_effect=lambda action,done:jobs.append(done)),patch.object(gui.tempo_hub,'token','one'):
            view.refresh_account_indicator(); self.assertEqual(view.account_indicator.text(),'Checking account…')
            view.refresh_account_indicator()
            jobs.pop(0)('old',None); self.assertEqual(view.account_indicator.text(),'Checking account…')
            with patch.object(gui.tempo_hub,'get_account_display_name',return_value='alice'):
                jobs.pop(0)('hf:alice',None)
            self.assertEqual(view.account_indicator.text(),'Account: alice')
            view.refresh_account_indicator(); jobs.pop(0)(None,ConnectionError('offline'))
            self.assertEqual(view.account_indicator.text(),'Account not verified')
        with patch.object(gui.tempo_hub,'token',None):view.refresh_account_indicator()
        self.assertEqual(view.account_indicator.text(),'Signed out')
        self.assertEqual(gui.display_cell_name('cell_00023'),'Cell 00023')

    def test_dashboard_account_indicator_remains_visible_and_opens_account_setup(self):
        import maintainer_tab
        original=APP.font()
        try:
            for size in (12,18,24):
                APP.setFont(QFont('Arial',size))
                with patch.object(gui,'run_background',return_value=None),patch.object(maintainer_tab,'run_background',return_value=None), \
                     patch.object(gui.tempo_backend,'UNIVERSAL_DATASET_DIR',str(self.root)), \
                     patch.object(gui.tempo_hub,'cached_lock_manifest',return_value={'images':{}}), \
                     patch.object(gui.tempo_hub,'token',None):
                    view=gui.TempoDashboard()
                self.addCleanup(close_fixture, view)
                view.tab_capture.timer.stop();view.tab_maintainer.timer.stop()
                view.tab_annotator.lease_timer.stop();view.tab_annotator._sync_timer.stop()
                for width,height in ((960,620),(1100,760),(1700,950)):
                    settle(view,width,height)
                    button=view.account_indicator
                    self.assertTrue(button.isVisible())
                    bounds=QRect(button.mapTo(view,QPoint()),button.size())
                    self.assertTrue(view.rect().contains(bounds))
                    with patch.object(view.tab_settings,'run_account_setup') as setup:
                        button.click();setup.assert_called_once()
                view.close()
        finally:APP.setFont(original)


    def test_application_tools_and_account_buttons_keep_keyboard_focus(self):
        settings = gui.SettingsTab(); self.addCleanup(close_fixture, settings)
        self.assertEqual(settings.btn_tools.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        self.assertTrue(all(btn.focusPolicy() != Qt.FocusPolicy.NoFocus for btn in settings.tools_menu.buttons))

        with patch.object(gui, 'run_background', return_value=None), \
             patch.object(gui.tempo_backend, 'UNIVERSAL_DATASET_DIR', str(self.root)), \
             patch.object(gui.tempo_hub, 'token', None):
            dashboard = gui.TempoDashboard(); self.addCleanup(close_fixture, dashboard)
        dashboard.tab_capture.timer.stop(); dashboard.tab_maintainer.timer.stop()
        dashboard.tab_annotator.lease_timer.stop(); dashboard.tab_annotator._sync_timer.stop()
        self.assertEqual(dashboard.account_indicator.focusPolicy(), Qt.FocusPolicy.StrongFocus)

    def test_guidelines_wrap_at_large_font_without_horizontal_scrolling(self):
        from PyQt6.QtWidgets import QScrollArea
        original = APP.font()
        try:
            for size in (12, 18, 24):
                APP.setFont(QFont('Arial', size))
                view = gui.GuidelinesDialog(); self.addCleanup(close_fixture, view)
                for width in (720, 960, 1500):
                    with self.subTest(font=size, width=width):
                        settle(view, width, 620)
                        self.assertEqual(view.width(), width)
                        scroll = view.findChild(QScrollArea)
                        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                        instruction = next(label for label in view.findChildren(QLabel) if label.text().startswith('Left-click'))
                        self.assertTrue(instruction.wordWrap())
                        self.assertGreaterEqual(instruction.height(), instruction.heightForWidth(instruction.width()))
                        examples = view.findChildren(gui.GuidelineIllustration)
                        self.assertEqual(len(examples), 5)
                        for example in examples:
                            self.assertEqual(example.pixmap().size(), example.size())
                            self.assertLessEqual(example.width(), example.parentWidget().contentsRect().width())
                            scroll.ensureWidgetVisible(example, 0, 0)
                            APP.processEvents()
                            bounds = QRect(example.mapTo(scroll.viewport(), QPoint(0, 0)), example.size())
                            self.assertTrue(scroll.viewport().rect().contains(bounds))
                view.close()
        finally:
            APP.setFont(original)


if __name__=='__main__':unittest.main()
