"""Bounded, headless model assistance shared by packaged CPU and NVIDIA workers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


STAGES = {
    'weights': 'Loading model…',
    'image': 'Reading image…',
    'nuclear': 'Preparing nuclear guidance…',
    'inference': 'Running model…',
    'result': 'Preparing annotations…',
}
RESULT_TEXT = {'scout': 'Preparing counts…', 'stardist': 'Preparing nuclear proposals…',
               'cellpose': 'Preparing outlines…'}
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_STATUS_BYTES = 1024
TIMEOUT_SECONDS = 900


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + '-', suffix='.tmp', dir=path.parent)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def assist_command(image, model, conf, iou, result_path, status_path, *, require_cuda=False):
    """Run before importing Qt or desktop bootstrap in a frozen executable."""
    def stage(value):
        if value in STAGES:
            _atomic_json(status_path, {'stage': value})
    try:
        from yolo_inference_helper import run_inference
        response = run_inference(image, model, conf=float(conf), iou=float(iou),
                                 require_cuda=require_cuda, progress=stage)
        if not isinstance(response, dict) or type(response.get('success')) is not bool:
            raise RuntimeError('Model assistance returned an invalid result.')
        _atomic_json(result_path, response)
        return 0 if response['success'] else 1
    except Exception as error:
        from tempo_credentials import redact_credentials
        _atomic_json(result_path, {'success': False, 'error': redact_credentials(str(error))[:2000]})
        return 1


def _stop_tree(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


def run_assistance(image, model, conf, iou, progress=lambda text: None, *,
                   stage=lambda code: None, timeout=TIMEOUT_SECONDS):
    """Poll actual child stages; bound native hangs and never launch a dashboard."""
    if model not in ('scout', 'stardist', 'cellpose'):
        raise ValueError('Unknown assistance model.')
    executable = sys.executable
    command = '--assist-model'
    frozen = bool(getattr(sys, 'frozen', False))
    if not frozen and sys.platform == 'darwin' and '.venv_x86' in executable:
        native = Path(__file__).resolve().parent / '.venv' / 'bin' / 'python'
        if native.is_file():
            executable = str(native)
    if model == 'cellpose' and sys.platform == 'win32' and getattr(sys, 'frozen', False):
        from tempo_nvidia_runtime import active_executable
        worker = active_executable()
        if worker is not None:
            executable, command = str(worker), '--assist-model-cuda'
    with tempfile.TemporaryDirectory(prefix='tempo-assistance-') as directory:
        report = Path(directory) / 'result.json'
        status = Path(directory) / 'status.json'
        arguments = [str(executable)]
        if not frozen:
            arguments.append(str(Path(__file__).resolve().with_name('tempo_gui.py')))
        arguments.extend((command, str(image), model, str(conf), str(iou), str(report), str(status)))
        options = ({'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)} if os.name == 'nt'
                   else {'start_new_session': True})
        process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
        last_stage = None
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Model assistance exceeded 15 minutes; no annotations were changed.')
                try:
                    if status.stat().st_size <= MAX_STATUS_BYTES:
                        value = json.loads(status.read_text(encoding='utf-8')).get('stage')
                        if value in STAGES and value != last_stage:
                            last_stage = value
                            stage(value)
                            progress(RESULT_TEXT[model] if value == 'result' else STAGES[value])
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
                time.sleep(0.25)
            if status.is_file() and status.stat().st_size <= MAX_STATUS_BYTES:
                try:
                    value = json.loads(status.read_text(encoding='utf-8')).get('stage')
                    if value in STAGES and value != last_stage:
                        stage(value)
                        progress(RESULT_TEXT[model] if value == 'result' else STAGES[value])
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
        finally:
            _stop_tree(process)
        if not report.is_file() or report.stat().st_size > MAX_REPORT_BYTES:
            raise RuntimeError('The model worker did not return a valid result.')
        result = json.loads(report.read_text(encoding='utf-8'))
        if not isinstance(result, dict) or type(result.get('success')) is not bool:
            raise RuntimeError('The model worker returned an invalid result.')
        if process.returncode and result['success']:
            raise RuntimeError('The model worker exited unexpectedly.')
        return result
