"""Check publishable files, notebooks and secrets without reading ignored data.

Use --staged for the index, --scanner PATH for the pinned Gitleaks binary, or
--payload PATH to inspect application-owned files inside a packaged payload.
Reports contain locations and rule names, never matching credential values.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCANNER_VERSION = '8.30.1'
SCANNERS = {
    ('Darwin', 'arm64'): ('darwin_arm64.tar.gz', 'b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5'),
    ('Linux', 'x86_64'): ('linux_x64.tar.gz', '551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb'),
    ('Windows', 'AMD64'): ('windows_x64.zip', 'd29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e'),
}
RULES = {
    'huggingface_token': re.compile(rb'\bhf_(?:oauth_)?[A-Za-z0-9]{20,}\b'),
    'github_token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'aws_access_key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'private_key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'credential_url': re.compile(rb'https?://[^\s/<>:@]+:[^\s/<>@]+@[^\s/<>]+'),
    'personal_path': re.compile(rb'(?:/Users/|/home/|[A-Za-z]:[\\/]Users[\\/])[A-Za-z0-9._-]+'),
    'email': re.compile(rb'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
    'colab_drive_link': re.compile(rb'https://colab\.research\.google\.com/drive/[A-Za-z0-9_-]+'),
}
FORBIDDEN_DIRS = {'.git', '.venv', '.venv_x86', '.venv_build', '.runtime', '.agent-artifacts',
                  '__pycache__', '.pytest_cache', 'output', 'build', 'dist', 'assays',
                  'datasets', 'runs', 'tempo_models', 'annotation_recovery'}


def forbidden_path(name):
    path = PurePosixPath(name)
    return (any(part.lower() in FORBIDDEN_DIRS for part in path.parts)
            or path.name.startswith('pending_queue.json')
            or name == 'config/tempo_preferences.json'
            or path.name.startswith('.env') and path.name != '.env.example'
            or path.suffix.lower() in ('.pt', '.pyc', '.pem', '.key'))


def repository_files(root, staged=False):
    command = ['git', 'ls-files', '-z', '--cached']
    if not staged:
        command += ['--others', '--exclude-standard']
    names = sorted(set(filter(None, subprocess.check_output(command, cwd=root).decode().split('\0'))))
    symlinks = set()
    if staged:
        entries = subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=root).decode().split('\0')
        symlinks = {entry.partition('\t')[2] for entry in entries if entry.startswith('120000 ')}
    for name in names:
        path = root/name
        if forbidden_path(name):
            yield name, b''
        elif name in symlinks or not staged and path.is_symlink():
            yield name, None
        elif staged:
            yield name, subprocess.check_output(['git', 'show', ':'+name], cwd=root)
        elif path.exists():
            yield name, path.read_bytes()


def inspect_files(files, exceptions=()):
    allowed = {(r['path'], r['rule'], r['sha256']) for r in exceptions if r.get('reason')}
    findings = []
    approved_values = []
    visited = 0
    def inspect(name, data, nested=False, depth=0):
        nonlocal visited
        visited += 1
        if data is None or not nested and forbidden_path(name):
            findings.append(dict(path=name, rule='symlink' if data is None else 'prohibited_file'))
            return
        if len(data) > 8*1024*1024 or depth > 3:
            findings.append(dict(path=name, rule='inspection_limit'))
            return
        for rule, pattern in RULES.items():
            for match in pattern.finditer(data):
                digest = hashlib.sha256(match.group()).hexdigest()
                if (name, rule, digest) in allowed:
                    approved_values.append((name, match.group().decode('ascii')))
                else:
                    findings.append(dict(path=name, rule=rule, line=data.count(b'\n', 0, match.start())+1))
        if name.endswith('.ipynb'):
            try:
                notebook = json.loads(data)
                for cell in notebook['cells']:
                    if cell.get('outputs') or cell.get('execution_count') is not None:
                        findings.append(dict(path=name, rule='saved_notebook_output'))
                    source = ''.join(cell.get('source', []))
                    # The maintained trainer embeds its source ZIP in a literal.
                    for encoded in re.findall(r"base64\.b64decode\(['\"]([A-Za-z0-9+/=]{100,})['\"]", source):
                        payload = base64.b64decode(encoded, validate=True)
                        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                            if sum(entry.file_size for entry in archive.infolist()) > 16*1024*1024:
                                raise ValueError('Notebook archive exceeds inspection limit')
                            for member in archive.infolist():
                                entry = member.filename
                                if ('\\' in entry or ':' in entry or PurePosixPath(entry).is_absolute()
                                        or '..' in PurePosixPath(entry).parts):
                                    raise ValueError('Unsafe notebook archive entry')
                                inspect(name+'!'+entry, archive.read(member), nested=True, depth=depth+1)
            except (ValueError, KeyError, TypeError, zipfile.BadZipFile):
                findings.append(dict(path=name, rule='invalid_notebook'))
    for name, data in files:
        inspect(name, data)
    return findings, approved_values, visited


def install_scanner(destination):
    suffix, expected = SCANNERS[(platform.system(), platform.machine())]
    url = f'https://github.com/gitleaks/gitleaks/releases/download/v{SCANNER_VERSION}/gitleaks_{SCANNER_VERSION}_{suffix}'
    with urlopen(url, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('Secret scanner checksum mismatch')
    name = 'gitleaks.exe' if platform.system() == 'Windows' else 'gitleaks'
    if suffix.endswith('.zip'):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            binary = archive.read(name)
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            binary = archive.extractfile(name).read()
    destination.mkdir(parents=True, exist_ok=True)
    executable = destination/name
    executable.write_bytes(binary); executable.chmod(0o700)
    return executable


def secret_scan(scanner, files, approved):
    with tempfile.TemporaryDirectory(prefix='TEMPO repository check ') as temporary:
        root = Path(temporary); snapshot = root/'source'; snapshot.mkdir()
        for name, data in files:
            if data is not None:
                target = snapshot/name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
        config = '[extend]\nuseDefault = true\n'
        # Exact reviewed values, including synthetic fixtures and upstream notices,
        # scoped to the individual file.
        for name, value in sorted(set(approved)):
            pattern = '(?:^|/)'+re.escape(name)+'$'
            config += '\n[[allowlists]]\ndescription = "Reviewed exact value"\ncondition = "AND"\nregexTarget = "secret"\n'
            config += "paths = ['''"+pattern+"''']\nregexes = ['''^"+re.escape(value)+"$''']\n"
        config_path = root/'gitleaks.toml'; config_path.write_text(config)
        report = root/'findings.json'
        result = subprocess.run([str(scanner), 'dir', '.', '--config', str(config_path), '--redact=100',
                                 '--no-banner', '--no-color', '--max-archive-depth', '3', '--max-decode-depth', '5',
                                 '--report-format', 'json', '--report-path', str(report), '--timeout', '180'],
                                cwd=snapshot, capture_output=True, text=True, timeout=200)
        if result.returncode not in (0, 1) or not report.exists():
            raise RuntimeError('Secret scanner did not complete; no clean result was established.')
        records = json.loads(report.read_text())
        return [dict(path=r['File'], line=r['StartLine'], rule='gitleaks:'+r['RuleID']) for r in records]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged', action='store_true')
    parser.add_argument('--scanner', type=Path)
    parser.add_argument('--install-scanner', type=Path)
    parser.add_argument('--payload', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.install_scanner:
        print(install_scanner(args.install_scanner).resolve()); return 0
    exceptions = json.loads((ROOT/'scripts/repository_exceptions.json').read_text())
    if args.payload:
        # Explicit owned source/assets only; do not mistake runtime CA bundles
        # or third-party test fixtures for developer-supplied project data.
        payload = args.payload.resolve()
        module = payload/'_internal' if (payload/'_internal').is_dir() else payload
        from tempo_cloud import BUNDLE_FILES
        from scripts.prepare_dependency_sources import INDEX_FILES
        names = list(BUNDLE_FILES)+['docs/TRAINING_WORKFLOW.md']
        names += ['third-party-sources/'+name for name in INDEX_FILES]
        names += [path.relative_to(module).as_posix() for path in (module/'assets').rglob('*') if path.is_file()]
        files = [(name, (module/name).read_bytes()) for name in names]
        findings, _, count = inspect_files(files, exceptions)
        for path in module.rglob('*'):
            relative = path.relative_to(module).as_posix()
            if path.is_file() and (path.relative_to(module).parts[0].lower() in FORBIDDEN_DIRS
                                   or relative == 'config/tempo_preferences.json' or path.name.startswith('pending_queue.json')):
                findings.append(dict(path=relative, rule='prohibited_payload_file'))
        mode = 'owned_payload'
    else:
        files = list(repository_files(ROOT, args.staged))
        findings, approved, count = inspect_files(files, exceptions)
        if args.scanner:
            findings.extend(secret_scan(args.scanner.resolve(), files, approved))
        mode = 'index' if args.staged else 'worktree'
    result = dict(success=not findings, mode=mode, inspected_files=count,
                  secret_scanner=SCANNER_VERSION if args.scanner and not args.payload else None, findings=findings)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
