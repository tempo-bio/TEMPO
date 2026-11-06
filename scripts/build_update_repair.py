"""Build the independent repair executable with embedded Ed25519 public keys.

The JSON input maps key IDs to base64-encoded raw public keys. It must never
contain private signing material. Build and certify in an isolated environment.
"""
import argparse
import base64
import json
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def read_keys(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not data or len(data) > 32:
        raise ValueError('Expected a nonempty public-key mapping.')
    keys = {}
    for name, encoded in data.items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', name):
            raise ValueError('Invalid key ID.')
        if not isinstance(encoded, str):
            raise ValueError('Public key must be base64 text.')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError('Invalid public key encoding.') from exc
        if len(raw) != 32 or base64.b64encode(raw).decode('ascii') != encoded:
            raise ValueError('Expected a canonical raw Ed25519 public key.')
        keys[name] = raw
    return keys


def build(keys_path, native_updater, dist, work):
    keys = read_keys(keys_path)
    native_updater = Path(native_updater)
    if native_updater.is_symlink() or not native_updater.is_file() or native_updater.stat().st_size == 0:
        raise ValueError('An existing native updater file is required.')
    def updater_digest():
        digest = hashlib.sha256()
        with native_updater.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        return digest.hexdigest()
    updater_hash = updater_digest()
    dist, work = Path(dist).resolve(), Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tempo-repair-build-', dir=work) as temp:
        wrapper = Path(temp) / 'repair_entry.py'
        wrapper.write_text('import tempo_update_recovery as recovery\n'
                           f'recovery.EMBEDDED_TRUSTED_KEYS = {keys!r}\n'
                           f'recovery.EMBEDDED_UPDATER_SHA256 = {updater_hash!r}\n'
                           'recovery.main()\n', encoding='utf-8')
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                        '--onefile', '--name', 'TEMPORepair', '--paths', str(ROOT),
                        '--distpath', str(dist), '--workpath', str(Path(temp) / 'build'),
                        '--specpath', temp, str(wrapper)], check=True)
        if updater_digest() != updater_hash:
            raise ValueError('Native updater changed while building the repair companion.')
    return dist / ('TEMPORepair.exe' if sys.platform == 'win32' else 'TEMPORepair')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-keys', required=True, type=Path)
    parser.add_argument('--native-updater', required=True, type=Path)
    parser.add_argument('--dist', required=True, type=Path)
    parser.add_argument('--work', required=True, type=Path)
    args = parser.parse_args()
    print(build(args.public_keys, args.native_updater, args.dist, args.work))


if __name__ == '__main__':
    main()
