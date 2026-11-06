"""Compile and run the actual sketch against bounded host fixtures."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


class PacerFirmwareTests(unittest.TestCase):
    def test_actual_sketch_watchdog_and_output_state_machine(self):
        compiler=shutil.which('clang++') or shutil.which('g++')
        if compiler is None:
            self.skipTest('Host C++ compiler unavailable; Arduino board compile remains a separate check.')
        fixture=Path(__file__).parent/'firmware'
        with tempfile.TemporaryDirectory() as directory:
            binary=Path(directory)/('pacer-harness.exe' if os.name=='nt' else 'pacer-harness')
            # Windows compiler startup and linking can exceed 30 s on a busy CI runner.
            # Keep compilation bounded separately from the firmware's execution deadline.
            try:
                subprocess.run([compiler,'-std=c++17','-I',str(fixture),str(fixture/'pacer_harness.cpp'),'-o',str(binary)],
                               check=True,capture_output=True,text=True,timeout=120)
            except subprocess.TimeoutExpired as exc:
                self.fail(f'Firmware host compilation exceeded 120 seconds: {exc.cmd}\n'
                          f'stdout: {exc.stdout!r}\nstderr: {exc.stderr!r}')
            except subprocess.CalledProcessError as exc:
                self.fail(f'Firmware host compilation failed ({exc.returncode}): {exc.cmd}\n'
                          f'{exc.stdout}\n{exc.stderr}')
            result=subprocess.run([str(binary)],check=True,capture_output=True,text=True,timeout=10)
            self.assertIn('checks passed',result.stdout)
