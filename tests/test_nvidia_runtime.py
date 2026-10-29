"""Optional worker identity and headless cell result protocol."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import tempo_nvidia_runtime as runtime
from tempo_release import VERSION


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.profile = self.root / 'config'
        self.profile.mkdir()
        location = patch('tempo_config.get_config_dir', return_value=str(self.profile))
        location.start(); self.addCleanup(location.stop)
        identity = patch('tempo_release.build_identity', return_value={'commit': 'a' * 40})
        identity.start(); self.addCleanup(identity.stop)

    def install_fixture(self):
        executable = runtime.runtime_root() / 'build-a' / 'TEMPO' / 'TEMPO.exe'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'isolated synthetic worker')
        state = dict(schema_version=1, version=VERSION, commit='a' * 40,
                     bundle='build-a', executable='TEMPO/TEMPO.exe',
                     executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
                     archive_sha256='b' * 64)
        (runtime.runtime_root() / 'active.json').write_text(json.dumps(state))
        from tempo_nvidia_setup import release_descriptor
        descriptor = patch('tempo_nvidia_setup.release_descriptor', return_value={
            'archive_sha256': state['archive_sha256'],
            'executable_sha256': state['executable_sha256'],
            'executable': state['executable'],
        })
        descriptor.start(); self.addCleanup(descriptor.stop)
        return executable, state

    def frozen(self):
        return patch.object(runtime.sys, 'frozen', True, create=True)

    def test_only_matching_verified_worker_is_selected(self):
        executable, state = self.install_fixture()
        with patch.object(runtime.sys, 'platform', 'win32'), self.frozen():
            self.assertEqual(runtime.active_executable(), executable.resolve())
            executable.write_bytes(b'tampered')
            self.assertIsNone(runtime.active_executable())
            executable.write_bytes(b'isolated synthetic worker')
            state['commit'] = 'c' * 40
            (runtime.runtime_root() / 'active.json').write_text(json.dumps(state))
            self.assertIsNone(runtime.active_executable())

    def test_unsafe_paths_and_uninstalled_workers_are_ignored(self):
        _, state = self.install_fixture()
        with patch.object(runtime.sys, 'platform', 'win32'), self.frozen():
            for unsafe in ('../other', '/tmp/other', 'C:/other', 'TEMPO\\other'):
                state['bundle'] = unsafe
                (runtime.runtime_root() / 'active.json').write_text(json.dumps(state))
                self.assertIsNone(runtime.active_executable())
            (runtime.runtime_root() / 'active.json').unlink()
            self.assertIsNone(runtime.active_executable())

    def test_parent_reads_file_result_even_with_no_child_output(self):
        executable, _ = self.install_fixture()
        image = self.root / 'image.tif'; image.write_bytes(b'image')
        model = self.root / 'model.pt'; model.write_bytes(b'model')
        def subprocess_fixture(command, **kwargs):
            self.assertEqual(command[0], str(executable))
            self.assertEqual(command[1], '--predict-cells')
            self.assertEqual(kwargs['timeout'], 900)
            self.assertIs(kwargs['stdout'], runtime.subprocess.DEVNULL)
            Path(command[-1]).write_text(json.dumps(dict(success=True, result_kind='cells', boxes=[{'polygon': [[1, 2]]}])))
            return Mock(returncode=0)
        with patch.object(runtime, 'active_executable', return_value=executable), \
             patch.object(runtime.subprocess, 'run', side_effect=subprocess_fixture):
            result = runtime.run_cellpose(image, model)
        self.assertEqual(len(result['boxes']), 1)

    def test_child_failure_report_and_missing_output_are_distinct(self):
        executable, _ = self.install_fixture()
        with patch.object(runtime, 'active_executable', return_value=executable), \
             patch.object(runtime.subprocess, 'run', return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, 'exited with code 1'):
                runtime.run_cellpose('image', 'model')
        def failed(command, **kwargs):
            Path(command[-1]).write_text(json.dumps(dict(success=False, error='CUDA unavailable')))
            return Mock(returncode=1)
        with patch.object(runtime, 'active_executable', return_value=executable), \
             patch.object(runtime.subprocess, 'run', side_effect=failed):
            self.assertEqual(runtime.run_cellpose('image', 'model')['error'], 'CUDA unavailable')

    def test_worker_reports_failure_without_traceback_or_unbounded_output(self):
        report = self.root / 'result.json'
        with patch.dict(os.environ), patch.dict(sys.modules, {'torch': Mock(__version__='2.14.1+cu126',
                                              cuda=Mock(is_available=Mock(return_value=False)),
                                              version=Mock(cuda='12.6')),
                                      'torchvision': Mock(__version__='0.29.1+cu126')}):
            code = runtime.probe_command(report)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(report.read_text()), {'success': False, 'error': 'RuntimeError'})
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_prediction_rejects_unavailable_cuda_before_loading_model(self):
        report = self.root / 'prediction.json'
        image = self.root / 'image.tif'; image.write_bytes(b'image')
        model = self.root / 'model.pt'; model.write_bytes(b'model')
        fake_torch = Mock(cuda=Mock(is_available=Mock(return_value=False)), version=Mock(cuda=None))
        with patch.dict(os.environ), patch.dict(sys.modules, {'torch': fake_torch}):
            self.assertEqual(runtime.predict_command(image, model, '0.15', '0.4', report), 1)
        self.assertEqual(json.loads(report.read_text()), {'success': False, 'error': 'RuntimeError'})

    def test_probe_records_exact_runtime_identity_after_cuda_calculation(self):
        report = self.root / 'probe.json'
        tensor = Mock()
        tensor.__mul__ = Mock(return_value=tensor)
        tensor.sum.return_value = Mock(item=Mock(return_value=12.0))
        fake_torch = Mock(__version__='2.14.1+cu126', version=Mock(cuda='12.6'))
        fake_torch.cuda.is_available.return_value = True
        fake_torch.device.return_value = 'cuda:0'
        fake_torch.ones.return_value = tensor
        fake_torch.cuda.get_device_name.return_value = 'Synthetic NVIDIA'
        fake_vision = Mock(__version__='0.29.1+cu126')
        with patch.dict(os.environ), patch.dict(sys.modules, {'torch': fake_torch, 'torchvision': fake_vision}):
            self.assertEqual(runtime.probe_command(report), 0)
        value = json.loads(report.read_text())
        self.assertEqual(value, dict(success=True, cuda_version='12.6',
                                     device='Synthetic NVIDIA', app_version=VERSION, commit='a' * 40))
        fake_torch.cuda.synchronize.assert_called_once_with('cuda:0')

    def test_early_dispatch_skips_desktop_imports(self):
        source = Path(__file__).resolve().parents[1] / 'tempo_gui.py'
        report = self.root / 'report.json'
        with patch.object(sys, 'argv', ['TEMPO.exe', '--probe-cuda', str(report)]), \
             patch.object(runtime, 'probe_command', return_value=7) as probe:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(source), run_name='__main__')
        self.assertEqual(raised.exception.code, 7)
        probe.assert_called_once_with(str(report))

    def test_packaged_cellpose_uses_verified_worker(self):
        import yolo_inference_helper as helper
        model = self.root / 'cellpose4_cardio.pt'; model.write_bytes(b'fixture')
        image = self.root / 'image.tif'; image.write_bytes(b'fixture')
        expected = dict(success=True, result_kind='cells', boxes=[])
        with patch.object(helper.platform, 'system', return_value='Windows'), \
             patch.object(helper.sys, 'frozen', True, create=True), \
             patch.object(helper, 'resolve_model_path', return_value=str(model)), \
             patch.object(helper, 'run_inference', side_effect=AssertionError('CPU path invoked')), \
             patch.object(runtime, 'active_executable', return_value=Path('worker.exe')), \
             patch.object(runtime, 'run_cellpose', return_value=expected) as worker:
            self.assertIs(helper.detect_cells_via_bridge(str(image), 'cellpose'), expected)
        worker.assert_called_once_with(str(image), str(model), conf=0.15, iou=0.40)

    def test_uninstalled_worker_preserves_cpu_path(self):
        import yolo_inference_helper as helper
        model = self.root / 'cellpose4_cardio.pt'; model.write_bytes(b'fixture')
        image = self.root / 'image.tif'; image.write_bytes(b'fixture')
        expected = dict(success=True, result_kind='cells', boxes=[])
        with patch.object(helper.platform, 'system', return_value='Windows'), \
             patch.object(helper.sys, 'frozen', True, create=True), \
             patch.object(helper, 'resolve_model_path', return_value=str(model)), \
             patch.object(runtime, 'active_executable', return_value=None), \
             patch.object(helper, 'run_inference', return_value=expected) as local:
            self.assertIs(helper.detect_cells_via_bridge(str(image), 'cellpose'), expected)
        local.assert_called_once()


if __name__ == '__main__':
    unittest.main()
