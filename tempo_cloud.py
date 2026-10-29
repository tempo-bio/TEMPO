"""Approved dataset snapshots and atomic training-run registration."""
import hashlib
import io
import json
import math
import threading
import time
from pathlib import Path
import uuid
import zipfile

BUNDLE_FILES = ('tempo_evaluation.py','tempo_evaluation_protocol.py','tempo_evaluation_metrics.py','tempo_evaluation_notebook.py','tempo_evaluation_runner.py', 'tempo_preprocessing.py', 'tempo_nuclei.py', 'tempo_nuclear_training.py', 'tempo_scout_training.py', 'train_yolo.py', 'tempo_training.py', 'tempo_training_runtime.py', 'tempo_training_groups.py', 'tempo_cellpose.py', 'tempo_cloud.py',
                'tempo_model_catalog.py', 'tempo_regions.py', 'tempo_annotations.py', 'tempo_annotation_store.py', 'tempo_config.py',
                'tempo_hub.py', 'tempo_credentials.py', 'tempo_secure_store.py', 'tempo_progress.py', 'tempo_model_transfer.py',
                'tempo_cloud_runner.py', 'requirements-training.txt', 'notebooks/colab_runtime.py')


def archive_sources(root):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name in BUNDLE_FILES:
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            # Windows builds and the Linux Colab runner must hash identical ZIP
            # bytes, including metadata that otherwise follows sys.platform.
            entry.create_system = 3
            entry.compress_type = zipfile.ZIP_DEFLATED
            # Canonical text keeps requests compatible with the same notebook
            # when an existing checkout uses Windows line endings.
            archive.writestr(entry, (Path(root)/name).read_text(encoding='utf-8').encode('utf-8'))
    return stream.getvalue()


def sync_retry_delay(error,failures,*,commit_limit=False):
    """Read reset hints without depending on a frozen engine's helper version."""
    from email.utils import parsedate_to_datetime
    import re
    delay=min(300,10*2**min(failures,5));hinted=False
    headers=getattr(getattr(error,'response',None),'headers',{}) or {}
    value=headers.get('Retry-After') or headers.get('retry-after')
    if value:
        try:seconds=float(value)
        except (ValueError,TypeError):
            try:seconds=parsedate_to_datetime(value).timestamp()-time.time()
            except (ValueError,TypeError,OverflowError):seconds=-1
        if math.isfinite(seconds) and seconds>=0:delay=max(delay,seconds);hinted=True
    if getattr(getattr(error,'response',None),'status_code',None)==429:
        retry=re.search(r'retry (?:this action )?in (?:about )?(\d+(?:\.\d+)?)\s*(second|minute|hour)s?',str(error),re.I)
        if retry:
            delay=max(delay,float(retry[1])*{'second':1,'minute':60,'hour':3600}[retry[2].lower()]);hinted=True
        from huggingface_hub.utils import parse_ratelimit_headers
        limit=parse_ratelimit_headers(headers)
        if limit is not None and limit.remaining==0:delay=max(delay,limit.reset_in_seconds);hinted=True
        if commit_limit and not hinted:delay=max(delay,3600)
    return delay


class RepositoryWriteBudget:
    """Share commit pacing and server cooldown across one cloud run's writers."""
    def __init__(self,interval=35,*,clock=None,wait=None):
        self.interval=interval;self.clock=clock or time.monotonic;self.wait=wait or time.sleep
        self.lock=threading.Lock();self.next_write=0.

    def take(self,*,defer=False):
        while True:
            with self.lock:
                now=self.clock();delay=self.next_write-now
                if delay<=0:
                    self.next_write=now+self.interval
                    return True
            if defer:return False
            self.wait(min(delay,10))

    def rate_limited(self,error):
        delay=sync_retry_delay(error,1,commit_limit=True)
        # Repository commit limits can use an hourly window, distinct from the
        # general API bucket. No reset hint is grounds for a conservative wait.
        with self.lock:self.next_write=max(self.next_write,self.clock()+delay)
        print(f'Checkpoint saving paused by the server for {delay:.0f}s. '
              'Completed files remain in this Colab runtime; saving will retry without training.',flush=True)


