"""Audited compatibility repairs and recovery of fitted TEMPO scout candidates.

This module imports only the standard library until a training function is called.
Compatibility rules identify source revisions, never evaluations or protocols.
"""
import hashlib
import io
import json
from pathlib import Path
import zipfile


def clear_runtime_environment(environment):
    """Discard only import paths owned by earlier generated TEMPO notebook runs."""
    import os
    environment.pop('TEMPO_EXECUTION_RUNTIME', None)
    paths = [value for value in environment.get('PYTHONPATH','').split(os.pathsep)
             if value and 'TEMPO-cloud' not in Path(value).parts]
    if paths:
        environment['PYTHONPATH'] = os.pathsep.join(paths)
    else:
        environment.pop('PYTHONPATH', None)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def candidate_identity(dataset, info, epochs, batch, baseline_sha256):
    from tempo_cloud_runner import dataset_digest
    return dict(dataset_sha256=dataset_digest(dataset), epochs=epochs, batch=batch,
                seed=info.get('seed', 17), baseline_sha256=baseline_sha256)


def record_scout_candidate(artifact, output, dataset, info, epochs, batch, baseline_sha256, *, provenance=None):
    from tempo_annotation_store import atomic_bytes
    artifact, output = Path(artifact).resolve(), Path(output).resolve()
    if not artifact.is_relative_to(output) or not artifact.is_file():
        raise ValueError('A trained scout must be retained in its own output directory.')
    receipt = dict(format='tempo-trained-scout-v1',
                   identity=candidate_identity(dataset, info, epochs, batch, baseline_sha256),
                   artifact=artifact.relative_to(output).as_posix(), sha256=file_sha256(artifact),
                   provenance=provenance or {'kind':'completed-fit'})
    atomic_bytes(output/'trained_candidate.json', json.dumps(receipt, allow_nan=False).encode())
    return receipt


def resume_scout_candidate(output, dataset, info, epochs, batch, baseline_sha256, *, allow_legacy=False):
    """Return an identified completed export, without fitting or publishing anything."""
    from tempo_training import MODEL_FILES
    output = Path(output).resolve()
    receipt_path = output/'trained_candidate.json'
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text())
        artifact = (output/receipt['artifact']).resolve()
        if (receipt.get('format') != 'tempo-trained-scout-v1'
                or not artifact.is_relative_to(output) or not artifact.is_file()
                or receipt.get('identity') != candidate_identity(dataset, info, epochs, batch, baseline_sha256)
                or receipt.get('sha256') != file_sha256(artifact)):
            raise ValueError('The saved scout candidate differs from its training inputs or checksum. It was retained.')
        return str(artifact)
    artifact = output/MODEL_FILES['nano_scout']
    if not artifact.exists():
        return None
    # Older workers wrote their final export only AFTER fitting returned, but did
    # not save the scoring receipt. Adoption is limited to frozen evaluations.
    if not allow_legacy or info.get('origin') != 'experiment':
        raise ValueError('A scout export exists without a training receipt. It was retained for recovery.')
    import torch
    checkpoint = torch.load(artifact, map_location='cpu', weights_only=False)
    model = checkpoint.get('model')
    metadata = getattr(model, 'tempo_metadata', {})
    args = checkpoint.get('train_args', {})
    if (metadata != dict(role='scout', format='centroid-v1', preprocessing=info['preprocessing'], box_width=info['box_width'])
            or Path(args.get('data', '')).resolve() != (Path(dataset)/'detect/data.yaml').resolve()
            or args.get('epochs') != epochs or args.get('batch') != batch or args.get('seed') != info.get('seed', 17)):
        raise ValueError('The completed scout export does not match this frozen training task. It was retained.')
    record_scout_candidate(artifact, output, dataset, info, epochs, batch, baseline_sha256,
        provenance=dict(kind='legacy-final-export',training_arguments={key:args[key] for key in ('data','epochs','batch','seed')}))
    return str(artifact)


