"""Bounded, offline verification entry point for the Windows executable."""
import importlib
import io
import json
import os
from pathlib import Path
import sys
import traceback

VERSION = '1.0.0'

RUNTIME_IMPORTS = ('cv2', 'numpy', 'scipy.signal', 'pandas', 'matplotlib', 'pyqtgraph',
                   'torch', 'torchvision', 'ultralytics', 'tensorflow', 'stardist.models',
                   'csbdeep.utils', 'cellpose.models', 'dinov3.hub.backbones', 'huggingface_hub', 'keyring', 'onnxruntime',
                   'pymmcore', 'pymmcore_plus', 'serial', 'serial.tools.list_ports',
                   'tempo_logo', 'tempo_activity_icon', 'tempo_gui')


def verify_plot_lifecycle(application):
    """Exercise bundled plot axes offscreen without recordings or hardware."""
    import pyqtgraph as pg
    from PyQt6 import sip
    from PyQt6.QtCore import QCoreApplication, QEvent
    from PyQt6.QtWidgets import QWidget, QVBoxLayout
    from tempo_plot import PlotWidget, ColorBarItem, add_plot

    host = QWidget()
    try:
        layout = QVBoxLayout(host)
        graph = pg.GraphicsLayoutWidget()
        layout.addWidget(graph)
        plot = add_plot(graph)
        plot.plot([0, 1, 2], [0, 2, 0])
        bar = ColorBarItem(values=(0, 2), colorMap=pg.colormap.get('viridis'))
        graph.addItem(bar)
        signal = PlotWidget()
        layout.addWidget(signal)
        signal.plot([0, 1, 2], [0, 2, 0])
        axes = [entry['item'] for item in (plot, bar, signal.getPlotItem())
                for entry in item.axes.values()]
        host.resize(900, 600)
        host.show()
        application.processEvents()
        host.resize(640, 480)
        application.processEvents()
    finally:
        host.close()
        from tempo_tasks import dispose_closed_views
        dispose_closed_views(host)
        application.processEvents()
    if not all(sip.isdeleted(axis) for axis in axes):
        raise RuntimeError('Plot verification did not finish widget disposal.')
    for axis in axes:
        axis.resizeEvent()  # reproduce the late callback seen in Windows logs
    application.processEvents()


REPORT_SCHEMA = 2
EXTRA_CHECKS = ('bundled payload', 'Windows Qt platform dependencies',
                'offscreen Qt startup and close', 'offscreen plot resize and disposal',
                'scientific processing and export', 'combined native runtime')
REQUIRED_CHECKS = RUNTIME_IMPORTS + EXTRA_CHECKS


def build_identity():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    candidates = (root/'assets/build-provenance.json', root/'Output/build-provenance.json')
    for path in candidates:
        if path.is_file():
            return json.loads(path.read_text(encoding='utf-8'))
    return {'commit': os.environ.get('GITHUB_SHA', 'local-uncommitted')}


def module_origin(module):
    origin = getattr(module, '__file__', None)
    if not origin:
        raise RuntimeError(f'Runtime {module.__name__} has no attributable module origin.')
    path = Path(origin).resolve()
    if getattr(sys, 'frozen', False):
        root = Path(sys._MEIPASS).resolve()
        if not path.is_relative_to(root):
            raise RuntimeError(f'Runtime came from outside the executable bundle: {path}')
    return str(path)


