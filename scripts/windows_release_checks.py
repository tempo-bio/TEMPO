"""Bounded CI processes, frozen report validation, and release provenance."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import threading


def stop_process_tree(process):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=30)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_logged(command, log_path, timeout, *, cwd=None, env=None):
    """Stream diagnostics while retaining them, including native crashes/timeouts."""
    log_path = Path(log_path).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    env = dict(os.environ if env is None else env)
    env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8:backslashreplace')
    print('Running:', subprocess.list2cmdline([str(arg) for arg in command]), flush=True)
    with log_path.open('w', encoding='utf-8') as log:
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True,
                                       encoding='utf-8', errors='replace', start_new_session=os.name != 'nt')
        except OSError as error:
            log.write(f'{type(error).__name__}: {error}\n')
            print(str(error), flush=True)
            return 1
        output_errors = []
        def copy_output():
            for line in process.stdout:
                try:
                    log.write(line)
                    log.flush()
                except OSError as error:
                    output_errors.append(str(error))
                try:
                    print(line, end='', flush=True)
                except (UnicodeError, OSError, ValueError) as error:
                    # Never abandon the pipe: an abandoned reader can deadlock
                    # the child once its output buffer fills.
                    if not output_errors: output_errors.append(str(error))
        reader = threading.Thread(target=copy_output, daemon=True)
        reader.start()
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            stop_process_tree(process)
            process.wait(timeout=30)
            log.write(f'\nTimeoutExpired: {error}\n')
            print(f'TimeoutExpired: {error}', flush=True)
            code = 1
        reader.join(timeout=30)
        if reader.is_alive():
            stop_process_tree(process)
            reader.join(timeout=5)
            code = 1
        process.stdout.close()
        if output_errors:
            log.write('\nOutput forwarding failed: '+repr(output_errors)+'\n')
            code = 1
    return code


def isolated_environment(root):
    env = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
        env.pop(key, None)
    for key, location in {
        'LOCALAPPDATA': root/'AppData/Local', 'APPDATA': root/'AppData/Roaming',
        'USERPROFILE': root/'profile', 'MPLCONFIGDIR': root/'matplotlib',
        'HF_HOME': root/'huggingface', 'YOLO_CONFIG_DIR': root/'ultralytics',
        'TORCH_HOME': root/'torch', 'KERAS_HOME': root/'keras', 'CELLPOSE_LOCAL_MODELS_PATH': root/'cellpose',
        'NUMBA_CACHE_DIR': root/'numba',
    }.items():
        location.mkdir(parents=True, exist_ok=True)
        env[key] = str(location)
    if os.name == 'nt':
        # os.environ is case insensitive on Windows, but its copied dict is not.
        windows = Path(env.get('SYSTEMROOT') or env['SystemRoot'])
        env['PATH'] = os.pathsep.join(str(windows/p) for p in ('System32', '', 'System32/Wbem'))
    env.update(QT_QPA_PLATFORM='offscreen', MPLBACKEND='Agg', QT_API='pyqt6',
               HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1', PYTHONUNBUFFERED='1',
               PYTHONUTF8='1', PYTHONIOENCODING='utf-8:backslashreplace')
    return env


def validate_report(report, *, frozen=True, commit=None):
    from tempo_release import REQUIRED_CHECKS, REPORT_SCHEMA
    if not isinstance(report, dict) or report.get('schema_version') != REPORT_SCHEMA:
        raise ValueError('Missing or unsupported verification schema.')
    if report.get('frozen') is not frozen or report.get('success') is not True:
        raise ValueError('Verification must report the expected frozen state and success=true.')
    if report.get('failures') or set(report.get('required_checks', ())) != set(REQUIRED_CHECKS) or len(report.get('required_checks', ())) != len(REQUIRED_CHECKS):
        raise ValueError('Failed or changed required verification checks.')
    if set(report.get('checks', ())) != set(REQUIRED_CHECKS) or len(report.get('checks', ())) != len(REQUIRED_CHECKS):
        raise ValueError('Incomplete verification checks.')
    if commit and report.get('commit') != commit:
        raise ValueError('Verification commit does not match the built revision.')
    if not report.get('commit') or not report.get('runtime_details'):
        raise ValueError('Verification identity/runtime evidence is missing.')
    if set(report['runtime_details']) != set(REQUIRED_CHECKS):
        raise ValueError('Required runtime evidence is incomplete.')


def verify_executable(executable, report_path, timeout=900):
    executable = Path(executable).resolve()
    report_path = Path(report_path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='TEMPO verification ') as directory:
        root = Path(directory)
        code = run_logged([str(executable), '--verify-installation', str(report_path)],
                          report_path.with_suffix('.log'), timeout, cwd=root, env=isolated_environment(root))
    try:
        report = json.loads(report_path.read_text(encoding='utf-8'))
        print(json.dumps(report, indent=2), flush=True)
        validate_report(report, commit=os.environ.get('GITHUB_SHA'))
    except (OSError, ValueError, TypeError) as error:
        print(f'Executable verification failed: {error}', flush=True)
        return code or 1
    return code


def run_regressions(output):
    output = Path(output)
    checks = [('regressions', ['-m', 'scripts.windows_test_suite', '--output', str(output/'unittest-results.json')], 900)]
    results = {name: run_logged([sys.executable, *command], output/f'{name}.log', timeout)
               for name, command, timeout in checks}
    # Discovery now includes these tests. Run them separately only when the
    # full suite fails, retaining independent diagnostics after an early crash.
    if results['regressions']:
        for name, module in (('kinetics', 'tests.test_kinetics_validity'),
                             ('innovations', 'tests.test_model_components')):
            results[name] = run_logged([sys.executable, '-m', module], output/f'{name}.log', 180)
    (output/'regression-results.json').write_text(json.dumps(results, indent=2)+'\n', encoding='utf-8')
    return 1 if any(results.values()) else 0


def provenance(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    packages = {d.metadata['Name'].lower().replace('_', '-'): d.version for d in importlib.metadata.distributions()}
    data = dict(commit=os.environ.get('GITHUB_SHA') or subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
        python=platform.python_version(), architecture=platform.machine(), platform=platform.platform(),
        runner_image=os.environ.get('ImageOS'), runner_image_version=os.environ.get('ImageVersion'),
        lock_sha256=hashlib.sha256((root/'requirements-windows.lock').read_bytes()).hexdigest(), packages=packages,
        project_license_sha256=hashlib.sha256((root/'LICENSE').read_bytes()).hexdigest(),
        dependency_sources_sha256=hashlib.sha256((root/'installer/dependency-sources.json').read_bytes()).hexdigest(),
        compiler=dict(version='7.1.0', sha256='0362a383ed217d4c4239b5933866dd96d3eb2102737da92f80f6057a4b40df2f'))
    # Ultralytics and Cellpose require different distributions of the cv2
    # namespace. Record which exact native file won their shared installation.
    import base64
    providers = [importlib.metadata.distribution(name) for name in ('opencv-python','opencv-python-headless')]
    if len({provider.version for provider in providers}) != 1:
        raise RuntimeError('OpenCV namespace providers have conflicting versions.')
    native = next(file for file in providers[1].files
                  if str(file).startswith('cv2/') and str(file).endswith('.pyd'))
    path = providers[1].locate_file(native)
    digest = hashlib.sha256(path.read_bytes()).digest()
    expected_hash = base64.urlsafe_b64encode(digest).decode().rstrip('=')
    matches = [provider.metadata['Name'] for provider in providers
               if any(file == native and file.hash and file.hash.mode == 'sha256'
                      and file.hash.value == expected_hash for file in provider.files)]
    if not matches:
        raise RuntimeError('Installed cv2 native file matches neither locked provider.')
    data['shared_namespace_audit'] = {'cv2': {'providers': [provider.metadata['Name'] for provider in providers],
        'version': providers[0].version, 'native_file': str(path), 'sha256': digest.hex(),
        'matching_distribution_records': matches}}
    (output/'build-provenance.json').write_text(json.dumps(data, indent=2)+'\n', encoding='utf-8')
    from tempo_release import REPORT_SCHEMA, REQUIRED_CHECKS
    (output/'verification-contract.json').write_text(json.dumps(dict(
        schema_version=REPORT_SCHEMA, commit=data['commit'], lock_sha256=data['lock_sha256'],
        required_checks=list(REQUIRED_CHECKS)), indent=2)+'\n', encoding='utf-8')
    (output/'pip-freeze.txt').write_text(subprocess.check_output([sys.executable, '-m', 'pip', 'freeze', '--all'], text=True), encoding='utf-8')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    for name in ('regressions', 'provenance'):
        mode = modes.add_parser(name)
        mode.add_argument('--output', default='Output')
    run = modes.add_parser('run')
    run.add_argument('--log', required=True)
    run.add_argument('--timeout', type=float, required=True)
    run.add_argument('command', nargs=argparse.REMAINDER)
    verify = modes.add_parser('verify')
    verify.add_argument('--executable', required=True)
    verify.add_argument('--report', required=True)
    verify.add_argument('--timeout', type=float, default=900)
    args = parser.parse_args()
    if args.mode == 'regressions': return run_regressions(args.output)
    if args.mode == 'provenance': return provenance(args.output)
    if args.mode == 'verify': return verify_executable(args.executable, args.report, args.timeout)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command: parser.error('run requires a command after --')
    return run_logged(command, args.log, args.timeout)


if __name__ == '__main__':
    raise SystemExit(main())
