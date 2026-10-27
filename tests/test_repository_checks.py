"""Repository checks must reject hidden secrets without exposing their values."""
import base64
import hashlib
import io
import contextlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.check_repository import inspect_files, repository_files, secret_scan, install_scanner, main


class RepositoryCheckTests(unittest.TestCase):

    def setUp(self):
        self.secret = ('hf_'+'S'*36).encode()

    def test_generated_data_and_private_configuration_are_rejected_without_secret_matches(self):
        names = ['datasets/images/image.png', 'config/tempo_preferences.json',
                 '.env', 'Output/results.json', '.venv/package.py', 'pending_queue.json.lock']
        findings, _, _ = inspect_files([(name, b'{}') for name in names])
        self.assertEqual({r['path'] for r in findings}, set(names))
        self.assertTrue(all(r['rule'] == 'prohibited_file' for r in findings))

    def test_fixture_exception_does_not_allow_another_value_or_another_file(self):
        exception = dict(path='tests/fixture.py', rule='huggingface_token',
                         sha256=hashlib.sha256(self.secret).hexdigest(), reason='Synthetic test value')
        approved, _, _ = inspect_files([('tests/fixture.py', self.secret)], [exception])
        self.assertEqual(approved, [])
        for path, value in [('tests/other.py', self.secret),
                            ('tests/fixture.py', ('hf_'+'T'*36).encode())]:
            findings, _, _ = inspect_files([(path, value)], [exception])
            self.assertEqual(findings[0]['rule'], 'huggingface_token')
            self.assertNotIn(value.decode(), json.dumps(findings))

    def test_payload_accepts_only_reviewed_upstream_address_and_rejects_added_email(self):
        repository=Path(__file__).resolve().parents[1]
        relative='assets/licenses/pyserial/LICENSE'
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);notice=root/relative;notice.parent.mkdir(parents=True)
            notice.write_bytes((repository/relative).read_bytes())
            (root/'docs').mkdir();(root/'docs/TRAINING_WORKFLOW.md').write_text('Public guide')
            with patch('tempo_cloud.BUNDLE_FILES',[]), \
                 patch('scripts.prepare_dependency_sources.INDEX_FILES',[]), \
                 patch('sys.argv',['check_repository','--payload',str(root)]), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(),0)
                output.seek(0);output.truncate()
                notice.write_bytes(notice.read_bytes()+b'private'+b'@'+b'example.invalid')
                self.assertEqual(main(),1)
                report=json.loads(output.getvalue())
                self.assertEqual(report['findings'],[dict(path=relative,rule='email',line=notice.read_bytes().count(b'\n')+1)])

    def notebook(self, name='source.py', content=None, outputs=None, count=None):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr(name, content if content is not None else self.secret)
        encoded = base64.b64encode(stream.getvalue()).decode()
        return json.dumps(dict(cells=[dict(cell_type='code',
            source=['payload = base64.b64decode('+repr(encoded)+', validate=True)'],
            outputs=outputs or [], execution_count=count)])).encode()

    def test_embedded_notebook_sources_are_scanned(self):
        findings, _, count = inspect_files([('notebooks/trainer.ipynb', self.notebook())])
        self.assertEqual(count, 2)
        self.assertTrue(any(r['path'].endswith('!source.py') and r['rule']=='huggingface_token' for r in findings))
        self.assertNotIn(self.secret.decode(), json.dumps(findings))

    def test_saved_outputs_and_execution_counts_are_rejected(self):
        for options in (dict(outputs=[{'text':'private output'}]), dict(count=0)):
            findings, _, _ = inspect_files([('notebooks/trainer.ipynb', self.notebook(content=b'pass', **options))])
            self.assertTrue(any(r['rule']=='saved_notebook_output' for r in findings))

    def test_notebook_archive_paths_cannot_escape_inspection(self):
        findings, _, _ = inspect_files([('notebooks/trainer.ipynb', self.notebook(name='../outside.py'))])
        self.assertTrue(any(r['rule']=='invalid_notebook' for r in findings))

    def test_staged_check_reads_index_even_when_working_copy_was_cleaned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
            target = root/'source.py'; target.write_bytes(self.secret)
            subprocess.run(['git', 'add', 'source.py'], cwd=root, check=True, capture_output=True)
            target.write_text('clean working copy')
            staged, _, _ = inspect_files(repository_files(root, staged=True))
            working, _, _ = inspect_files(repository_files(root))
            self.assertTrue(any(r['rule']=='huggingface_token' for r in staged))
            self.assertEqual(working, [])

    def test_symlink_is_reported_instead_of_reading_its_target(self):
        findings, _, _ = inspect_files([('source.py', None)])
        self.assertEqual(findings, [dict(path='source.py', rule='symlink')])

    def test_staged_symlink_mode_is_rejected_without_reading_the_target(self):
        with patch('scripts.check_repository.subprocess.check_output', side_effect=[
                b'source.py\0', b'120000 synthetic-oid 0\tsource.py\0']) as read:
            files = list(repository_files(Path('/synthetic'), staged=True))
        self.assertEqual(files, [('source.py', None)])
        self.assertEqual(read.call_count, 2)

    def test_failed_scanner_cannot_be_reported_as_clean(self):
        with patch('scripts.check_repository.subprocess.run') as run:
            run.return_value.returncode = 2
            with self.assertRaisesRegex(RuntimeError, 'did not complete'):
                secret_scan(Path('/synthetic/scanner'), [('source.py', b'pass')], [])

    def test_scanner_download_checksum_failure_writes_no_executable(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch('scripts.check_repository.platform.system', return_value='Linux'), \
             patch('scripts.check_repository.platform.machine', return_value='x86_64'), \
             patch('scripts.check_repository.urlopen', return_value=io.BytesIO(b'wrong archive')):
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                install_scanner(Path(temporary))
            self.assertEqual(list(Path(temporary).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