def training_transaction(hub, mutate, message, *, preupload=False, commit_lock=None,
                         write_budget=None,defer_write=False,repo_type='dataset'):
    """Only training records are written; image and annotation records are untouched."""
    from huggingface_hub import CommitOperationAdd
    from tempo_hub import commit_and_confirm
    from contextlib import nullcontext
    context = hub.token, hub.get_dataset_repo(), hub.get_models_repo()
    if repo_type not in ('dataset', 'model'):
        raise ValueError('Unsupported training repository type.')
    repo_id = context[1] if repo_type == 'dataset' else context[2]
    head = hub._head if repo_type == 'dataset' else lambda: hub._head(repo_type='model')
    read_json = hub._read if repo_type == 'dataset' else lambda name, revision: hub._read(name, revision, repo_type='model')
    prepared = {}
    if preupload:
        files, _ = mutate({}, head())
        if context != (hub.token, hub.get_dataset_repo(), hub.get_models_repo()):
            raise PermissionError('The account or repositories changed during the operation.')
        if any(not name.startswith('training/') for name in files):
            raise ValueError('Training writes must remain in the training directory.')
        prepared = {name: CommitOperationAdd(path_in_repo=name, path_or_fileobj=value)
                    for name, value in files.items() if isinstance(value, (str, Path))}
        if prepared:
            # LFS uploads can take minutes. They must finish BEFORE choosing the
            # optimistic parent; ordinary progress writes can continue meanwhile.
            hub.api.preupload_lfs_files(repo_id=repo_id, repo_type=repo_type, token=context[0],
                                       additions=list(prepared.values()), free_memory=False)
        if context != (hub.token, hub.get_dataset_repo(), hub.get_models_repo()):
            raise PermissionError('The account or repositories changed during the operation.')
    conflicts=0
    while True:
        with commit_lock if commit_lock is not None else nullcontext():
            if write_budget is not None and not write_budget.take(defer=defer_write):return False
            if context != (hub.token, hub.get_dataset_repo(), hub.get_models_repo()):
                raise PermissionError('The account or repositories changed during the operation.')
            revision = head()
            files, result = mutate({}, revision)
            if context != (hub.token, hub.get_dataset_repo(), hub.get_models_repo()):
                raise PermissionError('The account or repositories changed during the operation.')
            if any(not name.startswith('training/') for name in files):
                raise ValueError('Training writes must remain in the training directory.')
            if not files:
                return result
            operations = []
            for name, value in files.items():
                operation = prepared.get(name)
                if operation is not None:
                    from tempo_hub import TempoHub
                    if (str(value) != str(operation.path_or_fileobj) or
                            TempoHub.calculate_sha256(value) != operation.upload_info.sha256.hex()):
                        raise ValueError('A checkpoint file changed during upload. The checkpoint was not committed.')
                else:
                    if preupload and isinstance(value, (str, Path)):
                        raise ValueError('The checkpoint file list changed during upload. Retry preservation.')
                    operation = CommitOperationAdd(path_in_repo=name, path_or_fileobj=value)
                operations.append(operation)
            try:
                commit_and_confirm(hub.api, repo_id=repo_id, repo_type=repo_type, token=hub.token,
                                   parent_commit=revision, operations=operations, commit_message=message, read_json=read_json)
                return result
            except Exception as error:
                code=getattr(getattr(error,'response',None),'status_code',None)
                if code==429 and write_budget is not None:
                    write_budget.rate_limited(error)
                    if defer_write:return False
                    continue
                if code in (409, 412) and conflicts < 3:
                    conflicts+=1
                    continue
                raise


def resolve_disconnected_run(hub, request_id, *, expected_context=None):
    """Clear a stopped stale run after confirmation, preserving its records."""
    hub.ensure_credentials()
    if expected_context is not None and expected_context != (hub.token, hub.get_dataset_repo(), hub.get_models_repo()):
        raise PermissionError('The account or repositories changed before run recovery.')
    if not hub.is_maintainer(raise_on_error=True):
        raise PermissionError('Maintainer access is required to clear a training run.')

    def resolve(_, revision):
        request = hub._read('training/request.json', revision, {})
        current_id = request.get('id')
        status = hub._read(f'training/{current_id}/status.json', revision, {}) if current_id else {}
        result = dict(request=request, status=status, outcome='changed')
        if current_id != request_id:
            return {}, result
        if status.get('phase') in ('completed', 'failed', 'cancelled', 'disconnected'):
            result['outcome'] = 'finished'
            return {}, result
        heartbeat = status.get('updated_at')
        if type(heartbeat) not in (int, float) or not math.isfinite(heartbeat):
            raise ValueError('The run has no valid heartbeat. Its status cannot be safely cleared.')
        if time.time() - heartbeat <= 120:
            raise ValueError('Colab reported within the last two minutes. The record was kept. Use Cancel Run for an active run.')
        if status.get('phase') == 'awaiting_colab':
            raise ValueError('This run is waiting for Colab. Use Cancel Run to clear it.')
        status.update(phase='disconnected', updated_at=time.time(),
                      error='Run record cleared after confirmation that Colab execution stopped.')
        result.update(status=status, outcome='cleared')
        return {f'training/{request_id}/status.json': json.dumps(status).encode(),
                f'training/{request_id}/control.json': json.dumps({'cancel': True}).encode()}, result
    return training_transaction(hub, resolve, 'Clear disconnected training run')


