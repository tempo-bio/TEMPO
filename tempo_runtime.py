"""Initialize Windows model runtimes before other native libraries load."""
import importlib
import os
from pathlib import Path
import sys
import threading


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
    # Hugging Face creates tqdm bars for startup downloads. This windowed app
    # shows its own progress, and the released build was caught waiting in a
    # tqdm finalizer before its dashboard appeared. Disable those bars before
    # Hugging Face imports its configuration, and keep other bars thread-only.
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    from tqdm.std import tqdm
    tqdm.set_lock(threading.RLock())
    tqdm.monitor_interval = 0
    # TensorFlow 2.20's Windows DLL initialization fails after pandas loads.
    # Torch also needs to initialize before the Windows Qt runtime. Use the
    # same order in the GUI entry point and the combined installation probe.
    modules = {name: importlib.import_module(name) for name in ('tensorflow', 'torch')}
    return {name: module.__version__ for name, module in modules.items()}
