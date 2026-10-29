"""Historical aliases, authenticated ownership and lossless collaboration writes."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import tempo_hub as hub_module
import tempo_annotation_store as store
from tempo_hub import TempoHub
from tempo_annotations import (reconcile_image_identities, merge_image_records,
                               normalize_record)
from tests.test_collaboration_lifecycle import MemoryRepo


def record(name, count=1, status=None):
    result = {'original_filename':name, 'width':100, 'height':100, 'coordinate_space':'pixels',
              'boxes':[{'class_id':0,'polygon':[[10,10],[20,10],[20,20],[10,20]]}
                       for _ in range(count)]}
    if status:
        result['status'] = status
    return result


class IdentityTests(unittest.TestCase):
    def test_same_account_and_name_on_second_computer_does_not_commit(self):
        self.repo.files['lock_manifest.json']['collaborators'] = {
            'hf:alice-id': {'profile_name': 'Shared Laboratory', 'account_name': 'alice'}}
        before = copy.deepcopy(self.repo.files)
        with patch.object(self.hub, 'profile_name', return_value='Shared Laboratory'), \
             patch.object(self.hub, '_prepare_manifest', side_effect=AssertionError('Profile save must not migrate images')):
            self.assertTrue(self.hub.publish_collaborator_profile())
        self.assertEqual(self.repo.files, before)
        self.assertEqual(self.repo.commits, [])

    def test_profile_change_preserves_reservations_and_other_accounts(self):
        manifest = self.repo.files['lock_manifest.json']
        manifest['collaborators'] = {
            'hf:alice-id': {'profile_name': 'Old name', 'account_name': 'alice'},
            'hf:bob-id': {'profile_name': 'Other Laboratory', 'account_name': 'bob'}}
        before = copy.deepcopy(self.repo.files)
        with patch.object(self.hub, 'profile_name', return_value='Shared Laboratory'):
            self.assertTrue(self.hub.publish_collaborator_profile())
            # A second device saving the same name must not write again.
            self.assertTrue(self.hub.publish_collaborator_profile())
        expected = before['lock_manifest.json']
        expected['collaborators']['hf:alice-id']['profile_name'] = 'Shared Laboratory'
        self.assertEqual(self.repo.files, before)
        self.assertEqual(self.repo.commits, [['lock_manifest.json']])

    def test_concurrent_matching_profile_finishes_without_retry_commit(self):
        def other_device(repo):
            repo.files['lock_manifest.json']['collaborators'] = {
                'hf:alice-id': {'profile_name': 'Shared Laboratory', 'account_name': 'alice'}}
        self.repo.conflict = other_device
        with patch.object(self.hub, 'profile_name', return_value='Shared Laboratory'), \
             patch.object(self.repo, 'create_commit', wraps=self.repo.create_commit) as commit:
            self.assertTrue(self.hub.publish_collaborator_profile())
        self.assertEqual(commit.call_count, 1)
        self.assertEqual(self.repo.commits, [])

    def test_readonly_preview_pins_identity_and_does_not_migrate_or_reserve(self):
        before = copy.deepcopy(self.repo.files)
        catalog = self.hub.fetch_image_catalog()
        result = self.hub.fetch_image_preview('frame20.png', catalog)
        self.assertEqual(result['name'], self.legacy20)
        self.assertEqual(len(result['record']['boxes']), 9)
        self.assertEqual(result['revision'], catalog['revision'])
        self.assertEqual(self.download_mock.call_args.kwargs['revision'], catalog['revision'])
        self.assertEqual(self.repo.files, before)
        self.assertEqual(self.repo.commits, [])
        self.assertFalse((self.local/'session.json').exists())
        self.assertFalse((self.local/'manifest.json').exists())
        self.assertFalse((self.local/self.legacy20).exists())

    def test_preview_uses_submission_over_draft_over_master_without_touching_session(self):
        self.hub.claim_batch(['frame20.png'], 'Alice')
        name = self.legacy20
        self.repo.files['annotation_drafts.json'] = {name:record('frame20.png', 2)}
        self.repo.files['lock_manifest.json']['images'][name]['submission_id'] = 'pending'
        self.repo.files['submissions/pending/annotations.json'] = {name:record('frame20.png', 3)}
        catalog = self.hub.fetch_image_catalog()
        self.assertEqual(len(self.hub.fetch_image_preview(name,catalog)['record']['boxes']), 3)
        self.assertFalse((self.local/'session.json').exists())

    def test_preview_rejects_changed_dataset_or_changed_image_bytes(self):
        catalog = self.hub.fetch_image_catalog()
        with patch.object(self.hub,'get_dataset_repo',return_value='other/data'):
            with self.assertRaisesRegex(ValueError,'dataset changed'):
                self.hub.fetch_image_preview('frame20.png',catalog)
        (self.remote/'frame20.png').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'saved image identity'):
            self.hub.fetch_image_preview('frame20.png',catalog)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.local = self.root/'local'; self.local.mkdir()
        self.remote = self.root/'remote'; self.remote.mkdir()
        self.legacy20 = '20260427_055328_frame20.png'
        self.legacy21 = '20260427_055328_frame21.png'
        self.files = {}
        for name, content in [('frame20.png',b'image20'), ('frame21.png',b'image21'),
                              (self.legacy21,b'image21')]:
            path = self.remote/name; path.write_bytes(content)
            self.files['images/'+name] = str(path)
        self.files['lock_manifest.json'] = {'version':1,'images':{
            'frame20.png':{'status':'available'}, 'frame21.png':{'status':'available'},
            self.legacy21:{'status':'approved'}}}
        self.files['universal_annotations.json'] = {
            self.legacy20:record('frame20.png',9), self.legacy21:record('frame21.png',54,'approved')}
        self.repo = MemoryRepo(self.files)
        self.repo.list_repo_files = lambda **kw:list(self.repo.files)
        self.repo.list_repo_tree = self.tree
        self.repo.whoami = lambda token=None: {
            'id':'alice-id' if token == 'alice-token' else 'bob-id',
            'name':'alice' if token == 'alice-token' else 'bob',
            'orgs':[{'name':'lab','roleInOrg':'admin' if token == 'alice-token' else 'write'}],
            'auth':{'accessToken':{'role':'write'}}}
        self.hub = TempoHub.__new__(TempoHub)
        self.hub.token = 'alice-token'; self.hub.api = self.repo
        self.hub.get_dataset_repo = lambda:'lab/data'
        self.hub._read = lambda name,revision,default=None,**kw:copy.deepcopy(self.repo.files.get(name,default))
        for key, value in [('UNIVERSAL_DIR',str(self.local)), ('LOCAL_SESSION_PATH',str(self.local/'session.json')),
                           ('LOCAL_LOCK_CACHE',str(self.local/'manifest.json'))]:
            patcher = patch.object(hub_module,key,value); patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(hub_module,'hf_hub_download',side_effect=self.download)
        self.download_mock = patcher.start(); self.addCleanup(patcher.stop)

    def download(self, filename, **kw):
        return self.repo.files[filename]

    def tree(self, **kw):
        return [types.SimpleNamespace(path=name,blob_id='blob',
                    lfs=types.SimpleNamespace(sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))
                for name,path in self.repo.files.items() if name.startswith('images/')]

    def checkout20(self):
        return self.hub.checkout_batch(['frame20.png'],'Same Lab Name')

    def session(self):
        return json.loads((self.local/'session.json').read_text())

    def test_historical_annotations_inherited_without_new_local_copy(self):
        (self.local/self.legacy20).write_bytes(b'image20')
        self.assertEqual(self.checkout20(),([self.legacy20],[]))
        self.download_mock.assert_not_called()
        saved = self.session()['annotations']
        self.assertEqual(len(saved[self.legacy20]['boxes']),9)
        self.assertEqual(saved[self.legacy20]['draft_owner'],'hf:alice-id')
        self.assertFalse((self.local/'frame20.png').exists())
        self.assertIn('identity_migrations/1/lock_manifest.json',self.repo.files)

    def test_approved_image_cannot_be_reserved_through_available_alias(self):
        claimed, conflicts = self.hub.claim_batch(['frame21.png'],'Same Lab Name')
        self.assertEqual(claimed,[])
        self.assertEqual(conflicts,[self.legacy21])
        images = self.repo.files['lock_manifest.json']['images']
        self.assertEqual(images[self.legacy21]['status'],'approved')
        self.assertNotIn('frame21.png',images)
        self.assertEqual(len(self.repo.files['universal_annotations.json'][self.legacy21]['boxes']),54)

    def test_approval_alias_rejected_without_uploading_second_shared_image(self):
        self.hub.claim_batch(['frame20.png'],'Same Lab Name')
        before = len(self.repo.commits)
        with self.assertRaisesRegex(ValueError,'already approved'):
            self.hub.approve_local_images({'frame21.png':normalize_record(record('frame21.png'),100,100)},
                                         {'frame21.png':self.remote/'frame21.png'},'Same Lab Name')
        self.assertEqual(len(self.repo.commits),before)

    def test_renamed_duplicate_approval_is_rejected(self):
        self.hub.claim_batch(['frame20.png'],'Same Lab Name')
        with self.assertRaisesRegex(ValueError,'same image already exists'):
            self.hub.approve_local_images({'renamed.png':normalize_record(record('renamed.png'),100,100)},
                                         {'renamed.png':self.remote/'frame21.png'},'Same Lab Name')

    def test_duplicate_raw_ingest_is_rejected(self):
        self.hub.claim_batch(['frame20.png'],'Same Lab Name')
        renamed = self.root/'renamed.png'; renamed.write_bytes(b'image21')
        ok, message = self.hub.upload_raw_images([str(renamed)])
        self.assertEqual(ok,0); self.assertIn('already in the pool',message)

    def test_two_accounts_with_same_label_cannot_share_reservation(self):
        self.checkout20()
        self.hub.token = 'bob-token'
        with self.assertRaisesRegex(ValueError,'no longer editable'):
            self.hub.renew_claim(['frame20.png'],'Same Lab Name')
        self.assertEqual(self.hub.get_collaborator_id(),'hf:bob-id')
        self.assertEqual(self.repo.files['lock_manifest.json']['images'][self.legacy20]['claimed_by'],'hf:alice-id')

    def test_stale_authenticated_actor_rejected_after_token_switch(self):
        self.checkout20(); self.hub.token = 'bob-token'
        with self.assertRaisesRegex(PermissionError,'account changed'):
            self.hub.renew_claim(['frame20.png'],'hf:alice-id')

    def test_local_maintainer_override_cannot_escalate_collaborator(self):
        self.hub.token = 'bob-token'
        with patch.object(hub_module.tempo_preferences,'get',return_value=True):
            self.assertFalse(self.hub.is_maintainer())

    def test_corrupt_served_image_rejected_before_publication(self):
        self.checkout20()
        manifest = self.repo.files['lock_manifest.json']
        (self.local/self.legacy20).unlink()
        (self.remote/'frame20.png').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'checksum changed'):
            self.hub.download_image(self.legacy20,revision='2',manifest=manifest)
        self.assertFalse((self.local/self.legacy20).exists())

    def test_different_bytes_under_historical_name_are_not_merged(self):
        path = self.remote/self.legacy20; path.write_bytes(b'different-image')
        self.repo.files['images/'+self.legacy20] = str(path)
        with self.assertRaisesRegex(ValueError,'different image bytes'):
            self.hub.claim_batch(['frame20.png'],'Same Lab Name')
        self.assertEqual(self.repo.commits,[])

    def test_nonempty_alias_annotation_conflict_preserves_repository(self):
        self.repo.files['universal_annotations.json']['frame20.png'] = record('frame20.png',2)
        with self.assertRaisesRegex(ValueError,'different annotations'):
            self.hub.claim_batch(['frame20.png'],'Same Lab Name')
        self.assertEqual(self.repo.commits,[])

    def test_conflicting_alias_reservations_are_not_silently_combined(self):
        self.repo.files['lock_manifest.json']['images']['frame20.png'] = TempoHub._lease('hf:alice-id')
        self.repo.files['lock_manifest.json']['images'][self.legacy20] = TempoHub._lease('hf:bob-id')
        with self.assertRaisesRegex(ValueError,'conflicting reservations'):
            self.hub.claim_batch(['frame20.png'],'Same Lab Name')
        self.assertEqual(self.repo.commits,[])

    def test_newer_unsynced_own_draft_survives_checkout(self):
        draft = record('frame20.png',10)
        draft.update(sync_pending=True,draft_owner='hf:alice-id')
        (self.local/'session.json').write_text(json.dumps({'annotations':{self.legacy20:draft}}))
        self.checkout20()
        saved = self.session()['annotations'][self.legacy20]
        self.assertEqual(len(saved['boxes']),10)
        self.assertTrue(saved['sync_pending'])

    def test_checkout_refuses_drafts_from_another_dataset(self):
        (self.local/'session.json').write_text(json.dumps({'dataset_repo':'other/data','annotations':{}}))
        with self.assertRaisesRegex(ValueError,'different shared dataset'):
            self.checkout20()
        self.assertEqual(self.repo.commits,[])

    def test_canonical_submission_review_round_trip_preserves_identity(self):
        self.checkout20()
        annotation = normalize_record(record('frame20.png',9),100,100)
        ok, sid = self.hub.upload_submission('Same Lab Name',['frame20.png'],{'frame20.png':annotation},{})
        self.assertTrue(ok,sid)
        loaded = self.hub.fetch_submission_annotations(sid)
        self.assertEqual(set(loaded),{self.legacy20})
        self.assertEqual(self.hub.sync_annotation_drafts('Same Lab Name',{'frame20.png':annotation}),1)
        ok, message = self.hub.merge_submission_to_master(sid,expected_annotations=loaded)
        self.assertTrue(ok,message)
        image = self.repo.files['lock_manifest.json']['images'][self.legacy20]
        self.assertEqual(image['status'],'approved')
        self.assertEqual(image['approved_by'],'hf:alice-id')
        self.assertEqual(self.repo.files['universal_annotations.json'][self.legacy20]['image_id'],image['image_id'])

    def test_unowned_submission_is_rejected_even_with_same_display_label(self):
        self.checkout20(); self.hub.token='bob-token'
        annotation=normalize_record(record('frame20.png'),100,100)
        ok,message=self.hub.upload_submission('Same Lab Name',['frame20.png'],{'frame20.png':annotation},{})
        self.assertFalse(ok);self.assertIn('lease is required',message)

    def test_alias_selection_cannot_submit_one_image_twice(self):
        self.checkout20(); annotation=normalize_record(record('frame20.png'),100,100)
        ok,message=self.hub.upload_submission('Same Lab Name',['frame20.png',self.legacy20],
                                              {'frame20.png':annotation,self.legacy20:annotation},{})
        self.assertFalse(ok);self.assertIn('duplicate names',message)

    def test_changed_annotation_identity_is_rejected(self):
        self.checkout20();annotation=normalize_record(record('frame20.png'),100,100)
        annotation['image_id']='sha256:'+'0'*64
        ok,message=self.hub.upload_submission('Same Lab Name',['frame20.png'],{'frame20.png':annotation},{})
        self.assertFalse(ok);self.assertIn('identity changed',message)

    def test_lease_changed_during_download_preserves_local_drafts(self):
        original = self.hub.download_image
        def download(*args, **kwargs):
            result = original(*args, **kwargs)
            self.repo.files['lock_manifest.json']['images'][self.legacy20]['claimed_by'] = 'hf:bob-id'
            self.repo.sha += 1
            return result
        with patch.object(self.hub,'download_image',side_effect=download):
            with self.assertRaisesRegex(ValueError,'reservation changed'):
                self.checkout20()
        self.assertFalse((self.local/'session.json').exists())

    def test_foreign_private_draft_is_backed_up_without_reassignment(self):
        self.repo.files['universal_annotations.json'].pop(self.legacy20)
        draft = record('frame20.png',10); draft['draft_owner'] = 'hf:bob-id'
        (self.local/'session.json').write_text(json.dumps({'annotations':{'frame20.png':draft}}))
        self.checkout20()
        self.assertEqual(self.session()['annotations']['frame20.png']['boxes'],[])
        backups=list(self.local.glob('checkout_backup_*.json'))
        self.assertEqual(json.loads(backups[0].read_text())['frame20.png'],draft)

    def test_master_identity_mismatch_fails_without_commit(self):
        self.repo.files['universal_annotations.json'][self.legacy20]['image_id']='sha256:'+'0'*64
        with self.assertRaisesRegex(ValueError,'identify a different image'):
            self.checkout20()
        self.assertEqual(self.repo.commits,[])

    def test_duplicate_approved_identities_cannot_enter_training(self):
        from tempo_training import select_approved_annotations
        first=record('frame21.png',54,'approved');first['image_id']='sha256:'+'a'*64
        with self.assertRaisesRegex(ValueError,'duplicate approved image identity'):
            select_approved_annotations({'frame21.png':first,'renamed.png':copy.deepcopy(first)})

    def test_empty_new_dataset_can_ingest_its_first_image(self):
        self.repo.files={'lock_manifest.json':{'version':1,'images':{}}}
        self.repo.list_repo_tree=lambda **kw:(_ for _ in ()).throw(hub_module.EntryNotFoundError('images does not exist yet'))
        count,message=self.hub.upload_raw_images([str(self.remote/'frame20.png')])
        self.assertEqual(count,1,message)
        self.assertEqual(self.repo.files['lock_manifest.json']['version'],2)


class RepairTests(unittest.TestCase):
    # Use the same historical byte/annotation fixture, without re-running its tests.
    setUp = IdentityTests.setUp
    tree = IdentityTests.tree
    download = IdentityTests.download

    def prepare(self):
        from scripts.repair_annotation_identities import prepare_repair
        (self.local/self.legacy20).write_bytes(b'image20')
        (self.local/'frame20.png').write_bytes(b'image20')
        (self.local/self.legacy21).write_bytes(b'image21')
        records=copy.deepcopy(self.repo.files['universal_annotations.json'])
        records['frame20.png']={'boxes':[],'draft_owner':'default_collaborator'}
        (self.local/'universal_annotations.json').write_text(json.dumps(records))
        (self.local/'local_session.json').write_text(json.dumps({'annotations':records,'claimed_images':['frame20.png']}))
        self.repo.files['lock_manifest.json']['images']['frame20.png']=TempoHub._lease('default_collaborator')
        return prepare_repair(self.hub,self.local,['frame20.png'])

    def test_review_plan_makes_no_writes(self):
        plan=self.prepare()
        self.assertEqual(self.repo.commits,[])
        self.assertFalse((self.local/'annotation_recovery').exists())
        self.assertEqual(plan['moves'],[{'source':'frame20.png','canonical':self.legacy20,'action':'quarantine'}])
        self.assertEqual(len(plan['records'][self.legacy20]['boxes']),9)

    def test_apply_preserves_all_annotations_and_quarantines_verified_copy(self):
        from scripts.repair_annotation_identities import apply_repair
        plan=self.prepare();before=(self.local/self.legacy20).read_bytes()
        result=apply_repair(self.hub,plan,app_closed=True)
        self.assertEqual(result['status'],'completed')
        self.assertEqual((self.local/self.legacy20).read_bytes(),before)
        self.assertFalse((self.local/'frame20.png').exists())
        backup=Path(result['backup'])
        self.assertEqual((backup/'redundant-images/frame20.png').read_bytes(),before)
        self.assertIn('frame20.png',json.loads((backup/'local_session.json').read_text())['annotations'])
        saved=json.loads((self.local/'local_session.json').read_text())
        self.assertEqual(set(saved['annotations']),{self.legacy20,self.legacy21})
        self.assertEqual(len(saved['annotations'][self.legacy21]['boxes']),54)
        self.assertEqual(saved['claimed_images'],[])
        self.assertEqual(self.repo.files['lock_manifest.json']['images'][self.legacy21]['status'],'approved')

    def test_apply_requires_closed_app(self):
        from scripts.repair_annotation_identities import apply_repair
        plan=self.prepare()
        with self.assertRaisesRegex(ValueError,'Close TEMPO'):
            apply_repair(self.hub,plan)
        self.assertEqual(self.repo.commits,[])

    def test_changed_remote_revision_prevents_repair(self):
        from scripts.repair_annotation_identities import apply_repair
        plan=self.prepare();self.repo.sha+=1
        with self.assertRaisesRegex(ValueError,'repository changed'):
            apply_repair(self.hub,plan,app_closed=True)
        self.assertEqual(self.repo.commits,[])

    def test_changed_local_annotations_prevent_remote_write(self):
        from scripts.repair_annotation_identities import apply_repair
        plan=self.prepare();(self.local/'local_session.json').write_text('{"annotations":{}}')
        with self.assertRaisesRegex(ValueError,'Local annotations changed'):
            apply_repair(self.hub,plan,app_closed=True)
        self.assertEqual(self.repo.commits,[])

    def test_remote_failure_keeps_local_data_and_backup(self):
        from scripts.repair_annotation_identities import apply_repair
        plan=self.prepare();before=(self.local/'local_session.json').read_bytes();self.repo.fail=True
        with self.assertRaises(ConnectionError):
            apply_repair(self.hub,plan,app_closed=True)
        self.assertEqual((self.local/'local_session.json').read_bytes(),before)
        self.assertTrue((self.local/'frame20.png').exists())
        self.assertTrue((self.local/'annotation_recovery'/plan['id']/'local_session.json').exists())

    def test_missing_canonical_image_is_promoted_before_local_session_save(self):
        from scripts.repair_annotation_identities import apply_repair,prepare_repair
        self.prepare();(self.local/self.legacy20).unlink()
        plan=prepare_repair(self.hub,self.local,['frame20.png'])
        result=apply_repair(self.hub,plan,app_closed=True)
        self.assertEqual(result['status'],'completed')
        self.assertEqual((self.local/self.legacy20).read_bytes(),b'image20')
        self.assertFalse((self.local/'frame20.png').exists())

    def test_pending_submission_is_not_cleared_by_cleanup(self):
        from scripts.repair_annotation_identities import prepare_repair
        self.prepare();lease=self.repo.files['lock_manifest.json']['images']['frame20.png']
        lease.update(status='submitted',submission_id='pending')
        with self.assertRaisesRegex(ValueError,'pending submission'):
            prepare_repair(self.hub,self.local,['frame20.png'])
        self.assertEqual(self.repo.commits,[])


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);self.path=self.root/'session.json';self.annotations=self.root/'annotations.json'

    def test_stale_revision_cannot_replace_newer_drafts(self):
        store.update_session(self.path,lambda old:{'annotations':{'frame.png':record('frame.png',9)}})
        before=self.path.read_bytes()
        with self.assertRaisesRegex(OSError,'changed in another operation'):
            store.update_session(self.path,lambda old:{'annotations':{}},expected_revision=0)
        self.assertEqual(self.path.read_bytes(),before)

    def test_second_file_failure_restores_first_file(self):
        self.annotations.write_bytes(b'{"old":{}}');self.path.write_bytes(b'{"annotations":{}}')
        previous=(self.annotations.read_bytes(),self.path.read_bytes());original=store.atomic_bytes
        def write(path,data):
            if Path(path)==self.path:raise OSError('injected full disk')
            return original(path,data)
        with patch.object(store,'atomic_bytes',side_effect=write),self.assertRaisesRegex(OSError,'full disk'):
            store.update_session(self.path,lambda old:{'annotations':{'new':{}}},annotations_path=self.annotations)
        self.assertEqual((self.annotations.read_bytes(),self.path.read_bytes()),previous)

    def test_competing_local_writer_fails_without_waiting_or_overwriting(self):
        with store.session_lock(self.path):
            with self.assertRaises(BlockingIOError):
                store.update_session(self.path,lambda old:{'annotations':{}})
        self.assertFalse(self.path.exists())

    def test_invalid_revision_is_not_an_empty_session(self):
        self.path.write_text('{"session_revision":true,"annotations":{}}')
        with self.assertRaisesRegex(ValueError,'malformed'):
            store.update_session(self.path,lambda old:{'annotations':{}})


if __name__ == '__main__':
    unittest.main()