def runtime_operation(name, module):
    """Small offline operations exercise compiled runtimes without pretrained weights."""
    import numpy as np
    if name == 'keyring':
        from tempo_secure_store import native_backend
        import uuid
        backend = native_backend()
        service = 'TEMPO/ReleaseVerification/' + uuid.uuid4().hex
        value = 'synthetic-release-verification'
        try:
            backend.set_password(service, 'fixture', value)
            if backend.get_password(service, 'fixture') != value:
                raise RuntimeError('Native credential-store round trip failed.')
        finally:
            if backend.get_password(service, 'fixture') is not None:
                backend.delete_password(service, 'fixture')
        if backend.get_password(service, 'fixture') is not None:
            raise RuntimeError('Native credential-store verification left a credential behind.')
        return {'operation': 'native credential-store write/read/delete'}
    if name == 'cv2':
        image = np.array([[1, 3], [2, 4]], dtype=np.uint8)
        if int(module.absdiff(image, image + 1).sum()) != 4:
            raise RuntimeError('OpenCV arithmetic failed.')
        import tempfile
        with tempfile.TemporaryDirectory(prefix='TEMPO video with spaces ') as directory:
            path = str(Path(directory)/'recording with spaces.avi')
            # Match CaptureWorker's uncompressed AVI output. Force the bundled
            # FFmpeg backend so a development/system codec cannot hide omission.
            writer = module.VideoWriter(path, module.CAP_FFMPEG, 0, 10., (64,48))
            try:
                if not writer.isOpened(): raise RuntimeError('Bundled AVI writer could not open.')
                for value in (40,80,120):
                    writer.write(np.full((48,64,3), value, dtype=np.uint8))
            finally:
                writer.release()
            capture = module.VideoCapture(path, module.CAP_FFMPEG)
            try:
                if not capture.isOpened(): raise RuntimeError('Bundled AVI reader could not open.')
                frames = []
                while len(frames) < 4:
                    ready, frame = capture.read()
                    if not ready: break
                    if frame.shape != (48,64,3): raise RuntimeError('AVI frame dimensions changed.')
                    frames.append(float(frame.mean()))
                if len(frames) != 3: raise RuntimeError(f'AVI frame count changed: {frames}')
                np.testing.assert_allclose(frames, [40,80,120], atol=3)
                video = {'backend':capture.getBackendName(),'frames':len(frames),'frame_means':frames,
                         'format':'uncompressed AVI','shape':[48,64,3]}
                if getattr(sys,'frozen',False):
                    video['native_libraries'] = loaded_native_libraries()
            finally:
                capture.release()
        return {'operation':'absdiff and AVI write/read','version':module.__version__,
                'build':module.getBuildInformation(),'video':video}
    if name == 'numpy':
        np.testing.assert_allclose(module.linalg.solve([[2., 0.], [0., 2.]], [2., 4.]), [1., 2.])
    elif name == 'scipy.signal':
        if module.find_peaks([0, 1, 0, 2, 0])[0].tolist() != [1, 3]:
            raise RuntimeError('SciPy peak extraction failed.')
    elif name == 'pandas':
        with __import__('tempfile').TemporaryDirectory() as directory:
            path = Path(directory)/'results with spaces.csv'
            module.DataFrame({'value': [1., 2.]}).to_csv(path, index=False)
            if module.read_csv(path)['value'].sum() != 3.: raise RuntimeError('CSV round trip failed.')
    elif name == 'torch':
        if module.ones(2, 2).matmul(module.ones(2, 2)).sum().item() != 8:
            raise RuntimeError('CPU Torch matrix operation failed.')
    elif name == 'torchvision':
        import torch
        keep = module.ops.nms(torch.tensor([[0., 0., 2., 2.], [0., 0., 2., 2.]]), torch.tensor([.9, .5]), .5)
        if keep.tolist() != [0]: raise RuntimeError('Torchvision native NMS failed.')
    elif name == 'ultralytics':
        # YAML model construction and a tiny random-weight CPU inference are offline.
        model = module.YOLO('yolo11n-seg.yaml')
        prediction = model.predict(np.zeros((64, 64, 3), dtype=np.uint8), imgsz=64, device='cpu', verbose=False)
        if len(prediction) != 1: raise RuntimeError('Ultralytics inference returned no result.')
    elif name == 'tensorflow':
        result = module.linalg.matmul(module.ones((2, 2)), module.ones((2, 2))).numpy()
        np.testing.assert_allclose(result, np.full((2, 2), 2.))
    elif name == 'stardist.models':
        from stardist.geometry import star_dist
        labels = np.zeros((16, 16), dtype=np.uint16); labels[4:12, 4:12] = 1
        rays = star_dist(labels, n_rays=8, mode='cpp')
        if rays.shape != (16, 16, 8) or not np.isfinite(rays).all():
            raise RuntimeError('StarDist native ray geometry failed.')
        import tensorflow as tf
        with tf.device('/CPU:0'):
            model = module.StarDist2D(module.Config2D(n_rays=8, grid=(2,2), n_channel_in=1,
                unet_n_depth=1, unet_n_filter_base=4, train_patch_size=(32,32)),
                name='installation_probe', basedir=None)
            probability, distance = model.predict(np.zeros((32,32), dtype=np.float32), verbose=False)
        if probability.shape != (16,16) or distance.shape != (16,16,8) or not np.isfinite(probability).all() or not np.isfinite(distance).all():
            raise RuntimeError('StarDist random-weight CPU network failed.')
        return {'operation': 'native rays and random-weight CPU network', 'output_shape': list(distance.shape)}
    elif name == 'csbdeep.utils':
        normalized = module.normalize(np.arange(64).reshape(8, 8))
        if not np.isfinite(normalized).all(): raise RuntimeError('CSBDeep normalization failed.')
    elif name == 'cellpose.models':
        from cellpose import dynamics
        labels = np.zeros((16, 16), dtype=np.int32); labels[4:12, 4:12] = 1
        flows, _ = dynamics.masks_to_flows_gpu(labels, device=__import__('torch').device('cpu'))
        if flows.shape != (2, 16, 16) or not np.isfinite(flows).all():
            raise RuntimeError('Cellpose CPU flow extraction failed.')
    elif name == 'onnxruntime':
        providers = module.get_available_providers()
        if 'CPUExecutionProvider' not in providers:
            raise RuntimeError('ONNX Runtime CPU provider is unavailable.')
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
        model = root/'assets/verification_add.onnx'
        session = module.InferenceSession(str(model), providers=['CPUExecutionProvider'])
        values = np.array([[1.,2.],[3.,4.]], dtype=np.float32)
        result, = session.run(None, {'x': values})
        np.testing.assert_allclose(result, values*2)
        return {'operation': 'offline CPU graph inference', 'providers': session.get_providers(),
                'model': str(model), 'output': result.tolist()}
    return {'operation': 'offline runtime check'}


