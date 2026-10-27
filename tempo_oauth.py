"""Public-client Hugging Face sign-in using PKCE and a loopback callback.

No client secret or credentials are written to files. The caller validates
repository access and saves the returned token through tempo_secure_store.
"""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import secrets
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.request import HTTPSHandler

import certifi


# Public identifier: set this after registering the TEMPO OAuth application.
# Source developers can override it without creating a private config file.
PUBLIC_CLIENT_ID = 'a28c9f34-42cd-47eb-a72e-5e1e277fb4a6'
CALLBACK_PATH = '/oauth/callback'
REGISTERED_REDIRECT_URI = 'http://127.0.0.1' + CALLBACK_PATH
# Hugging Face requires OpenID Connect. write-repos includes read-repos;
# repository access is still checked explicitly before any credential is saved.
SCOPES = ('openid', 'profile', 'read-memberships', 'write-repos')
AUTHORIZE_URL = 'https://huggingface.co/oauth/authorize'
TOKEN_URL = 'https://huggingface.co/oauth/token'


class SignInError(RuntimeError):
    """Safe, user-facing authentication failure without provider payloads."""


class SignInCancelled(SignInError):
    pass


def client_id():
    value = os.environ.get('TEMPO_HF_OAUTH_CLIENT_ID', PUBLIC_CLIENT_ID).strip()
    if len(value) > 512 or any(character.isspace() for character in value):
        raise SignInError('The Hugging Face sign-in application ID is invalid.')
    return value


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        # Never forward an authorization code or verifier to another endpoint.
        return None


def _exchange_code(identifier, code, verifier, redirect_uri):
    request = Request(TOKEN_URL, data=urlencode({
        'grant_type': 'authorization_code', 'client_id': identifier,
        'code': code, 'code_verifier': verifier, 'redirect_uri': redirect_uri,
    }).encode('ascii'), headers={
        'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json',
    }, method='POST')
    try:
        with build_opener(_NoRedirects(), HTTPSHandler(context=_tls_context())).open(request, timeout=15) as response:
            data = response.read(65537)
        if len(data) > 65536:
            raise ValueError('Oversized response')
        result = json.loads(data)
    except HTTPError as error:
        error.close()
        raise SignInError('Hugging Face could not complete sign-in. Try again, or use an access token.') from None
    except (URLError, OSError, ValueError):
        raise SignInError('Hugging Face could not complete sign-in. Try again, or use an access token.') from None
    if not isinstance(result, dict):
        raise SignInError('Hugging Face returned an invalid sign-in response. Try again.')
    token = result.get('access_token')
    if (not isinstance(token, str) or not token or len(token) > 1024
            or any(character.isspace() for character in token)
            or str(result.get('token_type', '')).lower() != 'bearer'):
        raise SignInError('Hugging Face returned an invalid sign-in credential. Try again.')
    granted = result.get('scope')
    if granted is not None and (not isinstance(granted, str) or not set(SCOPES).issubset(granted.split())):
        raise SignInError('Required permissions were not approved. Sign in again and approve repository and membership access.')
    # Identity is verified with whoami, not an unverified ID token. Expired
    # credentials require another browser sign-in; no refresh secret is stored.
    return token


def _tls_context():
    # Framework Python on macOS can ship without installed OpenSSL CA roots.
    # Add the same public CA bundle used by the Hub SDK, retaining system/custom
    # trust, hostname checks, and mandatory certificate verification.
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


class BrowserSignIn:
    """One cancellable attempt. Create off-thread; open the URL on the GUI thread."""
    def __init__(self, identifier, *, timeout=180):
        if not identifier:
            raise SignInError('Browser sign-in is not configured for this build. Use an access token.')
        self._identifier = identifier
        self._state = secrets.token_urlsafe(32)
        self._verifier = secrets.token_urlsafe(64)
        self._cancelled = threading.Event()
        self._code = None
        self._error = None
        self._started = False
        self._deadline = time.monotonic() + timeout
        attempt = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(1)

            def log_message(self, *args):
                pass  # Callback paths contain short-lived credentials.

            def do_GET(self):
                status, message = attempt._receive(self.path, self.headers.get('Host', ''))
                body = message.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'")
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(body)

        try:
            self._server = HTTPServer(('127.0.0.1', 0), Handler)
            self._server.timeout = 0.2
        except OSError:
            raise SignInError('Could not open the local sign-in callback. Use an access token or try again.') from None
        self._host = '127.0.0.1:' + str(self._server.server_port)
        self.redirect_uri = 'http://' + self._host + CALLBACK_PATH
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self._verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
        self.authorization_url = AUTHORIZE_URL + '?' + urlencode({
            'response_type': 'code', 'client_id': identifier, 'redirect_uri': self.redirect_uri,
            'scope': ' '.join(SCOPES), 'state': self._state,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
        })

    def _receive(self, path, host):
        if self._cancelled.is_set() or self._code is not None or self._error is not None:
            return 410, 'This sign-in attempt has ended. Return to TEMPO.'
        if time.monotonic() >= self._deadline:
            return 410, 'This sign-in attempt expired. Return to TEMPO and try again.'
        try:
            parsed = urlsplit(path)
        except ValueError:
            return 400, 'Invalid sign-in callback.'
        if host != self._host or parsed.path != CALLBACK_PATH or parsed.scheme or parsed.netloc or len(path) > 8192:
            return 400, 'Invalid sign-in callback.'
        try:
            values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=10)
        except ValueError:
            return 400, 'Invalid sign-in callback.'
        state = values.get('state', [])
        if len(state) != 1 or not secrets.compare_digest(state[0].encode('utf-8'), self._state.encode('ascii')):
            return 400, 'This response does not match the TEMPO sign-in attempt.'
        if 'error' in values:
            self._error = SignInError('Sign-in was not approved. Try again, or use an access token.')
            return 200, 'Sign-in was not approved. Return to TEMPO.'
        codes = values.get('code', [])
        if len(codes) != 1 or not codes[0] or len(codes[0]) > 4096:
            return 400, 'Invalid sign-in callback.'
        self._code = codes[0]
        return 200, 'Authorization received. Return to TEMPO to finish connecting your account.'

    def cancel(self):
        self._cancelled.set()

    def close(self):
        """Close an attempt before its wait worker has started."""
        self.cancel()
        self._server.server_close()
        self._code = self._verifier = self._state = None

    def wait(self):
        if self._started:
            raise SignInError('This sign-in attempt has already been used. Start another sign-in.')
        self._started = True
        try:
            while not self._cancelled.is_set() and self._code is None and self._error is None:
                if time.monotonic() >= self._deadline:
                    raise SignInError('Browser sign-in timed out. Try again, or use an access token.')
                self._server.handle_request()
            if self._cancelled.is_set():
                raise SignInCancelled('Browser sign-in cancelled.')
            if self._error:
                raise self._error
            token = _exchange_code(self._identifier, self._code, self._verifier, self.redirect_uri)
            if self._cancelled.is_set():
                raise SignInCancelled('Browser sign-in cancelled.')
            return token
        finally:
            self.close()
