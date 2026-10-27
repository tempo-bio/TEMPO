"""Publish exact evaluated models and their training recipe after recorded approval.

Protocols may propose a model_release in their decision. A maintainer may also
attach a proposal to an older evaluation without editing its frozen report.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import zipfile

from tempo_evaluation import (artifact_path, checksum, file_checksum, plan_stages,
                              prefix, require_access, validate_plan, INTENT, TERMINAL,
                              evaluation_head, evaluation_read, evaluation_transaction)
from tempo_model_catalog import MODEL_FILES, MODEL_LABELS


def approved_source(hub, evaluation_id, revision=None):
    actor = require_access(hub)
    revision = revision or evaluation_head(hub)
    base = prefix(evaluation_id)
    plan = validate_plan(evaluation_read(hub,base+'/plan.json', revision))
    report = evaluation_read(hub,base+'/reports/'+plan_stages(plan)[-1]['id']+'.json', revision, {})
    original = dict(report)
    digest = original.pop('report_sha256', None)
    if not digest or hashlib.sha256(json.dumps(original, indent=2, allow_nan=False).encode()).hexdigest() != digest:
        raise ValueError('The evaluated report failed verification.')
    approval = evaluation_read(hub,'training/evaluation_adoptions/'+evaluation_id+'.json', revision, {})
    if (report.get('decision', {}).get('passed') is not True or
            approval.get('report_sha256') != digest or approval.get('evaluation_id') != evaluation_id):
        raise ValueError('Record the passing evaluation decision before publishing its models.')
    if plan['dataset_repo'] != hub.get_dataset_repo() or plan['models_repo'] != hub.get_models_repo():
        raise ValueError('The evaluation belongs to different repositories.')
    return dict(actor=actor, revision=revision, plan=plan, report=report, approval=approval,
                artifacts=evaluation_read(hub,base+'/artifacts.json', revision, {}))


def proposal_for(payload):
    stages = plan_stages(payload['plan'])
    report = payload['reports'].get(stages[-1]['id'], {})
    return report.get('decision', {}).get('model_release') or payload.get('model_proposal')


def validate_proposal(proposal, inventory):
    if not isinstance(proposal, dict) or proposal.get('format') != 'tempo-evaluated-model-release-v1':
        raise ValueError('This evaluation has no model-release proposal.')
    models = proposal.get('models')
    if not isinstance(models, dict) or not models or not set(models) <= set(MODEL_FILES):
        raise ValueError('Choose evaluated TEMPO model roles for publication.')
    paths = set()
    for role, row in models.items():
        if not isinstance(row, dict):
            raise ValueError('Each proposed model needs an evaluated artifact.')
        path = str(artifact_path(row.get('artifact', '')))
        entry = inventory.get(path)
        if path in paths or not entry or not isinstance(entry.get('size'), int) or entry['size'] <= 0:
            raise ValueError('A proposed model is missing from the saved evaluation.')
        paths.add(path)
        if row.get('sha256', entry['sha256']) != entry['sha256']:
            raise ValueError('A proposed checksum differs from its saved evaluation.')
    if not isinstance(proposal.get('training_receipt', {}), dict):
        raise ValueError('Proposed training settings must be an object.')
    json.dumps(proposal, allow_nan=False)
    return copy.deepcopy(proposal)


def save_proposal(hub, evaluation_id, proposal):
    """Attach an explicit selection to an older, already-approved evaluation."""
    def write(_, revision):
        source = approved_source(hub, evaluation_id, revision)
        selected = validate_proposal(proposal, source['artifacts'])
        target = prefix(evaluation_id)+'/model_proposal.json'
        existing = evaluation_read(hub,target, revision, {})
        if existing and existing != selected:
            raise ValueError('A different model selection is already attached to this evaluation.')
        return ({target: json.dumps(selected, indent=2).encode()} if not existing else {}), selected
    return evaluation_transaction(hub, write, 'Save evaluated model selection')


def prepare_adoption(hub, evaluation_id, *, workspace=None):
    """Download, verify and prepare a release without publishing or fitting."""
    from tempo_evaluation_runner import download_artifact
    from tempo_cellpose import bundle_metadata
    from tempo_training import DATASET_FORMAT
    source = approved_source(hub, evaluation_id)
    context = hub.token, hub.get_dataset_repo(), hub.get_models_repo()
    from tempo_evaluation import resolve_previous_assignments
    training_splits, assignment_resolution = resolve_previous_assignments(hub, hub._head())
    base = prefix(evaluation_id)
    proposal = validate_proposal(source['report']['decision'].get('model_release') or
                                evaluation_read(hub,base+'/model_proposal.json', source['revision'], {}), source['artifacts'])
    workspace = Path(workspace) if workspace else Path(tempfile.gettempdir())/'TEMPO-model-adoptions'/evaluation_id
    workspace.mkdir(parents=True, exist_ok=True)
    previous = hub.model_manifest()
    files = {}
    for role, row in proposal['models'].items():
        entry = source['artifacts'][row['artifact']]
        target = workspace/MODEL_FILES[role]
        if not target.exists():
            download_artifact(hub, base+'/workspace/'+row['artifact'], source['revision'], target, entry['sha256'])
        if file_checksum(target) != entry['sha256']:
            raise ValueError('A downloaded model differs from the evaluated checkpoint.')
        files[role] = str(target)
    if context != (hub.token,hub.get_dataset_repo(),hub.get_models_repo()):
        raise PermissionError('The account or repositories changed while preparing models.')
    manifest = copy.deepcopy(previous)
    manifest.pop('revision', None)
    models = manifest.setdefault('models', {})
    cell = bundle_metadata(files['cellpose']) if 'cellpose' in files else None
    if cell and cell.get('helper_sha256'):
        if 'stardist' not in files or file_checksum(files['stardist']) != cell['helper_sha256']:
            raise ValueError('Publish Cellpose together with its exact evaluated nuclear helper.')
        with zipfile.ZipFile(files['cellpose']) as archive:
            if hashlib.sha256(archive.read('nuclei.zip')).hexdigest() != cell['helper_sha256']:
                raise ValueError('The embedded Cellpose helper differs from its manifest.')
        with zipfile.ZipFile(files['stardist']) as archive:
            helper = json.loads(archive.read('model/tempo_nuclei.json'))
        if helper['preprocessing'] != cell['preprocessing']:
            raise ValueError('The coupled models use different preprocessing.')
    for role, path in files.items():
        models[MODEL_FILES[role]] = dict(sha256=file_checksum(path), version='evaluation-'+evaluation_id,
            architecture=role, input_format=DATASET_FORMAT, metrics=proposal['models'][role].get('metrics', {}),
            evaluation_id=evaluation_id, report_sha256=source['report']['report_sha256'],
            evaluation_basis='held-out-study-fields', scope=source['plan']['scope'])
    if cell:
        models[MODEL_FILES['cellpose']]['helper_sha256'] = cell.get('helper_sha256')
    dependency = models.get(MODEL_FILES['cellpose'], {}).get('helper_sha256')
    if dependency and models.get(MODEL_FILES['stardist'], {}).get('sha256') != dependency:
        raise ValueError('This release would separate Cellpose from its nuclear helper.')
    # Follow the normal publisher's retirement policy, retaining old Git revisions.
    if cell and dependency:
        models.pop('yolo26s-seg.pt', None)
        models.pop('stardist_cardio.zip', None)
    recipe = copy.deepcopy(previous.get('training_recipe') or
                          evaluation_read(hub,'training/architecture_validation.json', source['revision'], {}))
    old_scout_profile = recipe.get('scout_profile', recipe.get('profile', 'P0'))
    proposed = proposal.get('training_receipt', {})
    if cell and cell.get('preprocessing'):
        if proposed.get('profile', cell['preprocessing']['profile']) != cell['preprocessing']['profile']:
            raise ValueError('The training recipe differs from the evaluated Cellpose preprocessing.')
        if proposed.get('nuclear_assistance_passed', bool(dependency)) is not bool(dependency):
            raise ValueError('The training recipe differs from the evaluated helper configuration.')
        recipe.update(profile=cell['preprocessing']['profile'], nuclear_assistance_passed=bool(dependency))
    recipe.update(proposed)
    batches=recipe.get('batch_sizes',{})
    if (not isinstance(batches,dict) or not set(batches)<=set(MODEL_FILES) or
            any(type(value) is not int or not 1<=value<=64 for value in batches.values())):
        raise ValueError('Proposed model batch sizes must be integers from 1 to 64.')
    recipe.setdefault('scout_profile', old_scout_profile)
    recipe.setdefault('box_width', 16)
    recipe.setdefault('centroid_scout_passed', False)
    recipe.update(format='tempo-experiment-validation-v1', confirmation_sha256=source['report']['report_sha256'],
        scope=source['plan']['scope'], approved_fields=source['plan']['manifest']['images'],
        evaluation_id=evaluation_id, approved_by=source['approval']['approved_by'], approved_at=source['approval']['approved_at'])
    from tempo_preprocessing import metadata
    metadata(recipe.get('profile', 'P0')); metadata(recipe['scout_profile'])
    manifest['training_recipe'] = recipe
    manifest['adopted_evaluation'] = dict(id=evaluation_id, report_sha256=source['report']['report_sha256'], proposal_sha256=checksum(proposal))
    prepared = dict(evaluation_id=evaluation_id, dataset_repo=hub.get_dataset_repo(), models_repo=hub.get_models_repo(),
        report_sha256=source['report']['report_sha256'], proposal=proposal, previous_release=previous,
        manifest=manifest, files=files, prepared_by=source['actor'],
        scope_label=source['plan']['manifest'].get('scope_label',source['plan']['scope']),
        training_splits=training_splits, assignment_resolution=assignment_resolution)
    (workspace/'prepared.json').write_text(json.dumps(prepared, indent=2, allow_nan=False))
    return prepared


def publish_adoption(hub, prepared):
    """Publish one atomic model/recipe release, then mirror its adoption record."""
    from huggingface_hub import CommitOperationAdd
    from tempo_hub import commit_and_confirm
    from tempo_cloud import training_transaction
    source = approved_source(hub, prepared['evaluation_id'])
    context = hub.token, hub.get_dataset_repo(), hub.get_models_repo()
    if (source['report']['report_sha256'] != prepared['report_sha256'] or
            hub.get_dataset_repo() != prepared['dataset_repo'] or hub.get_models_repo() != prepared['models_repo']):
        raise ValueError('The evaluation, account repositories or model selection changed.')
    selection = validate_proposal(source['report']['decision'].get('model_release') or
                                 evaluation_read(hub,prefix(prepared['evaluation_id'])+'/model_proposal.json', source['revision'], {}), source['artifacts'])
    if selection != prepared['proposal']:
        raise ValueError('The prepared model selection changed.')
    for role, path in prepared['files'].items():
        expected=source['artifacts'][selection['models'][role]['artifact']]['sha256']
        if (file_checksum(path) != expected or
                prepared['manifest']['models'][MODEL_FILES[role]]['sha256'] != expected):
            raise ValueError('Prepared model weights changed. Publication was stopped.')
    if set(prepared['files']) != set(selection['models']):
        raise ValueError('The prepared model roles differ from the approved selection.')
    for name, row in prepared['previous_release']['models'].items():
        if name not in {MODEL_FILES[role] for role in prepared['files']} and name not in ('yolo26s-seg.pt','stardist_cardio.zip'):
            if prepared['manifest']['models'].get(name)!=row:
                raise ValueError('An unrelated published model changed in the prepared release.')
    current = hub.model_manifest()
    adoption = prepared['manifest']['adopted_evaluation']
    if current.get('adopted_evaluation') == adoption:
        if current['models'] != prepared['manifest']['models'] or current.get('training_recipe') != prepared['manifest']['training_recipe']:
            raise ValueError('The published adoption no longer matches this prepared release.')
        revision = current['revision']
    else:
        if current['revision'] != prepared['previous_release']['revision']:
            raise ValueError('Published models changed after preparation. Review and prepare again.')
        def idle():
            head=evaluation_head(hub)
            job=evaluation_read(hub,INTENT,head,{})
            data_head=hub._head()
            regular=hub._read('training/request.json',data_head,{})
            if job and job.get('status') not in TERMINAL:
                raise ValueError('Finish or cancel the active evaluation before publishing models.')
            if regular and hub._read('training/'+regular['id']+'/status.json',data_head,{}).get('phase') not in TERMINAL:
                raise ValueError('Finish or cancel regular training before publishing models.')
        idle()
        archive = 'evaluations/'+prepared['evaluation_id']
        provenance = dict(evaluation_id=prepared['evaluation_id'], report_sha256=prepared['report_sha256'],
            proposal=selection, approved=source['approval'], published_by=source['actor'],
            previous_revision=current['revision'], published_at=time.time())
        operations = [CommitOperationAdd(path_in_repo=MODEL_FILES[role], path_or_fileobj=path) for role, path in prepared['files'].items()]
        hub.api.preupload_lfs_files(repo_id=hub.get_models_repo(), repo_type='model', token=hub.token,
                                   additions=operations, free_memory=False)
        if context != (hub.token,hub.get_dataset_repo(),hub.get_models_repo()):
            raise PermissionError('The account or repositories changed during model upload.')
        idle()
        operations.extend([
            CommitOperationAdd(path_in_repo='model_manifest.json', path_or_fileobj=json.dumps(prepared['manifest'], indent=2, allow_nan=False).encode()),
            CommitOperationAdd(path_in_repo='training_splits.json', path_or_fileobj=json.dumps(prepared['training_splits'], indent=2).encode()),
            CommitOperationAdd(path_in_repo=archive+'/adoption.json', path_or_fileobj=json.dumps(provenance, indent=2, allow_nan=False).encode()),
            CommitOperationAdd(path_in_repo=archive+'/previous_manifest.json', path_or_fileobj=json.dumps(current, indent=2, allow_nan=False).encode())])
        result = commit_and_confirm(hub.api, repo_id=hub.get_models_repo(), repo_type='model', token=hub.token,
            parent_commit=current['revision'], operations=operations, commit_message='Adopt approved evaluated TEMPO models')
        revision = getattr(result, 'oid', None) or hub.model_manifest()['revision']
    receipt = dict(evaluation_id=prepared['evaluation_id'], report_sha256=prepared['report_sha256'],
                   model_revision=revision, proposal_sha256=checksum(selection), published_by=source['actor'],
                   models={MODEL_FILES[role]:prepared['manifest']['models'][MODEL_FILES[role]] for role in prepared['files']})
    def mirror(_, head):
        checked = approved_source(hub, prepared['evaluation_id'])
        if checked['report']['report_sha256'] != prepared['report_sha256']:
            raise ValueError('The evaluation changed after publication; model release remains preserved.')
        from tempo_evaluation import resolve_previous_assignments, assignment_policy, ASSIGNMENT_POLICY
        assignments, resolution = resolve_previous_assignments(hub, head)
        updates = {'training/split_assignments.json':json.dumps(assignments,indent=2).encode(),
                   ASSIGNMENT_POLICY:json.dumps(assignment_policy(assignments,resolution,prepared['manifest']['models']),indent=2).encode(),
                   'training/architecture_validation.json':json.dumps(prepared['manifest']['training_recipe'], indent=2).encode()}
        return updates, receipt
    def record(_, head):
        checked = approved_source(hub, prepared['evaluation_id'], head)
        if checked['report']['report_sha256'] != prepared['report_sha256']:
            raise ValueError('The evaluation changed after publication; model release remains preserved.')
        return {prefix(prepared['evaluation_id'])+'/deployment.json':json.dumps(receipt, indent=2).encode()},receipt
    evaluation_transaction(hub, record, 'Record evaluated model deployment')
    return training_transaction(hub, mirror, 'Record evaluated model release and training recipe')


def adoption_summary(prepared):
    labels = [MODEL_LABELS[role] for role in prepared['files']]
    recipe = prepared['manifest']['training_recipe']
    profiles={'P0':'Normalization only','P1':'CLAHE 2','P2':'Median + CLAHE 2','clahe4':'CLAHE 4',
              'gaussian1':'Gaussian σ=1 px','gaussian1-clahe2':'Gaussian σ=1 px → CLAHE 2'}
    return ('Publish '+', '.join(labels)+' to the shared model repository?\n\n'
            'Preprocessing: '+profiles.get(recipe.get('profile'),str(recipe.get('profile','P0')))+'\n'
            'Nuclear helper: '+('enabled' if recipe.get('nuclear_assistance_passed') else 'disabled')+'\n'
            'Scope: '+prepared.get('scope_label',str(recipe['scope']))+'\n\n'
            'This changes the shared model release and regular-training recipe. Install it through Settings → Update Models.')
