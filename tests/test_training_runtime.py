"""Compatibility selection, source integrity and completed-candidate identity."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import tempo_training_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def test_registered_repairs_match_immutable_legacy_source_fixtures(self):
        fixtures = Path(__file__).parent/'fixtures/legacy_training'
        sources = {name:(fixtures/(name.replace('/','__')+'.txt')).read_bytes() for name in runtime.REPAIRS}
        before = dict(sources)
        for name,(expected,_) in runtime.REPAIRS.items():
            self.assertEqual(hashlib.sha256(sources[name]).hexdigest(),expected)
        support = {'tempo_training_runtime.py':Path(runtime.__file__).read_bytes(),
                   'colab_runtime.py':(Path(__file__).parents[1]/'notebooks/colab_runtime.py').read_bytes()}
        files, manifest = runtime.compatibility_files(sources,support)
        self.assertEqual(set(manifest['overrides']),{name[:-3].replace('/','.') for name in sources})
        self.assertEqual(sources,before)
        for name,data in files.items():
            if name.endswith('.py'):compile(data,name,'exec')

    def test_windows_checkout_preserves_registered_legacy_source_bytes(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'source'; root.mkdir()
            shutil.copyfile(repository/'.gitattributes', root/'.gitattributes')
            shutil.copytree(repository/'tests/fixtures/legacy_training', root/'tests/fixtures/legacy_training')
            subprocess.run(['git', 'init', '--quiet', str(root)], check=True, capture_output=True)
            command = ['git', '-C', str(root), '-c', 'core.autocrlf=true', '-c', 'core.eol=crlf']
            subprocess.run(command+['add', '.'], check=True, capture_output=True)
            checkout = Path(directory)/'windows-checkout'
            subprocess.run(command+['checkout-index', '--all', '--prefix='+checkout.as_posix()+'/'],
                           check=True, capture_output=True)
            for name, (expected, _) in runtime.REPAIRS.items():
                with self.subTest(source=name):
                    raw = (checkout/'tests/fixtures/legacy_training'/(name.replace('/', '__')+'.txt')).read_bytes()
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), expected)

    def test_a_new_run_removes_prior_tempo_import_hooks_and_preserves_other_paths(self):
        environment={'PYTHONPATH':os.pathsep.join(['/content/TEMPO-cloud/old/hash/runtime-support',
            '/content/TEMPO-cloud/older/hash/scout-compatibility','/other/project']),
            'TEMPO_EXECUTION_RUNTIME':'old','HF_TOKEN':'fixture'}
        runtime.clear_runtime_environment(environment)
        self.assertEqual(environment,{'PYTHONPATH':'/other/project','HF_TOKEN':'fixture'})

    def test_unknown_sources_receive_no_automatic_repair(self):
        files, manifest = runtime.compatibility_files({'train_yolo.py':b'unknown code'}, {'colab_runtime.py':b'# runtime'})
        self.assertEqual(manifest['overrides'], {})
        self.assertIn('sitecustomize.py', files)

    def test_identified_source_is_repaired_without_changing_its_file_and_tampering_blocks_imports(self):
        source = b'import json\nVALUE = 42\n'
        digest = hashlib.sha256(source).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); support = root/'runtime-support'; support.mkdir()
            original = root/'tempo_evaluation_metrics.py'; original.write_bytes(source)
            with patch.dict(runtime.REPAIRS, {'tempo_evaluation_metrics.py':(digest,'clock-test')},clear=True):
                files, manifest = runtime.compatibility_files({'tempo_evaluation_metrics.py':source},{'colab_runtime.py':b'# runtime'})
            for name,data in files.items():
                path = support/name; path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
            env = dict(os.environ,PYTHONPATH=str(support))
            code = "import tempo_evaluation_metrics as m; from pathlib import Path; assert m.VALUE==42 and callable(m.time.perf_counter); assert Path(m.__file__).parent.samefile(Path.cwd())"
            result = subprocess.run([sys.executable,'-c',code],cwd=root,env=env,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(original.read_bytes(),source)
            target = support/manifest['overrides']['tempo_evaluation_metrics']['replacement'];target.write_bytes(b'changed')
            result = subprocess.run([sys.executable,'-c',code],cwd=root,env=env,capture_output=True,text=True,timeout=20)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('checksum verification',result.stderr)

    def test_runtime_verifier_compares_the_materialized_support_with_the_archived_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'runtime-support/helper.py';path.parent.mkdir();path.write_bytes(b'unchanged')
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,'w') as archive:archive.writestr('runtime-support/helper.py',b'unchanged')
            runtime.verify_runtime_support(root,stream.getvalue())
            path.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'runtime changed'):runtime.verify_runtime_support(root,stream.getvalue())

    def test_launcher_routes_only_the_recorded_worker_and_uses_the_child_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();support=root/'runtime-support';support.mkdir()
            (support/'compatibility.json').write_text(json.dumps({'overrides':{'train_yolo':{}}}))
            command=['python','-u',str(root/'train_yolo.py'),'--worker','nano_scout']
            env={'TEMPO_EXECUTION_RUNTIME':str(root)}
            result=runtime.execution_command(command,env)
            self.assertEqual(result[:4],['python','-u',str(support/'execute_worker.py'),str(root/'train_yolo.py')])
            self.assertEqual(runtime.execution_command(['python','other.py','--worker','nano_scout'],env),['python','other.py','--worker','nano_scout'])
            self.assertEqual(runtime.execution_command(command,{}),command)

    def test_receipt_rejects_changed_inputs_weights_and_escaped_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);output=root/'output';output.mkdir();dataset=root/'dataset';dataset.mkdir()
            (dataset/'image.bin').write_bytes(b'inputs');artifact=output/'yolo26n-scout.pt';artifact.write_bytes(b'completed weights')
            info={'seed':17}
            receipt=runtime.record_scout_candidate(artifact,output,dataset,info,150,4,None)
            self.assertEqual(runtime.resume_scout_candidate(output,dataset,info,150,4,None),str(artifact.resolve()))
            with self.assertRaisesRegex(ValueError,'training inputs'):
                runtime.resume_scout_candidate(output,dataset,info,149,4,None)
            artifact.write_bytes(b'changed weights')
            with self.assertRaisesRegex(ValueError,'checksum'):
                runtime.resume_scout_candidate(output,dataset,info,150,4,None)
            receipt['artifact']='../elsewhere.pt';(output/'trained_candidate.json').write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError,'retained'):
                runtime.resume_scout_candidate(output,dataset,info,150,4,None)


if __name__ == '__main__':unittest.main()
