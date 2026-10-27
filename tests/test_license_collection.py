"""Dependency notices are copied without leaking build-machine paths."""
import json
import hashlib
import shutil
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.collect_licenses import collect


class LicenseCollectionTests(unittest.TestCase):

    def test_notices_are_copied_and_inventory_has_no_machine_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root/'source'; source.mkdir()
            (source/'LICENSE').write_text('Synthetic public license notice')
            (source/'module.py').write_text('not a notice')
            distribution = SimpleNamespace(metadata={'Name':'Example-Package','License':'BSD-3-Clause'},
                version='1.0',files=['example.dist-info/LICENSE','example/module.py'],
                locate_file=lambda name:source/Path(name).name)
            output = root/'output'; report=collect(output,[distribution])
            self.assertEqual(report['packages_without_license_files'],[])
            copied=report['packages'][0]['files']
            self.assertEqual(len(copied),1)
            self.assertEqual((output/copied[0]).read_text(),'Synthetic public license notice')
            self.assertNotIn(str(root),json.dumps(report))

    def test_record_parent_components_cannot_escape_dependency_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source=root/'LICENSE';source.write_text('Synthetic notice')
            distribution=SimpleNamespace(metadata={'Name':'Example'},version='1',
                files=['../../LICENSE'],locate_file=lambda name:source)
            output=root/'output';report=collect(output,[distribution])
            target=(output/report['packages'][0]['files'][0]).resolve()
            self.assertTrue(target.is_relative_to((output/'example').resolve()))
            self.assertEqual(source.read_text(),'Synthetic notice')

    def test_missing_license_files_are_reported_for_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            distribution=SimpleNamespace(metadata={'Name':'Example'},version='1',files=[])
            report=collect(Path(temporary),[distribution])
            self.assertEqual(report['packages_without_license_files'],['Example'])

    def test_version_mismatch_cannot_reuse_an_upstream_notice(self):
        with tempfile.TemporaryDirectory() as temporary:
            distribution=SimpleNamespace(metadata={'Name':'flatbuffers'},version='999.0',files=[])
            report=collect(Path(temporary),[distribution])
            self.assertEqual(report['packages_without_license_files'],['flatbuffers'])
            self.assertNotIn('upstream_source',report['packages'][0])

    def test_missing_windows_runtime_notices_use_reviewed_exact_versions(self):
        repository=Path(__file__).resolve().parents[1]
        reviewed=json.loads((repository/'assets/licenses/sources.json').read_text())
        selected=[row for row in reviewed if row['package'] in ('antlr4-python3-runtime','cloudpickle','pyserial')]
        self.assertEqual(len(selected),3)
        distributions=[SimpleNamespace(metadata={'Name':row['package'],'License':'BSD-3-Clause'},
                                       version=row['version'],files=[]) for row in selected]
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary);report=collect(output,distributions)
            self.assertEqual(report['packages_without_license_files'],[])
            for package in report['packages']:
                expected=next(row for row in selected if row['package']==package['name'])
                self.assertEqual(package['upstream_source'],expected['source'])
                self.assertEqual(hashlib.sha256((output/package['files'][0]).read_bytes()).hexdigest(),expected['sha256'])

    def test_windows_text_checkout_preserves_upstream_notice_bytes(self):
        repository = Path(__file__).resolve().parents[1]
        manifest = json.loads((repository/'assets/licenses/sources.json').read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/'source'; root.mkdir()
            shutil.copyfile(repository/'.gitattributes',root/'.gitattributes')
            shutil.copytree(repository/'assets/licenses',root/'assets/licenses')
            subprocess.run(['git','init','--quiet',str(root)],check=True,capture_output=True)
            command = ['git','-C',str(root),'-c','core.autocrlf=true','-c','core.eol=crlf']
            subprocess.run(command+['add','.'],check=True,capture_output=True)
            checkout = Path(temporary)/'windows-checkout'
            subprocess.run(command+['checkout-index','--all','--prefix='+checkout.as_posix()+'/'],check=True,capture_output=True)
            for notice in manifest:
                with self.subTest(package=notice['package']):
                    raw = (checkout/notice['path']).read_bytes()
                    self.assertEqual(hashlib.sha256(raw).hexdigest(),notice['sha256'])

    def test_changed_upstream_notice_fails_checksum_verification(self):
        with tempfile.TemporaryDirectory() as temporary, patch('scripts.collect_licenses.hashlib.sha256') as digest:
            digest.return_value.hexdigest.return_value='incorrect'
            distribution=SimpleNamespace(metadata={'Name':'flatbuffers'},version='25.12.19',files=[])
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):
                collect(Path(temporary),[distribution])


if __name__ == '__main__':
    unittest.main()
