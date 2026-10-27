"""Offline regression tests for cloud transactions; no real credentials or remote writes."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
import tempo_hub as hub_module
from tempo_annotations import normalize_record, validate_annotations
from tempo_hub import TempoHub


def annotations():
    return {f'{i}.png': {'width':100,'height':100,'coordinate_space':'normalized','boxes':[
        {'class_id':1 if j==0 else 0,'polygon':[[.1,.1],[.2,.1],[.2,.2],[.1,.2]]} for j in range(10)]} for i in range(10)}


class MemoryRepo:
    def __init__(self, files):
        self.files = copy.deepcopy(files)
        self.sha = 1
        self.conflict = None
        self.fail = False
        self.commits = []
    def repo_info(self, **kwargs):
        return types.SimpleNamespace(sha=str(self.sha),private=True)
    def create_commit(self, parent_commit, operations, **kwargs):
        if self.fail:
            raise ConnectionError('offline')
        if self.conflict:
            callback,self.conflict = self.conflict,None
            callback(self)
            self.sha += 1
        if parent_commit != str(self.sha):
            error = RuntimeError('conflict'); error.response = types.SimpleNamespace(status_code=409)
            raise error
        updated = copy.deepcopy(self.files)
        for operation in operations:
            value = operation.path_or_fileobj
            updated[operation.path_in_repo] = json.loads(value) if isinstance(value, bytes) else value
        self.files = updated
        self.sha += 1
        self.commits.append([op.path_in_repo for op in operations])


class HubTests(unittest.TestCase):
    def test_maintainer_approves_only_selected_local_images(self):
        selected = {n:r for n,r in annotations().items() if n in ('0.png','1.png','2.png')}
        self.repo.list_repo_files = lambda **kw: []
        self.repo.files['universal_annotations.json'] = {'draft.png':{'boxes':[]}}
        paths = {}
        for name in selected:
            path = Path(self.tmp.name)/name
            path.write_bytes(('image-'+name).encode())
            paths[name] = str(path)
        with patch.object(self.hub, 'is_maintainer', return_value=True):
            count = self.hub.approve_local_images(selected, paths, 'Alice')
        self.assertEqual(count, 3)
        master = self.repo.files['universal_annotations.json']
        self.assertEqual({n for n,r in master.items() if r.get('status') == 'approved'}, set(selected))
        self.assertEqual(master['draft.png']['boxes'], [])
    def test_local_approval_rejects_other_lease_without_commit(self):
        self.seed(status='locked', owner='Bob')
        self.repo.list_repo_files = lambda **kw: []
        selected = {'0.png':annotations()['0.png']}
        with patch.object(self.hub, 'is_maintainer', return_value=True):
            with self.assertRaisesRegex(ValueError, 'lease'):
                self.hub.approve_local_images(selected, {'0.png':'/fake/0.png'}, 'Alice')
        self.assertEqual(self.repo.commits, [])
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        self.hub = TempoHub()
        self.hub.token = 'fake'
        # Transaction fixtures use explicit fake principals and filename records.
        # Verified account/content identity is exercised in test_annotation_identity.
        self.hub._require_actor = lambda requested=None: requested or 'FixtureReviewer'
        self.hub.authenticated_account = lambda: 'FixtureReviewer'
        self.hub._prepare_manifest = lambda manifest, revision: (manifest, {})
        self.repo = MemoryRepo({'lock_manifest.json':{'version':1,'images':{}}})
        self.hub.api = self.repo
        self.hub._read = lambda name, revision, default=None, **kw:copy.deepcopy(self.repo.files.get(name, default))
        patch.object(hub_module,'LOCAL_LOCK_CACHE',str(Path(self.tmp.name)/'cache.json')).start()
        patch.object(hub_module,'UNIVERSAL_DIR',self.tmp.name).start()
        patch.object(hub_module,'LOCAL_SESSION_PATH',str(Path(self.tmp.name)/'session.json')).start()
    def seed(self, status='available', owner=None):
        self.repo.files['lock_manifest.json']['images'] = {name:dict(status=status,claimed_by=owner,expires_at=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat()) for name in annotations()}
    def test_competing_claim_rechecks_new_revision(self):
        self.seed()
        self.repo.conflict = lambda repo:repo.files['lock_manifest.json']['images']['0.png'].update(status='locked',claimed_by='Bob')
        claimed,conflicts = self.hub.claim_batch(['0.png'],'Alice')
        self.assertEqual(claimed,[]); self.assertEqual(conflicts,['0.png'])
        self.assertEqual(self.repo.files['lock_manifest.json']['images']['0.png']['claimed_by'],'Bob')
    def test_failed_remote_claim_never_succeeds_or_updates_cache(self):
        self.seed(); self.repo.fail=True
        with self.assertRaises(ConnectionError): self.hub.claim_batch(['0.png'],'Alice')
        self.assertFalse(Path(hub_module.LOCAL_LOCK_CACHE).exists())
    def test_lost_claim_acknowledgement_is_confirmed_without_second_commit(self):
        self.seed()
        original = self.repo.create_commit
        def accepted_without_response(**kwargs):
            original(**kwargs)
            raise ConnectionError('Response lost after server commit')
        self.repo.create_commit = accepted_without_response
        self.assertEqual(self.hub.claim_batch(['0.png'], 'Alice'), (['0.png'], []))
        self.assertEqual(len(self.repo.commits), 1)
        self.assertEqual(json.loads(Path(hub_module.LOCAL_LOCK_CACHE).read_text())['images']['0.png']['claimed_by'], 'Alice')

    def test_unreadable_confirmation_preserves_uncertain_error_and_cache(self):
        self.seed()
        original = self.repo.create_commit
        original_read = self.hub._read
        response_lost = []
        def read(*args, **kwargs):
            if response_lost:
                raise ConnectionError('Cannot reconcile')
            return original_read(*args, **kwargs)
        self.hub._read = read
        def accepted_without_response(**kwargs):
            original(**kwargs)
            response_lost.append(True)
            raise ConnectionError('Response lost after server commit')
        self.repo.create_commit = accepted_without_response
        with self.assertRaisesRegex(ConnectionError, 'Response lost'):
            self.hub.claim_batch(['0.png'], 'Alice')
        self.assertEqual(len(self.repo.commits), 1)
        self.assertFalse(Path(hub_module.LOCAL_LOCK_CACHE).exists())

    def test_partial_confirmation_cannot_report_submission_success(self):
        self.seed('locked', 'Alice')
        original = self.repo.create_commit
        def accepted_then_changed(**kwargs):
            original(**kwargs)
            name = next(n for n in self.repo.files if n.endswith('/metadata.json'))
            self.repo.files[name]['collaborator_id'] = 'Changed remotely'
            raise ConnectionError('Response lost after server commit')
        self.repo.create_commit = accepted_then_changed
        ok, detail = self.hub.upload_submission('Alice', list(annotations()), annotations(), {})
        self.assertFalse(ok)
        self.assertIn('Response lost', detail)
        self.assertEqual(len(self.repo.commits), 1)
    def test_no_offline_claim(self):
        self.hub.token=None
        with self.assertRaises(RuntimeError): self.hub.claim_batch(['0.png'],'Alice')
    def test_unclassified_outlines_sync_as_draft_but_cannot_be_submitted(self):
        self.seed('locked', 'Alice')
        draft = annotations()['0.png']
        draft['boxes'][0]['requires_viability_review'] = True
        self.hub.sync_annotation_drafts('Alice', {'0.png': draft})
        self.assertTrue(self.repo.files['annotation_drafts.json']['0.png']['boxes'][0]['requires_viability_review'])
        ok, error = self.hub.upload_submission('Alice', ['0.png'], {'0.png': draft}, {})
        self.assertFalse(ok)
        self.assertIn('classify all unclassified', error)

    def test_release_retains_synced_draft_and_rejects_other_owners_and_submissions(self):
        self.seed('locked', 'Alice')
        self.hub.sync_annotation_drafts('Alice', {'0.png': annotations()['0.png']})
        with patch.object(self.hub, 'is_maintainer', return_value=False):
            with self.assertRaises(ValueError):
                self.hub.release_claim(['0.png'], 'Bob')
            self.hub.release_claim(['0.png'], 'Alice')
            record = self.repo.files['lock_manifest.json']['images']['0.png']
            self.assertEqual(record['status'], 'available')
            self.assertNotIn('claimed_by', record)
            self.assertEqual(self.repo.files['annotation_drafts.json']['0.png']['boxes'], annotations()['0.png']['boxes'])
            self.seed('submitted', 'Alice')
            with self.assertRaises(ValueError):
                self.hub.release_claim(['0.png'], 'Alice')
    def test_only_approved_is_latched(self):
        self.seed('approved','Alice')
        self.assertEqual(self.hub.claim_batch(['0.png'],'Alice')[0],[])
        with self.assertRaises(ValueError): self.hub.renew_claim(['0.png'],'Alice')
        with self.assertRaises(ValueError): self.hub.sync_annotation_drafts('Alice', {'0.png':annotations()['0.png']})
        self.seed('submitted','Alice')
        self.assertTrue(self.hub._owned(self.repo.files['lock_manifest.json']['images']['0.png'], 'Alice'))

    def test_renewal_keeps_pending_submission_and_uses_seven_days(self):
        self.seed('submitted', 'Alice')
        original = self.repo.files['lock_manifest.json']['images']['0.png']
        original['submission_id'] = 'pending-review'
        self.hub.renew_claim(['0.png'], 'Alice', ttl_days=30)
        record = self.repo.files['lock_manifest.json']['images']['0.png']
        self.assertEqual(record['status'], 'submitted')
        self.assertEqual(record['submission_id'], 'pending-review')
        self.assertEqual(datetime.fromisoformat(record['expires_at']) - datetime.fromisoformat(record['claimed_at']), timedelta(days=7))
    def test_expired_adoption_and_original_submission_denial(self):
        self.seed('locked','Alice')
        self.repo.files['lock_manifest.json']['images']['0.png']['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        self.assertEqual(self.hub.claim_batch(['0.png'],'Bob')[0],['0.png'])
        self.assertEqual(self.hub.check_orphan_status('0.png','Alice'),'adopted_by_other')
        self.assertFalse(self.hub.upload_submission('Alice',list(annotations()),annotations(),{})[0])
        with self.assertRaises(ValueError): self.hub.renew_claim(['0.png'],'Alice')
    def test_submission_and_locks_are_one_commit(self):
        self.seed('locked','Alice')
        ok,sid = self.hub.upload_submission('Alice',list(annotations()),annotations(),{})
        self.assertTrue(ok,sid)
        self.assertEqual(len(self.repo.commits),1)
        self.assertEqual(len(self.repo.commits[0]),4)
        self.assertFalse(self.hub.upload_submission('Alice',list(annotations()),annotations(),{})[0])
    def submitted(self, user='Lab_Harvard_Smith'):
        self.seed('locked',user)
        ok,sid = self.hub.upload_submission(user,list(annotations()),annotations(),{})
        self.assertTrue(ok,sid)
        self.hub.is_maintainer=lambda:True
        return sid
    def test_merge_preserves_remote_master_and_edits(self):
        sid=self.submitted()
        self.repo.files['universal_annotations.json']={'previous.png':{'status':'approved'}}
        edited=annotations(); edited['0.png']['boxes'][0]['class_id']=0
        ok,msg=self.hub.merge_submission_to_master(sid,edited)
        self.assertTrue(ok,msg)
        master=self.repo.files['universal_annotations.json']
        self.assertIn('previous.png',master)
        self.assertEqual(master['0.png']['boxes'][0]['class_id'],0)
        self.assertFalse(self.hub.merge_submission_to_master(sid)[0])
        self.assertEqual(len(self.repo.commits[-1]),5)
        self.assertIn('nuclear_annotations.json',self.repo.commits[-1])
    def test_reject_preserves_full_author_and_feedback(self):
        sid=self.submitted()
        self.assertTrue(self.hub.reject_submission(sid,'Refine ends')[0])
        rec=self.repo.files['lock_manifest.json']['images']['0.png']
        self.assertEqual(rec['claimed_by'],'Lab_Harvard_Smith')
        self.assertEqual(rec['feedback'],'Refine ends')
        self.assertFalse(self.hub.merge_submission_to_master(sid)[0])
    def test_pending_edit_updates_submission_and_adoptable_draft(self):
        sid = self.submitted('Alice')
        changed = annotations()['0.png']; changed['boxes'].pop()
        self.hub.sync_annotation_drafts('Alice', {'0.png':changed})
        submitted = self.repo.files[f'submissions/{sid}/annotations.json']['0.png']
        self.assertEqual(len(submitted['boxes']), 9)
        self.assertEqual(self.repo.files['annotation_drafts.json']['0.png'], submitted)
        self.assertEqual(self.repo.files['lock_manifest.json']['images']['0.png']['status'], 'submitted')

    def test_adoption_inherits_pending_edits_and_preserves_submission(self):
        sid = self.submitted('Alice')
        changed = annotations()['0.png']; changed['boxes'].pop()
        self.hub.sync_annotation_drafts('Alice', {'0.png':changed})
        self.repo.files['lock_manifest.json']['images']['0.png']['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        self.hub.download_image = lambda name, revision=None: None
        self.hub.checkout_batch(['0.png'], 'Bob')
        rec = self.repo.files['lock_manifest.json']['images']['0.png']
        self.assertEqual((rec['claimed_by'],rec['submission_id'],rec['status']), ('Bob',sid,'submitted'))
        session = json.loads(Path(hub_module.LOCAL_SESSION_PATH).read_text())
        self.assertEqual(len(session['annotations']['0.png']['boxes']),9)
        with self.assertRaises(ValueError): self.hub.sync_annotation_drafts('Alice', {'0.png':changed})
        changed['boxes'].pop()
        self.hub.sync_annotation_drafts('Bob', {'0.png':changed})
        self.assertEqual(len(self.repo.files[f'submissions/{sid}/annotations.json']['0.png']['boxes']),8)
        self.assertTrue(self.hub.reject_submission(sid,'Correct boundaries')[0])
        self.assertEqual(self.repo.files['lock_manifest.json']['images']['0.png']['claimed_by'],'Bob')

    def test_unsubmitted_adoption_inherits_last_synced_draft(self):
        self.seed('locked', 'Alice')
        changed = annotations()['0.png']
        self.hub.sync_annotation_drafts('Alice', {'0.png':changed})
        self.repo.files['lock_manifest.json']['images']['0.png']['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        self.hub.download_image = lambda name, revision=None: None
        self.hub.checkout_batch(['0.png'], 'Bob')
        session = json.loads(Path(hub_module.LOCAL_SESSION_PATH).read_text())
        self.assertEqual(session['annotations']['0.png']['boxes'],changed['boxes'])

    def test_concurrent_edit_blocks_stale_review(self):
        sid = self.submitted('Alice')
        snapshot = self.hub.fetch_submission_annotations(sid)
        changed = copy.deepcopy(snapshot['0.png']); changed['boxes'].pop()
        self.repo.conflict = lambda repo: repo.files[f'submissions/{sid}/annotations.json'].update({'0.png':changed})
        ok, message = self.hub.merge_submission_to_master(sid, snapshot, expected_annotations=snapshot)
        self.assertFalse(ok)
        self.assertIn('changed while', message)
        self.assertNotIn('universal_annotations.json', self.repo.files)

    def test_approval_race_blocks_draft_overwrite(self):
        sid = self.submitted('Alice')
        self.repo.conflict = lambda repo: repo.files['lock_manifest.json']['images']['0.png'].update(status='approved')
        with self.assertRaises(ValueError):
            self.hub.sync_annotation_drafts('Alice', {'0.png':annotations()['0.png']})

    def test_non_maintainer_cannot_approve_or_reopen(self):
        sid = self.submitted('Alice')
        self.hub.is_maintainer = lambda: False
        self.assertFalse(self.hub.merge_submission_to_master(sid)[0])
        self.assertFalse(self.hub.reject_submission(sid,'Change this')[0])
    def test_read_member_not_maintainer(self):
        self.repo.whoami=lambda **kw:{'name':'reader','orgs':[{'name':'tempo-bio','roleInOrg':'read'}],'auth':{'accessToken':{'role':'write'}}}
        with patch.object(hub_module.tempo_preferences,'get',side_effect=lambda cat,key,default=None:default):
            self.assertFalse(self.hub.is_maintainer())
            self.repo.whoami=lambda **kw:{'name':'admin','orgs':[{'name':'tempo-bio','roleInOrg':'admin'}],'auth':{'accessToken':{'role':'write'}}}
            self.assertTrue(self.hub.is_maintainer())
    def test_checkout_downloads_and_persists_session(self):
        self.seed()
        downloaded=[]
        self.hub.download_image=lambda name, revision=None:downloaded.append(name)
        self.hub.checkout_batch(['0.png'],'Alice')
        self.assertEqual(downloaded,['0.png'])
        session=json.loads(Path(hub_module.LOCAL_SESSION_PATH).read_text())
        self.assertIn('0.png',session['annotations'])
    def test_corrupt_model_preserves_installed_file(self):
        target=Path(self.tmp.name)/'yolo26n-scout.pt'; target.write_bytes(b'old')
        source=Path(self.tmp.name)/'download'; source.write_bytes(b'corrupt')
        self.hub.model_manifest=lambda:{'revision':'abc','models':{'yolo26n-scout.pt':{'sha256':hashlib.sha256(b'valid').hexdigest()}}}
        with patch.object(hub_module,'hf_hub_download',return_value=str(source)):
            ok,msg=self.hub.download_champion_weights('yolo26n-scout.pt',self.tmp.name)
        self.assertFalse(ok); self.assertIn('checksum',msg)
        self.assertEqual(target.read_bytes(),b'old')
        self.assertFalse(list(Path(self.tmp.name).glob('*.tmp')))
    def test_valid_model_is_deployed(self):
        source=Path(self.tmp.name)/'download'; source.write_bytes(b'valid')
        self.hub.model_manifest=lambda:{'revision':'abc','models':{'yolo26n-scout.pt':{'sha256':hashlib.sha256(b'valid').hexdigest()}}}
        with patch.object(hub_module,'hf_hub_download',return_value=str(source)):
            self.assertTrue(self.hub.download_champion_weights('yolo26n-scout.pt',self.tmp.name)[0])
        self.assertEqual((Path(self.tmp.name)/'yolo26n-scout.pt').read_bytes(),b'valid')


class SchemaTests(unittest.TestCase):
    def test_submission_counts_and_geometry(self):
        self.assertEqual(validate_annotations(annotations()),(100,10,10))
        for points in ([[1,1]],[[0,0],[.5,.5],[1,1]],[[0,0],[2,0],[0,1]],[[0,0],[float('nan'),0],[0,1]]):
            records=annotations(); records['0.png']['boxes'][0]['polygon']=points
            with self.assertRaises(ValueError): validate_annotations(records)
        records={'one.png':annotations()['0.png']}
        records['one.png']['boxes'] = [records['one.png']['boxes'][1]]
        self.assertEqual(validate_annotations(records), (1, 1, 0))
    def test_legacy_boxes_normalize_without_mutating_draft(self):
        record={'boxes':[{'class_id':1,'box':[10,20,30,40]}]}
        normalized=normalize_record(record,100,100)
        self.assertEqual(normalized['boxes'][0]['polygon'][0],[.1,.2])
        self.assertIn('box',record['boxes'][0])
        validate_annotations({'a.png':normalized})


if __name__ == '__main__':
    unittest.main()
