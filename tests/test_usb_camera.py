"""DirectShow negotiation tests with capture substitutes and a Windows discovery smoke check."""

import ctypes as c
import sys
import unittest
from struct import pack_into
from unittest.mock import Mock, patch
from uuid import UUID

import cv2
import numpy as np

from tempo_usb_camera import (DirectShowDevice, DirectShowFormat, _MediaType, _decode_format,
                              _pin_formats, directshow_devices, open_directshow_camera)


class DirectShowTests(unittest.TestCase):
    def setUp(self):
        formats = patch('tempo_usb_camera.directshow_formats', return_value=[])
        self.advertised_formats = formats.start()
        self.addCleanup(formats.stop)

    @unittest.skipUnless(sys.platform == 'win32', 'DirectShow requires Windows')
    def test_native_discovery_can_list_windows_video_inputs(self):
        devices = directshow_devices()
        for index, device in devices.items():
            self.assertGreaterEqual(index, 0)
            self.assertIsInstance(device.name, str)

    def test_readable_mjpeg_recovers_from_default_mode_that_returns_no_frames(self):
        default, mjpeg = Mock(), Mock()
        default.open.return_value = mjpeg.open.return_value = True
        default.read.return_value = (False, None)
        mjpeg.read.return_value = (True, np.zeros((720, 1280, 3), dtype=np.uint8))
        device = DirectShowDevice('Microscope camera', 'usb-path')
        with patch('tempo_usb_camera.directshow_devices', return_value={2: device}), \
             patch('tempo_usb_camera.cv2.VideoCapture', side_effect=[default, mjpeg]):
            self.assertEqual(open_directshow_camera(2, expected_device=device), (mjpeg, 1280, 720))
        default.open.assert_called_once_with(2, cv2.CAP_DSHOW, [])
        mjpeg.open.assert_called_once_with(2, cv2.CAP_DSHOW, [
            cv2.CAP_PROP_FRAME_WIDTH, 1280, cv2.CAP_PROP_FRAME_HEIGHT, 720,
            cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG')])
        default.release.assert_called_once()
        mjpeg.release.assert_not_called()

    def test_advertised_microscope_mode_uses_its_actual_size_and_preserves_y16(self):
        raw_format = cv2.VideoWriter_fourcc(*'Y16 ')
        self.advertised_formats.return_value = [DirectShowFormat((2592, 1944), raw_format, 'Y16')]
        default, raw = Mock(), Mock()
        default.open.return_value = False
        raw.open.return_value = raw.set.return_value = True
        raw.read.return_value = (True, np.full((1944, 2592), 4095, dtype=np.uint16))
        device = DirectShowDevice('Monochrome microscope')
        with patch('tempo_usb_camera.directshow_devices', return_value={0: device}), \
             patch('tempo_usb_camera.cv2.VideoCapture', side_effect=[default, raw]):
            self.assertEqual(open_directshow_camera(0), (raw, 2592, 1944))
        self.advertised_formats.assert_called_once_with(0, device)
        raw.open.assert_called_once_with(0, cv2.CAP_DSHOW, [
            cv2.CAP_PROP_FRAME_WIDTH, 2592, cv2.CAP_PROP_FRAME_HEIGHT, 1944,
            cv2.CAP_PROP_FOURCC, raw_format])
        raw.set.assert_called_once_with(cv2.CAP_PROP_CONVERT_RGB, 0)
        default.release.assert_called_once()
        raw.release.assert_not_called()

    def test_working_default_skips_format_probes_and_preserves_negotiated_y16(self):
        camera = Mock()
        camera.open.return_value = camera.set.return_value = True
        camera.get.return_value = cv2.VideoWriter_fourcc(*'Y16 ')
        camera.read.return_value = (True, np.zeros((480, 640), dtype=np.uint16))
        with patch('tempo_usb_camera.directshow_devices', return_value={0: DirectShowDevice('Camera')}), \
             patch('tempo_usb_camera.cv2.VideoCapture', return_value=camera):
            self.assertEqual(open_directshow_camera(0), (camera, 640, 480))
        self.advertised_formats.assert_not_called()
        camera.set.assert_called_once_with(cv2.CAP_PROP_CONVERT_RGB, 0)

    def test_video_info_headers_decode_standard_and_negative_height_formats(self):
        subtype = UUID('3231564e-0000-0010-8000-00aa00389b71').bytes_le
        expected = DirectShowFormat((2592, 1944), cv2.VideoWriter_fourcc(*'NV12'), 'NV12')
        for offset, header in (
            (48, UUID('05589f80-c356-11ce-bf01-00aa0055595a').bytes_le),
            (72, UUID('f72a76a0-eb0a-11d0-ace4-0000c0cc16ba').bytes_le),
        ):
            with self.subTest(offset=offset):
                data = bytearray(offset + 40)
                pack_into('<Iii', data, offset, 40, 2592, -1944)
                self.assertEqual(_decode_format(subtype, header, data), expected)
                self.assertIsNone(_decode_format(subtype, header, data[:-1]))
                self.assertIsNone(_decode_format(b'unknown subtype!', header, data))

    def test_stream_caps_copies_mode_and_releases_driver_allocations(self):
        # Exercise the pointer layout and ownership at the COM call boundary.
        arena = []

        def interface(slots):
            table = (c.c_void_p * (max(slots) + 1))()
            for slot, callback in slots.items():
                table[slot] = c.cast(callback, c.c_void_p).value
            pointer = c.pointer(c.cast(table, c.POINTER(c.c_void_p)))
            arena.extend([table, pointer, *slots.values()])
            return c.cast(pointer, c.c_void_p)

        data = c.create_string_buffer(88)
        pack_into('<Iii', data, 48, 40, 1280, 960)
        media = _MediaType()
        media.major[:] = UUID('73646976-0000-0010-8000-00aa00389b71').bytes_le
        media.subtype[:] = UUID('30303859-0000-0010-8000-00aa00389b71').bytes_le
        media.format_type[:] = UUID('05589f80-c356-11ce-bf01-00aa0055595a').bytes_le
        media.format_size = len(data)
        media.format_data = c.addressof(data)
        released = []

        @c.CFUNCTYPE(c.c_uint32, c.c_void_p)
        def release(_):
            released.append(True)
            return 0

        @c.CFUNCTYPE(c.c_int32, c.c_void_p, c.POINTER(c.c_int32), c.POINTER(c.c_int32))
        def count(_, number, size):
            number[0], size[0] = 1, 128
            return 0

        @c.CFUNCTYPE(c.c_int32, c.c_void_p, c.c_int32, c.POINTER(c.POINTER(_MediaType)), c.c_void_p)
        def capabilities(_, index, output, buffer):
            output[0] = c.pointer(media)
            return 0

        config = interface({2: release, 5: count, 6: capabilities})

        @c.CFUNCTYPE(c.c_int32, c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p))
        def query(_, iid, output):
            output[0] = config.value
            return 0

        @c.CFUNCTYPE(c.c_int32, c.c_void_p, c.POINTER(c.c_int32))
        def direction(_, output):
            output[0] = 1
            return 0

        pin = interface({0: query, 9: direction})
        ole32 = Mock()
        with patch.object(c, 'WINFUNCTYPE', c.CFUNCTYPE, create=True):
            modes = list(_pin_formats(pin, ole32))
        self.assertEqual(modes, [DirectShowFormat((1280, 960), cv2.VideoWriter_fourcc(*'Y800'), 'Y800')])
        self.assertEqual(released, [True])
        frees = [call.args[0] for call in ole32.CoTaskMemFree.call_args_list]
        self.assertEqual([frees[0], frees[1].value], [c.addressof(data), c.addressof(media)])

    def test_requested_size_tries_mjpeg_when_the_driver_returns_a_different_size(self):
        preferred, mjpeg = Mock(), Mock()
        preferred.open.return_value = mjpeg.open.return_value = True
        preferred.read.return_value = (True, np.zeros((480, 640), dtype=np.uint8))
        mjpeg.read.return_value = (True, np.zeros((1080, 1920), dtype=np.uint8))
        with patch('tempo_usb_camera.directshow_devices', return_value={0: DirectShowDevice('Camera')}), \
             patch('tempo_usb_camera.cv2.VideoCapture', side_effect=[preferred, mjpeg]):
            self.assertEqual(open_directshow_camera(0, (1920, 1080)), (mjpeg, 1920, 1080))
        preferred.release.assert_called_once()
        mjpeg.release.assert_not_called()

    def test_camera_reordering_requires_a_rescan_before_opening(self):
        selected = DirectShowDevice('Capture adapter', 'usb-adapter')
        webcam = DirectShowDevice('Built-in webcam', 'webcam')
        with patch('tempo_usb_camera.directshow_devices', return_value={1: webcam}), \
             patch('tempo_usb_camera.cv2.VideoCapture') as capture:
            with self.assertRaisesRegex(RuntimeError, 'camera list changed'):
                open_directshow_camera(1, expected_device=selected)
        capture.assert_not_called()

    def test_failed_stream_releases_every_attempt_and_reports_modes(self):
        attempts = [Mock() for _ in range(3)]
        for cap in attempts:
            cap.open.return_value = False
        with patch('tempo_usb_camera.directshow_devices', return_value={0: DirectShowDevice('Camera')}), \
             patch('tempo_usb_camera.cv2.VideoCapture', side_effect=attempts):
            with self.assertRaisesRegex(RuntimeError, '640 × 480 YUY2 did not open'):
                open_directshow_camera(0)
        for cap in attempts:
            cap.release.assert_called_once()
