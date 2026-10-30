"""Opt-in, isolated NVIDIA inference runtime for packaged Windows TEMPO."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from tempo_process import run_model_worker


SCHEMA = 1
MAX_RESULT_BYTES = 16 * 1024 * 1024


def runtime_root():
    from tempo_config import get_config_dir
    return Path(get_config_dir()).parent / 'nvidia-runtime'


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _relative_path(root, value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('Invalid runtime path.')
    path = Path(value)
    if path.is_absolute() or any(part in ('', '.', '..') for part in path.parts):
        raise ValueError('Invalid runtime path.')
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError('Runtime path leaves its installation.')
    return resolved


def active_executable():
    """Return the verified optional worker, or None when it is unavailable."""
    if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
        return None
    root = runtime_root()
    marker = root / 'active.json'
    try:
        from tempo_nvidia_setup import release_descriptor
        descriptor = release_descriptor()
        if descriptor is None:
            return None
        if marker.stat().st_size > 4096:
            return None
        state = json.loads(marker.read_text(encoding='utf-8'))
        from tempo_release import VERSION, build_identity
        if (not isinstance(state, dict) or state.get('schema_version') != SCHEMA
                or state.get('version') != VERSION
                or state.get('commit') != build_identity().get('commit')
                or state.get('archive_sha256') != descriptor['archive_sha256']
                or state.get('executable_sha256') != descriptor['executable_sha256']
                or state.get('executable') != descriptor['executable']
                or not re.fullmatch(r'[0-9a-f]{64}', state.get('archive_sha256', ''))
                or not re.fullmatch(r'[0-9a-f]{64}', state.get('executable_sha256', ''))):
            return None
        bundle = _relative_path(root, state['bundle'])
        executable = _relative_path(bundle, state['executable'])
        if not executable.is_file() or _sha256(executable) != state['executable_sha256']:
            return None
        return executable
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _read_report(path):
    if path.stat().st_size > MAX_RESULT_BYTES:
        raise RuntimeError('NVIDIA worker returned an oversized result.')
    result = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(result, dict) or type(result.get('success')) is not bool:
        raise RuntimeError('NVIDIA worker returned an invalid result.')
    return result


def run_cellpose(image_path, model_path, conf=0.15, iou=0.40):
    worker = active_executable()
    if worker is None:
        raise RuntimeError('NVIDIA acceleration is not installed for this TEMPO build.')
    if not all(math.isfinite(float(value)) and 0 <= float(value) <= 1 for value in (conf, iou)):
        raise ValueError('Invalid inference thresholds.')
    with tempfile.TemporaryDirectory(prefix='tempo-nvidia-result-') as directory:
        report = Path(directory) / 'result.json'
        command = [str(worker), '--predict-cells', str(Path(image_path).resolve()),
                   str(Path(model_path).resolve()), str(conf), str(iou), str(report)]
        completed = run_model_worker(command, timeout=900)
        if completed.returncode:
            if report.is_file():
                result = _read_report(report)
                if result.get('success') is False:
                    return result
            raise RuntimeError(f'NVIDIA worker exited with code {completed.returncode}.')
        if not report.is_file():
            raise RuntimeError('NVIDIA worker did not return cell outlines.')
        result = _read_report(report)
        if result.get('success') is True and (result.get('result_kind') != 'cells'
                                             or not isinstance(result.get('boxes'), list)):
            raise RuntimeError('NVIDIA worker returned an invalid cell result.')
        return result


def _atomic_report(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def probe_command(report_path):
    """Exercise actual CUDA execution; import torch only in the headless child."""
    try:
        os.environ.update(HF_HUB_OFFLINE='1', YOLO_AUTOINSTALL='false')
        import torch
        import torchvision
        if (torch.__version__ != '2.14.1+cu126' or torchvision.__version__ != '0.29.1+cu126'
                or torch.version.cuda != '12.6'):
            raise RuntimeError('NVIDIA worker package versions do not match this release.')
        if not torch.cuda.is_available() or not torch.version.cuda:
            raise RuntimeError('CUDA is unavailable on this computer.')
        device = torch.device('cuda:0')
        result = (torch.ones(4, device=device) * 3).sum()
        torch.cuda.synchronize(device)
        if float(result.item()) != 12.0:
            raise RuntimeError('CUDA calculation failed.')
        from tempo_release import VERSION, build_identity
        _atomic_report(report_path, dict(success=True, cuda_version=torch.version.cuda,
                                         device=torch.cuda.get_device_name(device),
                                         app_version=VERSION, commit=build_identity().get('commit')))
        return 0
    except Exception as error:
        _atomic_report(report_path, dict(success=False, error=type(error).__name__))
        return 1


def predict_command(image_path, model_path, conf, iou, report_path):
    """Run Cellpose headlessly and write a result despite windowed stdout."""
    try:
        os.environ.update(HF_HUB_OFFLINE='1', YOLO_AUTOINSTALL='false')
        image = Path(image_path)
        model = Path(model_path)
        if not image.is_file() or not model.is_file():
            raise FileNotFoundError('The selected image or model is unavailable.')
        confidence, overlap = float(conf), float(iou)
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in (confidence, overlap)):
            raise ValueError('Invalid inference thresholds.')
        import torch
        if not torch.cuda.is_available() or not torch.version.cuda:
            raise RuntimeError('CUDA is unavailable on this computer.')
        from yolo_inference_helper import run_inference
        with open(os.devnull, 'w', encoding='utf-8') as output:
            from contextlib import redirect_stdout
            with redirect_stdout(output):
                result = run_inference(str(image), str(model), conf=confidence, iou=overlap,
                                       require_cuda=True)
        if not isinstance(result, dict) or type(result.get('success')) is not bool:
            raise RuntimeError('Cellpose returned an invalid result.')
        _atomic_report(report_path, result)
        return 0 if result['success'] else 1
    except Exception as error:
        _atomic_report(report_path, dict(success=False, error=type(error).__name__))
        return 1
