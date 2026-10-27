"""Run the real packager with periodic stack evidence and native crash traces."""
import faulthandler
import os
import sys
import json
import importlib.metadata
from pathlib import Path
import re
import threading
import traceback
import types


def loaded_native_extensions():
    """Inspect loaded files without invoking module proxies or lazy imports."""
    extensions = []
    for name, module in list(sys.modules.items()):
        if not isinstance(module, types.ModuleType):
            continue
        path = types.ModuleType.__getattribute__(module, '__dict__').get('__file__')
        if isinstance(path, str) and path.lower().endswith(('.pyd', '.dll', '.so', '.dylib')):
            extensions.append({'module': name, 'path': path})
    return extensions


def start_stack_sampler(interval=120, stream=None, label='Packaging'):
    """Take Python-owned frame snapshots rather than a native timer dump."""
    stream = stream or sys.stderr
    stopped = threading.Event()
    def sample():
        while not stopped.wait(interval):
            frame = sys._current_frames().get(threading.main_thread().ident)
            print(label+' stack snapshot:',file=stream,flush=True)
            if frame is not None: traceback.print_stack(frame,limit=40,file=stream)
            extensions = loaded_native_extensions()
            print('Parent native Python extensions:',json.dumps(extensions),file=stream,flush=True)
            stream.flush()
    thread = threading.Thread(target=sample,name=label.lower().replace(' ','-')+'-stack-sampler',daemon=True)
    thread.start()
    return stopped,thread


def check_environment(expected, distributions=None):
    normalize = lambda name: re.sub(r'[-_.]+','-',name).lower()
    expected = {normalize(name):version for name,version in expected.items()}
    actual = {normalize(d.metadata['Name']):d.version for d in
              (distributions if distributions is not None else importlib.metadata.distributions())}
    if actual != expected:
        difference = {name:{'expected':expected.get(name),'actual':actual.get(name)}
                      for name in actual.keys() | expected.keys() if actual.get(name) != expected.get(name)}
        raise RuntimeError(f'Packaging changed the locked environment: {difference}')
    return actual


def main():
    os.environ['PYTHONFAULTHANDLER'] = '1'
    os.environ['YOLO_AUTOINSTALL'] = 'false'
    faulthandler.enable()
    stopped,sampler = start_stack_sampler()
    try:
        root = Path(__file__).resolve().parents[1]
        expected = json.loads((root/'Output/build-provenance.json').read_text())['packages']
        check_environment(expected)
        from PyInstaller.__main__ import run
        run(sys.argv[1:])
        packages = check_environment(expected)
        (root/'Output/postbuild-environment.json').write_text(
            json.dumps({'success':True,'packages':packages},indent=2)+'\n',encoding='utf-8')
    finally:
        stopped.set()
        sampler.join(timeout=5)


if __name__ == '__main__':
    main()
