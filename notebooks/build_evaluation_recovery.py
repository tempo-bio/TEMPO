"""Build a save-only Colab cell for an existing, stopped evaluation runtime."""
import base64
import hashlib
import io
from pathlib import Path
import zipfile


DRIVER = '''import functools,hashlib,io,json,os,tempfile,types,zipfile
from pathlib import Path
from tempo_hub import tempo_hub as hub
from tempo_config import tempo_preferences
root=Path(__file__).resolve().parent
payload=(root/'preservation-runtime.zip').read_bytes()
modules=[]
with zipfile.ZipFile(io.BytesIO(payload)) as archive:
    for name in ('tempo_cloud.py','tempo_evaluation_runner.py'):
        module=types.ModuleType('_tempo_preservation_'+name[:-3])
        module.__file__=str(root/'preservation-runtime.zip')+'!'+name
        exec(compile(archive.read(name),name,'exec'),module.__dict__);modules.append(module)
transport,controller=modules
settings=json.loads((root/'notebook_settings.json').read_text())
job=settings['evaluation_request']
for key in ('dataset','models'):tempo_preferences.set('collaboration','hf_'+key+'_repo',settings[key+'_repo'])
hub.token=os.environ['HF_TOKEN']
workspace=Path('/content/TEMPO-evaluations')/job['evaluation_id']
receipt=controller.preserve_local_evaluation(root,workspace,hub,
    transaction=functools.partial(transport.training_transaction,write_budget=transport.RepositoryWriteBudget()),
    recovery_sources=payload,stopped_job_id=job['id'])
revision=hub._head(repo_type='model')
print('Verifying saved model checksums. No training will run.',flush=True)
for model in receipt['completed_models']:
    with tempfile.TemporaryDirectory(prefix='tempo-preservation-check-') as folder:
        controller.download_artifact(hub,controller.prefix(job['evaluation_id'])+'/workspace/'+model['path'],
            revision,Path(folder)/Path(model['path']).name,model['sha256'])
record=Path('/content')/('TEMPO-preservation-verification-'+job['evaluation_id']+'.json')
record.write_text(json.dumps(dict(receipt=receipt,verified_revision=revision),indent=2))
print('PRESERVATION CONFIRMED',flush=True)
print('Completed models verified:',len(receipt['completed_models']),flush=True)
for model in receipt['completed_models']:print(' ',model['path'],'| SHA256:',model['sha256'],flush=True)
print('Saved revision:',revision,flush=True)
print('Verification record:',record,flush=True)
'''


def build_cell(repository=None):
    repository=Path(repository) if repository else Path(__file__).resolve().parents[1]
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ('tempo_cloud.py','tempo_evaluation_runner.py'):
            entry=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));entry.create_system=3
            entry.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(entry,(repository/name).read_bytes())
    payload=stream.getvalue()
    return '''# Save completed files from this failed notebook. This cell never trains.
import base64,hashlib,io,json,pathlib,re,shutil,types,uuid,zipfile
if 'root' not in globals() or 'python' not in globals() or 'run_logged' not in globals():
    raise RuntimeError('Run this cell in the failed notebook, using its existing connected runtime.')
recovery_root=pathlib.Path(root)
recovery_settings=json.loads((recovery_root/'notebook_settings.json').read_text())
recovery_job=recovery_settings.get('evaluation_request',{})
if not re.fullmatch('[a-f0-9]{32}',recovery_job.get('evaluation_id','')):
    raise RuntimeError('This notebook does not identify a prepared evaluation.')
recovery_workspace=pathlib.Path('/content/TEMPO-evaluations')/recovery_job['evaluation_id']
if not (recovery_workspace/'manifest.json').is_file():
    raise RuntimeError('The stopped evaluation files are missing. Keep this runtime and its logs.')
print('Saving completed evaluation files. No training or model publication will run.',flush=True)
recovery_payload=base64.b64decode('''+repr(base64.b64encode(payload).decode())+''')
if hashlib.sha256(recovery_payload).hexdigest()!='''+repr(hashlib.sha256(payload).hexdigest())+''':
    raise ValueError('Recovery source verification failed.')
recovery_transport=types.ModuleType('_tempo_backup_transport')
with zipfile.ZipFile(io.BytesIO(recovery_payload)) as recovery_archive:
    exec(compile(recovery_archive.read('tempo_cloud.py'),'tempo_cloud.py','exec'),recovery_transport.__dict__)
recovery_files=[]
for recovery_path in sorted(recovery_workspace.rglob('*')):
    if recovery_path.is_file() and not recovery_path.is_symlink() and '__pycache__' not in recovery_path.parts:
        recovery_files.append((recovery_path,'workspace/'+recovery_path.relative_to(recovery_workspace).as_posix()))
for recovery_name in [*recovery_transport.BUNDLE_FILES,'notebook_settings.json','evaluation-runtime.zip','evaluation-runtime.py']:
    recovery_path=recovery_root/recovery_name
    if recovery_path.is_file():recovery_files.append((recovery_path,'engine/'+recovery_name))
recovery_needed=sum(path.stat().st_size for path,_ in recovery_files)
if shutil.disk_usage(recovery_workspace).free<recovery_needed+max((path.stat().st_size for path,_ in recovery_files),default=0)+16*1024**2:
    raise RuntimeError('There is not enough free space to make and verify a backup. Original files were retained.')
recovery_backup=pathlib.Path('/content')/('TEMPO-evaluation-backup-'+recovery_job['evaluation_id']+'-'+uuid.uuid4().hex[:8]+'.zip')
def recovery_digest(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()
recovery_inventory={}
with zipfile.ZipFile(recovery_backup,'w',allowZip64=True) as recovery_zip:
    for recovery_path,recovery_name in recovery_files:
        recovery_inventory[recovery_name]=dict(sha256=recovery_digest(recovery_path),size=recovery_path.stat().st_size)
        recovery_zip.write(recovery_path,recovery_name)
    recovery_zip.writestr('backup.json',json.dumps(dict(format='tempo-evaluation-local-backup-v1',
        evaluation_id=recovery_job['evaluation_id'],files=recovery_inventory),indent=2))
print('LOCAL BACKUP READY:',recovery_backup,flush=True)
print('Backup SHA256:',recovery_digest(recovery_backup),flush=True)
from google.colab import files
try:files.download(str(recovery_backup))
except Exception:
    print('Automatic backup download could not start. The backup remains at the path above; download it through Colab Files.',flush=True)
else:print('Backup download requested.',flush=True)
print('Keep this runtime connected until remote preservation is confirmed.',flush=True)
(recovery_root/'preservation-runtime.zip').write_bytes(recovery_payload)
(recovery_root/'save-evaluation.py').write_text('''+repr(DRIVER)+''')
run_logged([python,'-u',str(recovery_root/'save-evaluation.py')],label='Preserve completed evaluation files',
    cwd=recovery_root,log_path=recovery_root/'runs/preservation.log')
'''


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    arguments=parser.parse_args();arguments.output.write_text(build_cell())
