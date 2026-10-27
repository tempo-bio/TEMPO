"""Portable result exports retain decision history and reject incomplete bundles."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import pandas as pd
import numpy as np
from tempo_analysis_io import export_bundle,reanalyze_traces
from tempo_quality import decide_dataset


class AnalysisBundleTests(unittest.TestCase):
    def test_trace_reanalysis_works_without_video_and_retains_review_lineage(self):
        from tempo_processing import atomic_write,ProcessingResult
        from tempo_analysis import read_results
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'Assay_test_A01_FOV001_100fps_123';source.mkdir()
            original=ProcessingResult(str(root/'deleted-video.tif'),str(source),outcome='success')
            cells=[dict(id='cell_1',polygon=[[0,0],[20,0],[20,20],[0,20]],class_id=0,centroid_x=10,centroid_y=10)]
            atomic_write(source/'processing.json',dict(result=original.to_dict(),cells=cells,provenance={'input_sha256':'old'},artifacts={}))
            times=np.arange(500)*10.
            wave=sum(30*np.exp(-((times-center)/80)**2) for center in (600,1600,2600,3600))
            trace=source/'cell_1_raw_kinematics.csv'
            pd.DataFrame({'Time (ms)':times,'Contraction (Source a.u.)':wave,
                'Area-Norm (Source a.u.)':wave/400,'Fractional (Source dI/I0)':wave/100}).to_csv(trace,index=False)
            csv=source/'Master_Cell_Data.csv'
            pd.DataFrame([dict(Recording_ID=source.name,Well_ID='A01',FOV_ID='FOV001',Cell_ID='cell_1',Peak_Amplitude_au=30)]).to_csv(csv,index=False)
            decide_dataset(csv,{source.name:dict(included=False,reason='Original review')},'reviewer')
            before=trace.read_bytes();config={key:dict(value=value) for key,value in
                dict(enable_asls_detrending=False,enable_lowpass_filter=False).items()}
            result=reanalyze_traces(source,config,output_root=root/'selected-data-folder')
            self.assertTrue(result.resolved,result.error)
            self.assertEqual(trace.read_bytes(),before);self.assertNotEqual(result.output_dir,str(source))
            output=Path(result.output_dir)
            self.assertEqual(output.parent,root/'selected-data-folder')
            quality=json.loads((output/'quality.json').read_text())
            inherited=quality['source_dataset_decisions']['recordings'][source.name]['decisions']
            self.assertEqual(inherited[-1]['reason'],'Original review')
            data=read_results(result.results_csv)
            self.assertFalse(data.Quality_Included.any())
            self.assertTrue(data.Fractional_Anisotropy.isna().all())
            self.assertGreater(len(data),0)

    def test_export_preserves_reviewer_decisions_and_checksums_without_footage(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);csv=root/'Master_Cell_Data.csv'
            pd.DataFrame([dict(Recording_ID='field',Well_ID='A01',FOV_ID='FOV001',Cell_ID='cell_1',
                Peak_Amplitude_au=5,Trace_File='cell_1_raw_kinematics.csv')]).to_csv(csv,index=False)
            trace=root/'cell_1_raw_kinematics.csv';trace.write_text('Time (ms),Contraction (Raw a.u.)\n0,5\n')
            (root/'field.tif').write_bytes(b'original footage')
            decide_dataset(csv,{'field':dict(included=False,reason='Poor intracellular detail')},'reviewer')
            original=csv.read_bytes();destination=root/'analysis.zip'
            actual_fsync=os.fsync
            def writable_sync(fd):
                os.write(fd,b'')  # Reproduce Windows _commit's writable-handle requirement.
                return actual_fsync(fd)
            with patch('tempo_analysis_io.os.fsync',side_effect=writable_sync):
                export_bundle(csv,destination)
            with zipfile.ZipFile(destination) as archive:
                self.assertNotIn('field.tif',archive.namelist())
                manifest=json.loads(archive.read('bundle.json'))
                for name,digest in manifest['files'].items():
                    self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(),digest)
                saved=json.loads(archive.read(csv.name+'.quality.json'))
                self.assertEqual(saved['recordings']['field']['decisions'][-1]['reason'],'Poor intracellular detail')
                self.assertEqual(saved['source_sha256'],hashlib.sha256(archive.read(csv.name)).hexdigest())
                self.assertIn(b'False',archive.read(csv.name))
            self.assertEqual(csv.read_bytes(),original)

    def test_missing_trace_does_not_replace_previous_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);csv=root/'results.csv';destination=root/'previous.zip'
            pd.DataFrame([dict(Recording_ID='field',Well_ID='A01',Cell_ID='1',Trace_File='missing.csv')]).to_csv(csv,index=False)
            destination.write_bytes(b'previous export')
            with self.assertRaises(FileNotFoundError):export_bundle(csv,destination)
            self.assertEqual(destination.read_bytes(),b'previous export')
