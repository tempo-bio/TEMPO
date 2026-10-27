"""Run a prepared model evaluation in Colab, with durable stage checkpoints."""
import copy
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time

from tempo_evaluation import (INTENT, artifact_path, checksum, file_checksum, frozen_records,
                              prefix, require_access, validate_plan,plan_stages,
                              evaluation_head, evaluation_read)

LARGE_ARTIFACT_BYTES=256*1024*1024
LOCAL_WEIGHT_CACHE_BYTES=8*1024**3


def download_artifact(hub, remote, revision, destination, expected,*,repo_type=None):
    from huggingface_hub import hf_hub_download
    repo_type = repo_type or ('model' if remote.startswith('training/evaluations/') else 'dataset')
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    # local_dir avoids retaining every restored checkpoint in HF's global cache.
    with tempfile.TemporaryDirectory(prefix='.evaluation-download-',dir=destination.parent) as directory:
        source=Path(hf_hub_download(repo_id=hub.get_dataset_repo() if repo_type=='dataset' else hub.get_models_repo(),repo_type=repo_type,filename=remote,
                                   revision=revision,token=hub.token,local_dir=directory))
        if file_checksum(source)!=expected:
            raise ValueError('A saved model evaluation artifact failed checksum verification.')
        source.replace(destination)


def workspace_files(workspace):
    for path in sorted(Path(workspace).rglob('*')):
        relative=path.relative_to(workspace)
        if (path.is_file() and not path.is_symlink() and relative.parts[0] not in ('input','input-models','protocol','protocol.zip')
                and not any(part.startswith('.') or part in ('progress','__pycache__') for part in relative.parts)):
            yield relative.as_posix(),path


