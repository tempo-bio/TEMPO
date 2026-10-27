"""Versioned image-quality evidence and durable, independent inclusion decisions."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import math
import cv2
import numpy as np
from tempo_processing import atomic_write, file_lock

METHOD='cell-detail-v1'


def features(frame, sensor_bits=None):
    image=np.asarray(frame)
    if image.dtype not in (np.uint8,np.uint16) or image.size==0:
        raise ValueError('A nonempty acquired 8-bit or 16-bit frame is required.')
    gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY) if image.ndim==3 else image
    if gray.ndim!=2 or min(gray.shape)<32:
        raise ValueError('Image is too small to assess intracellular detail.')
    bits = int(sensor_bits or np.iinfo(gray.dtype).bits)
    if not 1 <= bits <= np.iinfo(gray.dtype).bits:
        raise ValueError('Invalid sensor bit depth for image-quality assessment.')
    full=float(2**bits-1)
    clipped=float(np.mean(gray>=full))
    regions=[]
    height,width=gray.shape
    size=min(128,height,width)
    for y in np.linspace(0,height-size,3,dtype=int):
        for x in np.linspace(0,width-size,3,dtype=int):
            tile=gray[y:y+size,x:x+size].astype(np.float32)/full
            contrast=float(np.std(tile))
            if contrast<1/full:
                continue
            gx=cv2.Sobel(tile,cv2.CV_32F,1,0)
            gy=cv2.Sobel(tile,cv2.CV_32F,0,1)
            edge=float(np.mean(gx*gx+gy*gy))
            interior=tile[8:-8,8:-8]
            lap=float(np.mean(cv2.Laplacian(interior,cv2.CV_32F)**2))
            responses=[]
            for period in (4.,8.,12.):
                for angle in (0.,np.pi/4,np.pi/2,3*np.pi/4):
                    kernel=cv2.getGaborKernel((15,15),3.,angle,period,.5,0,ktype=cv2.CV_32F)
                    kernel-=kernel.mean();kernel/=max(float(np.linalg.norm(kernel)),1e-9)
                    responses.append(float(np.mean(cv2.filter2D(interior,cv2.CV_32F,kernel)**2)))
            periodic=max(responses)/max(float(np.mean(responses)),1e-12)
            noise=float(np.mean(np.abs(interior-cv2.medianBlur(interior,3))))
            regions.append([contrast,edge,lap,periodic,noise])
    return np.asarray(regions).reshape((-1,5)),dict(clipped_fraction=clipped,
        dark_fraction=float(np.mean(gray<=full*.005)),regions=len(regions))


def fit_profile(records, signature):
    """Fit only declared development fields; evaluate on separate reviewed fields."""
    train=[row for row in records if row['partition']=='train']
    test=[row for row in records if row['partition']=='test']
    if not signature or not train or not test:
        raise ValueError('Reviewed development and validation fields are required.')
    if {row['field'] for row in train}&{row['field'] for row in test}:
        raise ValueError('All frames of one field must remain in the same partition.')
    def pixels(row):
        frame=np.asarray(row['frame'])
        return (frame.shape,str(frame.dtype),hashlib.sha256(frame.tobytes()).hexdigest())
    if {pixels(row) for row in train}&{pixels(row) for row in test}:
        raise ValueError('Identical images cannot appear in development and validation fields.')
    for rows in (train,test):
        if {row['label'] for row in rows}!={'sharp','out_of_focus'}:
            raise ValueError('Both sharp and out-of-focus examples are required in each partition.')
    values=np.asarray([np.median(features(row['frame'])[0],axis=0) for row in train])
    if not np.isfinite(values).all():
        raise ValueError('Development images must contain assessable intracellular detail.')
    center=values.mean(axis=0);scale=np.maximum(values.std(axis=0),1e-8)
    design=np.column_stack((np.ones(len(values)),(values-center)/scale))
    target=np.asarray([1. if row['label']=='sharp' else -1. for row in train])
    penalty=np.eye(design.shape[1])*.1;penalty[0,0]=0
    weights=np.linalg.solve(design.T@design+penalty,design.T@target)
    reference=[dict(field=row['field'],partition=row['partition'],label=row['label'],
                    pixels_sha256=hashlib.sha256(np.asarray(row['frame']).tobytes()).hexdigest()) for row in records]
    profile=dict(version=1,method=METHOD,signature=signature,center=center.tolist(),scale=scale.tolist(),
                 weights=weights.tolist(),reference=reference,beta=True)
    profile['id']=hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()
    outcomes=[]
    for row in test:
        assessment=assess(row['frame'],signature,profile)
        outcomes.append(dict(field=row['field'],expected=row['label'],actual=assessment['focus']))
    profile['validation']=outcomes
    return profile


def assess(frame, signature=None, profile=None, frame_id=None, sensor_bits=None):
    result=dict(version=1,method=METHOD,frame_id=frame_id,assessed_at=datetime.now(timezone.utc).isoformat(),
                focus='unavailable',flags=[],reason='',profile=None,beta=True)
    try:
        regions,evidence=features(frame,sensor_bits)
    except ValueError as exc:
        result['reason']=str(exc)
        result['flags']=['inadequate_signal','focus_unavailable']
        return result
    result['evidence']=evidence
    if evidence['clipped_fraction']>=.01:
        result['flags'].append('saturation')
    if evidence['dark_fraction']>=.95:
        result['flags'].append('inadequate_signal')
    if not len(regions):
        result['reason']='Insufficient image detail.'
        result['flags']=sorted(set(result['flags'])|{'inadequate_signal','focus_unavailable'})
        return result
    if not signature or profile is None or profile.get('signature')!=signature or profile.get('method')!=METHOD:
        result['reason']='No reviewed focus profile for this optical configuration.'
        result['flags'].append('focus_unavailable')
        return result
    try:
        center=np.asarray(profile['center'],dtype=float);scale=np.asarray(profile['scale'],dtype=float)
        weights=np.asarray(profile['weights'],dtype=float)
        if (center.shape!=(5,) or scale.shape!=(5,) or weights.shape!=(6,) or
                not all(np.isfinite(value).all() for value in (center,scale,weights)) or
                np.any(scale<=0) or not profile.get('id')):
            raise ValueError('Invalid focus profile.')
    except (KeyError,TypeError,ValueError):
        result['reason']='The focus profile is invalid. Recalibrate before using focus guidance.'
        result['flags'].append('focus_unavailable')
        return result
    scores=np.column_stack((np.ones(len(regions)),(regions-center)/scale))@weights
    sharp=float(np.mean(scores>=0))
    result['focus']='mixed' if .25<=sharp<=.75 else 'sharp' if sharp>.75 else 'out_of_focus'
    result['profile']=profile['id']
    result['evidence']['sharp_region_fraction']=sharp
    if result['focus']!='sharp':result['flags'].append('focus')
    return result


def store_assessment(output, assessment):
    path=Path(output)/'quality.json'
    with file_lock(path):
        document=json.loads(path.read_text()) if path.exists() else dict(version=1,assessments=[],decisions=[])
        if assessment not in document['assessments']:
            document['assessments'].append(assessment)
        atomic_write(path,document)
    return document


def decide(output, included, actor, reason=''):
    if type(included) is not bool or not actor.strip():
        raise ValueError('An inclusion decision requires an identified actor.')
    path=Path(output)/'quality.json'
    with file_lock(path):
        document=json.loads(path.read_text()) if path.exists() else dict(version=1,assessments=[],decisions=[])
        document['decisions'].append(dict(included=included,actor=actor,reason=reason,
            time=datetime.now(timezone.utc).isoformat()))
        atomic_write(path,document)
        csv_path=Path(output)/'Master_Cell_Data.csv'
        if csv_path.is_file():
            import os
            import tempfile
            import pandas as pd
            table=pd.read_csv(csv_path,dtype={'Cell_ID':str,'Well_ID':str,'FOV_ID':str,'Recording_ID':str})
            for name,value in columns(document).items():
                table[name]=value
            fd,temporary=tempfile.mkstemp(prefix='quality-',suffix='.csv',dir=output)
            try:
                with os.fdopen(fd,'w') as stream:
                    table.to_csv(stream,index=False);stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,csv_path)
            finally:
                Path(temporary).unlink(missing_ok=True)
    return document


def columns(document):
    assessments=document.get('assessments',[])
    decisions=document.get('decisions',[])
    return dict(Quality_Included=decisions[-1]['included'] if decisions else True,
        Quality_Flags=';'.join(sorted({flag for row in assessments for flag in row.get('flags',[])}|set(document.get('analysis_flags',[])))),
        Focus_Status=assessments[-1]['focus'] if assessments else 'unknown',
        Quality_Method=assessments[-1]['method'] if assessments else '',
        Quality_Decision_Actor=decisions[-1]['actor'] if decisions else '')


def included_rows(data):
    if 'Quality_Included' not in data:
        return data.copy()
    values=data['Quality_Included'].map(lambda value: str(value).strip().lower())
    if not values.isin(('true','false','1','0')).all():
        raise ValueError('Quality inclusion values must be true or false.')
    return data.loc[values.isin(('true','1'))].copy()


def dataset_decisions(csv_path):
    path=Path(str(csv_path)+'.quality.json')
    if not path.exists():
        return dict(version=1,source_sha256=None,recordings={})
    from tempo_recording import sha256
    document=json.loads(path.read_text())
    if document.get('version')!=1 or document.get('source_sha256')!=sha256(csv_path):
        raise ValueError('Saved quality decisions belong to a different version of this results file.')
    return document


def decide_dataset(csv_path,changes,actor):
    """Decisions accompany, rather than overwrite, an imported results CSV."""
    import pandas as pd
    from tempo_recording import sha256
    if not actor.strip():raise ValueError('An identified reviewer is required.')
    table=pd.read_csv(csv_path,dtype={'Recording_ID':str})
    known=set(table.get('Recording_ID',pd.Series(['Unknown'])).fillna('Unknown'))
    if set(changes)-known:raise ValueError('Quality decision does not identify a recording in this dataset.')
    path=Path(str(csv_path)+'.quality.json')
    with file_lock(path):
        document=dataset_decisions(csv_path)
        document['source_sha256']=sha256(csv_path)
        for recording,change in changes.items():
            if type(change['included']) is not bool:raise ValueError('Inclusion must be true or false.')
            record=document['recordings'].setdefault(recording,dict(decisions=[]))
            record['decisions'].append(dict(included=change['included'],reason=change.get('reason',''),
                actor=actor,time=datetime.now(timezone.utc).isoformat()))
        atomic_write(path,document)
    return document


def apply_dataset_decisions(data,csv_path):
    document=dataset_decisions(csv_path)
    for recording,record in document['recordings'].items():
        if record.get('decisions'):
            last=record['decisions'][-1]
            selected=data['Recording_ID']==recording
            data.loc[selected,'Quality_Included']=last['included']
            data.loc[selected,'Quality_Decision_Actor']=last['actor']
            data.loc[selected,'Quality_Decision_Reason']=last['reason']
    return data
