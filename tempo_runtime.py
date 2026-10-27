"""Initialize Windows model runtimes before other native libraries load."""
import importlib
import os
from pathlib import Path
import sys


def configure_matplotlib_cache():
    """Keep generated cache files out of the source and installed payload."""
    if 'MPLCONFIGDIR' not in os.environ:
        if sys.platform == 'win32':
            location = os.environ.get('LOCALAPPDATA')
            root = Path(location) if location else Path.home() / 'AppData/Local'
        elif sys.platform == 'darwin':
            root = Path.home() / 'Library/Caches'
        else:
            location = os.environ.get('XDG_CACHE_HOME')
            root = Path(location) if location else Path.home() / '.cache'
        os.environ['MPLCONFIGDIR'] = str(root / 'TEMPO/matplotlib')
    cache = Path(os.environ['MPLCONFIGDIR'])
    cache.mkdir(parents=True, exist_ok=True)
    return str(cache)


def prepare_windows_runtime():
    if sys.platform != 'win32':
        return {}
    # TensorFlow 2.20's Windows DLL initialization fails after pandas loads.
    # Torch also needs to initialize before the Windows Qt runtime. Use the
    # same order in the GUI entry point and the combined installation probe.
    modules = {name: importlib.import_module(name) for name in ('tensorflow', 'torch')}
    return {name: module.__version__ for name, module in modules.items()}
