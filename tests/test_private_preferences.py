"""Personal settings never write to the source tree or installed application."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tempo_config as config
LEGACY_RULE = config.get_legacy_config_path


class PrivatePreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.legacy = self.root / 'source/config/tempo_preferences.json'
        self.private = self.root / 'user/config/tempo_preferences.json'
        self.instance = object.__new__(config.TempoConfig)
        for item in (
            patch.object(config, 'CONFIG_DIR', str(self.private.parent)),
            patch.object(config, 'CONFIG_PATH', str(self.private)),
            patch.object(config, 'get_legacy_config_path', return_value=str(self.legacy)),
        ):
            item.start(); self.addCleanup(item.stop)

    def document(self, name='Fixture Researcher'):
        result = copy.deepcopy(config.DEFAULT_PREFERENCES)
        result['collaboration']['collaborator_id']['value'] = name
        result['capture']['target_fps']['value'] = 177
        result['future_category'] = {'custom': {'value': 'preserve'}}
        return result

    def write(self, path, name='Fixture Researcher'):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.document(name), indent=2).encode()
        path.write_bytes(payload)
        return payload

    def test_source_and_frozen_installs_use_same_platform_user_directory(self):
        home = str(self.root / 'home')
        for frozen in (False, True):
            for platform, expected in (
                ('win32', self.root / 'local/TEMPO/config'),
                ('darwin', self.root / 'home/Library/Application Support/TEMPO/config'),
                ('linux', self.root / 'xdg/TEMPO/config'),
            ):
                with self.subTest(frozen=frozen, platform=platform), \
                     patch.object(config.sys, 'frozen', frozen, create=True), \
                     patch.object(config.sys, 'platform', platform), \
                     patch.object(config.os.path, 'expanduser', return_value=home), \
                     patch.dict(os.environ, {'LOCALAPPDATA': str(self.root/'local'), 'XDG_CONFIG_HOME': str(self.root/'xdg')}):
                    self.assertEqual(Path(config.get_config_dir()), expected)
                    self.assertFalse(expected.exists())

    def test_missing_platform_environment_uses_home_without_source_fallback(self):
        for platform, relative in (('win32', 'AppData/Local/TEMPO/config'), ('linux', '.config/TEMPO/config')):
            with self.subTest(platform=platform), patch.object(config.sys, 'platform', platform), \
                 patch.object(config.os.path, 'expanduser', return_value=str(self.root/'home')), \
                 patch.dict(os.environ, {'LOCALAPPDATA': '', 'XDG_CONFIG_HOME': ''}):
                self.assertEqual(Path(config.get_config_dir()), self.root / 'home' / relative)

    def test_migration_preserves_name_all_values_and_unknown_metadata_byte_for_byte(self):
        original = self.write(self.legacy)
        self.instance.load()
        self.assertEqual(self.private.read_bytes(), original)
        self.assertEqual(self.legacy.read_bytes(), original)
        self.assertEqual(self.instance.get('collaboration', 'collaborator_id'), 'Fixture Researcher')
        self.assertEqual(self.instance.get('capture', 'target_fps'), 177)
        self.assertEqual(self.instance.load_error, '')
        self.assertEqual(list(self.private.parent.glob('*.tmp')), [])
        if os.name != 'nt':
            self.assertEqual(self.private.stat().st_mode & 0o777, 0o600)

    def test_existing_private_settings_always_win_over_legacy(self):
        legacy = self.write(self.legacy, 'Legacy Name')
        private = self.write(self.private, 'Private Name')
        self.instance.load()
        self.assertEqual(self.instance.get('collaboration', 'collaborator_id'), 'Private Name')
        self.assertEqual(self.private.read_bytes(), private)
        self.assertEqual(self.legacy.read_bytes(), legacy)

    @unittest.skipIf(os.name == 'nt', 'Creating Windows symlinks may require administrator privileges.')
    def test_dangling_private_link_reports_failure_without_migration_or_recursion(self):
        legacy = self.write(self.legacy)
        self.private.parent.mkdir(parents=True)
        self.private.symlink_to(self.root / 'missing')
        self.instance.load()
        self.assertTrue(self.instance.load_error)
        self.assertTrue(self.private.is_symlink())
        self.assertEqual(self.legacy.read_bytes(), legacy)

    def test_future_saves_write_private_file_and_never_modify_legacy(self):
        legacy = self.write(self.legacy)
        self.instance.load()
        self.instance.set('collaboration', 'collaborator_id', 'Changed Fixture Name')
        self.assertTrue(self.instance.save())
        self.assertEqual(self.legacy.read_bytes(), legacy)
        self.assertEqual(json.loads(self.private.read_bytes())['collaboration']['collaborator_id']['value'], 'Changed Fixture Name')

    def test_invalid_legacy_does_not_create_private_file_or_rewrite_source(self):
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text('{invalid')
        self.instance.load()
        self.assertTrue(self.instance.load_error)
        self.assertFalse(self.private.exists())
        self.assertEqual(self.legacy.read_text(), '{invalid')

    def test_migration_failure_retains_values_and_original_without_source_fallback(self):
        original = self.write(self.legacy)
        with patch.object(config.os, 'link', side_effect=PermissionError('Fixture access denied')):
            self.instance.load()
        self.assertEqual(self.instance.get('collaboration', 'collaborator_id'), 'Fixture Researcher')
        self.assertIn('retained', self.instance.load_error)
        self.assertEqual(self.legacy.read_bytes(), original)
        self.assertFalse(self.private.exists())
        self.assertEqual(list(self.private.parent.glob('*.tmp')), [])
        with patch.object(config.os, 'makedirs', side_effect=PermissionError('Fixture access denied')):
            self.assertFalse(self.instance.save())
        self.assertEqual(self.legacy.read_bytes(), original)

    def test_concurrent_private_creation_is_not_overwritten_and_is_reloaded(self):
        legacy = self.write(self.legacy)
        def publish(source, destination):
            self.write(self.private, 'Concurrent Fixture Name')
            raise FileExistsError()
        with patch.object(config.os, 'link', side_effect=publish):
            self.instance.load()
        self.assertEqual(self.instance.get('collaboration', 'collaborator_id'), 'Concurrent Fixture Name')
        self.assertEqual(self.legacy.read_bytes(), legacy)
        self.assertEqual(list(self.private.parent.glob('*.tmp')), [])

    def test_new_install_does_not_create_source_or_private_file_until_save(self):
        self.instance.load()
        self.assertFalse(self.private.exists()); self.assertFalse(self.legacy.exists())
        self.instance.set('collaboration', 'collaborator_id', 'New Fixture Name')
        self.assertTrue(self.instance.save())
        self.assertTrue(self.private.exists()); self.assertFalse(self.legacy.exists())

    def test_frozen_install_ignores_bundled_legacy_preferences(self):
        # Use the real legacy-path rule rather than this fixture's substitution.
        self.write(self.legacy, 'Bundled Name')
        with patch.object(config, 'get_legacy_config_path', side_effect=LEGACY_RULE), \
             patch.object(config, '__file__', str(self.root / 'source/tempo_config.py')), \
             patch.object(config.sys, 'frozen', True, create=True):
            self.instance.load()
        self.assertEqual(self.instance.get('collaboration', 'collaborator_id'), 'default_collaborator')
        self.assertFalse(self.private.exists())

    def test_fresh_process_saves_name_outside_repository(self):
        root = Path(__file__).resolve().parents[1]
        code = '''
from pathlib import Path
from tempo_config import CONFIG_PATH, tempo_preferences
tempo_preferences.set('collaboration', 'collaborator_id', 'Subprocess Fixture Name')
assert tempo_preferences.save()
assert not Path(CONFIG_PATH).is_relative_to(Path.cwd())
assert not (Path.cwd() / 'config/tempo_preferences.json').exists()
'''
        env = dict(os.environ, HOME=str(self.root/'home'), USERPROFILE=str(self.root/'home'),
                   LOCALAPPDATA=str(self.root/'local'), XDG_CONFIG_HOME=str(self.root/'xdg'))
        result = subprocess.run([sys.executable, '-c', code], cwd=root, env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