def preserve_local_evaluation(root, workspace, hub, *, transaction=None, recovery_sources=None,stopped_job_id=None):
    """Preserve a stopped evaluation's existing files; never invoke model work.

    The recovery controller may be newer than the frozen training engine. Both
    source identities are retained; model inputs, outputs and plan stay intact.
    """
    import hashlib
    import uuid
    from tempo_cloud import archive_sources, training_transaction
    from tempo_evaluation import TERMINAL
    from tempo_evaluation_protocol import read_package
    transaction = transaction or training_transaction
    actor = require_access(hub)
    root, workspace = Path(root).resolve(), Path(workspace).resolve()
    base = prefix(workspace.name)
    revision = evaluation_head(hub)
    plan = validate_plan(evaluation_read(hub,base+'/plan.json', revision))
    if hashlib.sha256(archive_sources(root)).hexdigest() != plan['bundle_sha256']:
        raise ValueError('The original training engine differs from the frozen evaluation.')
    if json.loads((workspace/'manifest.json').read_text()) != plan['manifest']:
        raise ValueError('The local images and assignments differ from the frozen evaluation.')
    descriptor, package = read_package(workspace/'protocol/protocol.json')
    if descriptor != plan['protocol'] or hashlib.sha256(package).hexdigest() != plan['protocol_sha256']:
        raise ValueError('The local protocol differs from the frozen evaluation.')
    def stopped(head):
        intent = evaluation_read(hub,INTENT, head, {})
        state = evaluation_read(hub,base+'/state.json', head, {})
        fenced=False
        if stopped_job_id and intent.get('status')=='running':
            import math
            updated=state.get('updated_at')
            fenced=(intent.get('evaluation_id')==plan['id'] and intent.get('id')==stopped_job_id
                and state.get('job_id')==stopped_job_id and type(updated) in (int,float)
                and math.isfinite(updated) and time.time()-updated>120)
        if intent and intent.get('status') not in TERMINAL and not fenced:
            raise ValueError('Stop and resolve the active evaluation before preserving its files.')
        if state.get('phase') not in TERMINAL:
            if not fenced:raise ValueError('This evaluation is not recorded as stopped. Resolve its stopped run in TEMPO first.')
        if evaluation_read(hub,base+'/plan.json', head)['plan_sha256'] != plan['plan_sha256']:
            raise ValueError('The frozen evaluation plan changed during recovery.')
        return (intent,state) if fenced else None
    stopped(revision)
    previous = evaluation_read(hub,base+'/artifacts.json', revision, {})
    local = {name: dict(sha256=file_checksum(path), size=path.stat().st_size)
             for name, path in workspace_files(workspace)}
    models = []
    for marker in sorted(workspace.rglob('evaluation_stage.json')):
        result_path = marker.with_name('result.json')
        result = json.loads(result_path.read_text())
        artifact = Path(result['artifact']).resolve()
        if not artifact.is_relative_to(workspace):
            raise ValueError('A completed model points outside this evaluation workspace.')
        name = artifact.relative_to(workspace).as_posix()
        entry = local.get(name, previous.get(name))
        if not entry or entry['sha256'] != result['sha256']:
            raise ValueError('A completed model is missing or has a different checksum: '+name)
        models.append(dict(path=name, sha256=entry['sha256'], local=artifact.is_file()))
    if not models:
        raise ValueError('No completed worker checkpoints were found. No files were changed.')
    recovery_id = uuid.uuid4().hex
    receipt_path = base+'/recoveries/'+recovery_id+'.json'
    receipt = dict(format='tempo-evaluation-preservation-v1', evaluation_id=plan['id'],
                   plan_sha256=plan['plan_sha256'], bundle_sha256=plan['bundle_sha256'],
                   protocol_sha256=plan['protocol_sha256'], preserved_at=time.time(),
                   preserved_by=actor, completed_models=models, local_files=len(local),
                   recovery_sha256=hashlib.sha256(recovery_sources).hexdigest() if recovery_sources else None)
    weights = ('.pt','.zip','.onnx','.bin','.safetensors','.h5','.hdf5')
    def preserve(_, head):
        fenced=stopped(head)
        inventory = dict(evaluation_read(hub,base+'/artifacts.json', head, {}))
        updates = {}
        for name, entry in local.items():
            old = inventory.get(name)
            if old and old != entry and Path(name).suffix in weights:
                raise ValueError('Recovery would overwrite a different saved model: '+name)
            if old != entry:
                path = workspace/name
                if file_checksum(path) != entry['sha256']:
                    raise ValueError('A local checkpoint file changed during recovery: '+name)
                updates[base+'/workspace/'+name] = str(path)
                inventory[name] = entry
        updates[base+'/artifacts.json'] = json.dumps(inventory, allow_nan=False).encode()
        updates[receipt_path] = json.dumps(receipt, allow_nan=False).encode()
        if fenced:
            intent,state=copy.deepcopy(fenced)
            intent['status']='failed'
            state.update(phase='failed',updated_at=time.time(),error='Stopped execution checkpoints preserved after an interrupted save.')
            updates[INTENT]=json.dumps(intent).encode();updates[base+'/state.json']=json.dumps(state).encode()
        if recovery_sources:
            updates[base+'/recoveries/'+recovery_id+'.zip'] = recovery_sources
        return updates, receipt
    transaction(hub, preserve, 'Preserve stopped evaluation checkpoints', preupload=True, repo_type='model')
    if evaluation_read(hub,receipt_path, evaluation_head(hub)) != receipt:
        raise RuntimeError('Preservation could not be confirmed. Keep the local runtime files.')
    return receipt


