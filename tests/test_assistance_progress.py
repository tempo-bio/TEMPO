"""Observed model stages and waiting messages in the active Annotator panel."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest

from PyQt6.QtWidgets import QApplication

from tempo_assistance_progress import ModelAssistanceProgress


APP = QApplication.instance() or QApplication([])


class AssistanceProgressTests(unittest.TestCase):
    def test_all_three_models_show_specific_real_stages_and_elapsed_waiting(self):
        expected = {
            'scout': ('Count', 'count estimate'),
            'stardist': ('Find nuclei', 'nuclear proposals'),
            'cellpose': ('Outline cells', 'cell outlines'),
        }
        for role, (run_label, result_label) in expected.items():
            with self.subTest(role=role):
                view = ModelAssistanceProgress()
                view.begin(role, now=100)
                self.assertTrue(view._timer.isActive())
                self.assertIn('Prepare image', view.steps[0].text())
                view.note_stage('image', now=105)
                view.refresh(now=136)
                self.assertIn('Waiting for model preparation', view.detail.text())
                view.note_stage('inference', now=140)
                self.assertIn(run_label.lower(), view.steps[1].text().lower())
                self.assertTrue(view.steps[0].text().startswith('✓'))
                view.refresh(now=171)
                self.assertIn('Waiting for a model result', view.detail.text())
                self.assertEqual(view.elapsed.text(), '1m 11s elapsed')
                view.note_stage('result', now=172)
                self.assertIn(result_label, view.steps[2].text())
                self.assertTrue(view.steps[1].text().startswith('✓'))
                view.refresh(now=210)
                self.assertNotIn('Waiting for a model result', view.detail.text())
                view.finish()
                self.assertFalse(view._timer.isActive())
                self.assertTrue(view.isHidden())
                view.deleteLater()

    def test_stale_stage_cannot_move_completed_work_backwards(self):
        view = ModelAssistanceProgress()
        view.begin('cellpose', now=0)
        view.note_stage('result', now=4)
        view.note_stage('weights', now=5)
        self.assertTrue(view.steps[2].text().startswith('●'))
        view.finish()
        view.deleteLater()


if __name__ == '__main__':
    unittest.main()
