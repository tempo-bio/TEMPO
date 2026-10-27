"""Portable result exports and new trace-analysis revisions without source video."""
from pathlib import Path
import io
import json
import os
import shutil
import tempfile
import zipfile
import numpy as np
import pandas as pd
from tempo_processing import atomic_write,ProcessingResult,CancellationToken
from tempo_recording import sha256


def export_bundle(csv_path,destination,cancel=None):
    from tempo_analysis import read_results
    from tempo_quality import dataset_decisions
    csv_path,destination=Path(csv_path),Path(destination)
    cancel=cancel or CancellationToken()
    base=csv_path.resolve().parent
    data=read_results(csv_path)
    table=data.to_csv(index=False).encode()
    files={csv_path.name:table}
    for column in ('Trace_File','Spatial_Metadata_File'):
        if column not in data:continue
        for value in data[column].dropna().unique():
            if not isinstance(value,str) or not value:continue
            path=(base/value).resolve()
            if not path.is_relative_to(base):raise ValueError('Export references a file outside the analysis folder.')
            if not path.is_file():raise FileNotFoundError('Referenced analysis file is missing: '+value)
            files[str(path.relative_to(base))]=path
    for pattern in ('*_raw_kinematics.csv','*_transients_summary.csv','*_mask.tif'):
        for path in base.glob(pattern):
            if path.is_symlink():raise ValueError('Analysis exports cannot follow external symbolic links.')
            files[path.name]=path
    for name in ('processing.json','quality.json','recording.json','capture.json','frames.jsonl','spatial.json','reference.tif','kinetics.json','revision.json'):
        path=base/name
        if path.is_file():
            if path.is_symlink():raise ValueError('Analysis export metadata must remain in the analysis folder.')
            files[name]=path
    decisions=dataset_decisions(csv_path)
    if decisions['recordings']:
        import hashlib
        decisions=dict(decisions,original_source_sha256=decisions['source_sha256'],source_sha256=hashlib.sha256(table).hexdigest())
        files[csv_path.name+'.quality.json']=json.dumps(decisions,indent=2).encode()
    fd,temporary=tempfile.mkstemp(prefix=destination.name+'.',suffix='.tmp',dir=destination.parent)
    os.close(fd)
    try:
        checksums={}
        with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED,allowZip64=True) as archive:
            import hashlib
            for name,value in files.items():
                cancel.check()
                if isinstance(value,Path):
                    archive.write(value,name);checksums[name]=sha256(value)
                else:
                    archive.writestr(name,value);checksums[name]=hashlib.sha256(value).hexdigest()
            archive.writestr('bundle.json',json.dumps(dict(version=1,results=csv_path.name,
                source_csv_sha256=sha256(csv_path),footage_included=False,files=checksums),indent=2))
        # Windows _commit requires a writable descriptor for fsync.
        with open(temporary,'r+b') as stream:os.fsync(stream.fileno())
        os.replace(temporary,destination)
    finally:Path(temporary).unlink(missing_ok=True)
    return str(destination)


_METRICS=(('Peak #','Peak_Number'),('Time of Peak (ms)','Time_of_Peak_ms'),
    ('Peak Amplitude (a.u.)','Peak_Amplitude_Raw_au'),('Peak Amplitude (Area-Norm a.u.)','Peak_Amplitude_AreaNorm_au'),
    ('Peak Amplitude (Fractional dI/I0)','Peak_Amplitude_Fractional_dI_I0'),
    ('Time-to-Peak (ms)','Time_to_Peak_ms'),('Relaxation Time (ms)','Relaxation_Time_ms'),
    ('Transient Duration (ms)','Transient_Duration_ms'),('CT50 (ms)','CT50_ms'),('CT90 (ms)','CT90_ms'),
    ('RT50 (ms)','RT50_ms'),('RT90 (ms)','RT90_ms'),('Max Contraction Velocity','Max_Contraction_Velocity'),
    ('Max Relaxation Velocity','Max_Relaxation_Velocity'),('Peak-to-Peak Time (ms)','Peak_to_Peak_Time_ms'),
    ('Fractional Anisotropy','Fractional_Anisotropy'),('Contraction Angle (deg)','Contraction_Angle_deg'),
    ('Vector Coherence','Vector_Coherence'))


