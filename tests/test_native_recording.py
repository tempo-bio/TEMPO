"""Pixel fidelity, timing and opt-in retention using isolated recordings."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import cv2
import numpy as np
from tempo_recording import NativeWriter,open_recording,timeline,display_frame,sha256,finalize_retention
from tempo_processing import ProcessingResult,atomic_write


class NativeRecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def record(self,retain=False,cells=False,fmt='native'):
        path=self.root/'field.tif'
        writer=NativeWriter(path,100,retain_original=retain,retain_cells=cells,export_format=fmt,owned=True)
        values=[]
        for index in range(12):
            frame=(np.arange(64*64,dtype=np.uint16).reshape(64,64)*8+index)
            values.append(frame)
            writer.write(frame,camera_time_ms=100+index*10,camera_frame_id=index)
        writer.close()
        return path,values

    def result(self,path):
        output=self.root/'results';output.mkdir()
        result=ProcessingResult(str(path),str(output),outcome='success')
        (output/'trace.csv').write_text('time,amplitude\n0,0\n')
        atomic_write(output/'processing.json',dict(result=result.to_dict(),
            provenance=dict(input_sha256=sha256(path)),artifacts={'trace.csv':sha256(output/'trace.csv')}))
        return result,output

    def test_exact_native_pixels_seek_and_camera_timing(self):
        path,frames=self.record();cap=open_recording(path)
        try:
            self.assertEqual(cap.get(cv2.CAP_PROP_FRAME_COUNT),12)
            for expected in frames:
                ok,actual=cap.read();self.assertTrue(ok);np.testing.assert_array_equal(actual,expected)
            cap.set(cv2.CAP_PROP_POS_FRAMES,2)
            np.testing.assert_array_equal(cap.read()[1],frames[2])
        finally:cap.release()
        values,basis=timeline(path,12,100)
        self.assertEqual(basis,'camera');np.testing.assert_equal(values,np.arange(12)*10)

    def test_color_is_stored_and_read_with_correct_channel_meaning(self):
        path=self.root/'color.tif';frame=np.zeros((32,32,3),np.uint8);frame[...,0]=21;frame[...,2]=183
        writer=NativeWriter(path,50);writer.write(frame);writer.close()
        cap=open_recording(path)
        try:np.testing.assert_array_equal(cap.read()[1],frame)
        finally:cap.release()

    def test_no_video_retained_only_after_verified_success(self):
        path,_=self.record();result,output=self.result(path)
        finalize_retention(path,output,result=result,expected_source=sha256(path))
        self.assertFalse(path.exists());self.assertTrue((output/'trace.csv').exists())
        self.assertFalse(list(output.glob('*.tif')));self.assertFalse(list(output.glob('*.avi')))
        self.assertEqual(json.loads((output/'recording.json').read_text())['cleanup'],'completed')

    def test_original_retention_is_independent_from_cell_videos(self):
        path,frames=self.record(retain=True);result,output=self.result(path)
        finalize_retention(path,output,result=result,expected_source=sha256(path))
        self.assertFalse(path.exists());cap=open_recording(output/'field.tif')
        try:np.testing.assert_array_equal(cap.read()[1],frames[0])
        finally:cap.release()
        self.assertFalse(list(output.glob('masked_*')))

    def test_all_four_retention_choices_keep_measurements_and_only_requested_videos(self):
        from tempo_backend import process_assay_result,finalize_processing
        base=self.root
        for original in (False,True):
            for cells in (False,True):
                with self.subTest(original=original,cells=cells):
                    self.root=base/('%s-%s'%(original,cells));self.root.mkdir()
                    path,_=self.record(retain=original,cells=cells)
                    regions=[dict(id='cell_1',polygon=[[2,2],[60,2],[60,60],[2,60]],class_id=0)]
                    result=process_assay_result(str(path),cells_data=regions,output_root=str(self.root/'processed'),_native=True)
                    self.assertTrue(result.resolved,result.error)
                    output=Path(result.output_dir);finalize_processing(result)
                    self.assertFalse(path.exists())
                    self.assertEqual((output/'field.tif').exists(),original)
                    self.assertEqual((output/'masked_cell_1.tif').exists(),cells)
                    self.assertTrue((output/'cell_1_raw_kinematics.csv').exists())
                    self.assertTrue((output/'cell_1_mask.tif').exists())
                    self.assertTrue((output/'reference.tif').exists())
        self.root=base

    def test_durable_retention_works_with_windows_writable_descriptor_requirement(self):
        path,frames=self.record(retain=True);result,output=self.result(path)
        expected=sha256(path);actual_fsync=os.fsync
        def writable_sync(fd):
            # Zero bytes preserve content, but reject a read-only descriptor on
            # POSIX too, reproducing the Windows _commit requirement.
            os.write(fd,b'')
            return actual_fsync(fd)
        with patch('tempo_recording.os.fsync',side_effect=writable_sync):
            finalize_retention(path,output,result=result,expected_source=expected)
        self.assertFalse(path.exists())
        self.assertEqual(sha256(output/'field.tif'),expected)
        cap=open_recording(output/'field.tif')
        try:np.testing.assert_array_equal(cap.read()[1],frames[0])
        finally:cap.release()

    def test_result_flush_failure_retains_temporary_footage(self):
        path,_=self.record();result,output=self.result(path);expected=sha256(path)
        with patch('tempo_recording.os.fsync',side_effect=OSError('Storage flush failed')):
            with self.assertRaisesRegex(OSError,'Storage flush failed'):
                finalize_retention(path,output,result=result,expected_source=expected)
        self.assertEqual(sha256(path),expected)
        self.assertTrue(Path(str(path)+'.frames.jsonl').exists())

    def test_failure_or_changed_artifacts_keep_source(self):
        path,_=self.record();result,output=self.result(path);digest=sha256(path)
        result.outcome='failure'
        finalize_retention(path,output,result=result,expected_source=digest)
        self.assertTrue(path.exists());result.outcome='success'
        (output/'trace.csv').write_text('changed')
        with self.assertRaises(ValueError):finalize_retention(path,output,result=result,expected_source=digest)
        self.assertTrue(path.exists())

    def test_imported_input_is_never_deleted(self):
        path=self.root/'external.tif';writer=NativeWriter(path,100);writer.write(np.zeros((32,32),np.uint16));writer.close()
        result,output=self.result(path)
        finalize_retention(path,output,result=result,expected_source=sha256(path))
        self.assertTrue(path.exists())

    def test_missing_timing_is_not_guessed_and_invalid_camera_time_rejected(self):
        with self.assertRaises(ValueError):timeline(self.root/'absent',3,0)
        path,_=self.record();journal=Path(str(path)+'.frames.jsonl')
        lines=journal.read_text().splitlines();row=json.loads(lines[2]);row['camera_time_ms']=100
        lines[2]=json.dumps(row);journal.write_text('\n'.join(lines))
        with self.assertRaises(ValueError):timeline(path,12,100)

    def test_fixed_conversion_does_not_invent_frame_contrast(self):
        a=display_frame(np.array([[1000,2000]],np.uint16))
        b=display_frame(np.array([[1000,4000]],np.uint16))
        self.assertEqual(a[0,0],b[0,0]);self.assertLess(int(a.max()),255)

    def test_native_processing_extracts_traces_without_video_outputs(self):
        from tempo_backend import process_assay_result,finalize_processing
        path,_=self.record();cells=[dict(id='cell_1',polygon=[[2,2],[60,2],[60,60],[2,60]],class_id=0)]
        result=process_assay_result(str(path),cells_data=cells,output_root=str(self.root/'processed'),_native=True)
        self.assertTrue(result.resolved,result.error)
        self.assertTrue(path.exists(),'Processing must not clean up before history is saved.')
        finalize_processing(result)
        self.assertFalse(path.exists())
        self.assertTrue((Path(result.output_dir)/'cell_1_raw_kinematics.csv').exists())
        self.assertFalse(list(Path(result.output_dir).glob('*.avi')))

    def test_live_writer_cannot_be_processed_or_recovered(self):
        from tempo_recording import recover_capture
        path=self.root/'active.tif';writer=NativeWriter(path,100,owned=True)
        writer.write(np.zeros((32,32),np.uint16))
        try:
            with self.assertRaises(RuntimeError):open_recording(path)
            with self.assertRaises(BlockingIOError):recover_capture(path,self.root/'copy.tif')
        finally:writer.close('interrupted')

    def test_recovery_preserves_original_and_marks_new_capture(self):
        from tempo_recording import recover_capture,read_metadata,set_declared_rate,declared_rate
        path=self.root/'partial.tif';writer=NativeWriter(path,100,owned=True)
        frames=[np.full((32,32),index,np.uint16) for index in range(4)]
        for i,frame in enumerate(frames):writer.write(frame,camera_time_ms=i*10)
        writer.close('Camera disconnected')
        set_declared_rate(path,97.,'reviewer')
        digest=sha256(path);recovered=self.root/'recovered.tif'
        recover_capture(path,recovered)
        self.assertEqual(sha256(path),digest)
        info=read_metadata(recovered)
        self.assertTrue(info['recovery_copy']);self.assertIn('recovered_partial',info['protocol_flags'])
        cap=open_recording(recovered)
        try:
            for frame in frames:np.testing.assert_array_equal(cap.read()[1],frame)
        finally:cap.release()
        self.assertEqual(info['frames'],4);self.assertEqual(info['status'],'completed')
        self.assertEqual(declared_rate(recovered),97.)

    def test_compatibility_retention_declares_conversion_and_preserves_timing(self):
        from tempo_recording import set_declared_rate,read_metadata,declared_rate
        path,_=self.record(retain=True,fmt='compatibility')
        set_declared_rate(path,97.,'reviewer')
        result,output=self.result(path)
        finalize_retention(path,output,result=result,expected_source=sha256(path))
        retained=output/'field.avi'
        self.assertTrue(retained.is_file());self.assertFalse(path.exists())
        self.assertEqual(declared_rate(retained),97.)
        info=read_metadata(retained)
        self.assertEqual(info['dtype'],'uint8');self.assertEqual(info['source_dtype'],'uint16')
        self.assertEqual(info['conversion'],'fixed_full_scale_8bit')

    def test_odd_sized_compatibility_frames_keep_every_source_edge_and_declare_padding(self):
        from tempo_recording import read_metadata
        path=self.root/'odd.tif';writer=NativeWriter(path,100,retain_original=True,export_format='compatibility',owned=True)
        for i in range(4):writer.write(np.full((33,35),30000+i,np.uint16))
        writer.close();result,output=self.result(path)
        finalize_retention(path,output,result=result,expected_source=sha256(path))
        retained=output/'odd.avi';info=read_metadata(retained)
        self.assertEqual((info['padding_bottom_px'],info['padding_right_px']),(1,1))
        cap=open_recording(retained)
        try:
            ok,frame=cap.read();self.assertTrue(ok)
            self.assertEqual(frame.shape[:2],(33,35))
            self.assertGreater(int(frame[-1,-1,0]),100)
        finally:cap.release()

    def test_completed_processing_is_reused_when_history_or_cleanup_must_be_retried(self):
        from tempo_backend import process_assay_result
        path,_=self.record();cells=[dict(id='cell_1',polygon=[[2,2],[60,2],[60,60],[2,60]],class_id=0)]
        first=process_assay_result(str(path),cells_data=cells,output_root=str(self.root/'processed'),_native=True)
        with patch('tempo_backend.process_cell_masks',side_effect=AssertionError('Completed extraction must not repeat.')):
            second=process_assay_result(str(path),cells_data=cells,output_root=str(self.root/'processed'),_native=True)
        self.assertTrue(second.resolved,second.error);self.assertEqual(second.output_dir,first.output_dir)

    def test_flush_failure_keeps_capture_and_releases_its_ownership_lock(self):
        import os
        from tempo_recording import read_metadata
        path=self.root/'interrupted.tif';writer=NativeWriter(path,100,owned=True)
        writer.write(np.ones((32,32),np.uint16))
        actual_fsync=os.fsync;calls=[]
        def fail_once(fd):
            calls.append(fd)
            if len(calls)==1:raise OSError('Storage flush failed')
            return actual_fsync(fd)
        with patch('tempo_recording.os.fsync',side_effect=fail_once):
            with self.assertRaises(OSError):writer.close()
        self.assertTrue(path.exists());self.assertEqual(read_metadata(path)['status'],'failed')
        cap=open_recording(path)
        try:self.assertTrue(cap.read()[0])
        finally:cap.release()


class QualityEvidenceTests(unittest.TestCase):
    def test_sensor_saturation_uses_active_bits_and_invalid_profile_is_unavailable(self):
        from tempo_quality import assess,METHOD
        frame=np.random.default_rng(9).integers(0,4096,(64,64),dtype=np.uint16);frame[:8]=4095
        result=assess(frame,sensor_bits=12)
        self.assertIn('saturation',result['flags'])
        malformed=dict(method=METHOD,signature='optics',center=[0],scale=[0],weights=[],id='bad')
        result=assess(frame,'optics',malformed)
        self.assertEqual(result['focus'],'unavailable');self.assertIn('invalid',result['reason'])

    def test_per_recording_decisions_preserve_imported_csv_and_filter_summaries(self):
        import pandas as pd
        from tempo_quality import decide_dataset,dataset_decisions
        from tempo_analysis import read_results,summarize_cells
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'results.csv'
            pd.DataFrame([dict(Recording_ID=recording,Well_ID='A01',FOV_ID=recording,Cell_ID='cell_1',Peak_Amplitude_au=number)
                          for recording,number in (('one',2),('two',8))]).to_csv(path,index=False)
            original=path.read_bytes()
            decide_dataset(path,{'one':dict(included=False,reason='Blurred detail'),
                                 'two':dict(included=True,reason='Retain saturation warning')},'reviewer')
            self.assertEqual(path.read_bytes(),original)
            data=read_results(path);means,_=summarize_cells(data)
            self.assertEqual(list(means.Recording_ID),['two'])
            saved=dataset_decisions(path)['recordings']
            self.assertEqual(saved['one']['decisions'][0]['reason'],'Blurred detail')
            self.assertEqual(saved['two']['decisions'][0]['reason'],'Retain saturation warning')
            decide_dataset(path,{'one':dict(included=True,reason='Reincluded for sensitivity analysis')},'reviewer')
            self.assertEqual(len(dataset_decisions(path)['recordings']['one']['decisions']),2)
            self.assertEqual(len(summarize_cells(read_results(path))[0]),2)
            path.write_text(path.read_text()+'\n')
            with self.assertRaises(ValueError):dataset_decisions(path)

    def test_profile_missing_and_flat_frame_never_assert_sharp(self):
        from tempo_quality import assess
        self.assertEqual(assess(np.zeros((64,64),np.uint8))['focus'],'unavailable')
        frame=np.random.default_rng(4).integers(0,256,(64,64),dtype=np.uint8)
        self.assertEqual(assess(frame)['focus'],'unavailable')

    def test_inclusion_decisions_do_not_remove_assessment(self):
        from tempo_quality import assess,store_assessment,decide,columns
        with tempfile.TemporaryDirectory() as root:
            assessment=assess(np.zeros((64,64),np.uint8));store_assessment(root,assessment)
            self.assertTrue(columns(json.loads((Path(root)/'quality.json').read_text()))['Quality_Included'])
            decide(root,False,'reviewer','blur');document=decide(root,True,'reviewer','retain for comparison')
            self.assertEqual(document['assessments'],[assessment]);self.assertEqual(len(document['decisions']),2)
            self.assertTrue(columns(document)['Quality_Included'])

    def test_profile_rejects_related_fields_across_partitions(self):
        from tempo_quality import fit_profile
        frame=np.zeros((64,64),np.uint8)
        records=[dict(field='same',partition=part,label=label,frame=frame)
                 for part in ('train','test') for label in ('sharp','out_of_focus')]
        with self.assertRaises(ValueError):fit_profile(records,'camera')
