"""Generate the maintained, self-contained TEMPO Colab notebook."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
from tempo_cloud import BUNDLE_FILES, archive_sources
from tempo_config import DEFAULT_PREFERENCES

DEFAULT_DATASET_REPO = DEFAULT_PREFERENCES['collaboration']['hf_dataset_repo']['value']
DEFAULT_MODELS_REPO = DEFAULT_PREFERENCES['collaboration']['hf_models_repo']['value']

EVALUATION_DRIVER = '''import hashlib, importlib.util, io, json, os, types, zipfile
from pathlib import Path
from tempo_hub import tempo_hub
from tempo_config import tempo_preferences
root = Path(__file__).resolve().parent
settings = json.loads((root/'notebook_settings.json').read_text())
payload = (root/'evaluation-runtime.zip').read_bytes()
if hashlib.sha256(payload).hexdigest() != settings['runtime_sha256']:
    raise ValueError('The evaluation controller changed. Upload the maintained notebook again.')
spec = importlib.util.spec_from_file_location('_tempo_verified_runtime', root/'runtime-support/tempo_training_runtime.py')
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
support.verify_runtime_support(root, payload)
for key in ('dataset', 'models'):
    tempo_preferences.set('collaboration', 'hf_'+key+'_repo', settings[key+'_repo'])
tempo_hub.token = os.environ['HF_TOKEN']
modules = []
with zipfile.ZipFile(io.BytesIO(payload)) as archive:
    for source in ('tempo_cloud.py', 'tempo_evaluation_runner.py'):
        module = types.ModuleType('_tempo_controller_'+source[:-3])
        module.__file__ = str(root/'evaluation-runtime.zip')+'!'+source
        exec(compile(archive.read(source), source, 'exec'), module.__dict__)
        modules.append(module)
transport, controller = modules
controller.run_prepared_evaluation(root, settings, tempo_hub,
    transaction=transport.training_transaction, runtime_sources=payload,
    write_budget=transport.RepositoryWriteBudget(),
    runtime_verifier=lambda:support.verify_runtime_support(root,payload))
'''


def build_notebook(dataset_repo=DEFAULT_DATASET_REPO, expected_request_id='', models_repo=DEFAULT_MODELS_REPO):
    repository = Path(__file__).resolve().parents[1]
    payload = archive_sources(repository)
    checksum = hashlib.sha256(payload).hexdigest()
    cells = []
    def markdown(text):
        cells.append(dict(cell_type='markdown', metadata={}, source=text.splitlines(keepends=True)))
    def code(text, form=False):
        compile(text, 'tempo-notebook-cell', 'exec')
        cells.append(dict(cell_type='code', metadata={'cellView':'form'} if form else {},
                          execution_count=None, outputs=[], source=text.splitlines(keepends=True)))
    markdown('''# TEMPO Model Training

Enable access to the `HF_TOKEN` Colab secret, select a GPU runtime, and choose **Run all**.
A queued **Model Evaluation** runs its prepared protocol stage automatically. Choose **Return to Regular Training** in TEMPO when you finish an evaluation. Review evaluation results in **TEMPO → Maintainer → Model Evaluation**.
''')
    markdown('''## Settings

StarDist trains on reviewed nuclei before the healthy-cell Cellpose specialist consumes its predicted helper. Cellpose trains separate cell and group specialists. **Maximum epochs** applies to each specialist.
''')
    code('''MAXIMUM_EPOCHS = 150 # @param {type:"integer"}
REQUIRED_GPU = "A100" # @param ["A100", "Any CUDA GPU"]

import base64, functools, hashlib, importlib.util, io, json, os, pathlib, re, subprocess, sys, uuid, venv, zipfile
from google.colab import userdata
DATASET_REPO = ''' + repr(dataset_repo) + '''
MODELS_REPO = ''' + repr(models_repo) + '''
setup_ready = training_ready = False
if sys.platform != 'linux' or not pathlib.Path('/content').is_dir():
    raise RuntimeError('Open this notebook in Colab with a GPU runtime.')
if type(MAXIMUM_EPOCHS) is not int or not 1 <= MAXIMUM_EPOCHS <= 10000:
    raise ValueError('Maximum epochs must be an integer from 1 to 10000.')
if REQUIRED_GPU not in ('A100', 'Any CUDA GPU'):
    raise ValueError('Select A100 or Any CUDA GPU.')
for repository in (DATASET_REPO, MODELS_REPO):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Repository names must use owner/name.')
try:
    token = (userdata.get('HF_TOKEN') or '').strip()
except Exception:
    raise RuntimeError('Enable access to HF_TOKEN in Colab Secrets.') from None
if not token:
    raise RuntimeError('Enable access to HF_TOKEN in Colab Secrets.')
payload = base64.b64decode(''' + repr(base64.b64encode(payload).decode()) + ''', validate=True)
bundle_sha256 = ''' + repr(checksum) + '''
if hashlib.sha256(payload).hexdigest() != bundle_sha256:
    raise ValueError('Training code checksum mismatch. Upload the maintained TEMPO notebook again.')
allowed = ''' + repr(BUNDLE_FILES) + '''
with zipfile.ZipFile(io.BytesIO(payload)) as archive:
    if len(archive.namelist()) != len(allowed) or set(archive.namelist()) != set(allowed):
        raise ValueError('Unexpected or duplicate training code files.')
    if sum(entry.file_size for entry in archive.infolist()) > 20*1024*1024:
        raise ValueError('Training code exceeds the supported size.')
    sources = {name:archive.read(name) for name in allowed}
for name, data in sources.items():
    if name.endswith('.py'):
        compile(data, name, 'exec')
bootstrap = {}
controller_sources = {name:sources[name] for name in ('tempo_cloud.py', 'tempo_evaluation_runner.py')}
support_sources = {'tempo_training_runtime.py':sources['tempo_training_runtime.py'],
                   'colab_runtime.py':sources['notebooks/colab_runtime.py']}
runtime_support = {'__name__':'tempo_notebook_runtime_support'}
exec(compile(support_sources['tempo_training_runtime.py'], 'tempo_training_runtime.py', 'exec'), runtime_support)
runtime_support['clear_runtime_environment'](os.environ)
exec(compile(sources['tempo_evaluation_notebook.py'], 'tempo_evaluation_notebook.py', 'exec'), bootstrap)
evaluation_request = bootstrap['prepared_evaluation'](DATASET_REPO, MODELS_REPO, token)
extra_requirements = None
if evaluation_request:
    MAXIMUM_EPOCHS = evaluation_request.get('epochs', MAXIMUM_EPOCHS)
    REQUIRED_GPU = evaluation_request['gpu']
    payload = bootstrap['evaluation_bundle'](evaluation_request, token)
    bundle_sha256 = evaluation_request['bundle_sha256']
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        frozen_names = set(archive.namelist())
        supported = (set(allowed), set(allowed)-{'tempo_training_runtime.py'})
        if len(archive.namelist()) != len(frozen_names) or frozen_names not in supported:
            raise ValueError('The evaluation uses another notebook version. Upload the matching maintained notebook.')
        if sum(entry.file_size for entry in archive.infolist()) > 20*1024*1024:
            raise ValueError('Evaluation sources exceed the supported size.')
        sources = {name:archive.read(name) for name in archive.namelist()}
    for name, data in sources.items():
        if name.endswith('.py'):
            compile(data, name, 'exec')
    extra_requirements = bootstrap['protocol_requirements'](evaluation_request, token)
gpu = subprocess.run(['nvidia-smi','--query-gpu=name','--format=csv,noheader'], capture_output=True, text=True)
print('Assigned GPU:', gpu.stdout.strip() or 'None', flush=True)
if gpu.returncode or not gpu.stdout.strip() or (REQUIRED_GPU == 'A100' and 'A100' not in gpu.stdout):
    raise RuntimeError('Select the required GPU in Colab runtime settings, then choose Run all.')
run_id = uuid.uuid4().hex
root = pathlib.Path('/content/TEMPO-cloud') / run_id / bundle_sha256[:16]
root.mkdir(parents=True)
for name, data in sources.items():
    target = root/name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
settings = dict(run_id=run_id, dataset_repo=DATASET_REPO, models_repo=MODELS_REPO,
                epochs=MAXIMUM_EPOCHS, gpu=REQUIRED_GPU, bundle_sha256=bundle_sha256,
                evaluation_request=evaluation_request)
(root/'notebook_settings.json').write_text(json.dumps(settings, indent=2))
source_hashes = {name:hashlib.sha256(data).hexdigest() for name,data in sources.items()}
if evaluation_request:
    support_files, compatibility = runtime_support['compatibility_files'](sources, support_sources)
    settings['compatibility_repairs'] = [record['id'] for record in compatibility['overrides'].values()]
    for name, data in support_files.items():
        target = root/'runtime-support'/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(data)
        source_hashes['runtime-support/'+name] = hashlib.sha256(data).hexdigest()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in controller_sources.items():
            entry = zipfile.ZipInfo(name, date_time=(1980,1,1,0,0,0)); entry.create_system=3
            entry.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
        for name, data in support_files.items():
            entry = zipfile.ZipInfo('runtime-support/'+name, date_time=(1980,1,1,0,0,0)); entry.create_system=3
            entry.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
    controller_archive = stream.getvalue()
    settings['runtime_sha256'] = hashlib.sha256(controller_archive).hexdigest()
    (root/'notebook_settings.json').write_text(json.dumps(settings, indent=2))
    (root/'evaluation-runtime.zip').write_bytes(controller_archive)
    (root/'evaluation-runtime.py').write_text('''+repr(EVALUATION_DRIVER)+''')
    for name in ('evaluation-runtime.zip', 'evaluation-runtime.py'):
        source_hashes[name] = hashlib.sha256((root/name).read_bytes()).hexdigest()
    os.environ['TEMPO_EXECUTION_RUNTIME'] = str(root)
    os.environ['PYTHONPATH'] = str(root/'runtime-support')+os.pathsep+os.environ.get('PYTHONPATH','')
    if settings['compatibility_repairs']:
        print('Applying recorded compatibility fixes:', ', '.join(settings['compatibility_repairs']), flush=True)
else:
    os.environ.pop('TEMPO_EXECUTION_RUNTIME', None)
if extra_requirements is not None:
    (root/'protocol-requirements.txt').write_bytes(extra_requirements)
    source_hashes['protocol-requirements.txt'] = hashlib.sha256(extra_requirements).hexdigest()
os.environ.update(HF_TOKEN=token, MPLBACKEND='Agg', PYTHONUNBUFFERED='1')
runtime_path = root/'runtime-support/colab_runtime.py' if evaluation_request else root/'notebooks/colab_runtime.py'
spec = importlib.util.spec_from_file_location('tempo_notebook_runtime', runtime_path)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
run_logged = functools.partial(runtime.run_logged, command_adapter=runtime_support['execution_command'])
configure_cuda_environment = runtime.configure_cuda_environment

def verify_sources():
    if json.loads((root/'notebook_settings.json').read_text()) != settings:
        raise ValueError('Run settings changed during execution. Choose Run all to start another run.')
    for name, digest in source_hashes.items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest() != digest:
            raise ValueError('Training code changed during execution. Choose Run all to start another run.')

print('Dataset repository:', DATASET_REPO)
print('Model repository:', MODELS_REPO)
if not evaluation_request:
    print('Maximum epochs:', MAXIMUM_EPOCHS)
print('Workflow:', 'Model Evaluation — ' + evaluation_request['phase'] if evaluation_request else 'Regular training')
''', form=True)
    markdown('## Environment\n')
    code('''verify_sources()
setup_ready = training_ready = False
if not (3, 12) <= sys.version_info[:2] < (3, 14):
    raise RuntimeError('This training environment supports Python 3.12–3.13.')
env = pathlib.Path('/content/tempo-evaluation-env-' + evaluation_request['protocol_sha256'][:16]) if evaluation_request else pathlib.Path('/content/tempo-training-env-cu126-v3')
if not (env/'bin/python').exists():
    venv.EnvBuilder(with_pip=False).create(env)
python = str(env/'bin/python')
run_logged([sys.executable, '-m', 'pip', '--python', python, 'install', '-r', str(root/'requirements-training.txt')],
           label='Install training packages', cwd=root, log_path=root/'runs/setup.log', timeout=1200)
if extra_requirements is not None:
    run_logged([sys.executable, '-m', 'pip', '--python', python, 'install', '-r', str(root/'protocol-requirements.txt')],
               label='Install protocol packages', cwd=root, log_path=root/'runs/setup.log', timeout=1200)
run_logged([sys.executable, '-m', 'pip', '--python', python, 'check'],
           label='Check package compatibility', cwd=root, log_path=root/'runs/setup.log', timeout=180)
os.environ.update(configure_cuda_environment(python))
diagnostic = "from notebooks.colab_runtime import check_training_environment; import sys; from pathlib import Path; check_training_environment(sys.executable, Path.cwd())"
run_logged([python, '-u', '-c', diagnostic], label='Check GPU and packages', cwd=root,
           log_path=root/'runs/diagnostics.log', timeout=4200)
setup_ready = True
''')
    markdown('## Training\n')
    code('''verify_sources()
training_ready = False
if not setup_ready:
    raise RuntimeError('Choose Run all to complete environment checks before training.')
command = ([python, '-u', str(root/'evaluation-runtime.py')] if evaluation_request else
           [python, '-u', str(root/'tempo_cloud_runner.py'), '--stage', 'start'])
run_logged(command, label='Run evaluation stage' if evaluation_request else 'Train approved images',
           cwd=root, log_path=root/'runs/cloud.log')
training_ready = True
''')
    markdown('## Results\n')
    code('''verify_sources()
if not training_ready:
    raise RuntimeError('Training did not finish. Inspect the training output before starting another run.')
summary = json.loads((root/'runs/release_summary.json').read_text())
if summary.get('workflow') == 'model-evaluation':
    print('Model Evaluation:', summary['evaluation_id'])
    print('Stage:', summary['stage'])
    print('Dataset revision:', summary['dataset_revision'])
    print('Scope:', summary['scope'])
    report = summary['report']
    print(report.get('summary', 'Stage complete.'))
    for table in report.get('tables', []):
        print(table.get('title', 'Results'))
        print(' | '.join(table['columns']))
        for row in table['rows']:
            print(' | '.join(str(value) for value in row))
    print('Review the stage in TEMPO before queuing another stage or recording a decision.')
    print('Current models were unchanged.')
else:
    print('Run:', summary['release_id'])
    print('Dataset revision:', summary['dataset_revision'])
    print('Evaluation:', 'Held-out images' if summary['evaluation_basis'] == 'held-out-images' else 'Training images (diagnostic scores)')
    catalog = {}
    exec(compile((root/'tempo_model_catalog.py').read_bytes(), 'tempo_model_catalog.py', 'exec'), catalog)
    model_names = catalog['MODEL_LABELS']
    def describe_checkpoint(label, details):
        print('  ' + label + 'backbone:', details['backbone'], '| Initial weights:', details['pretrained_model'])
        if details.get('fallback'):
            print('  ' + label + 'CPDINO initialization failed (' + details['fallback']['error_type'] + '); trained Cellpose-SAM.')
    for model, result in summary['models'].items():
        previous = result.get('baseline_metrics')
        print(model_names[model] + ':', result['outcome'])
        print('  New score:', result['metrics']['score'], '| Previous model score:', previous['score'] if previous else 'Unavailable')
        print('  Metric:', result['metrics']['metric'])
        class_scores = result['metrics'].get('class_scores', {})
        if class_scores:
            for name, score in class_scores.items():
                print('  ' + name.replace('_', ' ').capitalize() + ' F1:', score)
        if 'segmentation_score' in result['metrics']:
            print('  Outline-only F1:', result['metrics']['segmentation_score'])
            print('  Unclassified predictions:', result['metrics']['unclassified_predictions'])
        details = result.get('training_details')
        if details:
            if details.get('stages'):
                for name, specialist in details['stages'].items():
                    describe_checkpoint(name.capitalize() + ' specialist ', specialist)
                    print('  Training outlines:', specialist['training_cells'], '| Epochs:', specialist['epochs'])
            else:
                describe_checkpoint('', details)
    print('Current models updated:', summary['updated_count'])
    print('Logs and saved weights:', root/'runs')
''')
    return dict(nbformat=4, nbformat_minor=5,
                metadata=dict(colab=dict(name='TEMPO_Colab_Trainer.ipynb'),
                              kernelspec=dict(display_name='Python 3', language='python', name='python3'),
                              language_info=dict(name='python'), accelerator='GPU',
                              tempo=dict(workflow='colab-owned-v1', dataset_repo=dataset_repo, models_repo=models_repo,
                                         bundle_sha256=checksum)),
                cells=[dict(cell, id=f'tempo-stage-{index:02d}') for index,cell in enumerate(cells)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset-repo', default=DEFAULT_DATASET_REPO)
    parser.add_argument('--models-repo', default=DEFAULT_MODELS_REPO)
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('TEMPO_Colab_Trainer.ipynb'))
    args = parser.parse_args()
    args.output.write_text(json.dumps(build_notebook(args.dataset_repo, models_repo=args.models_repo), indent=1)+'\n')


if __name__ == '__main__':
    main()
