"""Verify and patch a clean pinned Velopack checkout for an experimental build.

This does not establish recovery acceptance or authorize use in TEMPO releases.
"""
import argparse
import hashlib
from pathlib import Path
import subprocess

COMMIT = '92d6a1c91716729d449034df5c50307dcce39493'
PATCH_SHA256 = '88a3124f069c0f5db1be6ed67fa8d281df9e4d6575de6103a064e6e533894c47'
SOURCE_SHA256 = {
    'src/bins/src/commands/uninstall.rs': '9d26594bfd9320798f59d3c43544e4d0bef6f4e6da5b9830bf1b1c167fd865e8',
    'src/bins/src/windows/self_delete.rs': '21641cc6dfd87328685b3667da4f613a53ce31f438a72a5c01c9050ca7217d56',
    'src/bins/Cargo.toml': '4d4f0967fa7249de83a0424c4b4fd0590adf891840b58a1bb1d8560922805f68',
    'src/bins/src/commands/apply.rs': 'f12aa15322c2ff4b42f251a78bc234c193168df8d692afc27d0a66276712ab37',
    'src/bins/src/commands/start.rs': '5dc71710a5a070bc8ed2133e8c73ae1667f428291b9a8d854d8fba72960a8966',
    'src/bins/src/commands/apply_windows_impl.rs': 'd3db6cfa465053f24245d41ab9532a002091829b2d051eb9942bc64147450d4d',
    'src/bins/src/shared/util_common.rs': '4610125beb18c8766bc009afa2f92c6dc05914d14120d42b9de18fdf9499a5e5',
    'src/bins/src/shared/util_windows.rs': '6e7603aa4c059341903b673fef39ef51bfc7bde4138cb8b15605993e4bfd81ef',
    'src/bins/src/windows/util.rs': '2a049c423bae091f670c3748ce340af8f026f82b83bf785e7820cb40117c5ee9',
}
PATCH = Path(__file__).resolve().parents[1] / 'installer/velopack/0001-tempo-recovery.patch'


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True).stdout.strip()


def prepare(root, patch=PATCH):
    root = Path(root).resolve(strict=True)
    patch = Path(patch).resolve(strict=True)
    if git(root, 'rev-parse', 'HEAD') != COMMIT:
        raise ValueError('Velopack checkout is not the pinned commit')
    status = git(root, 'status', '--porcelain', '--untracked-files=all')
    if status:
        raise ValueError('Velopack checkout must be clean: ' + status[:4000])
    if sha256(patch) != PATCH_SHA256:
        raise ValueError('Recovery patch SHA-256 mismatch')
    for relative, expected in SOURCE_SHA256.items():
        path = root / relative
        if path.is_symlink() or sha256(path) != expected:
            raise ValueError(f'Pinned source SHA-256 mismatch: {relative}')
    git(root, 'apply', '--check', '--whitespace=error', str(patch))
    git(root, 'apply', '--whitespace=error', str(patch))
    changed = set(git(root, 'diff', '--name-only').splitlines())
    if changed != set(SOURCE_SHA256):
        raise ValueError(f'Unexpected patched files: {sorted(changed)}')
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkout', type=Path, help='clean pinned Velopack checkout; will be modified')
    args = parser.parse_args()
    prepare(args.checkout)
    print('Patched source prepared. Windows recovery acceptance remains UNVERIFIED.')


if __name__ == '__main__':
    main()
