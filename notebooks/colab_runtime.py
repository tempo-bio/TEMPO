"""Standard-library-only subprocess helpers embedded in the Colab notebook.

Keep this independent of ML imports: a native crash must not take down diagnostics.
"""
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time


def configure_cuda_environment(python, env=None):
    """Expose CUDA wheels from the target environment before any ML imports.

    Inspection uses only standard-library metadata in a bounded fresh process.
    The NVIDIA driver remains Colab's; these paths contain user-space libraries.
    """
    import json
    import sys
    environment = dict(os.environ if env is None else env)
    if sys.platform != 'linux':
        return environment
    inspect = '''
import json
from importlib.metadata import distributions
from pathlib import Path
names, libraries, binaries = [], set(), set()
for distribution in distributions():
    name = distribution.metadata.get('Name', '').lower().replace('_', '-')
    if not name.startswith('nvidia-') or not name.endswith(('-cu12', '-cu13')):
        continue
    names.append(name)
    if not name.endswith('-cu12'):
        continue
    for item in distribution.files or []:
        path = Path(distribution.locate_file(item))
        if path.is_file() and (path.name.endswith('.so') or '.so.' in path.name):
            libraries.add(str(path.parent.resolve()))
        if path.is_file() and path.name == 'ptxas':
            binaries.add(str(path.parent.resolve()))
print(json.dumps(dict(packages=sorted(names), libraries=sorted(libraries), binaries=sorted(binaries))))
'''
    result = subprocess.run([str(python), '-c', inspect], check=True, capture_output=True,
                            text=True, timeout=30, env=environment)
    paths = json.loads(result.stdout)
    if any(name.endswith('-cu13') for name in paths['packages']):
        raise RuntimeError('This environment contains CUDA 13 wheels. Use the fresh CUDA 12 training environment; '
                           'do not repair the old environment by overwriting shared NVIDIA files.')
    required = {'nvidia-cuda-runtime-cu12', 'nvidia-cudnn-cu12', 'nvidia-cublas-cu12'}
    if not required <= set(paths['packages']) or not paths['libraries']:
        raise RuntimeError('CUDA 12 libraries are missing from the training environment. '
                           'Complete the corrected TensorFlow CUDA dependency setup before diagnostics.')
    for key, additions in [('LD_LIBRARY_PATH', paths['libraries']), ('PATH', paths['binaries'])]:
        existing = [value for value in environment.get(key, '').split(os.pathsep) if value]
        environment[key] = os.pathsep.join(dict.fromkeys([*additions, *existing]))
    print('CUDA 12 library search configured from the isolated training environment.', flush=True)
    return environment


