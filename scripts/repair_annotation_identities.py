"""Review and reconcile historical image aliases without deleting source data.

The default is a read-only plan. Applying requires a closed application, a
maintainer account, explicit reservation names and an unchanged remote revision.
Training, submissions, model weights and credentials are never modified here.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import uuid

from tempo_annotation_store import atomic_bytes, read_session, session_lock, update_session
from tempo_annotations import canonical_image_name, merge_image_records
from tempo_hub import (TempoHub, validate_lock_manifest, commit_and_confirm, CommitOperationAdd)


def file_receipt(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f'{path.name}: symbolic links need manual review; nothing was changed.')
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def prepare_repair(hub, local_dir, release_names=()):
    root = Path(local_dir)
    actor = hub.authenticated_account()
    if not hub.is_maintainer():
        raise PermissionError('An authenticated maintainer must review this dataset repair.')
    revision = hub._head()
    original = validate_lock_manifest(hub._read('lock_manifest.json', revision))
    manifest, updates = hub._prepare_manifest(original, revision)
    tested = {canonical_image_name(manifest, name) for name in release_names}
    released = []
    for requested in release_names:
        name = canonical_image_name(manifest, requested)
        if name not in manifest['images']:
            raise ValueError(f'{requested}: image is not registered; nothing was changed.')
        rec = manifest['images'].get(name, {})
        if rec.get('status') in ('available', 'approved'):
            continue
        if rec.get('status') != 'locked' or rec.get('submission_id'):
            raise ValueError(f'{requested}: a pending submission cannot be cleared by image cleanup.')
        if rec.get('claimed_by') not in (actor, 'default_collaborator'):
            raise ValueError(f'{requested}: reservation belongs to another identified account.')
        manifest['images'][name] = {key:value for key,value in rec.items()
                                    if key not in ('claimed_by','claimed_at','expires_at')}
        manifest['images'][name]['status'] = 'available'
        released.append(name)
    updates['lock_manifest.json'] = json.dumps(manifest, allow_nan=False).encode()
    old_session = read_session(root/'local_session.json')
    if old_session.get('dataset_repo') not in (None, hub.get_dataset_repo()):
        raise ValueError('The local session belongs to another dataset; nothing was changed.')
    old_annotations = json.loads((root/'universal_annotations.json').read_text()) if (root/'universal_annotations.json').exists() else {}
    records = merge_image_records({**old_annotations, **old_session.get('annotations', {})}, manifest)
    for name, record in records.items():
        identity = manifest['images'].get(name)
        if identity is None:
            raise ValueError(f'{name}: an unregistered local image needs manual review; nothing was changed.')
        if record.get('sync_pending') and record.get('draft_owner') not in (actor, None):
            raise ValueError(f'{name}: unsynced annotations have an unverified owner; nothing was changed.')
        record['image_id'] = identity['image_id']
        record['status'] = identity['status']
    moves = []
    canonical_sources = {}
    for name, identity in manifest['images'].items():
        target = root/name
        if target.exists():
            if file_receipt(target) != identity['sha256']:
                raise ValueError(f'{name}: local image checksum differs; nothing was changed.')
            canonical_sources[name] = name
    for alias, name in sorted(manifest.get('aliases', {}).items()):
        source = root/alias
        if not source.exists():
            continue
        if file_receipt(source) != manifest['images'][name]['sha256']:
            raise ValueError(f'{alias}: local alias contains different bytes; nothing was changed.')
        if name not in canonical_sources:
            canonical_sources[name] = alias
            moves.append({'source':alias,'canonical':name,'action':'promote'})
        else:
            if name in tested:
                moves.append({'source':alias,'canonical':name,'action':'quarantine'})
    for name, record in records.items():
        if record.get('boxes') and name not in canonical_sources:
            raise ValueError(f'{name}: annotated local image is missing; nothing was changed.')
    paths = ('universal_annotations.json', 'local_session.json', 'lock_manifest_cache.json')
    receipts = {name:file_receipt(root/name) for name in paths}
    return dict(id=uuid.uuid4().hex, repo=hub.get_dataset_repo(), revision=revision, actor=actor,
                manifest=manifest, updates=updates, records=records, old_session=old_session,
                local_receipts=receipts, released=released, moves=moves, local_dir=str(root.resolve()))


def repair_summary(plan):
    return {key:plan[key] for key in ('id','repo','revision','actor','released','moves','local_dir')} | {
        'canonical_images':len(plan['manifest']['images']),
        'historical_aliases':len(plan['manifest'].get('aliases',{})),
        'annotation_counts':{name:len(record.get('boxes',[])) for name,record in plan['records'].items()},
        'remote_images_deleted':0, 'training_started':False}


def apply_repair(hub, plan, *, app_closed=False):
    if not app_closed:
        raise ValueError('Close TEMPO completely before applying a local image repair.')
    if hub.authenticated_account() != plan['actor'] or not hub.is_maintainer():
        raise PermissionError('The authenticated maintainer account changed; prepare the repair again.')
    if hub._head() != plan['revision']:
        raise ValueError('The shared repository changed. Prepare and review a fresh repair plan.')
    root = Path(plan['local_dir'])
    for name, receipt in plan['local_receipts'].items():
        if file_receipt(root/name) != receipt:
            raise ValueError('Local annotations changed. Prepare and review a fresh repair plan.')
    backup = root/'annotation_recovery'/plan['id']
    for name in plan['local_receipts']:
        if (root/name).exists():
            atomic_bytes(backup/name, (root/name).read_bytes())
    atomic_bytes(backup/'repair-plan.json', json.dumps(repair_summary(plan),indent=2).encode())
    atomic_bytes(backup/'repair-result.json', json.dumps(dict(repair_summary(plan),
                 backup=str(backup),status='prepared'),indent=2).encode())
    prefix = f"identity_migrations/repair-{plan['id']}"
    updates = dict(plan['updates'])
    # Even an already indexed dataset retains its complete previous lease state.
    updates[f'{prefix}/lock_manifest.json'] = json.dumps(hub._read('lock_manifest.json',plan['revision'])).encode()
    updates[f'{prefix}/repair-plan.json'] = json.dumps(repair_summary(plan)).encode()
    operations = [CommitOperationAdd(path_in_repo=name,path_or_fileobj=data) for name,data in updates.items()]
    commit_and_confirm(hub.api,repo_id=plan['repo'],repo_type='dataset',token=hub.token,
                       parent_commit=plan['revision'],operations=operations,
                       commit_message='Reconcile verified image identities and clear listed test reservations',read_json=hub._read)
    revision = hub._head()
    confirmed = hub._read('lock_manifest.json',revision)
    if confirmed != plan['manifest']:
        raise ValueError('Shared repair differs from the reviewed plan. Local files were not changed.')
    for filename,data in updates.items():
        if filename.endswith('.json') and hub._read(filename,revision) != json.loads(data):
            raise ValueError(f'{filename}: repair confirmation failed. Local annotations were not changed.')
    atomic_bytes(backup/'repair-result.json', json.dumps(dict(repair_summary(plan),
                 backup=str(backup),confirmed_revision=revision,status='shared records confirmed'),indent=2).encode())
    # Publish any missing canonical image before a session can refer to it.
    # Promotion adds a verified link; source removal waits for the local save.
    with session_lock(root/'local_session.json'):
        for move in plan['moves']:
            if move['action'] == 'promote':
                source = root/move['source']; target = root/move['canonical']
                if file_receipt(source) != confirmed['images'][move['canonical']]['sha256']:
                    raise ValueError('An image changed during repair. Source images were preserved.')
                os.link(source,target)
    def transform(current):
        if current != plan['old_session']:
            raise ValueError('Local session changed during shared repair. Backups were retained; refresh the plan.')
        for name,receipt in plan['local_receipts'].items():
            if file_receipt(root/name) != receipt:
                raise ValueError('Local files changed during shared repair. Backups were retained; refresh the plan.')
        return dict(current,annotations=copy.deepcopy(plan['records']),collaborator_id=plan['actor'],
                    dataset_repo=plan['repo'],claimed_images=[name for name,record in confirmed['images'].items()
                                                            if hub._owned(record,plan['actor'])])
    update_session(root/'local_session.json',transform,
                   expected_revision=plan['old_session'].get('session_revision',0),
                   annotations_path=root/'universal_annotations.json')
    atomic_bytes(root/'lock_manifest_cache.json',json.dumps(confirmed,indent=2).encode())
    with session_lock(root/'local_session.json'):
        for move in plan['moves']:
            source = root/move['source']; canonical = root/move['canonical']
            digest = confirmed['images'][move['canonical']]['sha256']
            if file_receipt(source) != digest:
                raise ValueError('An image changed during repair. Remaining copies were preserved.')
            destination = canonical if move['action']=='promote' else backup/'redundant-images'/move['source']
            if move['action'] == 'quarantine' and destination.exists():
                raise FileExistsError('A repair destination already exists; source images were preserved.')
            if move['action']=='quarantine' and file_receipt(canonical) != digest:
                raise ValueError('The original image changed. The redundant copy was preserved.')
            if move['action'] == 'promote':
                if file_receipt(canonical) != digest:
                    raise ValueError('The promoted image changed. Source images were preserved.')
                source.unlink()
                continue
            destination.parent.mkdir(parents=True,exist_ok=True)
            # Same-filesystem publication refuses overwrite; source bytes remain recoverable.
            os.link(source,destination)
            source.unlink()
    result = dict(repair_summary(plan),confirmed_revision=revision,backup=str(backup),status='completed')
    atomic_bytes(backup/'repair-result.json',json.dumps(result,indent=2).encode())
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local-dir',default=str(Path(__file__).resolve().parents[1]/'datasets/universal_images'))
    parser.add_argument('--release-tested-reservations',nargs='*',default=[])
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--app-closed',action='store_true')
    args=parser.parse_args()
    hub=TempoHub();plan=prepare_repair(hub,args.local_dir,args.release_tested_reservations)
    result=apply_repair(hub,plan,app_closed=args.app_closed) if args.apply else repair_summary(plan)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
