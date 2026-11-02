"""Build the optional collection package from the published v1.0.1 executable.

The native bootloader and installed runtime are reused. Only selected Python
modules and explicitly listed investigation resources are replaced. The archive
structures follow PyInstaller 6's documented CArchive/PYZ formats.
"""
import argparse
import hashlib
import importlib.util
import json
import marshal
from pathlib import Path
import struct
import sys
import zipfile
import zlib

PACKAGE_VERSION = '0.1.0'
BASE_COMMIT = 'b386e96984c581bf8e8afd4679cd1500552386bc'
INSTALLER_SHA256 = '5f462548ef2c086140c91b207c6b67679f45b72f35f4432dee727107536381a4'
COOKIE = struct.Struct('!8sIIII64s')
ENTRY = struct.Struct('!IIIIBc')
MAGIC = b'MEI\014\013\012\013\016'
MODULES = ('tempo_gui', 'tempo_capture_ui', 'tempo_stimulation', 'tempo_firmware',
    'stimulation_investigation_1', 'stimulation_investigation_1.storage',
    'stimulation_investigation_1.capture', 'stimulation_investigation_1.firmware',
    'stimulation_investigation_1.ui', 'stimulation_investigation_1.verify')
RESOURCES = ('firmware/tempo_pacer/tempo_pacer.ino', 'firmware/tempo_pacer/investigation_1.h',
             'docs/STIMULATION_INVESTIGATION_1.md')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def read_archive(data):
    position = data.rfind(MAGIC)
    if position < 0 or position + COOKIE.size != len(data):
        raise ValueError('Expected an unsigned PyInstaller executable with an end-of-file archive.')
    magic, length, offset, toc_length, version, library = COOKIE.unpack_from(data, position)
    start = len(data) - length
    if start < 0 or offset + toc_length + COOKIE.size != length:
        raise ValueError('Invalid executable archive bounds.')
    entries = []
    cursor, end = start + offset, start + offset + toc_length
    while cursor < end:
        size, relative, compressed, original, flag, kind = ENTRY.unpack_from(data, cursor)
        if size < ENTRY.size + 1 or cursor + size > end or relative + compressed > offset:
            raise ValueError('Invalid executable archive entry.')
        name = data[cursor + ENTRY.size:cursor + size].rstrip(b'\0').decode('utf-8')
        payload = data[start + relative:start + relative + compressed]
        raw = zlib.decompress(payload) if flag else payload
        if len(raw) != original: raise ValueError('Executable entry length mismatch.')
        entries.append((name, kind, bool(flag), raw))
        cursor += size
    if cursor != end: raise ValueError('Executable table is incomplete.')
    return data[:start], version, library, entries


def write_archive(prefix, version, library, entries):
    payload = bytearray(); table = bytearray()
    for name, kind, compressed, raw in entries:
        encoded = name.encode('utf-8') + b'\0'
        size = ENTRY.size + len(encoded)
        size += (-size) % 16
        packed = zlib.compress(raw, 9) if compressed else raw
        table.extend(ENTRY.pack(size, len(payload), len(packed), len(raw), int(compressed), kind))
        table.extend(encoded.ljust(size - ENTRY.size, b'\0'))
        payload.extend(packed)
    cookie = COOKIE.pack(MAGIC, len(payload) + len(table) + COOKIE.size,
                         len(payload), len(table), version, library)
    return prefix + payload + table + cookie


def read_pyz(data):
    if data[:4] != b'PYZ\0' or data[4:8] != importlib.util.MAGIC_NUMBER:
        raise ValueError('Build with the same Python major/minor version as installed TEMPO.')
    offset = struct.unpack_from('!i', data, 8)[0]
    if not 12 <= offset < len(data): raise ValueError('Invalid Python archive.')
    table = marshal.loads(data[offset:])
    entries = {}
    for name, (kind, start, length) in dict(table).items():
        if not 0 <= start <= start + length <= offset: raise ValueError('Invalid module bounds.')
        entries[name] = (kind, data[start:start + length])
    return entries


def write_pyz(entries):
    data = bytearray(b'PYZ\0' + importlib.util.MAGIC_NUMBER + b'\0' * 9)
    table = []
    for name, (kind, packed) in entries.items():
        table.append((name, (kind, len(data), len(packed))))
        data.extend(packed)
    offset = len(data)
    data.extend(marshal.dumps(table))
    struct.pack_into('!i', data, 8, offset)
    return bytes(data)


def module_source(root, name):
    path = root.joinpath(*name.split('.'))
    path = path / '__init__.py' if path.is_dir() else path.with_suffix('.py')
    relative = path.relative_to(root).as_posix()
    return path, compile(path.read_bytes(), relative, 'exec', dont_inherit=True, optimize=0)


