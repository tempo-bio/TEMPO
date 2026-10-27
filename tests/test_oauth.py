"""PKCE, callback isolation, cancellation, and safe OAuth error handling."""
import base64
import hashlib
from http.client import HTTPConnection
import json
import ssl
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit

import tempo_oauth as oauth
from tempo_credentials import contains_access_token, redact_credentials
from tempo_hub import TempoHub


class OAuthTests(unittest.TestCase):
    def test_https_context_loads_public_roots_and_requires_valid_certificates(self):
        context = oauth._tls_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreater(len(context.get_ca_certs()), 0)

    def attempt(self, **kwargs):
        result = oauth.BrowserSignIn('public-fixture-client', **kwargs)
        self.addCleanup(result.close)
        return result

    def callback(self, attempt, **values):
        values.setdefault('state', parse_qs(urlsplit(attempt.authorization_url).query)['state'][0])
        return oauth.CALLBACK_PATH + '?' + urlencode(values, doseq=True)

    def test_authorization_uses_random_state_pkce_and_only_loopback(self):
        first = self.attempt(); second = self.attempt()
        query = parse_qs(urlsplit(first.authorization_url).query)
        self.assertEqual(query['response_type'], ['code'])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        expected = base64.urlsafe_b64encode(hashlib.sha256(first._verifier.encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(query['code_challenge'], [expected])
        self.assertNotEqual(first._verifier, second._verifier)
        self.assertNotEqual(first._state, second._state)
        self.assertEqual(first._server.server_address[0], '127.0.0.1')
        self.assertEqual(set(query['scope'][0].split()), set(oauth.SCOPES))
        self.assertNotIn('client_secret', query)

    def test_unmatched_duplicate_and_non_ascii_state_cannot_complete_signin(self):
        attempt = self.attempt()
        for state in ('wrong', ['one', 'two'], '\u00e9'):
            status, _ = attempt._receive(self.callback(attempt, state=state, code='private-code'), attempt._host)
            self.assertEqual(status, 400)
            self.assertIsNone(attempt._code)
        path = self.callback(attempt, code=['one', 'two'])
        self.assertEqual(attempt._receive(path, attempt._host)[0], 400)

    def test_callback_rejects_foreign_host_path_and_replay(self):
        attempt = self.attempt(); path = self.callback(attempt, code='private-code')
        self.assertEqual(attempt._receive(path, 'other-host')[0], 400)
        self.assertEqual(attempt._receive('/wrong-path?' + urlsplit(path).query, attempt._host)[0], 400)
        self.assertEqual(attempt._receive(path, attempt._host)[0], 200)
        self.assertEqual(attempt._receive(path, attempt._host)[0], 410)

    def test_real_loopback_callback_exchanges_only_the_matching_code(self):
        attempt = self.attempt(); result = []; failures = []
        def wait():
            try:
                result.append(attempt.wait())
            except Exception as exc:
                failures.append(exc)
        verifier = attempt._verifier
        with patch.object(oauth, '_exchange_code', return_value='fixture-access') as exchange:
            worker = threading.Thread(target=wait, daemon=True); worker.start()
            connection = HTTPConnection(attempt._host, timeout=3)
            connection.request('GET', self.callback(attempt, state='wrong', code='not-used'))
            response = connection.getresponse(); response.read()
            self.assertEqual(response.status, 400); connection.close()
            connection = HTTPConnection(attempt._host, timeout=3)
            connection.request('GET', self.callback(attempt, code='matching-code'))
            response = connection.getresponse(); body = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            self.assertNotIn('matching-code', body); connection.close()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(result, ['fixture-access'])
            exchange.assert_called_once_with('public-fixture-client', 'matching-code', verifier, attempt.redirect_uri)
        self.assertIsNone(attempt._verifier)
        self.assertEqual(attempt._server.socket.fileno(), -1)

    def test_denial_timeout_and_cancellation_do_not_exchange_a_code(self):
        with patch.object(oauth, '_exchange_code') as exchange:
            denied = self.attempt()
            denied._receive(self.callback(denied, error='access_denied'), denied._host)
            with self.assertRaisesRegex(oauth.SignInError, 'not approved'):
                denied.wait()
            expired = self.attempt(timeout=0)
            with self.assertRaisesRegex(oauth.SignInError, 'timed out'):
                expired.wait()
            cancelled = self.attempt(); cancelled.cancel()
            with self.assertRaises(oauth.SignInCancelled):
                cancelled.wait()
        exchange.assert_not_called()

    def exchange(self, payload):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with patch.object(oauth, 'build_opener', return_value=opener):
            result = oauth._exchange_code('fixture-client', 'private-code', 'private-verifier', oauth.REGISTERED_REDIRECT_URI)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, oauth.TOKEN_URL)
        values = parse_qs(request.data.decode())
        self.assertEqual(values['code_verifier'], ['private-verifier'])
        self.assertNotIn('client_secret', values)
        return result

    def test_exchange_validates_bearer_and_granted_scopes(self):
        payload = dict(access_token='fixture-access', token_type='Bearer', scope=' '.join(oauth.SCOPES))
        self.assertEqual(self.exchange(payload), 'fixture-access')
        for change in (dict(token_type='other'), dict(scope='profile'), dict(access_token='with spaces')):
            with self.assertRaises(oauth.SignInError):
                self.exchange(dict(payload, **change))

    def test_provider_failures_never_include_response_credentials(self):
        for failure in (URLError('secret-provider-detail'), HTTPError(oauth.TOKEN_URL, 400, 'private-code', {}, None)):
            opener = Mock(); opener.open.side_effect = failure
            with patch.object(oauth, 'build_opener', return_value=opener), self.assertRaises(oauth.SignInError) as caught:
                oauth._exchange_code('fixture-client', 'private-code', 'private-verifier', oauth.REGISTERED_REDIRECT_URI)
            self.assertNotIn('private-code', str(caught.exception))
            self.assertNotIn('secret-provider-detail', str(caught.exception))

    def test_oauth_tokens_are_redacted_and_cannot_be_profile_names(self):
        token = 'hf_' + 'oauth_' + 'SyntheticCredential0123456789'
        self.assertTrue(contains_access_token(token))
        self.assertEqual(redact_credentials('Bearer ' + token), 'Bearer [redacted]')

    def test_oauth_maintainer_needs_both_admin_role_and_real_write_access(self):
        hub = object.__new__(TempoHub); hub.token = 'fixture-access'
        hub.api = Mock()
        with patch.object(hub, 'get_dataset_repo', return_value='fixture-org/data'):
            hub.api.whoami.return_value = dict(name='researcher', auth=dict(type='app_token_as_user'),
                                               orgs=[dict(name='fixture-org', roleInOrg='read')])
            self.assertFalse(hub.is_maintainer())
            hub.api.auth_check.assert_not_called()
            hub.api.whoami.return_value['orgs'][0]['roleInOrg'] = 'admin'
            self.assertTrue(hub.is_maintainer())
            hub.api.auth_check.assert_called_once_with(repo_id='fixture-org/data', repo_type='dataset', token='fixture-access', write=True)
            hub.api.auth_check.side_effect = PermissionError('No write access')
            self.assertFalse(hub.is_maintainer())

    def test_current_hub_oauth_identity_retains_maintainer_access(self):
        hub = object.__new__(TempoHub); hub.token = 'fixture-access'
        hub.api = Mock()
        hub.api.whoami.return_value = dict(name='researcher', auth=dict(type='oauth'),
                                           orgs=[dict(name='fixture-org', roleInOrg='admin')])
        with patch.object(hub, 'get_dataset_repo', return_value='fixture-org/data'):
            self.assertTrue(hub.is_maintainer())
        hub.api.auth_check.assert_called_once_with(repo_id='fixture-org/data', repo_type='dataset',
                                                  token='fixture-access', write=True)


if __name__ == '__main__':
    unittest.main()
