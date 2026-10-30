"""Cloud handoff → approved dataset → publication → model update, with fake remote services.

Training is deliberately simulated: these tests never allocate a GPU or publish remotely.
"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import shutil
import sys
import time
import zipfile
import types
import unittest
from unittest.mock import patch
import cv2
import numpy as np
from huggingface_hub.errors import EntryNotFoundError
from tempo_cloud import prepare_request, queue_request, connection_cell, BUNDLE_FILES, resolve_disconnected_run
from tempo_hub import TempoHub
from tempo_training import MODEL_FILES, APPROVAL_POLICY
import tempo_cloud_runner


class CloudWorkflowTests(unittest.TestCase):
    def seed_stale_run(self, phase='checking_training_steps', age=180):
        self.files['data']['training/request.json'] = b'{"id":"stopped"}'
        self.files['data']['training/stopped/status.json'] = json.dumps(
            dict(phase=phase, updated_at=time.time()-age, models={'stardist':{'phase':'validating'}})).encode()
        self.checkpoint()

    def test_disconnected_resolution_only_changes_training_records_and_unblocks_registration(self):
        self.seed_approved(); self.seed_stale_run()
        before = copy.deepcopy(self.files)
        result = resolve_disconnected_run(self.hub, 'stopped')
        self.assertEqual(result['outcome'],'cleared')
        self.assertEqual(result['status']['phase'],'disconnected')
        self.assertEqual(result['status']['models'],{'stardist':{'phase':'validating'}})
        self.assertEqual(json.loads(self.files['data']['training/stopped/control.json']),{'cancel':True})
        for name,data in before['data'].items():
            if name != 'training/stopped/status.json': self.assertEqual(self.files['data'][name],data)
        self.assertEqual(self.files['models'],before['models'])
        self.assertTrue(all(name.startswith('training/') for name in self.commits[-1][1]))
        request,bundle = prepare_request(self.hub,Path(__file__).resolve().parents[1])
        queue_request(self.hub,request,bundle)
        self.assertEqual(json.loads(self.files['data']['training/request.json'])['id'],request['id'])

    def test_resolution_of_finished_or_replaced_run_reads_without_writing(self):
        for phase in ('completed','failed','cancelled','disconnected'):
            with self.subTest(phase=phase):
                self.seed_stale_run(phase)
                before = copy.deepcopy(self.files)
                self.assertEqual(resolve_disconnected_run(self.hub,'stopped')['outcome'],'finished')
                self.assertEqual(self.files,before)
        self.files['data']['training/request.json'] = b'{"id":"different"}'
        self.files['data']['training/different/status.json'] = b'{"phase":"training"}'
        self.checkpoint()
        self.assertEqual(resolve_disconnected_run(self.hub,'stopped')['outcome'],'changed')
        self.assertFalse(self.commits)

    def test_resolution_rejects_fresh_missing_and_invalid_heartbeat(self):
        self.seed_stale_run(age=1)
        with self.assertRaisesRegex(ValueError,'last two minutes'):
            resolve_disconnected_run(self.hub,'stopped')
        for heartbeat in (None, 'yesterday', float('nan'), False):
            self.files['data']['training/stopped/status.json'] = json.dumps(
                dict(phase='training',updated_at=heartbeat)).encode(); self.checkpoint()
            with self.subTest(heartbeat=heartbeat),self.assertRaisesRegex(ValueError,'no valid heartbeat'):
                resolve_disconnected_run(self.hub,'stopped')
        self.assertFalse(self.commits)

    def test_account_switch_before_recovery_rejects_write(self):
        self.seed_stale_run()
        with self.assertRaisesRegex(PermissionError,'changed before run recovery'):
            resolve_disconnected_run(self.hub,'stopped',expected_context=('another-token','data','models'))
        self.assertFalse(self.commits)

    def test_resolution_access_error_and_failed_write_keep_active_record(self):
        self.seed_stale_run()
        with patch.object(self.hub,'is_maintainer',side_effect=ConnectionError('Access server unavailable')) as access:
            with self.assertRaisesRegex(ConnectionError,'Access server unavailable'):
                resolve_disconnected_run(self.hub,'stopped')
        access.assert_called_once_with(raise_on_error=True)
        before = copy.deepcopy(self.files)
        with patch.object(self.api,'create_commit',side_effect=ConnectionError('Write unavailable')):
            with self.assertRaisesRegex(ConnectionError,'Write unavailable'):
                resolve_disconnected_run(self.hub,'stopped')
        self.assertEqual(self.files,before)

    def test_conflict_during_resolution_rechecks_resumed_heartbeat(self):
        self.seed_stale_run()
        def conflict(**kwargs):
            self.files['data']['training/stopped/status.json'] = json.dumps(
                dict(phase='training',updated_at=time.time())).encode()
            self.revision += 1; self.checkpoint()
            error = RuntimeError('Concurrent update'); error.response = types.SimpleNamespace(status_code=409)
            raise error
        with patch.object(self.api,'create_commit',side_effect=conflict) as write:
            with self.assertRaisesRegex(ValueError,'last two minutes'):
                resolve_disconnected_run(self.hub,'stopped')
        self.assertEqual(write.call_count,1)
        self.assertEqual(json.loads(self.files['data']['training/stopped/status.json'])['phase'],'training')

    def check_request(self):
        self.seed_approved()
        request, bundle = prepare_request(self.hub, Path(__file__).resolve().parents[1])
        queue_request(self.hub, request, bundle)
        (self.root/'cloud_request.json').write_text(json.dumps(request))
        def trial(python, root, **kwargs):
            self.assertTrue(kwargs['training_check'])
            self.assertEqual(kwargs['dataset'].name, 'compiled-check')
            results = {}
            for model in MODEL_FILES:
                artifact = root/'runs/check-candidates'/model
                artifact.parent.mkdir(parents=True, exist_ok=True); artifact.write_bytes(model.encode())
                results[model] = dict(artifact=str(artifact), sha256=hashlib.sha256(model.encode()).hexdigest(),
                                      metrics={'metric':'training-image-score','score':.5}, publishable=False)
            return dict(success=True, results=results, errors=[], publishable=False)
        self.trial.side_effect = trial
        return request

    def test_training_check_has_no_remote_writes_and_uses_only_training_partition(self):
        request = self.check_request()
        before = copy.deepcopy(self.files); commits = list(self.commits)
        with patch.object(tempo_cloud_runner, 'load_context', return_value=(self.root, request, self.hub)):
            report = tempo_cloud_runner.check_training()
            tempo_cloud_runner.verify_training_check(self.root, request, self.hub.model_manifest())
        dataset = json.loads((self.root/'datasets/compiled-check/dataset.json').read_text())
        self.assertEqual({row['name'] for row in dataset['records']},
                         {name for name, partition in request['split_assignments'].items() if partition == 'train'})
        self.assertFalse(report['publishable'])
        self.assertEqual(self.files, before); self.assertEqual(self.commits, commits)
        self.assertFalse((self.root/'runs/training_report.json').exists())

    def test_cancelled_training_check_does_not_launch_workers(self):
        request = self.check_request()
        self.files['data'][f"training/{request['id']}/control.json"] = b'{"cancel":true}'; self.checkpoint()
        with patch.object(tempo_cloud_runner, 'load_context', return_value=(self.root, request, self.hub)):
            with self.assertRaises(InterruptedError): tempo_cloud_runner.check_training()
        self.trial.assert_not_called()

    def test_changed_data_or_checkpoints_invalidate_training_check_before_run_claim(self):
        request = self.check_request()
        with patch.object(tempo_cloud_runner, 'load_context', return_value=(self.root, request, self.hub)):
            report = tempo_cloud_runner.check_training()
        for target in [next((self.root/'datasets/compiled-check').rglob('*.txt')),
                       Path(next(iter(report['results'].values()))['artifact'])]:
            original = target.read_bytes(); target.write_bytes(original + b'changed')
            with self.subTest(file=target.name), patch.object(tempo_cloud_runner, 'load_context', return_value=(self.root, request, self.hub)):
                with self.assertRaisesRegex(ValueError, 'Check Training'): tempo_cloud_runner.run(require_check=True)
            target.write_bytes(original)
        self.assertEqual(json.loads(self.files['data'][f"training/{request['id']}/status.json"])['phase'], 'awaiting_colab')
        self.assertFalse([repo for repo, _ in self.commits if repo == 'models'])

    def test_new_published_release_requires_a_new_training_check(self):
        request = self.check_request()
        with patch.object(tempo_cloud_runner, 'load_context', return_value=(self.root, request, self.hub)):
            tempo_cloud_runner.check_training()
        release = self.hub.model_manifest(); release['revision'] = 'changed-models'
        with self.assertRaisesRegex(ValueError, 'Check Training'):
            tempo_cloud_runner.verify_training_check(self.root, request, release)

    def test_failed_recheck_invalidates_previous_pass_without_deleting_candidates(self):
        request = self.check_request()
        with patch.object(tempo_cloud_runner, 'load_context', return_value=(self.root, request, self.hub)):
            report = tempo_cloud_runner.check_training()
            self.trial.side_effect = RuntimeError('Worker failed')
            with self.assertRaisesRegex(RuntimeError, 'Worker failed'): tempo_cloud_runner.check_training()
        self.assertFalse((self.root/'runs/training_check.json').exists())
        self.assertTrue(all(Path(result['artifact']).is_file() for result in report['results'].values()))

    def test_account_change_during_preparation_and_repository_change_before_queue_are_rejected(self):
        self.seed_approved()
        original = self.hub.model_manifest
        def changed():
            result=original();self.hub.token='different-account';return result
        with patch.object(self.hub,'model_manifest',side_effect=changed):
            with self.assertRaisesRegex(PermissionError,'changed while loading'):
                prepare_request(self.hub,Path(__file__).resolve().parents[1])
        request,bundle=prepare_request(self.hub,Path(__file__).resolve().parents[1])
        request['dataset_repo']='different/dataset'
        with self.assertRaisesRegex(ValueError,'repositories changed'):queue_request(self.hub,request,bundle)
        self.assertFalse(self.commits)

    def test_pending_submission_fetch_checks_credentials_and_access_without_writes(self):
        metadata = {'submission_id': 'reported', 'collaborator_id': 'Researcher', 'image_count': 1}
        self.files['data']['submissions/reported/metadata.json'] = json.dumps(metadata).encode()
        self.checkpoint()
        before = copy.deepcopy(self.files)
        with patch.object(self.hub, 'ensure_credentials') as load, \
             patch.object(self.hub, 'is_maintainer', return_value=True) as access:
            self.assertEqual(self.hub.fetch_pending_submissions(), [metadata])
        load.assert_called(); access.assert_called_once_with(raise_on_error=True)
        self.assertEqual(self.files, before)
        self.assertFalse(self.commits)

    def test_pending_submission_access_failure_is_an_error_not_empty_queue(self):
        with patch.object(self.hub, 'is_maintainer', side_effect=ConnectionError('offline')):
            with self.assertRaises(ConnectionError): self.hub.fetch_pending_submissions()
        with patch.object(self.hub, 'is_maintainer', return_value=False):
            with self.assertRaises(PermissionError): self.hub.fetch_pending_submissions()

    def test_pending_submissions_load_when_full_dataset_metadata_fails(self):
        metadata = {'submission_id': 'reported', 'collaborator_id': 'Researcher', 'image_count': 1}
        self.files['data']['submissions/reported/metadata.json'] = json.dumps(metadata).encode()
        self.checkpoint()
        def dataset_info(**kwargs):
            if set(kwargs.get('expand', [])) != {'sha', 'private'}:
                raise ConnectionError('HTTP 500 fetching full dataset metadata')
            return types.SimpleNamespace(sha=str(self.revision), private=True)
        with patch.object(self.api, 'repo_info', side_effect=dataset_info):
            self.assertEqual(self.hub.fetch_pending_submissions(), [metadata])
        self.assertFalse(self.commits)

    def test_submission_fetch_rejects_public_or_unpinned_dataset(self):
        for info, message in [(types.SimpleNamespace(sha='1', private=False), 'must be private'),
                              (types.SimpleNamespace(sha=None, private=True), 'revision could not be verified'),
                              (types.SimpleNamespace(sha='', private=True), 'revision could not be verified')]:
            with self.subTest(info=info), patch.object(self.api, 'repo_info', return_value=info), \
                 patch.object(self.api, 'list_repo_files') as files:
                with self.assertRaisesRegex((RuntimeError, ValueError), message):
                    self.hub.fetch_pending_submissions()
                files.assert_not_called()
        self.assertFalse(self.commits)

    def test_cuda_requirements_are_linux_only_and_preserve_windows_cpu_tensorflow(self):
        from packaging.requirements import Requirement
        path=Path(__file__).resolve().parents[1]/'requirements-training.txt'
        lines = path.read_text().splitlines()
        self.assertIn('--extra-index-url https://download.pytorch.org/whl/cu126', lines)
        requirements=[Requirement(line) for line in lines if line and not line.startswith(('#', '--'))]
        def selected(system,machine):
            environment={'platform_system':system,'platform_machine':machine}
            return {row.name:row for row in requirements if row.marker is None or row.marker.evaluate(environment)}
        linux=selected('Linux','x86_64')
        self.assertIn('and-cuda',linux['tensorflow'].extras)
        self.assertIn('nvidia-cuda-nvcc-cu12',linux)
        sources=json.loads((path.parent/'installer/dependency-sources.json').read_text())['sources']
        for name,requirement in linux.items():
            with self.subTest(package=name):
                if requirement.url:
                    self.assertRegex(requirement.url,r'^https://github\.com/[^/]+/[^/]+/archive/[0-9a-f]{40}\.zip$')
                    reviewed=[source for source in sources if name in source['required_by']]
                    self.assertTrue(reviewed,'Direct source dependency requires a reviewed source entry')
                    self.assertIn(requirement.url,[source['url'] for source in reviewed])
                else:
                    self.assertEqual(len(requirement.specifier),1)
                    self.assertEqual(next(iter(requirement.specifier)).operator,'==')
        self.assertEqual(str(linux['opencv-python'].specifier),str(linux['opencv-python-headless'].specifier))
        self.assertFalse(any(name.endswith('-cu13') for name in linux))
        for system,machine in [('Windows','AMD64'),('Darwin','arm64')]:
            other=selected(system,machine)
            self.assertFalse(other['tensorflow'].extras)
            self.assertFalse(any(name.startswith('nvidia-') for name in other))

    def test_source_bundle_is_identical_with_windows_or_unix_text_endings(self):
        from tempo_cloud import archive_sources
        expected = archive_sources(self.root)
        for name in BUNDLE_FILES:
            path = self.root/name
            path.write_bytes(path.read_text(encoding='utf-8').replace('\n','\r\n').encode('utf-8'))
        self.assertEqual(archive_sources(self.root),expected)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for name in BUNDLE_FILES:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes((Path(__file__).resolve().parents[1]/name).read_bytes())
        self.files={'data':{},'models':{}}
        self.revision=1; self.history={}; self.commits=[]
        self.hub=TempoHub(); self.hub.token='fake'
        self.hub._require_actor=lambda requested=None: requested or 'FixtureReviewer'
        self.hub.authenticated_account=lambda:'FixtureReviewer'
        self.hub._prepare_manifest=lambda manifest,revision:(manifest,{})
        self.hub.is_maintainer=lambda **kwargs:True
        self.hub.get_dataset_repo=lambda:'data'
        self.hub.get_models_repo=lambda:'models'
        self.api=types.SimpleNamespace(repo_info=lambda **kw:types.SimpleNamespace(sha=str(self.revision),private=True),
                                       create_commit=self.commit,
                                       list_repo_files=lambda repo_id,**kw:list(self.files[repo_id]))
        self.hub.api=self.api
        self.files['data']['lock_manifest.json']=json.dumps({'images':{}}).encode()
        self.hub._read=self.read
        self.hub.download_image=self.image
        self.addCleanup(patch.stopall)
        patch('tempo_hub.LOCAL_LOCK_CACHE',str(self.root/'cache.json')).start()
        patch('tempo_cloud_runner.check_training_environment').start()
        patch('tempo_cloud_runner.configure_cuda_environment', return_value={}).start()
        patch.dict(os.environ, {'HF_TOKEN': 'synthetic-colab-secret'}).start()
        self.trial = patch('tempo_cloud_runner.check_training_steps').start()
        patch('huggingface_hub.HfApi',return_value=self.api).start()
        patch('huggingface_hub.hf_hub_download',side_effect=self.download).start()
        patch('tempo_hub.hf_hub_download',side_effect=self.download).start()

    def checkpoint(self):
        self.history[str(self.revision)]=copy.deepcopy(self.files)

    def commit(self, repo_id, operations, parent_commit=None, **kwargs):
        if parent_commit and parent_commit != str(self.revision):
            raise RuntimeError('Unexpected stale remote revision')
        for operation in operations:
            value=operation.path_or_fileobj
            self.files[repo_id][operation.path_in_repo]=value if isinstance(value,bytes) else Path(value).read_bytes()
        self.commits.append((repo_id,[op.path_in_repo for op in operations]))
        self.revision+=1; self.checkpoint()

    def read(self,name,revision,default=None,repo_type='dataset'):
        files=self.history.get(str(revision),self.files)['data' if repo_type=='dataset' else 'models']
        if name not in files:
            if default is not None:return copy.deepcopy(default)
            raise EntryNotFoundError(name)
        return json.loads(files[name])

    def download(self,repo_id,filename,revision=None,**kwargs):
        files=self.history.get(str(revision),self.files)[repo_id]
        if filename not in files:raise EntryNotFoundError(filename)
        path=self.root/'downloads'/repo_id/str(revision)/filename
        path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(files[filename]); return str(path)

    def image(self,name,revision=None):
        source=self.download('data','images/'+name,revision)
        target=self.root/'datasets/universal_images'/name
        target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(Path(source).read_bytes())
        return str(target)

    def seed_approved(self):
        records={}
        for i in range(4):
            name=f'{i}.png'
            records[name]={'status':'approved' if i<3 else 'locked','source_well':f'well-{i}','width':64,'height':64,'coordinate_space':'normalized',
                           'boxes':[{'class_id':0,'polygon':[[.1,.1],[.5,.1],[.5,.5],[.1,.5]]}]}
            self.files['data']['images/'+name]=cv2.imencode('.png',np.zeros((64,64),np.uint8))[1].tobytes()
        self.files['data']['universal_annotations.json']=json.dumps(records).encode();self.checkpoint()

    def test_pending_edits_update_existing_submission_atomically_until_approval(self):
        record = {'width': 64, 'height': 64, 'coordinate_space': 'normalized',
                  'boxes': [{'class_id': 0, 'polygon': [[.1, .1], [.5, .1], [.5, .5]]}]}
        lease = dict(TempoHub._lease('Alice'), status='submitted', submission_id='pending')
        self.files['data']['lock_manifest.json'] = json.dumps({'images': {'a.png': lease}}).encode()
        untouched = copy.deepcopy(record)
        self.files['data']['submissions/pending/annotations.json'] = json.dumps({'a.png': record, 'b.png': untouched}).encode()
        self.checkpoint()
        edited = copy.deepcopy(record); edited['boxes'][0]['class_id'] = 1
        self.assertEqual(self.hub.sync_annotation_drafts('Alice', {'a.png': edited}), 1)
        submission = json.loads(self.files['data']['submissions/pending/annotations.json'])
        drafts = json.loads(self.files['data']['annotation_drafts.json'])
        self.assertEqual(submission['a.png'], drafts['a.png'])
        self.assertEqual(submission['a.png']['boxes'][0]['class_id'], 1)
        self.assertEqual(submission['b.png']['boxes'], untouched['boxes'])
        self.assertEqual(len(self.commits), 1)
        self.assertIn('submissions/pending/annotations.json', self.commits[0][1])
        self.assertIn('annotation_drafts.json', self.commits[0][1])
        self.assertNotIn('submissions/pending/metadata.json', self.commits[0][1])
        self.files['data']['lock_manifest.json'] = json.dumps({'images': {'a.png': {'status': 'approved'}}}).encode()
        self.checkpoint()
        before = copy.deepcopy(self.files)
        with self.assertRaises(ValueError):
            self.hub.sync_annotation_drafts('Alice', {'a.png': record})
        self.assertEqual(self.files, before)
        self.assertEqual(len(self.commits), 1)

    def test_approved_handoff_auto_publication_and_collaborator_update(self):
        self.seed_approved()
        request,bundle=prepare_request(self.hub,Path(__file__).resolve().parents[1],epochs=1)
        self.assertEqual(request['image_count'],3)
        self.assertEqual({row['name'] for row in request['images']}, {'0.png','1.png','2.png'})
        self.assertEqual(sum(row['annotations'] for row in request['images']),3)
        import zipfile
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:self.assertEqual(set(archive.namelist()),set(BUNDLE_FILES))
        if getattr(self, 'multi_epoch', False):
            request['epochs'] = 7
        queue_request(self.hub,request,bundle)
        (self.root/'cloud_request.json').write_text(json.dumps(request))
        def simulated_training(command,**kwargs):
            self.assertIn('--sequential',command)
            self.assertEqual(command[command.index('--epochs')+1], str(request['epochs']))
            dataset=json.loads((self.root/'datasets/compiled/dataset.json').read_text())
            self.assertEqual(len(dataset['records']),3)
            self.assertNotIn('3.png',{r['name'] for r in dataset['records']})
            results={}
            for model,name in MODEL_FILES.items():
                path=self.root/name;path.write_bytes(('new '+model).encode())
                results[model]=dict(artifact=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                   metrics={'score':0.0},promote=False)
            report=dict(success=True,origin='approved-master',approval_policy=APPROVAL_POLICY,split_policy=dataset['split_policy'],run_id='release-1',
                        results=results,remote_baselines={},test_fingerprint=dataset['test_fingerprint'])
            (self.root/'runs/training_report.json').write_text(json.dumps(report))
        with patch('tempo_hub.tempo_hub',self.hub),patch.object(self.hub,'_init_credentials'), \
             patch('tempo_config.tempo_preferences.set'),patch.dict('os.environ',{'HF_TOKEN':'fake'}), \
             patch.object(tempo_cloud_runner,'require_colab'), \
             patch.object(tempo_cloud_runner.subprocess,'check_output',return_value='NVIDIA A100'), \
             patch.object(tempo_cloud_runner,'run_logged',side_effect=simulated_training):
            tempo_cloud_runner.run(self.root, preflight=True)
            summary = tempo_cloud_runner.summarize_release(self.root)
            self.assertEqual(summary['request_id'], request['id'])
            self.assertEqual(len(summary['models']), 3)
            if not getattr(self, 'multi_epoch', False):
                self.trial.assert_not_called()
        state=json.loads(self.files['data'][f"training/{request['id']}/status.json"])
        self.assertEqual(state['phase'],'completed')
        self.assertEqual(state['release_id'],'release-1')
        manifest=self.hub.model_manifest()
        target=self.root/'collaborator'
        self.assertEqual(self.hub.download_model_release(manifest,target),3)
        for name,record in manifest['models'].items():
            self.assertEqual(hashlib.sha256((target/name).read_bytes()).hexdigest(),record['sha256'])
        model_commits=[paths for repo,paths in self.commits if repo=='models']
        self.assertEqual(len(model_commits),1)
        self.assertTrue(set(MODEL_FILES.values()) <= set(model_commits[0]))

    def test_multi_epoch_job_trials_once_then_trains_requested_epochs(self):
        self.multi_epoch = True
        self.test_approved_handoff_auto_publication_and_collaborator_update()
        self.trial.assert_called_once()
        self.assertTrue(callable(self.trial.call_args.kwargs['cancelled']))

    def test_failed_or_cancelled_trial_never_trains_or_publishes(self):
        for error in (RuntimeError('Trial failed'), InterruptedError('Trial cancelled')):
            with self.subTest(error=type(error).__name__):
                self.seed_approved()
                request,bundle=prepare_request(self.hub,Path(__file__).resolve().parents[1],epochs=5)
                queue_request(self.hub,request,bundle)
                (self.root/'cloud_request.json').write_text(json.dumps(request))
                with patch('tempo_hub.tempo_hub',self.hub),patch.object(self.hub,'_init_credentials'), \
                     patch('tempo_config.tempo_preferences.set'),patch.dict('os.environ',{'HF_TOKEN':'fake'}), \
                     patch.object(tempo_cloud_runner,'require_colab'), \
                     patch.object(tempo_cloud_runner.subprocess,'check_output',return_value='NVIDIA A100'), \
                     patch.object(tempo_cloud_runner,'check_training_steps',side_effect=error), \
                     patch.object(tempo_cloud_runner,'run_logged') as launch:
                    with self.assertRaises(type(error)):tempo_cloud_runner.run(self.root,preflight=True)
                launch.assert_not_called()
                state=json.loads(self.files['data'][f"training/{request['id']}/status.json"])
                self.assertEqual(state['phase'], 'cancelled' if isinstance(error,InterruptedError) else 'failed')
        self.assertFalse([repo for repo, paths in self.commits if repo=='models'])

    def test_read_only_preview_keeps_pending_state_and_excludes_later_approvals(self):
        self.seed_approved()
        request,bundle=prepare_request(self.hub,Path(__file__).resolve().parents[1],epochs=12)
        queue_request(self.hub,request,bundle)
        (self.root/'cloud_request.json').write_text(json.dumps(request))
        before = len(self.commits)
        records=json.loads(self.files['data']['universal_annotations.json'])
        records['3.png']['status']='approved'
        self.files['data']['universal_annotations.json']=json.dumps(records).encode()
        self.revision+=1;self.checkpoint()
        with patch('tempo_hub.tempo_hub',self.hub),patch.object(self.hub,'_init_credentials'), \
             patch('tempo_config.tempo_preferences.set'),patch.object(tempo_cloud_runner,'require_colab'):
            preview=tempo_cloud_runner.inspect_request(self.root)
        self.assertEqual(preview['image_count'],3)
        self.assertEqual(preview['epochs'],12)
        self.assertEqual(len(self.commits),before)
        self.assertEqual(json.loads(self.files['data'][f"training/{request['id']}/status.json"])['phase'],'awaiting_colab')

    def test_changed_handoff_refused_before_claim(self):
        self.seed_approved()
        request,bundle=prepare_request(self.hub,Path(__file__).resolve().parents[1],epochs=1)
        queue_request(self.hub,request,bundle)
        (self.root/'cloud_request.json').write_text(json.dumps(dict(request,bundle_sha256='outdated')))
        before=len(self.commits)
        with patch('tempo_hub.tempo_hub',self.hub),patch.object(self.hub,'_init_credentials'), \
             patch('tempo_config.tempo_preferences.set'),patch.dict('os.environ',{'HF_TOKEN':'fake'}), \
             patch.object(tempo_cloud_runner,'require_colab'), \
             patch.object(tempo_cloud_runner.subprocess,'check_output',return_value='NVIDIA A100'):
            with self.assertRaisesRegex(ValueError,'code changed'):tempo_cloud_runner.run(self.root)
        self.assertEqual(len(self.commits),before)

    def test_baseline_weights_verified_at_pinned_revision_before_training(self):
        self.seed_approved()
        content=b'champion'
        name=MODEL_FILES['nano_scout']
        self.files['models'].update({name:content, 'model_manifest.json':json.dumps({'models':{
            name:{'sha256':hashlib.sha256(content).hexdigest(),'version':'previous'}}}).encode()})
        self.checkpoint()
        request,bundle=prepare_request(self.hub,Path(__file__).resolve().parents[1],epochs=1)
        queue_request(self.hub,request,bundle)
        (self.root/'cloud_request.json').write_text(json.dumps(request))
        def training(command,**kwargs):
            self.assertEqual((self.root/name).read_bytes(),content)
            self.assertEqual(json.loads((self.root/'runs/champion_baselines.json').read_text())[name],hashlib.sha256(content).hexdigest())
            raise RuntimeError('Stop simulated training after verified baseline')
        with patch('tempo_hub.tempo_hub',self.hub),patch.object(self.hub,'_init_credentials'), \
             patch('tempo_config.tempo_preferences.set'),patch.dict('os.environ',{'HF_TOKEN':'fake'}), \
             patch.object(tempo_cloud_runner,'require_colab'), \
             patch.object(tempo_cloud_runner.subprocess,'check_output',return_value='NVIDIA A100'), \
             patch.object(tempo_cloud_runner,'run_logged',side_effect=training):
            with self.assertRaisesRegex(RuntimeError,'verified baseline'):tempo_cloud_runner.run(self.root)

    def test_summary_rejects_foreign_request_and_changed_remote_artifact(self):
        self.test_approved_handoff_auto_publication_and_collaborator_update()
        with patch('tempo_hub.tempo_hub',self.hub),patch.object(self.hub,'_init_credentials'), \
             patch('tempo_config.tempo_preferences.set'),patch.dict('os.environ',{'HF_TOKEN':'fake'}), \
             patch.object(tempo_cloud_runner,'require_colab'):
            report=json.loads((self.root/'runs/training_report.json').read_text())
            original=copy.deepcopy(report)
            report['cloud_request_id']='foreign'
            (self.root/'runs/training_report.json').write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError,'belongs to this run'):tempo_cloud_runner.summarize_release(self.root)
            (self.root/'runs/training_report.json').write_text(json.dumps(original))
            self.files['models'][MODEL_FILES['nano_scout']]=b'corrupt';self.revision+=1;self.checkpoint()
            # A later revision must not change verification of the pinned saved run.
            tempo_cloud_runner.summarize_release(self.root)
            revision=original['publication_revision']
            self.history[revision]['models'][MODEL_FILES['nano_scout']]=b'corrupt'
            with self.assertRaisesRegex(ValueError,'remote model checksum'):tempo_cloud_runner.summarize_release(self.root)

    def test_windows_bundle_can_prepare_a_cloud_request_from_packaged_sources(self):
        from tests.packaging_fixtures import stage_spec_inputs
        repository = stage_spec_inputs(Path(__file__).resolve().parents[1], self.root/'spec-source')
        hooks = types.ModuleType('PyInstaller.utils.hooks')
        hooks.collect_data_files = lambda name: []
        hooks.collect_submodules = lambda name: []
        hooks.copy_metadata = lambda name: []
        def analysis(*args, **kwargs):
            self.assertNotIn('tensorflow', kwargs['excludes'])
            self.assertNotIn('torchvision', kwargs['excludes'])
            self.assertTrue({'tensorflow','torchvision','stardist.models','cellpose.models'}.issubset(kwargs['hiddenimports']))
            return types.SimpleNamespace(pure=[], zipped_data=[], scripts=[], binaries=[],
                                         zipfiles=[], datas=kwargs['datas'])
        namespace = {'SPEC': str(repository/'tempo.spec'),
                     'Analysis': analysis, 'PYZ': lambda *a, **k: None,
                     'EXE': lambda *a, **k: None, 'COLLECT': lambda *a, **k: None}
        with patch('scripts.windows_native_bundle.assemble_windows_cpp_runtime',side_effect=lambda binaries,root:binaries), \
             patch('scripts.windows_native_bundle.collect_wheel_libraries',return_value=([],[],{'success':True,'files':[]})), \
             patch('scripts.collect_licenses.collect',return_value={'packages':[]}), \
             patch.dict(os.environ, {'TEMPO_WINDOWS_PROFILE': 'cpu'}), \
             patch.object(sys, 'path', list(sys.path)), \
             patch.object(sys, 'platform', 'win32'), \
             patch.dict(sys.modules, {'PyInstaller': types.ModuleType('PyInstaller'),
                                     'PyInstaller.utils': types.ModuleType('PyInstaller.utils'),
                                     'PyInstaller.utils.hooks': hooks}):
            exec(compile((repository/'tempo.spec').read_text(), 'tempo.spec', 'exec'), namespace)
        packaged = self.root/'packaged-runtime'
        for source, destination in namespace['a'].datas:
            if Path(source).relative_to(repository).as_posix() in BUNDLE_FILES:
                target = packaged/destination/Path(source).name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        self.seed_approved()
        request, bundle = prepare_request(self.hub, packaged, epochs=1)
        self.assertEqual(request['image_count'], 3)
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            for name in BUNDLE_FILES:
                self.assertEqual(archive.read(name), (repository/name).read_bytes())
        self.assertEqual(self.commits, [])

    def test_duplicate_request_refused(self):
        self.seed_approved()
        request,bundle=prepare_request(self.hub,Path(__file__).resolve().parents[1])
        queue_request(self.hub,request,bundle)
        with self.assertRaisesRegex(ValueError,'already active'):
            queue_request(self.hub,request,bundle)

    def test_lost_cloud_queue_acknowledgement_confirms_whole_bundle_once(self):
        self.seed_approved()
        request, bundle = prepare_request(self.hub, Path(__file__).resolve().parents[1], epochs=1)
        original = self.api.create_commit
        def lost_response(**kwargs):
            original(**kwargs)
            raise ConnectionError('Accepted queue response lost')
        self.api.create_commit = lost_response
        self.assertEqual(queue_request(self.hub, request, bundle), request)
        self.assertEqual(len(self.commits), 1)
        self.assertEqual(self.files['data'][f"training/{request['id']}/bundle.zip"], bundle)

    def test_lost_publication_acknowledgement_reconciles_artifacts_and_status(self):
        original = self.api.create_commit
        def lost_model_response(**kwargs):
            original(**kwargs)
            if kwargs['repo_id'] == 'models':
                raise ConnectionError('Accepted model release response lost')
        self.api.create_commit = lost_model_response
        self.test_approved_handoff_auto_publication_and_collaborator_update()

    def test_lost_status_acknowledgement_reconciles_actual_final_phase(self):
        original = self.api.create_commit
        def lost_status_response(**kwargs):
            original(**kwargs)
            if kwargs.get('commit_message') == 'Update cloud training status':
                raise ConnectionError('Accepted status response lost')
        self.api.create_commit = lost_status_response
        self.test_approved_handoff_auto_publication_and_collaborator_update()

    def test_unconfirmed_final_status_does_not_claim_success_or_repeat_publication(self):
        original = self.api.create_commit
        def unavailable_final_status(**kwargs):
            if kwargs.get('commit_message') == 'Update cloud training status':
                raise ConnectionError('Final status unavailable before send')
            return original(**kwargs)
        self.api.create_commit = unavailable_final_status
        with self.assertRaisesRegex(RuntimeError,'completion could not be confirmed'):
            self.test_approved_handoff_auto_publication_and_collaborator_update()
        state=json.loads(self.files['data'][next(name for name in self.files['data'] if name.endswith('/status.json'))])
        self.assertNotEqual(state['phase'],'completed')
        self.assertEqual(sum(repo=='models' for repo,paths in self.commits),1)
        self.assertEqual(set(self.hub.model_manifest()['models']),set(MODEL_FILES.values()))

    def test_unconfirmed_changed_release_never_reports_completed(self):
        original = self.api.create_commit
        def changed_release_without_response(**kwargs):
            original(**kwargs)
            if kwargs['repo_id'] == 'models':
                self.files['models'][MODEL_FILES['nano_scout']] = b'changed remotely'
                self.checkpoint()
                raise ConnectionError('Model release response lost')
        self.api.create_commit = changed_release_without_response
        with self.assertRaisesRegex(ConnectionError, 'Model release response lost'):
            self.test_approved_handoff_auto_publication_and_collaborator_update()
        states = [json.loads(value) for name, value in self.files['data'].items() if name.endswith('/status.json')]
        self.assertEqual(states[-1]['phase'], 'failed')
        self.assertEqual(sum(repo == 'models' for repo, paths in self.commits), 1)

    def test_corrupt_release_replaces_nothing(self):
        self.files['models'].update({'a.pt':b'new','b.pt':b'corrupt'});self.checkpoint()
        manifest={'revision':str(self.revision),'models':{n:{'sha256':hashlib.sha256(b'new').hexdigest()} for n in ('a.pt','b.pt')}}
        target=self.root/'installed';target.mkdir()
        for name in manifest['models']:(target/name).write_bytes(b'old')
        with self.assertRaisesRegex(ValueError,'checksum'):
            self.hub.download_model_release(manifest,target)
        self.assertEqual((target/'a.pt').read_bytes(),b'old')
        self.assertEqual((target/'b.pt').read_bytes(),b'old')

    def test_connection_has_no_credentials_and_is_valid_python(self):
        cell=connection_cell('owner/private-data',models_repo='owner/private-models')
        compile(cell,'connection','exec')
        self.assertIn('userdata.get',cell)
        self.assertNotIn('fake',cell)
        self.assertIn('nvidia-smi',cell)

    def test_local_runner_refused(self):
        with patch.object(tempo_cloud_runner.sys,'platform','darwin'):
            with self.assertRaisesRegex(RuntimeError,'Local training is disabled'):
                tempo_cloud_runner.require_colab()

    def test_training_failure_never_publishes(self):
        self.seed_approved()
        request,bundle=prepare_request(self.hub,Path(__file__).resolve().parents[1],epochs=1)
        queue_request(self.hub,request,bundle)
        (self.root/'cloud_request.json').write_text(json.dumps(request))
        with patch('tempo_hub.tempo_hub',self.hub),patch.object(self.hub,'_init_credentials'), \
             patch('tempo_config.tempo_preferences.set'),patch.dict('os.environ',{'HF_TOKEN':'fake'}), \
             patch.object(tempo_cloud_runner,'require_colab'), \
             patch.object(tempo_cloud_runner.subprocess,'check_output',return_value='NVIDIA A100'), \
             patch.object(tempo_cloud_runner,'run_logged',side_effect=RuntimeError('Worker failed')):
            with self.assertRaisesRegex(RuntimeError,'Worker failed'):tempo_cloud_runner.run(self.root)
        state=json.loads(self.files['data'][f"training/{request['id']}/status.json"])
        self.assertEqual(state['phase'],'failed')
        self.assertFalse([r for r,_ in self.commits if r=='models'])

    def test_wrong_gpu_stops_before_claim_or_training(self):
        self.seed_approved()
        request,bundle=prepare_request(self.hub,Path(__file__).resolve().parents[1])
        queue_request(self.hub,request,bundle)
        (self.root/'cloud_request.json').write_text(json.dumps(request))
        with patch('tempo_hub.tempo_hub',self.hub),patch.object(self.hub,'_init_credentials'), \
             patch('tempo_config.tempo_preferences.set'),patch.dict('os.environ',{'HF_TOKEN':'fake'}), \
             patch.object(tempo_cloud_runner,'require_colab'), \
             patch.object(tempo_cloud_runner.subprocess,'check_output',return_value='NVIDIA T4'), \
             patch.object(tempo_cloud_runner,'run_logged') as launch:
            with self.assertRaisesRegex(RuntimeError,'A100 requested'):tempo_cloud_runner.run(self.root)
        launch.assert_not_called()
        state=json.loads(self.files['data'][f"training/{request['id']}/status.json"])
        self.assertEqual(state['phase'],'awaiting_colab')


class ColabOwnedWorkflowTests(unittest.TestCase):
    setUp = CloudWorkflowTests.setUp
    checkpoint = CloudWorkflowTests.checkpoint
    commit = CloudWorkflowTests.commit
    read = CloudWorkflowTests.read
    download = CloudWorkflowTests.download
    image = CloudWorkflowTests.image
    seed_approved = CloudWorkflowTests.seed_approved

    def setup_notebook(self, root=None, epochs=1):
        from tempo_cloud import archive_sources
        self.root = root or self.root
        self.root.mkdir(exist_ok=True)
        source=Path(__file__).resolve().parents[1]
        for name in BUNDLE_FILES:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((source/name).read_bytes())
        payload=archive_sources(self.root)
        (self.root/'notebook_settings.json').write_text(json.dumps(dict(run_id='a'*32,dataset_repo='data',models_repo='models',
            epochs=epochs,gpu='A100',bundle_sha256=hashlib.sha256(payload).hexdigest())))

    def seed_current(self):
        models={}
        for model,name in MODEL_FILES.items():
            value=('old '+model).encode();self.files['models'][name]=value
            models[name]=dict(sha256=hashlib.sha256(value).hexdigest(),version='prior',metrics={'metric':'fixture-score','score':.5})
        self.files['models']['model_manifest.json']=json.dumps({'models':models}).encode();self.checkpoint()

    def simulate_training(self, command, **kwargs):
        from tempo_training import validate_training_dataset
        self.assertIn('--sequential',command)
        info=validate_training_dataset(self.root/'datasets/compiled')
        results={}
        for model,name in MODEL_FILES.items():
            artifact=self.root/'runs/candidates'/name;artifact.parent.mkdir(parents=True,exist_ok=True)
            artifact.write_bytes(('new '+model).encode())
            previous=self.current_before.get(name)
            results[model]=dict(artifact=str(artifact),sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
                metrics={'metric':'fixture-score','score':getattr(self,'scores',{}).get(model,.6)},
                baseline_metrics={'metric':'fixture-score','score':.5} if previous else None,publishable=True)
        report=dict(success=True,origin='approved-master',approval_policy=APPROVAL_POLICY,split_policy=info['split_policy'],
            run_id=getattr(self,'next_run_id','run-1'),results=results,remote_baselines={name:row['sha256'] for name,row in self.current_before.items()},
            test_fingerprint=info['test_fingerprint'],evaluation_partition=info['evaluation_partition'],evaluation_basis=info['evaluation_basis'])
        (self.root/'runs/training_report.json').write_text(json.dumps(report))

    def execute_run(self, side_effect=None):
        self.current_before=self.hub.model_manifest()['models']
        with patch('tempo_hub.tempo_hub',self.hub),patch('tempo_config.tempo_preferences.set'), \
             patch.object(tempo_cloud_runner,'require_colab'), \
             patch.object(tempo_cloud_runner.subprocess,'check_output',return_value='NVIDIA A100'), \
             patch.object(tempo_cloud_runner,'run_logged',side_effect=side_effect or self.simulate_training):
            return tempo_cloud_runner.create_run(self.root)

    def test_run_all_registers_without_desktop_request_and_preserves_annotations(self):
        self.seed_approved();self.setup_notebook()
        original={name:self.files['data'][name] for name in ('universal_annotations.json','lock_manifest.json')}
        summary=self.execute_run()
        self.assertEqual(summary['updated_count'],3)
        request=json.loads(self.files['data']['training/request.json'])
        self.assertEqual(request['image_count'],3)
        self.assertEqual(set(request['split_assignments'].values()),{'train','val','test'})
        self.assertEqual(request['source_groups'],{})
        self.assertEqual({name:self.files['data'][name] for name in original},original)
        for repo,paths in self.commits:
            if repo=='data':self.assertTrue(all(path.startswith('training/') for path in paths))
        self.trial.assert_not_called()

    def test_higher_equal_and_lower_scores_update_only_improvements(self):
        self.seed_approved();self.seed_current();self.setup_notebook()
        self.scores=dict(nano_scout=.7,yolo_seg=.5,stardist=.4,cellpose=.6)
        summary=self.execute_run()
        self.assertEqual(summary['updated_count'],2)
        models=self.hub.model_manifest()['models']
        for kind,name in MODEL_FILES.items():
            self.assertEqual(models[name]['version'],'run-1' if kind in ('nano_scout','cellpose') else 'prior')
            self.assertTrue(summary['models'][kind]['remote_verified'])
        self.assertNotIn('yolo_seg',summary['models'])
        model_commits=[paths for repo,paths in self.commits if repo=='models']
        self.assertEqual(len(model_commits),1)
        self.assertTrue(all(f'runs/run-1/weights/{name}' in model_commits[0] for name in MODEL_FILES.values()))

    def test_no_improvement_archives_results_and_keeps_current_manifest(self):
        self.seed_approved();self.seed_current();original=self.files['models']['model_manifest.json'];self.setup_notebook()
        self.scores={kind:.5 for kind in MODEL_FILES}
        summary=self.execute_run()
        self.assertEqual(summary['updated_count'],0)
        self.assertEqual(self.files['models']['model_manifest.json'],original)
        self.assertIn('runs/run-1/report.json',self.files['models'])
        self.assertIn('training/split_assignments.json',self.files['data'])

    def test_single_image_can_train_but_cannot_replace_existing_models(self):
        self.seed_approved();self.seed_current()
        records=json.loads(self.files['data']['universal_annotations.json'])
        for name in ('1.png','2.png'):records[name]['status']='draft'
        self.files['data']['universal_annotations.json']=json.dumps(records).encode();self.checkpoint()
        self.setup_notebook();summary=self.execute_run()
        self.assertEqual(summary['updated_count'],0)
        self.assertEqual(summary['evaluation_basis'],'training-images')
        self.assertEqual(len(json.loads((self.root/'datasets/compiled/dataset.json').read_text())['records']),1)

    def test_single_image_can_save_first_models_without_an_installation_gate(self):
        self.seed_approved()
        records=json.loads(self.files['data']['universal_annotations.json'])
        for name in ('1.png','2.png'):records[name]['status']='draft'
        self.files['data']['universal_annotations.json']=json.dumps(records).encode();self.checkpoint()
        self.setup_notebook();summary=self.execute_run()
        self.assertEqual(summary['updated_count'],3)
        self.assertTrue(all(row['outcome']=='First model saved' for row in summary['models'].values()))

    def test_rerunning_registered_cell_cannot_repeat_training_or_publication(self):
        self.seed_approved();self.setup_notebook();self.execute_run();before=len(self.commits)
        with self.assertRaisesRegex(ValueError,'already registered'):self.execute_run()
        self.assertEqual(len(self.commits),before)

    def test_active_remote_run_blocks_another_notebook_session(self):
        self.seed_approved()
        self.files['data']['training/request.json']=b'{"id":"active"}'
        self.files['data']['training/active/status.json']=b'{"phase":"training"}';self.checkpoint();self.setup_notebook()
        with self.assertRaisesRegex(ValueError,'already active'):self.execute_run()
        self.assertFalse(self.commits)

    def test_multi_epoch_trial_uses_only_training_images_and_continues_automatically(self):
        self.seed_approved();self.setup_notebook(epochs=7)
        def trial(python,root,**kwargs):
            info=json.loads((kwargs['dataset']/'dataset.json').read_text())
            self.assertEqual({row['split'] for row in info['records']},{'train'})
            self.assertTrue(kwargs['training_check'])
        self.trial.side_effect=trial
        summary=self.execute_run();self.trial.assert_called_once()
        self.assertEqual(summary['updated_count'],3)

    def test_new_approvals_enter_successive_runs_with_previous_assignments_kept(self):
        self.seed_approved();self.setup_notebook();self.execute_run()
        original_root=self.root
        first=json.loads(self.files['data']['training/split_assignments.json'])
        records=json.loads(self.files['data']['universal_annotations.json']);records['3.png']['status']='approved'
        self.files['data']['universal_annotations.json']=json.dumps(records).encode();self.revision+=1;self.checkpoint()
        second=original_root/'second-notebook';self.setup_notebook(second);self.next_run_id='run-2'
        self.execute_run()
        request=json.loads(self.files['data']['training/request.json'])
        self.assertEqual(request['image_count'],4)
        self.assertEqual({name:request['split_assignments'][name] for name in first},first)
        self.assertTrue((original_root/'runs/training_report.json').exists())
        self.assertIn('runs/run-1/report.json',self.files['models'])

    def test_failed_training_or_nan_scores_preserve_current_models(self):
        for failure in ('worker','nan'):
            with self.subTest(failure=failure):
                self.seed_approved();self.seed_current();self.setup_notebook(Path(self.tmp.name)/('failure-'+failure))
                before=dict(self.files['models'])
                if failure=='nan':self.scores={'cellpose':float('nan')}
                with self.assertRaises((RuntimeError,ValueError)):
                    self.execute_run(side_effect=RuntimeError('Worker failed') if failure=='worker' else None)
                self.assertEqual(self.files['models'],before)
                request=json.loads(self.files['data']['training/request.json'])
                self.assertEqual(json.loads(self.files['data'][f"training/{request['id']}/status.json"])['phase'],'failed')

    def test_source_changes_rejected_before_registration(self):
        self.seed_approved();self.setup_notebook();(self.root/'tempo_training.py').write_text('# changed')
        with self.assertRaisesRegex(ValueError,'Training code changed'):self.execute_run()
        self.assertFalse(self.commits)

    def test_notebook_run_all_reaches_real_registration_dataset_and_update_paths(self):
        from tests.test_staged_colab_notebook import StagedColabNotebookTests
        notebook=StagedColabNotebookTests();notebook.setUp();self.addCleanup(notebook.doCleanups)
        self.seed_approved()
        self.files['fixture/data']=self.files['data'];self.files['fixture/models']=self.files['models'];self.checkpoint()
        self.hub.get_dataset_repo=lambda:'fixture/data';self.hub.get_models_repo=lambda:'fixture/models'
        notebook.execute(0);notebook.environment();self.root=notebook.namespace['root']
        def execute(command,**kwargs):
            self.assertEqual(command[-2:],['--stage','start'])
            return self.execute_run()
        notebook.namespace['run_logged']=execute
        notebook.execute(2);notebook.execute(3)
        self.assertIn('Current models updated: 3',notebook.output.getvalue())
        self.assertEqual(json.loads(self.files['data']['training/request.json'])['epochs'],150)
        self.assertTrue(notebook.namespace['training_ready']);self.trial.assert_called_once()
        for files in self.files.values():
            for name,value in files.items():
                if name.endswith('.json'):self.assertNotIn(b'fixture-secret-never-print',value)

    def test_permission_or_current_model_fetch_error_cannot_bootstrap_models(self):
        self.seed_approved();self.setup_notebook()
        for error in (PermissionError('No access'),ConnectionError('Connection failed')):
            with patch.object(self.hub,'model_manifest',side_effect=error):
                with self.assertRaises(type(error)):self.execute_run()
        self.assertFalse(self.commits)

    def test_compiled_label_change_during_training_prevents_saving(self):
        self.seed_approved();self.seed_current();self.setup_notebook();before=dict(self.files['models'])
        def changed(command,**kwargs):
            self.simulate_training(command,**kwargs)
            label=next((self.root/'datasets/compiled/cells/labels/train').glob('*.txt'))
            label.write_text('0 0 0 1 0 1 1\n')
        with self.assertRaisesRegex(ValueError,'annotations changed'):self.execute_run(side_effect=changed)
        self.assertEqual(self.files['models'],before)

    def test_error_status_redacts_colab_secret(self):
        self.seed_approved();self.setup_notebook()
        with self.assertRaises(RuntimeError):self.execute_run(side_effect=RuntimeError('Failure: synthetic-colab-secret'))
        request=json.loads(self.files['data']['training/request.json'])
        status=self.files['data'][f"training/{request['id']}/status.json"]
        self.assertNotIn(b'synthetic-colab-secret',status)
        self.assertIn(b'[redacted]',status)
