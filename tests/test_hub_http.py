"""Desktop network policy with the real Hub client and an offline transport."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from huggingface_hub.utils._http import httpx
from huggingface_hub import HfApi, set_client_factory
from huggingface_hub.utils import _http
from huggingface_hub.errors import OfflineModeIsEnabled

import tempo_hub as hub_module


class DesktopHttpTests(unittest.TestCase):
    def setUp(self):
        previous = _http._GLOBAL_CLIENT_FACTORY
        self.addCleanup(set_client_factory, previous)
        configured = patch.object(hub_module, '_desktop_http_configured', False)
        configured.start(); self.addCleanup(configured.stop)
        # HTTP calls use only MockTransport, independent of the host's mode.
        offline = patch.object(_http.constants, 'HF_HUB_OFFLINE', False)
        offline.start(); self.addCleanup(offline.stop)

    def test_desktop_metadata_timeout_reaches_the_caller(self):
        requests = []
        def stalled(request):
            requests.append(request)
            raise httpx.ReadTimeout('Server stopped responding', request=request)
        def factory():
            return httpx.Client(transport=httpx.MockTransport(stalled), timeout=None,
                                event_hooks={'request': [_http.hf_request_event_hook]})
        with patch.object(_http, 'default_client_factory', side_effect=factory):
            hub_module.configure_desktop_http()
            api = HfApi(endpoint='https://desktop.invalid', token='fixture-token')
            with self.assertRaises(httpx.ReadTimeout):
                api.repo_info('fixture/data', repo_type='dataset')
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].extensions['timeout'],
                         {'connect': 10, 'read': 30, 'write': 30, 'pool': 30})

    def test_sdk_offline_guard_and_shorter_download_timeout_are_preserved(self):
        hub_module.configure_desktop_http()
        client = _http.get_session()
        self.assertTrue(client.follow_redirects)
        with patch.object(_http.constants, 'HF_HUB_OFFLINE', True):
            with self.assertRaises(OfflineModeIsEnabled):
                client.get('https://desktop.invalid', timeout=3)
        request = client.build_request('GET', 'https://desktop.invalid', timeout=3)
        self.assertEqual(request.extensions['timeout']['read'], 3)

    def test_profile_commit_timeout_is_confirmed_only_when_remotely_applied(self):
        state = {}
        requests = []
        def handle(request):
            requests.append(request)
            if '/preupload/' in request.url.path:
                files = json.loads(request.content)['files']
                return httpx.Response(200, json={'files': [
                    {'path': item['path'], 'uploadMode': 'regular', 'shouldIgnore': False}
                    for item in files]})
            if '/commit/' in request.url.path:
                state['commits'] += 1
                if state['apply']:
                    items = [json.loads(line) for line in request.content.splitlines()]
                    value = items[1]['value']
                    state['manifest'] = json.loads(base64.b64decode(value['content']))
                raise httpx.ReadTimeout('Commit response stalled', request=request)
            return httpx.Response(200, json={'id': 'fixture/data', 'sha': 'a'*40, 'private': True})
        def factory():
            return httpx.Client(transport=httpx.MockTransport(handle), timeout=None,
                                event_hooks={'request': [_http.hf_request_event_hook]})
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(_http, 'default_client_factory', side_effect=factory), \
             patch.object(hub_module, 'LOCAL_LOCK_CACHE', str(Path(folder)/'cache.json')):
            hub_module.configure_desktop_http()
            for applied in (False, True):
                with self.subTest(applied=applied):
                    state.update(apply=applied, commits=0, manifest={'version': 1, 'images': {}})
                    hub = hub_module.TempoHub(token='fixture-token')
                    hub.api = HfApi(endpoint='https://desktop.invalid', token=hub.token)
                    hub._identity_token, hub._account_id, hub._account_name = hub.token, 'hf:fixture', 'fixture'
                    with patch.object(hub, 'get_dataset_repo', return_value='fixture/data'), \
                         patch.object(hub, 'profile_name', return_value='Shared Laboratory'), \
                         patch.object(hub, '_read', side_effect=lambda *args: copy.deepcopy(state['manifest'])):
                        if applied:
                            self.assertTrue(hub.publish_collaborator_profile())
                        else:
                            with self.assertRaises(httpx.ReadTimeout):
                                hub.publish_collaborator_profile()
                            self.assertFalse((Path(folder)/'cache.json').exists())
                    self.assertEqual(state['commits'], 1)
        self.assertTrue(requests)
        self.assertTrue(all(all(value is not None for value in request.extensions['timeout'].values())
                            for request in requests))

    def test_repeated_desktop_setup_keeps_the_existing_client(self):
        hub_module.configure_desktop_http()
        client = _http.get_session()
        hub_module.configure_desktop_http()
        self.assertIs(_http.get_session(), client)