def prepare_request(hub, root, epochs=150, gpu='A100'):
    hub.ensure_credentials()
    context = hub.token, hub.get_dataset_repo(), hub.get_models_repo()
    if not hub.is_maintainer(raise_on_error=True):
        raise PermissionError('Maintainer access is required to train.')
    if type(epochs) is not int or not 1 <= epochs <= 10000 or gpu not in ('A100', 'Any CUDA GPU'):
        raise ValueError('Invalid training settings.')
    revision = hub._head()
    from tempo_training import select_approved_annotations
    from tempo_training_groups import plan_splits, SPLIT_POLICY
    from tempo_annotations import validate_annotations
    from tempo_nuclei import migrate_record
    approved = select_approved_annotations({name:migrate_record(record) for name,record in hub._read('universal_annotations.json', revision, {}).items()})
    validate_annotations(approved)
    groups = hub._read('training/preparation_groups.json',revision,{})
    validation = hub._read('training/architecture_validation.json',revision,{})
    if validation.get('scope')=='single_mouse_fields':
        groups={name:record['image_id'] for name,record in approved.items()}
    if not groups and all(record.get('animal_id') or record.get('preparation_id') for record in approved.values()):
        groups={name:record.get('animal_id') or record['preparation_id'] for name,record in approved.items()}
    from tempo_evaluation import resolve_previous_assignments
    previous,assignment_resolution = resolve_previous_assignments(hub,revision)
    release = hub.model_manifest()
    bundle = archive_sources(root)
    request = dict(id=uuid.uuid4().hex, dataset_repo=hub.get_dataset_repo(), models_repo=hub.get_models_repo(),
                   dataset_revision=revision, image_count=len(approved), epochs=epochs, gpu=gpu,
                   images=[dict(name=name, display_name=record.get('original_filename') or name,
                                annotations=len(record['boxes'])) for name, record in sorted(approved.items())],
                   previous_splits=previous, assignment_resolution=assignment_resolution,
                   source_groups=groups, split_assignments=plan_splits(approved,groups=groups, previous=previous),
                   split_policy=SPLIT_POLICY, bundle_sha256=hashlib.sha256(bundle).hexdigest(),
                   baseline_revision=release['revision'], baseline_models=release['models'])
    if context != (hub.token, hub.get_dataset_repo(), hub.get_models_repo()):
        raise PermissionError('The account or repositories changed while loading training data.')
    return request, bundle


def queue_request(hub, request, bundle):
    from tempo_training_groups import validate_assignments
    if not hub.is_maintainer(raise_on_error=True):
        raise PermissionError('Maintainer access is required to train.')
    if (request['dataset_repo'], request['models_repo']) != (hub.get_dataset_repo(), hub.get_models_repo()):
        raise ValueError('The repositories changed while loading training data.')
    if hashlib.sha256(bundle).hexdigest() != request['bundle_sha256']:
        raise ValueError('Training code checksum mismatch.')
    names = [row['name'] for row in request['images']]
    validate_assignments(names, request['split_assignments'])
    def mutate(_, revision):
        from tempo_evaluation import evaluation_head, evaluation_read
        evaluation=evaluation_read(hub,'training/evaluation_request.json',evaluation_head(hub),{})
        if evaluation:
            raise ValueError('Model Evaluation is selected for this notebook. Resume its queued stage or choose Return to Regular Training in TEMPO.')
        previous = hub._read('training/request.json', revision, {})
        if previous:
            status = hub._read(f"training/{previous['id']}/status.json", revision, {})
            if status.get('phase') not in ('completed', 'failed', 'cancelled', 'disconnected'):
                raise ValueError('A training run is already active. Inspect or cancel it before starting another.')
        from tempo_evaluation import previous_assignments,assignment_policy,ASSIGNMENT_POLICY
        ledger = previous_assignments(hub,revision)
        if (request.get('previous_splits',{})!=ledger
                or any(name in ledger and ledger[name] != partition for name, partition in request['split_assignments'].items())):
            raise ValueError('Saved image assignments changed. Run the notebook again.')
        ledger = dict(ledger)
        ledger.update(request['split_assignments'])
        prefix = f"training/{request['id']}"
        return {'training/request.json': json.dumps(request, allow_nan=False).encode(),
                'training/split_assignments.json': json.dumps(ledger, allow_nan=False).encode(),
                ASSIGNMENT_POLICY:json.dumps(assignment_policy(ledger,request.get('assignment_resolution',{}),request['baseline_models'])).encode(),
                f'{prefix}/bundle.zip': bundle,
                f'{prefix}/status.json': json.dumps(dict(phase='awaiting_colab', request_id=request['id'], models={})).encode()}, request
    return training_transaction(hub, mutate, 'Register Colab training run')


def connection_cell(dataset_repo, *, models_repo, epochs=150, gpu='A100'):
    """Complete execution code for isolated verification notebooks with explicit repositories."""
    from notebooks.build_staged_colab_notebook import build_notebook
    notebook = build_notebook(dataset_repo, models_repo=models_repo)
    code = '\n\n'.join(''.join(cell['source']) for cell in notebook['cells'] if cell['cell_type'] == 'code')
    return code.replace('MAXIMUM_EPOCHS = 150', f'MAXIMUM_EPOCHS = {epochs!r}', 1).replace(
        'REQUIRED_GPU = "A100"', f'REQUIRED_GPU = {gpu!r}', 1)