def scientific_processing_export():
    import tempfile
    import numpy as np
    import pandas as pd
    from vk_math_engine import analyze_transients
    from tempo_statistics import compile_master_dataset
    from tempo_training import read_image_plane
    import cv2
    with tempfile.TemporaryDirectory(prefix='TEMPO scientific fixture ') as directory:
        root = Path(directory)
        image = np.arange(256, dtype=np.uint8).reshape(16, 16)
        ok, encoded = cv2.imencode('.png', image)
        if not ok: raise RuntimeError('Image encoding failed.')
        image_path = root/'cell image.png'; encoded.tofile(str(image_path))
        np.testing.assert_array_equal(read_image_plane(image_path), image)
        import tifffile
        tiff_path = root/'compressed cell image.tif'
        tifffile.imwrite(tiff_path,image,compression='lzw')
        np.testing.assert_array_equal(tifffile.imread(tiff_path),image)
        np.testing.assert_array_equal(read_image_plane(tiff_path),image)
        time = np.arange(500)/100.
        signal = 20*np.sin(np.pi*time/0.8)**8
        metrics = analyze_transients(list(zip(time*1000., signal)), fps=100.)
        if not metrics: raise RuntimeError('Synthetic contraction peaks were not extracted.')
        rows = [{'Well_ID': 'A01', 'FOV_ID': 'FOV001', 'Cell_ID': '1',
                 'Peak Amplitude (a.u.)': m['Peak_Amplitude_Raw_au'],
                 'Time-to-Peak (ms)': m['Time_to_Peak_ms']} for m in metrics]
        pd.DataFrame(rows).to_csv(root/'A01_FOV001_cell_1_transients_summary.csv', index=False)
        master = compile_master_dataset(str(root))
        if master is None or master.empty: raise RuntimeError('Master results compilation failed.')
        exported = root/'master results.csv'; master.to_csv(exported, index=False)
        if pd.read_csv(exported).empty: raise RuntimeError('Results export failed.')
        return {'beats': len(metrics), 'export_rows': len(master), 'image_shape': list(image.shape),
                'image_formats':['PNG','LZW-compressed TIFF']}


