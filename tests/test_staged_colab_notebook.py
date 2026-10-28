"""Execute the self-contained notebook with isolated Colab and runner services."""
import ast
import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from subprocess import run as run_process
import sys
import urllib.request
from urllib.error import HTTPError
from unittest.mock import patch
from notebooks.build_staged_colab_notebook import build_notebook
from tempo_cloud import BUNDLE_FILES, archive_sources


ISOLATED_KERNEL_SETUP = r'''import ast,base64,contextlib,importlib.util,io,json,os,subprocess,sys,types,urllib.request
from pathlib import Path
from unittest.mock import patch
fixture=json.loads(Path(sys.argv[1]).read_text())
destination=Path(sys.argv[1]).parent/'content';destination.mkdir()
assert importlib.util.find_spec('tempo_training_runtime') is None
colab=types.ModuleType('google.colab');colab.userdata=types.SimpleNamespace(get=lambda name:'fixture-secret')
def response(request,**kwargs):
    if request.full_url.endswith('evaluation_request.json'):return io.BytesIO(json.dumps(fixture['job']).encode())
    return io.BytesIO(base64.b64decode(fixture['protocol'] if request.full_url.endswith('protocol.zip') else fixture['bundle']))
class ColabPaths(ast.NodeTransformer):
    def visit_Constant(self,node):
        if isinstance(node.value,str) and node.value.startswith('/content'):
            return ast.copy_location(ast.Constant(str(destination)+node.value[len('/content'):]),node)
        return node
code=[''.join(cell['source']) for cell in fixture['notebook']['cells'] if cell['cell_type']=='code']
namespace={};commands=[];popen=subprocess.Popen
def launch(command,**kwargs):
    commands.append(command)
    # Keep the actual logger and subprocess path. Substitute only the external
    # installer/GPU diagnostic programs: this check installs no ML packages.
    return popen([sys.executable,'-I','-S','-c','print("isolated setup child completed")'],**kwargs)
def execute(index):
    tree=ast.fix_missing_locations(ColabPaths().visit(ast.parse(code[index])))
    exec(compile(tree,'isolated-notebook-cell','exec'),namespace)
with patch.dict(sys.modules,{'google.colab':colab}),patch('sys.platform','linux'):
    for attempt in range(2):
        with patch('urllib.request.OpenerDirector.open',side_effect=response),patch('subprocess.run',return_value=types.SimpleNamespace(returncode=0,stdout='NVIDIA A100')):
            execute(0)
        assert importlib.util.find_spec('tempo_training_runtime') is None
        namespace['configure_cuda_environment']=lambda python:{}
        with patch('sys.version_info',(3,12,10)),patch('venv.EnvBuilder'),patch('subprocess.Popen',side_effect=launch):
            execute(1)
        assert namespace['setup_ready']
        assert not namespace['training_ready']
assert len(commands)==6
assert all('pip' in commands[i] for i in (0,1,3,4))
assert all('check_training_environment' in commands[i][-1] for i in (2,5))
assert 'tempo_training_runtime' not in sys.modules
print('ISOLATED KERNEL SETUP PASSED')
'''


class StagedColabNotebookTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.content=Path(temporary.name)/'content';self.content.mkdir()
        self.notebook=build_notebook('fixture/data',models_repo='fixture/models')
        self.code=[''.join(cell['source']) for cell in self.notebook['cells'] if cell['cell_type']=='code']
        self.namespace={};self.output=io.StringIO();self.commands=[]
        colab=types.ModuleType('google.colab')
        colab.userdata=types.SimpleNamespace(get=lambda name:'fixture-secret-never-print')
        self.colab = colab
        self.addCleanup(patch.stopall)
        patch.dict('os.environ',{}).start()
        patch.dict('sys.modules',{'google.colab':colab}).start()
        patch('sys.platform','linux').start()
        patch('subprocess.run',return_value=types.SimpleNamespace(returncode=0,stdout='NVIDIA A100')).start()
        patch('urllib.request.OpenerDirector.open',side_effect=HTTPError('fixture',404,'No evaluation',{},None)).start()

    def execute(self,index):
        destination=self.content
        class ColabPaths(ast.NodeTransformer):
            def visit_Constant(self,node):
                if isinstance(node.value,str) and node.value.startswith('/content'):
                    return ast.copy_location(ast.Constant(str(destination)+node.value[len('/content'):]),node)
                return node
        tree=ast.fix_missing_locations(ColabPaths().visit(ast.parse(self.code[index])))
        with contextlib.redirect_stdout(self.output):exec(compile(tree,'notebook-cell','exec'),self.namespace)

    def simulate_runner(self,command,**kwargs):
        self.commands.append(command)
        if '--stage' in command:
            self.assertEqual(command[-1],'start')
            root=self.namespace['root'];(root/'runs').mkdir(exist_ok=True)
            models={name:dict(outcome='First model saved',metrics={'metric':'fixture-score','score':.5},baseline_metrics=None)
                    for name in ('nano_scout','stardist','cellpose')}
            summary=dict(release_id='fixture-run',dataset_revision='pinned',updated_count=3,
                         evaluation_basis='held-out-images',models=models)
            (root/'runs/release_summary.json').write_text(json.dumps(summary))

    def environment(self):
        self.namespace['run_logged']=self.simulate_runner
        self.namespace['configure_cuda_environment']=lambda python:{}
        with patch('sys.version_info',(3,12,10)),patch('venv.EnvBuilder'):
            self.execute(1)

    def test_generated_artifact_matches_builder_and_is_clean(self):
        path=Path(__file__).resolve().parents[1]/'notebooks/TEMPO_Colab_Trainer.ipynb'
        actual=json.loads(path.read_text());self.assertEqual(actual,build_notebook())
        self.assertEqual(actual['metadata']['tempo']['workflow'],'colab-owned-v1')
        self.assertEqual(actual['metadata']['tempo']['dataset_repo'],'tempo-bio/cardiomyocyte-data')
        for cell in actual['cells']:
            if cell['cell_type']=='code':
                compile(''.join(cell['source']),'delivered-cell','exec')
                self.assertIsNone(cell['execution_count']);self.assertEqual(cell['outputs'],[])
        parameters=[line.split('=')[0].strip() for text in self.code for line in text.splitlines() if '@param' in line]
        self.assertEqual(parameters,['MAXIMUM_EPOCHS','REQUIRED_GPU'])

    def test_setup_uses_real_logger_in_kernel_without_workspace_imports(self):
        from tests.evaluation_fixtures import protocol
        from tempo_evaluation_protocol import read_package
        import zipfile
        for legacy in (False,True):
            with self.subTest(legacy=legacy),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);_,package=read_package(protocol(root/'protocol'))
                bundle=archive_sources(Path(__file__).resolve().parents[1])
                if legacy:
                    stream=io.BytesIO()
                    old_runtime=(Path(__file__).parent/'fixtures/legacy_training/notebooks__colab_runtime.py.txt').read_bytes()
                    with zipfile.ZipFile(io.BytesIO(bundle)) as original,zipfile.ZipFile(stream,'w') as frozen:
                        for name in original.namelist():
                            if name!='tempo_training_runtime.py':
                                frozen.writestr(name,old_runtime if name=='notebooks/colab_runtime.py' else original.read(name))
                    bundle=stream.getvalue()
                job=dict(trust='protected-model-repository-v1',plan_sha256='d'*64,mode='model-evaluation',id='a'*32,evaluation_id='b'*32,status='queued',phase='calibrate',
                         dataset_repo='fixture/data',models_repo='fixture/models',gpu='A100',
                         bundle_sha256=hashlib.sha256(bundle).hexdigest(),protocol_sha256=hashlib.sha256(package).hexdigest())
                fixture=root/'fixture.json';fixture.write_text(json.dumps(dict(notebook=self.notebook,job=job,
                    bundle=base64.b64encode(bundle).decode(),protocol=base64.b64encode(package).decode())))
                # -I -S removes both the checkout import path and installed project
                # modules; the kernel receives only the delivered notebook payload.
                result=run_process([sys.executable,'-I','-S','-X','utf8','-',str(fixture)],input=ISOLATED_KERNEL_SETUP,
                    text=True,encoding='utf-8',capture_output=True,cwd=root,timeout=30)
                self.assertEqual(result.returncode,0,(result.stdout or '')+'\n'+(result.stderr or ''))
                self.assertIn('ISOLATED KERNEL SETUP PASSED',result.stdout)

    def test_sources_are_included_and_missing_evaluation_uses_regular_training(self):
        self.execute(0)
        root=self.namespace['root'];repository=Path(__file__).resolve().parents[1]
        for name in BUNDLE_FILES:self.assertEqual((root/name).read_bytes(),(repository/name).read_bytes())
        settings=json.loads((root/'notebook_settings.json').read_text())
        self.assertEqual(settings['epochs'],150);self.assertEqual(settings['gpu'],'A100')
        self.assertNotIn('fixture-secret-never-print',self.output.getvalue())
        self.assertNotIn('HF_TOKEN',settings)
        self.assertFalse((root/'cloud_request.json').exists())

    def test_windows_and_colab_build_identical_source_archives(self):
        repository = Path(__file__).resolve().parents[1]
        with patch('sys.platform', 'win32'):
            windows = archive_sources(repository)
        with patch('sys.platform', 'linux'):
            colab = archive_sources(repository)
        self.assertEqual(windows, colab)

    def test_queued_evaluation_supplies_settings_and_runs_through_standard_run_all(self):
        bundle=archive_sources(Path(__file__).resolve().parents[1])
        import zipfile
        frozen = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(bundle)) as original, zipfile.ZipFile(frozen, 'w') as archive:
            for name in original.namelist():
                data = original.read(name)
                if name in ('tempo_cloud.py', 'tempo_evaluation_runner.py'):
                    data += b'\n# Frozen source before the checkpoint persistence repair.\n'
                archive.writestr(name, data)
        bundle = frozen.getvalue()
        from tests.evaluation_fixtures import protocol
        from tempo_evaluation_protocol import read_package
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            _,package=read_package(protocol(Path(directory)))
        job=dict(trust='protected-model-repository-v1',plan_sha256='d'*64,mode='model-evaluation',id='a'*32,evaluation_id='b'*32,status='queued',phase='calibrate',
                 dataset_repo='fixture/data',models_repo='fixture/models',epochs=1,gpu='Any CUDA GPU',
                 bundle_sha256=hashlib.sha256(bundle).hexdigest(),protocol_sha256=hashlib.sha256(package).hexdigest())
        def response(request,**kwargs):
            return io.BytesIO(json.dumps(job).encode() if request.full_url.endswith('evaluation_request.json') else package if request.full_url.endswith('protocol.zip') else bundle)
        with patch('urllib.request.OpenerDirector.open',side_effect=response),patch('subprocess.run',return_value=types.SimpleNamespace(returncode=0,stdout='NVIDIA T4')):
            self.execute(0)
        self.assertEqual(self.namespace['settings']['evaluation_request'],job)
        self.assertEqual(self.namespace['settings']['epochs'],1)
        self.assertEqual(self.namespace['settings']['gpu'],'Any CUDA GPU')
        root = self.namespace['root']
        runtime = (root/'evaluation-runtime.zip').read_bytes()
        self.assertEqual(hashlib.sha256(runtime).hexdigest(), self.namespace['settings']['runtime_sha256'])
        repository = Path(__file__).resolve().parents[1]
        with zipfile.ZipFile(io.BytesIO(runtime)) as archive:
            for name in ('tempo_cloud.py', 'tempo_evaluation_runner.py'):
                self.assertEqual(archive.read(name), (repository/name).read_bytes())
                self.assertNotEqual(archive.read(name), (root/name).read_bytes())
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            for name in archive.namelist():
                self.assertEqual((root/name).read_bytes(), archive.read(name))
        (root/'evaluation-runtime.zip').write_bytes(b'changed controller')
        driver = root/'evaluation-runtime.py'
        with self.assertRaisesRegex(ValueError, 'controller changed'):
            exec(compile(driver.read_bytes(), str(driver), 'exec'), {'__file__':str(driver)})
        (root/'evaluation-runtime.zip').write_bytes(runtime)
        self.environment()
        def evaluation_runner(command,**kwargs):
            self.commands.append(command)
            path=self.namespace['root']/'runs/release_summary.json';path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(dict(workflow='model-evaluation',evaluation_id=job['evaluation_id'],stage='calibrate',
                dataset_revision='frozen',scope='Custom study',report=dict(summary='Custom result',tables=[]))))
        self.namespace['run_logged']=evaluation_runner
        self.execute(2);self.execute(3)
        self.assertEqual(Path(self.commands[-1][-1]).name, 'evaluation-runtime.py')
        self.assertIn('Review the stage in TEMPO',self.output.getvalue())
        self.assertIn('Current models were unchanged',self.output.getvalue())
        self.assertNotIn('fixture-secret-never-print',self.output.getvalue())

    def test_completed_evaluation_cannot_silently_start_regular_training(self):
        job=dict(trust='protected-model-repository-v1',plan_sha256='d'*64,mode='model-evaluation',id='a'*32,evaluation_id='b'*32,status='completed',phase='pilot',
                 dataset_repo='fixture/data',models_repo='fixture/models',epochs=1,gpu='A100',protocol_sha256='c'*64,bundle_sha256='e'*64)
        with patch('urllib.request.OpenerDirector.open',return_value=io.BytesIO(json.dumps(job).encode())):
            with self.assertRaisesRegex(RuntimeError,'queue its next stage'):
                self.execute(0)

    def test_run_all_automatically_executes_setup_diagnostics_training_and_results(self):
        self.execute(0);self.environment();self.execute(2);self.execute(3)
        self.assertEqual(len(self.commands),4)
        self.assertIn('install',self.commands[0]);self.assertIn('check',self.commands[1])
        self.assertIn('check_training_environment',self.commands[2][-1])
        self.assertEqual(self.commands[3][-2:],['--stage','start'])
        self.assertIn('Current models updated: 3',self.output.getvalue())
        self.assertTrue(self.namespace['training_ready'])

    def test_results_distinguish_new_score_from_previous_model_score(self):
        self.execute(0);self.environment();self.execute(2)
        path=self.namespace['root']/'runs/release_summary.json'
        summary=json.loads(path.read_text())
        summary['models']['nano_scout'].update(outcome='Updated',metrics={'metric':'mAP50','score':.0911},
                                             baseline_metrics={'metric':'mAP50','score':.0206})
        path.write_text(json.dumps(summary));self.execute(3)
        self.assertIn('New score: 0.0911 | Previous model score: 0.0206',self.output.getvalue())
        self.assertNotIn('Current score:',self.output.getvalue())

    def test_successive_run_all_creates_new_folder_and_preserves_old_files(self):
        self.execute(0);first=self.namespace['root'];(first/'saved-weight').write_bytes(b'keep')
        self.namespace.update(setup_ready=True,training_ready=True)
        self.execute(0)
        self.assertNotEqual(first,self.namespace['root'])
        self.assertEqual((first/'saved-weight').read_bytes(),b'keep')
        self.assertFalse(self.namespace['setup_ready']);self.assertFalse(self.namespace['training_ready'])

    def test_one_epoch_and_other_cuda_gpus_are_not_separate_workflows(self):
        self.code[0]=self.code[0].replace('MAXIMUM_EPOCHS = 150','MAXIMUM_EPOCHS = 1').replace('REQUIRED_GPU = "A100"','REQUIRED_GPU = "Any CUDA GPU"')
        with patch('subprocess.run',return_value=types.SimpleNamespace(returncode=0,stdout='NVIDIA T4')):self.execute(0)
        self.assertEqual(self.namespace['settings']['epochs'],1)
        self.environment();self.execute(2)
        self.assertEqual(self.commands[-1][-2:],['--stage','start'])

    def test_missing_secret_is_actionable_and_redacts_details(self):
        with patch.object(self.colab.userdata, 'get', side_effect=RuntimeError('fixture-secret-never-print')):
            with self.assertRaisesRegex(RuntimeError,'Colab Secrets') as raised:self.execute(0)
        self.assertNotIn('fixture-secret-never-print',str(raised.exception))
        self.assertFalse(list(self.content.iterdir()))

    def test_gpu_mismatch_stops_before_extraction_or_training(self):
        with patch('subprocess.run',return_value=types.SimpleNamespace(returncode=0,stdout='NVIDIA T4')):
            with self.assertRaisesRegex(RuntimeError,'required GPU'):self.execute(0)
        self.assertFalse(list(self.content.iterdir()))

    def test_invalid_epochs_stop_before_runtime_work(self):
        self.code[0]=self.code[0].replace('MAXIMUM_EPOCHS = 150','MAXIMUM_EPOCHS = 0')
        with self.assertRaisesRegex(ValueError,'Maximum epochs'):self.execute(0)
        self.assertFalse(list(self.content.iterdir()))

    def test_changed_sources_or_settings_stop_later_execution(self):
        for kind in ('source','settings'):
            self.execute(0);self.environment();root=self.namespace['root']
            target=root/('tempo_training.py' if kind=='source' else 'notebook_settings.json')
            target.write_text('{}' if kind=='settings' else '# changed')
            with self.assertRaises(ValueError):self.execute(2)
            self.assertFalse(self.namespace['training_ready'])

    def test_failed_setup_prevents_training(self):
        self.execute(0)
        self.namespace['run_logged']=lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError('Package failure'))
        with patch('sys.version_info',(3,12,10)),patch('venv.EnvBuilder'):
            with self.assertRaisesRegex(RuntimeError,'Package failure'):self.execute(1)
        self.assertFalse(self.namespace['setup_ready'])
        with self.assertRaisesRegex(RuntimeError,'environment checks'):self.execute(2)

    def test_failed_training_cannot_show_old_success(self):
        self.execute(0);self.environment();self.execute(2)
        self.namespace['run_logged']=lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError('Worker failure'))
        with self.assertRaisesRegex(RuntimeError,'Worker failure'):self.execute(2)
        self.assertFalse(self.namespace['training_ready'])
        with self.assertRaisesRegex(RuntimeError,'did not finish'):self.execute(3)

    def test_cellpose_fallback_is_reported_from_run_details(self):
        self.execute(0);self.environment();self.execute(2)
        path=self.namespace['root']/'runs/release_summary.json';summary=json.loads(path.read_text())
        summary['models']['cellpose']['training_details']=dict(backbone='sam_vitl',pretrained_model='cpsam',fallback={'error_type':'ImportError'})
        path.write_text(json.dumps(summary));self.execute(3)
        self.assertIn('trained Cellpose-SAM',self.output.getvalue());self.assertIn('sam_vitl',self.output.getvalue())

    def test_notebook_embeds_specialists_and_reports_both_stages_and_class_scores(self):
        self.execute(0)
        self.assertTrue((self.namespace['root'] / 'tempo_cellpose.py').is_file())
        self.environment(); self.execute(2)
        path = self.namespace['root'] / 'runs/release_summary.json'
        summary = json.loads(path.read_text())
        result = summary['models']['cellpose']
        result['metrics'] = {'metric': 'cell_class_F1_IoU50', 'score': .7,
                             'class_scores': {'healthy': .5, 'dead': .9, 'healthy_group': .8, 'dead_group': .6},
                             'segmentation_score': .85, 'unclassified_predictions': 2}
        result['training_details'] = {'stages': {
            'healthy': {'backbone': 'dino_vitl', 'pretrained_model': 'cpdino',
                        'training_cells': 8, 'epochs': 150, 'fallback': None},
            'dead': {'backbone': 'sam_vitl', 'pretrained_model': 'cpsam',
                     'training_cells': 26, 'epochs': 150, 'fallback': {'error_type': 'ImportError'}}}}
        path.write_text(json.dumps(summary)); self.execute(3)
        output = self.output.getvalue()
        self.assertIn('Healthy specialist backbone: dino_vitl', output)
        self.assertIn('Dead specialist backbone: sam_vitl', output)
        for name, value in (('Healthy', .5), ('Dead', .9), ('Healthy group', .8), ('Dead group', .6)):
            self.assertIn(f'{name} F1: {value}', output)
        self.assertIn('Outline-only F1: 0.85', output)
        self.assertIn('Unclassified predictions: 2', output)
        self.assertIn('Dead specialist CPDINO initialization failed', output)

    def test_embedded_archive_checksum_is_checked(self):
        text=self.code[0]
        marker='bundle_sha256 = '
        start=text.index(marker)+len(marker);end=text.index('\n',start)
        self.code[0]=text[:start]+repr('0'*64)+text[end:]
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):self.execute(0)
        self.assertFalse(list(self.content.iterdir()))


if __name__ == '__main__':unittest.main()
