"""Compile and run the actual sketch against bounded host fixtures."""
from pathlib import Path
import os
import json
import shutil
import subprocess
import tempfile
import unittest


class PacerFirmwareTests(unittest.TestCase):
    def compile_harness(self, source, directory, *include_dirs):
        # MinGW supplies matching C++ headers on Windows runners.
        preferred, alternate = ('g++', 'clang++') if os.name == 'nt' else ('clang++', 'g++')
        compiler = shutil.which(preferred) or shutil.which(alternate)
        if compiler is None:
            self.skipTest('Host C++ compiler unavailable; Arduino board compile remains a separate check.')
        binary = Path(directory) / source.stem
        if os.name == 'nt':
            binary = binary.with_suffix('.exe')
        command = [compiler, '-std=c++17']
        for path in (source.parent, *include_dirs):
            command.extend(['-I', str(path)])
        command.extend([str(source), '-o', str(binary)])
        # Windows compiler startup and linking can exceed 30 s on a busy CI runner.
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as exc:
            self.fail(f'Firmware host compilation exceeded 120 seconds: {exc.cmd}\n'
                      f'stdout: {exc.stdout!r}\nstderr: {exc.stderr!r}')
        except subprocess.CalledProcessError as exc:
            self.fail(f'Firmware host compilation failed ({exc.returncode}): {exc.cmd}\n'
                      f'{exc.stdout}\n{exc.stderr}')
        return binary

    def test_actual_sketch_watchdog_and_output_state_machine(self):
        source = Path(__file__).parent / 'firmware' / 'pacer_harness.cpp'
        with tempfile.TemporaryDirectory() as directory:
            binary = self.compile_harness(source, directory)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=10)
            self.assertIn('checks passed', result.stdout)

    def test_development_capture_preserves_output_watchdog_and_raw_protocol(self):
        source = Path(__file__).parent / 'firmware' / 'investigation_harness.cpp'
        with tempfile.TemporaryDirectory() as directory:
            binary = self.compile_harness(source, directory, source.parents[2] / 'stimulation_investigation_1')
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=10)
            pages = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            self.assertEqual(len(pages), 2)
            from stimulation_investigation_1.capture import parse_page
            _, first = parse_page(pages[0], 1, 0, 8)
            _, second = parse_page(pages[1], 1, 6, 8)
            self.assertEqual(len(first + second), 8)
            self.assertEqual([r['raw_count'] for r in first + second], [1024, 64] * 4)
            self.assertIn('checks passed', result.stdout)
