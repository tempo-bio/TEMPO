"""Prepare a version-pinned dependency source index; never publish files.

Downloads are upstream source archives, not a copy of TEMPO or its Git history.
Archives stay outside tracking. No downloaded code is extracted or executed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'installer/dependency-sources.json'
INDEX_FILES = ('dependency-sources.json', 'DEPENDENCY_SOURCES.md', 'SHA256SUMS.txt')


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def validate(manifest, lock_path):
    if manifest.get('schema') != 1:
        raise ValueError('Unsupported dependency source manifest')
    if sha256_file(lock_path) != manifest.get('windows_lock_sha256'):
        raise ValueError('Windows dependencies changed; review and update dependency sources')
    versions = {}
    for line in Path(lock_path).read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        match = re.fullmatch(r'(\S+) @ (https://\S+) --hash=sha256:([a-f0-9]{64})', line)
        if not match:
            raise ValueError('Unsupported Windows lock entry')
        wheel = urlsplit(match[2]).path.rsplit('/', 1)[-1]
        source = next((source for source in manifest.get('sources',[]) if source['url']==match[2] and source['sha256']==match[3] and normalized(match[1]) in source.get('required_by',{})),None)
        if source:
            versions[normalized(match[1])] = source['required_by'][normalized(match[1])]
        elif wheel.endswith('.whl'):
            versions[normalized(match[1])] = wheel.split('-')[1].replace('%2B','+').replace('%2b','+')
        else:
            from packaging.utils import parse_sdist_filename
            package,version = parse_sdist_filename(wheel)
            if normalized(package)!=normalized(match[1]):
                raise ValueError('Source artifact names a different package')
            versions[normalized(match[1])] = str(version)
    ids, filenames, covered = set(), set(), set()
    if not manifest.get('sources'):
        raise ValueError('No dependency sources configured')
    for source in manifest['sources']:
        identity, filename = source['id'], source['filename']
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', identity) or identity in ids:
            raise ValueError('Invalid or duplicate source identifier')
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', filename)
                or filename in filenames):
            raise ValueError('Invalid or duplicate source filename')
        ids.add(identity)
        filenames.add(filename)
        url = urlsplit(source['url'])
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError('Sources require an HTTPS URL without credentials or query parameters')
        if not re.fullmatch(r'[a-f0-9]{64}', source['sha256']):
            raise ValueError('Invalid source checksum')
        if type(source['size']) is not int or source['size'] <= 0:
            raise ValueError('Invalid source size')
        if not source.get('version') or not source.get('license') or not source.get('basis'):
            raise ValueError('Source version, license and review basis are required')
        if not source.get('required_by'):
            raise ValueError('Source must identify its locked dependency')
        for name, version in source['required_by'].items():
            if versions.get(normalized(name)) != version:
                raise ValueError('Source does not match locked dependency: ' + name)
            covered.add(normalized(name))
    return versions, covered


def check_inventory(inventory, manifest):
    """Reject unreviewed copyleft packages in the actual build environment."""
    if inventory.get('packages_without_license_files'):
        raise ValueError('Missing dependency license notices: ' +
                         ', '.join(inventory['packages_without_license_files']))
    coverage = {normalized(name): version for source in manifest['sources']
                for name, version in source['required_by'].items()}
    for package in inventory['packages']:
        if normalized(package['name']) in coverage and coverage[normalized(package['name'])] != package['version']:
            raise ValueError('Unreviewed dependency version: '+package['name'])
        license_text = package.get('declared_license', '')
        if re.search(r'(?i)\b(?:[AL]?GPL|MPL)(?:[- v]|\b)|Mozilla Public|Lesser General|Affero', license_text):
            if coverage.get(normalized(package['name'])) != package['version']:
                raise ValueError('Unreviewed copyleft dependency: ' + package['name'])


class HTTPSRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if url.scheme != 'https' or url.username or url.password:
            raise ValueError('Unsafe source download redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def retain_source(source, cache, fetch=False, opener=None):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / source['filename']
    if target.is_symlink():
        raise ValueError('Source cache entry is a symbolic link')
    if not target.exists():
        if not fetch:
            raise ValueError('Missing source archive: ' + source['filename'])
        opener = opener or build_opener(HTTPSRedirects()).open
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache, prefix='.download-', delete=False) as stream:
                temporary = Path(stream.name)
                count = 0
                with opener(source['url'], timeout=60) as response:
                    for block in iter(lambda: response.read(1024 * 1024), b''):
                        count += len(block)
                        if count > source['size']:
                            raise ValueError('Source exceeds reviewed size: ' + source['id'])
                        stream.write(block)
            if temporary.stat().st_size != source['size'] or sha256_file(temporary) != source['sha256']:
                raise ValueError('Source download checksum mismatch: ' + source['id'])
            temporary.replace(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    if target.stat().st_size != source['size'] or sha256_file(target) != source['sha256']:
        raise ValueError('Cached source checksum mismatch: ' + source['id'])
    return dict(id=source['id'], filename=source['filename'], sha256=source['sha256'], size=source['size'])


def write_index(manifest, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    # Preserve the reviewed bytes on Windows as well as Unix checkouts.
    (output / 'dependency-sources.json').write_bytes((json.dumps(manifest, indent=2) + '\n').encode('utf-8'))
    lines = ['# Dependency sources', '',
             'These upstream sources match the dependency versions recorded in this index. '
             'The matching TEMPO source revision and build instructions accompany each release.', '',
             '| Component | Version | Source |', '| --- | --- | --- |']
    for source in manifest['sources']:
        lines.append('| {} | {} | [{}]({}) |'.format(source['id'], source['version'],
                                                    source['filename'], source['url']))
    lines += ['', 'SHA-256 checksums are recorded in `dependency-sources.json` and `SHA256SUMS.txt`.',
              'Third-party licenses remain applicable. See `docs/THIRD_PARTY_SOFTWARE.md` in TEMPO source.', '']
    (output / 'DEPENDENCY_SOURCES.md').write_bytes('\n'.join(lines).encode('utf-8'))
    (output / 'SHA256SUMS.txt').write_bytes(''.join('{}  {}\n'.format(s['sha256'], s['filename'])
                                                 for s in manifest['sources']).encode('utf-8'))
    return [output / name for name in INDEX_FILES]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=MANIFEST)
    parser.add_argument('--lock', type=Path, default=ROOT / 'requirements-windows.lock')
    parser.add_argument('--inventory', type=Path, help='License index from the actual Windows build')
    parser.add_argument('--output', type=Path, help='Write the release source index here')
    parser.add_argument('--cache', type=Path, help='Local upstream source archive directory')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--fetch', action='store_true', help='Download and verify missing upstream archives')
    mode.add_argument('--verify-cache', action='store_true', help='Verify retained archives without network access')
    args = parser.parse_args()
    if (args.fetch or args.verify_cache) and args.cache is None:
        parser.error('--fetch and --verify-cache require --cache')
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    versions, covered = validate(manifest, args.lock)
    if args.inventory:
        check_inventory(json.loads(args.inventory.read_text(encoding='utf-8')), manifest)
    # Verify everything before producing an index that could accompany a release.
    verified = []
    if args.fetch or args.verify_cache:
        for source in manifest['sources']:
            verified.append(retain_source(source, args.cache, fetch=args.fetch))
    if args.output:
        write_index(manifest, args.output)
        report = dict(manifest_sha256=sha256_file(args.manifest),
                      windows_lock_sha256=manifest['windows_lock_sha256'],
                      archives_verified=bool(verified), archives=verified)
        (args.output / 'source-verification.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(sources=len(manifest['sources']), locked_dependencies=len(versions),
                         source_dependencies=len(covered), archives_verified=len(verified))))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
