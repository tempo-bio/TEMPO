"""Native-store selection, fail-closed saves and isolated legacy migration."""
import configparser
import copy
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import tempo_secure_store as store
from tempo_hub import TempoHub

TOKEN = 'hf_SYNTHETICSecureStore0123456789'
OTHER = 'hf_SYNTHETICOtherAccount0123456789'


class MemoryVault:
    def __init__(self, value=None):
        self.value = value
        self.writes = []

    def get_password(self, service, account):
        return self.value

    def set_password(self, service, account, value):
        self.writes.append(value); self.value = value

    def delete_password(self, service, account):
        self.value = None


class SecureStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.active, self.saved = self.root / 'token', self.root / 'stored_tokens'
        self.vault = MemoryVault()
        for p in (patch.object(store, 'native_backend', return_value=self.vault),
                  patch.object(store, 'legacy_paths', return_value=(self.active, self.saved))):
            p.start(); self.addCleanup(p.stop)

    def seed(self, token=TOKEN, other=True):
        self.active.write_text(token)
        self.saved.write_text('[tempo]\nhf_token = ' + token + '\n' +
                              ('\n[other]\nhf_token = ' + OTHER + '\n' if other else ''))

    def test_background_read_requires_protected_migration_without_writing(self):
        self.seed(); original = self.saved.read_bytes()
        with self.assertRaises(store.CredentialMigrationRequired):
            store.load_hf_token()
        self.assertEqual(self.vault.writes, [])
        self.assertEqual(self.active.read_text(), TOKEN)
        self.assertEqual(self.saved.read_bytes(), original)

    def test_migration_verifies_vault_and_removes_only_active_token_copies(self):
        self.seed()
        self.assertEqual(store.load_hf_token(migrate=True), TOKEN)
        self.assertEqual(self.vault.value, TOKEN)
        self.assertFalse(self.active.exists())
        parser = configparser.RawConfigParser(); parser.read(self.saved)
        self.assertEqual(parser.sections(), ['other'])
        self.assertEqual(parser['other']['hf_token'], OTHER)
        self.assertNotIn(TOKEN, self.saved.read_text())
        self.assertEqual(store.load_hf_token(), TOKEN)

    def test_duplicate_copies_and_empty_cache_are_removed_without_backups(self):
        self.seed(other=False)
        self.saved.write_text(self.saved.read_text() + '\n[duplicate]\nhf_token = ' + TOKEN + '\n')
        store.load_hf_token(migrate=True)
        self.assertFalse(self.active.exists()); self.assertFalse(self.saved.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_no_legacy_active_token_does_not_guess_from_other_saved_tokens(self):
        self.saved.write_text('[other]\nhf_token = ' + OTHER + '\n')
        self.assertIsNone(store.load_hf_token(migrate=True))
        self.assertEqual(self.vault.writes, [])
        self.assertIn(OTHER, self.saved.read_text())

    def test_migration_cleans_default_entry_without_dropping_unrelated_options(self):
        self.seed(other=False)
        self.saved.write_text('[DEFAULT]\nhf_token = ' + TOKEN + '\nkeep = unchanged\n')
        store.load_hf_token(migrate=True)
        self.assertFalse(self.active.exists())
        parser = configparser.RawConfigParser(); parser.read(self.saved)
        self.assertEqual(parser.defaults(), {'keep': 'unchanged'})
        self.assertEqual(store.load_hf_token(), TOKEN)

    def test_explicit_environment_token_does_not_override_desktop_vault(self):
        self.vault.value = TOKEN
        with patch.dict(os.environ, {'HF_TOKEN': OTHER, 'HUGGING_FACE_HUB_TOKEN': OTHER}):
            self.assertEqual(store.load_hf_token(), TOKEN)

    def test_unavailable_store_never_falls_back_to_legacy_or_saves_plaintext(self):
        self.seed(); original = self.saved.read_bytes()
        with patch.object(store, 'native_backend', side_effect=store.CredentialStorageError('Unavailable')):
            with self.assertRaises(store.CredentialStorageError): store.load_hf_token(migrate=True)
            with self.assertRaises(store.CredentialStorageError): store.save_hf_token(OTHER)
        self.assertEqual(self.active.read_text(), TOKEN)
        self.assertEqual(self.saved.read_bytes(), original)

    def test_locked_read_is_sanitized_and_preserves_original_sign_in(self):
        self.seed()
        with patch.object(self.vault, 'get_password', side_effect=RuntimeError('denied ' + TOKEN)):
            with self.assertRaises(store.CredentialStorageError) as error:
                store.load_hf_token(migrate=True)
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertIn('Unlock', str(error.exception))
        self.assertEqual(self.active.read_text(), TOKEN)

    def test_failed_read_back_restores_previous_vault_and_preserves_files(self):
        self.seed(); self.vault.value = OTHER
        reads = iter([OTHER, OTHER, 'wrong-value', OTHER])
        with patch.object(self.vault, 'get_password', side_effect=lambda *a: next(reads)):
            with self.assertRaises(store.CredentialStorageError): store.save_hf_token(TOKEN)
        self.assertEqual(self.vault.value, OTHER)
        self.assertEqual(self.active.read_text(), TOKEN)
        self.assertIn(TOKEN, self.saved.read_text())

    def test_first_save_verification_failure_removes_unverified_vault_entry(self):
        reads = iter([None, None, 'wrong-value', 'wrong-value', None])
        with patch.object(self.vault, 'get_password', side_effect=lambda *a: next(reads)):
            with self.assertRaises(store.CredentialStorageError): store.save_hf_token(TOKEN)
        self.assertIsNone(self.vault.value)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_partial_cleanup_retains_verified_secure_token_and_retries(self):
        self.seed()
        with patch.object(store, '_replace_legacy', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(store.CredentialStorageError, 'old plaintext copy'):
                store.load_hf_token(migrate=True)
        self.assertEqual(self.vault.value, TOKEN)
        self.assertEqual(self.active.read_text(), TOKEN)
        with self.assertRaises(store.CredentialMigrationRequired): store.load_hf_token()
        self.assertEqual(store.load_hf_token(migrate=True), TOKEN)
        self.assertFalse(self.active.exists())

    def test_delete_hf_token_clears_vault_and_legacy_copies(self):
        store.save_hf_token(TOKEN)
        self.assertEqual(self.vault.value, TOKEN)
        store.delete_hf_token()
        self.assertIsNone(self.vault.value)
        # Verify subsequent delete does not raise even if vault is empty
        store.delete_hf_token()
        self.assertIsNone(self.vault.value)

    def test_modified_cache_is_not_overwritten(self):
        self.seed(); original = self.saved.read_bytes()
        self.saved.write_text('[replacement]\nhf_token = ' + OTHER + '\n')
        with self.assertRaisesRegex(store.CredentialStorageError, 'changed during migration'):
            store._replace_legacy(self.saved, original, None)
        self.assertIn(OTHER, self.saved.read_text())

    def test_symbolic_link_is_not_followed_or_deleted(self):
        target = self.root / 'unrelated'; target.write_text(TOKEN)
        self.active.symlink_to(target)
        with self.assertRaisesRegex(store.CredentialStorageError, 'symbolic link'):
            store.load_hf_token(migrate=True)
        self.assertEqual(target.read_text(), TOKEN)
        self.assertTrue(self.active.is_symlink())

    def test_corrupt_cache_errors_never_echo_secret_text(self):
        self.seed(); self.saved.write_text(TOKEN + '\ninvalid ini')
        with self.assertRaises(store.CredentialStorageError) as error:
            store.load_hf_token(migrate=True)
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertEqual(self.vault.value, TOKEN)
        self.assertEqual(self.active.read_text(), TOKEN)

    def test_account_switch_does_not_delete_other_hub_accounts(self):
        self.seed(token=OTHER); self.vault.value = 'hf_SYNTHETICPreviousTempo0123456789'
        store.save_hf_token(TOKEN)
        self.assertEqual(self.vault.value, TOKEN)
        self.assertEqual(self.active.read_text(), OTHER)
        self.assertIn(OTHER, self.saved.read_text())

    def test_switch_before_startup_secures_and_cleans_previous_tempo_token(self):
        self.seed(other=False)
        store.save_hf_token(OTHER)
        self.assertEqual(self.vault.writes, [TOKEN, OTHER])
        self.assertEqual(self.vault.value, OTHER)
        self.assertFalse(self.active.exists()); self.assertFalse(self.saved.exists())

    def test_hub_import_and_explicit_cloud_token_do_not_touch_vault(self):
        with patch('tempo_hub.load_hf_token', side_effect=AssertionError('Unexpected vault access')):
            hub = TempoHub()
            self.assertIsNone(hub.token)
            hub = TempoHub(token=TOKEN); hub._online()
            self.assertEqual(hub.token, TOKEN)

    def test_failed_hub_loading_is_fail_closed_and_can_retry(self):
        hub = TempoHub()
        with patch('tempo_hub.load_hf_token', side_effect=store.CredentialStorageError('Unlock secure storage')):
            with self.assertRaises(store.CredentialStorageError): hub.ensure_credentials()
        self.assertIsNone(hub.token)
        self.assertEqual(hub.api.token, False)
        self.assertFalse(hub._credentials_ready)
        with patch('tempo_hub.load_hf_token', return_value=TOKEN) as load:
            hub.ensure_credentials(migrate=True)
        load.assert_called_once_with(migrate=True)
        self.assertTrue(hub._credentials_ready)
        self.assertEqual(hub.credential_error, '')


class NativeSelectionTests(unittest.TestCase):
    def test_explicit_native_backends_ignore_plaintext_plugin_configuration(self):
        for system, module, class_name in (
            ('win32', 'keyring.backends.Windows', 'WinVaultKeyring'),
            ('darwin', 'keyring.backends.macOS', 'Keyring'),
            ('linux', 'keyring.backends.SecretService', 'Keyring'),
        ):
            with self.subTest(platform=system):
                backend = SimpleNamespace(priority=5)
                factory = Mock(return_value=backend)
                with patch.object(store.sys, 'platform', system), \
                     patch.object(store.importlib, 'import_module', return_value=SimpleNamespace(**{class_name: factory})) as imported, \
                     patch.dict(os.environ, {'PYTHON_KEYRING_BACKEND': 'keyrings.alt.file.PlaintextKeyring'}):
                    self.assertIs(store.native_backend(), backend)
                imported.assert_called_once_with(module)
                if system == 'win32': self.assertEqual(backend.persist, 'local machine')
                if system == 'darwin': self.assertIsNone(backend.keychain)

    def test_missing_native_backend_is_an_error_without_fallback(self):
        with patch.object(store.importlib, 'import_module', side_effect=ImportError('missing')):
            with self.assertRaises(store.CredentialStorageError): store.native_backend()

    def test_windows_backend_writes_local_user_generic_credentials(self):
        from keyring.backends import Windows
        credentials = {}
        class WindowsError(Exception):
            def __init__(self, function):
                self.winerror, self.funcname = 1168, function
        def read(**kwargs):
            if kwargs['TargetName'] not in credentials: raise WindowsError('CredRead')
            return copy.deepcopy(credentials[kwargs['TargetName']])
        def write(credential, flags):
            self.assertEqual(flags, 0); self.assertEqual(credential['Persist'], 2)
            self.assertEqual(credential['Type'], 1)
            saved = dict(credential); saved['CredentialBlob'] = credential['CredentialBlob'].encode('utf-16')
            credentials[credential['TargetName']] = saved
        def delete(**kwargs):
            if kwargs['TargetName'] not in credentials: raise WindowsError('CredDelete')
            del credentials[kwargs['TargetName']]
        api = SimpleNamespace(CRED_TYPE_GENERIC=1, CRED_PERSIST_LOCAL_MACHINE=2, CRED_PERSIST_ENTERPRISE=3,
                              CredRead=read, CredWrite=write, CredDelete=delete)
        with patch.object(Windows, 'missing_deps', False), patch.object(Windows, 'win32cred', api, create=True), \
             patch.object(Windows, 'pywintypes', SimpleNamespace(error=WindowsError), create=True), \
             patch.object(store.sys, 'platform', 'win32'):
            backend = store.native_backend()
            backend.set_password('fixture-service', 'fixture-account', TOKEN)
            self.assertEqual(backend.get_password('fixture-service', 'fixture-account'), TOKEN)
            backend.set_password('fixture-service', 'fixture-account', OTHER)
            self.assertEqual(backend.get_password('fixture-service', 'fixture-account'), OTHER)
            backend.delete_password('fixture-service', 'fixture-account')
            self.assertEqual(credentials, {})


if __name__ == '__main__':
    unittest.main()