def reanalyze_traces(source,config,cancel=None,output_root=None):
    """Recompute scalar kinetics in a new revision; no synthetic image/flow claims."""
    from vk_math_engine import analyze_transients,asls_baseline_detrend
    from tempo_backend import _statistics,_save_processing_record
    from tempo_quality import columns
    source=Path(source);cancel=cancel or CancellationToken()
    manifest=json.loads((source/'processing.json').read_text())
    original=manifest['result']
    result=ProcessingResult(original['input_path'])
    paths=list(source.glob('*_raw_kinematics.csv'))
    required=['Time (ms)','Contraction (Source a.u.)','Area-Norm (Source a.u.)','Fractional (Source dI/I0)']
    if not paths:raise ValueError('No saved source measurement traces are available.')
    for path in paths:
        cancel.check()
        if path.is_symlink() or not set(required)<=set(pd.read_csv(path,nrows=0).columns):
            raise ValueError('These traces lack unprocessed source measurements; reanalysis requires original footage.')
    parent=Path(output_root) if output_root is not None else source.parent
    parent.mkdir(parents=True,exist_ok=True)
    output=Path(tempfile.mkdtemp(prefix=source.name+'_trace_',dir=parent))
    result.output_dir=str(output)
    try:
        for name in ('quality.json','recording.json','capture.json','reference.tif','frames.jsonl'):
            if (source/name).is_file():shutil.copy2(source/name,output/name)
        for path in source.glob('*_mask.tif'):shutil.copy2(path,output/path.name)
        if (source/'spatial.json').is_file():
            spatial=json.loads((source/'spatial.json').read_text());spatial['recording_id']=output.name
            atomic_write(output/'spatial.json',spatial)
        quality=json.loads((output/'quality.json').read_text()) if (output/'quality.json').exists() else dict(version=1,assessments=[],decisions=[])
        quality['analysis_flags']=list(set(quality.get('analysis_flags',[]))|{'optical_flow_not_recalculated'})
        from tempo_analysis import read_results
        original_csv=source/'Master_Cell_Data.csv'
        if original_csv.is_file():
            from tempo_quality import dataset_decisions
            quality['source_dataset_decisions']=dataset_decisions(original_csv)
            prior=read_results(original_csv)
            if 'Quality_Included' in prior and len(prior):
                row=prior.iloc[0]
                quality.setdefault('decisions',[]).append(dict(included=str(row['Quality_Included']).lower() in ('true','1'),
                    actor=str(row.get('Quality_Decision_Actor','inherited')),reason='Inherited from '+source.name,
                    source_analysis=source.name))
        atomic_write(output/'quality.json',quality)
        atomic_write(output/'kinetics.json',config)
        def setting(key,default):return config.get(key,{}).get('value',default)
        for path in paths:
            cancel.check();data=pd.read_csv(path)
            values=data[required].to_numpy(dtype=float)
            if not np.isfinite(values).all() or np.any(np.diff(values[:,0])<=0):
                raise ValueError('Saved source trace has invalid timing or values.')
            times=values[:,0];intervals=np.diff(times)
            dt=float(np.median(intervals)) if len(intervals) else None
            if dt is None:raise ValueError('At least two timed samples are required.')
            fps=1000/dt;segments=np.split(np.arange(len(times)),np.flatnonzero(intervals>1.5*dt)+1)
            processed=values[:,1:].copy();metrics=[]
            for indices in segments:
                if len(indices)<5:continue
                if setting('enable_asls_detrending',True) and len(indices)>10:
                    for column in range(3):
                        processed[indices,column]=asls_baseline_detrend(processed[indices,column],
                            lam=setting('asls_lambda',100000.),p=setting('asls_p',.001))[0]
                uniform=np.arange(times[indices[0]],times[indices[-1]]+1e-6,dt)
                sampled=[np.interp(uniform,times[indices],processed[indices,column]).tolist() for column in range(3)]
                rows=analyze_transients(list(zip(uniform,sampled[0])),fps=fps,area_norm_data=sampled[1],fractional_data=sampled[2],
                    cutoff_hz=setting('lowpass_cutoff_hz',18.),enable_lowpass=setting('enable_lowpass_filter',True),
                    savgol_window_pct=setting('savgol_window_pct',7),peak_prominence_pct=setting('peak_prominence_pct',30),cancel=cancel)
                for row in rows:
                    row['Peak_Number']=len(metrics)+1
                    for key in ('Fractional_Anisotropy','Contraction_Angle_deg','Vector_Coherence'):row[key]=float('nan')
                    metrics.append(row)
            for column,name in enumerate(('Contraction (Raw a.u.)','Contraction (Area-Norm a.u.)','Contraction (Fractional dI/I0)')):
                data[name]=processed[:,column]
            data.to_csv(output/path.name,index=False)
            cell_id=path.name.removesuffix('_raw_kinematics.csv')
            if metrics:
                pd.DataFrame([{label:row[key] for label,key in _METRICS} for row in metrics]).to_csv(output/(cell_id+'_transients_summary.csv'),index=False)
        result.completed_stages=['trace_reanalysis','kinematics']
        cells=manifest['cells']
        _statistics(result,cells)
        provenance=dict(manifest['provenance'],source_analysis=str(source.resolve()),
            trace_analysis_code_sha256=sha256(Path(__file__)),kinetics=config,image_measurements_recomputed=False)
        artifacts={path.name:sha256(path) for path in output.iterdir() if path.is_file()}
        _save_processing_record(result,provenance,cells,artifacts)
        return result
    except Exception as exc:
        result.outcome='cancelled' if cancel.is_cancelled() else 'failure';result.error=str(exc)
        atomic_write(output/'processing.json',dict(result=result.to_dict(),source_analysis=str(source),error=str(exc)))
        return result


