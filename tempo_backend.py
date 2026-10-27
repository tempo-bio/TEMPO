import os
import sys
import platform
import subprocess
import tempfile
import json
import cv2
import numpy as np
import hashlib
import time
from contextlib import contextmanager, ExitStack
from pathlib import Path
from tempo_processing import (ProcessingResult, CancellationToken, ProcessingCancelled, SCHEMA_VERSION,
                              atomic_write, edit_queue, read_queue, readable_recording)
from tempo_processing import read_result_history, record_result
from tempo_processing import file_lock
from masking_engine import process_cell_masks, validate_cells
from vk_math_engine import process_cell_kinematics  # <-- The new native Python engine
from tempo_recording import open_recording, read_metadata

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
from tempo_config import get_collaboration_dir, get_config_dir, get_data_dir
RAW_DIR = os.path.join(get_data_dir(), "Raw")
PROCESSED_DIR = os.path.join(get_data_dir(), "Processed")
UNIVERSAL_DATASET_DIR = get_collaboration_dir()
QUEUE_FILE = os.path.join(get_config_dir(), 'processing', 'pending_queue.json')

def apply_data_folder():
    global RAW_DIR, PROCESSED_DIR
    RAW_DIR = os.path.join(get_data_dir(), 'Raw')
    PROCESSED_DIR = os.path.join(get_data_dir(), 'Processed')


def initialize_processing_storage():
    """Copy legacy queue/history once; never move recordings or overwrite state."""
    target = Path(QUEUE_FILE)
    expected = Path(get_config_dir(), 'processing', 'pending_queue.json')
    if target != expected:
        return  # Isolated callers and test fixtures own their queue.
    for suffix in ('', '.results.json'):
        old = Path(BASE_DIR, 'pending_queue.json' + suffix)
        new = Path(str(target) + suffix)
        if not old.is_file():
            continue
        with file_lock(new):
            if new.exists():
                continue
            if suffix:
                records = read_result_history(old)
                payload = {'schema_version': SCHEMA_VERSION, 'results': [r.to_dict() for r in records]}
            else:
                payload = read_queue(old)
            atomic_write(new, payload)

def add_to_queue(video_path):
    video_path = os.path.abspath(video_path)
    return edit_queue(QUEUE_FILE, lambda queue: queue if video_path in queue else queue + [video_path])


@contextmanager
def claim_queue_entries(paths):
    with ExitStack() as claims:
        for path in sorted({os.path.abspath(path) for path in paths}):
            identity = hashlib.sha256(path.encode()).hexdigest()
            lock_path = Path(QUEUE_FILE + '.active', identity)
            try:
                claims.enter_context(file_lock(lock_path, blocking=False))
            except BlockingIOError as exc:
                raise ValueError('This recording is processing in another operation. Wait until it settles.') from exc
        yield


def queue_entry_running(path):
    try:
        with claim_queue_entries([path]):
            return False
    except ValueError:
        return True


def remove_from_queue(paths, _settled=False):
    if _settled:
        return edit_queue(QUEUE_FILE, lambda queue: [p for p in queue if p not in paths])
    with claim_queue_entries(paths):
        return edit_queue(QUEUE_FILE, lambda queue: [p for p in queue if p not in paths])


def relink_queue(old, new):
    new = os.path.abspath(new)
    if not readable_recording(new):
        raise ValueError('Choose a readable recording.')
    with claim_queue_entries([old, new]):
        def replace(queue):
            if old not in queue:
                raise ValueError('This queue entry changed or was already completed. Refresh the queue.')
            if new != old and new in queue:
                raise ValueError('That location is already queued; existing entries were preserved.')
            return [new if p == old else p for p in queue]
        return edit_queue(QUEUE_FILE, replace)


def undo_queue_location(old, new):
    """Compare before restoring one path; never reinsert a completed recording."""
    with claim_queue_entries([old, new]):
        def restore(queue):
            if new not in queue or old in queue:
                raise ValueError('The queue changed or processing settled this entry. The location change cannot be undone.')
            return [old if p == new else p for p in queue]
        return edit_queue(QUEUE_FILE, restore)