def check_scout_training(device='cpu'):
    """Exercise actual YOLO26 gradients, export and TEMPO scoring without downloads."""
    from types import SimpleNamespace
    import torch
    from ultralytics.cfg import DEFAULT_CFG_DICT
    from tempo_scout_training import ScoutModel
    model = ScoutModel('yolo26n.yaml', nc=4, verbose=False).to(device)
    model.args = SimpleNamespace(**DEFAULT_CFG_DICT)
    batch = dict(img=torch.rand(2, 3, 64, 64, device=device),
                 batch_idx=torch.tensor([0, 1], device=device), cls=torch.tensor([[0.], [2.]], device=device),
                 bboxes=torch.tensor([[.5, .5, .2, .2], [.5, .5, .5, .5]], device=device),
                 unknown_cells=torch.ones(2, 64, 64, dtype=torch.bool, device=device))
    loss, _ = model(batch)
    if not torch.isfinite(loss).all():
        raise RuntimeError('Scout training produced a non-finite loss.')
    loss.sum().backward()
    for name in ('cv2', 'cv3', 'one2one_cv2', 'one2one_cv3'):
        branch = getattr(model.model[-1], name, None)
        gradients = [p.grad for p in branch.parameters() if p.grad is not None] if branch is not None else []
        if not gradients or any(not torch.isfinite(value).all() for value in gradients):
            raise RuntimeError('Scout training gradients failed for '+name+'.')
    import tempfile
    import cv2
    import numpy as np
    from tempo_training import export_yolo_for_inference
    from tempo_evaluation_metrics import benchmark_scout
    from tempo_preprocessing import metadata
    with tempfile.TemporaryDirectory(prefix='tempo-scout-check-') as directory:
        root = Path(directory)
        for folder in ('images', 'unknown', 'labels'):
            (root/'detect'/folder/'val').mkdir(parents=True)
        cv2.imwrite(str(root/'detect/images/val/check.png'), np.zeros((64,64),np.uint8))
        (root/'detect/unknown/val/check.json').write_text(json.dumps(dict(centers=[],groups=[])))
        (root/'detect/labels/val/check.txt').write_text('')
        (root/'dataset.json').write_text(json.dumps(dict(records=[dict(stem='check',name='check',preparation='check')],scout_confidence=.99)))
        source = root/'trained.pt'
        torch.save(dict(model=model,train_args={}),source)
        artifact = export_yolo_for_inference(source,root/'exported.pt',metadata=dict(
            role='scout',format='centroid-v1',preprocessing=metadata('P0'),box_width=16))
        scores = benchmark_scout(artifact,root,'val',device)
        json.dumps(scores,allow_nan=False)
        if len(scores['per_image']) != 1:
            raise RuntimeError('Scout scoring did not produce a field measurement.')
    return dict(loss=loss.detach().cpu().tolist(), criterion=type(model.criterion).__name__,scoring=scores['metric'])


def _once(source, old, new):
    if source.count(old) != 1:
        raise ValueError('A known runtime repair no longer matches its identified source.')
    return source.replace(old, new, 1)


def repair_worker(source):
    source = _once(source, '    if model_type in ("stardist", "cellpose"):\n',
        "    from tempo_training_runtime import resume_scout_candidate, record_scout_candidate\n"
        "    completed_scout = resume_scout_candidate(output, dataset, dataset_info, epochs, batch, baseline_hash, allow_legacy=True) if model_type == 'nano_scout' else None\n"
        "    if completed_scout:\n"
        "        print('Reusing completed scout training; scoring saved weights.', flush=True)\n"
        "        artifact = completed_scout\n"
        '    elif model_type in ("stardist", "cellpose"):\n')
    source = _once(source, "    if model_type == 'nano_scout':\n        from tempo_training import export_yolo_for_inference\n",
                   "    if model_type == 'nano_scout' and not completed_scout:\n        from tempo_training import export_yolo_for_inference\n")
    return _once(source, "    progress(model_type, 'validating')\n",
        "    if model_type == 'nano_scout' and not completed_scout:\n"
        "        record_scout_candidate(artifact, output, dataset, dataset_info, epochs, batch, baseline_hash)\n"
        "    progress(model_type, 'validating')\n")


# Exact revisions known to contain these defects. Other source is never rewritten.
REPAIRS = {
    'tempo_scout_training.py': ('6d8170a55174572e0f9635d84227a0834a65379525e8ebf2ec7a40f6de730080',
                              'yolo26-training-branches-v1'),
    'tempo_evaluation_metrics.py': ('ceb81cf04322b2d84bce942f8d164742518bc3479ee4e4f69996d85522a7481f',
                                  'scout-benchmark-clock-v1'),
    'train_yolo.py': ('bd72af07e5faeba7226a31667b0cccb9f86d3c6083b29ab8fc903a4e9187f643',
                      'scout-scoring-recovery-v1'),
    'notebooks/colab_runtime.py': ('3d259c70aecd4419a9a3657e6a08cbaf7d050b7cbf2eff1a814f2c79af4e5b31', 'audited-worker-launcher-v1'),
}


