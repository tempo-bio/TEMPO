# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller Specification for TEMPO
Configured for Windows 10/11 64-bit distribution.
"""
import os
import sys
import json
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# The console entry point does not add the spec directory to sys.path.
root = Path(SPEC).resolve().parent
sys.path.insert(0, str(root))
from tempo_cloud import BUNDLE_FILES
from tempo_model_catalog import MODEL_FILES
from tempo_release import RUNTIME_IMPORTS
os.environ['YOLO_AUTOINSTALL'] = 'false'

# Dynamically collect all Ultralytics configuration YAMLs (default.yaml, etc.)
ultralytics_datas = collect_data_files('ultralytics')
ultralytics_hidden = collect_submodules('ultralytics')

# Pre-bundled models and assets
datas = [
    (str(root / 'assets'), 'assets'),
    (str(root / 'README.md'), '.'),
    (str(root / 'docs/GETTING_STARTED.md'), 'docs'),
    (str(root / 'docs/CREDENTIAL_SECURITY.md'), 'docs'),
    (str(root / 'docs/COLLABORATION_WORKFLOW.md'), 'docs'),
    (str(root / 'docs/WORKSPACE_ADMINISTRATION.md'), 'docs'),
    (str(root / 'docs/PACKAGING_AND_DISTRIBUTION.md'), 'docs'),
    (str(root / 'docs/SPATIAL_METADATA.md'), 'docs'),
    (str(root / 'docs/CAPTURE_QUALITY.md'), 'docs'),
    (str(root / 'docs/DEVELOPMENT_AND_RELEASE_GUIDE.md'), 'docs'),
    (str(root / 'docs/THIRD_PARTY_SOFTWARE.md'), 'docs'),
    (str(root / 'docs/MODEL_EVALUATION.md'), 'docs'),
    (str(root / 'installer/windows-native-runtime.json'), 'assets'),
    (str(root / 'docs/TRAINING_WORKFLOW.md'), 'docs'),
    (str(root / 'docs/MODEL_EVALUATION_WORKFLOW.md'), 'docs'),
    (str(root / 'docs/STIMULATION_SETUP.md'), 'docs'),
    (str(root / 'firmware/tempo_pacer/tempo_pacer.ino'), 'firmware/tempo_pacer'),
] + ultralytics_datas
binaries = []
if sys.platform == 'win32':
    from PyInstaller.utils.hooks import copy_metadata
    from scripts.windows_native_bundle import collect_wheel_libraries
    from scripts.collect_licenses import collect as collect_licenses
    from scripts.prepare_dependency_sources import validate, check_inventory, write_index
    notices = root/'Output/third-party-licenses'
    license_inventory = collect_licenses(notices)
    source_manifest = json.loads((root/'installer/dependency-sources.json').read_text(encoding='utf-8'))
    validate(source_manifest, root/'requirements-windows.lock')
    check_inventory(license_inventory, source_manifest)
    source_index = root/'Output/dependency-sources'
    source_files = write_index(source_manifest, source_index)
    datas += [(str(notices), 'third-party-licenses'),
              (str(root/'docs/THIRD_PARTY_SOFTWARE.md'), 'docs'),
              (str(root/'LICENSE'), '.')]
    datas += [(str(path), 'third-party-sources') for path in source_files]
    datas += copy_metadata('msvc-runtime')
    manifest = json.loads((root/'installer/windows-native-runtime.json').read_text())
    private_datas, binaries, audit = collect_wheel_libraries(manifest=manifest)
    for provider in sorted({entry['provider'] for entry in audit['files']}):
        datas += copy_metadata(provider)
    audit_path = root/'Output/wheel-native-libraries.json'
    audit_path.parent.mkdir(parents=True,exist_ok=True)
    audit_path.write_text(json.dumps(audit,indent=2)+'\n',encoding='utf-8')
    datas += private_datas + [(str(audit_path),'assets')]
identity = root / 'Output/build-provenance.json'
if identity.is_file():
    datas.append((str(identity), 'assets'))
elif sys.platform == 'win32':
    raise RuntimeError('Windows packaging requires Output/build-provenance.json')
for runtime in ('stardist', 'csbdeep', 'cellpose'):
    datas += collect_data_files(runtime)

# Cloud preparation reads these source files to create the revision-pinned handoff.
# Compiled Python modules alone cannot provide that source payload.
datas += [(str(root / name), os.path.dirname(name) or '.') for name in BUNDLE_FILES]

# Include model weights if present in the build directory
for weight_file in MODEL_FILES.values():
    if (root / weight_file).exists():
        datas.append((str(root / weight_file), '.'))

hidden_imports = [
    'tempo_onboarding',
    'PyQt6.QtCore',
    'PyQt6.QtGui',
    'PyQt6.QtWidgets',
    'PyQt6.sip',
    'pyqtgraph',
    'scipy.signal',
    'scipy.ndimage',
    'scipy.optimize',
    'cv2',
    'pymmcore',
    'pymmcore_plus',
    'huggingface_hub',
    'onnxruntime',
    'torchvision',
    'tensorflow',
    'stardist.models',
    'csbdeep.utils',
    'cellpose.models',
] + collect_submodules('dinov3.hub') + collect_submodules('dinov3.models') + collect_submodules('dinov3.layers') + ultralytics_hidden + collect_submodules('imagecodecs') + list(RUNTIME_IMPORTS)
# Verification uses dynamic imports, including the GUI entry point itself.
# The script in a.scripts is not an importable module in the PYZ archive.
hidden_imports = sorted(set(hidden_imports))
if sys.platform == 'win32':
    # Torchvision loads these stable-ABI libraries by filename. Its upstream
    # PyInstaller hook still names the older _C and image extensions.
    hidden_imports += ['torchvision._C_stable', 'torchvision.image_stable']
    hidden_imports += ['keyring.backends.Windows', 'win32ctypes.pywin32.win32cred',
                      'win32ctypes.pywin32.pywintypes', 'win32ctypes.core.ctypes']
elif sys.platform == 'darwin':
    hidden_imports += ['keyring.backends.macOS', 'keyring.backends.macOS.api']

excludes = [
    'pytest',
    'tkinter',
    'PyQt5', 'PySide2', 'PySide6',
]

a = Analysis(
    [str(root / 'tempo_gui.py')],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)

if sys.platform == 'win32':
    from scripts.windows_native_bundle import assemble_windows_cpp_runtime
    a.binaries = assemble_windows_cpp_runtime(a.binaries, root)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TEMPO',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # Windowed GUI application
    icon=str(root / 'assets/tempo_icon.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='TEMPO',
)