def run_yolo_inference(video_path):
    """Unattended detection: an empty result is distinct from an inference error."""
    from yolo_inference_helper import detect_cells_via_bridge
    from tempo_regions import object_type
    result = detect_cells_via_bridge(video_path)
    if not isinstance(result, dict):
        raise ValueError('Invalid cell detection response.')
    if result.get('success') is not True:
        raise RuntimeError(result.get('error') or 'Cell detection failed.')
    boxes = result.get('boxes')
    if not isinstance(boxes, list) or any(not isinstance(box, dict) for box in boxes):
        raise ValueError('Invalid cell detection response: expected a list of cell boxes.')
    cells = []
    for index, box in enumerate(boxes):
        polygon = box.get('polygon', [])
        if len(polygon) < 3:
            raise ValueError('Detection returned an invalid cell polygon.')
        moments = cv2.moments(np.asarray(polygon, dtype=np.float32))
        if not moments['m00']:
            raise ValueError('Detection returned a zero-area cell polygon.')
        kind = object_type(box)
        cells.append({'id': f'{kind}_{index + 1:03d}' if kind == 'group' else f'cell_{index + 1}', 'polygon': polygon,
                      'object_type': kind,
                      'class_id': box.get('class_id', 0),
                      'centroid_x': moments['m10'] / moments['m00'],
                      'centroid_y': moments['m01'] / moments['m00']})
    validate_cells(cells)
    return cells


def _file_digest(path, cancel=None):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            if cancel:
                cancel.check()
            digest.update(chunk)
    return digest.hexdigest()


def processing_provenance(video_path, cells_data, export_debug_avis, cancel=None):
    from tempo_config import tempo_preferences
    stat = os.stat(video_path)
    digest = hashlib.sha256()
    sources = [Path(sys.executable)] if getattr(sys, 'frozen', False) else [
        Path(BASE_DIR, name) for name in ('tempo_backend.py', 'vk_math_engine.py',
        'masking_engine.py', 'tempo_statistics.py', 'tempo_processing.py', 'tempo_recording.py', 'tempo_quality.py', 'tempo_angles.py',
        'yolo_inference_helper.py', 'tempo_config.py', 'tempo_spatial.py', 'tempo_capture.py', 'tempo_stimulation.py')]
    for source in sources:
        digest.update(_file_digest(source, cancel).encode())
    model_hashes = {}
    from tempo_training import MODEL_FILES
    for name in MODEL_FILES.values():
        model = Path(BASE_DIR, name)
        if model.is_file():
            model_hashes[str(name)] = _file_digest(model, cancel)
    registration = None
    sidecar = Path(str(video_path) + '.spatial.json')
    if sidecar.exists():
        from tempo_spatial import read_registration
        registration = read_registration(sidecar)
        if registration['recording_id'] != Path(video_path).stem:
            raise ValueError('Spatial sidecar does not identify this input recording.')
    from tempo_capture import read_capture_metadata
    capture_metadata = read_capture_metadata(video_path)
    timing_journal=Path(str(video_path)+'.frames.jsonl')
    timing_declaration=Path(str(video_path)+'.timing.json')
    return {'input': os.path.abspath(video_path), 'spatial_registration': registration,
            'capture_metadata': capture_metadata, 'size': stat.st_size,
            'recording_metadata': read_metadata(video_path),
            'frame_timing_sha256':_file_digest(timing_journal,cancel) if timing_journal.is_file() else None,
            'timing_declaration':json.loads(timing_declaration.read_text()) if timing_declaration.is_file() else None,
            'mtime_ns': stat.st_mtime_ns, 'input_sha256': _file_digest(video_path, cancel), 'cells': cells_data,
            'intermediate_videos': export_debug_avis,
            'config': json.loads(json.dumps({category: tempo_preferences.get_category(category)
                                             for category in ('kinetics', 'cruncher', 'annotator')})),
            'code': digest.hexdigest(), 'models': model_hashes, 'schema': SCHEMA_VERSION}


def _statistics(result, cells, replace_existing=False):
    from tempo_statistics import compile_master_dataset, generate_run_statistics
    master = compile_master_dataset(result.output_dir, cells_data=cells)
    if master is None or master.empty:
        result.outcome = 'no_beats'
    else:
        if not generate_run_statistics(master, result.output_dir, replace_existing=replace_existing):
            raise RuntimeError('Statistics output was not generated.')
        result.results_csv = os.path.join(result.output_dir, 'Master_Cell_Data.csv')
        result.outcome = 'success' if master['Total_Beats_Analyzed'].gt(0).any() else 'no_beats'
    result.completed_stages.append('statistics')


def _save_processing_record(result, provenance, cells, artifacts):
    atomic_write(Path(result.output_dir, 'processing.json'), {
        'result': result.to_dict(), 'provenance': provenance,
        'cells': cells, 'artifacts': artifacts,
    })


