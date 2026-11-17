"""Create the separately distributed, deterministic Stimulation Investigation package."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from zipfile import ZipFile, ZipInfo

from tempo_optional_packages import INVESTIGATION, PACKAGES


ROOT = Path(__file__).resolve().parents[1]
RELEASE_VERSION = '0.2.0'


def build(output):
    source = ROOT / INVESTIGATION
    files = sorted((*source.glob('*.py'), *source.glob('*.md'), *source.glob('*.h')))
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f'TEMPO_Stimulation_Investigation_1_v{RELEASE_VERSION}.zip'
    with ZipFile(destination, 'w') as archive:
        for path in (*files, ROOT / 'LICENSE'):
            name = path.relative_to(ROOT).as_posix()
            content = path.read_bytes().replace(b'\r\n', b'\n')
            entry = ZipInfo(name)
            entry.create_system = 3
            archive.writestr(entry, content)
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--print-hash', action='store_true')
    parser.add_argument('--verify-executable', type=Path)
    args = parser.parse_args()
    archive = build(args.output)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    print(f'{digest}  {archive.name}')
    if not args.print_hash and PACKAGES[INVESTIGATION].archives.get(RELEASE_VERSION) != digest:
        archive.unlink()
        raise SystemExit('Package checksum changed. Review a new package version and record its approved hash.')
    if args.verify_executable:
        from scripts.windows_release_checks import isolated_environment, run_logged
        with tempfile.TemporaryDirectory(prefix='TEMPO optional package ') as directory:
            root = Path(directory)
            env = isolated_environment(root)
            env['TEMPO_VERIFY_OPTIONAL_PACKAGE'] = str(archive.resolve())
            report = (args.output / 'optional-package-verification.json').resolve()
            report.unlink(missing_ok=True)
            result = run_logged([str(args.verify_executable.resolve()), '--verify-probe',
                                 'stimulation investigation workflow', str(report)],
                                report.with_suffix('.log'), 120, env=env, cwd=root)
        if result or not report.is_file():
            raise SystemExit(result or 'The executable did not report optional-package verification.')
        evidence = json.loads(report.read_text(encoding='utf-8'))
        if evidence.get('check') != 'stimulation investigation workflow' or evidence.get('success') is not True:
            raise SystemExit('Optional-package verification failed.')


if __name__ == '__main__':
    main()
