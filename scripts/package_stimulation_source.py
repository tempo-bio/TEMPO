"""Archive the tracked package source tree with stable ZIP metadata."""
import argparse
from pathlib import Path
import subprocess
import zipfile


def archive_source(root, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode('utf-8').split('\0')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(filter(None, names)):
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('Expected a regular tracked source file: ' + name)
            info = zipfile.ZipInfo(name, (2026, 11, 2, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('.'))
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    print(archive_source(arguments.source, arguments.output))