def process_assay_result(video_path, cells_data=None, export_debug_avis=False, cancel=None,
                         retry_output=None, output_root=None, _native=False):
    cancel = cancel or CancellationToken()
    result = ProcessingResult(os.path.abspath(video_path))
    provenance = None
    artifacts = {}
    try:
        cancel.check()
        if not readable_recording(video_path):
            raise FileNotFoundError(f'Recording is missing or unreadable: {video_path}')
        native_python = os.path.join(BASE_DIR, '.venv', 'bin', 'python')
        if not _native and '.venv_x86' in sys.executable and platform.system() == 'Darwin' and os.path.exists(native_python):
            with tempfile.TemporaryDirectory(prefix='tempo-process-') as directory:
                request = os.path.join(directory, 'request.json')
                response = os.path.join(directory, 'result.json')
                cancel_path = os.path.join(directory, 'cancel')
                atomic_write(request, {'video_path': os.path.abspath(video_path), 'cells_data': cells_data,
                                      'export_debug_avis': export_debug_avis, 'retry_output': retry_output,
                                      'output_root': os.path.abspath(output_root or PROCESSED_DIR)})
                child = subprocess.Popen(['arch', '-arm64', native_python, os.path.abspath(__file__),
                                          '--request', request, '--result', response, '--cancel', cancel_path])
                while child.poll() is None:
                    if cancel.is_cancelled():
                        Path(cancel_path).touch()
                    time.sleep(0.05)
                if not Path(response).exists():
                    raise RuntimeError(f'Native processing exited {child.returncode} without a result.')
                return ProcessingResult(**json.loads(Path(response).read_text()))
        provenance = processing_provenance(video_path, cells_data, export_debug_avis, cancel)
        if cells_data is not None:
            validate_cells(cells_data)
        marker=Path(str(video_path)+'.processed.json')
        if retry_output is None and provenance.get('recording_metadata',{}).get('temporary_owned') and marker.is_file():
            saved_marker=json.loads(marker.read_text())
            cached_path=Path(saved_marker['output_dir'])/'processing.json'
            cached=json.loads(cached_path.read_text())
            if cached.get('provenance')==provenance:
                recovered=ProcessingResult(**cached['result'])
                if (recovered.resolved and recovered.input_path==os.path.abspath(video_path)
                        and Path(recovered.output_dir).resolve()==cached_path.parent.resolve() and cached.get('artifacts') and all((cached_path.parent/name).parent.resolve()==cached_path.parent.resolve()
                        and _file_digest(cached_path.parent/name,cancel)==digest
                        for name,digest in cached.get('artifacts',{}).items())):
                    return recovered
        if retry_output:
            manifest = json.loads(Path(retry_output, 'processing.json').read_text())
            if manifest['provenance'] != provenance or 'kinematics' not in manifest['result']['completed_stages']:
                raise ValueError('Input, model, or configuration changed. Start a new processing attempt explicitly.')
            previous = ProcessingResult(**manifest['result'])
            if previous.outcome != 'failure' or 'statistics' in previous.completed_stages:
                raise ValueError('This run is not eligible for statistics-only retry.')
            if (Path(previous.output_dir).resolve() != Path(retry_output).resolve()
                    or previous.input_path != result.input_path):
                raise ValueError('Processing record does not identify this input and output directory.')
            artifacts = manifest.get('artifacts', {})
            if not artifacts:
                raise ValueError('Extraction outputs cannot be verified. Start a new processing attempt.')
            for filename, digest in artifacts.items():
                artifact = Path(retry_output, filename)
                if artifact.parent.resolve() != Path(retry_output).resolve() or _file_digest(artifact, cancel) != digest:
                    raise ValueError('Extraction outputs changed. Start a new processing attempt.')
            result = previous
            cells_data = manifest['cells']
            result.error = ''
            cancel.check()
            _statistics(result, cells_data, replace_existing=True)
        else:
            name = os.path.splitext(os.path.basename(video_path))[0]
            output_root = output_root or PROCESSED_DIR
            Path(output_root).mkdir(parents=True, exist_ok=True)
            result.output_dir = tempfile.mkdtemp(prefix=name + '_', dir=output_root)
            capture = open_recording(video_path)
            try:
                readable, reference_frame = capture.read()
            finally:
                capture.release()
            if not readable:
                raise ValueError('Recording could not be decoded.')
            from tempo_quality import assess, store_assessment
            from tempo_recording import display_frame
            recording_info = provenance.get('recording_metadata', {})
            if not recording_info.get('camera_timing_complete'):
                recording_info = dict(recording_info,protocol_flags=list(recording_info.get('protocol_flags',[]))+['nominal_timing'])
            if recording_info.get('conversion'):
                recording_info = dict(recording_info,protocol_flags=list(recording_info.get('protocol_flags',[]))+['intensity_conversion'])
            if provenance.get('timing_declaration'):
                recording_info = dict(recording_info,protocol_flags=list(recording_info.get('protocol_flags',[]))+['user_declared_timing'])
            assessments = recording_info.get('quality_assessments') or [assess(reference_frame, recording_info.get('quality_signature'),sensor_bits=recording_info.get('sensor_bits'))]
            for assessment in assessments:
                assessment=dict(assessment,flags=sorted(set(assessment.get('flags',[]))|set(recording_info.get('protocol_flags',[]))))
                store_assessment(result.output_dir, assessment)
            source_format=dict(dtype=str(reference_frame.dtype),sensor_bits=recording_info.get('sensor_bits'),
                timing_basis='user_declared' if provenance.get('timing_declaration') else
                    'camera' if recording_info.get('camera_timing_complete') else 'nominal')
            reference_path = Path(result.output_dir, 'reference.tif')
            if not cv2.imwrite(str(reference_path),reference_frame):
                raise OSError('Could not preserve the processing reference image.')
            artifacts['reference.tif'] = _file_digest(reference_path,cancel)
            if cells_data is None:
                cells_data = run_yolo_inference(video_path)
            cells_data=[dict(cell,source_format=source_format) for cell in cells_data]
            registration = provenance.get('spatial_registration')
            capture_metadata = provenance.get('capture_metadata')
            if capture_metadata:
                atomic_write(Path(result.output_dir, 'capture.json'), capture_metadata)
                enriched = []
                for cell in cells_data:
                    cell = dict(cell, capture_metadata=capture_metadata)
                    for key, value in (('well_id', capture_metadata['well_id']), ('fov_id', capture_metadata['field_id'])):
                        if cell.get(key) not in (None, '', 'Unknown', value):
                            raise ValueError('Cell identity conflicts with its capture metadata.')
                        cell[key] = value
                    enriched.append(cell)
                cells_data = enriched
            if registration:
                if registration['image_size_px'] != [reference_frame.shape[1], reference_frame.shape[0]]:
                    raise ValueError('Spatial image dimensions do not match the input recording.')
                registration = dict(registration, source_recording_id=registration['recording_id'],
                                    recording_id=os.path.basename(result.output_dir))
                atomic_write(Path(result.output_dir, 'spatial.json'), registration)
                enriched = []
                for cell in cells_data:
                    cell = dict(cell)
                    for key, registered in [('well_id', registration['well_id']), ('fov_id', registration['field_id'])]:
                        if cell.get(key) not in (None, '', 'Unknown', registered):
                            raise ValueError('Cell identity conflicts with its spatial registration.')
                        cell[key] = registered
                    cell['spatial_metadata_file'] = 'spatial.json'
                    enriched.append(cell)
                cells_data = enriched
            result.completed_stages.append('detection')
            _save_processing_record(result, provenance, cells_data, artifacts)
            cancel.check()
            if not cells_data:
                result.outcome = 'no_cells'
            else:
                save_cells = bool(recording_info.get('retain_cells')) if recording_info.get('temporary_owned') else export_debug_avis
                masks = process_cell_masks(video_path, result.output_dir, cells_data,
                                           export_debug_avis=save_cells, cancel=cancel)
                if not masks or not masks.cell_frames:
                    raise RuntimeError('No usable cell regions were extracted.')
                result.completed_stages.append('extraction')
                _save_processing_record(result, provenance, cells_data, artifacts)
                for cell_id, frames in masks.cell_frames.items():
                    cancel.check()
                    timing = dict(times_ms=masks.times_ms) if getattr(masks,'times_ms',None) is not None else {}
                    if not process_cell_kinematics(frames, result.output_dir, cell_id,
                                                   fps=masks.fps, polygon_mask=masks.local_masks[cell_id], cancel=cancel,
                                                   kinetics_config=provenance.get('config', {}).get('kinetics', {}),**timing):
                        raise RuntimeError(f'Kinematics failed for {cell_id}.')
                    mask_path=Path(result.output_dir,cell_id+'_mask.tif')
                    if not cv2.imwrite(str(mask_path),masks.local_masks[cell_id]):
                        raise OSError('Could not save the confirmed cell mask.')
                    artifacts[mask_path.name]=_file_digest(mask_path,cancel)
                result.completed_stages.append('kinematics')
                artifact_files = list(Path(result.output_dir).glob('*_raw_kinematics.csv')) + list(
                    Path(result.output_dir).glob('*_transients_summary.csv'))
                if registration:
                    artifact_files.append(Path(result.output_dir, 'spatial.json'))
                if capture_metadata:
                    artifact_files.append(Path(result.output_dir, 'capture.json'))
                artifacts.update({path.name: _file_digest(path, cancel) for path in artifact_files})
                if not artifacts:
                    raise RuntimeError('Kinematics produced no recorded signals.')
                _save_processing_record(result, provenance, cells_data, artifacts)
                cancel.check()
                _statistics(result, cells_data)
    except ProcessingCancelled as exc:
        result.outcome, result.error = 'cancelled', str(exc)
    except Exception as exc:
        result.outcome, result.error = 'failure', str(exc)
    if result.output_dir:
        try:
            if result.resolved:
                for path in Path(result.output_dir).iterdir():
                    if path.is_file() and (path.suffix=='.csv' or path.name.startswith('masked_') or
                            path.name in ('quality.json','capture.json','spatial.json')):
                        if not path.name.endswith('.lock'):artifacts[path.name] = _file_digest(path,cancel)
            _save_processing_record(result, provenance, cells_data, artifacts)
            if result.resolved and provenance and provenance.get('recording_metadata',{}).get('temporary_owned'):
                atomic_write(Path(str(video_path)+'.processed.json'),dict(output_dir=result.output_dir))
        except Exception as exc:
            result.outcome = 'failure'
            result.error += f' Could not save processing record: {exc}'
    return result