def install_training_fixes(root, files):
    """Install the notebook's reviewed training sources, keeping original copies."""
    import hashlib
    import shutil
    import tempfile

    root = Path(root)
    allowed = {'train_yolo.py', 'tempo_training.py', 'requirements-training.txt', 'tempo_progress.py'}
    if not {'train_yolo.py', 'tempo_training.py', 'requirements-training.txt'} <= set(files) or not set(files) <= allowed:
        raise ValueError('Unexpected training bundle contents.')
    # Validate the whole bundle before modifying the checkout.
    for name, source in files.items():
        if name.endswith('.py'):
            compile(source, name, 'exec')
    for name, source in files.items():
        target = root / name
        data = source.encode('utf-8')
        if target.exists() and target.read_bytes() == data:
            print(f'Source file current: {name}', flush=True)
            continue
        if target.exists():
            digest = hashlib.sha256(target.read_bytes()).hexdigest()[:16]
            backup = root / '.tempo-notebook-backups' / f'{name}.{digest}'
            backup.parent.mkdir(exist_ok=True)
            if not backup.exists():
                shutil.copy2(target, backup)
        fd, temporary = tempfile.mkstemp(dir=root, prefix=f'.{name}-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print(f'Source file installed: {name}', flush=True)


def run_logged(command, *, label, cwd=None, env=None, log_path=None,
               timeout=None, heartbeat=15, cancelled=None, command_adapter=None):
    """Stream child output, report silent progress, and identify native signals."""
    child_env = dict(os.environ if env is None else env)
    if child_env.get('TEMPO_EXECUTION_RUNTIME'):
        if command_adapter is None:
            from tempo_training_runtime import execution_command
            command_adapter = execution_command
        command = command_adapter(command, child_env)
    # Colab's inline backend belongs to its kernel, not this separate environment.
    child_env.update(PYTHONUNBUFFERED="1", PYTHONFAULTHANDLER="1", MPLBACKEND="Agg")
    secrets = [child_env.get(key) for key in ("HF_TOKEN", "GITHUB_TOKEN", "TEMPO_GITHUB_TOKEN")]
    log = None
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        log = Path(log_path).open("a", encoding="utf-8")

    def emit(message):
        for secret in secrets:
            if secret:
                message = message.replace(secret, "[REDACTED]")
        print(message, end="" if message.endswith("\n") else "\n", flush=True)
        if log:
            log.write(message + ("" if message.endswith("\n") else "\n"))
            log.flush()

    process = None
    started = time.monotonic()
    try:
        emit(f"START: {label}")
        process = subprocess.Popen(
            command, cwd=cwd, env=child_env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1,
            start_new_session=(os.name == "posix"),
        )
        messages = queue.Queue()

        def read_output():
            try:
                for line in process.stdout:
                    messages.put(line)
            finally:
                messages.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        while True:
            if cancelled is not None and cancelled():
                raise InterruptedError('Cloud training cancelled by maintainer.')
            elapsed = time.monotonic() - started
            if timeout is not None and elapsed >= timeout:
                raise TimeoutError(f"{label} exceeded {timeout} seconds.")
            wait = heartbeat if timeout is None else min(heartbeat, timeout - elapsed)
            if cancelled is not None:
                wait = min(wait, 1)
            try:
                line = messages.get(timeout=max(0.001, wait))
            except queue.Empty:
                if cancelled is None or int(elapsed) % heartbeat == 0:
                    emit(f"RUNNING: {label} ({time.monotonic() - started:.0f}s elapsed)")
                continue
            if line is None:
                break
            emit(line)
        code = process.wait()
        if code:
            reason = f"exit code {code}"
            if code < 0:
                reason = f"{signal.Signals(-code).name} ({code})"
            raise RuntimeError(f"{label} failed: {reason}. See output above" +
                               (f" or {log_path}." if log_path else "."))
        emit(f"PASS: {label} ({time.monotonic() - started:.1f}s)")
    except BaseException as exc:
        emit(f"FAILED: {label} ({time.monotonic() - started:.1f}s): {type(exc).__name__}: {exc}")
        if process is not None and process.poll() is None:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()
        raise
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()
        if log:
            log.close()


def check_training_environment(python, root, cancelled=None):
    """Check libraries separately, then the real trainer's combined import path."""
    root = Path(root)
    log_path = root / "runs/colab-preflight.log"
    failures = []

    def check(label, code):
        try:
            run_logged([str(python), "-u", "-X", "faulthandler", "-c", code],
                       label=label, cwd=root, log_path=log_path, timeout=180, cancelled=cancelled)
        except (RuntimeError, TimeoutError) as exc:
            failures.append(str(exc))

    check("Installed package versions", """
import sys, importlib.metadata as metadata
print(sys.version)
for name in ['numpy', 'torch', 'torchvision', 'tensorflow', 'keras', 'stardist',
             'csbdeep', 'cellpose', 'dinov3', 'ultralytics', 'protobuf', 'numba', 'scipy', 'triton', 'tensorboard',
             'nvidia-cuda-runtime-cu12', 'nvidia-cudnn-cu12', 'nvidia-cublas-cu12']:
    try:
        print(name, metadata.version(name))
    except metadata.PackageNotFoundError:
        print(name, 'NOT INSTALLED')
""")
    for module in ["numpy", "cv2", "scipy", "pandas", "matplotlib", "huggingface_hub",
                   "torch", "ultralytics", "tensorflow", "tensorboard", "csbdeep.utils",
                   "stardist.models", "cellpose.models", "cellpose.train", "dinov3.hub.backbones", "train_yolo"]:
        check(f"Import {module}", f"import importlib; importlib.import_module({module!r})")
    if not failures:
        check("PyTorch GPU operation", """
import torch
if not torch.cuda.is_available():
    raise RuntimeError('No CUDA GPU. Select a Colab GPU runtime before training.')
print(torch.cuda.get_device_name(0))
x = torch.ones((32, 32), device='cuda')
print((x @ x).sum().item())
torch.cuda.synchronize()
""")
        check("Scout partial-supervision gradients", """
import torch
from tempo_scout_training import classification_weights
from tempo_training_runtime import check_scout_training
scores=torch.zeros((1,3,4),device='cuda',requires_grad=True)
target=torch.zeros_like(scores);target[0,1,0]=1
inside=torch.tensor([[True,True,False]],device='cuda')
loss=torch.nn.functional.binary_cross_entropy_with_logits(scores,target,reduction='none')
(loss*classification_weights(target,inside)).sum().backward()
assert scores.grad[0,0,0]==0 and scores.grad[0,1,0]!=0 and scores.grad[0,0,2]!=0
print('Scout partial-supervision gradients are finite:',torch.isfinite(scores.grad).all().item())
print('Actual YOLO26 forward/backward:',check_scout_training('cuda'))
""")
        check("TensorFlow GPU operation", """
import tensorflow as tf
gpus = tf.config.list_physical_devices('GPU')
if not gpus:
    raise RuntimeError('TensorFlow cannot access a GPU. Resolve its CUDA setup before StarDist training.')
for gpu in gpus:
    tf.config.experimental.set_memory_growth(gpu, True)
tf.config.set_soft_device_placement(False)
with tf.device('/GPU:0'):
    x = tf.ones((32, 32))
    print(tf.reduce_sum(tf.matmul(x, x)).numpy())
    # Exercise cuDNN as well as the runtime/cuBLAS before StarDist training.
    image = tf.ones((1, 16, 16, 1))
    kernel = tf.ones((3, 3, 1, 2))
    with tf.GradientTape() as tape:
        tape.watch(image)
        loss = tf.reduce_sum(tf.nn.conv2d(image, kernel, strides=1, padding='SAME'))
    gradient = tape.gradient(loss, image)
    tf.debugging.assert_all_finite(gradient, 'TensorFlow convolution gradients are not finite')
    print('GPU convolution and gradient:', loss.numpy(), gradient.shape)
""")
        # train_yolo imports Torch at module scope even for a StarDist worker.
        check("StarDist trainer compatibility", """
print('Importing TEMPO trainer', flush=True)
import train_yolo
print('Importing TensorFlow through TEMPO', flush=True)
from tempo_training import configure_tensorflow
configure_tensorflow(1024)
print('Checking TensorBoard summary writing', flush=True)
import tensorflow as tf
import tempfile
with tempfile.TemporaryDirectory() as folder:
    writer = tf.summary.create_file_writer(folder)
    try:
        with writer.as_default():
            tf.summary.scalar('preflight', 1.0, step=0)
        writer.flush()
    finally:
        writer.close()
print('Importing StarDist training API', flush=True)
from csbdeep.utils import normalize
from stardist.models import Config2D, StarDist2D
print('Constructing a small StarDist model', flush=True)
model = StarDist2D(Config2D(n_rays=8, n_channel_in=1, unet_n_depth=1,
                          unet_n_filter_base=4), name='preflight', basedir=None)
""")
        check("Cellpose training API", """
import inspect
import train_yolo
from cellpose import models, train
from dinov3.hub.backbones import dinov3_vitl16
assert callable(dinov3_vitl16)
parameters = inspect.signature(train.train_seg).parameters
for name in ['train_data', 'train_labels', 'test_data', 'test_labels', 'channel_axis',
             'n_epochs', 'batch_size', 'min_train_masks', 'save_path', 'model_name', 'bsize']:
    if name not in parameters:
        raise RuntimeError('Cellpose train_seg no longer accepts ' + name)
""")
    if failures:
        raise RuntimeError("Preflight failed; do not start training.\n" + "\n".join(failures))
    print("All dependency and GPU checks passed.", flush=True)


def check_training_steps(python, root, cancelled=None, *, dataset=None, training_check=False):
    """Run each real worker for one epoch before committing to a full run.

    Each worker also saves and benchmarks its candidate. These disposable
    candidates have their own directory and never create a publishable report.
    """
    import hashlib
    import json
    import math
    import uuid
    from tempo_credentials import redact_credentials

    root = Path(root)
    dataset = Path(dataset) if dataset is not None else root / 'datasets/compiled'
    destination = root / "runs/training-checks" / uuid.uuid4().hex
    destination.mkdir(parents=True)
    results = {}
    report = dict(success=False, results=results, errors=[], publishable=False,
                  evaluation_partition='train' if training_check else 'test')

    def save_report():
        data = json.dumps(report, indent=2)
        (destination/'summary.json').write_text(data)
        (root/'runs/preflight_report.json').write_text(data)

    print("Checking one training epoch, checkpoint saving, and validation per model.", flush=True)
    for model in ("stardist", "cellpose", "nano_scout"):
        output = destination / model
        log = destination / f"{model}.log"
        try:
            if cancelled is not None and cancelled():
                raise InterruptedError('Cancelled before the next one-epoch check.')
            command = [str(python), "-u", "-X", "faulthandler", str(root / "train_yolo.py"),
                 "--worker", model, "--epochs", "1", "--batch", "1", "--workers", "0",
                 "--device", "cuda", "--dataset", str(dataset), "--output", str(output)]
            if training_check:
                command.append('--training-check')
            run_logged(command,
                label=f"One-epoch training check: {model}", cwd=root, log_path=log,
                cancelled=cancelled,
            )
            result = json.loads((output / "result.json").read_text())
            if not Path(result["artifact"]).is_file() or not math.isfinite(result["metrics"]["score"]):
                raise ValueError("Training check did not produce a valid checkpoint and metric.")
            digest = hashlib.sha256()
            with Path(result['artifact']).open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024*1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != result['sha256']:
                raise ValueError('Training-check checkpoint checksum mismatch.')
            results[model] = result
        except (InterruptedError, KeyboardInterrupt) as exc:
            report.update(cancelled=True, errors=[redact_credentials(
                f"{model}: {exc} (log: {log})", os.environ.get('HF_TOKEN'))])
            save_report()
            raise
        except (RuntimeError, ValueError, OSError, KeyError, TypeError) as exc:
            report['errors'] = [redact_credentials(f"{model}: {exc} (log: {log})", os.environ.get('HF_TOKEN'))]
            save_report()
            raise RuntimeError("Training stopped after a failed one-epoch check:\n" + report['errors'][0]) from exc
        save_report()
    report['success'] = True
    save_report()
    print("All three model-role training checks passed.", flush=True)
    return report
