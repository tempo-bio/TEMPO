"""Cheap source and release checks; deliberately require no installed packages."""
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import unittest

from scripts.windows_build_profile import PROFILES

ROOT = Path(__file__).resolve().parents[1]


class ReleasePreflightTests(unittest.TestCase):
    def test_python_sources_parse_on_the_target_interpreter(self):
        names = subprocess.check_output(
            ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', '*.py'], cwd=ROOT
        ).decode().split('\0')
        names = sorted(set(filter(None, names)))
        self.assertTrue(names, 'No Python source files were found.')
        for name in names:
            with self.subTest(source=name):
                ast.parse((ROOT / name).read_text(encoding='utf-8'), filename=name)

    def test_every_profile_lock_matches_its_reviewed_source_identity(self):
        for profile in PROFILES.values():
            with self.subTest(profile=profile.key):
                manifest = json.loads((ROOT / profile.source_manifest).read_text(encoding='utf-8'))
                self.assertEqual(hashlib.sha256((ROOT / profile.lock).read_bytes()).hexdigest(),
                                 manifest['windows_lock_sha256'])

    def test_release_requires_preflight_before_installing_application_dependencies(self):
        release = (ROOT / '.github/workflows/release-windows.yml').read_text(encoding='utf-8')
        # Only read job headers and their inline dependencies. Preflight runs
        # with python -S before any YAML library can be installed.
        _, *blocks = re.split(r'^  ([\w-]+):\n', release.split('\njobs:\n', 1)[1], flags=re.M)
        jobs = dict(zip(blocks[::2], blocks[1::2]))

        def dependencies(job):
            match = re.search(r'^    needs: ([^\n]+)$', jobs[job], flags=re.M)
            self.assertIsNotNone(match, f'{job} must declare its dependencies')
            return {name.strip() for name in match[1].strip('[]').split(',')}

        self.assertIn('uses: ./.github/workflows/windows-preflight.yml', jobs['preflight'])
        early_gates = {'velopack-native', 'stimulation-firmware', 'validate-source'}
        for gate in early_gates:
            self.assertEqual(dependencies(gate), {'preflight'})
        self.assertIn('Run complete regression and scientific suites', jobs['validate-source'])
        self.assertTrue(early_gates <= dependencies('build-nvidia-runtime'))
        self.assertIn('build-nvidia-runtime', dependencies('build-windows'))
        self.assertIn('verify-velopack', dependencies('replay-velopack'))
        required = early_gates | {'preflight', 'build-nvidia-runtime', 'build-windows',
                                  'verify-installed', 'verify-velopack', 'replay-velopack'}
        self.assertTrue(required <= dependencies('certify'))
        for gate in required:
            self.assertIn(f'needs.{gate}.result', jobs['certify'])
        self.assertIn("Where-Object Value -ne 'success'", jobs['certify'])
        fast = (ROOT / '.github/workflows/windows-preflight.yml').read_text(encoding='utf-8')
        self.assertIn('python -S -m unittest -v tests.test_assistance_launch tests.test_release_preflight', fast)
        self.assertIn('tests.test_nvidia_manifest tests.test_nvidia_runtime_package', fast)
        self.assertIn('Output/TEMPO_NVIDIA_Runtime_*.zip.part*', release)
        self.assertIn('--verify-descriptor Output/nvidia-runtime.json --commit', release)
        self.assertNotIn('--verify-archive', release)
        self.assertIn(r'python -m venv "$env:RUNNER_TEMP\tempo-preflight-python"', fast)
        self.assertIn("if line.startswith('psutil @ ')", fast)
        self.assertIn('-m pip install --no-deps --require-hashes', fast)
        self.assertIn('tests.test_model_assistance_runtime.AssistanceRuntimeTests.test_cancel_stops_real_helper_descendants', fast)
        self.assertIn('tests.test_model_assistance_runtime.AssistanceRuntimeTests.test_failed_parent_cannot_leave_a_running_helper', fast)
        self.assertNotIn('-r requirements-windows.lock', fast)


if __name__ == '__main__':
    unittest.main()
