"""Verify the repair builder binds the exact supplied updater without building PyInstaller."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.build_update_repair import build


class RepairBuilderTests(unittest.TestCase):
    def test_embeds_public_keys_and_exact_native_updater_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            keys = root / 'public.json'
            keys.write_text(json.dumps({'fixture': base64.b64encode(b'x' * 32).decode()}))
            updater = root / 'Update.exe'
            updater.write_bytes(b'certified updater')
            wrapper_contents = []
            def capture(command, **kwargs):
                wrapper_contents.append(Path(command[-1]).read_text())
            with patch('scripts.build_update_repair.subprocess.run', side_effect=capture) as run:
                build(keys, updater, root / 'dist', root / 'work')
            run.assert_called_once()
            self.assertIn("'fixture': " + repr(b'x' * 32), wrapper_contents[0])
            expected = hashlib.sha256(b'certified updater').hexdigest()
            self.assertIn('recovery.EMBEDDED_UPDATER_SHA256 = ' + repr(expected), wrapper_contents[0])

    def test_rejects_changed_or_missing_native_updater(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            keys = root / 'public.json'
            keys.write_text(json.dumps({'fixture': base64.b64encode(b'x' * 32).decode()}))
            updater = root / 'Update.exe'
            with self.assertRaises(ValueError):
                build(keys, updater, root/'dist', root/'work')
            updater.write_bytes(b'initial')
            with patch('scripts.build_update_repair.subprocess.run', side_effect=lambda *a, **kw: updater.write_bytes(b'changed')):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    build(keys, updater, root/'dist', root/'work')


if __name__ == '__main__':
    unittest.main()
