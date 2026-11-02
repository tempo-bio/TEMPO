"""Stage the optional add-on as an OCI artifact; upload only with --publish.

Requires a native ORAS client, not Docker. Credentials stay in the client's
registry configuration; this script never receives or prints access tokens.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

from scripts.package_stimulation_investigation import digest, PACKAGE_VERSION

REGISTRY = 'ghcr.io/tempo-bio/stimulation-investigation-1'


def stage(package, source_archive, oras='oras', publish=False):
    package, source_archive = Path(package).resolve(), Path(source_archive).resolve()
    root = package.parent
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read('package.json'))
        if (manifest.get('package') != 'stimulation-investigation-1' or
                manifest.get('version') != PACKAGE_VERSION):
            raise ValueError('Unexpected package identity or version.')
        if (manifest.get('source_sha256') != digest(source_archive) or
                manifest.get('source_archive') != source_archive.name):
            raise ValueError('The corresponding source archive does not match the package.')
        for item in manifest['files']:
            raw = archive.read('payload/' + item['path'])
            if len(raw) != item['size'] or hashlib.sha256(raw).hexdigest() != item['after_sha256']:
                raise ValueError('A packaged file differs from its manifest: ' + item['path'])
        readme = archive.read('README.txt')
    copied_source = root / source_archive.name
    if copied_source != source_archive:
        if copied_source.exists() and digest(copied_source) != digest(source_archive):
            raise ValueError('A different source archive already exists in the output folder.')
        if not copied_source.exists(): shutil.copyfile(source_archive, copied_source)
    (root / 'README.txt').write_bytes(readme)
    checksums = root / 'SHA256SUMS.txt'
    checksums.write_text(''.join(digest(path) + '  ' + path.name + '\n'
                                 for path in (package, copied_source)), encoding='utf-8')
    layout = root / 'registry'
    if layout.exists() and not (layout / 'oci-layout').is_file():
        raise ValueError('The registry output folder contains unrelated files.')
    tag = str(layout) + ':' + PACKAGE_VERSION
    subprocess.run([oras, 'push', '--oci-layout', tag,
        '--artifact-type', 'application/vnd.tempo.stimulation-investigation.package.v1',
        '--annotation', 'org.opencontainers.image.source=https://github.com/tempo-bio/TEMPO',
        '--annotation', 'org.opencontainers.image.licenses=AGPL-3.0-only',
        '--annotation', 'org.opencontainers.image.version=' + PACKAGE_VERSION,
        '--annotation', 'org.opencontainers.image.description=Experimental data collection add-on for existing TEMPO v1.0.1 Windows installations.',
        package.name + ':application/zip', copied_source.name + ':application/zip',
        'SHA256SUMS.txt:text/plain', 'README.txt:text/plain'], cwd=root, check=True)
    if publish:
        target = REGISTRY + ':' + PACKAGE_VERSION
        subprocess.run([oras, 'cp', '--from-oci-layout', tag, target], check=True)
        # Public availability is part of publication, not assumed from upload.
        # Use an empty auth file so a publisher's credentials cannot mask privacy.
        with tempfile.TemporaryDirectory(prefix='tempo-package-readback-') as temporary:
            temporary = Path(temporary)
            config = temporary / 'auth.json'; config.write_text('{"auths":{}}')
            download = temporary / 'download'; download.mkdir()
            subprocess.run([oras, 'pull', '--registry-config', str(config),
                            '--output', str(download), target], check=True)
            for path in (package, copied_source, checksums, root / 'README.txt'):
                if digest(download / path.name) != digest(path):
                    raise ValueError('Public package readback differs: ' + path.name)
        print('Published and verified public package: ' + target)
    else:
        print('Staged locally; no upload performed: ' + tag)
    return layout


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--source-archive', type=Path, required=True)
    parser.add_argument('--oras', default='oras')
    parser.add_argument('--publish', action='store_true')
    arguments = parser.parse_args()
    stage(arguments.package, arguments.source_archive, arguments.oras, arguments.publish)
