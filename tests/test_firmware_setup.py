"""Guided upload tests never download tools or flash an actual device."""
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile
import yaml
from tempo_firmware import FirmwareInstaller,FQBN,CORE_VERSION


class FirmwareSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.installer=FirmwareInstaller(self.temp.name)

    def test_wrong_board_and_missing_port_never_upload(self):
        self.installer.run=Mock(return_value=json.dumps(dict(detected_ports=[dict(
            port=dict(address='COM3'),matching_boards=[dict(fqbn='arduino:avr:uno')])])) )
        for port in ('COM3','COM4'):
            with self.subTest(port=port),self.assertRaises(RuntimeError): self.installer.identify(port)
        self.assertTrue(all(call.args[0][:2]==['board','list'] for call in self.installer.run.call_args_list))

    def test_correct_board_is_compiled_uploaded_and_verified_in_order(self):
        events=[]
        self.installer.prepare=lambda: events.append('prepare')
        self.installer.identify=lambda port: events.append('identify:'+port)
        self.installer.run=lambda args,**kwargs: events.append(args[0])
        self.installer.install('fixture','sketch',lambda port: events.append('verify:'+port))
        self.assertEqual(events,['prepare','identify:fixture','compile','identify:fixture','upload','verify:fixture'])
        self.assertFalse(self.installer.uploading)

    def test_compile_failure_never_uploads(self):
        self.installer.prepare=Mock(); self.installer.identify=Mock()
        self.installer.run=Mock(side_effect=RuntimeError('compile failed'))
        with self.assertRaisesRegex(RuntimeError,'compile failed'): self.installer.install('fixture','sketch')
        self.assertEqual(self.installer.run.call_count,1)
        self.assertEqual(self.installer.run.call_args.args[0][0],'compile')

    def test_cancellation_before_flashing_retains_current_firmware(self):
        self.installer.cancelled.set()
        with self.assertRaises(InterruptedError): self.installer.ensure_cli()

    def test_private_config_preserves_user_arduino_configuration(self):
        self.installer.prepare_config()
        value=yaml.safe_load(self.installer.config.read_text())
        expected={key:str((Path(self.temp.name)/name).resolve()) for key,name in
                  (('data','data'),('downloads','downloads'),('user','sketchbook'))}
        self.assertEqual(value,{'directories':expected})

    def test_archive_checksum_failure_does_not_install_executable(self):
        self.installer.platform_key=('Windows','x86_64')
        response=Mock(); response.__enter__=Mock(return_value=response); response.__exit__=Mock()
        response.read=Mock(side_effect=[b'invalid archive',b''])
        with patch('tempo_firmware.urlopen',return_value=response),self.assertRaisesRegex(RuntimeError,'checksum'):
            self.installer.ensure_cli()
        self.assertFalse(self.installer.executable.exists())

    def test_cached_board_support_avoids_network_installation(self):
        self.installer.ensure_cli=Mock(); self.installer.prepare_config=Mock()
        self.installer.run=Mock(return_value=json.dumps(dict(platforms=[dict(id='arduino:renesas_uno',installed=CORE_VERSION)])))
        self.installer.prepare()
        self.assertEqual(self.installer.run.call_count,1)

    def test_cancel_is_refused_after_upload_begins(self):
        self.installer.uploading=True
        self.assertFalse(self.installer.cancel_setup())
        self.assertFalse(self.installer.cancelled.is_set())
        self.installer.uploading=False
        self.assertTrue(self.installer.cancel_setup())
        self.assertTrue(self.installer.cancelled.is_set())


if __name__=='__main__': unittest.main()
