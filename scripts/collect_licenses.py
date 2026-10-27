"""Collect installed dependency license files without copying account metadata."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import shutil


def license_file(filename):
    path = Path(filename)
    basename = path.name.upper()
    if path.suffix.lower() in ('.py', '.pyc', '.so', '.dll'):
        return False
    return (basename.startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE', 'THIRD_PARTY_NOTICE'))
            or any(part.lower() == 'licenses' for part in path.parts) and path.suffix.lower() in ('.txt', '.md'))


def collect(destination, distributions=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    root = Path(__file__).resolve().parents[1]
    manifest = root/'assets/licenses/sources.json'
    fallbacks = json.loads(manifest.read_text()) if manifest.exists() else []
    for distribution in distributions if distributions is not None else importlib.metadata.distributions():
        name = distribution.metadata['Name']
        folder = re.sub(r'[^a-z0-9._-]', '-', name.lower())
        record = dict(name=name, version=distribution.version, files=[])
        expression = distribution.metadata.get('License-Expression') or distribution.metadata.get('License', '')
        record['declared_license'] = expression if len(expression) < 160 else 'See license files'
        for filename in distribution.files or []:
            if not license_file(str(filename)):
                continue
            source = Path(distribution.locate_file(filename))
            if source.is_symlink() or not source.is_file():
                continue
            relative = Path(str(filename).replace('\\', '/'))
            # Wheel RECORD paths may include parent components. Flatten those
            # instead of allowing a notice to leave its dependency directory.
            parts = [part for part in relative.parts if part not in ('..', '.', '/') and ':' not in part]
            target = destination/folder/Path(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            record['files'].append(target.relative_to(destination).as_posix())
        if not record['files']:
            for fallback in fallbacks:
                if fallback['package'].lower() != name.lower() or fallback['version'] != distribution.version:
                    continue
                source = root/fallback['path']
                if hashlib.sha256(source.read_bytes()).hexdigest() != fallback['sha256']:
                    raise ValueError('Upstream license checksum mismatch: '+name)
                target = destination/folder/'upstream-LICENSE'
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                record['files'].append(target.relative_to(destination).as_posix())
                record['upstream_source'] = fallback['source']
        records.append(record)
    records.sort(key=lambda item:item['name'].lower())
    report = dict(description='License files from the installed build environment; includes build-time dependencies.',
                  packages=records,
                  packages_without_license_files=[r['name'] for r in records if not r['files']])
    (destination/'index.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = collect(args.output)
    print(json.dumps(dict(packages=len(report['packages']),
                         without_files=report['packages_without_license_files']),indent=2))
