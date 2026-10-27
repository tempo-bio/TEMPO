"""Evaluated model releases preserve approval, dependencies and atomic recipes."""
import copy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

import tempo_model_adoption as adoption
import tempo_evaluation as evaluation
from tempo_preprocessing import metadata
from tests import test_model_evaluation as evaluation_fixture


class ModelAdoptionTests(unittest.TestCase):
    def setUp(self):
        fixture=evaluation_fixture.EvaluationTests('test_protocol_defines_stages_settings_without_nuclear_requirements')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture;self.hub=fixture.hub;self.repo=fixture.repo;self.root=fixture.root
        self.plan=fixture.save();self.base=evaluation.prefix(self.plan['id'])
        helper=io.BytesIO()
        with zipfile.ZipFile(helper,'w') as archive:
            archive.writestr('model/tempo_nuclei.json',json.dumps({'preprocessing':metadata('clahe4')}))
        self.helper=helper.getvalue();self.helper_sha=hashlib.sha256(self.helper).hexdigest()
        bundle=io.BytesIO()
        with zipfile.ZipFile(bundle,'w') as archive:
            archive.writestr('healthy.pt',b'healthy checkpoint')
            archive.writestr('dead.pt',b'dead checkpoint')
            archive.writestr('nuclei.zip',self.helper)
            archive.writestr('tempo_cellpose.json',json.dumps(dict(format='tempo-cellpose-specialists-v3',
                classes={'healthy':0,'dead':1},conflict_overlap=.5,preprocessing=metadata('clahe4'),
                channels={'healthy':['image','nuclei','zero']},helper_sha256=self.helper_sha)))
        self.blobs={'models/cells.pt':bundle.getvalue(),'models/nuclei.zip':self.helper}
        self.inventory={name:dict(sha256=hashlib.sha256(data).hexdigest(),size=len(data)) for name,data in self.blobs.items()}
        self.repo.files[self.base+'/artifacts.json']=self.inventory
        self.proposal=dict(format='tempo-evaluated-model-release-v1',models={
            'cellpose':dict(artifact='models/cells.pt',metrics={'metric':'instance_F1','score':.75}),
            'stardist':dict(artifact='models/nuclei.zip',metrics={'metric':'partial_nuclear_recall','score':0.})},
            training_receipt={'profile':'clahe4','nuclear_assistance_passed':True})
        report={'decision':dict(passed=True,model_release=self.proposal)}
        report['report_sha256']=hashlib.sha256(json.dumps(report,indent=2,allow_nan=False).encode()).hexdigest()
        self.repo.files[self.base+'/reports/measure.json']=report
        evaluation.approve_evaluation(self.hub,self.plan['id'])
        self.release=dict(revision='old-model-head',models={'yolo26n-scout.pt':{'sha256':'a'*64,'version':'existing-scout'}})
        self.hub.model_manifest=lambda:copy.deepcopy(self.release)
        self.hub.api=Mock()
        def download(hub,remote,revision,destination,expected,**options):
            name=remote.split('/workspace/')[1];data=self.blobs[name]
            self.assertEqual(hashlib.sha256(data).hexdigest(),expected)
            destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(data)
        self.download=patch('tempo_evaluation_runner.download_artifact',side_effect=download).start()

    def prepare(self):
        return adoption.prepare_adoption(self.hub,self.plan['id'],workspace=self.root/'prepared')

    def test_prepare_keeps_exact_pair_and_unrelated_scout_recipe(self):
        prepared=self.prepare()
        self.assertEqual(prepared['manifest']['models']['stardist_nuclei.zip']['sha256'],self.helper_sha)
        self.assertEqual(prepared['manifest']['models']['cellpose4_cardio.pt']['helper_sha256'],self.helper_sha)
        self.assertEqual(prepared['manifest']['models']['yolo26n-scout.pt'],self.release['models']['yolo26n-scout.pt'])
        recipe=prepared['manifest']['training_recipe']
        self.assertEqual((recipe['profile'],recipe['scout_profile']),('clahe4','P0'))
        self.assertTrue(recipe['nuclear_assistance_passed']);self.assertFalse(recipe['centroid_scout_passed'])
        self.hub.api.create_commit.assert_not_called()

    def test_missing_approval_and_modified_report_are_rejected(self):
        key='training/evaluation_adoptions/'+self.plan['id']+'.json'
        approval=self.repo.files.pop(key)
        with self.assertRaisesRegex(ValueError,'Record the passing'):self.prepare()
        self.repo.files[key]=approval
        self.repo.files[self.base+'/reports/measure.json']['decision']['passed']=False
        with self.assertRaisesRegex(ValueError,'verification'):self.prepare()
        self.download.assert_not_called()

    def test_missing_helper_and_traversal_are_rejected(self):
        with patch.object(adoption,'validate_proposal',return_value=dict(self.proposal,models={'cellpose':self.proposal['models']['cellpose']})):
            with self.assertRaisesRegex(ValueError,'exact evaluated nuclear helper'):self.prepare()
        invalid=copy.deepcopy(self.proposal);invalid['models']['cellpose']['artifact']='../outside.pt'
        with self.assertRaises(ValueError):adoption.validate_proposal(invalid,self.inventory)

    def test_artifact_and_prepared_manifest_cannot_override_source_checksum(self):
        prepared=self.prepare();path=Path(prepared['files']['cellpose']);path.write_bytes(b'changed')
        prepared['manifest']['models']['cellpose4_cardio.pt']['sha256']=hashlib.sha256(b'changed').hexdigest()
        with self.assertRaisesRegex(ValueError,'weights changed'):adoption.publish_adoption(self.hub,prepared)
        self.hub.api.preupload_lfs_files.assert_not_called()

    def test_concurrent_release_is_not_overwritten(self):
        prepared=self.prepare();self.release['revision']='someone-elses-release'
        with self.assertRaisesRegex(ValueError,'changed after preparation'):adoption.publish_adoption(self.hub,prepared)
        self.hub.api.preupload_lfs_files.assert_not_called()

    def test_active_training_prevents_publication_before_any_upload(self):
        prepared=self.prepare()
        self.repo.files[evaluation.INTENT]={'status':'running','evaluation_id':'another-evaluation'}
        with self.assertRaisesRegex(ValueError,'active evaluation'):
            adoption.publish_adoption(self.hub,prepared)
        self.hub.api.preupload_lfs_files.assert_not_called()

    def test_publication_is_atomic_and_retry_does_not_publish_twice(self):
        prepared=self.prepare()
        def commit(api,**options):
            self.assertEqual(options['parent_commit'],'old-model-head')
            operations={op.path_in_repo:op for op in options['operations']}
            self.assertIn('cellpose4_cardio.pt',operations);self.assertIn('stardist_nuclei.zip',operations)
            self.assertNotIn('yolo26n-scout.pt',operations)
            self.release=json.loads(operations['model_manifest.json'].path_or_fileobj)
            self.assertEqual(self.release['training_recipe']['profile'],'clahe4')
            self.release['revision']='published-head'
            return SimpleNamespace(oid='published-head')
        with patch('tempo_hub.commit_and_confirm',side_effect=commit) as published:
            receipt=adoption.publish_adoption(self.hub,prepared)
            self.assertEqual(receipt['model_revision'],'published-head')
            self.assertEqual(self.repo.files['training/architecture_validation.json']['profile'],'clahe4')
            adoption.publish_adoption(self.hub,prepared)
            published.assert_called_once()

    def test_older_evaluation_can_attach_selection_without_changing_report(self):
        report=self.repo.files[self.base+'/reports/measure.json'];report.pop('report_sha256')
        report['decision'].pop('model_release')
        report['report_sha256']=hashlib.sha256(json.dumps(report,indent=2,allow_nan=False).encode()).hexdigest()
        self.repo.files['training/evaluation_adoptions/'+self.plan['id']+'.json']['report_sha256']=report['report_sha256']
        original=copy.deepcopy(report)
        adoption.save_proposal(self.hub,self.plan['id'],self.proposal)
        self.assertEqual(self.repo.files[self.base+'/reports/measure.json'],original)
        self.assertEqual(self.prepare()['proposal'],self.proposal)

    def test_lost_dataset_mirror_does_not_republish_model_release(self):
        prepared=self.prepare()
        from tempo_cloud import training_transaction
        def commit(api,**options):
            manifest=next(op for op in options['operations'] if op.path_in_repo=='model_manifest.json')
            self.release=json.loads(manifest.path_or_fileobj);self.release['revision']='published-head'
            return SimpleNamespace(oid='published-head')
        with patch('tempo_hub.commit_and_confirm',side_effect=commit) as published:
            with patch('tempo_cloud.training_transaction',side_effect=ConnectionError('mirror interrupted')):
                with self.assertRaises(ConnectionError):adoption.publish_adoption(self.hub,prepared)
            self.assertEqual(self.release['training_recipe']['profile'],'clahe4')
            receipt=adoption.publish_adoption(self.hub,prepared)
            self.assertEqual(receipt['model_revision'],'published-head');published.assert_called_once()

    def test_regular_workers_retain_published_batch_sizes_without_changing_scout(self):
        import train_yolo
        from tempo_training import APPROVAL_POLICY,SPLIT_POLICY,DATASET_FORMAT
        data=self.root/'compiled';data.mkdir()
        info=dict(format=DATASET_FORMAT,approval_policy=APPROVAL_POLICY,split_policy=SPLIT_POLICY,
                  origin='approved-master',test_fingerprint='b'*64,
                  architecture_validation={'batch_sizes':{'cellpose':1,'stardist':4}})
        (data/'dataset.json').write_text(json.dumps(info));batches={}
        def worker(command,**options):
            role=command[command.index('--worker')+1];batches[role]=int(command[command.index('--batch')+1])
            output=Path(command[command.index('--output')+1]);output.mkdir(parents=True)
            (output/'result.json').write_text(json.dumps({'metrics':{'score':.5}}))
            return SimpleNamespace(returncode=0)
        with patch.object(train_yolo,'BASE_DIR',str(self.root)),patch.object(train_yolo,'ensure_model_dependencies'), \
             patch.object(train_yolo.subprocess,'run',side_effect=worker):
            self.assertTrue(train_yolo.train_parallel_pipeline(override_workers=0,compiled_dataset=data))
        self.assertEqual(batches,{'cellpose':1,'stardist':4,'nano_scout':4})

    def test_regular_training_uses_recipe_pinned_with_models(self):
        from tempo_cloud_runner import prepare_dataset
        recipe=dict(profile='clahe4',nuclear_assistance_passed=True)
        release=dict(models={},training_recipe=recipe)
        self.hub.download_model_release=Mock()
        with patch('tempo_cloud_runner.approved_snapshot',return_value={}),patch('tempo_cloud_runner.request_partitions',return_value=({},{})), \
             patch('tempo_training.build_dataset',return_value='compiled'):
            result=prepare_dataset(self.root,{'dataset_revision':'snapshot'},self.hub,release=release)
        self.assertEqual(result,'compiled')
        self.assertEqual(json.loads((self.root/'datasets/architecture_validation.json').read_text()),recipe)

    def test_protected_settings_survive_progress_commits_with_unchanged_models(self):
        from tempo_cloud_runner import prepare_dataset
        recipe=dict(profile='P0',base_model_revision='prior-evaluation-head',base_models_sha256=evaluation.checksum({}))
        self.repo.files['training/architecture_validation.json']=recipe
        release=dict(revision='current',models={},training_recipe={'profile':'clahe4'})
        self.hub.download_model_release=Mock()
        with patch('tempo_cloud_runner.approved_snapshot',return_value={}),patch('tempo_cloud_runner.request_partitions',return_value=({},{})), \
             patch('tempo_training.build_dataset',return_value='compiled'):
            prepare_dataset(self.root,{'dataset_revision':'snapshot'},self.hub,release=release)
        self.assertEqual(json.loads((self.root/'datasets/architecture_validation.json').read_text()),recipe)


if __name__=='__main__':unittest.main()
