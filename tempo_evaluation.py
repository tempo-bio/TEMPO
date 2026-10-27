"""Frozen, maintainer-owned evaluations of developer-supplied protocols."""
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
import types
import uuid

FORMAT='tempo-model-evaluation-v3'
INTENT='training/evaluation_request.json'
INDEX='training/evaluations/index.json'
ASSIGNMENT_POLICY='training/split_assignments_policy.json'
TERMINAL=('completed','failed','cancelled','disconnected')


def evaluation_head(hub):
    return hub._head(repo_type='model')


def evaluation_read(hub, name, revision, default=None):
    return hub._read(name, revision, default, repo_type='model')


def evaluation_transaction(hub, mutate, message, **options):
    from tempo_cloud import training_transaction
    return training_transaction(hub, mutate, message, repo_type='model', **options)

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('Invalid model evaluation identifier.')
    return value

def prefix(evaluation_id):
    return 'training/evaluations/' + identifier(evaluation_id)

def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()

def file_checksum(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def assignment_policy(assignments,resolution,models):
    return dict(format='tempo-image-assignment-ledger-v1',assignments_sha256=checksum(assignments),
                resolution=resolution,model_digests={name:row['sha256'] for name,row in models.items()})

def require_access(hub):
    hub.ensure_credentials()
    if not hub.is_maintainer(raise_on_error=True):
        raise PermissionError('Maintainer access is required for Model Evaluation.')
    return hub._require_actor()

def _legacy_assignment_resolution(hub, revision, assignments, saved, latest, release, aliases):
    """Disprove exposure only from pinned, approved-only input provenance.

    Historical split files assigned empty pool entries as well as actual inputs.
    Unknown assignments remain protected. Never infer non-exposure from today's
    annotations or from a run's success/failure state.
    """
    from tempo_training import APPROVAL_POLICY
    from tempo_training_groups import SPLIT_POLICY
    canonical=lambda name:aliases.get(name,name)
    inherited=latest.get('previous_splits',{})
    images=latest.get('images')
    if (release.get('approval_policy')!=APPROVAL_POLICY or latest.get('split_policy')!=SPLIT_POLICY
            or not isinstance(inherited,dict) or not isinstance(images,list)
            or not isinstance(latest.get('dataset_revision'),str)):
        return {}
    # A newer current model with different weights needs its own provenance.
    digests=lambda models:{name:row.get('sha256') for name,row in models.items()}
    if digests(release.get('models',{}))!=digests(latest.get('baseline_models',{})):
        return {}
    names={canonical(row['name']) for row in images}
    splits=latest.get('split_assignments',{})
    if len(names)!=latest.get('image_count') or names!={canonical(name) for name in splits}:
        raise ValueError('The previous training input list does not match its saved assignments.')
    inherited={canonical(name):role for name,role in inherited.items()}
    candidates={name for name in assignments if name not in names and name in inherited}
    if not candidates:return {}
    frozen=hub._read('universal_annotations.json',latest['dataset_revision'])
    if not isinstance(frozen,dict):raise ValueError('The previous training snapshot is unreadable.')
    frozen={canonical(name):row for name,row in frozen.items()}
    approved={name for name,row in frozen.items() if row.get('status')=='approved'}
    if approved!=names:
        raise ValueError('The previous training snapshot does not match its approved input list.')
    unannotated={name for name in candidates if name in frozen and frozen[name].get('status')!='approved'
            and not frozen[name].get('boxes')}
    if not unannotated:return {}
    # A later evaluation can reserve an image without fitting anything. Its
    # actual selected images remain protected; its inherited split map does not
    # prove that every entry was selected.
    protected_revision=evaluation_head(hub)
    index=evaluation_read(hub,INDEX,protected_revision,[])
    if not isinstance(index,list):raise ValueError('The saved evaluation index is unreadable.')
    priority={'test':0,'val':1,'train':2}
    actual={name:role for name,role in saved.items() if name in unannotated and role!=inherited[name]}
    protected=[]
    for entry in index:
        plan=validate_plan(evaluation_read(hub,prefix(entry['id'])+'/plan.json',protected_revision))
        for name,row in plan['manifest']['images'].items():
            name=canonical(name);role=row['split']
            if name in unannotated and (name not in actual or priority[role]>priority[actual[name]]):actual[name]=role
        protected.append(plan['id'])
    return dict(unannotated_assignments={name:inherited[name] for name in sorted(unannotated)},
                selected_assignments=actual,
                training_snapshot=latest['dataset_revision'],training_request=latest.get('id'),
                protected_evaluations=protected)


def resolve_previous_assignments(hub, revision):
    from tempo_training import APPROVAL_POLICY
    saved = hub._read('training/split_assignments.json', revision, {})
    if not isinstance(saved, dict):
        raise ValueError('Saved training assignments are unreadable.')
    previous = dict(saved)
    policy=hub._read(ASSIGNMENT_POLICY,revision,{})
    if policy and (policy.get('format')!='tempo-image-assignment-ledger-v1'
                   or policy.get('assignments_sha256')!=checksum(saved)):
        raise ValueError('Saved image assignments no longer match their recorded provenance. Review the training records before proceeding.')
    priority={'test':0,'val':1,'train':2}
    if any(role not in priority for role in previous.values()):
        raise ValueError('Invalid saved image role.')
    def merge(source):
        if not isinstance(source,dict):raise ValueError('Saved training assignments are unreadable.')
        for name,partition in source.items():
            if partition not in priority:raise ValueError('Invalid saved image role.')
            if name not in previous or priority[partition]>priority[previous[name]]:previous[name]=partition
    latest = hub._read('training/request.json', revision, {})
    merge(latest.get('split_assignments',{}))
    release = hub.model_manifest()
    model_digests={name:row['sha256'] for name,row in release['models'].items()}
    if release.get('approval_policy') == APPROVAL_POLICY and (not policy or policy.get('model_digests')!=model_digests):
        merge(hub._read('training_splits.json', release['revision'], {}, repo_type='model'))
    aliases=hub._read('lock_manifest.json',revision,{}).get('aliases',{})
    def canonical_roles(source):
        result={}
        for name,role in source.items():
            name=aliases.get(name,name)
            if name not in result or priority[role]>priority[result[name]]:result[name]=role
        return result
    canonical=canonical_roles(previous)
    if policy:return canonical,policy.get('resolution',{})
    resolution=_legacy_assignment_resolution(hub,revision,canonical,canonical_roles(saved),latest,release,aliases)
    for name in resolution.get('unannotated_assignments',{}):canonical.pop(name)
    canonical.update(resolution.get('selected_assignments',{}))
    return canonical,resolution


def previous_assignments(hub, revision):
    return resolve_previous_assignments(hub,revision)[0]

def cancel_evaluation(hub, evaluation_id):
    require_access(hub)
    def cancel(_, revision):
        job = evaluation_read(hub,INTENT, revision, {})
        if job.get('evaluation_id') != identifier(evaluation_id) or job.get('status') in TERMINAL:
            return {}, False
        files = {prefix(evaluation_id) + '/control.json': json.dumps(dict(cancel=True, job_id=job['id'])).encode()}
        if job['status'] == 'queued':
            job['status'] = 'cancelled'
            files[INTENT] = json.dumps(job).encode()
            files[prefix(evaluation_id) + '/state.json'] = json.dumps(dict(phase='cancelled',stage=job['phase'],updated_at=time.time())).encode()
        return files, True
    return evaluation_transaction(hub, cancel, 'Cancel model evaluation')

def clear_evaluation_intent(hub):
    require_access(hub)
    def clear(_, revision):
        job = evaluation_read(hub,INTENT, revision, {})
        if job and job.get('status') not in TERMINAL:
            raise ValueError('Cancel or finish Model Evaluation before returning to regular training.')
        return {INTENT: b'{}'}, True
    return evaluation_transaction(hub, clear, 'Return notebook to regular training')

def recover_evaluation(hub, evaluation_id):
    """Keep the old job fenced out when a stopped notebook is requeued."""
    require_access(hub)
    def recover(_, revision):
        job = evaluation_read(hub,INTENT, revision, {})
        state = evaluation_read(hub,prefix(evaluation_id) + '/state.json', revision, {})
        if job.get('evaluation_id') != evaluation_id or job.get('status') != 'running':
            raise ValueError('This evaluation is not recorded as running.')
        if time.time() - state.get('updated_at', time.time()) <= 120:
            raise ValueError('Colab reported within the last two minutes. Stop its execution before recovery.')
        job['status'] = 'disconnected'
        state.update(phase='disconnected', updated_at=time.time())
        return {INTENT: json.dumps(job).encode(), prefix(evaluation_id) + '/state.json': json.dumps(state).encode()}, True
    return evaluation_transaction(hub, recover, 'Recover stopped model evaluation')

def artifact_path(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value or path.as_posix() != value:
        raise ValueError('Invalid model evaluation artifact path.')
    return path


def frozen_records(hub,revision):
    from tempo_nuclei import attach_supplement,migrate_record
    from tempo_training import select_approved_annotations
    raw=hub._read('universal_annotations.json',revision,{})
    approved={name:migrate_record(row) for name,row in raw.items() if row.get('status')=='approved'}
    if not approved:return {}
    records=select_approved_annotations(approved)
    supplements=hub._read('nuclear_annotations.json',revision,{})
    return {name:attach_supplement(row,supplements.get(row.get('image_id'))) for name,row in records.items()}


def prepare_evaluation(hub,root,*,protocol_path,parameters=None,gpu='Any CUDA GPU',name=''):
    from tempo_cloud import archive_sources
    from tempo_evaluation_protocol import read_package,parameter_values,unpack_package,load_protocol,active_stages
    actor=require_access(hub)
    if gpu not in ('A100','Any CUDA GPU'):raise ValueError('Choose the required Colab GPU.')
    descriptor,payload=read_package(protocol_path);values=parameter_values(descriptor,parameters)
    stages=active_stages(descriptor,values)
    revision=hub._head();records=frozen_records(hub,revision);previous,assignment_resolution=resolve_previous_assignments(hub,revision)
    with tempfile.TemporaryDirectory() as directory:
        unpack_package(payload,directory,hashlib.sha256(payload).hexdigest())
        with load_protocol(directory,descriptor) as protocol:
            manifest=protocol.prepare(types.SimpleNamespace(records=copy.deepcopy(records),previous_splits=copy.deepcopy(previous),
                parameters=copy.deepcopy(values),stages=copy.deepcopy(stages),dataset_revision=revision))
    if not isinstance(manifest,dict) or not isinstance(manifest.get('images'),dict) or not manifest.get('scope'):
        raise ValueError('Protocol preparation must return images and the scope of its evidence.')
    selected={};groups={};identities=set();priority={'test':0,'val':1,'train':2}
    for image,row in manifest['images'].items():
        artifact_path(image)
        if image not in records:raise ValueError('A protocol selected an image outside the approved snapshot.')
        role=row.get('split');unit=row.get('unit')
        if role not in priority or not isinstance(unit,str) or not unit:raise ValueError('Each selected image needs a role and an independent-unit identifier.')
        if image in previous and priority[role]<priority[previous[image]]:raise ValueError('Prior training or validation exposure cannot become reserved evaluation evidence.')
        if unit in groups and groups[unit]!=role:raise ValueError('Related images cannot cross dataset roles.')
        groups[unit]=role
        identity=records[image].get('image_id')
        if not isinstance(identity,str) or not re.fullmatch(r'sha256:[a-f0-9]{64}',identity) or identity in identities:
            raise ValueError('Selected images need unique verified image identities.')
        identities.add(identity);selected[image]=records[image]
    bundle=archive_sources(root);release=hub.model_manifest()
    plan=dict(format=FORMAT,id=uuid.uuid4().hex,name=name.strip()[:100] or descriptor['name'],created_at=time.time(),
        created_by=actor,dataset_repo=hub.get_dataset_repo(),models_repo=hub.get_models_repo(),dataset_revision=revision,
        protocol=descriptor,stages=stages,protocol_sha256=hashlib.sha256(payload).hexdigest(),parameters=values,gpu=gpu,
        records_sha256=checksum(selected),manifest=manifest,scope=manifest['scope'],previous_splits=previous,
        assignment_resolution=assignment_resolution,
        partitions={role:sum(row['split']==role for row in manifest['images'].values()) for role in priority},
        bundle_sha256=hashlib.sha256(bundle).hexdigest(),baseline_revision=release['revision'],baseline_models=release['models'])
    plan['plan_sha256']=checksum(plan)
    return plan,bundle,payload


def validate_plan(plan):
    from tempo_evaluation_protocol import validate_descriptor,parameter_values,active_stages
    if not isinstance(plan,dict) or plan.get('format')!=FORMAT:
        raise ValueError('This evaluation uses an unsupported plan version; create it with a versioned protocol package.')
    identifier(plan['id']);original=dict(plan);digest=original.pop('plan_sha256',None)
    if checksum(original)!=digest:raise ValueError('The frozen evaluation configuration changed.')
    validate_descriptor(plan['protocol']);parameter_values(plan['protocol'],plan['parameters'])
    if plan.get('stages')!=active_stages(plan['protocol'],plan['parameters']):raise ValueError('The frozen stage selection differs from its settings.')
    return plan


def save_evaluation(hub,plan,bundle,protocol_bundle):
    from tempo_cloud import training_transaction
    actor=require_access(hub);validate_plan(plan)
    if (plan['created_by']!=actor or (plan['dataset_repo'],plan['models_repo'])!=(hub.get_dataset_repo(),hub.get_models_repo()) or
        hashlib.sha256(bundle).hexdigest()!=plan['bundle_sha256'] or hashlib.sha256(protocol_bundle).hexdigest()!=plan['protocol_sha256']):
        raise ValueError('The evaluation account, repositories or frozen sources changed.')
    context=hub.token,hub.get_dataset_repo(),hub.get_models_repo()
    base=prefix(plan['id'])
    if evaluation_read(hub,base+'/plan.json',evaluation_head(hub),{}):
        raise ValueError('This evaluation already exists.')
    ledger=dict(plan['previous_splits']);ledger.update({name:row['split'] for name,row in plan['manifest']['images'].items()})
    def reserve(_,revision):
        if previous_assignments(hub,revision)!=plan['previous_splits']:
            raise ValueError('Image exposure changed; prepare the evaluation again.')
        return {'training/split_assignments.json':json.dumps(ledger).encode(),
                ASSIGNMENT_POLICY:json.dumps(assignment_policy(ledger,plan.get('assignment_resolution',{}),plan['baseline_models'])).encode()},True
    # Reserve exposure before publishing executable content. A failed second
    # commit conservatively retains reservations; it never enables unreserved work.
    training_transaction(hub,reserve,'Reserve model evaluation images')
    if context!=(hub.token,hub.get_dataset_repo(),hub.get_models_repo()):
        raise PermissionError('The account or repositories changed during the operation.')
    def save(_,revision):
        if evaluation_read(hub,base+'/plan.json',revision,{}):raise ValueError('This evaluation already exists.')
        if previous_assignments(hub,hub._head())!=ledger:raise ValueError('Image exposure changed; prepare the evaluation again.')
        entry={key:plan[key] for key in ('id','name','created_at','created_by','partitions','scope')}
        return {base+'/plan.json':json.dumps(plan).encode(),base+'/bundle.zip':bundle,base+'/protocol.zip':protocol_bundle,
            base+'/state.json':json.dumps(dict(phase='ready',updated_at=time.time())).encode(),
            INDEX:json.dumps([entry,*evaluation_read(hub,INDEX,revision,[])]).encode()},plan
    return evaluation_transaction(hub,save,'Create frozen model evaluation')


def list_evaluations(hub):
    require_access(hub);return evaluation_read(hub,INDEX,evaluation_head(hub),[])


def load_evaluation(hub,evaluation_id):
    require_access(hub);revision=evaluation_head(hub);base=prefix(evaluation_id)
    plan=validate_plan(evaluation_read(hub,base+'/plan.json',revision))
    reports={stage['id']:evaluation_read(hub,base+'/reports/'+stage['id']+'.json',revision,{}) for stage in plan_stages(plan)}
    return dict(plan=plan,state=evaluation_read(hub,base+'/state.json',revision,{}),reports=reports,revision=revision,intent=evaluation_read(hub,INTENT,revision,{}),
                model_proposal=evaluation_read(hub,base+'/model_proposal.json',revision,{}),deployment=evaluation_read(hub,base+'/deployment.json',revision,{}))


def next_stage(payload):
    return next((stage for stage in plan_stages(payload['plan']) if not payload['reports'].get(stage['id'])),None)


def plan_stages(plan):
    return plan.get('stages',plan['protocol']['stages'])


def queue_evaluation(hub,evaluation_id,phase):
    actor=require_access(hub)
    def queue(_,revision):
        base=prefix(evaluation_id);plan=validate_plan(evaluation_read(hub,base+'/plan.json',revision))
        stages=plan_stages(plan);ids=[stage['id'] for stage in stages]
        if phase not in ids:raise ValueError('Choose a stage defined by this protocol.')
        previous=evaluation_read(hub,INTENT,revision,{})
        if previous and previous.get('status') not in TERMINAL:raise ValueError('A model evaluation is already queued or running.')
        data_revision=hub._head()
        regular=hub._read('training/request.json',data_revision,{})
        if regular and hub._read('training/'+regular['id']+'/status.json',data_revision,{}).get('phase') not in TERMINAL:
            raise ValueError('A regular training run is active.')
        if evaluation_read(hub,base+'/reports/'+phase+'.json',revision,{}):raise ValueError('This stage is complete and cannot be repeated.')
        if any(not evaluation_read(hub,base+'/reports/'+stage['id']+'.json',revision,{}) for stage in stages[:ids.index(phase)]):
            raise ValueError('Complete the preceding protocol stages first.')
        job=dict(trust='protected-model-repository-v1',mode='model-evaluation',id=uuid.uuid4().hex,evaluation_id=evaluation_id,phase=phase,status='queued',requested_by=actor,
            dataset_repo=plan['dataset_repo'],models_repo=plan['models_repo'],gpu=plan['gpu'],
            plan_sha256=plan['plan_sha256'],bundle_sha256=plan['bundle_sha256'],protocol_sha256=plan['protocol_sha256'])
        state=evaluation_read(hub,base+'/state.json',revision,{})
        state.update(phase='awaiting_colab',stage=phase,job_id=job['id'],updated_at=time.time(),error=None)
        return {INTENT:json.dumps(job).encode(),base+'/state.json':json.dumps(state).encode()},job
    return evaluation_transaction(hub,queue,'Queue model evaluation stage')


def approve_evaluation(hub,evaluation_id):
    actor=require_access(hub)
    def approve(_,revision):
        base=prefix(evaluation_id);plan=validate_plan(evaluation_read(hub,base+'/plan.json',revision))
        report=evaluation_read(hub,base+'/reports/'+plan_stages(plan)[-1]['id']+'.json',revision,{})
        original=dict(report);digest=original.pop('report_sha256',None)
        if not digest or hashlib.sha256(json.dumps(original,indent=2,allow_nan=False).encode()).hexdigest()!=digest:
            raise ValueError('The saved report failed verification.')
        decision=report.get('decision',{})
        if decision.get('passed') is not True:raise ValueError('The protocol did not produce a passing decision.')
        receipt=dict(evaluation_id=evaluation_id,protocol=plan['protocol']['id'],protocol_version=plan['protocol']['version'],
            scope=plan['scope'],approved_by=actor,approved_at=time.time(),report_sha256=digest,decision=decision)
        state=evaluation_read(hub,base+'/state.json',revision,{});state['adoption_recorded']=True
        updates={'training/evaluation_adoptions/'+evaluation_id+'.json':json.dumps(receipt).encode(),base+'/state.json':json.dumps(state).encode()}
        if decision.get('training_receipt') is not None:
            settings=decision['training_receipt']
            if not isinstance(settings,dict):raise ValueError('A proposed training receipt must be an object.')
            release=hub.model_manifest()
            settings=dict(settings,confirmation_sha256=digest,scope=plan['scope'],approved_fields=plan['manifest']['images'],
                          approved_by=actor,approved_at=receipt['approved_at'],evaluation_id=evaluation_id,
                          base_model_revision=release['revision'],base_models_sha256=checksum(release['models']))
            updates['training/architecture_validation.json']=json.dumps(settings).encode()
        return updates,receipt
    context=hub.token,hub.get_dataset_repo(),hub.get_models_repo()
    receipt=evaluation_transaction(hub,approve,'Record model evaluation decision')
    if receipt['decision'].get('training_receipt') is not None:
        from tempo_cloud import training_transaction
        settings=evaluation_read(hub,'training/architecture_validation.json',evaluation_head(hub))
        if context!=(hub.token,hub.get_dataset_repo(),hub.get_models_repo()):
            raise PermissionError('The account or repositories changed during the operation.')
        training_transaction(hub,lambda _,revision:({'training/architecture_validation.json':json.dumps(settings).encode()},True),
                             'Share approved training settings')
    return receipt


def load_evidence(hub,payload,image_name,roles,stage=None):
    from huggingface_hub import hf_hub_download
    require_access(hub);plan=payload['plan'];base=prefix(plan['id'])
    stage=stage or next(key for key in reversed(payload['reports']) if payload['reports'][key])
    report=payload['reports'][stage];preview=next(row for row in report.get('previews',[]) if row['name']==image_name)
    inventory=evaluation_read(hub,base+'/artifacts.json',payload['revision'],{});paths=[]
    for role in roles:
        relative=str(artifact_path(preview['views'][role]));entry=inventory.get(relative)
        if not entry or entry['size']>64*1024*1024:raise ValueError('This saved image is unavailable.')
        path=hf_hub_download(repo_id=plan['models_repo'],repo_type='model',revision=payload['revision'],filename=base+'/workspace/'+relative,token=hub.token)
        if file_checksum(path)!=entry['sha256']:raise ValueError('A saved image failed checksum verification.')
        paths.append(path)
    return paths
