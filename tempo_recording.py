"""Native recording I/O and explicit acquisition timing, without GUI dependencies."""
from pathlib import Path
import hashlib
import json
import math
import os
import time
import tempfile
from functools import lru_cache

import cv2
import numpy as np

from tempo_processing import atomic_write,file_lock


def probe_storage(directory,required_bytes_per_second):
    """A bounded flushed-write check; actual acquisition still monitors overflow."""
    payload=os.urandom(2*1024*1024)
    fd,name=tempfile.mkstemp(prefix='.tempo-write-check-',dir=directory)
    started=time.perf_counter()
    try:
        with os.fdopen(fd,'wb') as stream:
            for _ in range(4):stream.write(payload)
            stream.flush();os.fsync(stream.fileno())
        elapsed=max(time.perf_counter()-started,1e-9)
        measured=len(payload)*4/elapsed
        if measured<required_bytes_per_second:
            raise OSError('Storage write speed is below the requested recording data rate. '
                          'Choose a faster data folder or reduce the recording rate or image size.')
        return dict(bytes=4*len(payload),elapsed_s=elapsed,bytes_per_second=measured,
                    required_bytes_per_second=required_bytes_per_second)
    finally:Path(name).unlink(missing_ok=True)


def display_frame(frame, sensor_bits=None):
    """Fixed full-scale conversion for display/export; never normalize each frame."""
    frame = np.asarray(frame)
    if frame.dtype == np.uint8:
        return frame
    if frame.dtype != np.uint16:
        raise ValueError('Recording pixels must be unsigned 8-bit or 16-bit.')
    bits = int(sensor_bits or 16)
    if not 1 <= bits <= 16:
        raise ValueError('Invalid sensor bit depth.')
    return np.rint(np.clip(frame.astype(np.float64) / (2**bits-1), 0, 1)*255).astype(np.uint8)


def compatibility_frame(frame,sensor_bits=None):
    frame=display_frame(frame,sensor_bits)
    if frame.ndim==2:frame=cv2.cvtColor(frame,cv2.COLOR_GRAY2BGR)
    elif frame.shape[2]==4:frame=cv2.cvtColor(frame,cv2.COLOR_BGRA2BGR)
    height,width=frame.shape[:2]
    return cv2.copyMakeBorder(frame,0,height%2,0,width%2,cv2.BORDER_CONSTANT,value=0)


def metadata_path(path):
    return Path(str(path)+'.recording.json')


def read_metadata(path):
    sidecar = metadata_path(path)
    if not sidecar.exists():
        return {}
    value = json.loads(sidecar.read_text())
    if value.get('version') != 1 or value.get('recording_id') != Path(path).stem:
        raise ValueError('Recording metadata does not identify this recording.')
    return value


@lru_cache(maxsize=32)
def _cached_rate(path,size,mtime,sidecar_mtime):
    document=json.loads(Path(path+'.timing.json').read_text())
    rate=document.get('fps')
    if document.get('source_sha256')!=sha256(path) or isinstance(rate,bool) or not isinstance(rate,(int,float)) or not math.isfinite(rate) or rate<=0:
        raise ValueError('The declared frame rate does not match this recording.')
    return float(rate)


def declared_rate(path):
    source=Path(path);sidecar=Path(str(path)+'.timing.json')
    if not sidecar.exists():return None
    info=source.stat()
    return _cached_rate(str(source.resolve()),info.st_size,info.st_mtime_ns,sidecar.stat().st_mtime_ns)


def set_declared_rate(path,fps,actor):
    if isinstance(fps,bool) or not math.isfinite(fps) or fps<=0 or not actor:
        raise ValueError('An identified reviewer and positive frame rate are required.')
    sidecar=Path(str(path)+'.timing.json')
    with file_lock(sidecar):
        old=json.loads(sidecar.read_text()) if sidecar.exists() else {}
        digest=sha256(path)
        if old and old.get('source_sha256')!=digest:raise ValueError('The recording changed since its timing was declared.')
        history=list(old.get('history',[]))+[dict(fps=float(fps),actor=actor,time=time.time())]
        atomic_write(sidecar,dict(version=1,source_sha256=digest,fps=float(fps),history=history))
    _cached_rate.cache_clear()