def probe_action(name):
    if name in RUNTIME_IMPORTS:
        module = importlib.import_module(name)
        return dict(origin=module_origin(module), **runtime_operation(name, module))
    if name == 'bundled payload':
        from tempo_cloud import BUNDLE_FILES
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
        for filename in BUNDLE_FILES:
            path = root/filename
            if not path.is_file(): raise RuntimeError(f'Missing bundled cloud source: {filename}')
            if filename.endswith('.py'): compile(path.read_bytes(), filename, 'exec')
        for asset in ('tempo_icon.ico','tempo_icon.png','tempo_logo_base.png','verification_add.onnx'):
            if not (root/'assets'/asset).is_file(): raise RuntimeError(f'Required bundled asset is missing: {asset}')
        if not (root/'docs/TRAINING_WORKFLOW.md').is_file():
            raise RuntimeError('Bundled training workflow is missing.')
        for relative in ('docs/STIMULATION_SETUP.md','firmware/tempo_pacer/tempo_pacer.ino'):
            if not (root/relative).is_file():
                raise RuntimeError('Bundled stimulation setup is missing: ' + relative)
        identity = build_identity()
        if getattr(sys, 'frozen', False) and not identity.get('lock_sha256'):
            raise RuntimeError('Bundled build identity is missing.')
        details = {'sources': list(BUNDLE_FILES), 'identity': identity}
        import hashlib
        license_path = root/'LICENSE'
        if not license_path.is_file():
            raise RuntimeError('The project license is missing.')
        license_digest = hashlib.sha256(license_path.read_bytes()).hexdigest()
        expected_license = identity.get('project_license_sha256')
        if (getattr(sys, 'frozen', False) or expected_license) and license_digest != expected_license:
            raise RuntimeError('The project license differs from the tested source revision.')
        details['project_license'] = {'identifier': 'AGPL-3.0-only', 'sha256': license_digest}
        source_index = root/('third-party-sources/dependency-sources.json' if getattr(sys, 'frozen', False)
                             else 'installer/dependency-sources.json')
        if not source_index.is_file():
            raise RuntimeError('The dependency source index is missing.')
        source_digest = hashlib.sha256(source_index.read_bytes()).hexdigest()
        expected_sources = identity.get('dependency_sources_sha256')
        if (getattr(sys, 'frozen', False) or expected_sources) and source_digest != expected_sources:
            raise RuntimeError('The dependency source index differs from the tested source revision.')
        details['dependency_sources'] = {'sha256': source_digest}
        if getattr(sys, 'frozen', False):
            details['cpp_runtime'] = verify_bundled_cpp_runtime(root)
            details['wheel_libraries'] = verify_wheel_libraries(root)
        return details
    if name == 'Windows Qt platform dependencies':
        import ctypes
        from PyQt6.QtCore import QLibraryInfo
        plugin = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))/'platforms/qwindows.dll'
        if not plugin.is_file(): raise RuntimeError('qwindows.dll is missing.')
        if getattr(sys, 'frozen', False) and not plugin.resolve().is_relative_to(Path(sys._MEIPASS).resolve()):
            raise RuntimeError('Windows Qt platform plugin comes from outside the bundle.')
        ctypes.WinDLL(str(plugin))  # load dependency graph without launching a desktop GUI
        return {'plugin': str(plugin)}
    if name in ('offscreen Qt startup and close', 'offscreen plot resize and disposal'):
        from tempo_tasks import dispose_application
        from PyQt6.QtWidgets import QApplication
        application = QApplication.instance() or QApplication([])
        from tempo_ui import prepare_offscreen_fonts
        fonts = prepare_offscreen_fonts(application)
        if name == 'offscreen plot resize and disposal':
            verify_plot_lifecycle(application)
        else:
            # Construct the shipped dashboard, including its lazily imported
            # pages. External services and research storage use offline fixtures.
            import copy
            import tempfile
            from contextlib import ExitStack
            from unittest.mock import patch
            from PyQt6.QtCore import QTimer
            from tempo_tasks import dispose_closed_views
            import tempo_gui as gui
            import maintainer_tab
            from tempo_config import DEFAULT_PREFERENCES
            application.setStyle('Fusion')
            application.setQuitOnLastWindowClosed(False)
            with tempfile.TemporaryDirectory(prefix='TEMPO dashboard ') as directory, ExitStack() as patches:
                root = Path(directory)
                patches.enter_context(patch.object(gui.tempo_preferences,'_preferences',copy.deepcopy(DEFAULT_PREFERENCES)))
                patches.enter_context(patch.object(gui.tempo_backend,'UNIVERSAL_DATASET_DIR',str(root)))
                patches.enter_context(patch.object(gui.tempo_backend,'QUEUE_FILE',str(root/'queue.json')))
                patches.enter_context(patch.object(gui.tempo_hub,'get_collaborator_id',return_value='Installation Fixture'))
                patches.enter_context(patch.object(gui.tempo_hub,'cached_lock_manifest',return_value={'images':{}}))
                patches.enter_context(patch.object(gui.tempo_hub,'is_maintainer',return_value=False))
                patches.enter_context(patch.object(gui,'run_background',return_value=None))
                patches.enter_context(patch.object(maintainer_tab,'run_background',return_value=None))
                widget = gui.TempoDashboard()
                if (widget.activity_icon.enabled or widget.activity_icon.poll_timer.isActive()
                        or widget.activity_icon.animation_timer.isActive() or widget.activity_icon.frames is not None):
                    raise RuntimeError('Activity icon animation must be inactive by default.')
                for timer in widget.findChildren(QTimer): timer.stop()
                widget.resize(960,620); widget.show()
                for _ in range(8): application.processEvents()
                if (widget.width(),widget.height()) != (960,620):
                    raise RuntimeError('Installed dashboard cannot fit its ordinary window size.')
                pages = [widget.tabs.tabText(index) for index in range(widget.tabs.count())]
                destroyed, shutdown_errors, disposal = [], [], {}
                widget.destroyed.connect(lambda: destroyed.append(True))
                def close_dashboard():
                    try:
                        widget.close()
                        disposal.update(dispose_closed_views(widget))
                    except BaseException as error:
                        shutdown_errors.append(str(error))
                    finally:
                        application.quit()
                deadline = QTimer(application); deadline.setSingleShot(True)
                deadline.timeout.connect(lambda: application.exit(1)); deadline.start(5000)
                QTimer.singleShot(0,close_dashboard)
                exit_code = application.exec(); deadline.stop()
                if exit_code or shutdown_errors or not destroyed:
                    raise RuntimeError(f'Dashboard shutdown did not complete: {shutdown_errors}')
            result = {'platform':application.platformName(),'fonts':fonts,'dashboard_pages':pages,
                      'external_services':'offline fixtures','window_size':[960,620], **disposal}
            dispose_application(application)
            result['application_disposed'] = True
            return result
        result = {'platform': application.platformName(), 'fonts': fonts}
        dispose_application(application)
        result['application_disposed'] = True
        return result
    if name == 'scientific processing and export': return scientific_processing_export()
    if name == 'combined native runtime':
        # Individual probes identify a crashing dependency. This additional
        # probe catches libraries that work alone but conflict in the real app.
        from tempo_runtime import prepare_windows_runtime
        bootstrap = prepare_windows_runtime()
        modules = {key: importlib.import_module(key) for key in RUNTIME_IMPORTS}
        for key in ('cv2', 'torch', 'torchvision', 'tensorflow', 'ultralytics', 'stardist.models', 'cellpose.models', 'onnxruntime'):
            runtime_operation(key, modules[key])
        from PyQt6.QtWidgets import QApplication
        application = QApplication.instance() or QApplication([])
        from tempo_ui import prepare_offscreen_fonts
        prepare_offscreen_fonts(application)
        verify_plot_lifecycle(application)
        result = scientific_processing_export()
        result['origins'] = {key: module_origin(module) for key, module in modules.items()}
        result['bootstrap'] = bootstrap
        from tempo_tasks import dispose_application
        dispose_application(application)
        result['application_disposed'] = True
        return result
    raise ValueError(f'Unknown verification check: {name}')


