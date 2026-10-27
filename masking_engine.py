import cv2
import os
import numpy as np
import re
from tempo_recording import open_recording, MaskedFrames, compatibility_frame, timeline, NativeCapture, NativeWriter, read_metadata
from tempo_processing import atomic_write
from pathlib import Path
from contextlib import ExitStack
from tempo_regions import object_type


def validate_cells(cells_data):
    """Reject ambiguous identities or unusable contours before producing files."""
    if not isinstance(cells_data, list):
        raise ValueError('Cells must be supplied as a list.')
    identities = set()
    for cell in cells_data:
        if not isinstance(cell, dict):
            raise ValueError('Each cell must contain an identity and polygon.')
        object_type(cell)
        identity = cell.get('id')
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', identity):
            raise ValueError('Cell identity must be a nonempty, portable filename identifier.')
        if identity in identities:
            raise ValueError(f'Duplicate cell identity: {identity}.')
        identities.add(identity)
        try:
            polygon = np.asarray(cell.get('polygon'), dtype=float)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'{identity}: invalid cell polygon.') from exc
        if (polygon.ndim != 2 or polygon.shape[1] != 2 or len(polygon) < 3
                or not np.isfinite(polygon).all()
                or np.abs(polygon).max() > np.iinfo(np.int32).max
                or cv2.contourArea(polygon.astype(np.float32)) == 0):
            raise ValueError(f'{identity}: cell polygon must contain finite points and nonzero area.')

class MaskingResult(dict):
    """Dictionary subclass mapping cell_id -> polygon mask, while also exposing cell_frames and fps."""
    def __init__(self, local_masks, cell_frames, fps):
        super().__init__(local_masks)
        self.local_masks = local_masks
        self.cell_frames = cell_frames
        self.fps = fps

def process_cell_masks(input_video_path, output_directory, cells_data, export_debug_avis=False, cancel=None):
    if not os.path.exists(input_video_path): return False
    validate_cells(cells_data)
    
    cap = open_recording(input_video_path)
    writers = {}
    bboxes = {}
    local_masks = {}
    cell_frames = {}
    video_paths = {}
    source_dtype = None
    
    with ExitStack() as resources:
        resources.callback(cap.release)
        frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0.0:
            raise ValueError('Recording frame rate is missing; supply it before processing.')
        native = isinstance(cap, NativeCapture)
        source_info = read_metadata(input_video_path)
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if native else 0
        timed_source=native or os.path.exists(str(input_video_path)+'.frames.jsonl') or os.path.exists(str(input_video_path)+'.timing.json')
        source_times,source_basis = timeline(input_video_path,count,fps) if native else (None,'nominal')
        for cell in cells_data:
            cell_id = cell["id"]
            polygon = np.array(cell["polygon"], np.int32)
            x, y, w, h = cv2.boundingRect(polygon)
        
            x_start, y_start = max(0, x), max(0, y)
            x_end, y_end = min(frame_width, x + w), min(frame_height, y + h)
            actual_w, actual_h = x_end - x_start, y_end - y_start
        
            if actual_w <= 0 or actual_h <= 0: continue
            bboxes[cell_id] = (x_start, y_start, x_end, y_end)
            cell_frames[cell_id] = MaskedFrames(input_video_path,(x_start,y_start,x_end,y_end),None,count) if native else []
        
            # Pre-rasterize polygon into a local bounding-box mask once
            local_poly = polygon.copy()
            local_poly[:, 0] -= x_start
            local_poly[:, 1] -= y_start
            mask = np.zeros((actual_h, actual_w), dtype=np.uint8)
            cv2.fillPoly(mask, [local_poly], 255)
            local_masks[cell_id] = mask
            if native:
                cell_frames[cell_id].mask = mask
        
            # HEADLESS FIX: Use Uncompressed AVI (fourcc = 0) if debug export requested
            # This allows Fiji's Virtual Stack to stream it for golden-truth cross-validation.
            if export_debug_avis:
                if native and source_info.get('export_format','native') == 'native':
                    video_path = os.path.join(output_directory,f'masked_{cell_id}.tif')
                    writers[cell_id] = NativeWriter(video_path,fps,sensor_bits=source_info.get('sensor_bits'))
                    writers[cell_id].metadata['expected_frames']=count
                    resources.callback(writers[cell_id].close)
                else:
                    video_path = os.path.join(output_directory, f"masked_{cell_id}.avi")
                    writers[cell_id] = cv2.VideoWriter(video_path, 0, fps, (actual_w+actual_w%2, actual_h+actual_h%2))
                    resources.callback(writers[cell_id].release)
                    if not writers[cell_id].isOpened():
                        raise OSError(f'Could not create intermediate cell video: {video_path}')
                video_paths[cell_id]=video_path

        frame_index=0
        while True:
            if cancel:
                cancel.check()
            ret, frame = cap.read()
            if not ret: break
            source_dtype=str(frame.dtype)
            
            for cell in cells_data:
                cell_id = cell["id"]
                if cell_id in bboxes:
                    x_start, y_start, x_end, y_end = bboxes[cell_id]
                    cropped = frame[y_start:y_end, x_start:x_end]
                    # Efficient bitwise masking on the small crop
                    final_cropped_frame = cv2.bitwise_and(cropped, cropped, mask=local_masks[cell_id])
                
                    # In-memory frame collection
                    if not native:
                        cell_frames[cell_id].append(final_cropped_frame)
                
                    if export_debug_avis and cell_id in writers:
                        if isinstance(writers[cell_id],NativeWriter):
                            writers[cell_id].write(final_cropped_frame,camera_time_ms=(
                                float(source_times[frame_index]) if source_basis=='camera' else None))
                        else:
                            preview = compatibility_frame(final_cropped_frame,source_info.get('sensor_bits'))
                            writers[cell_id].write(preview)
            frame_index+=1

    for cell_id,video_path in video_paths.items():
        if not isinstance(writers[cell_id],NativeWriter):
            check=cv2.VideoCapture(video_path)
            try:
                if not check.isOpened() or frame_index<=0 or int(check.get(cv2.CAP_PROP_FRAME_COUNT))!=frame_index:
                    raise OSError('The intermediate cell video could not be verified: '+video_path)
            finally:check.release()
            left,top,right,bottom=bboxes[cell_id];height,width=bottom-top,right-left
            atomic_write(video_path+'.recording.json',dict(version=1,recording_id=Path(video_path).stem,
                status='completed',frames=frame_index,requested_fps=fps,temporary_owned=False,
                source_recording=str(Path(input_video_path).resolve()),source_dtype=source_dtype,
                dtype='uint8',sensor_bits=8,conversion='fixed_full_scale_8bit',
                content_size_px=[width,height],padding_bottom_px=height%2,padding_right_px=width%2))
        # Windows _commit requires a writable descriptor for fsync.
        with open(video_path,'r+b') as stream:os.fsync(stream.fileno())

    result = MaskingResult(local_masks, cell_frames, fps)
    if timed_source:
        if not native:source_times,source_basis=timeline(input_video_path,frame_index,fps)
        result.times_ms,result.timing_basis = source_times,source_basis
    return result
