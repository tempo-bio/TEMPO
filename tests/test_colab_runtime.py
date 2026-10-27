"""Exercise Colab logging/failure handling without ML packages or a GPU."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from notebooks.colab_runtime import check_training_environment, check_training_steps, configure_cuda_environment, install_training_fixes, run_logged


class ColabRuntimeTests(unittest.TestCase):
    def test_cuda_paths_come_from_target_environment_and_preserve_driver_paths(self):
        paths=dict(packages=['nvidia-cuda-runtime-cu12','nvidia-cudnn-cu12','nvidia-cublas-cu12'],
                   libraries=['/isolated/nvidia/cuda/lib','/isolated/nvidia/cudnn/lib'],
                   binaries=['/isolated/nvidia/nvcc/bin'])
        environment={'LD_LIBRARY_PATH':'/colab/driver:/isolated/nvidia/cuda/lib',
                     'PATH':'/system/bin','HF_TOKEN':'test-secret'}
        completed=subprocess.CompletedProcess([],0,stdout=json.dumps(paths),stderr='')
        with patch('sys.platform','linux'), patch('os.pathsep', ':'), patch('subprocess.run',return_value=completed) as inspect:
            configured=configure_cuda_environment('/isolated/bin/python',environment)
        self.assertEqual(inspect.call_args.args[0][0],'/isolated/bin/python')
        self.assertEqual(inspect.call_args.kwargs['timeout'],30)
        self.assertNotIn('import tensorflow',inspect.call_args.args[0][-1])
        self.assertEqual(configured['LD_LIBRARY_PATH'],
                         '/isolated/nvidia/cuda/lib:/isolated/nvidia/cudnn/lib:/colab/driver')
        self.assertEqual(configured['PATH'],'/isolated/nvidia/nvcc/bin:/system/bin')
        self.assertEqual(configured['HF_TOKEN'],'test-secret')
        self.assertEqual(environment['PATH'],'/system/bin')

    def test_cuda_setup_refuses_missing_libraries_or_mixed_major_versions(self):
        fixtures=[(dict(packages=[],libraries=[],binaries=[]),'libraries are missing'),
                  (dict(packages=['nvidia-cudnn-cu13'],libraries=['/wrong/lib'],binaries=[]),'CUDA 13 wheels')]
        for paths,message in fixtures:
            with self.subTest(message=message),patch('sys.platform','linux'), \
                 patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,stdout=json.dumps(paths))):
                with self.assertRaisesRegex(RuntimeError,message):
                    configure_cuda_environment('/isolated/bin/python',{})

    def test_cuda_setup_leaves_non_linux_platforms_unchanged(self):
        for platform in ('darwin','win32'):
            with self.subTest(platform=platform),patch('sys.platform',platform),patch('subprocess.run') as inspect:
                environment={'PATH':'/unchanged'}
                self.assertEqual(configure_cuda_environment('unused',environment),environment)
                inspect.assert_not_called()

    def test_cuda_discovery_reads_real_metadata_without_loading_fake_libraries(self):
        with tempfile.TemporaryDirectory() as directory:
            site=Path(directory)
            packages={'nvidia-cuda-runtime-cu12':('cuda_runtime','libcudart.so.12'),
                      'nvidia-cudnn-cu12':('cudnn','libcudnn.so.9'),
                      'nvidia-cublas-cu12':('cublas','libcublas.so.12')}
            for name,(folder,filename) in packages.items():
                relative=Path('nvidia')/folder/'lib'/filename
                artifact=site/relative;artifact.parent.mkdir(parents=True);artifact.write_bytes(b'not a loadable library')
                metadata=site/(name.replace('-','_')+'-1.0.dist-info');metadata.mkdir()
                (metadata/'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n')
                (metadata/'RECORD').write_text(f'{relative.as_posix()},,\n')
            environment=dict(os.environ,PYTHONPATH=str(site))
            with patch('sys.platform','linux'):
                configured=configure_cuda_environment(sys.executable,environment)
            library_paths = configured['LD_LIBRARY_PATH'].split(os.pathsep)
            for folder,filename in packages.values():
                self.assertIn(str((site/'nvidia'/folder/'lib').resolve()), library_paths)

    def test_bundle_installs_fixes_and_preserves_originals_and_results(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            original = 'print("old trainer")\n'
            (root/'train_yolo.py').write_text(original)
            (root/'runs').mkdir()
            (root/'runs/checkpoint.pt').write_bytes(b'preserved weights')
            files = {'train_yolo.py':'print("fixed trainer")\n',
                     'tempo_training.py':'print("fixed adapters")\n',
                     'requirements-training.txt':'tensorboard>=2.16\n'}
            install_training_fixes(root, files)
            install_training_fixes(root, files)
            self.assertEqual((root/'train_yolo.py').read_text(), files['train_yolo.py'])
            backups = list((root/'.tempo-notebook-backups').iterdir())
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), original)
            self.assertEqual((root/'runs/checkpoint.pt').read_bytes(), b'preserved weights')

    def test_invalid_bundle_is_rejected_before_any_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                install_training_fixes(root, {'../outside.py':'print(1)'})
            with self.assertRaises(SyntaxError):
                install_training_fixes(root, {'train_yolo.py':'print(1)',
                                             'tempo_training.py':'invalid python here!',
                                             'requirements-training.txt':'tensorboard'})
            self.assertEqual(list(root.iterdir()), [])

    def test_real_worker_check_stops_at_first_failure_and_retains_partial_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'runs').mkdir()
            report = root/'runs/training_report.json'
            report.write_text('previous results')
            models = []
            def run(command, **kwargs):
                model = command[command.index('--worker') + 1]
                models.append(model)
                self.assertEqual(command[command.index('--epochs') + 1], '1')
                self.assertEqual(command[command.index('--workers') + 1], '0')
                if model != 'stardist':
                    raise RuntimeError(f'{model} training failed')
                output = Path(command[command.index('--output') + 1])
                output.mkdir()
                artifact = output/'checkpoint.pt'
                artifact.write_bytes(b'model')
                (output/'result.json').write_text(json.dumps({'artifact':str(artifact),
                    'sha256':hashlib.sha256(b'model').hexdigest(), 'metrics':{'score':0.1}}))
            with patch('notebooks.colab_runtime.run_logged', side_effect=run), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError) as raised:
                    check_training_steps(sys.executable, root)
            self.assertEqual(models, ['stardist', 'cellpose'])
            self.assertIn('cellpose', str(raised.exception))
            partial = json.loads((root/'runs/preflight_report.json').read_text())
            self.assertFalse(partial['success'])
            self.assertEqual(set(partial['results']), {'stardist'})
            self.assertEqual(len(partial['errors']), 1)
            self.assertIn('cellpose.log', partial['errors'][0])
            self.assertEqual(json.loads(next((root/'runs/training-checks').glob('*/summary.json')).read_text()), partial)
            self.assertEqual(report.read_text(), 'previous results')

    def test_one_epoch_checks_propagate_cancellation_without_launching_next_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cancelled = lambda: False
            with patch('notebooks.colab_runtime.run_logged', side_effect=InterruptedError('Cancelled')) as launch:
                with self.assertRaises(InterruptedError):
                    check_training_steps(sys.executable, root, cancelled=cancelled)
            self.assertEqual(launch.call_count, 1)
            self.assertIs(launch.call_args.kwargs['cancelled'], cancelled)
            self.assertIsNone(launch.call_args.kwargs.get('timeout'))
            self.assertTrue(json.loads((root/'runs/preflight_report.json').read_text())['cancelled'])
            self.assertFalse((root/'runs/training_report.json').exists())

    def test_one_epoch_checks_reject_corrupt_checkpoints_before_next_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def run(command, **kwargs):
                output = Path(command[command.index('--output')+1]); output.mkdir()
                artifact = output/'checkpoint.pt'; artifact.write_bytes(b'changed')
                (output/'result.json').write_text(json.dumps({'artifact':str(artifact),
                    'sha256':hashlib.sha256(b'original').hexdigest(), 'metrics':{'score':0.1}}))
            with patch('notebooks.colab_runtime.run_logged', side_effect=run) as launch:
                with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                    check_training_steps(sys.executable, root)
            self.assertEqual(launch.call_count, 1)
            self.assertFalse(json.loads((root/'runs/preflight_report.json').read_text())['success'])

    def test_stardist_failure_does_not_launch_cellpose_and_retains_logs_and_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launched = []
            def run(command, **kwargs):
                model = command[command.index('--worker')+1]
                launched.append(model)
                self.assertIsNone(kwargs.get('timeout'))
                Path(kwargs['log_path']).write_text(f'{model} output')
                output = Path(command[command.index('--output')+1]); output.mkdir()
                artifact = output/'checkpoint.pt'; artifact.write_bytes(b'candidate')
                if model == 'stardist':
                    raise RuntimeError('StarDist worker failed')
                (output/'result.json').write_text(json.dumps(dict(artifact=str(artifact),
                    sha256=hashlib.sha256(b'candidate').hexdigest(), metrics={'score':0.2})))
            with patch('notebooks.colab_runtime.run_logged', side_effect=run), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'stardist'):
                    check_training_steps(sys.executable, root, training_check=True)
            self.assertEqual(launched, ['stardist'])
            report = json.loads((root/'runs/preflight_report.json').read_text())
            self.assertEqual(set(report['results']), set())
            self.assertFalse(report['publishable'])
            self.assertEqual(report['evaluation_partition'], 'train')
            folder = next((root/'runs/training-checks').iterdir())
            self.assertEqual((folder/'stardist.log').read_text(), 'stardist output')
            self.assertTrue((folder/'stardist/checkpoint.pt').is_file())

    def test_unbounded_worker_is_not_terminated_after_thirty_minutes(self):
        ticks = iter([0])
        with contextlib.redirect_stdout(io.StringIO()), \
             patch('notebooks.colab_runtime.time.monotonic', side_effect=lambda: next(ticks, 1801)):
            run_logged([sys.executable, '-c', 'print("worker completed")'],
                       label='long training', cancelled=lambda: False)

    def test_failed_check_does_not_save_credentials_in_partial_report(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'HF_TOKEN': 'fixture-secret-token'}), \
             patch('notebooks.colab_runtime.run_logged', side_effect=RuntimeError('Denied fixture-secret-token')), \
             contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            with self.assertRaises(RuntimeError):
                check_training_steps(sys.executable, root)
            self.assertNotIn('fixture-secret-token', (root/'runs/preflight_report.json').read_text())

    def test_child_output_is_visible_before_child_finishes(self):
        # The child can finish only after its first line has reached the notebook.
        with tempfile.TemporaryDirectory() as directory:
            ack = Path(directory) / 'ack'
            def display(message, **kwargs):
                if 'CHILD READY' in message:
                    ack.touch()
            code = ("import pathlib,time; print('CHILD READY',flush=True); "
                    f"ack=pathlib.Path({str(ack)!r}); deadline=time.monotonic()+5\n"
                    "while not ack.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                    "assert ack.exists(), 'output was buffered until process exit'\n")
            with patch('builtins.print', side_effect=display):
                run_logged([sys.executable, '-u', '-c', code], label='live output', timeout=8)
            self.assertTrue(ack.exists())

    def test_colab_backend_override_reaches_child_and_grandchild_only(self):
        output = io.StringIO()
        inline = 'module://matplotlib_inline.backend_inline'
        with patch.dict(os.environ, {'MPLBACKEND': inline}), contextlib.redirect_stdout(output):
            run_logged([sys.executable, '-c',
                        "import os,subprocess,sys; assert os.environ['MPLBACKEND']=='Agg'; "
                        "subprocess.run([sys.executable,'-c',\"import os; assert os.environ['MPLBACKEND']=='Agg'\"],check=True)"],
                       label='backend inheritance')
            self.assertEqual(os.environ['MPLBACKEND'], inline)
        self.assertIn('PASS: backend inheritance', output.getvalue())

    def test_streams_both_outputs_and_redacts_secrets(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(output):
            log = Path(directory) / 'run.log'
            run_logged([sys.executable, '-c',
                        "import os,sys; print(os.environ['HF_TOKEN']); print('stderr',file=sys.stderr)"],
                       label='child', env={**os.environ, 'HF_TOKEN': 'test-secret'}, log_path=log)
            self.assertNotIn('test-secret', log.read_text())
            self.assertIn('stderr', log.read_text())
        self.assertIn('[REDACTED]', output.getvalue())
        self.assertIn('PASS: child', output.getvalue())

    def test_native_signal_names_the_failed_stage(self):
        if os.name != 'posix':
            self.skipTest('POSIX signal test')
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'native test failed: SIGTERM'):
                run_logged([sys.executable, '-c', 'import os,signal; os.kill(os.getpid(),signal.SIGTERM)'],
                           label='native test')

    def test_nonzero_exit_is_failure(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, 'exit code 3'):
                run_logged([sys.executable, '-c', 'raise SystemExit(3)'], label='bad import')
        self.assertIn('FAILED: bad import', output.getvalue())

    def test_timeout_terminates_child_and_emits_heartbeat(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('notebooks.colab_runtime.subprocess.Popen',
                                                       wraps=subprocess.Popen) as popen:
            with self.assertRaises(TimeoutError):
                run_logged([sys.executable, '-c', 'import time; time.sleep(30)'],
                           label='stalled import', timeout=0.3, heartbeat=0.05)
            self.assertEqual(popen.call_count, 1)
        self.assertIn('RUNNING: stalled import', output.getvalue())

    def test_cancellation_terminates_child(self):
        with contextlib.redirect_stdout(io.StringIO()), patch('notebooks.colab_runtime.subprocess.Popen',wraps=subprocess.Popen) as popen:
            with self.assertRaises(InterruptedError):
                run_logged([sys.executable,'-c','import time; time.sleep(30)'],label='cancelled worker',cancelled=lambda:True)
        self.assertEqual(popen.call_count,1)

    def test_preflight_continues_isolated_checks_but_blocks_training(self):
        labels = []
        def fake_run(command, *, label, **kwargs):
            labels.append(label)
            if label == 'Import tensorflow':
                raise RuntimeError('Import tensorflow failed: SIGSEGV (-11)')
        with patch('notebooks.colab_runtime.run_logged', side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, 'SIGSEGV'):
                check_training_environment(sys.executable, '.')
        self.assertIn('Import cellpose.train', labels)
        self.assertNotIn('StarDist trainer compatibility', labels)


if __name__ == '__main__':
    unittest.main()
