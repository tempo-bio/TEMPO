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
            subprocess.run([compiler,'-std=c++17','-I',str(fixture),str(fixture/'pacer_harness.cpp'),'-o',str(binary)],check=True,capture_output=True,timeout=30)
            result=subprocess.run([str(binary)],check=True,capture_output=True,text=True,timeout=10)
            self.assertIn('checks passed',result.stdout)
