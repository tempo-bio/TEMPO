"""Repository permissions, model destinations and redirect credential boundaries."""
import copy
from email.message import Message
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlunsplit
from urllib.request import Request
from urllib.request import HTTPSHandler
from urllib.response import addinfourl

from huggingface_hub.errors import EntryNotFoundError
import tempo_evaluation as evaluation
import tempo_evaluation_notebook as bootstrap
from tempo_evaluation_runner import download_artifact
from tempo_hub import TempoHub
from tempo_model_adoption import approved_source
from tempo_model_catalog import installed_model_dir, local_model_path


class RepositoryBoundaryTests(unittest.TestCase):
    """Real transport/Hub code against two independent, revisioned in-memory repos."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ids = {'dataset': 'fixture-data/annotations', 'model': 'fixture-admin/models'}
        self.heads = {'dataset': 'dataset-0', 'model': 'model-0'}
        self.history = {kind: {head: {}} for kind, head in self.heads.items()}
        self.reads, self.commits = [], []
        self.hub = TempoHub(token='fixture-credential')
        self.hub.api = Mock()
        self.hub.api.repo_info.side_effect = self.info
        self.hub.api.create_commit.side_effect = self.commit
        self.hub.get_dataset_repo = lambda: self.ids['dataset']
        self.hub.get_models_repo = lambda: self.ids['model']
        self.hub.is_maintainer = lambda **kwargs: True
        self.hub._require_actor = lambda: 'fixture-admin'
        self.hub.model_manifest = lambda: dict(revision=self.heads['model'], models={})
        for target in ('tempo_hub.hf_hub_download', 'huggingface_hub.hf_hub_download'):
            mocked = patch(target, side_effect=self.download); mocked.start(); self.addCleanup(mocked.stop)
        descriptor = dict(format='tempo-evaluation-protocol-v1', id='fixture', version='1', name='Fixture',
                          entrypoint='protocol.py', files=['protocol.py'], parameters=[],
                          stages=[dict(id='measure', label='Measure')])
        self.bundle, self.protocol = b'fixture source archive', b'fixture protocol archive'
        self.plan = dict(format=evaluation.FORMAT, id='a'*32, name='Fixture', created_by='fixture-admin', created_at=0,
                         dataset_repo=self.ids['dataset'], models_repo=self.ids['model'], dataset_revision=self.heads['dataset'],
                         protocol=descriptor, stages=descriptor['stages'], parameters={}, gpu='Any CUDA GPU',
                         manifest={'images': {}}, previous_splits={}, partitions={}, scope='Fixture', baseline_models={},
                         bundle_sha256=hashlib.sha256(self.bundle).hexdigest(), protocol_sha256=hashlib.sha256(self.protocol).hexdigest())
        self.plan['plan_sha256'] = evaluation.checksum(self.plan)
        self.base = evaluation.prefix(self.plan['id'])

    def files(self, kind):
        return self.history[kind][self.heads[kind]]

    def info(self, *, repo_id, repo_type, **kwargs):
        self.assertEqual(repo_id, self.ids[repo_type])
        return SimpleNamespace(sha=self.heads[repo_type], private=True)

    def commit(self, *, repo_id, repo_type, parent_commit, operations, **kwargs):
        self.assertEqual(repo_id, self.ids[repo_type])
        self.assertEqual(parent_commit, self.heads[repo_type])
        files = copy.deepcopy(self.files(repo_type))
        names = []
        for operation in operations:
            value = operation.path_or_fileobj
            files[operation.path_in_repo] = value if isinstance(value, bytes) else Path(value).read_bytes()
            names.append(operation.path_in_repo)
        revision = repo_type+'-'+str(len(self.history[repo_type]))
        self.history[repo_type][revision] = files; self.heads[repo_type] = revision
        self.commits.append((repo_type, names))
        return SimpleNamespace(oid=revision)

    def download(self, *, repo_id, repo_type, filename, revision, **kwargs):
        self.assertEqual(repo_id, self.ids[repo_type])
        self.assertIn(revision, self.history[repo_type], 'A revision from the other repository was used.')
        self.reads.append((repo_type, filename))
        files = self.history[repo_type][revision]
        if filename not in files:
            raise EntryNotFoundError('Fixture file is absent')
        path = self.root/hashlib.sha256((repo_type+revision+filename).encode()).hexdigest()
        path.write_bytes(files[filename]); return str(path)

    def save(self):
        evaluation.save_evaluation(self.hub, self.plan, self.bundle, self.protocol)

    def test_creation_queue_and_artifacts_use_protected_repository(self):
        self.save()
        job = evaluation.queue_evaluation(self.hub, self.plan['id'], 'measure')
        for name in ('plan.json', 'bundle.zip', 'protocol.zip', 'state.json'):
            self.assertIn(self.base+'/'+name, self.files('model'))
            self.assertNotIn(self.base+'/'+name, self.files('dataset'))
        self.assertIn(evaluation.INTENT, self.files('model'))
        self.assertNotIn(evaluation.INTENT, self.files('dataset'))
        self.assertIn('training/split_assignments.json', self.files('dataset'))
        destination = self.root/'restored.zip'
        download_artifact(self.hub, self.base+'/protocol.zip', self.heads['model'], destination, job['protocol_sha256'])
        self.assertEqual(destination.read_bytes(), self.protocol)
        self.assertEqual(self.reads[-1], ('model', self.base+'/protocol.zip'))

    def test_dataset_cannot_replace_plan_bundle_or_queue(self):
        self.save()
        self.files('dataset')[self.base+'/plan.json'] = b'{"changed": true}'
        self.files('dataset')[self.base+'/bundle.zip'] = b'different archive'
        self.files('dataset')[evaluation.INTENT] = b'{"status": "running"}'
        job = evaluation.queue_evaluation(self.hub, self.plan['id'], 'measure')
        self.assertEqual(job['bundle_sha256'], self.plan['bundle_sha256'])
        self.assertEqual(evaluation.load_evaluation(self.hub, self.plan['id'])['plan'], self.plan)

    def test_dataset_only_evaluation_is_not_loaded_or_queued(self):
        self.files('dataset')[evaluation.INDEX] = json.dumps([{'id': self.plan['id']}]).encode()
        self.files('dataset')[self.base+'/plan.json'] = json.dumps(self.plan).encode()
        self.assertEqual(evaluation.list_evaluations(self.hub), [])
        with self.assertRaises(EntryNotFoundError):
            evaluation.queue_evaluation(self.hub, self.plan['id'], 'measure')
        self.assertEqual(self.commits, [])

    def test_dataset_approval_and_report_cannot_authorize_publication(self):
        self.save()
        report = {'decision': {'passed': True}}
        digest = hashlib.sha256(json.dumps(report, indent=2).encode()).hexdigest()
        report['report_sha256'] = digest
        self.files('model')[self.base+'/reports/measure.json'] = json.dumps(report).encode()
        key = 'training/evaluation_adoptions/'+self.plan['id']+'.json'
        self.files('dataset')[key] = json.dumps(dict(evaluation_id=self.plan['id'], report_sha256=digest)).encode()
        with self.assertRaisesRegex(ValueError, 'Record the passing'):
            approved_source(self.hub, self.plan['id'])
        evaluation.approve_evaluation(self.hub, self.plan['id'])
        self.assertIn(key, self.files('model'))
        self.files('dataset')[self.base+'/reports/measure.json'] = b'{"decision": {"passed": false}}'
        self.assertEqual(approved_source(self.hub, self.plan['id'])['report'], report)

    def test_protected_training_settings_survive_head_changes_and_ignore_dataset_override(self):
        from tempo_cloud_runner import prepare_dataset
        protected = dict(profile='P0', base_models_sha256=evaluation.checksum({}))
        self.files('model')['training/architecture_validation.json'] = json.dumps(protected).encode()
        self.files('dataset')['training/architecture_validation.json'] = b'{"profile": "P2"}'
        self.hub.download_model_release = Mock()
        for models, expected in (({}, protected), ({'yolo26n-scout.pt': {'sha256': 'f'*64}}, {'profile': 'P1'})):
            release = dict(revision=self.heads['model'], models=models, training_recipe={'profile': 'P1'})
            with self.subTest(models=models), patch('tempo_cloud_runner.approved_snapshot', return_value={}), \
                 patch('tempo_cloud_runner.request_partitions', return_value=({}, {})), \
                 patch('tempo_training.build_dataset', return_value='fixture dataset'):
                prepare_dataset(self.root, {'dataset_revision': self.heads['dataset']}, self.hub, release=release)
            self.assertEqual(json.loads((self.root/'datasets/architecture_validation.json').read_text()), expected)

    def test_model_write_denial_never_falls_back_to_dataset(self):
        self.plan['manifest']['images'] = {'field.png': {'split': 'train'}}
        self.plan.pop('plan_sha256'); self.plan['plan_sha256'] = evaluation.checksum(self.plan)
        commit = self.hub.api.create_commit.side_effect
        def denied(**kwargs):
            if kwargs['repo_type'] == 'model':
                raise PermissionError('Fixture protected repository denies writes')
            return commit(**kwargs)
        self.hub.api.create_commit.side_effect = denied
        with self.assertRaises(PermissionError):
            self.save()
        self.assertNotIn(self.base+'/bundle.zip', self.files('dataset'))
        self.assertNotIn(self.base+'/plan.json', self.files('dataset'))
        self.assertEqual(self.files('model'), {})
        self.assertEqual(json.loads(self.files('dataset')['training/split_assignments.json']), {'field.png': 'train'})

    def test_preupload_and_lost_acknowledgement_stay_in_protected_repository(self):
        from tempo_cloud import training_transaction
        path = self.root/'checkpoint.pt'; path.write_bytes(b'fixture checkpoint')
        key = self.base+'/workspace/checkpoint.pt'
        commit = self.hub.api.create_commit.side_effect
        def lost(**kwargs):
            commit(**kwargs)
            raise ConnectionError('Fixture acknowledgement lost')
        self.hub.api.create_commit.side_effect = lost
        training_transaction(self.hub, lambda _, revision: ({key: path}, True), 'Fixture checkpoint',
                             repo_type='model', preupload=True)
        self.assertEqual(self.hub.api.preupload_lfs_files.call_args.kwargs['repo_type'], 'model')
        self.assertEqual(self.hub.api.preupload_lfs_files.call_args.kwargs['repo_id'], self.ids['model'])
        self.assertEqual(self.files('model')[key], path.read_bytes())
        self.assertNotIn(key, self.files('dataset'))


class DownloadBoundaryTests(unittest.TestCase):
    def test_real_opener_redirect_chain_strips_credentials_before_storage_request(self):
        seen = []
        class FixtureHTTPS(HTTPSHandler):
            def https_open(self, request):
                seen.append((request.full_url, request.get_header('Authorization')))
                targets = ['https://huggingface.co/api/resolve-cache/fixture', 'https://storage.example.invalid/fixture']
                headers = Message()
                if len(seen) < 3:
                    headers['Location'] = targets[len(seen)-1]
                response = addinfourl(io.BytesIO(b'fixture bytes'), headers, request.full_url, 302 if headers else 200)
                response.msg = 'Found' if headers else 'OK'
                return response
        with patch.object(bootstrap, 'HTTPSHandler', return_value=FixtureHTTPS()):
            request = Request('https://huggingface.co/fixture/models/resolve/main/file',
                              headers={'Authorization': 'Bearer fixture-credential'})
            with bootstrap.urlopen(request, timeout=1) as response:
                self.assertEqual(response.read(), b'fixture bytes')
        self.assertEqual([credential for _, credential in seen],
                         ['Bearer fixture-credential', 'Bearer fixture-credential', None])

    def test_redirects_keep_credentials_only_on_same_https_origin(self):
        request = Request('https://huggingface.co/fixture/models/resolve/main/bundle.zip',
                          headers={'Authorization': 'Bearer fixture-credential', 'Cookie': 'fixture-cookie'})
        handler = bootstrap.HubRedirectHandler()
        same = handler.redirect_request(request, None, 302, 'Found', {}, 'https://huggingface.co/api/resolve-cache/fixture')
        self.assertEqual(same.get_header('Authorization'), 'Bearer fixture-credential')
        for target in ('https://storage.example.invalid/file', 'https://huggingface.co:444/file'):
            with self.subTest(target=target):
                redirected = handler.redirect_request(request, None, 302, 'Found', {}, target)
                self.assertIsNone(redirected.get_header('Authorization'))
                self.assertIsNone(redirected.get_header('Cookie'))
                # Returning to the Hub must not restore a removed credential.
                back = handler.redirect_request(redirected, None, 302, 'Found', {}, request.full_url)
                self.assertIsNone(back.get_header('Authorization'))
        self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-credential')

    def test_insecure_or_credentialed_redirect_urls_are_rejected(self):
        handler = bootstrap.HubRedirectHandler()
        authority = '{user}:{password}@{host}'.format(user='fixture-name', password='fixture-password', host='huggingface.co')
        credentialed = urlunsplit(('https', authority, '/file', '', ''))
        for target in ('http://huggingface.co/file', credentialed):
            with self.subTest(target=target), self.assertRaises(ValueError):
                handler.redirect_request(Request('https://huggingface.co/file'), None, 302, 'Found', {}, target)

    def test_missing_protected_queue_checks_only_legacy_metadata(self):
        with patch.object(bootstrap, 'urlopen', side_effect=HTTPError('fixture', 404, 'Missing', {}, None)) as opened:
            self.assertIsNone(bootstrap.prepared_evaluation('fixture/data', 'fixture/models', 'fixture-credential'))
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(opened.call_args_list[0].args[0].full_url,
                         'https://huggingface.co/fixture/models/resolve/main/training/evaluation_request.json')
        self.assertEqual(opened.call_args_list[1].args[0].full_url,
                         'https://huggingface.co/datasets/fixture/data/resolve/main/training/evaluation_request.json')

    def test_dataset_only_intent_cannot_execute_or_silently_start_regular_training(self):
        legacy = io.BytesIO(b'{"mode":"model-evaluation","status":"queued"}')
        with patch.object(bootstrap, 'urlopen', side_effect=[HTTPError('fixture', 404, 'Missing', {}, None), legacy]) as opened:
            with self.assertRaisesRegex(RuntimeError, 'older TEMPO version'):
                bootstrap.prepared_evaluation('fixture/data', 'fixture/models', 'fixture-credential')
        self.assertEqual(opened.call_count, 2)
        self.assertTrue(all(call.args[0].full_url.endswith('evaluation_request.json') for call in opened.call_args_list))

    def test_legacy_queue_is_rejected_before_loading_sources(self):
        job = dict(mode='model-evaluation', dataset_repo='fixture/data', models_repo='fixture/models')
        with patch.object(bootstrap, 'urlopen', return_value=io.BytesIO(json.dumps(job).encode())):
            with self.assertRaises(ValueError):
                bootstrap.prepared_evaluation('fixture/data', 'fixture/models', 'fixture-credential')

    def test_invalid_queue_shape_cannot_start_regular_training(self):
        for payload in (b'null', b'false', b'[]'):
            with self.subTest(payload=payload), patch.object(bootstrap, 'urlopen', return_value=io.BytesIO(payload)) as opened:
                with self.assertRaises(ValueError):
                    bootstrap.prepared_evaluation('fixture/data', 'fixture/models', 'fixture-credential')
                self.assertEqual(opened.call_count, 1)

    def test_model_directory_is_separate_from_code_and_namespaced_by_repository(self):
        with tempfile.TemporaryDirectory() as directory, patch('tempo_config.get_config_dir', return_value=directory+'/config'):
            first, second = installed_model_dir('fixture/one'), installed_model_dir('fixture/two')
            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, Path(directory)/'models')
            first.mkdir(parents=True); (first/'yolo26n-scout.pt').write_bytes(b'fixture weights')
            with patch('tempo_model_catalog.installed_model_dir', return_value=first):
                self.assertEqual(local_model_path('yolo26n-scout.pt'), first/'yolo26n-scout.pt')
        with self.assertRaises(ValueError):
            local_model_path('tempo_gui.py')

    def test_default_installer_writes_only_to_model_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'candidate'; source.write_bytes(b'fixture weights')
            code = root/'app/tempo_gui.py'; code.parent.mkdir(); code.write_bytes(b'fixture application')
            destination = root/'personal/models'
            hub = TempoHub(token='fixture-credential'); hub.get_models_repo = lambda: 'fixture/models'
            manifest = dict(revision='fixture', models={'yolo26n-scout.pt': {'sha256': hub.calculate_sha256(source)}})
            with patch('tempo_model_catalog.installed_model_dir', return_value=destination), \
                 patch('tempo_hub.hf_hub_download', return_value=str(source)):
                self.assertEqual(hub.download_model_release(manifest), 1)
                self.assertEqual(hub.download_champion_weights('tempo_gui.py')[0], False)
            self.assertEqual((destination/'yolo26n-scout.pt').read_bytes(), source.read_bytes())
            self.assertEqual(code.read_bytes(), b'fixture application')
            self.assertFalse((code.parent/'yolo26n-scout.pt').exists())


if __name__ == '__main__':
    unittest.main()