def finalize_processing(result):
    """Call only after processing history is durable. Failures keep temporary input."""
    if not result.resolved or not Path(result.input_path).is_file():
        return
    if not read_metadata(result.input_path).get('temporary_owned'):
        return
    from tempo_recording import finalize_retention
    manifest=json.loads(Path(result.output_dir,'processing.json').read_text())
    finalize_retention(result.input_path,result.output_dir,result=result,
                       expected_source=manifest['provenance']['input_sha256'])


def process_assay(video_path, cells_data=None, export_debug_avis=False):
    """Compatibility entry point; application callers use the explicit result."""
    return process_assay_result(video_path, cells_data, export_debug_avis).resolved


def process_entire_queue(export_debug_avis=False, cancel=None, queue=None, on_result=None, output_root=None):
    cancel = cancel or CancellationToken()
    starting = list(dict.fromkeys(read_queue(QUEUE_FILE) if queue is None else queue))
    results = []
    for path in starting:
        if cancel.is_cancelled():
            break
        try:
            with claim_queue_entries([path]):
                options = {'output_root': output_root} if output_root is not None else {}
                result = process_assay_result(path, export_debug_avis=export_debug_avis, cancel=cancel, **options)
                try:
                    record_result(QUEUE_FILE + '.results.json', result)
                    if result.resolved:
                        remove_from_queue([path], _settled=True)
                        finalize_processing(result)
                except Exception as exc:
                    result.outcome = 'failure'
                    result.error = f'Outputs retained, but processing history, queue or footage finalization failed: {exc}'
                    if readable_recording(path):
                        try:add_to_queue(path)
                        except (OSError,ValueError):
                            result.error+=' Use Add Recordings to retry this retained source.'
                    try:
                        record_result(QUEUE_FILE + '.results.json', result)
                    except (OSError, ValueError, TypeError):
                        pass
        except Exception as exc:
            result = ProcessingResult(os.path.abspath(path), error=str(exc))
        results.append(result)
        if on_result:
            on_result(result)
    return {'completed': sum(r.resolved for r in results),
            'failed': sum(r.outcome == 'failure' for r in results),
            'cancelled': cancel.is_cancelled(), 'results': results}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('video', nargs='?')
    parser.add_argument('--request'); parser.add_argument('--result'); parser.add_argument('--cancel')
    parser.add_argument('--cells-json'); parser.add_argument('--export-debug-avis', action='store_true')
    args = parser.parse_args()
    if args.request:
        request = json.loads(Path(args.request).read_text())
    elif args.video:
        request = {'video_path': args.video, 'export_debug_avis': args.export_debug_avis,
                   'cells_data': json.loads(Path(args.cells_json).read_text()) if args.cells_json else None}
    else:
        parser.error('Specify a recording or a processing request.')
    result = process_assay_result(**request, cancel=CancellationToken(args.cancel), _native=True)
    if args.result:
        atomic_write(args.result, result.to_dict())
    print(json.dumps(result.to_dict()))
    sys.exit(0 if result.resolved else 1)
