"""Isolated source trees and synthetic build inputs for specification tests."""
import json
from pathlib import Path
import shutil

from tempo_cloud import BUNDLE_FILES


SPEC_CAPTURE_SCRIPT = '''
import builtins, json, sys, types
from pathlib import Path
hooks = types.ModuleType('PyInstaller.utils.hooks')
hooks.collect_data_files = lambda name: []
hooks.collect_submodules = lambda name: []
hooks.copy_metadata = lambda name: []
sys.modules['PyInstaller'] = types.ModuleType('PyInstaller')
sys.modules['PyInstaller.utils'] = types.ModuleType('PyInstaller.utils')
sys.modules['PyInstaller.utils.hooks'] = hooks
native = types.ModuleType('scripts.windows_native_bundle')
native.assemble_windows_cpp_runtime = lambda binaries,root: binaries
native.collect_wheel_libraries = lambda **kwargs: ([],[],{'success':True,'files':[]})
sys.modules['scripts.windows_native_bundle'] = native
licenses = types.ModuleType('scripts.collect_licenses')
licenses.collect = lambda destination: {'packages': []}
sys.modules['scripts.collect_licenses'] = licenses
captured = {}
def analysis(scripts, **kwargs):
    captured.update(scripts=scripts, **kwargs)
    return types.SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])
namespace = dict(SPEC=sys.argv[1], Analysis=analysis, PYZ=lambda *a,**k:None,
                 EXE=lambda *a,**k:None, COLLECT=lambda *a,**k:None)
# Select the Windows spec branch without changing the host's standard-library
# imports (a macOS interpreter cannot import Windows-only _winapi).
def spec_import(name, *args, **kwargs):
    if name == 'sys':
        return types.SimpleNamespace(platform='win32', path=sys.path)
    return builtins.__import__(name, *args, **kwargs)
namespace['__builtins__'] = dict(vars(builtins), __import__=spec_import)
exec(compile(Path(sys.argv[1]).read_bytes(), sys.argv[1], 'exec'), namespace)
print(json.dumps(captured))
'''


def stage_spec_inputs(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    required = set(BUNDLE_FILES) | {
        'tempo.spec', 'tempo_release.py', 'tempo_gui.py', 'LICENSE', 'README.md',
        'requirements-windows.lock', 'requirements-windows-nvidia.lock',
        'installer/dependency-sources.json', 'installer/dependency-sources-nvidia.json',
        'installer/windows-native-runtime.json',
        'firmware/tempo_pacer/tempo_pacer.ino',
        'scripts/windows_build_profile.py', 'scripts/prepare_dependency_sources.py',
    }
    for name in required:
        destination = target/name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source/name, destination)
    for name in ('assets', 'docs'):
        shutil.copytree(source/name, target/name)
    output = target/'Output'
    output.mkdir()
    (output/'third-party-licenses').mkdir()
    (output/'build-provenance.json').write_text(json.dumps({'commit': 'a'*40, 'profile': 'cpu'}))
    (output/'nvidia-runtime.json').write_text(json.dumps({
        'schema_version': 1, 'profile': 'nvidia-cu126', 'app_version': '1.0.1',
        'commit': 'a'*40, 'archive_size': 1, 'archive_sha256': 'b'*64,
        'archive_url': 'https://github.com/tempo-bio/TEMPO/releases/download/v1.0.1/TEMPO_NVIDIA_Runtime_v1.0.1.zip',
        'executable': 'TEMPO/TEMPO.exe', 'executable_sha256': 'c'*64,
    }))
    return target