LOADER = '''"""Load only explicitly identified and archived TEMPO compatibility repairs."""
import hashlib, importlib.abc, importlib.machinery, importlib.util, json, sys
from pathlib import Path
support = Path(__file__).resolve().parent
engine = support.parent
failure = None
try:
    manifest = json.loads((support/'compatibility.json').read_text())
    for name, expected in manifest['support_files'].items():
        if hashlib.sha256((support/name).read_bytes()).hexdigest() != expected:
            raise ImportError('Evaluation runtime support failed checksum verification: '+name)
except Exception as error:
    failure = str(error)
    manifest = {'overrides':{}}
class TempoCompatibilityFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if failure and (fullname.startswith('tempo_') or fullname in ('train_yolo','notebooks.colab_runtime')):
            raise ImportError(failure)
        record = manifest['overrides'].get(fullname)
        if record is None:
            return None
        original = importlib.machinery.PathFinder.find_spec(fullname, path)
        expected = engine/record['original']
        if original is None or Path(original.origin).resolve() != expected.resolve():
            raise ImportError('Evaluation compatibility source belongs to another engine: '+fullname)
        if hashlib.sha256(expected.read_bytes()).hexdigest() != record['original_sha256']:
            raise ImportError('Frozen evaluation source changed: '+fullname)
        replacement = support/record['replacement']
        if hashlib.sha256(replacement.read_bytes()).hexdigest() != record['replacement_sha256']:
            raise ImportError('Evaluation compatibility repair changed: '+fullname)
        class RepairLoader(importlib.abc.Loader):
            def create_module(self, spec):
                return None
            def exec_module(self, module):
                exec(compile(replacement.read_bytes(), str(replacement), 'exec'), module.__dict__)
        return importlib.util.spec_from_file_location(fullname, expected, loader=RepairLoader())
sys.meta_path.insert(0, TempoCompatibilityFinder())
'''

WORKER_LAUNCHER = '''"""Execute an identified repair while preserving the frozen engine's paths."""
import hashlib, json, sys
from pathlib import Path
support = Path(__file__).resolve().parent
engine = support.parent
original = Path(sys.argv[1]).resolve()
manifest = json.loads((support/'compatibility.json').read_text())
record = manifest['overrides']['train_yolo']
replacement = support/record['replacement']
if (original != engine/'train_yolo.py'
        or hashlib.sha256(original.read_bytes()).hexdigest() != record['original_sha256']
        or hashlib.sha256(replacement.read_bytes()).hexdigest() != record['replacement_sha256']):
    raise ValueError('The recorded training worker repair failed verification.')
sys.argv = sys.argv[1:]
sys.path.insert(0, str(engine))
__file__ = str(original)
exec(compile(replacement.read_bytes(), str(replacement), 'exec'), globals())
'''


def execution_command(command, environment=None):
    """Route repaired native workers through their explicit archived launcher."""
    import os
    value = (os.environ if environment is None else environment).get('TEMPO_EXECUTION_RUNTIME')
    if not value or '--worker' not in command:
        return command
    root = Path(value).resolve()
    indexes = [index for index,item in enumerate(command)
               if isinstance(item,(str,Path)) and Path(item).name == 'train_yolo.py'
               and Path(item).resolve() == root/'train_yolo.py']
    if not indexes:
        return command
    manifest = json.loads((root/'runtime-support/compatibility.json').read_text())
    if 'train_yolo' not in manifest['overrides']:
        return command
    index = indexes[0]
    return [*command[:index], str(root/'runtime-support/execute_worker.py'), str(root/'train_yolo.py'), *command[index+1:]]


def compatibility_files(engine_sources, support_sources):
    """Build explicit repairs without changing the original engine archive."""
    files = dict(support_sources)
    manifest = dict(format='tempo-runtime-compatibility-v1', overrides={})
    for name, (expected, repair_id) in REPAIRS.items():
        original = engine_sources.get(name, b'')
        if hashlib.sha256(original).hexdigest() != expected:
            continue
        source = original.decode('utf-8')
        if name == 'tempo_scout_training.py':
            source = _once(source, 'return E2ELoss(self, loss_fn=ScoutLoss) if self.end2end else ScoutLoss(self)',
                "return E2ELoss(self, loss_fn=ScoutLoss) if getattr(self.model[-1], 'one2one_cv2', None) is not None else ScoutLoss(self)")
        elif name == 'tempo_evaluation_metrics.py':
            source = _once(source, 'import json\n', 'import json\nimport time\n')
        elif name == 'train_yolo.py':
            source = repair_worker(source)
        else:
            source = support_sources['colab_runtime.py'].decode()
        replacement = 'overrides/'+name
        files[replacement] = source.encode()
        manifest['overrides'][name[:-3].replace('/', '.')] = dict(id=repair_id, original=name,
            original_sha256=expected, replacement=replacement,
            replacement_sha256=hashlib.sha256(files[replacement]).hexdigest())
    files['sitecustomize.py'] = LOADER.encode()
    files['execute_worker.py'] = WORKER_LAUNCHER.encode()
    manifest['support_files'] = {name:hashlib.sha256(data).hexdigest() for name,data in files.items()}
    files['compatibility.json'] = json.dumps(manifest,sort_keys=True,indent=2).encode()
    return files, manifest


def verify_runtime_support(root, payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in archive.namelist():
            if name.startswith('runtime-support/'):
                path = Path(root)/name
                if not path.is_file() or path.read_bytes() != archive.read(name):
                    raise ValueError('The archived evaluation runtime changed: '+name)
