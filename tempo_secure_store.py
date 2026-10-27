"""Native credential storage; never fall back to a plaintext password backend."""
import configparser
import hmac
import importlib
import io
import os
from pathlib import Path
import sys
import tempfile
import threading

SERVICE = 'TEMPO/HuggingFace'
ACCOUNT = 'active-token'
_LOCK = threading.RLock()


class CredentialStorageError(RuntimeError):
    """Safe, actionable errors that never include a credential/backend exception."""


class CredentialMigrationRequired(CredentialStorageError):
    """A protected write must migrate or clean up the active credential."""


def native_backend():
    try:
        if sys.platform == 'win32':
            backend = importlib.import_module('keyring.backends.Windows').WinVaultKeyring()
            backend.persist = 'local machine'
        elif sys.platform == 'darwin':
            backend = importlib.import_module('keyring.backends.macOS').Keyring()
            backend.keychain = None
        elif sys.platform.startswith('linux'):
            backend = importlib.import_module('keyring.backends.SecretService').Keyring()
            backend.appid = 'TEMPO'
        else:
            raise RuntimeError('Unsupported platform.')
        if backend.priority <= 0:
            raise RuntimeError('No native credential storage.')
        return backend
    except Exception:
        raise CredentialStorageError('Secure credential storage is unavailable. Install TEMPO’s credential-storage dependency and unlock your operating system’s credential store, then retry.') from None


def _read(backend):
    try:
        token = backend.get_password(SERVICE, ACCOUNT)
        if token is not None and (not isinstance(token, str) or not token.strip()):
            raise ValueError('Invalid stored credential.')
        return token
    except Exception:
        raise CredentialStorageError('The saved account could not be read from secure storage. Unlock your operating system’s credential store and retry.') from None


def _write_verified(backend, token):
    previous = _read(backend)
    try:
        backend.set_password(SERVICE, ACCOUNT, token)
        saved = backend.get_password(SERVICE, ACCOUNT)
        if not isinstance(saved, str) or not hmac.compare_digest(saved, token):
            raise ValueError('Credential read-back failed.')
    except Exception:
        restored = False
        try:
            if previous is None:
                if backend.get_password(SERVICE, ACCOUNT) is not None:
                    backend.delete_password(SERVICE, ACCOUNT)
            else:
                backend.set_password(SERVICE, ACCOUNT, previous)
            restored = _read(backend) == previous
        except Exception:
            pass
        message = 'The token could not be saved and verified in secure storage. Unlock the credential store and retry.'
        if not restored:
            message += ' The previous saved credential could not be restored; reconnect your account.'
        raise CredentialStorageError(message) from None


def legacy_paths():
    from huggingface_hub import constants
    return Path(constants.HF_TOKEN_PATH), Path(constants.HF_STORED_TOKENS_PATH)


def _snapshot(path):
    if path.is_symlink():
        raise CredentialStorageError('A legacy credential file is a symbolic link. Resolve the link before migrating your account.')
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except Exception:
        raise CredentialStorageError('A legacy credential file could not be read. Check its permissions and retry account migration.') from None


def _unchanged(path, snapshot):
    if _snapshot(path) != snapshot:
        raise CredentialStorageError('A legacy credential file changed during migration. Close other Hugging Face sign-in operations and retry.')


def _replace_legacy(path, snapshot, replacement):
    _unchanged(path, snapshot)
    if replacement is None:
        path.unlink()
        return
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.tempo-credentials-', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(replacement); stream.flush(); os.fsync(stream.fileno())
        _unchanged(path, snapshot)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def remove_legacy_copies(token, *, check_only=False):
    """Remove only copies of this token, retaining unrelated Hub credentials."""
    active_path, saved_path = legacy_paths()
    try:
        saved = _snapshot(saved_path)
        if saved:
            parser = configparser.RawConfigParser()
            parser.read_string(saved.decode('utf-8'))
            changed = False
            default_token = parser.defaults().get('hf_token', '').strip()
            if default_token and hmac.compare_digest(default_token, token):
                parser.remove_option(parser.default_section, 'hf_token')
                changed = True
            for section in parser.sections():
                value = parser.get(section, 'hf_token', fallback='').strip()
                if value and hmac.compare_digest(value, token):
                    parser.remove_option(section, 'hf_token')
                    if not parser.items(section):
                        parser.remove_section(section)
                    changed = True
            if changed:
                if check_only:
                    return True
                stream = io.StringIO(); parser.write(stream)
                replacement = stream.getvalue().encode('utf-8') if parser.sections() or parser.defaults() else None
                _replace_legacy(saved_path, saved, replacement)
        active = _snapshot(active_path)
        if active and hmac.compare_digest(active.decode('utf-8').strip(), token):
            if check_only:
                return True
            _replace_legacy(active_path, active, None)
        return False
    except CredentialStorageError:
        raise
    except Exception:
        raise CredentialStorageError('The token is saved securely, but an old plaintext copy could not be removed. Check the legacy credential-file permissions and retry. The secure copy has been retained.') from None


def load_hf_token(*, migrate=False):
    """Migrate the active legacy token only after secure storage is confirmed."""
    with _LOCK:
        backend = native_backend()
        return _load_token(backend, migrate=migrate)


def _load_token(backend, *, migrate):
    token = _read(backend)
    if token:
        if migrate:
            remove_legacy_copies(token)
        elif remove_legacy_copies(token, check_only=True):
            raise CredentialMigrationRequired('The saved token needs secure-storage migration. The existing sign-in will be preserved until the secure copy is verified.')
        return token
    active_path, _ = legacy_paths()
    original = _snapshot(active_path)
    if not original:
        return None
    if not migrate:
        raise CredentialMigrationRequired('The saved token needs secure-storage migration. The existing sign-in will be preserved until the secure copy is verified.')
    try:
        token = original.decode('utf-8').strip()
    except Exception:
        raise CredentialStorageError('The legacy token could not be read. Reconnect your account with a valid token.') from None
    if not token or len(token) > 1024 or any(character.isspace() for character in token):
        raise CredentialStorageError('The legacy token is invalid. Reconnect your account with a valid token.')
    _write_verified(backend, token)
    _unchanged(active_path, original)
    remove_legacy_copies(token)
    return token


def save_hf_token(token):
    if not isinstance(token, str) or not token or len(token) > 1024 or any(character.isspace() for character in token):
        raise CredentialStorageError('Enter a valid Hugging Face token before saving.')
    with _LOCK:
        backend = native_backend()
        # Secure the previous TEMPO sign-in before switching accounts.
        _load_token(backend, migrate=True)
        _write_verified(backend, token)
        remove_legacy_copies(token)