def run_prepared_evaluation(root,settings,hub,*,workspace=None,transaction=None,runtime_sources=None,runtime_verifier=None,write_budget=None):
    """The notebook supplies only a queued intent; all fitting settings are frozen."""
    from tempo_cloud import archive_sources,training_transaction
    from tempo_cloud_runner import require_colab,watch_training_status,sync_error_details
    from tempo_progress import snapshots
    import hashlib
    if (transaction is None) != (runtime_sources is None):
        raise ValueError('An updated persistence controller must retain its exact sources.')
    if write_budget is None and transaction is None:
        from tempo_cloud import RepositoryWriteBudget
        write_budget=RepositoryWriteBudget()
    if write_budget is not None:
        from functools import partial
        training_transaction=partial(transaction or training_transaction,write_budget=write_budget,repo_type='model')
    else:
        from functools import partial
        training_transaction=partial(transaction or training_transaction,repo_type='model')
    runtime_sha256 = hashlib.sha256(runtime_sources).hexdigest() if runtime_sources is not None else None
    if runtime_verifier:runtime_verifier()
    require_colab();require_access(hub)
    root=Path(root)
    job=copy.deepcopy(settings['evaluation_request'])
    evaluation_id=job['evaluation_id'];base=prefix(evaluation_id)
    revision=evaluation_head(hub);plan=validate_plan(evaluation_read(hub,base+'/plan.json',revision))
    if (plan['plan_sha256']!=job['plan_sha256'] or plan['bundle_sha256']!=settings['bundle_sha256'] or
            plan['protocol_sha256']!=job['protocol_sha256'] or job['phase'] not in [stage['id'] for stage in plan_stages(plan)] or
            hashlib.sha256(archive_sources(root)).hexdigest()!=plan['bundle_sha256']):
        raise ValueError('The frozen evaluation source or configuration differs from this notebook.')
    gpu=subprocess.check_output(['nvidia-smi','--query-gpu=name','--format=csv,noheader'],text=True).strip()
    if not gpu or plan['gpu']=='A100' and 'A100' not in gpu:
        raise ValueError('Select the GPU requested by Model Evaluation before Run all.')
    state=evaluation_read(hub,base+'/state.json',revision,{})
    state.update(phase='preparing',stage=job['phase'],job_id=job['id'],gpu=gpu,updated_at=time.time(),error=None)
    if runtime_sha256:state['runtime_sha256']=runtime_sha256
    def claim(_,head):
        if evaluation_read(hub,INTENT,head,{})!=job or job.get('status')!='queued':
            raise ValueError('This evaluation request changed or was already claimed. Refresh Model Evaluation in TEMPO.')
        current=dict(job,status='running')
        updates = {INTENT:json.dumps(current).encode(),base+'/state.json':json.dumps(state).encode()}
        if runtime_sha256:updates[base+'/runtimes/'+runtime_sha256+'.zip']=runtime_sources
        return updates,True
    training_transaction(hub,claim,'Claim Model Evaluation in Colab')
    workspace=Path(workspace) if workspace is not None else Path('/content/TEMPO-evaluations')/evaluation_id
    workspace.mkdir(parents=True,exist_ok=True)
    inventory=evaluation_read(hub,base+'/artifacts.json',revision,{})
    accessed={};protocol_hashes={}
    lock=threading.RLock();write_lock=threading.RLock();cancelled=threading.Event();done=threading.Event();active_output=[None]
    started=time.monotonic()
    last_saved=[started]
    def control():
        head=evaluation_head(hub);current=evaluation_read(hub,INTENT,head,{})
        if current.get('id')!=job['id'] or current.get('status')!='running':
            cancelled.set()
            return dict(cancel=True)
        command=evaluation_read(hub,base+'/control.json',head,{})
        return dict(cancel=command.get('job_id')==job['id'] and command.get('cancel') is True)
    def save_state(files=None):
        if not files and time.monotonic()-last_saved[0]<60:return False
        def mutate(_,head):
            with lock:
                state['elapsed_seconds']=time.monotonic()-started
                state['updated_at']=time.time()
                state['models']=snapshots(Path(active_output[0])/'progress') if active_output[0] else {}
                snapshot=copy.deepcopy(state)
            current=evaluation_read(hub,INTENT,head,{})
            if current.get('id')!=job['id'] or current.get('status')!='running':
                raise InterruptedError('This notebook no longer owns the Model Evaluation request.')
            updates=dict(files or {})
            updates[base+'/state.json']=json.dumps(snapshot,allow_nan=False).encode()
            return updates,True
        # Do not block cancellation polling behind another commit. A checkpoint
        # includes current progress itself; the next heartbeat can try again.
        if not files and not write_lock.acquire(blocking=False):
            return False
        try:
            saved=training_transaction(hub,mutate,'Save Model Evaluation progress',
                preupload=bool(files),commit_lock=write_lock,defer_write=not bool(files))
            if saved:last_saved[0]=time.monotonic()
            return saved
        finally:
            if not files:write_lock.release()
    def verify_sources():
        if runtime_verifier:runtime_verifier()
        if hashlib.sha256(archive_sources(root)).hexdigest()!=plan['bundle_sha256']:
            raise ValueError('The frozen evaluation engine changed during execution.')
        if any(not (workspace/'protocol'/name).is_file() or file_checksum(workspace/'protocol'/name)!=digest for name,digest in protocol_hashes.items()):
            raise ValueError('The frozen protocol sources changed during execution.')
    def checkpoint():
        verify_sources()
        updates={};new_inventory=dict(inventory)
        for relative,path in workspace_files(workspace):
            entry=dict(sha256=file_checksum(path),size=path.stat().st_size)
            new_inventory[relative]=entry
            if inventory.get(relative)!=entry:
                updates[base+'/workspace/'+relative]=str(path)
        if not updates and new_inventory==inventory:return
        updates[base+'/artifacts.json']=json.dumps(new_inventory).encode()
        save_state(updates)
        inventory.clear();inventory.update(new_inventory)
        # Native final bundles are durable remotely; keep a bounded local cache.
        large=[]
        for relative,entry in inventory.items():
            path=workspace/relative
            if path.is_file() and path.suffix in ('.pt','.zip','.onnx','.bin','.safetensors','.h5','.hdf5') and entry['size']>LARGE_ARTIFACT_BYTES:
                large.append((accessed.get(relative,0),path,entry['size']))
        size=sum(item[2] for item in large)
        for _,path,length in sorted(large):
            if size<=LOCAL_WEIGHT_CACHE_BYTES:break
            path.unlink();size-=length
    def materialize(path):
        path=Path(path)
        relative=path.relative_to(workspace).as_posix()
        entry=inventory.get(relative)
        if not path.is_file():
            if entry is None:raise ValueError('A saved evaluation checkpoint is unavailable.')
            download_artifact(hub,base+'/workspace/'+relative,evaluation_head(hub),path,entry['sha256'])
        accessed[relative]=time.monotonic()
    def progress(message,**details):
        verify_sources()
        with lock:
            state.update(task=message,details={key:value for key,value in details.items() if key!='output'})
            if details.get('output'):active_output[0]=details['output']
        if cancelled.is_set():
            raise InterruptedError('Model evaluation cancelled.')
        print('Model Evaluation:',message,details.get('role',''),details.get('arm',''),details.get('seed',''),flush=True)
    watcher=threading.Thread(target=watch_training_status,kwargs=dict(
        read_control=control,upload_status=save_state,cancelled=cancelled,done=done),daemon=True)
    watcher.start()
    previous_term=signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM,lambda *_:cancelled.set())
    terminal='failed';failure=None
    try:
        # Restore immutable completed stages into the same stable workspace path.
        for relative,entry in inventory.items():
            if Path(relative).suffix in ('.pt','.zip','.onnx','.bin','.safetensors','.h5','.hdf5') and entry['size']>LARGE_ARTIFACT_BYTES:continue
            destination=workspace/str(artifact_path(relative))
            if not destination.is_file() or file_checksum(destination)!=entry['sha256']:
                download_artifact(hub,base+'/workspace/'+relative,revision,destination,entry['sha256'])
        from tempo_evaluation_protocol import unpack_package,load_protocol,EvaluationContext,write_json,validate_report
        protocol_archive=workspace/'protocol.zip'
        download_artifact(hub,base+'/protocol.zip',revision,protocol_archive,plan['protocol_sha256'])
        descriptor=unpack_package(protocol_archive.read_bytes(),workspace/'protocol',plan['protocol_sha256'])
        if descriptor!=plan['protocol']:raise ValueError('The protocol descriptor differs from its frozen plan.')
        protocol_hashes.update({name:file_checksum(workspace/'protocol'/name) for name in ['protocol.json',*descriptor['files']]})
        selected=plan['manifest']['images']
        records={name:row for name,row in frozen_records(hub,plan['dataset_revision']).items() if name in selected}
        if checksum(records)!=plan['records_sha256']:
            raise ValueError('Approved annotations differ from the frozen evaluation revision.')
        input_root=workspace/'input';images=input_root/'datasets/images';images.mkdir(parents=True,exist_ok=True)
        write_json(input_root/'datasets/universal_annotations.json',records)
        write_json(workspace/'manifest.json',plan['manifest'])
        identities=hub._read('lock_manifest.json',plan['dataset_revision'],{}).get('images',{})
        for name,record in records.items():
            if control()['cancel']:
                raise InterruptedError('Cancelled while preparing evaluation images.')
            expected=record['image_id'].removeprefix('sha256:')
            remote_name=identities.get(name,{}).get('remote_filename',name)
            download_artifact(hub,'images/'+remote_name,plan['dataset_revision'],images/name,expected)
        result_root=workspace/'results';result_root.mkdir(exist_ok=True)
        reports={stage['id']:evaluation_read(hub,base+'/reports/'+stage['id']+'.json',revision,{}) for stage in plan_stages(plan)}
        stage=next(stage for stage in plan_stages(plan) if stage['id']==job['phase'])
        with lock:state['phase']='running'
        save_state()
        context=EvaluationContext(plan=plan,stage=stage,workspace=workspace,input_root=input_root,reports=reports,
            progress=progress,checkpoint=checkpoint,materialize=materialize,cancelled=cancelled.is_set,engine_root=root)
        def model_path(filename):
            entry=plan['baseline_models'].get(filename)
            if not entry:raise ValueError('The requested model is not part of this frozen release.')
            destination=workspace/'input-models'/filename
            if not destination.is_file() or file_checksum(destination)!=entry['sha256']:
                download_artifact(hub,filename,plan['baseline_revision'],destination,entry['sha256'],repo_type='model')
            return destination
        context._model_fetch=model_path
        report_path=result_root/(stage['id']+'-report.json')
        if report_path.is_file():
            report=validate_report(json.loads(report_path.read_text()))
        else:
            with load_protocol(workspace/'protocol',descriptor) as protocol:
                report=validate_report(protocol.run(context))
            report['execution']=dict(stage=stage['id'],attempt_id=job['id'],elapsed_seconds=time.monotonic()-started,gpu=gpu,
                dataset_revision=plan['dataset_revision'],model_revision=plan['baseline_revision'],
                bundle_sha256=plan['bundle_sha256'],protocol_sha256=plan['protocol_sha256'])
            if runtime_sha256:report['execution']['runtime_sha256']=runtime_sha256
            if settings.get('compatibility_repairs'):
                report['execution']['compatibility_repairs']=list(settings['compatibility_repairs'])
            validate_report(report)
            verify_sources();write_json(report_path,report)
        checkpoint()
        # Keep the exact file hash alongside parsed JSON for adoption verification.
        remote_report=dict(report,report_sha256=file_checksum(report_path))
        outputs={base+'/reports/'+stage['id']+'.json':json.dumps(remote_report,allow_nan=False).encode()}
        state['phase']='completed' if stage==plan_stages(plan)[-1] else 'ready'
        save_state(outputs)
        terminal='completed'
        summary=dict(workflow='model-evaluation',evaluation_id=evaluation_id,stage=job['phase'],
                     dataset_revision=plan['dataset_revision'],scope=plan['scope'],report=remote_report)
        write_json(root/'runs/release_summary.json',summary)
        return summary
    except BaseException as error:
        terminal='cancelled' if isinstance(error,(InterruptedError,KeyboardInterrupt)) else 'failed'
        failure=sync_error_details(error)
        # Preserve completed stages even when a later stage fails.
        try:checkpoint()
        except Exception as save_error:print('Checkpoint preservation:',sync_error_details(save_error),flush=True)
        raise
    finally:
        signal.signal(signal.SIGTERM,previous_term)
        done.set();watcher.join(timeout=2)
        def finish(_,head):
            current=evaluation_read(hub,INTENT,head,{})
            if current.get('id')!=job['id'] or current.get('status')!='running':
                return {},False
            current['status']=terminal
            state.update(updated_at=time.time(),elapsed_seconds=time.monotonic()-started,error=failure)
            if terminal!='completed':state['phase']=terminal
            return {INTENT:json.dumps(current).encode(),base+'/state.json':json.dumps(state,allow_nan=False).encode()},True
        try:training_transaction(hub,finish,'Finish Model Evaluation stage',commit_lock=write_lock)
        except Exception as finish_error:
            print('Final evaluation status could not be saved:',sync_error_details(finish_error),flush=True)
            if failure is None:raise