def extend_executable(original, root):
    prefix, version, library, entries = read_archive(original)
    if version != sys.version_info.major * 100 + sys.version_info.minor:
        raise ValueError('Python version differs from the v1.0.1 executable.')
    compiled = {name: module_source(root, name) for name in MODULES}
    pyz_count = main_count = 0
    rebuilt = []
    for name, kind, compressed, raw in entries:
        if kind == b'z':
            pyz_count += 1
            modules = read_pyz(raw)
            for module, (path, code) in compiled.items():
                if not module.startswith('stimulation_investigation_1') and module not in modules:
                    raise ValueError('A required v1.0.1 module is missing: ' + module)
                modules[module] = (1 if path.name == '__init__.py' else 0, zlib.compress(marshal.dumps(code), 6))
            raw = write_pyz(modules)
        elif kind == b's' and name in ('tempo_gui', 'tempo_gui.py'):
            main_count += 1; raw = marshal.dumps(compiled['tempo_gui'][1])
        rebuilt.append((name, kind, compressed, raw))
    if pyz_count != 1 or main_count != 1:
        raise ValueError('Unexpected v1.0.1 executable entry points.')
    return write_archive(prefix, version, library, rebuilt)


def verify_source_archive(source, source_archive):
    required = set(RESOURCES) | {'LICENSE'}
    required.update(module_source(source, name)[0].relative_to(source).as_posix() for name in MODULES)
    required.update('installer/stimulation_investigation_1/' + name
                    for name in ('Install.cmd', 'Remove.cmd', 'Manage-Package.ps1', 'README.txt'))
    with zipfile.ZipFile(source_archive) as archive:
        for name in sorted(required):
            try:
                matches = archive.read(name) == (source / name).read_bytes()
            except KeyError:
                matches = False
            if not matches:
                raise ValueError('The source archive differs from the package source: ' + name)


def build(installed, source, output, source_archive):
    installed, source, output = Path(installed).resolve(), Path(source).resolve(), Path(output).resolve()
    source_archive = Path(source_archive).resolve()
    if not source_archive.is_file() or not zipfile.is_zipfile(source_archive):
        raise ValueError('Provide the matching source ZIP for distribution with this package.')
    verify_source_archive(source, source_archive)
    identity = json.loads((installed / '_internal/assets/build-provenance.json').read_text(encoding='utf-8'))
    if identity.get('commit') != BASE_COMMIT:
        raise ValueError('Use the published stable v1.0.1 installation as the package base.')
    if sys.version_info[:2] != (3, 13):
        raise ValueError('The published Windows v1.0.1 runtime requires Python 3.13 for packaging.')
    original = installed / 'TEMPO.exe'
    files = {'TEMPO.exe': extend_executable(original.read_bytes(), source)}
    for relative in RESOURCES:
        files['_internal/' + relative] = (source / relative).read_bytes()
    manifest = dict(schema=1, package='stimulation-investigation-1', version=PACKAGE_VERSION,
        tempo_version='1.0.1', base_commit=BASE_COMMIT,
        source_archive=source_archive.name, source_sha256=digest(source_archive), files=[], runtime=[])
    for name, content in files.items():
        old = installed / name
        manifest['files'].append(dict(path=name, before_sha256=digest(old) if old.is_file() else None,
            after_sha256=hashlib.sha256(content).hexdigest(), size=len(content)))
    guards = [installed / '_internal/python313.dll', installed / '_internal/base_library.zip']
    qt = list((installed / '_internal/PyQt6').rglob('Qt6Core.dll'))
    if len(qt) != 1: raise ValueError('The installed Qt runtime could not be identified.')
    for path in guards + qt:
        manifest['runtime'].append(dict(path=path.relative_to(installed).as_posix(), sha256=digest(path)))
    output.mkdir(parents=True, exist_ok=True)
    filename = f'TEMPO_Stimulation_Investigation_1_v{PACKAGE_VERSION}.zip'
    destination = output / filename
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr('package.json', json.dumps(manifest, indent=2) + '\n')
        for name, content in files.items(): archive.writestr('payload/' + name, content)
        for name in ('Install.cmd', 'Remove.cmd', 'Manage-Package.ps1', 'README.txt'):
            archive.write(source / 'installer/stimulation_investigation_1' / name, name)
        archive.write(source / 'docs/STIMULATION_INVESTIGATION_1.md', 'GUIDE.md')
        archive.write(source / 'LICENSE', 'LICENSE')
        archive.writestr('SOURCE.txt', source_archive.name + '\nSHA-256: ' + manifest['source_sha256'] + '\n'
            'The matching source archive is distributed alongside this package in GitHub Packages.\n')
        # The replacement executable retains the base PYZ's third-party modules.
        # Distribute their existing notices and source references with it.
        for folder in ('third-party-licenses', 'third-party-sources'):
            origin = installed / '_internal' / folder
            if not origin.is_dir(): raise ValueError('The base dependency notices are missing: ' + folder)
            for path in sorted(origin.rglob('*')):
                if path.is_file(): archive.write(path, folder + '/' + path.relative_to(origin).as_posix())
    (output / 'SHA256SUMS.txt').write_text(digest(destination) + '  ' + filename + '\n', encoding='utf-8')
    (output / 'package.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', type=Path, required=True)
    parser.add_argument('--source', type=Path, default=Path('.'))
    parser.add_argument('--output', type=Path, default=Path('Output/investigation-package'))
    parser.add_argument('--source-archive', type=Path, required=True)
    args = parser.parse_args()
    print(build(args.installed, args.source, args.output, args.source_archive))