def verification_probe(name, report_path):
    # Windowed executables do not expose Python's stdout/stderr. Keep their
    # diagnostics in a separate file rather than colliding with the parent log.
    stream = None
    previous_stdout, previous_stderr = sys.stdout, sys.stderr
    if sys.stdout is None or sys.stderr is None:
        stream = Path(report_path).with_suffix('.python.log').open('w', encoding='utf-8')
        if sys.stdout is None: sys.stdout = stream
        if sys.stderr is None: sys.stderr = stream
    result = dict(check=name, success=False)
    try:
        if sys.platform != 'win32': raise RuntimeError('Native installation probes are Windows-only.')
        import faulthandler
        faulthandler.enable(file=sys.stderr)
        faulthandler.dump_traceback_later(60, repeat=True, file=sys.stderr)
        result['details'] = probe_action(name)
        if getattr(sys, 'frozen', False):
            result['details']['native_libraries'] = loaded_native_libraries()
        result['success'] = True
    except BaseException as error:
        result.update(error=f'{type(error).__name__}: {error}', traceback=traceback.format_exc())
    finally:
        if sys.platform == 'win32':
            import faulthandler
            faulthandler.cancel_dump_traceback_later()
    sys.stdout.flush()
    sys.stderr.flush()
    if stream is not None:
        import faulthandler
        faulthandler.disable()
        sys.stdout, sys.stderr = previous_stdout, previous_stderr
        stream.close()
    Path(report_path).write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    return 0 if result['success'] else 1


