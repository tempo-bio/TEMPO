"""Cloud-only orchestration invoked by the maintained Colab notebook."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import signal
import sys
import threading
import time
import hashlib
from tempo_training_groups import SPLIT_POLICY, validate_assignments
from tempo_cloud import sync_retry_delay

from notebooks.colab_runtime import run_logged, check_training_environment, check_training_steps, configure_cuda_environment
from tempo_progress import snapshots


def sync_error_details(error):
    """Keep useful server diagnostics without exposing notebook credentials."""
    from tempo_credentials import redact_credentials
    response = getattr(error, 'response', None)
    code = getattr(response, 'status_code', None)
    description = f'HTTP {code}; ' if code is not None else ''
    message = redact_credentials(str(error), os.environ.get('HF_TOKEN'))
    return f'{description}{type(error).__name__}: {message[:2000]}'


def watch_training_status(*, read_control, upload_status, cancelled, done,status_interval=60):
    """Retry monitoring separately so failed status writes cannot skip cancellation."""
    failures = {'control': 0, 'status': 0}
    due = {'control': 0.0, 'status': 0.0}
    while not done.wait(10):
        for operation in ('control', 'status'):
            if done.is_set():
                return
            if time.monotonic() < due[operation]:
                continue
            try:
                if operation == 'control':
                    if read_control().get('cancel'):
                        cancelled.set()
                else:
                    upload_status()
            except Exception as error:
                failures[operation] += 1
                delay = sync_retry_delay(error, failures[operation])
                due[operation] = time.monotonic() + delay
                action = 'Cancellation check' if operation == 'control' else 'Status upload'
                consequence = ('Use Colab to stop execution while cancellation checks are unavailable.'
                               if operation == 'control' else 'Training continues in Colab; TEMPO progress may be delayed.')
                print(f'{action} failed ({sync_error_details(error)}). '
                      f'Retrying in {delay:.0f}s. {consequence}', flush=True)
            else:
                if failures[operation]:
                    action = 'Cancellation checks' if operation == 'control' else 'Status uploads'
                    print(f'{action} resumed.', flush=True)
                failures[operation] = 0
                due[operation] = time.monotonic()+status_interval if operation=='status' else 0.0


def require_colab():
    if not Path('/content').is_dir() or sys.platform != 'linux':
        raise RuntimeError('This runner requires a Colab Linux GPU runtime. Local training is disabled.')


def load_context(root=None):
    """Use only the repositories and sources named by the queued request."""
    require_colab()
    from tempo_hub import tempo_hub as hub
    from tempo_config import tempo_preferences
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    request = json.loads((root/'cloud_request.json').read_text())
    for key, value in [('hf_dataset_repo', request['dataset_repo']), ('hf_models_repo', request['models_repo'])]:
        tempo_preferences.set('collaboration', key, value)
    token = (os.environ.get('HF_TOKEN') or '').strip()
    if not token:
        raise RuntimeError('Enable the HF_TOKEN Colab secret before connecting.')
    hub.token = token
    return root, request, hub


def approved_snapshot(hub, request):
    from tempo_training import select_approved_annotations
    from tempo_nuclei import migrate_record
    master = select_approved_annotations({name:migrate_record(record) for name,record in hub._read('universal_annotations.json', request['dataset_revision']).items()})
    images = request['images']
    if (len(master) != request['image_count'] or len(images) != len(master)
            or {row['name'] for row in images} != set(master)
            or any(row['annotations'] != len(master[row['name']]['boxes']) for row in images)):
        raise ValueError('Approved annotations do not match the recorded dataset version.')
    from tempo_nuclei import attach_supplement
    nuclear = hub._read('nuclear_annotations.json',request['dataset_revision'],{})
    return {name:attach_supplement(record,nuclear.get(record.get('image_id'))) for name,record in master.items()}


def request_partitions(request):
    names = [row['name'] for row in request['images']]
    groups = request.get('source_groups', {})
    assignments = request.get('split_assignments')
    if request.get('split_policy') != SPLIT_POLICY:
        raise ValueError('Run the maintained notebook to create current image assignments.')
    assignments = validate_assignments(names, assignments)
    return groups, assignments


def require_pending(hub, request):
    revision = hub._head()
    if hub._read('training/request.json', revision) != request:
        raise ValueError('The active run changed. Check its status before running the notebook again.')
    if hub._read(f"training/{request['id']}/control.json", revision, {}).get('cancel'):
        raise InterruptedError('This run was cancelled. Run the notebook again for another run.')
    if hub._read(f"training/{request['id']}/status.json", revision).get('phase') != 'awaiting_colab':
        raise ValueError('This request is running or finished. Inspect its status before starting another run.')


def prepare_dataset(root, request, hub, *, training_check=False, release=None, cancelled=None):
    from tempo_training import build_dataset
    master = approved_snapshot(hub, request)
    groups, assignments = request_partitions(request)
    (root/'datasets').mkdir(exist_ok=True)
    (root/'datasets/universal_annotations.json').write_text(json.dumps(master))
    release = release if release is not None else hub.model_manifest()
    receipt=release.get('training_recipe')
    protected={}
    if release.get('revision'):
        pinned=hub._read('model_manifest.json',release['revision'],{},repo_type='model')
        if receipt is None:
            receipt=pinned.get('training_recipe')
        protected=hub._read('training/architecture_validation.json',release['revision'],{},repo_type='model')
    configured=hub._read('training/architecture_validation.json',request['dataset_revision'],{}) or hub._read('architecture_validation.json',request['dataset_revision'],{})
    if not isinstance(configured,dict):
        raise ValueError('The shared training recipe is invalid.')
    if protected:
        from tempo_evaluation import checksum
        # Evaluation progress changes Git HEAD without changing published weights.
        # Only a protected receipt can use this revision-independent identity.
        if protected.get('base_models_sha256')==checksum(release['models']):
            receipt=protected
        elif receipt is None:
            receipt={}
    elif receipt is None or configured.get('base_model_revision')==release.get('revision') and configured.get('base_model_revision'):
        receipt=configured
    if not isinstance(receipt,dict):
        raise ValueError('The published training recipe is invalid.')
    (root/'datasets/architecture_validation.json').write_text(json.dumps(receipt))
    for name in master:
        if cancelled is not None and cancelled():
            raise InterruptedError('Cancelled during preparation.')
        if not training_check or assignments[name] == 'train':
            hub.download_image(name, revision=request['dataset_revision'])
    hub.download_model_release(release, root)
    (root/'runs').mkdir(exist_ok=True)
    (root/'runs/champion_baselines.json').write_text(json.dumps({name: record['sha256'] for name, record in release['models'].items()}))
    return build_dataset(root, source_groups=groups, split_assignments=assignments, training_check=training_check)


def dataset_digest(dataset):
    from tempo_hub import TempoHub
    hashes = {path.relative_to(dataset).as_posix(): TempoHub.calculate_sha256(path)
              for path in sorted(Path(dataset).rglob('*')) if path.is_file() and path.suffix != '.cache'}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def verify_run_inputs(root, request, dataset=None, expected_digest=None):
    from tempo_cloud import archive_sources
    if hashlib.sha256(archive_sources(root)).hexdigest() != request['bundle_sha256']:
        raise ValueError('Training code changed during this run.')
    if json.loads((root/'cloud_request.json').read_text()) != request:
        raise ValueError('Run settings changed during execution.')
    if dataset is not None and dataset_digest(dataset) != expected_digest:
        raise ValueError('Compiled images or annotations changed during training.')


def check_training(root=None):
    """Exercise each real worker without claiming a run or publishing models."""
    root, request, hub = load_context(root)
    os.environ.update(configure_cuda_environment(sys.executable))
    require_pending(hub, request)
    (root/'runs/training_check.json').unlink(missing_ok=True)
    release = hub.model_manifest()
    dataset = prepare_dataset(root, request, hub, training_check=True, release=release)
    def cancelled():
        revision = hub._head()
        return bool(hub._read(f"training/{request['id']}/control.json", revision, {}).get('cancel'))
    report = check_training_steps(sys.executable, root, cancelled=cancelled, dataset=dataset, training_check=True)
    from tempo_training import MODEL_FILES
    if not report.get('success') or set(report.get('results', {})) != set(MODEL_FILES):
        raise RuntimeError('Training checks did not finish successfully.')
    require_pending(hub, request)
    groups, assignments = request_partitions(request)
    report.update(request_id=request['id'], dataset_revision=request['dataset_revision'],
                  bundle_sha256=request['bundle_sha256'], source_groups=groups, split_assignments=assignments,
                  baseline_revision=release['revision'], dataset_digest=dataset_digest(dataset), publishable=False)
    (root/'runs').mkdir(exist_ok=True)
    (root/'runs/training_check.json').write_text(json.dumps(report, indent=2))
    print('Training checks passed. Models were not published.', flush=True)
    print('Diagnostic scores use training images; they do not measure performance on unseen wells.', flush=True)
    return report


def verify_training_check(root, request, release):
    from tempo_training import MODEL_FILES
    from tempo_hub import TempoHub
    try:
        report = json.loads((root/'runs/training_check.json').read_text())
        groups, assignments = request_partitions(request)
        expected = dict(request_id=request['id'], dataset_revision=request['dataset_revision'],
                        bundle_sha256=request['bundle_sha256'], source_groups=groups,
                        split_assignments=assignments, baseline_revision=release['revision'])
        if (not report.get('success') or report.get('publishable') is not False
                or any(report.get(key) != value for key, value in expected.items())
                or set(report.get('results', {})) != set(MODEL_FILES)
                or report.get('dataset_digest') != dataset_digest(root/'datasets/compiled-check')):
            raise ValueError('Training check does not match this run, its data, or the current published models.')
        for result in report['results'].values():
            artifact = Path(result['artifact']).resolve()
            if root.resolve() not in artifact.parents or TempoHub.calculate_sha256(artifact) != result['sha256']:
                raise ValueError('A training-check checkpoint changed or is missing.')
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError('Run Check Training successfully for this request before Train and Publish.') from error
    return report


def inspect_request(root=None):
    """Read-only preview; no claim, training, publication or approval prompt."""
    root, request, hub = load_context(root)
    require_pending(hub, request)
    groups, assignments = request_partitions(request)
    master = approved_snapshot(hub, request)
    release = hub.model_manifest()
    preview = dict(request_id=request['id'], dataset_revision=request['dataset_revision'],
                   image_count=len(master), annotations=sum(len(row['boxes']) for row in master.values()),
                   epochs=request['epochs'], gpu=request['gpu'], models_repo=request['models_repo'],
                   baseline_revision=release['revision'], baseline_models=release['models'])
    preview.update(partitions={partition: sum(value == partition for value in assignments.values())
                               for partition in ('train', 'val', 'test')})
    (root/'runs').mkdir(exist_ok=True)
    (root/'runs/request_preview.json').write_text(json.dumps(preview, indent=2))
    print(json.dumps(preview, indent=2), flush=True)
    return preview


def summarize_release(root=None):
    """Verify archived trained weights and the current models at the saved revision."""
    from huggingface_hub import hf_hub_download
    from tempo_training import MODEL_FILES
    root, request, hub = load_context(root)
    report = json.loads((root/'runs/training_report.json').read_text())
    if (not report.get('success') or set(report.get('results', {})) != set(MODEL_FILES)
            or report.get('cloud_request_id') != request['id']
            or report.get('dataset_revision') != request['dataset_revision']
            or report.get('bundle_sha256') != request['bundle_sha256']):
        raise ValueError('No complete training report belongs to this run.')
    status = hub._read(f"training/{request['id']}/status.json", hub._head())
    if status.get('phase') != 'completed' or status.get('release_id') != report['run_id']:
        raise ValueError('Run completion has not been confirmed. Inspect its status before retrying.')
    revision = report['publication_revision']
    archive = hub._read(report['archive_prefix']+'/report.json', revision, repo_type='model')
    if archive['model_outcomes'] != report['model_outcomes'] or archive['results'] != report['results']:
        raise ValueError('Saved run results differ from the verified local results.')
    manifest = hub._read('model_manifest.json', revision, repo_type='model')
    if manifest['models'] != report['current_models']:
        raise ValueError('Current model records do not match this run.')
    summary = dict(request_id=request['id'], release_id=report['run_id'], dataset_revision=request['dataset_revision'],
                   remote_revision=revision, updated_count=report['updated_count'],
                   evaluation_basis=report.get('evaluation_basis', 'held-out-images'), models={})
    for model, name in MODEL_FILES.items():
        result = report['results'][model]
        artifact = Path(result['artifact']).resolve()
        if root.resolve() not in artifact.parents or hub.calculate_sha256(artifact) != result['sha256']:
            raise ValueError(f'{model}: local trained weights changed or are outside this run.')
        remote_checks=[(report['archive_prefix']+'/weights/'+name,result['sha256'])]
        if name in report['current_models']:
            remote_checks.append((name,report['current_models'][name]['sha256']))
        for remote_name,expected in remote_checks:
            remote = hf_hub_download(repo_id=request['models_repo'], filename=remote_name, repo_type='model',
                                     revision=revision, token=os.environ['HF_TOKEN'])
            if hub.calculate_sha256(remote) != expected:
                raise ValueError(f'{model}: remote model checksum mismatch.')
        summary['models'][model] = dict(report['model_outcomes'][model], artifact=str(artifact),
                                       sha256=result['sha256'], local_verified=True, remote_verified=True)
        if result.get('training_details'):
            summary['models'][model]['training_details'] = result['training_details']
    (root/'runs/release_summary.json').write_text(json.dumps(summary, indent=2))
    print(f"Run verified. {summary['updated_count']} current models updated.", flush=True)
    return summary


def create_run(root=None):
    """Colab owns settings, approved-data selection and run registration."""
    require_colab()
    from tempo_hub import tempo_hub as hub
    from tempo_config import tempo_preferences
    from tempo_cloud import prepare_request, queue_request
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    settings = json.loads((root/'notebook_settings.json').read_text())
    if (root/'cloud_request.json').exists():
        raise ValueError('This execution already registered a run. Inspect its status; choose Run all for another run.')
    token = (os.environ.get('HF_TOKEN') or '').strip()
    if not token:
        raise RuntimeError('Enable access to the HF_TOKEN Colab secret.')
    for key in ('dataset', 'models'):
        tempo_preferences.set('collaboration', f'hf_{key}_repo', settings[f'{key}_repo'])
    hub.token = token
    if settings.get('evaluation_request'):
        from tempo_evaluation_runner import run_prepared_evaluation
        return run_prepared_evaluation(root,settings,hub)
    request, bundle = prepare_request(hub, root, settings['epochs'], settings['gpu'])
    if request['bundle_sha256'] != settings['bundle_sha256']:
        raise ValueError('Training code changed. Start again from the maintained notebook.')
    if root.parent.name == settings.get('run_id'):
        request['id'] = settings['run_id']
    (root/'cloud_request.json').write_text(json.dumps(request, indent=2))
    queue_request(hub, request, bundle)
    print(f"Approved images: {request['image_count']}. Maximum epochs: {request['epochs']}.", flush=True)
    for partition in ('train', 'val', 'test'):
        label = {'train':'Training', 'val':'Validation', 'test':'Comparison'}[partition]
        print(f"{label} images: {sum(value == partition for value in request['split_assignments'].values())}", flush=True)
    run(root, preflight=True)
    return summarize_release(root)


def run(root=None, preflight=True, require_check=False):
    from huggingface_hub import HfApi, hf_hub_download, CommitOperationAdd
    from tempo_hub import commit_and_confirm
    root, request, hub = load_context(root)
    groups, assignments = request_partitions(request)
    verify_run_inputs(root, request)
    release = (dict(revision=request['baseline_revision'], models=request['baseline_models'])
               if 'baseline_revision' in request else hub.model_manifest())
    if require_check:
        verify_training_check(root, request, release)
    os.environ.update(configure_cuda_environment(sys.executable))
    api = HfApi(token=os.environ['HF_TOKEN'])
    prefix = f"training/{request['id']}"
    gpu = subprocess.check_output(['nvidia-smi','--query-gpu=name','--format=csv,noheader'], text=True).strip()
    if request['gpu'] == 'A100' and 'A100' not in gpu:
        raise RuntimeError(f'A100 requested; assigned {gpu}. Training has not started.')
    def claim(manifest, revision):
        if hub._read('training/request.json', revision) != request:
            raise ValueError('The active run changed. Check its status before starting another.')
        if hub._read(f'{prefix}/control.json', revision, {}).get('cancel'):
            raise InterruptedError('The queued request was cancelled before training.')
        current = hub._read(f'{prefix}/status.json', revision)
        if current.get('phase') != 'awaiting_colab':
            raise RuntimeError('This run is already running or finished. Check its status before starting another.')
        current.update(phase='preparing', gpu=gpu, updated_at=time.time())
        return {f'{prefix}/status.json':json.dumps(current).encode()}, current
    from tempo_cloud import training_transaction
    state = training_transaction(hub, claim, 'Start Colab training run')
    started = time.time()
    cancelled, done = threading.Event(), threading.Event()
    def interrupt(signum, frame):
        cancelled.set()
        raise InterruptedError('Colab stopped the cloud run.')
    previous_term = signal.signal(signal.SIGTERM, interrupt)
    state_lock = threading.Lock()
    upload_lock = threading.Lock()
    progress_dir = root/'runs/progress'
    os.environ['TEMPO_PROGRESS_DIR'] = str(progress_dir)
    def upload_status():
        with upload_lock:
            with state_lock:
                value = dict(state, elapsed_seconds=int(time.time()-started), updated_at=time.time(), models=snapshots(progress_dir))
            commit_and_confirm(api, repo_id=request['dataset_repo'], repo_type='dataset',
                               token=os.environ['HF_TOKEN'],
                               operations=[CommitOperationAdd(path_in_repo=f'{prefix}/status.json', path_or_fileobj=json.dumps(value).encode())],
                               commit_message='Update cloud training status', read_json=hub._read)
    watcher = threading.Thread(target=watch_training_status, kwargs=dict(
        read_control=lambda: hub._read(f'{prefix}/control.json', hub._head(), {}),
        upload_status=upload_status, cancelled=cancelled, done=done), daemon=True)
    watcher.start()
    def phase(value):
        with state_lock:
            state['phase'] = value
        print(f'CLOUD PHASE: {value}; GPU: {gpu}', flush=True)
    try:
        phase('checking_runtime')
        check_training_environment(sys.executable, root, cancelled=cancelled.is_set)
        phase('preparing_dataset')
        from tempo_training import MODEL_FILES, publish_champions
        phase('preparing_baselines')
        dataset = prepare_dataset(root, request, hub, release=release, cancelled=cancelled.is_set)
        compiled_digest = dataset_digest(dataset)
        if preflight and request['epochs'] > 1:
            phase('checking_training_steps')
            trial_data = prepare_dataset(root, request, hub, training_check=True, release=release, cancelled=cancelled.is_set)
            check_training_steps(sys.executable, root, cancelled=cancelled.is_set, dataset=trial_data, training_check=True)
            for path in progress_dir.glob('*.json'):
                path.unlink()
        elif preflight:
            print('Training checks included in the requested one-epoch run.', flush=True)
        phase('training')
        verify_run_inputs(root, request, dataset, compiled_digest)
        run_logged([sys.executable, '-u', str(root/'train_yolo.py'), '--epochs', str(request['epochs']),
                    '--workers','0','--sequential','--dataset',str(dataset)], label='Train and validate four models',
                   cwd=root, log_path=root/'runs/training.log', cancelled=cancelled.is_set)
        control = hub._read(f'{prefix}/control.json', hub._head(), {})
        if cancelled.is_set() or control.get('cancel'):
            raise InterruptedError('Cancelled before publication.')
        verify_run_inputs(root, request, dataset, compiled_digest)
        phase('publishing')
        report = json.loads((root/'runs/training_report.json').read_text())
        report.update(cloud_request_id=request['id'], dataset_revision=request['dataset_revision'],
                      bundle_sha256=request['bundle_sha256'])
        (root/'runs/training_report.json').write_text(json.dumps(report, indent=2))
        count = publish_champions(root, request['models_repo'], os.environ['HF_TOKEN'])
        report = json.loads((root/'runs/training_report.json').read_text())
        with state_lock:
            state.update(phase='completed', release_id=report['run_id'],
                         metrics={m:r['metrics'] for m,r in report['results'].items()},
                         model_outcomes=report['model_outcomes'], updated_count=count,
                         evaluation_basis=report.get('evaluation_basis', 'held-out-images'))
        print(f'Training results saved. {count} current models updated.', flush=True)
    except BaseException as exc:
        from tempo_credentials import redact_credentials
        with state_lock:
            state.update(phase='cancelled' if isinstance(exc, (InterruptedError,KeyboardInterrupt)) else 'failed',
                         error=redact_credentials(str(exc), os.environ.get('HF_TOKEN')))
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        done.set(); watcher.join(timeout=2)
        try:
            upload_status()
            if state.get('phase') == 'completed':
                print('Run completed and status saved.', flush=True)
        except Exception as exc:
            print(f'Final status upload failed ({sync_error_details(exc)}). '
                  'Check Colab logs and remote run records before retrying.', flush=True)
            if state.get('phase') == 'completed':
                raise RuntimeError('Results were saved, but final run completion could not be confirmed. '
                                   'Inspect the release/status before retrying; do not repeat training or publication.') from exc


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=('start', 'run', 'inspect', 'check', 'summary'), default='run')
    parser.add_argument('--preflight', action='store_true', help='Trial each worker before multi-epoch training.')
    parser.add_argument('--require-check', action='store_true', help='Require successful nonpublishing training checks for this exact request.')
    args = parser.parse_args()
    if args.stage == 'start':
        create_run()
    elif args.stage == 'inspect':
        inspect_request()
    elif args.stage == 'summary':
        summarize_release()
    elif args.stage == 'check':
        check_training()
    else:
        run(preflight=args.preflight, require_check=args.require_check)
