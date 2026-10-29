"""Failure injection for process isolation and mandatory Windows release gates."""
import json
import io
import hashlib
import importlib.metadata
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.windows_release_checks import run_logged, run_regressions, verify_executable, validate_report
from scripts.windows_gates import evaluate_gates
from scripts.windows_native_bundle import canonicalize_vc_runtime, prepare_build_environment, collect_wheel_libraries
from scripts.windows_packager import check_environment, start_stack_sampler, loaded_native_extensions
import tempo_release
import tempo_cloud


def valid_report():
    checks=list(tempo_release.REQUIRED_CHECKS)
    return dict(schema_version=tempo_release.REPORT_SCHEMA, commit='fixture', frozen=True,
                success=True, required_checks=checks, checks=checks, failures=[],
                runtime_details={name: {'origin':'fixture'} for name in checks})


class PackagingCommandTests(unittest.TestCase):
    def test_windows_runtime_disables_tqdm_monitor_before_model_imports(self):
        import tempo_runtime
        from tqdm.std import tqdm
        original = tqdm.monitor_interval
        original_lock = tqdm.get_lock()
        observed = []
        def load(name):
            observed.append((name, tqdm.monitor_interval, tqdm.get_lock(),
                             os.environ.get('HF_HUB_DISABLE_PROGRESS_BARS')))
            return Mock(__version__='fixture')
        try:
            with patch.dict(os.environ, {}, clear=False), \
                 patch.object(tempo_runtime.sys, 'platform', 'win32'), \
                 patch.object(tempo_runtime.importlib, 'import_module', side_effect=load):
                self.assertEqual(tempo_runtime.prepare_windows_runtime(),
                                 {'tensorflow': 'fixture', 'torch': 'fixture'})
        finally:
            tqdm.monitor_interval = original
            tqdm.set_lock(original_lock)
        self.assertEqual([name for name, _, _, _ in observed], ['tensorflow', 'torch'])
        self.assertTrue(all(interval == 0 and lock is not original_lock
                            and disabled == '1'
                            for _, interval, lock, disabled in observed))
        self.assertIs(observed[0][2], observed[1][2])

    def test_subtest_failure_is_reported_before_the_next_test_runs(self):
        from scripts.windows_test_suite import ImmediateFailures
        output = io.StringIO()
        observed = []
        class Fixture(unittest.TestCase):
            def test_failure(self):
                with self.subTest(kind='recording'):
                    self.fail('fixture callback was not delivered')
            def test_next(self):
                observed.append(output.getvalue())
        suite = unittest.TestSuite([Fixture('test_failure'), Fixture('test_next')])
        result = unittest.TextTestRunner(stream=output, resultclass=ImmediateFailures).run(suite)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("kind='recording'", observed[0])
        self.assertIn('fixture callback was not delivered', observed[0])

    def test_test_suite_sampler_stops_if_discovery_raises(self):
        from scripts import windows_test_suite
        stopped,sampler=Mock(),Mock()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(sys,'argv',['windows_test_suite','--output',str(Path(directory)/'result.json')]), \
             patch.object(windows_test_suite,'start_stack_sampler',return_value=(stopped,sampler)), \
             patch.object(windows_test_suite.faulthandler,'dump_traceback_later') as native_timer, \
             patch.object(unittest.defaultTestLoader,'discover',side_effect=RuntimeError('fixture discovery failure')):
            with self.assertRaisesRegex(RuntimeError,'fixture discovery failure'):
                windows_test_suite.main()
            stopped.set.assert_called_once_with()
            sampler.join.assert_called_once_with(timeout=5)
            native_timer.assert_not_called()

    def test_packaging_sampler_retains_stack_and_stops_without_native_timer(self):
        import threading
        ready=threading.Event()
        class EvidenceStream(io.StringIO):
            def write(self,text):
                result=super().write(text)
                if 'Parent native Python extensions:' in text: ready.set()
                return result
        output=EvidenceStream()
        stopped,sampler=start_stack_sampler(.01,output)
        try:
            self.assertTrue(ready.wait(10),'Packaging sampler did not produce evidence within its deadline. '+output.getvalue())
        finally:
            stopped.set();sampler.join(timeout=1)
        self.assertFalse(sampler.is_alive())
        self.assertIn('Packaging stack snapshot:',output.getvalue())
        self.assertIn('Parent native Python extensions:',output.getvalue())

    def test_sampler_inventory_does_not_trigger_module_lazy_loading(self):
        import types
        class LazyNativeModule(types.ModuleType):
            def __getattribute__(self, name):
                raise AssertionError('Diagnostic inspection invoked a lazy module hook')
        module = LazyNativeModule('fixture_native')
        module.__file__ = 'fixture_native.pyd'
        with patch.dict(sys.modules, {'fixture_native':module}):
            self.assertIn({'module':'fixture_native','path':'fixture_native.pyd'},loaded_native_extensions())

    def test_windows_cp1252_console_and_python_child_use_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);buffer=io.BytesIO()
            console=io.TextIOWrapper(buffer,encoding='cp1252')
            command=[sys.executable,'-c',"import sys;print('alpha=\\u03b1 check=\\u2713');print(sys.stdout.encoding)"]
            with patch('sys.stdout',console):
                self.assertEqual(run_logged(command,root/'unicode.log',5),0)
                console.flush()
                output=buffer.getvalue().decode('utf-8')
            self.assertIn('alpha=α check=✓',output)
            self.assertIn('alpha=α check=✓',(root/'unicode.log').read_text(encoding='utf-8'))
            self.assertIn('utf-8',(root/'unicode.log').read_text(encoding='utf-8'))
            console.close()

    def test_console_write_failure_still_drains_large_child_output(self):
        class BrokenConsole(io.StringIO):
            def write(self,text):
                if 'α' in text:raise UnicodeEncodeError('cp1252',text,0,1,'fixture')
                return super().write(text)
        with tempfile.TemporaryDirectory() as directory, patch('sys.stdout',BrokenConsole()):
            log=Path(directory)/'large.log'
            code=run_logged([sys.executable,'-c',"print('\\u03b1'*200000);print('finished draining')"],log,5)
            content=log.read_text(encoding='utf-8')
        self.assertEqual(code,1,'Console forwarding failure must fail the gate')
        self.assertIn('finished draining',content)
        self.assertNotIn('TimeoutExpired',content)

    def test_discovery_failure_does_not_skip_scientific_checks(self):
        with tempfile.TemporaryDirectory() as directory, patch('scripts.windows_release_checks.run_logged', side_effect=[1, 0, 0]) as run:
            self.assertEqual(run_regressions(directory), 1)
            self.assertEqual(run.call_count, 3)
            results=json.loads((Path(directory)/'regression-results.json').read_text())
            self.assertEqual(results, dict(regressions=1, kinetics=0, innovations=0))

    def test_command_failure_and_timeout_retain_output(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'command.log'
            self.assertEqual(run_logged([sys.executable, '-c', "print('diagnostic'); raise SystemExit(7)"], log, 5), 7)
            self.assertIn('diagnostic', log.read_text())
            self.assertEqual(run_logged([sys.executable, '-c', 'import time; time.sleep(10)'], log, .1), 1)
            self.assertIn('TimeoutExpired', log.read_text())

    def test_timeout_terminates_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); marker=root/'leaked-child'
            child="import time;from pathlib import Path;time.sleep(1);Path("+repr(str(marker))+ ").touch()"
            parent="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',"+repr(child)+"]);time.sleep(10)"
            self.assertEqual(run_logged([sys.executable,'-c',parent],root/'tree.log',.3),1)
            import time;time.sleep(1.2)
            self.assertFalse(marker.exists(),'Timed-out child survived process-tree cleanup')

    def test_frozen_verification_uses_isolated_cwd_and_rejects_stale_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();report=root/'report.json';exe=root/'application with spaces.exe'
            def launch(command, log, timeout, **kwargs):
                self.assertEqual(command,[str(exe),'--verify-installation',str(report)])
                self.assertFalse(report.exists())
                self.assertNotEqual(Path(kwargs['cwd']),root)
                self.assertIn(' ',str(kwargs['cwd']))
                self.assertEqual(kwargs['env']['HF_HUB_OFFLINE'],'1')
                self.assertNotIn('PYTHONPATH',kwargs['env'])
                self.assertTrue(Path(kwargs['env']['LOCALAPPDATA']).is_dir())
                report.write_text(json.dumps(valid_report()))
                return 0
            report.write_text(json.dumps(valid_report()))
            with patch('scripts.windows_release_checks.run_logged',side_effect=launch), patch.dict(os.environ,{'PYTHONPATH':'fixture','GITHUB_SHA':'fixture'}):
                self.assertEqual(verify_executable(exe,report),0)
            with patch('scripts.windows_release_checks.run_logged',return_value=0):
                self.assertEqual(verify_executable(exe,report),1)
            self.assertFalse(report.exists())

    def test_successful_exit_requires_valid_complete_identity_report(self):
        for mutate in (lambda r:r.update(success=False),lambda r:r.update(frozen=False),
                       lambda r:r.update(checks=[]),lambda r:r.update(required_checks=[]),
                       lambda r:r.update(commit='foreign'),lambda r:r.update(failures=[{'error':'fixture'}]),
                       lambda r:r.update(schema_version=-1),lambda r:r.update(runtime_details={})):
            with self.subTest(mutate=mutate):
                report=valid_report();mutate(report)
                with self.assertRaises(ValueError):validate_report(report,commit='fixture')
        validate_report(valid_report(),commit='fixture')
        for report in ([],None,'invalid'):
            with self.assertRaises(ValueError):validate_report(report)

    def test_every_missing_skipped_failed_or_timed_out_gate_fails(self):
        for state in ('failure','skipped','cancelled','timed_out','missing'):
            steps={'build':{'outcome':'success'},'verify':{'outcome':state}}
            self.assertFalse(evaluate_gates(steps,['build','verify'])['success'])
        self.assertFalse(evaluate_gates({'build':{'outcome':'success'}},['build','verify'])['success'])
        self.assertTrue(evaluate_gates({'build':{'outcome':'success'},'verify':{'outcome':'success'}},['build','verify'])['success'])

    def test_spec_loads_project_helpers_without_checkout_on_search_path(self):
        root = Path(__file__).resolve().parents[1]
        # Execute the real spec in an isolated subprocess, replacing only the
        # PyInstaller machinery. This checks its entry-point path handling and
        # data declarations without producing a macOS application.
        script = '''
import json, sys, types
from pathlib import Path
hooks = types.ModuleType('PyInstaller.utils.hooks')
hooks.collect_data_files = lambda name: []
hooks.collect_submodules = lambda name: []
hooks.copy_metadata = lambda name: []
sys.modules['PyInstaller'] = types.ModuleType('PyInstaller')
sys.modules['PyInstaller.utils'] = types.ModuleType('PyInstaller.utils')
sys.modules['PyInstaller.utils.hooks'] = hooks
native = types.ModuleType('scripts.windows_native_bundle')
native.assemble_windows_cpp_runtime = lambda binaries,root: binaries
native.collect_wheel_libraries = lambda **kwargs: ([],[],{'success':True,'files':[]})
sys.modules['scripts.windows_native_bundle'] = native
captured = {}
def analysis(scripts, **kwargs):
    captured.update(scripts=scripts, **kwargs)
    return types.SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])
namespace = dict(SPEC=sys.argv[1], Analysis=analysis, PYZ=lambda *a,**k:None,
                 EXE=lambda *a,**k:None, COLLECT=lambda *a,**k:None)
exec(compile(Path(sys.argv[1]).read_bytes(), sys.argv[1], 'exec'), namespace)
print(json.dumps(captured))
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-I', '-c', script, str(root/'tempo.spec')],
                                    cwd=directory, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        declarations = json.loads(result.stdout)
        self.assertTrue(set(tempo_release.RUNTIME_IMPORTS).issubset(declarations['hiddenimports']))
        self.assertTrue({'tempo_logo', 'tempo_activity_icon', 'tempo_onboarding'}.issubset(declarations['hiddenimports']))
        self.assertEqual(declarations['scripts'], [str(root/'tempo_gui.py')])
        self.assertEqual(declarations['pathex'], [str(root)])
        declared = {Path(source).relative_to(root).as_posix(): destination
                    for source, destination in declarations['datas']}
        for name in tempo_cloud.BUNDLE_FILES:
            self.assertIn(name, declared)
            self.assertEqual(declared[name], str(Path(name).parent))
        self.assertEqual(declared['assets'], 'assets')
        for name in ('tempo_icon.png', 'tempo_icon.ico', 'tempo_shell_icon.png', 'tempo_logo_base.png'):
            self.assertTrue((root/'assets'/name).is_file(), name)
        self.assertEqual(declared['docs/TRAINING_WORKFLOW.md'], 'docs')
        for name in ('GETTING_STARTED.md', 'CREDENTIAL_SECURITY.md', 'COLLABORATION_WORKFLOW.md',
                     'WORKSPACE_ADMINISTRATION.md', 'CAPTURE_QUALITY.md'):
            self.assertEqual(declared['docs/' + name], 'docs')
        self.assertEqual(declared['README.md'], '.')
        for source, destination in declarations['datas']:
            self.assertTrue(Path(source).exists(), source)


class RuntimeReportTests(unittest.TestCase):
    def test_diagnostic_prediction_disables_an_already_enabled_analytics_sender(self):
        import importlib
        from types import SimpleNamespace
        analytics = importlib.import_module('ultralytics.utils.events')
        cfg = SimpleNamespace(mode='predict', task='detect', model='synthetic.yaml')
        def predict(*args, **kwargs):
            analytics.events(cfg)
            return [object()]
        model = SimpleNamespace(YOLO=lambda name: SimpleNamespace(predict=predict))
        with patch.object(analytics.events, 'enabled', True), patch.object(analytics.events, 't', 0), \
             patch.object(analytics.events, 'events', []), patch.object(analytics, 'Thread') as transport:
            tempo_release.runtime_operation('ultralytics', model)
            transport.assert_not_called()
            self.assertFalse(analytics.events.events)

    def test_both_verification_entry_points_force_offline_environment(self):
        expected = dict(HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                        YOLO_OFFLINE='true', YOLO_AUTOINSTALL='false')
        observed = []
        def inspect(name, *args):
            observed.append(name)
            for key, value in expected.items():
                self.assertEqual(os.environ.get(key), value)
            return dict(check=name, success=True, details={})
        with tempfile.TemporaryDirectory() as directory:
            for mode in ('suite', 'single'):
                with self.subTest(mode=mode), patch.dict(os.environ, {key:'wrong' for key in expected}), \
                     patch.object(sys, 'platform', 'win32'), \
                     patch('faulthandler.dump_traceback_later'), patch('faulthandler.cancel_dump_traceback_later'):
                    output = Path(directory)/(mode+'.json')
                    if mode == 'suite':
                        with patch.object(tempo_release, 'run_probe', side_effect=inspect), patch('sys.stdout', io.StringIO()):
                            self.assertEqual(tempo_release.verify_installation(output), 0)
                    else:
                        with patch.object(tempo_release, 'probe_action', side_effect=inspect):
                            self.assertEqual(tempo_release.verification_probe('fixture', output), 0)
        self.assertIn('fixture', observed)
        self.assertIn('ultralytics', observed)

    def test_credential_store_probe_cleans_up_and_returns_runtime_evidence(self):
        backend = Mock()
        backend.get_password.side_effect = [
            'synthetic-release-verification', 'synthetic-release-verification', None]
        with patch('tempo_secure_store.native_backend', return_value=backend):
            report = tempo_release.probe_action('keyring')
        self.assertTrue(report['origin'])
        self.assertTrue(report['operation'])
        backend.set_password.assert_called_once()
        service, account, value = backend.set_password.call_args.args
        self.assertTrue(service.startswith('TEMPO/ReleaseVerification/'))
        self.assertEqual(value, 'synthetic-release-verification')
        backend.delete_password.assert_called_once_with(service, account)
        self.assertEqual(backend.get_password.call_count, 3)

    def test_private_wheel_libraries_keep_each_provider_directory_and_detect_loss(self):
        import base64
        import types
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'venv';root.mkdir()
            providers=[]
            for provider in ('pandas','ml_dtypes'):
                folder=root/(provider+'.libs');folder.mkdir()
                files=[]
                for name in ('msvcp140-shared.dll','.load_order'):
                    path=folder/name;path.write_bytes(b'private runtime' if name.endswith('.dll') else b'load order')
                    record=importlib.metadata.PackagePath(path.relative_to(root).as_posix())
                    record.hash=types.SimpleNamespace(mode='sha256',value=base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).decode().rstrip('='))
                    files.append(record)
                providers.append(types.SimpleNamespace(files=files,metadata={'Name':provider},locate_file=lambda record:root/str(record)))
            datas,binaries,audit=collect_wheel_libraries(root,providers)
            self.assertEqual({destination for _,destination in binaries},{'pandas.libs','ml_dtypes.libs'})
            self.assertEqual(len(datas),2)
            bundle=Path(directory)/'bundle';(bundle/'assets').mkdir(parents=True)
            for source,destination in datas+binaries:
                target=bundle/destination/Path(source).name;target.parent.mkdir(exist_ok=True)
                shutil.copyfile(source,target)
            (bundle/'assets/wheel-native-libraries.json').write_text(json.dumps(audit))
            self.assertEqual(tempo_release.verify_wheel_libraries(bundle)['count'],4)
            (bundle/'ml_dtypes.libs/msvcp140-shared.dll').unlink()
            with self.assertRaisesRegex(RuntimeError,'Missing or changed'):
                tempo_release.verify_wheel_libraries(bundle)
            (root/'pandas.libs/msvcp140-shared.dll').write_bytes(b'tampered provider')
            with self.assertRaisesRegex(RuntimeError,'checksum mismatch'):
                collect_wheel_libraries(root,providers)

    def test_dependency_drift_missing_and_added_packages_prevent_packaging(self):
        import types
        expected={'fixture-package':'1'}
        self.assertEqual(check_environment(expected,[types.SimpleNamespace(metadata={'Name':'Fixture_Package'},version='1')]),expected)
        for packages in ([],[('fixture-package','2')],[('fixture-package','1'),('unlocked-package','1')]):
            distributions=[types.SimpleNamespace(metadata={'Name':name},version=version) for name,version in packages]
            with self.assertRaisesRegex(RuntimeError,'locked environment'):
                check_environment(expected,distributions)

    def test_analysis_cpp_override_checks_original_provider_and_venv_boundary(self):
        import base64
        import types
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'Scripts').mkdir();(root/'Qt').mkdir()
            source=root/'Scripts/msvcp140.dll';source.write_bytes(b'pinned runtime')
            destination=root/'Qt/MSVCP140.dll';destination.write_bytes(b'old Qt runtime')
            original=base64.urlsafe_b64encode(hashlib.sha256(destination.read_bytes()).digest()).decode().rstrip('=')
            record=importlib.metadata.PackagePath('Qt/MSVCP140.dll')
            record.hash=types.SimpleNamespace(mode='sha256',value=original)
            distribution=types.SimpleNamespace(files=[record],metadata={'Name':'fixture Qt'},locate_file=lambda _:destination)
            manifest={'package':'fixture','version':'1','files':{'msvcp140.dll':{'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}}}
            report=root/'audit.json'
            result=prepare_build_environment(manifest,report,root,[distribution])
            self.assertTrue(result['success']);self.assertEqual(destination.read_bytes(),source.read_bytes())
            self.assertEqual(len(result['overrides']),1)
            self.assertEqual(prepare_build_environment(manifest,report,root,[distribution])['overrides'],[])
            destination.write_bytes(b'unaccounted change')
            with self.assertRaisesRegex(RuntimeError,'Original C.*checksum mismatch'):
                prepare_build_environment(manifest,report,root,[distribution])
            self.assertFalse(json.loads(report.read_text())['success'])
            outside=root.parent/(root.name+'-outside.dll')
            distribution.locate_file=lambda _:outside
            with self.assertRaisesRegex(RuntimeError,'escape the build venv'):
                prepare_build_environment(manifest,report,root,[distribution])
            self.assertFalse(outside.exists())

    def test_cache_respects_external_profile_without_creating_bundle_directories(self):
        from tempo_runtime import configure_matplotlib_cache
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {'MPLCONFIGDIR': str(root/'external cache')}):
                self.assertEqual(configure_matplotlib_cache(), str(root/'external cache'))
            self.assertTrue((root/'external cache').is_dir())
            with patch.dict(os.environ, {'LOCALAPPDATA': str(root)}, clear=True), patch.object(sys,'platform','win32'), \
                 patch.object(Path,'home',side_effect=RuntimeError('Home directory is unavailable')):
                self.assertEqual(Path(configure_matplotlib_cache()), root/'TEMPO/matplotlib')

    def test_conflicting_cpp_copies_use_one_pinned_provider_and_corruption_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'Scripts').mkdir()
            dll=root/'Scripts/msvcp140.dll';dll.write_bytes(b'new runtime')
            manifest={'package':'fixture','version':'1','files':{'msvcp140.dll':{'sha256':hashlib.sha256(dll.read_bytes()).hexdigest()}}}
            binaries=[('PyQt6/Qt6/bin/MSVCP140.dll','old Qt runtime','BINARY'),
                      ('numpy.libs/msvcp140-hashed.dll','private hashed runtime','BINARY')]
            result,audit=canonicalize_vc_runtime(binaries,manifest,root)
            self.assertEqual(result[0][1],str(dll))
            self.assertIn(('msvcp140.dll',str(dll),'BINARY'),result)
            self.assertEqual(result[1],binaries[1]);self.assertEqual(len(audit['replacements']),1)
            with self.assertRaisesRegex(RuntimeError,'no valid Microsoft signature'):
                canonicalize_vc_runtime(binaries,manifest,root,verify_signature=lambda _: {'status':'NotSigned'})
            dll.write_bytes(b'corrupt')
            with self.assertRaisesRegex(RuntimeError,'checksum mismatch'):
                canonicalize_vc_runtime(binaries,manifest,root)
            dll.unlink()
            with self.assertRaisesRegex(RuntimeError,'Missing pinned'):
                canonicalize_vc_runtime(binaries,manifest,root)

    def test_bundled_cpp_inventory_rejects_missing_or_conflicting_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'assets').mkdir();(root/'Qt').mkdir()
            dll=root/'msvcp140.dll';dll.write_bytes(b'pinned')
            manifest={'version':'fixture','files':{'msvcp140.dll':{'sha256':hashlib.sha256(dll.read_bytes()).hexdigest()}}}
            (root/'assets/windows-native-runtime.json').write_text(json.dumps(manifest))
            self.assertEqual(tempo_release.verify_bundled_cpp_runtime(root)['copies']['msvcp140.dll'],['msvcp140.dll'])
            (root/'Qt/msvcp140.dll').write_bytes(b'old Qt copy')
            with self.assertRaisesRegex(RuntimeError,'Conflicting bundled'):
                tempo_release.verify_bundled_cpp_runtime(root)
            (root/'Qt/msvcp140.dll').unlink();dll.unlink()
            with self.assertRaisesRegex(RuntimeError,'Missing bundled'):
                tempo_release.verify_bundled_cpp_runtime(root)

    def test_only_signed_microsoft_defender_component_is_allowed_outside_bundle(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'ProgramData':directory,'SystemRoot':directory}):
            defender=Path(directory)/'Microsoft/Windows Defender/Platform/4.18.26080.4-0/MpOAV.dll'
            signature={'status':'Valid','subject':'CN=Microsoft Windows, O=Microsoft Corporation, C=US',
                       'thumbprint':'fixture','sha256':'fixture','version':'4.18.26080.4'}
            def completed(value):return subprocess.CompletedProcess([],0,json.dumps(value),'')
            with patch('subprocess.run',return_value=completed(signature)) as run:
                self.assertEqual(tempo_release.verify_system_security_library(defender),signature)
                self.assertEqual(run.call_args.kwargs['timeout'],30)
                with self.assertRaisesRegex(RuntimeError,'outside the bundle'):
                    tempo_release.verify_system_security_library(Path(directory)/'unknown.dll')
                self.assertEqual(run.call_count,1)
            for value in ({**signature,'status':'HashMismatch'}, {**signature,'subject':'O=Other Publisher'}, {**signature,'thumbprint':''}):
                with patch('subprocess.run',return_value=completed(value)), self.assertRaisesRegex(RuntimeError,'Untrusted Windows Defender'):
                    tempo_release.verify_system_security_library(defender)

    def test_signature_command_failure_retains_windows_error(self):
        completed = subprocess.CompletedProcess([],1,'','fixture native shell error')
        with patch.dict(os.environ,{'SystemRoot':'C:/Windows','PSMODULEPATH':'incompatible PowerShell 7 modules'}), patch('subprocess.run',return_value=completed) as run:
            with self.assertRaisesRegex(RuntimeError,'fixture native shell error'):
                tempo_release.windows_signature(Path('runtime with spaces.dll'))
            environment = run.call_args.kwargs['env']
            self.assertEqual([key for key in environment if key.upper() == 'PSMODULEPATH'],['PSModulePath'])
            self.assertEqual(environment['PSModulePath'],str(Path('C:/Windows/System32/WindowsPowerShell/v1.0/Modules')))

    def test_windowed_probe_without_standard_streams_retains_diagnostics(self):
        # Real probes run separately from the GUI and its native signal handlers.
        script = """import sys
from pathlib import Path
from unittest.mock import patch
import tempo_release
def action(name):
    print('windowed probe diagnostic')
    return {'operation':'fixture'}
with patch.object(sys,'platform','win32'), patch.object(sys,'stdout',None), patch.object(sys,'stderr',None), patch.object(tempo_release,'probe_action',side_effect=action):
    code=tempo_release.verification_probe('fixture',Path(sys.argv[1]))
raise SystemExit(code)
"""
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/'probe.json'
            result=subprocess.run([sys.executable,'-c',script,str(report)],capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,report.read_text() if report.exists() else result.stderr)
            self.assertIn('windowed probe diagnostic',report.with_suffix('.python.log').read_text())
            self.assertTrue(json.loads(report.read_text())['success'])

    def test_reports_multiple_native_failures_and_continues_remaining_probes(self):
        def probe(name,destination):
            return dict(check=name,success=name not in ('torch','tensorflow'),error='fixture DLL crash',details={'origin':'fixture'})
        with tempfile.TemporaryDirectory() as directory, patch.object(tempo_release.sys,'platform','win32'), patch.object(tempo_release,'run_probe',side_effect=probe) as run:
            report=Path(directory)/'report.json'
            self.assertEqual(tempo_release.verify_installation(report),1)
            self.assertEqual(run.call_count,len(tempo_release.REQUIRED_CHECKS)+4)
            self.assertEqual({call.args[0] for call in run.call_args_list},set(tempo_release.REQUIRED_CHECKS))
            result=json.loads(report.read_text())
            self.assertEqual([failure['check'] for failure in result['failures']],['torch','tensorflow'])
            self.assertIn('offscreen plot resize and disposal',result['checks'])

    def test_native_crash_is_reported_and_next_real_subprocess_can_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fake=root/'tempo_gui.py'
            fake.write_text("import json,os,sys\nif sys.argv[2]=='crash': os._exit(139)\njson.dump(dict(check=sys.argv[2],success=True,details={}),open(sys.argv[3],'w'))\n")
            with patch.object(tempo_release,'__file__',str(root/'tempo_release.py')):
                failed=tempo_release.run_probe('crash',root/'failed.json')
                passed=tempo_release.run_probe('next',root/'passed.json')
            self.assertFalse(failed['success']);self.assertIn('139',failed['error'])
            self.assertTrue(passed['success'])

    def test_missing_bundled_payload_fails(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tempo_release.sys,'_MEIPASS',directory,create=True), patch.object(tempo_cloud,'BUNDLE_FILES',('missing.py',)):
            with self.assertRaisesRegex(RuntimeError,'Missing bundled cloud source'):
                tempo_release.probe_action('bundled payload')

    def test_scientific_processing_and_export_uses_real_runtime(self):
        result=tempo_release.scientific_processing_export()
        self.assertGreater(result['beats'],0);self.assertGreater(result['export_rows'],0)


if __name__ == '__main__':
    unittest.main()