class NativeWriter:
    """Incremental BigTIFF pages; unfinished recordings retain readable pages."""
    def __init__(self, path, fps, *, sensor_bits=None, retain_original=False,
                 retain_cells=False, export_format='native', owned=False, z_index=None):
        import tifffile
        self.path = Path(path)
        if self.path.exists() or metadata_path(path).exists() or Path(str(path)+'.frames.jsonl').exists():
            raise FileExistsError('This recording already exists.')
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError('A positive requested frame rate is required.')
        if export_format not in ('native', 'compatibility'):
            raise ValueError('Unknown retained recording format.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._owner=file_lock(str(path)+'.capture-owner',blocking=False)
        self._owner.__enter__()
        # Exclusive creation prevents accidental replacement of existing research.
        try:
            self._file = self.path.open('xb')
            self._writer = tifffile.TiffWriter(self._file, bigtiff=True)
            self._journal = Path(str(path)+'.frames.jsonl').open('x')
        except BaseException:
            for name in ('_writer','_file','_journal'):
                resource=getattr(self,name,None)
                if resource is not None:
                    try:resource.close()
                    except Exception:pass
            self._owner.__exit__(None,None,None)
            raise
        self.metadata = dict(version=1, recording_id=self.path.stem, status='recording',
            requested_fps=float(fps), sensor_bits=sensor_bits, frames=0, shape=None,
            dtype=None, retain_original=bool(retain_original), retain_cells=bool(retain_cells),
            export_format=export_format, temporary_owned=bool(owned),
            ownership_token=os.urandom(16).hex() if owned else None,
            owned_path=str(self.path.resolve()) if owned else None,
            z_index=z_index, z_source='manual_index' if z_index is not None else None,
            created_at=time.time(), cleanup='pending')
        self.metadata['camera_timing_complete']=True
        self._closed = False
        try:atomic_write(metadata_path(path), self.metadata)
        except BaseException:
            self.close('Recording metadata could not be initialized.')
            raise

    def write(self, frame, *, camera_time_ms=None, camera_frame_id=None):
        received=time.monotonic_ns()
        frame = np.asarray(frame)
        if frame.dtype not in (np.uint8, np.uint16) or frame.ndim not in (2, 3):
            raise ValueError('Unsupported acquired pixel format.')
        if frame.ndim == 3 and frame.shape[2] not in (3, 4):
            raise ValueError('Unsupported acquired channel count.')
        if self.metadata['shape'] is not None and (
                list(frame.shape) != self.metadata['shape'] or str(frame.dtype) != self.metadata['dtype']):
            raise ValueError('Camera pixel format changed during recording.')
        self.metadata.update(shape=list(frame.shape), dtype=str(frame.dtype))
        color = frame.ndim == 3
        stored = frame[..., [2,1,0,3] if frame.shape[2] == 4 else [2,1,0]] if color else frame
        self._writer.write(stored, photometric='rgb' if color else 'minisblack', metadata=None)
        self._file.flush()
        timestamp = None if camera_time_ms is None else float(camera_time_ms)
        if timestamp is not None and not math.isfinite(timestamp):
            raise ValueError('Camera timestamp is not finite.')
        entry = dict(index=self.metadata['frames'], camera_time_ms=timestamp,
                     camera_frame_id=None if camera_frame_id is None else str(camera_frame_id),
                     host_receipt_ns=received)
        if timestamp is None:self.metadata['camera_timing_complete']=False
        self._journal.write(json.dumps(entry, allow_nan=False)+'\n')
        self._journal.flush()
        self.metadata['frames'] += 1
        if self.metadata['frames'] % max(1,int(self.metadata['requested_fps']))==0:
            atomic_write(metadata_path(self.path),self.metadata)

    def close(self, error=''):
        if self._closed:
            return
        self._closed = True
        failure=None
        for action in (self._writer.close,self._file.flush,lambda:os.fsync(self._file.fileno()),
                       self._journal.flush,lambda:os.fsync(self._journal.fileno()),self._file.close,self._journal.close):
            try:action()
            except BaseException as exc:
                if failure is None:failure=exc
        expected=self.metadata.get('expected_frames')
        if expected is not None and expected!=self.metadata['frames']:
            error=error or 'Frame extraction was incomplete.'
        error=error or (str(failure) if failure else '')
        self.metadata.update(status='failed' if error else 'completed', error=str(error), finished_at=time.time())
        try:
            atomic_write(metadata_path(self.path), self.metadata)
        finally:
            self._owner.__exit__(None,None,None)
        if failure is not None:raise failure


class NativeCapture:
    """Small VideoCapture-compatible reader retaining native dtype."""
    def __init__(self, path):
        import tifffile
        self.path = Path(path)
        if Path(str(path)+'.capture-owner.lock').exists():
            try:
                with file_lock(str(path)+'.capture-owner',blocking=False):pass
            except BlockingIOError as exc:
                raise RuntimeError('Recording is still being acquired. Wait for capture to finish.') from exc
        self._file = tifffile.TiffFile(path)
        try:
            self.metadata = read_metadata(path)
            self.declared_fps=declared_rate(path)
        except BaseException:
            self._file.close()
            raise
        self.index = 0

    def isOpened(self):
        return self._file is not None and len(self._file.pages) > 0

    def get(self, prop):
        shape = self._file.pages[0].shape
        return {cv2.CAP_PROP_FRAME_WIDTH:shape[1], cv2.CAP_PROP_FRAME_HEIGHT:shape[0],
                cv2.CAP_PROP_FRAME_COUNT:len(self._file.pages), cv2.CAP_PROP_POS_FRAMES:self.index,
                cv2.CAP_PROP_FPS:self.declared_fps or self.metadata.get('requested_fps',0)}.get(prop,0)

    def set(self, prop, value):
        if prop != cv2.CAP_PROP_POS_FRAMES:
            return False
        self.index = max(0,int(value))
        return True

    def read(self):
        if self._file is None or self.index >= len(self._file.pages):
            return False,None
        frame = self._file.pages[self.index].asarray()
        self.index += 1
        if frame.ndim == 3:
            frame = frame[..., [2,1,0,3] if frame.shape[2] == 4 else [2,1,0]]
        return True,frame

    def release(self):
        if self._file is not None:
            self._file.close()
            self._file = None


def open_recording(path):
    if Path(path).suffix.lower() in ('.tif','.tiff'):return NativeCapture(path)
    cap=cv2.VideoCapture(str(path))
    try:
        rate=declared_rate(path)
        info=read_metadata(path)
    except BaseException:
        cap.release();raise
    content=info.get('content_size_px')
    if info.get('source_shape') and (info.get('padding_right_px') or info.get('padding_bottom_px')):
        content=info['source_shape'][1::-1]
    if rate is None and not content:return cap
    class DeclaredRateCapture:
        def __getattr__(self,name):return getattr(cap,name)
        def get(self,prop):
            if prop==cv2.CAP_PROP_FPS and rate is not None:return rate
            if content and prop in (cv2.CAP_PROP_FRAME_WIDTH,cv2.CAP_PROP_FRAME_HEIGHT):
                return content[0 if prop==cv2.CAP_PROP_FRAME_WIDTH else 1]
            return cap.get(prop)
        def read(self):
            ok,frame=cap.read()
            return (ok,frame[:content[1],:content[0]]) if ok and content else (ok,frame)
    return DeclaredRateCapture()


def timeline(path, count, nominal_fps):
    """Camera time is primary; host receipt time is never exposure time."""
    override=declared_rate(path)
    if override is not None:return np.arange(count,dtype=float)*1000/override,'user_declared'
    entries = []
    journal = Path(str(path)+'.frames.jsonl')
    if journal.exists():
        for line in journal.read_text().splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                break  # A crash can leave the last journal line incomplete.
    if len(entries) >= count and count:
        stamps = [row.get('camera_time_ms') for row in entries[:count]]
        if all(value is not None for value in stamps):
            values = np.asarray(stamps, dtype=float)
            if not np.isfinite(values).all() or np.any(np.diff(values) <= 0):
                raise ValueError('Camera timestamps must be finite and strictly increasing.')
            return values-values[0], 'camera'
    if not math.isfinite(nominal_fps) or nominal_fps <= 0:
        raise ValueError('Recording timing is missing. Supply its frame rate before time-dependent analysis.')
    return np.arange(count, dtype=float)*1000/nominal_fps, 'nominal'


class MaskedFrames:
    """Lazy, bounded cell-crop sequence shared by native and legacy recordings."""
    def __init__(self, path, bounds, mask, count):
        self.path,self.bounds,self.mask,self.count = str(path),bounds,mask,count

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if isinstance(index,slice):
            return [self[i] for i in range(*index.indices(self.count))]
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        cap = open_recording(self.path)
        try:
            cap.set(cv2.CAP_PROP_POS_FRAMES,index)
            ok,frame = cap.read()
            if not ok:
                raise OSError('A recorded cell frame could not be decoded.')
            x,y,right,bottom = self.bounds
            crop=frame[y:bottom,x:right]
            return cv2.bitwise_and(crop,crop,mask=self.mask)
        finally:
            cap.release()


    def __iter__(self):
        cap = open_recording(self.path)
        try:
            for _ in range(self.count):
                ok,frame=cap.read()
                if not ok:
                    raise OSError('Recording ended before its declared frame count.')
                x,y,right,bottom=self.bounds
                crop=frame[y:bottom,x:right]
                yield cv2.bitwise_and(crop,crop,mask=self.mask)
        finally:
            cap.release()


class FrameSlice:
    """Index view without copying an entire cell's image sequence."""
    def __init__(self,frames,start,stop):
        self.frames,self.start,self.stop=frames,int(start),int(stop)
    def __len__(self):return self.stop-self.start
    def __getitem__(self,index):
        if index<0:index+=len(self)
        if not 0<=index<len(self):raise IndexError(index)
        return self.frames[self.start+index]


def sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            digest.update(block)
    return digest.hexdigest()


def finalize_retention(path, output, *, result, expected_source):
    """Only TEMPO-owned temporary footage can be removed after verified results."""
    path,output=Path(path),Path(output)
    info=read_metadata(path)
    if (not info.get('temporary_owned') or not info.get('ownership_token') or not result.resolved
            or info.get('owned_path')!=str(path.resolve())):
        return
    capture_path=Path(str(path)+'.capture.json')
    if capture_path.exists() and json.loads(capture_path.read_text()).get('status')!='completed' and not info.get('recovery_copy'):
        return  # Explicit recovery/discard is required for interrupted captures.
    if info.get('status') != 'completed' or sha256(path) != expected_source:
        raise ValueError('Temporary footage changed or is incomplete; it was retained.')
    manifest=output/'processing.json'
    saved=json.loads(manifest.read_text())
    if saved['result']['outcome'] != result.outcome or saved['provenance']['input_sha256'] != expected_source:
        raise ValueError('Saved processing results could not be verified; footage was retained.')
    for name,digest in saved.get('artifacts',{}).items():
        artifact=output/name
        if artifact.parent.resolve()!=output.resolve() or sha256(artifact)!=digest:
            raise ValueError('Saved result artifacts changed; footage was retained.')
        # Windows _commit requires a writable descriptor for fsync.
        with artifact.open('r+b') as stream:os.fsync(stream.fileno())
    if info.get('retain_original'):
        if info.get('export_format') == 'compatibility':
            retained=output/(path.stem+'.avi')
            if retained.exists():
                prior=read_metadata(retained)
                if prior.get('original_source_sha256')!=expected_source or prior.get('retained_original_sha256')!=sha256(retained):
                    raise FileExistsError('A different compatibility recording already exists; temporary footage was kept.')
            else:export_compatibility(path,retained)
        else:
            import shutil
            retained=output/path.name
            if retained.exists() and sha256(retained)!=expected_source:
                raise FileExistsError('A different retained original already exists; temporary source was kept.')
            if not retained.exists():shutil.copy2(path,retained)
            if sha256(retained)!=expected_source:
                raise OSError('Retained original did not verify; temporary source was kept.')
        info['retained_original']=retained.name
        info['retained_original_sha256']=sha256(retained)
        retained_info=dict(info,recording_id=retained.stem,temporary_owned=False,ownership_token=None)
        if info.get('export_format')=='compatibility':
            retained_info.update(source_dtype=info.get('dtype'),source_sensor_bits=info.get('sensor_bits'),
                                 dtype='uint8',sensor_bits=8,conversion='fixed_full_scale_8bit',
                                 original_source_sha256=expected_source,source_shape=info['shape'],
                                 shape=[info['shape'][0]+info['shape'][0]%2,info['shape'][1]+info['shape'][1]%2,3],
                                 padding_bottom_px=info['shape'][0]%2,padding_right_px=info['shape'][1]%2)
        atomic_write(metadata_path(retained),retained_info)
        with retained.open('r+b') as stream:os.fsync(stream.fileno())
    else:
        info['retained_original']=None
    info['cleanup']='pending'
    atomic_write(output/'recording.json',info)
    # Persist source timing/capture data before removing the temporary frames.
    import shutil
    for suffix,name in (('.frames.jsonl','frames.jsonl'),('.capture.json','capture.json'),('.spatial.json','source-spatial.json'),('.timing.json','timing.json')):
        source=Path(str(path)+suffix)
        if source.exists():
            shutil.copy2(source,output/name)
            with (output/name).open('r+b') as stream:os.fsync(stream.fileno())
            if info.get('retain_original'):
                if suffix=='.timing.json':
                    declaration=json.loads(source.read_text())
                    declaration.update(original_source_sha256=declaration['source_sha256'],source_sha256=sha256(retained))
                    atomic_write(Path(str(retained)+suffix),declaration)
                else:shutil.copy2(source,Path(str(retained)+suffix))
                with Path(str(retained)+suffix).open('r+b') as stream:os.fsync(stream.fileno())
    path.unlink()
    for suffix in ('.frames.jsonl','.capture.json','.spatial.json','.timing.json','.processed.json'):
        Path(str(path)+suffix).unlink(missing_ok=True)
    metadata_path(path).unlink(missing_ok=True)
    info['cleanup']='completed'
    atomic_write(output/'recording.json',info)


def export_compatibility(path, destination):
    destination=Path(destination)
    if destination.exists():raise FileExistsError('Compatibility output already exists.')
    cap=open_recording(path)
    writer=None;temporary=None
    try:
        expected_count=int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps=cap.get(cv2.CAP_PROP_FPS)
        if fps<=0:raise ValueError('A frame rate is required for compatibility export.')
        width,height=int(cap.get(3)),int(cap.get(4))
        fd,name=tempfile.mkstemp(prefix=destination.stem+'.',suffix='.avi',dir=destination.parent)
        os.close(fd);temporary=Path(name)
        writer=cv2.VideoWriter(str(temporary),0,fps,(width+width%2,height+height%2))
        if not writer.isOpened():raise OSError('Could not create compatibility recording.')
        bits=read_metadata(path).get('sensor_bits')
        while True:
            ok,frame=cap.read()
            if not ok:break
            writer.write(compatibility_frame(frame,bits))
        writer.release();writer=None
        check=cv2.VideoCapture(str(temporary))
        try:
            if (not check.isOpened() or expected_count<=0 or
                    int(check.get(cv2.CAP_PROP_FRAME_COUNT))!=expected_count or
                    int(check.get(3))!=width+width%2 or int(check.get(4))!=height+height%2):
                raise OSError('Compatibility export could not be verified.')
        finally:check.release()
        with temporary.open('r+b') as stream:os.fsync(stream.fileno())
        # Publish without replacing an existing file, including concurrent writes.
        os.link(temporary,destination)
    finally:
        cap.release()
        if writer is not None:writer.release()
        if temporary is not None:temporary.unlink(missing_ok=True)


def recover_capture(path,destination,retention=None,cancel=None):
    """Copy only decodable pages to a new recording; never alter the original."""
    from tempo_processing import CancellationToken
    cancel=cancel or CancellationToken()
    path,destination=Path(path),Path(destination)
    original=read_metadata(path)
    if not original.get('temporary_owned'):
        raise ValueError('Choose a TEMPO temporary capture for recovery.')
    with file_lock(str(path)+'.capture-owner',blocking=False):pass
    cap=NativeCapture(path)
    writer=None
    entries=[]
    journal=Path(str(path)+'.frames.jsonl')
    if journal.is_file():
        for line in journal.read_text().splitlines():
            try:entries.append(json.loads(line))
            except json.JSONDecodeError:break
    try:
        cancel.check()
        ok,frame=cap.read()
        if not ok:raise ValueError('This capture contains no decodable frames.')
        options=dict(retain_original=original.get('retain_original',False),retain_cells=original.get('retain_cells',False),
                     export_format=original.get('export_format','native'),owned=True,z_index=original.get('z_index'))
        options.update(retention or {});options['owned']=True
        writer=NativeWriter(destination,float(original.get('requested_fps',0)),sensor_bits=original.get('sensor_bits'),**options)
        writer.metadata.update(recovery_copy=True,recovered_from=str(path.resolve()),source_sha256=sha256(path),
            optical_signature=original.get('optical_signature'),
            protocol_flags=list(set(original.get('protocol_flags',[]))|{'recovered_partial'}),
            quality_assessments=original.get('quality_assessments',[]))
        index=0
        while ok:
            cancel.check()
            entry=entries[index] if index<len(entries) else {}
            writer.write(frame,camera_time_ms=entry.get('camera_time_ms'),camera_frame_id=entry.get('camera_frame_id'))
            index+=1
            try:ok,frame=cap.read()
            except (OSError,ValueError):break
        writer.close()
        for suffix in ('.capture.json','.spatial.json','.timing.json'):
            source=Path(str(path)+suffix)
            if source.is_file():
                metadata=json.loads(source.read_text())
                if suffix=='.timing.json':
                    metadata.update(original_source_sha256=metadata['source_sha256'],source_sha256=sha256(destination))
                else:
                    metadata['source_recording_id']=metadata.get('recording_id')
                    metadata['recording_id']=destination.stem
                    if suffix=='.capture.json':
                        metadata['source_frames']=metadata.get('frames')
                        metadata['frames']=index
                atomic_write(Path(str(destination)+suffix),metadata)
    except BaseException:
        if writer is not None:writer.close('Recovery was interrupted; original capture retained.')
        raise
    finally:cap.release()
    return str(destination)


def captured_source(result):
    """Resolve and verify footage for a new geometry-analysis revision."""
    manifest=json.loads((Path(result.output_dir)/'processing.json').read_text())
    source=Path(result.input_path)
    expected=manifest['provenance']['input_sha256']
    if source.is_file():
        if sha256(source)!=expected:raise ValueError('Original recording changed since analysis.')
        return str(source)
    record=Path(result.output_dir)/'recording.json'
    if record.is_file():
        info=json.loads(record.read_text());name=info.get('retained_original')
        if name and Path(name).name==name:
            source=Path(result.output_dir)/name
            if source.is_file() and sha256(source)==info.get('retained_original_sha256',expected):return str(source)
    raise FileNotFoundError('Original footage was not retained. Trace reanalysis remains available; geometry re-extraction requires the original recording.')
