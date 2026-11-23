"""Exercise the offline focus workflow with original-depth TIFFs."""

import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import cv2
import numpy as np

from scripts.calibrate_focus import read_reference
from tempo_quality import assess
from tempo_recording import NativeWriter, metadata_path


ROOT = Path(__file__).resolve().parents[1]


def focal_planes(field):
    y, x = np.indices((96, 96), dtype=float)
    texture = .5 + .18 * np.sin((x + y * field / 4) * 2 * np.pi / 7) + .09 * np.cos(y * 2 * np.pi / 14)
    sharp = (texture * 4095).astype(np.uint16)
    return sharp, cv2.GaussianBlur(sharp, (0, 0), 4)


class FocusCalibrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.held_out = focal_planes(2)
        self.signature = 'a' * 64
        self.reference = self.directory / 'reference.tif'
        writer = NativeWriter(self.reference, 20, sensor_bits=12)
        writer.metadata['quality_signature'] = self.signature
        try:
            writer.write(self.held_out[0])
        finally:
            writer.close()
        self.manifest = self.directory / 'focus-labels.csv'
        self.profile_path = self.directory / 'candidate' / 'focus_profile.json'
        with self.manifest.open('w', encoding='utf-8-sig', newline='') as stream:
            rows = csv.writer(stream)
            rows.writerow(('image', 'field', 'partition', 'label'))
            for field, partition in ((1, 'train'), (2, 'test')):
                for label, frame in zip(('sharp', 'out_of_focus'), focal_planes(field)):
                    name = f'field {field}, {label}.tif'
                    ok, data = cv2.imencode('.tif', frame)
                    self.assertTrue(ok)
                    (self.directory / name).write_bytes(data.tobytes())
                    rows.writerow((name, f'field-{field}', partition, label))

    def calibrate(self, reference):
        return subprocess.run([sys.executable, '-m', 'scripts.calibrate_focus',
                               '--manifest', str(self.manifest), '--reference', str(reference),
                               '--output', str(self.profile_path)],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)

    def test_command_fits_at_active_sensor_depth_and_preserves_existing_profile(self):
        # The sidecar can be transferred to a computer without the reference movie.
        self.reference.unlink()
        result = self.calibrate(metadata_path(self.reference))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('2/2 matched, 0 false Sharp', result.stdout)
        created = self.profile_path.read_bytes()
        profile = json.loads(created)
        self.assertEqual(profile['sensor_bits'], 12)
        self.assertEqual(profile['signature'], self.signature)
        live_results = [assess(frame, self.signature, profile, sensor_bits=12)['focus'] for frame in self.held_out]
        self.assertEqual(live_results, ['sharp', 'out_of_focus'])
        self.assertEqual(live_results, [row['actual'] for row in profile['validation']])

        result = self.calibrate(metadata_path(self.reference))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already exists', result.stderr)
        self.assertEqual(self.profile_path.read_bytes(), created)

    def test_command_reports_inconsistent_snapshot_format(self):
        path = self.directory / 'field 2, sharp.tif'
        ok, data = cv2.imencode('.tif', np.zeros((96, 96), np.uint8))
        self.assertTrue(ok)
        path.write_bytes(data.tobytes())
        result = self.calibrate(self.reference)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('field 2, sharp.tif', result.stderr)
        self.assertIn('expected uint16 pixels', result.stderr)
        self.assertFalse(self.profile_path.exists())

    def test_reference_requires_native_16_bit_sensor_depth_and_accepts_legacy_8_bit(self):
        sidecar = metadata_path(self.reference)
        metadata = json.loads(sidecar.read_text())
        metadata['sensor_bits'] = None
        sidecar.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, 'active sensor bit depth'):
            read_reference(sidecar)
        metadata['dtype'] = 'uint8'
        sidecar.write_text(json.dumps(metadata))
        self.assertEqual(read_reference(sidecar), (self.signature, 8, (96, 96), 'uint8'))
