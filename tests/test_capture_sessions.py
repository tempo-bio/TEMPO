"""Session accounting and geometry tests write only isolated temporary recordings."""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from tempo_capture import CaptureSession,FieldCalibration,search_grid,read_capture_metadata,capture_columns,FocusPause
from tempo_stimulation import PacerStatus,StimulusParameters


def status(mode='PACING',parameters=None):
    return PacerStatus(mode,parameters or StimulusParameters(),False,100,2000,4.77)


class FocusResponseTests(unittest.TestCase):
    def test_explicit_response_within_window_does_not_expire_while_camera_drains(self):
        now=[0.]
        pause=FocusPause(clock=lambda:now[0])
        now[0]=59.;pause.request_resume();now[0]=65.
        self.assertFalse(pause.expired)

    def test_deadline_requires_resume_not_incidental_activity(self):
        now=[0.];pause=FocusPause(clock=lambda:now[0])
        now[0]=60.;self.assertTrue(pause.expired)
        pause.request_resume();self.assertTrue(pause.expired)

    def test_unstimulated_pause_has_no_pacing_deadline(self):
        now=[0.];pause=FocusPause(None,clock=lambda:now[0]);now[0]=600.
        self.assertFalse(pause.expired)


class CaptureSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name); self.now=0.
        self.parameters=StimulusParameters()
        self.session=CaptureSession(self.root,'experiment','A01',self.parameters,clock=lambda:self.now,channel=0)
    def start_ready(self):
        self.session.start(status()); self.now=30.; self.session.begin_imaging()
    def record(self,name,field,count=5,position=None,error='',footprint=None):
        path=self.root/(name+'.avi')
        self.session.begin_field(path,field,dict(live_count=count),position,footprint)
        self.session.complete_field(100,error)
        return read_capture_metadata(path)

    def test_no_capture_before_acknowledged_pacing_or_preparation(self):
        with self.assertRaises(ValueError): self.session.start(status('REST_DISCHARGE'))
        self.session.start(status())
        with self.assertRaises(ValueError): self.session.begin_field(self.root/'one.avi','FOV001')

    def test_continuous_well_session_and_elapsed_before_each_field(self):
        self.start_ready()
        first=self.record('one','FOV001')
        self.now=45.; second=self.record('two','FOV002')
        self.assertEqual(first['prepacing_elapsed_s'],30)
        self.assertEqual(second['prepacing_elapsed_s'],45)
        self.assertEqual(first['session_id'],second['session_id'])
        self.assertEqual(self.session.phase,'between_fields')
        self.assertEqual(self.session.document['healthy_cell_estimate'],10)
        self.session.finish(status('REST_DISCHARGE'))
        self.assertEqual(self.session.phase,'completed')
        self.assertTrue(read_capture_metadata(self.root/'one.avi')['output_confirmed'])

    def test_focus_restart_tracks_a_new_pacing_period_without_counting_stopped_time(self):
        self.start_ready();self.record('first','FOV001')
        self.now=45.;self.session._transition('focus_paused')
        self.now=150.;self.session.restart_pacing(status())
        self.now=180.;self.session.begin_imaging()
        result=self.record('after_restart','FOV002')
        self.assertEqual(result['prepacing_elapsed_s'],30.)
        self.assertEqual(result['pacing_epoch'],2)
        self.assertEqual(self.session.document['initial_prepacing_s'],30.)
        self.assertEqual(result['configured_prepacing_completed_s'],30.)

    def test_repeat_identifiers_overlap_and_failed_fields_do_not_inflate_target(self):
        self.start_ready()
        self.record('one','FOV001',position=(0,0),footprint=(100,100))
        self.record('repeat','FOV001',count=100,position=(500,500),footprint=(100,100))
        overlap=self.record('overlap','FOV002',count=100,position=(50,0),footprint=(100,100))
        self.record('failed','FOV003',count=100,error='Disk full')
        self.record('adjacent','FOV004',position=(100,0),footprint=(100,100))
        self.assertTrue(overlap['duplicate'])
        self.assertEqual(self.session.document['healthy_cell_estimate'],10)

    def test_unknown_output_finishes_as_interrupted_and_preserves_recordings(self):
        self.start_ready(); self.record('one','FOV001')
        self.session.finish(None,'USB lost')
        self.assertEqual(self.session.phase,'interrupted')
        self.assertEqual(self.session.document['fields'][0]['status'],'completed')
        self.assertFalse(read_capture_metadata(self.root/'one.avi')['output_confirmed'])

    def test_unstimulated_and_legacy_metadata_remain_supported(self):
        session=CaptureSession(self.root,'experiment','A01'); session.start()
        session.begin_field(self.root/'unstimulated.avi','FOV001'); session.complete_field(10)
        session.finish()
        metadata=read_capture_metadata(self.root/'unstimulated.avi')
        self.assertFalse(capture_columns(metadata)['Stimulation_Enabled'])
        self.assertIsNone(read_capture_metadata(self.root/'legacy.avi'))
        self.assertIsNone(capture_columns(None)['Stimulation_Enabled'])

    def test_metadata_recording_identity_is_validated(self):
        self.start_ready(); self.record('one','FOV001')
        path=self.root/'one.avi.capture.json'; document=json.loads(path.read_text()); document['recording_id']='other'
        path.write_text(json.dumps(document))
        with self.assertRaises(ValueError): read_capture_metadata(self.root/'one.avi')

    def test_grid_is_serpentine_nonoverlapping_and_bounded(self):
        calibration=FieldCalibration(((1,0),(0,1)),(100,50),'optics')
        points=search_grid((0,0),(300,100),calibration,5)
        self.assertEqual(points,[(50,25),(150,25),(250,25),(250,75),(150,75)])
        self.assertFalse(calibration.matches('changed',(100,50)))
        with self.assertRaises(ValueError): search_grid((0,0),(99,49),calibration,5)

    def test_rotated_calibration_uses_conservative_footprints(self):
        calibration=FieldCalibration(((0,-2),(2,0)),(100,50),'optics')
        self.assertEqual(calibration.footprint,(100,200))
        registration=calibration.registration('one','A01','FOV001',(500,400),'session')
        self.assertEqual(registration['pixel_to_well'],[[0.,-2.,550.],[2.,0.,300.]])


if __name__=='__main__': unittest.main()
