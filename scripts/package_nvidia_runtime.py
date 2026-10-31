"""Package the tested CUDA build as an optional, checksummed TEMPO worker."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import urllib.request
import zipfile

from tempo_nvidia_manifest import (JoinedFiles, MAX_ARCHIVE, MAX_DESCRIPTOR,
                                   MAX_PART, MAX_PARTS, validate_descriptor)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


class _PartWriter:
    """Write one ZIP directly to bounded release assets without a giant scratch ZIP."""
    def __init__(self, output, tag, part_size):
        self.output, self.tag, self.part_size = output, tag, part_size
        self.parts = []
        self.stream = None
        self.size = 0
        self.checksum = hashlib.sha256()

    def tell(self):
        return self.size

    def write(self, data):
        view = memoryview(data)
        length = len(view)
        if self.size + length > MAX_ARCHIVE:
            raise ValueError('The NVIDIA archive exceeds the setup download limit.')
        while view:
            if self.stream is None:
                if len(self.parts) >= MAX_PARTS:
                    raise ValueError('The NVIDIA archive needs too many release parts.')
                name = f'TEMPO_NVIDIA_Runtime_{self.tag}.zip.part{len(self.parts) + 1:03d}'
                self.stream = (self.output / name).open('xb')
                self.part_bytes = 0
                self.part_hash = hashlib.sha256()
                self.part_url = f'https://github.com/tempo-bio/TEMPO/releases/download/{self.tag}/{name}'
            block = view[:self.part_size - self.part_bytes]
            self.stream.write(block)
            self.part_hash.update(block)
            self.checksum.update(block)
            self.size += len(block)
            self.part_bytes += len(block)
            view = view[len(block):]
            if self.part_bytes == self.part_size:
                self.close()
        return length

    def flush(self):
        if self.stream is not None:
            self.stream.flush()

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.parts.append(dict(url=self.part_url, size=self.part_bytes, sha256=self.part_hash.hexdigest()))
            self.stream = None


def package(bundle, output, *, tag, provenance, part_size=MAX_PART):
    from tempo_release import VERSION
    bundle, output = Path(bundle), Path(output)
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?', tag):
        raise ValueError('Release tag must be a version such as v1.0.1.')
    if tag != 'v' + VERSION:
        raise ValueError('Release tag must match the application version.')
    if type(part_size) is not int or not 0 < part_size <= MAX_PART:
        raise ValueError('Release parts must be no larger than 1 GiB.')
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
    with tempfile.TemporaryDirectory(prefix='nvidia-package-', dir=output) as folder:
        staging = Path(folder)
        writer = _PartWriter(staging, tag, part_size)
        try:
            with zipfile.ZipFile(writer, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as zipped:
                for path in files:
                    name = 'TEMPO/' + path.relative_to(bundle).as_posix()
                    info = zipfile.ZipInfo(name)
                    info.compress_type = zipfile.ZIP_STORED
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    with path.open('rb') as source, zipped.open(info, 'w', force_zip64=True) as target:
                        for block in iter(lambda: source.read(1024 * 1024), b''):
                            target.write(block)
        finally:
            writer.close()
        descriptor = dict(schema_version=2, profile='nvidia-cu126', app_version=VERSION,
                          commit=identity['commit'], parts=writer.parts,
                          archive_sha256=writer.checksum.hexdigest(), archive_size=writer.size,
                          executable='TEMPO/TEMPO.exe', executable_sha256=digest(executable))
        manifest = staging/'nvidia-runtime.json'
        manifest.write_text(json.dumps(descriptor, indent=2)+'\n', encoding='utf-8')
        verify_descriptor(manifest, commit=identity['commit'])
        assets = sorted(staging.iterdir())
        if any((output/path.name).exists() for path in assets):
            raise ValueError('The output folder already contains NVIDIA release assets. Use a fresh output folder.')
        for path in assets:
            os.replace(path, output/path.name)
    return descriptor


def verify_descriptor(descriptor_path, *, commit=None):
    from tempo_release import VERSION
    manifest = Path(descriptor_path)
    if manifest.stat().st_size > MAX_DESCRIPTOR:
        raise ValueError('NVIDIA archive descriptor is too large.')
    descriptor = json.loads(manifest.read_text(encoding='utf-8'))
    validate_descriptor(descriptor, version=VERSION, commit=commit, allow_legacy=False)
    parts = [manifest.parent/part['url'].rsplit('/', 1)[-1] for part in descriptor['parts']]
    if set(manifest.parent.glob(f'TEMPO_NVIDIA_Runtime_v{VERSION}.zip*')) != set(parts):
        raise ValueError('NVIDIA release assets are missing or include undeclared files.')
    for path, part in zip(parts, descriptor['parts']):
        if path.stat().st_size != part['size'] or digest(path) != part['sha256']:
            raise ValueError('NVIDIA archive part does not match its release checksum.')
    with JoinedFiles(parts) as archive:
        checksum = hashlib.sha256()
        for block in iter(lambda: archive.read(1024 * 1024), b''):
            checksum.update(block)
        if checksum.hexdigest() != descriptor['archive_sha256']:
            raise ValueError('NVIDIA archive does not match its release checksum.')
        archive.seek(0)
        _verify_archive(archive, descriptor)
    return descriptor


def _verify_archive(archive, descriptor):
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        names = [entry.filename for entry in entries]
        if (len(names) > 30000 or len(names) != len({name.casefold() for name in names})
                or sum(entry.file_size for entry in entries) > 16 * 1024**3
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


def verify_published(descriptor_path, *, commit, open_url=None):
    """Check the public release's exact assets and source tag before promotion."""
    from tempo_release import VERSION
    manifest = Path(descriptor_path)
    if manifest.stat().st_size > MAX_DESCRIPTOR:
        raise ValueError('NVIDIA archive descriptor is too large.')
    descriptor = json.loads(manifest.read_text(encoding='utf-8'))
    validate_descriptor(descriptor, version=VERSION, commit=commit, allow_legacy=False)
    opener = open_url or urllib.request.urlopen
    def api(path):
        request = urllib.request.Request('https://api.github.com/repos/tempo-bio/TEMPO/'+path,
            headers={'Accept':'application/vnd.github+json', 'User-Agent':'TEMPO-release-check'})
        with opener(request, timeout=30) as response:
            data = response.read(4*1024**2+1)
            if len(data) > 4*1024**2:
                raise ValueError('GitHub release metadata is too large.')
            return json.loads(data)
    tag = 'v'+VERSION
    release = api('releases/tags/'+tag)
    if release.get('draft') is not False or release.get('tag_name') != tag:
        raise ValueError('The matching public release is not available.')
    assets = release.get('assets', [])
    if (not isinstance(assets, list) or any(type(asset.get('size')) is not int
            or not 0 < asset['size'] < 2*1024**3 for asset in assets)):
        raise ValueError('A release asset is empty or exceeds the GitHub file-size limit.')
    for part in descriptor['parts']:
        name = part['url'].rsplit('/', 1)[-1]
        matches = [asset for asset in assets if asset.get('name') == name]
        if (len(matches) != 1 or matches[0].get('state') != 'uploaded'
                or matches[0].get('size') != part['size']
                or matches[0].get('digest') != 'sha256:'+part['sha256']
                or matches[0].get('browser_download_url') != part['url']):
            raise ValueError('Published NVIDIA part is missing or does not match: '+name)
    source = api('commits/'+tag)
    if source.get('sha') != commit:
        raise ValueError('The release tag does not match the tested NVIDIA source revision.')
    return dict(success=True, tag=tag, commit=commit,
                verified_assets=[part['url'].rsplit('/', 1)[-1] for part in descriptor['parts']])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', default='dist/TEMPO')
    parser.add_argument('--output', default='Output')
    parser.add_argument('--provenance', default='Output/build-provenance.json')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--release-tag')
    mode.add_argument('--verify-descriptor', type=Path)
    mode.add_argument('--verify-published', type=Path)
    parser.add_argument('--commit')
    args = parser.parse_args()
    if args.verify_descriptor or args.verify_published:
        if not args.commit:
            parser.error('Verification requires --commit')
        result = (verify_published(args.verify_published, commit=args.commit) if args.verify_published else
                  verify_descriptor(args.verify_descriptor, commit=args.commit))
    else:
        if not args.release_tag:
            parser.error('--release-tag is required when creating the runtime archive')
        result = package(args.bundle, args.output, tag=args.release_tag,
                         provenance=args.provenance)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
