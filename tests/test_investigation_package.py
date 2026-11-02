"""Archive integrity checks; Windows execution needs separate native validation."""
import importlib.util
import marshal
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
import zlib

from scripts.package_stimulation_investigation import (
    MODULES, RESOURCES, read_archive, write_archive, read_pyz, write_pyz,
    extend_executable, verify_source_archive)


class InvestigationPackageTests(unittest.TestCase):
    def fixture(self, root):
        modules = {}
        for name in MODULES:
            path = root.joinpath(*name.split('.'))
            if name == 'stimulation_investigation_1': path = path / '__init__.py'
            else: path = path.with_suffix('.py')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('new_value = ' + repr(name) + '\n')
            if not name.startswith('stimulation_investigation_1'):
                modules[name] = (0, zlib.compress(marshal.dumps(compile('old_value = True', name+'.py', 'exec'))))
        modules['untouched'] = (0, zlib.compress(marshal.dumps(compile('constant = 19', 'untouched.py', 'exec'))))
        modules['namespace'] = (3, b'')
        entries = [('boot', b'm', True, b'unchanged boot payload'),
                   ('pyi-contents-directory _internal', b'o', False, b''),
                   ('tempo_gui', b's', True, marshal.dumps(compile('old_main = True', 'tempo_gui.py', 'exec'))),
                   ('PYZ.pyz', b'z', False, write_pyz(modules))]
        version = sys.version_info.major * 100 + sys.version_info.minor
        return write_archive(b'MZ synthetic bootloader', version, b'python.dll', entries), modules

    def test_only_selected_modules_change_and_runtime_bootloader_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, modules = self.fixture(root)
            updated = extend_executable(original, root)
            before = read_archive(original); after = read_archive(updated)
            self.assertEqual(before[:3], after[:3])
            self.assertEqual(before[3][:2], after[3][:2])
            entries = {name: (kind, compressed, content) for name, kind, compressed, content in after[3]}
            packed = read_pyz(entries['PYZ.pyz'][2])
            self.assertEqual(packed['untouched'], modules['untouched'])
            self.assertEqual(packed['namespace'], modules['namespace'])
            for name in MODULES:
                namespace = {}; exec(marshal.loads(zlib.decompress(packed[name][1])), namespace)
                self.assertEqual(namespace['new_value'], name)
            namespace = {}; exec(marshal.loads(entries['tempo_gui'][2]), namespace)
            self.assertEqual(namespace['new_value'], 'tempo_gui')

    def test_foreign_bytecode_and_signed_or_truncated_executables_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            original, _ = self.fixture(Path(directory))
            for damaged in (original[:-10], original+b'certificate'):
                with self.assertRaises(ValueError): read_archive(damaged)
            pyz = bytearray(write_pyz({})); pyz[4:8] = b'xxxx'
            with self.assertRaises(ValueError): read_pyz(pyz)

    def test_mismatched_or_incomplete_corresponding_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            names = list(RESOURCES) + ['LICENSE'] + [
                'installer/stimulation_investigation_1/' + name for name in
                ('Install.cmd', 'Remove.cmd', 'Manage-Package.ps1', 'README.txt')]
            for name in names:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('source content')
            archive_path = root / 'source.zip'
            files = [path for path in root.rglob('*') if path.is_file()]
            with zipfile.ZipFile(archive_path, 'w') as archive:
                for path in files: archive.write(path, path.relative_to(root).as_posix())
            verify_source_archive(root, archive_path)
            (root / 'tempo_gui.py').write_text('different = True')
            with self.assertRaisesRegex(ValueError, 'tempo_gui.py'):
                verify_source_archive(root, archive_path)
            (root / 'tempo_gui.py').write_text('new_value = '+repr('tempo_gui')+'\n')
            with zipfile.ZipFile(archive_path, 'w') as archive: archive.writestr('LICENSE', 'source content')
            with self.assertRaises(ValueError): verify_source_archive(root, archive_path)


if __name__ == '__main__': unittest.main()
