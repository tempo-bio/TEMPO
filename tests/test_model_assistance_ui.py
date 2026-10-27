"""Offscreen layer safety for the three model-assistance actions."""
import copy
import unittest
from tests import test_desktop_workflows as desktop


class ModelAssistanceTests(unittest.TestCase):
    setUp = desktop.DesktopTests.setUp

    def open_image(self):
        self.view.refresh_universal_file_list(refresh=False)
        self.view.on_file_selected(self.view.list_files.item(0))
        self.view.lease_timer.stop(); self.view._sync_timer.stop()

    def complete(self, role, boxes):
        request=dict(file=self.view.current_file,universal=self.view.is_universal_mode,
                     boxes=copy.deepcopy(self.view.get_current_boxes()),role=role)
        self.view._auto_pending=request
        self.view.on_auto_annotate_finished(dict(success=True,boxes=boxes),request)

    def test_scout_estimates_cannot_replace_even_readonly_cell_masks(self):
        self.open_image()
        before=copy.deepcopy(self.view.get_current_boxes())
        self.view._current_file_readonly=True
        self.complete('scout',[dict(class_id=0,polygon=[[2,2],[6,2],[6,6],[2,6]],box=[2,2,4,4],center=[4,4])])
        self.assertEqual(self.view.get_current_boxes(),before)
        self.assertIn('Scout estimate',self.view.lbl_auto_status.text())
        self.assertTrue(self.view._scout_items)

    def test_star_proposals_preserve_cell_geometry_and_have_nuclear_kind(self):
        self.open_image()
        before=copy.deepcopy(self.view.get_current_boxes())
        self.complete('stardist',[dict(class_id=2,object_type='nucleus',polygon=[[2,2],[6,2],[6,6],[2,6]])])
        result=self.view.get_current_boxes()
        self.assertEqual(result[:len(before)],before)
        self.assertEqual(result[-1]['object_type'],'nucleus')
        self.assertTrue(result[-1]['proposal'])
        self.assertIn('nuclear proposals',self.view.lbl_auto_status.text())

    def test_actions_and_categories_have_no_retired_background_or_segmentation(self):
        self.assertFalse(hasattr(self.view,'btn_auto_yolo'))
        self.assertEqual([item['name'] for item in self.view.class_config],['healthy','dead','nuclei'])
        self.assertEqual(self.view.btn_auto_annotate,self.view.btn_auto_cellpose)

    def test_local_save_retains_multiple_nuclei_and_drops_legacy_background(self):
        import json
        self.view.is_universal_mode=False
        self.view.current_dir=str(self.root)
        path=str(self.root/'a.png')
        shapes=[dict(class_id=0,polygon=[[1,1],[30,1],[30,30],[1,30]])]
        for x in (3,12,21):
            shapes.append(dict(class_id=2,object_type='nucleus',polygon=[[x,3],[x+3,3],[x+3,7],[x,7]]))
        self.view.annotations={path:shapes}
        self.view.save_annotations_to_disk()
        saved=json.loads((self.root/'annotations.json').read_text())
        self.assertEqual(saved['format'],'tempo-local-annotations-v3')
        self.assertEqual(self.view.read_local_annotations(str(self.root))[path],shapes)
        saved['images']['a.png'].append(dict(class_id=2,polygon=[[40,40],[50,40],[50,50]]))
        (self.root/'annotations.json').write_text(json.dumps(saved))
        self.assertEqual(self.view.read_local_annotations(str(self.root))[path],shapes)

    def test_edits_during_nuclear_sync_are_retried_after_the_older_save_finishes(self):
        from unittest.mock import patch
        self.open_image()
        self.view._nuclear_only=True
        self.view.universal_annotations['a.png']['nuclear_sync_pending']=True
        with patch.object(desktop.tempo_gui,'run_background') as background:
            self.view.sync_nuclear_layer()
        self.view.get_current_boxes().append(dict(class_id=2,object_type='nucleus',polygon=[[3,3],[6,3],[6,6],[3,6]]))
        background.call_args.args[1]({'revision':1},None)
        self.assertTrue(self.view.universal_annotations['a.png']['nuclear_sync_pending'])
        self.assertTrue(self.view._sync_timer.isActive())
        self.assertEqual(self.view.lbl_sync_status.text(),'Nuclear edits pending sync')

    def test_expired_nuclear_reservation_keeps_edits_and_exposes_renewal(self):
        from unittest.mock import patch
        from tempo_hub import TempoHub
        self.open_image()
        before=copy.deepcopy(self.view.get_current_boxes())
        self.view._nuclear_only=True
        self.view.list_classes.setCurrentRow(2)
        self.view.lock_manifest['nuclear_leases']={'a.png':dict(TempoHub._lease('Alice'),expires_at='2000-01-01T00:00:00+00:00')}
        self.view._apply_current_lease()
        self.assertTrue(self.view._current_file_readonly)
        self.assertFalse(self.view.btn_renew.isHidden())
        self.assertFalse(self.view.btn_nuclear_submit.isEnabled())
        self.assertTrue(self.view.btn_publish.isHidden())
        self.assertTrue(self.view.lbl_readiness_badge.isHidden())
        self.assertEqual(self.view.auto_availability.text(),'')
        def renew(name,**kwargs):
            self.view.lock_manifest['nuclear_leases'][name]=TempoHub._lease('Alice')
        with patch.object(desktop.tempo_gui.tempo_hub,'renew_nuclear_annotations',side_effect=renew):
            self.view.renew_current_lease()
        self.assertFalse(self.view._current_file_readonly)
        self.assertEqual(self.view.get_current_boxes(),before)
