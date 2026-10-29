"""Package the tested CUDA build as an optional, checksummed TEMPO worker."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def package(bundle, output, *, tag, provenance):
    from tempo_release import VERSION
    bundle, output = Path(bundle), Path(output)
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?', tag):
        raise ValueError('Release tag must be a version such as v1.0.1.')
    if tag != 'v' + VERSION:
        raise ValueError('Release tag must match the application version.')
    identity = json.loads(Path(provenance).read_text(encoding='utf-8'))
    if identity.get('profile') != 'nvidia-cu126' or not re.fullmatch(r'[a-f0-9]{40}', identity.get('commit', '')):
        raise ValueError('CUDA build provenance is missing or has the wrong profile.')
    executable = bundle/'TEMPO.exe'
    if not executable.is_file():
        raise FileNotFoundError('The NVIDIA executable has not been built.')
    files = sorted(path for path in bundle.rglob('*') if path.is_file())
    if not files or any(path.is_symlink() for path in bundle.rglob('*')):
        raise ValueError('The runtime bundle is empty or contains a symbolic link.')
    native_names = {path.name.lower() for path in files}
    if not {'torch_cuda.dll', 'c10_cuda.dll'} <= native_names:
        raise ValueError('The bundled NVIDIA PyTorch libraries are incomplete.')
    output.mkdir(parents=True, exist_ok=True)
    archive = output/f'TEMPO_NVIDIA_Runtime_{tag}.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as zipped:
        for path in files:
            name = 'TEMPO/' + path.relative_to(bundle).as_posix()
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            with path.open('rb') as source, zipped.open(info, 'w', force_zip64=True) as target:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    target.write(block)
    if archive.stat().st_size > 8 * 1024**3:
        archive.unlink()
        raise ValueError('The NVIDIA archive exceeds the setup download limit.')
    descriptor = dict(schema_version=1, profile='nvidia-cu126', app_version=VERSION,
                      commit=identity['commit'],
                      archive_url=f'https://github.com/tempo-bio/TEMPO/releases/download/{tag}/{archive.name}',
                      archive_sha256=digest(archive), archive_size=archive.stat().st_size,
                      executable='TEMPO/TEMPO.exe', executable_sha256=digest(executable))
    (output/'nvidia-runtime.json').write_text(json.dumps(descriptor, indent=2)+'\n', encoding='utf-8')
    return descriptor


def verify_descriptor(descriptor_path, archive_path, *, commit=None):
    from tempo_release import VERSION
    descriptor = json.loads(Path(descriptor_path).read_text(encoding='utf-8'))
    archive = Path(archive_path)
    if (descriptor.get('schema_version') != 1 or descriptor.get('profile') != 'nvidia-cu126'
            or descriptor.get('app_version') != VERSION or descriptor.get('executable') != 'TEMPO/TEMPO.exe'
            or descriptor.get('commit') != commit
            or not re.fullmatch(r'[a-f0-9]{40}', descriptor.get('commit', ''))
            or not re.fullmatch(r'https://github\.com/tempo-bio/TEMPO/releases/download/'
                                r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?/'
                                r'TEMPO_NVIDIA_Runtime_v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?\.zip',
                                descriptor.get('archive_url', ''))
            or descriptor['archive_url'].rsplit('/', 1)[-1] != archive.name
            or archive.name != f'TEMPO_NVIDIA_Runtime_v{VERSION}.zip'
            or descriptor['archive_url'].split('/download/', 1)[-1].split('/', 1)[0]
               != archive.name.removeprefix('TEMPO_NVIDIA_Runtime_').removesuffix('.zip')
            or archive.stat().st_size > 8 * 1024**3
            or descriptor.get('archive_size') != archive.stat().st_size
            or descriptor.get('archive_sha256') != digest(archive)):
        raise ValueError('NVIDIA archive descriptor does not match this release.')
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        names = [entry.filename for entry in entries]
        if (len(names) != len(set(names)) or sum(entry.file_size for entry in entries) > 16 * 1024**3
                or any(name.startswith('/') or '\\' in name or '..' in PurePosixPath(name).parts
                       or not name.startswith('TEMPO/') or entry.is_dir()
                       or stat.S_IFMT(entry.external_attr >> 16) == stat.S_IFLNK
                       for name, entry in zip(names, entries))):
            raise ValueError('Invalid NVIDIA archive entries.')
        with zipped.open('TEMPO/TEMPO.exe') as stream:
            executable_hash = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                executable_hash.update(block)
        if executable_hash.hexdigest() != descriptor.get('executable_sha256'):
            raise ValueError('NVIDIA executable hash mismatch.')
    return descriptor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', default='dist/TEMPO')
    parser.add_argument('--output', default='Output')
    parser.add_argument('--provenance', default='Output/build-provenance.json')
    parser.add_argument('--release-tag')
    parser.add_argument('--verify-descriptor', type=Path)
    parser.add_argument('--verify-archive', type=Path)
    parser.add_argument('--commit')
    args = parser.parse_args()
    if args.verify_descriptor:
        if not args.verify_archive or not args.commit:
            parser.error('--verify-descriptor requires --verify-archive and --commit')
        result = verify_descriptor(args.verify_descriptor, args.verify_archive, commit=args.commit)
    else:
        if not args.release_tag:
            parser.error('--release-tag is required when creating the runtime archive')
        result = package(args.bundle, args.output, tag=args.release_tag,
                         provenance=args.provenance)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
