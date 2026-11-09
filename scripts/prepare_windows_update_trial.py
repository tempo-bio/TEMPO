"""Prepare one isolated TEMPO source revision for the private Windows update trial."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = '4dc336fc8b655ed347a6a1e37a0ae944bf3502e3'
VERSIONS = ('1.0.2-trial.1', '1.0.2-trial.2', '1.0.2-trial.3')
OLDER_TQDM = '4.67.1'
OLDER_WHEEL = ('https://files.pythonhosted.org/packages/d0/30/'
               'dc54f88dd4a2b5dc8a0279bdd7270e735851848b762aeb1c1184ed1f6b14/tqdm-4.67.1-py3-none-any.whl')
OLDER_WHEEL_SHA256 = '26445eca388f82e72884e0d580d5464cd801a3ea01e63e5601bdff9ba6a48de2'
OLDER_SOURCE = ('https://files.pythonhosted.org/packages/a8/4b/'
                '29b4ef32e036bb34e4ab51796dd745cdba7ed47ad142a9f4a1eb8e0c744d/tqdm-4.67.1.tar.gz')
OLDER_SOURCE_SHA256 = 'f8aef9c52c08c13a65f30ea34f4e5aac3fd1a34959879d7e59e63027286627f2'


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f'The reviewed trial source has changed near {old.splitlines()[0]}.')
    return text.replace(old, new, 1)


def without_block(text, start, end):
    before = text.index(start)
    after = text.index(end, before)
    return text[:before] + text[after:]


def write_text(path, value):
    path.write_bytes(value.encode('utf-8'))


def use_older_dependency(source):
    lock = source / 'requirements-windows.lock'
    text, count = re.subn(r'^tqdm @ [^\n]+$', f'tqdm @ {OLDER_WHEEL} --hash=sha256:{OLDER_WHEEL_SHA256}',
                          lock.read_text(encoding='utf-8'), flags=re.M)
    if count != 1:
        raise ValueError('Expected one reviewed tqdm pin.')
    write_text(lock, text)
    manifest = source / 'installer/dependency-sources.json'
    values = json.loads(manifest.read_bytes())
    entry, = [item for item in values['sources'] if item['id'] == 'tqdm']
    entry.update(version=OLDER_TQDM, required_by={'tqdm': OLDER_TQDM}, url=OLDER_SOURCE,
                 filename='tqdm-4.67.1.tar.gz', sha256=OLDER_SOURCE_SHA256, size=169737)
    values['windows_lock_sha256'] = hashlib.sha256(lock.read_bytes()).hexdigest()
    write_text(manifest, json.dumps(values, indent=2) + '\n')


def prepare(ordinal, output):
    """Create a fixture repository outside the working tree with one actual commit."""
    version = VERSIONS[ordinal - 1]
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Use a new directory for the isolated trial source.')
    archive = subprocess.check_output(['git', 'archive', '--format=tar', SOURCE_COMMIT], cwd=ROOT)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.tempo-trial-source-', dir=output.parent) as folder:
        source = Path(folder) / 'source'
        source.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(source, filter='data')
        release = source / 'tempo_release.py'
        write_text(release, replace_once(release.read_text(encoding='utf-8'),
                                         "VERSION = '1.0.2'\n", f"VERSION = '{version}'\n"))
        desktop = source / 'tempo_gui.py'
        entry = "if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] in ('--assist-model', '--assist-model-cuda'):"
        probe = (
            "if __name__ == '__main__' and len(sys.argv) == 1 and os.environ.get('TEMPO_SIGNED_TRIAL_REPORT'):\n"
            "    from tempo_release import verify_installation\n"
            "    sys.exit(verify_installation(os.environ['TEMPO_SIGNED_TRIAL_REPORT']))\n\n"
        )
        write_text(desktop, replace_once(desktop.read_text(encoding='utf-8'), entry, probe + entry))
        spec = source / 'tempo.spec'
        text = spec.read_text(encoding='utf-8')
        text = without_block(text, "    if profile.key == 'cpu':\n        optional_runtime =",
                             'identity = root /')
        text = without_block(text, "if sys.platform == 'win32' and profile.key == 'cpu':\n    validate_descriptor(",
                             'for runtime in')
        write_text(spec, replace_once(text, 'from tempo_nvidia_manifest import validate_descriptor\n', ''))
        if ordinal == 1:
            use_older_dependency(source)
        subprocess.run(['git', 'init', '-q'], cwd=source, check=True)
        git = ['git', '-c', 'core.autocrlf=false', '-c', 'user.name=TEMPO update test',
               '-c', 'user.email=tempo-update-fixture@example.invalid', '-c', 'commit.gpgsign=false']
        subprocess.run([*git, 'add', '--all'], cwd=source, check=True)
        subprocess.run([*git, 'commit', '-qm', f'Isolated update fixture {version}'], cwd=source, check=True)
        identity = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
        source.rename(output)
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ordinal', type=int, choices=(1, 2, 3), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.ordinal, args.output))


if __name__ == '__main__':
    main()