def loaded_native_libraries():
    """Reject DLLs accidentally supplied by a build Python or other dev tools."""
    import ctypes
    from ctypes import wintypes
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    process = kernel.GetCurrentProcess()
    handles = (wintypes.HMODULE * 4096)()
    needed = wintypes.DWORD()
    psapi.EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    if not psapi.EnumProcessModules(process, handles, ctypes.sizeof(handles), ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    if needed.value > ctypes.sizeof(handles): raise RuntimeError('DLL inventory buffer exceeded.')
    roots = [Path(sys._MEIPASS).resolve(), Path(sys.executable).resolve().parent,
             Path(os.environ['SystemRoot']).resolve()]
    paths = []
    for handle in handles[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
        buffer = ctypes.create_unicode_buffer(32768)
        if not psapi.GetModuleFileNameExW(process, handle, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())
        path = Path(buffer.value).resolve()
        if not any(path.is_relative_to(root) for root in roots):
            paths.append({'path': str(path), 'system_security': verify_system_security_library(path)})
        else:
            paths.append({'path': str(path)})
    return sorted(paths, key=lambda entry: entry['path'])


def verify_system_security_library(path):
    """Allow the observed Defender DLL only with a valid Microsoft signature."""
    import re
    root = Path(os.environ['ProgramData']) / 'Microsoft/Windows Defender/Platform'
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        relative = Path()
    if (len(relative.parts) != 2 or relative.name.lower() != 'mpoav.dll'
            or not re.fullmatch(r'\d+(?:\.\d+){3}(?:-\d+)?', relative.parts[0])):
        raise RuntimeError(f'Native library came from outside the bundle/system: {path}')
    signature = windows_signature(path)
    if (signature.get('status') != 'Valid'
            or not re.search(r'(?:^|,\s*)O=Microsoft Corporation(?:,|$)', signature.get('subject', ''))
            or not signature.get('thumbprint') or not signature.get('sha256')):
        raise RuntimeError(f'Untrusted Windows Defender library: {path}: {signature}')
    return signature


def windows_signature(path):
    import base64
    import subprocess
    shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    command = r"""$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
$signature=Get-AuthenticodeSignature -LiteralPath $env:TEMPO_SIGNATURE_FILE
@{status=$signature.Status.ToString();subject=$signature.SignerCertificate.Subject;
thumbprint=$signature.SignerCertificate.Thumbprint;
sha256=(Get-FileHash -LiteralPath $env:TEMPO_SIGNATURE_FILE -Algorithm SHA256).Hash;
version=(Get-Item -LiteralPath $env:TEMPO_SIGNATURE_FILE).VersionInfo.FileVersion} | ConvertTo-Json -Compress
"""
    # The runner uses PowerShell 7; its inherited modules cannot load in 5.1.
    # A copied Windows environment may use uppercase keys, so remove all casing.
    environment = {key:value for key,value in os.environ.items() if key.upper() != 'PSMODULEPATH'}
    environment['PSModulePath'] = str(shell.parent/'Modules')
    environment['TEMPO_SIGNATURE_FILE'] = str(path)
    encoded = base64.b64encode(command.encode('utf-16le')).decode('ascii')
    completed = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                               env=environment, capture_output=True, text=True, encoding='utf-8',
                               timeout=30)
    if completed.returncode:
        raise RuntimeError(f'Windows signature command failed for {path} ({completed.returncode}): '
                           f'{completed.stderr.strip()} {completed.stdout.strip()}')
    return json.loads(completed.stdout.lstrip('\ufeff'))


def verify_bundled_cpp_runtime(root):
    import hashlib
    manifest = json.loads((root/'assets/windows-native-runtime.json').read_text())
    copies = {name: [] for name in manifest['files']}
    for path in root.rglob('*.dll'):
        name = path.name.lower()
        if name in copies:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != manifest['files'][name]['sha256']:
                raise RuntimeError(f'Conflicting bundled C++ runtime: {path}')
            copies[name].append(str(path.relative_to(root)))
    missing = [name for name, paths in copies.items() if not paths]
    if missing:
        raise RuntimeError(f'Missing bundled C++ runtime: {missing}')
    return {'version': manifest['version'], 'files': manifest['files'], 'copies': copies}


def verify_wheel_libraries(root):
    import hashlib
    manifest = json.loads((root/'assets/wheel-native-libraries.json').read_text())
    if manifest.get('success') is not True or not manifest.get('files'):
        raise RuntimeError('Missing wheel library inventory.')
    for entry in manifest['files']:
        relative = Path(entry['destination'])
        path = (root/relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root.resolve()):
            raise RuntimeError(f'Unsafe bundled wheel library path: {relative}')
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise RuntimeError(f'Missing or changed bundled wheel library: {relative}')
    return {'files':manifest['files'],'count':len(manifest['files'])}


def run_probe(name, destination):
    from scripts.windows_release_checks import run_logged
    destination.unlink(missing_ok=True)
    if getattr(sys, 'frozen', False): command = [sys.executable]
    else: command = [sys.executable, str(Path(__file__).resolve().parent/'tempo_gui.py')]
    code = run_logged([*command, '--verify-probe', name, str(destination)], destination.with_suffix('.log'), 120)
    try:
        result = json.loads(destination.read_text(encoding='utf-8'))
        if not isinstance(result, dict) or result.get('check') != name:
            raise ValueError('Invalid probe identity.')
        if code or result.get('success') is not True:
            result['success'] = False
            result.setdefault('error', f'Probe process exited {code}.')
        return result
    except (OSError, ValueError) as error:
        return dict(check=name, success=False, error=f'Probe process exited {code}; {type(error).__name__}: {error}')


def verify_installation(report_path):
    report_path = Path(report_path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None: sys.stdout = io.StringIO()
    if sys.stderr is None: sys.stderr = io.StringIO()
    report = dict(schema_version=REPORT_SCHEMA, version=VERSION, success=False,
                  frozen=bool(getattr(sys, 'frozen', False)), commit=build_identity().get('commit'),
                  required_checks=list(REQUIRED_CHECKS), checks=[], failures=[], runtime_details={})
    try:
        if sys.platform != 'win32': raise RuntimeError('Executable verification is Windows-only; do not launch native macOS fixtures.')
        os.environ.update(QT_QPA_PLATFORM='offscreen', MPLBACKEND='Agg')
        probe_root = report_path.parent/(report_path.stem+'-probes'); probe_root.mkdir(exist_ok=True)
        for index, name in enumerate(REQUIRED_CHECKS):
            report['active_check'] = name
            report_path.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
            attempts = 5 if name == 'offscreen Qt startup and close' else 1
            results = []
            destinations = []
            for attempt in range(attempts):
                suffix = '' if attempt == 0 else f'-{attempt:02}'
                destination = probe_root/f'{index:02}{suffix}.json'
                result = run_probe(name, destination)
                results.append(result)
                destinations.append(destination.name)
                if result.get('success') is not True:
                    report['failures'].append(dict(result, attempt=attempt+1))
            if all(result.get('success') is True for result in results):
                report['checks'].append(name)
                details = dict(results[0].get('details', {}))
                if attempts > 1:
                    details.update(independent_processes=attempts, probe_reports=destinations)
                report['runtime_details'][name] = details
        report['success'] = not report['failures'] and set(report['checks']) == set(REQUIRED_CHECKS)
        if report['failures']:
            report['error'] = '; '.join(f"{entry['check']}: {entry.get('error', 'Failed')}" for entry in report['failures'])
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
        report['traceback'] = traceback.format_exc()
    finally:
        report.pop('active_check', None)
        report_path.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        # Keep failures diagnosable in the source job log even when GitHub
        # cannot retain uploaded evidence (for example, a storage quota error).
        print('Installation verification: ' + json.dumps(dict(
            success=report['success'], checks=report['checks'],
            failures=report['failures'], error=report.get('error'))), flush=True)
    return 0 if report['success'] else 1
