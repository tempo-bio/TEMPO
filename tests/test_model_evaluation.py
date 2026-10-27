"""Infrastructure tests use independent, developer-defined evaluation protocols."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import types
import unittest
from unittest.mock import patch
import numpy as np
import cv2
import tempo_evaluation as service
from tempo_evaluation_protocol import read_package,unpack_package,load_protocol,validate_report
from tests.test_collaboration_lifecycle import MemoryRepo
ROOT=Path(__file__).resolve().parents[1]


def records(count=14):
    return {f'field-{index}.png':dict(status='approved',width=64,height=64,coordinate_space='normalized',
        image_id='sha256:'+hashlib.sha256(str(index).encode()).hexdigest(),boxes=[
            dict(class_id=0,polygon=[[.1,.1],[.4,.1],[.4,.8],[.1,.8]]),
            dict(class_id=1,polygon=[[.6,.2],[.9,.2],[.9,.7],[.6,.7]])]) for index in range(count)}


def protocol(folder,stages=('calibrate','measure'),images=True,parameters=None):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    descriptor=dict(format='tempo-evaluation-protocol-v1',id='developer-comparison',version='2',name='Developer comparison',
        entrypoint='protocol.py',files=['protocol.py'],stages=[dict(id=name,label=name.capitalize()) for name in stages],
        parameters=parameters or [dict(name='repeats',label='Repeats',type='integer',minimum=1,maximum=10,default=3)])
    (folder/'protocol.json').write_text(json.dumps(descriptor))
    (folder/'protocol.py').write_text('''from tempo_training_groups import plan_splits
def prepare(context):
    assignments=plan_splits(context.records,previous=context.previous_splits) if context.records else {}
    return dict(scope='Developer-defined units',images={name:dict(split=role,unit=name) for name,role in assignments.items()} if IMAGES else {})
def run(context):
    context.check_cancelled()
    folder=context.workspace/'results';folder.mkdir(exist_ok=True)
    (folder/(context.stage['id']+'.onnx')).write_bytes(b'custom model artifact')
    context.progress('Custom operation',phase='protocol-metadata',stage='custom-detail')
    if context.stage['id']==context.plan['stages'][-1]['id']:
        assert all(context.reports[stage['id']] for stage in context.plan['stages'][:-1])
        for stage in context.plan['stages'][:-1]:
            path=folder/(stage['id']+'.onnx');context.materialize(path)
            assert path.read_bytes()==b'custom model artifact'
        context.freeze_selection({'weights':'frozen-choice'})
    context.checkpoint()
    return dict(summary='Custom latency comparison',tables=[dict(title='Latency',columns=['Condition','Milliseconds'],rows=[['reference',12.],['candidate',9.]])],decision=dict(passed=True))
'''.replace('IMAGES','True' if images else 'False'))
    return folder/'protocol.json'


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.data=records();self.repo=MemoryRepo({'universal_annotations.json':self.data})
        self.hub=types.SimpleNamespace(token='fixture-token',api=self.repo,ensure_credentials=lambda:None,is_maintainer=lambda **kwargs:True,
            _require_actor=lambda:'fixture-actor',get_dataset_repo=lambda:'fixture/data',get_models_repo=lambda:'fixture/models',
            _head=lambda **kwargs:str(self.repo.sha),model_manifest=lambda:dict(revision='model-sha',models={}))
        self.hub._read=lambda name,revision,default=None,**kwargs:copy.deepcopy(self.repo.files.get(name,default))
        self.addCleanup(patch.stopall)
        def transaction(hub,mutate,message,**options):
            updates,value=mutate({},str(self.repo.sha))
            for name,item in updates.items():
                if isinstance(item,str):item=Path(item).read_bytes()
                self.repo.files[name]=json.loads(item) if name.endswith('.json') and '/workspace/' not in name else item
                if name.endswith('/state.json'):self.assertNotEqual(self.repo.files[name].get('phase'),'protocol-metadata')
            self.repo.sha+=1;return value
        patch('tempo_cloud.training_transaction',side_effect=transaction).start();self.path=protocol(self.root/'protocol')

    def plan(self,**kwargs):return service.prepare_evaluation(self.hub,ROOT,protocol_path=self.path,**kwargs)

    def save(self):
        prepared=self.plan();service.save_evaluation(self.hub,*prepared);return prepared[0]

    def test_protocol_defines_stages_settings_without_nuclear_requirements(self):
        plan,bundle,package=self.plan(parameters={'repeats':7})
        self.assertEqual(plan['parameters'],{'repeats':7});self.assertEqual(plan['protocol']['stages'][0]['id'],'calibrate')
        self.assertNotIn('seeds',plan);self.assertNotIn('nuclear_positive_fields',plan)
        self.assertEqual(plan['bundle_sha256'],hashlib.sha256(bundle).hexdigest());self.assertEqual(plan['protocol_sha256'],hashlib.sha256(package).hexdigest())

    def test_data_free_protocol_with_different_settings_and_single_stage(self):
        self.repo.files['universal_annotations.json']={}
        self.path=protocol(self.root/'benchmark',stages=('latency',),images=False,parameters=[
            dict(name='threshold',label='Threshold',type='number',default=.3,minimum=0.,maximum=1.),
            dict(name='enabled',label='Enabled',type='boolean',default=True),
            dict(name='mode',label='Mode',type='choice',choices=['fast','full'],default='full'),dict(name='note',label='Note',type='text',default='')])
        plan,_,_=self.plan();self.assertEqual(plan['manifest']['images'],{});self.assertEqual(plan['parameters']['mode'],'full')
        with self.assertRaisesRegex(ValueError,'outside'):self.plan(parameters={'threshold':2.})

    def test_package_integrity_and_path_boundaries(self):
        descriptor,payload=read_package(self.path);destination=self.root/'restored'
        unpack_package(payload,destination,hashlib.sha256(payload).hexdigest())
        with load_protocol(destination,descriptor) as module:self.assertTrue(callable(module.run))
        with self.assertRaisesRegex(ValueError,'verification'):unpack_package(payload+b'x',destination,hashlib.sha256(payload).hexdigest())
        descriptor['files'].append('../outside.py');self.path.write_text(json.dumps(descriptor))
        with self.assertRaisesRegex(ValueError,'path'):read_package(self.path)

    def test_settings_select_active_stages_and_reject_inactive_queues(self):
        descriptor=json.loads(self.path.read_text());descriptor['parameters']=[dict(name='test',label='Test',type='choice',choices=['speed','quality'],default='speed')]
        descriptor['stages']=[dict(id='speed',label='Speed measurement',when={'test':'speed'}),dict(id='quality',label='Quality measurement',when={'test':'quality'})]
        self.path.write_text(json.dumps(descriptor))
        plan=self.save();self.assertEqual([stage['id'] for stage in plan['stages']],['speed'])
        with self.assertRaisesRegex(ValueError,'defined'):service.queue_evaluation(self.hub,plan['id'],'quality')
        self.assertEqual(service.queue_evaluation(self.hub,plan['id'],'speed')['phase'],'speed')

    def test_packages_are_deterministic_and_sibling_imports_are_isolated(self):
        paths=[self.path,protocol(self.root/'different')]
        descriptors=[]
        for index,path in enumerate(paths):
            descriptor=json.loads(path.read_text());descriptor['files'].append('constants.py');path.write_text(json.dumps(descriptor))
            (path.parent/'constants.py').write_text('LABEL='+repr(str(index)))
            entry=path.parent/'protocol.py';entry.write_text('from .constants import LABEL\n'+entry.read_text())
            descriptor,payload=read_package(path);self.assertEqual(payload,read_package(path)[1]);descriptors.append(descriptor)
        with load_protocol(paths[0].parent,descriptors[0]) as first,load_protocol(paths[1].parent,descriptors[1]) as second:
            self.assertEqual(first.LABEL,'0');self.assertEqual(second.LABEL,'1')

    def test_optional_protocol_dependencies_are_bundled_and_verified(self):
        from tempo_evaluation_notebook import protocol_requirements
        import io
        descriptor=json.loads(self.path.read_text());descriptor['files'].append('requirements.txt');descriptor['requirements']='requirements.txt'
        self.path.write_text(json.dumps(descriptor))
        for expected in (b'example-package==1.2.3\n',b'example-package==1.2.3\r\n'):
            with self.subTest(requirements=expected):
                (self.path.parent/'requirements.txt').write_bytes(expected)
                _,payload=read_package(self.path);job=dict(dataset_repo='fixture/data',models_repo='fixture/models',evaluation_id='a'*32,protocol_sha256=hashlib.sha256(payload).hexdigest())
                with patch('tempo_evaluation_notebook.urlopen',return_value=io.BytesIO(payload)):
                    self.assertEqual(protocol_requirements(job,'fixture-token'),expected)

    def test_protocol_cannot_reassign_prior_training_exposure_to_test(self):
        self.repo.files['training/split_assignments.json']={'field-0.png':'train'}
        path=self.path.parent/'protocol.py';path.write_text(path.read_text().replace('return dict(scope=',"assignments['field-0.png']='test'\n    return dict(scope="))
        with self.assertRaisesRegex(ValueError,'Prior training'):self.plan()

    def test_aliases_keep_strongest_exposure_and_invalid_roles_are_rejected(self):
        self.repo.files['training/split_assignments.json']={'old':'train','other':'test'}
        self.repo.files['lock_manifest.json']={'aliases':{'old':'field-0.png','other':'field-0.png'}}
        self.assertEqual(service.previous_assignments(self.hub,'0')['field-0.png'],'train')
        self.repo.files['training/split_assignments.json']={'field-0.png':'invalid'}
        with self.assertRaisesRegex(ValueError,'Invalid saved'):self.plan()

    def legacy_pool_assignments(self):
        from tempo_training import APPROVAL_POLICY
        from tempo_training_groups import SPLIT_POLICY
        names=list(self.data);old=set(names[:6])
        planned={name:('val' if i%3==1 else 'test' if i%3==2 else 'train') for i,name in enumerate(names)}
        snapshot=copy.deepcopy(self.data)
        for name in set(names)-old:snapshot[name].update(status='available',boxes=[])
        models={'model.pt':{'sha256':'c'*64}}
        release=dict(revision='published',approval_policy=APPROVAL_POLICY,models=models)
        self.hub.model_manifest=lambda:copy.deepcopy(release)
        request=dict(id='old-run',dataset_revision='historical',split_policy=SPLIT_POLICY,
            image_count=len(old),images=[dict(name=name) for name in sorted(old)],
            split_assignments={name:planned[name] for name in old},previous_splits=planned,
            baseline_models=copy.deepcopy(models))
        self.repo.files.update({'training/request.json':request,'training/split_assignments.json':planned})
        original=self.hub._read
        def read(name,revision,default=None,**kwargs):
            if name=='universal_annotations.json' and revision=='historical':return copy.deepcopy(snapshot)
            if name=='training_splits.json' and kwargs.get('repo_type')=='model':return copy.deepcopy(planned)
            return original(name,revision,default,**kwargs)
        self.hub._read=read
        return planned,old,snapshot,release

    def test_empty_pool_assignments_are_not_prior_exposure_after_approval(self):
        planned,old,_,_=self.legacy_pool_assignments()
        previous,resolution=service.resolve_previous_assignments(self.hub,'0')
        self.assertEqual(previous,{name:planned[name] for name in old})
        self.assertEqual(set(resolution['unannotated_assignments']),set(self.data)-old)
        self.assertEqual(resolution['training_snapshot'],'historical')
        self.assertEqual(len(self.data)-len(previous),8)

    def test_reconciled_assignments_survive_save_and_old_model_split_reimports(self):
        self.legacy_pool_assignments()
        plan=self.save()
        expected={name:row['split'] for name,row in plan['manifest']['images'].items()}
        self.assertTrue(plan['assignment_resolution']['unannotated_assignments'])
        self.assertEqual(service.previous_assignments(self.hub,str(self.repo.sha)),expected)
        self.assertEqual(service.previous_assignments(self.hub,str(self.repo.sha)),expected)
        # All selected fields stay reserved even when no stage has been queued.
        for name,role in expected.items():
            self.assertEqual(service.previous_assignments(self.hub,str(self.repo.sha))[name],role)

    def test_regular_training_uses_and_persists_the_same_assignment_provenance(self):
        from tempo_cloud import prepare_request,queue_request
        planned,old,_,_=self.legacy_pool_assignments()
        self.repo.files['training/old-run/status.json']={'phase':'completed'}
        request,bundle=prepare_request(self.hub,ROOT)
        self.assertEqual(request['previous_splits'],{name:planned[name] for name in old})
        queue_request(self.hub,request,bundle)
        self.assertEqual(service.previous_assignments(self.hub,str(self.repo.sha)),request['split_assignments'])
        self.assertTrue(self.repo.files[service.ASSIGNMENT_POLICY]['resolution'])

    def test_changed_assignment_provenance_cannot_silently_relabel_fields(self):
        self.legacy_pool_assignments();self.save()
        saved=self.repo.files['training/split_assignments.json']
        name=next(iter(saved));saved[name]='val' if saved[name]=='train' else 'train'
        with self.assertRaisesRegex(ValueError,'recorded provenance'):
            service.previous_assignments(self.hub,str(self.repo.sha))

    def test_unproven_or_annotated_history_is_never_erased(self):
        planned,old,snapshot,release=self.legacy_pool_assignments()
        new=next(name for name in planned if name not in old)
        snapshot[new].update(status='draft',boxes=copy.deepcopy(self.data[new]['boxes']))
        self.assertEqual(service.previous_assignments(self.hub,'0')[new],planned[new])
        release['models']['model.pt']['sha256']='d'*64
        self.assertEqual(service.previous_assignments(self.hub,'0'),planned)

    def test_unreadable_or_inconsistent_pinned_history_stops_reconciliation(self):
        _,_,snapshot,_=self.legacy_pool_assignments()
        new=next(name for name,row in snapshot.items() if row['status']=='available')
        snapshot[new]['status']='approved'
        with self.assertRaisesRegex(ValueError,'snapshot does not match'):
            service.previous_assignments(self.hub,'0')
        original=self.hub._read
        def failed(name,revision,default=None,**kwargs):
            if name=='universal_annotations.json' and revision=='historical':raise OSError('Historical input unavailable')
            return original(name,revision,default,**kwargs)
        self.hub._read=failed
        with self.assertRaisesRegex(OSError,'Historical input unavailable'):
            service.previous_assignments(self.hub,'0')

    def test_queue_order_exclusivity_and_cancellation(self):
        plan=self.save()
        with self.assertRaisesRegex(ValueError,'preceding'):service.queue_evaluation(self.hub,plan['id'],'measure')
        with self.assertRaisesRegex(ValueError,'defined'):service.queue_evaluation(self.hub,plan['id'],'pilot')
        job=service.queue_evaluation(self.hub,plan['id'],'calibrate');self.assertEqual(job['protocol_sha256'],plan['protocol_sha256'])
        with self.assertRaisesRegex(ValueError,'already'):service.queue_evaluation(self.hub,plan['id'],'calibrate')
        service.cancel_evaluation(self.hub,plan['id']);self.assertEqual(self.repo.files[service.INTENT]['status'],'cancelled')
        service.clear_evaluation_intent(self.hub);self.assertEqual(self.repo.files[service.INTENT],{})

    def test_recovery_fences_old_attempt(self):
        plan=self.save();job=service.queue_evaluation(self.hub,plan['id'],'calibrate');job['status']='running'
        self.repo.files[service.INTENT]=job;self.repo.files[service.prefix(plan['id'])+'/state.json']={'phase':'running','updated_at':0}
        service.recover_evaluation(self.hub,plan['id']);self.assertNotEqual(job['id'],service.queue_evaluation(self.hub,plan['id'],'calibrate')['id'])

    def test_new_evaluation_does_not_replace_an_active_job(self):
        first=self.save();second=self.save()
        original=service.queue_evaluation(self.hub,first['id'],'calibrate')
        with self.assertRaisesRegex(ValueError,'already queued or running'):
            service.queue_evaluation(self.hub,second['id'],'calibrate')
        self.assertEqual(self.repo.files[service.INTENT],original)
        service.cancel_evaluation(self.hub,first['id'])
        replacement=service.queue_evaluation(self.hub,second['id'],'calibrate')
        self.assertEqual(replacement['evaluation_id'],second['id'])
        self.assertIn(service.prefix(first['id'])+'/plan.json',self.repo.files)

    def test_malformed_reports_are_rejected(self):
        for report in ({'tables':[{'columns':['A'],'rows':[[1,2]]}]},{'previews':[{'name':'x','views':{'view':'../x.png'}}]},
                       {'tables':[{'columns':['Value'],'rows':[[float('nan')]]}]},{'decision':{'passed':1}}):
            with self.assertRaises(ValueError):validate_report(report)

    def test_custom_stages_cold_restore_and_generic_decision_do_not_publish(self):
        from tempo_evaluation_runner import run_prepared_evaluation
        for index,(name,row) in enumerate(self.repo.files['universal_annotations.json'].items()):
            data=cv2.imencode('.png',np.random.default_rng(index).integers(0,255,(64,64),dtype=np.uint8))[1].tobytes()
            row['image_id']='sha256:'+hashlib.sha256(data).hexdigest();self.repo.files['images/'+name]=data
        def download(hub,remote,revision,destination,expected,**kwargs):
            data=self.repo.files[remote];self.assertEqual(hashlib.sha256(data).hexdigest(),expected)
            destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(data)
        with patch('tempo_cloud_runner.require_colab'),patch('tempo_evaluation_runner.subprocess.check_output',return_value='NVIDIA A100'), \
             patch('tempo_evaluation_runner.download_artifact',side_effect=download), \
             patch('tempo_evaluation_runner.LARGE_ARTIFACT_BYTES',1),patch('tempo_evaluation_runner.LOCAL_WEIGHT_CACHE_BYTES',1):
            plan=self.save();workspace=self.root/'workspace'
            for stage in ('calibrate','measure'):
                job=service.queue_evaluation(self.hub,plan['id'],stage)
                result=run_prepared_evaluation(ROOT,dict(evaluation_request=job,bundle_sha256=plan['bundle_sha256']),self.hub,workspace=workspace)
                self.assertEqual(result['stage'],stage)
                self.assertEqual(result['report']['execution']['gpu'],'NVIDIA A100')
                self.assertGreater(result['report']['execution']['elapsed_seconds'],0.)
                self.assertEqual(result['report']['execution']['protocol_sha256'],plan['protocol_sha256'])
                if stage=='calibrate':shutil.rmtree(workspace)
            self.assertIsNone(service.next_stage(service.load_evaluation(self.hub,plan['id'])))
            self.assertEqual(service.approve_evaluation(self.hub,plan['id'])['protocol'],'developer-comparison')
            self.assertNotIn('model_manifest.json',self.repo.files);self.assertNotIn('training/architecture_validation.json',self.repo.files)
            self.repo.files[service.prefix(plan['id'])+'/reports/measure.json']['decision']['passed']=False
            with self.assertRaisesRegex(ValueError,'verification'):service.approve_evaluation(self.hub,plan['id'])

    def test_updated_controller_resumes_frozen_engine_and_archives_its_sources(self):
        from tempo_cloud import archive_sources, training_transaction
        from tempo_evaluation_runner import run_prepared_evaluation
        import io
        import zipfile
        self.repo.files['universal_annotations.json'] = {}
        self.path = protocol(self.root/'data-free', images=False)
        frozen_root = self.root/'frozen-engine'; frozen_root.mkdir()
        with zipfile.ZipFile(io.BytesIO(archive_sources(ROOT))) as archive:
            archive.extractall(frozen_root)
        old_runner = frozen_root/'tempo_evaluation_runner.py'
        old_runner.write_text(old_runner.read_text()+'\n# Frozen engine from before the persistence repair.\n')
        prepared = service.prepare_evaluation(self.hub, frozen_root, protocol_path=self.path)
        service.save_evaluation(self.hub, *prepared)
        plan, frozen_bundle, _ = prepared
        runtime_sources = b'exact updated persistence controller sources'
        runtime_sha = hashlib.sha256(runtime_sources).hexdigest()
        calls = []
        def updated_transaction(*args, **kwargs):
            calls.append(kwargs)
            return training_transaction(*args, **kwargs)
        def download(hub, remote, revision, destination, expected, **kwargs):
            data = self.repo.files[remote]; self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
            destination = Path(destination); destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        with patch('tempo_cloud_runner.require_colab'), \
             patch('tempo_evaluation_runner.subprocess.check_output', return_value='NVIDIA A100'), \
             patch('tempo_evaluation_runner.download_artifact', side_effect=download):
            workspace = self.root/'restored'
            for stage in ('calibrate', 'measure'):
                job = service.queue_evaluation(self.hub, plan['id'], stage)
                result = run_prepared_evaluation(frozen_root,
                    dict(evaluation_request=job, bundle_sha256=plan['bundle_sha256']), self.hub,
                    workspace=workspace, transaction=updated_transaction, runtime_sources=runtime_sources)
                self.assertEqual(result['report']['execution']['runtime_sha256'], runtime_sha)
                self.assertEqual(result['report']['execution']['bundle_sha256'], plan['bundle_sha256'])
                self.assertEqual(archive_sources(frozen_root), frozen_bundle)
                shutil.rmtree(workspace)
        base = service.prefix(plan['id'])
        self.assertEqual(self.repo.files[base+'/runtimes/'+runtime_sha+'.zip'], runtime_sources)
        self.assertEqual(self.repo.files[base+'/plan.json'], plan)
        self.assertEqual(self.repo.files[base+'/state.json']['runtime_sha256'], runtime_sha)
        self.assertTrue(any(call.get('preupload') for call in calls))
        self.assertNotIn('model_manifest.json', self.repo.files)
        with self.assertRaisesRegex(ValueError, 'exact sources'):
            run_prepared_evaluation(frozen_root, {}, self.hub, transaction=updated_transaction)

    def test_final_status_failure_does_not_mask_the_protocol_error(self):
        import tempo_cloud
        from tempo_evaluation_runner import run_prepared_evaluation
        self.repo.files['universal_annotations.json']={}
        self.path=protocol(self.root/'failing',images=False)
        entry=self.path.parent/'protocol.py'
        entry.write_text(entry.read_text().replace('    context.check_cancelled()','    raise ValueError("original protocol failure")',1))
        plan=self.save();job=service.queue_evaluation(self.hub,plan['id'],'calibrate')
        original=tempo_cloud.training_transaction.side_effect
        def transaction(hub,mutate,message,**options):
            if message=='Finish Model Evaluation stage':raise ConnectionError('Final status unavailable')
            return original(hub,mutate,message,**options)
        tempo_cloud.training_transaction.side_effect=transaction
        def download(hub,remote,revision,destination,expected,**kwargs):
            destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_bytes(self.repo.files[remote])
        with patch('tempo_cloud_runner.require_colab'),patch('tempo_evaluation_runner.subprocess.check_output',return_value='NVIDIA A100'), \
             patch('tempo_evaluation_runner.download_artifact',side_effect=download):
            with self.assertRaisesRegex(ValueError,'original protocol failure'):
                run_prepared_evaluation(ROOT,dict(evaluation_request=job,bundle_sha256=plan['bundle_sha256']),
                    self.hub,workspace=self.root/'workspace')
