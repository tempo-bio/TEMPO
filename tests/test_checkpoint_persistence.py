"""Real Hub client requests against an in-process HTTP transport; no GPU or Hub writes."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from huggingface_hub.utils._http import httpx
from huggingface_hub import HfApi, set_client_factory
from huggingface_hub.utils import _http
from tempo_cloud import training_transaction,RepositoryWriteBudget
from tests import test_model_evaluation as evaluation_fixtures
import tempo_evaluation as service
import tempo_evaluation_runner as runner
from tempo_evaluation_protocol import unpack_package


class CheckpointHttpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/'model.bin'; self.path.write_bytes(b'completed weights'*100000)
        self.sha = 1; self.blobs = {}; self.files = {}; self.uploads = 0; self.conflicts = 0
        self.preuploads = 0; self.before_commit = None; self.during_preupload = None
        self.rate_limits=[];self.commit_attempts=0
        previous = _http._GLOBAL_CLIENT_FACTORY
        set_client_factory(lambda: httpx.Client(transport=httpx.MockTransport(self.handle)))
        self.addCleanup(set_client_factory, previous)
        self.api = HfApi(endpoint='https://checkpoint.invalid', token='fixture-token')
        self.hub = types.SimpleNamespace(token='fixture-token', api=self.api,
            get_dataset_repo=lambda:'fixture/data', get_models_repo=lambda:'fixture/models',
            _head=lambda:f'{self.sha:040x}', _read=lambda name, revision, default=None:copy.deepcopy(self.files.get(name,default)))

    def handle(self, request):
        path = request.url.path
        body = request.read()
        if '/preupload/' in path:
            data = json.loads(body); rows = []
            for item in data['files']:
                model = item['path'].endswith('.bin')
                if model:
                    self.preuploads += 1
                    if self.during_preupload:
                        callback, self.during_preupload = self.during_preupload, None; callback()
                    # A heartbeat lands during every slow model upload-mode request.
                    self.sha += 1
                rows.append(dict(path=item['path'], uploadMode='lfs' if model else 'regular', shouldIgnore=False))
            return httpx.Response(200, json={'files':rows})
        if path.endswith('/objects/batch'):
            objects = []
            for obj in json.loads(body)['objects']:
                item = dict(obj)
                if obj['oid'] not in self.blobs:
                    item['actions'] = {'upload':{'href':'https://checkpoint.invalid/blob/'+obj['oid']}}
                objects.append(item)
            return httpx.Response(200, json={'objects':objects})
        if path.startswith('/blob/'):
            self.blobs[path.rsplit('/',1)[1]] = body; self.uploads += 1
            return httpx.Response(200)
        if '/commit/' in path:
            self.commit_attempts+=1
            if self.rate_limits:
                return httpx.Response(429,headers=self.rate_limits.pop(0),json={'error':'Repository commit limit reached'})
            items = [json.loads(line) for line in body.splitlines()]
            if self.before_commit:
                callback, self.before_commit = self.before_commit, None; callback()
            if items[0]['value']['parentCommit'] != f'{self.sha:040x}':
                self.conflicts += 1
                return httpx.Response(412, json={'error':'The branch was updated.'})
            for item in items[1:]:
                value = item['value']
                self.files[value['path']] = (self.blobs[value['oid']] if item['key']=='lfsFile'
                                             else json.loads(base64.b64decode(value['content'])))
            self.sha += 1
            return httpx.Response(200,json={'commitUrl':f'https://checkpoint.invalid/datasets/fixture/data/commit/{self.sha:040x}',
                                           'commitOid':f'{self.sha:040x}'})
        raise AssertionError('Unexpected HTTP request: '+str(request.url))

    def mutation(self, _, revision):
        return {'training/model.bin':str(self.path), 'training/state.json':json.dumps({'revision_seen':revision}).encode()}, True

    def test_original_order_reproduces_repeated_412_with_the_real_client(self):
        with self.assertRaises(Exception) as failure:
            training_transaction(self.hub, self.mutation, 'Original order')
        self.assertEqual(failure.exception.response.status_code,412)
        self.assertEqual(self.conflicts,4); self.assertNotIn('training/model.bin',self.files)

    def test_preupload_commits_after_heartbeats_and_verifies_exact_weights(self):
        self.assertTrue(training_transaction(self.hub,self.mutation,'Preserve model',preupload=True,commit_lock=threading.RLock()))
        self.assertEqual(self.files['training/model.bin'],self.path.read_bytes())
        self.assertEqual(self.preuploads,1); self.assertEqual(self.uploads,1); self.assertEqual(self.conflicts,0)

    def test_unrelated_commit_retries_without_uploading_weights_again(self):
        self.before_commit = lambda:setattr(self,'sha',self.sha+1)
        training_transaction(self.hub,self.mutation,'Retry conflict',preupload=True)
        self.assertEqual(self.conflicts,1); self.assertEqual(self.uploads,1); self.assertEqual(self.preuploads,1)
        self.assertEqual(self.files['training/model.bin'],self.path.read_bytes())

    def test_account_switch_during_preupload_prevents_commit(self):
        execute = self.api.preupload_lfs_files
        def upload(**kwargs):
            execute(**kwargs); self.hub.token='different-account'
        with patch.object(self.api,'preupload_lfs_files',side_effect=upload),self.assertRaises(PermissionError):
            training_transaction(self.hub,self.mutation,'Reject changed account',preupload=True)
        self.assertEqual(self.files,{})

    def test_commit_limit_waits_and_reuses_uploaded_model_without_retraining(self):
        now=[0.];waits=[]
        def wait(seconds):waits.append(seconds);now[0]+=seconds
        budget=RepositoryWriteBudget(clock=lambda:now[0],wait=wait)
        self.rate_limits=[{'Retry-After':'3600'}]
        training_transaction(self.hub,self.mutation,'Preserve completed model',preupload=True,write_budget=budget)
        self.assertEqual(now[0],3600)
        self.assertEqual(self.commit_attempts,2)
        self.assertEqual(self.uploads,1);self.assertEqual(self.preuploads,1)
        self.assertEqual(self.files['training/model.bin'],self.path.read_bytes())

    def test_progress_yields_commit_slot_to_checkpoint_and_unknown_reset_waits(self):
        now=[0.]
        budget=RepositoryWriteBudget(clock=lambda:now[0],wait=lambda seconds:now.__setitem__(0,now[0]+seconds))
        training_transaction(self.hub,self.mutation,'Checkpoint',preupload=True,write_budget=budget)
        self.assertFalse(training_transaction(self.hub,lambda *_:({'training/progress.json':b'{}'},True),
            'Progress',write_budget=budget,defer_write=True))
        self.assertEqual(self.commit_attempts,1)
        error=RuntimeError('Quota');error.response=types.SimpleNamespace(status_code=429,headers={})
        budget.rate_limited(error)
        self.assertFalse(budget.take(defer=True))
        budget.take();self.assertEqual(now[0],3600)

    def test_budget_does_not_call_a_frozen_engines_older_retry_helper(self):
        old=types.SimpleNamespace(sync_retry_delay=lambda error,failures:20)
        error=RuntimeError('Quota');error.response=types.SimpleNamespace(status_code=429,headers={'Retry-After':'30'})
        budget=RepositoryWriteBudget(clock=lambda:0)
        with patch.dict('sys.modules',{'tempo_cloud_runner':old}):budget.rate_limited(error)
        self.assertEqual(budget.next_write,30)

    def test_heartbeat_continues_during_upload_and_both_writes_are_retained(self):
        entered, resume = threading.Event(), threading.Event(); errors = []; lock = threading.RLock()
        def wait_for_heartbeat():
            entered.set()
            if not resume.wait(5):raise AssertionError('The heartbeat was blocked by the model upload.')
        self.during_preupload = wait_for_heartbeat
        def checkpoint():
            try:training_transaction(self.hub,self.mutation,'Model checkpoint',preupload=True,commit_lock=lock)
            except BaseException as error:errors.append(error)
        worker = threading.Thread(target=checkpoint);worker.start()
        try:
            self.assertTrue(entered.wait(5))
            training_transaction(self.hub,lambda _,head:({'training/heartbeat.json':b'{"alive":true}'},True),
                                 'Heartbeat',commit_lock=lock)
        finally:
            resume.set();worker.join(5)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
        self.assertEqual(self.files['training/heartbeat.json'],{'alive':True})
        self.assertEqual(self.files['training/model.bin'],self.path.read_bytes())

    def test_changed_file_is_rejected_after_upload_without_committing_it(self):
        execute = self.api.preupload_lfs_files
        def upload(**kwargs):
            execute(**kwargs); self.path.write_bytes(b'changed after upload')
        with patch.object(self.api,'preupload_lfs_files',side_effect=upload),self.assertRaisesRegex(ValueError,'changed during upload'):
            training_transaction(self.hub,self.mutation,'Reject changed file',preupload=True)
        self.assertEqual(self.files,{})


class PreservationTests(unittest.TestCase):
    setUp = evaluation_fixtures.EvaluationTests.setUp
    plan = evaluation_fixtures.EvaluationTests.plan
    save = evaluation_fixtures.EvaluationTests.save
    def prepare_completed_files(self):
        plan = self.save(); base = service.prefix(plan['id']); workspace = self.root/plan['id']; workspace.mkdir()
        self.repo.files[base+'/state.json'] = {'phase':'failed'}
        self.repo.files[service.INTENT] = {'evaluation_id':plan['id'],'status':'failed'}
        (workspace/'manifest.json').write_text(json.dumps(plan['manifest']))
        package = self.repo.files[base+'/protocol.zip']
        unpack_package(package,workspace/'protocol',plan['protocol_sha256'])
        model = workspace/'results/model'; model.mkdir(parents=True)
        artifact = model/'weights.pt'; artifact.write_bytes(b'expensive completed model')
        (model/'result.json').write_text(json.dumps({'artifact':str(artifact),'sha256':service.file_checksum(artifact)}))
        (model/'evaluation_stage.json').write_text(json.dumps({'role':'custom-model'}))
        return plan,workspace,artifact

    def test_preservation_saves_completed_files_without_training_or_changing_frozen_plan(self):
        plan,workspace,artifact = self.prepare_completed_files(); before = copy.deepcopy(plan)
        with patch.object(runner,'run_prepared_evaluation',side_effect=AssertionError('Training must not run')):
            receipt = runner.preserve_local_evaluation(evaluation_fixtures.ROOT,workspace,self.hub,recovery_sources=b'verified recovery sources')
        self.assertEqual(len(receipt['completed_models']),1)
        self.assertEqual(artifact.read_bytes(),b'expensive completed model')
        self.assertEqual(self.repo.files[service.prefix(plan['id'])+'/plan.json'],before)
        self.assertEqual(self.repo.files[service.prefix(plan['id'])+'/state.json'],{'phase':'failed'})
        self.assertEqual(self.repo.files[service.INTENT]['status'],'failed')
        self.assertNotIn('model_manifest.json',self.repo.files)

    def test_preservation_rejects_active_execution_and_changed_model(self):
        plan,workspace,artifact = self.prepare_completed_files()
        self.repo.files[service.INTENT]['status']='running'
        with self.assertRaisesRegex(ValueError,'active evaluation'):runner.preserve_local_evaluation(evaluation_fixtures.ROOT,workspace,self.hub)
        self.repo.files[service.INTENT]['status']='failed'; artifact.write_bytes(b'changed model')
        with self.assertRaisesRegex(ValueError,'different checksum'):runner.preserve_local_evaluation(evaluation_fixtures.ROOT,workspace,self.hub)

    def test_preservation_failure_retains_local_files(self):
        _,workspace,artifact = self.prepare_completed_files()
        with self.assertRaises(ConnectionError):
            runner.preserve_local_evaluation(evaluation_fixtures.ROOT,workspace,self.hub,transaction=lambda *a,**k:(_ for _ in ()).throw(ConnectionError('offline')))
        self.assertEqual(artifact.read_bytes(),b'expensive completed model')

    def test_save_only_recovery_fences_exact_stopped_job_in_the_same_commit(self):
        plan,workspace,artifact=self.prepare_completed_files();base=service.prefix(plan['id'])
        self.repo.files[service.INTENT].update(id='stopped-job',status='running')
        self.repo.files[base+'/state.json']=dict(phase='running',job_id='stopped-job',updated_at=time.time()-180)
        with patch.object(runner,'run_prepared_evaluation',side_effect=AssertionError('Training must not run')):
            receipt=runner.preserve_local_evaluation(evaluation_fixtures.ROOT,workspace,self.hub,stopped_job_id='stopped-job')
        self.assertEqual(receipt['completed_models'][0]['sha256'],service.file_checksum(artifact))
        self.assertEqual(self.repo.files[service.INTENT]['status'],'failed')
        self.assertEqual(self.repo.files[base+'/state.json']['phase'],'failed')

    def test_save_only_recovery_cannot_fence_a_different_or_fresh_job(self):
        plan,workspace,_=self.prepare_completed_files();base=service.prefix(plan['id'])
        self.repo.files[service.INTENT].update(id='still-running',status='running')
        self.repo.files[base+'/state.json']=dict(phase='running',job_id='still-running',updated_at=time.time()-180)
        with self.assertRaisesRegex(ValueError,'active evaluation'):
            runner.preserve_local_evaluation(evaluation_fixtures.ROOT,workspace,self.hub,stopped_job_id='another-job')
        self.repo.files[base+'/state.json']['updated_at']=time.time()
        with self.assertRaisesRegex(ValueError,'active evaluation'):
            runner.preserve_local_evaluation(evaluation_fixtures.ROOT,workspace,self.hub,stopped_job_id='still-running')


if __name__=='__main__':unittest.main()