def reanalyze_outlines(previous,cells,*,output_root=None,export_videos=False,cancel=None,actor=''):
    """Verified source footage and explicit corrected geometry create a new analysis."""
    from datetime import datetime,timezone
    from tempo_recording import captured_source
    from tempo_backend import process_assay_result,_save_processing_record
    from tempo_quality import dataset_decisions,columns
    from tempo_analysis import read_results
    if not actor:raise ValueError('An identified reviewer is required for outline corrections.')
    source=Path(previous.output_dir)
    source_manifest=source/'processing.json'
    original_hash=sha256(source_manifest)
    footage=captured_source(previous)
    result=process_assay_result(footage,cells_data=cells,output_root=output_root,
                                export_debug_avis=export_videos,cancel=cancel)
    if not result.resolved:return result
    output=Path(result.output_dir)
    try:
        prior=json.loads((source/'quality.json').read_text()) if (source/'quality.json').is_file() else {}
        quality=json.loads((output/'quality.json').read_text())
        quality['source_quality_document']=prior
        quality['assessments']=prior.get('assessments',[])+quality.get('assessments',[])
        quality['decisions']=list(prior.get('decisions',[]))
        old_csv=source/'Master_Cell_Data.csv'
        if old_csv.is_file():
            quality['source_dataset_decisions']=dataset_decisions(old_csv)
            rows=read_results(old_csv)
            if len(rows) and 'Quality_Included' in rows:
                row=rows.iloc[0]
                quality['decisions'].append(dict(included=str(row.Quality_Included).lower() in ('true','1'),
                    actor=actor,time=datetime.now(timezone.utc).isoformat(),reason='Inherited from '+source.name))
        atomic_write(output/'quality.json',quality)
        if result.results_csv:
            table=pd.read_csv(result.results_csv,dtype={'Cell_ID':str,'Well_ID':str,'FOV_ID':str,'Recording_ID':str})
            for name,value in columns(quality).items():table[name]=value
            table.to_csv(result.results_csv,index=False)
        atomic_write(output/'revision.json',dict(version=1,kind='outline_correction',source_analysis=str(source.resolve()),
            source_processing_sha256=original_hash,actor=actor,time=datetime.now(timezone.utc).isoformat(),
            image_measurements_recomputed=True,corrected_cells=cells))
        manifest=json.loads((output/'processing.json').read_text())
        manifest['provenance']['source_analysis']=str(source.resolve())
        artifacts={path.name:sha256(path) for path in output.iterdir() if path.is_file()
                   and path.name!='processing.json' and not path.name.endswith('.lock')}
        _save_processing_record(result,manifest['provenance'],manifest['cells'],artifacts)
        return result
    except Exception as exc:
        result.outcome='failure';result.error='New outline analysis could not be finalized: '+str(exc)
        atomic_write(output/'revision-error.json',dict(error=result.error,source_analysis=str(source)))
        return result
