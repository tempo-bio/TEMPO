"""The optional worker archive must match the tested build and pinned descriptor."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.package_nvidia_runtime import package, verify_descriptor


class NvidiaRuntimePackageTests(unittest.TestCase):
    def test_archive_identity_and_tamper_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundle = root/'bundle'
            (bundle/'_internal/torch/lib').mkdir(parents=True)
            (bundle/'TEMPO.exe').write_bytes(b'tested executable')
            (bundle/'_internal/torch/lib/torch_cuda.dll').write_bytes(b'CUDA A')
            (bundle/'_internal/torch/lib/c10_cuda.dll').write_bytes(b'CUDA B')
            provenance = root/'build-provenance.json'
            commit = 'a'*40
            provenance.write_text(json.dumps({'commit': commit, 'profile':'nvidia-cu126'}))
            descriptor = package(bundle, root/'output', tag='v1.0.1', provenance=provenance)
            archive = root/'output/TEMPO_NVIDIA_Runtime_v1.0.1.zip'
            self.assertEqual(verify_descriptor(root/'output/nvidia-runtime.json', archive, commit=commit), descriptor)
            with self.assertRaisesRegex(ValueError, 'wrong profile'):
                provenance.write_text(json.dumps({'commit': commit, 'profile':'cpu'}))
                package(bundle, root/'other', tag='v1.0.1', provenance=provenance)
            with archive.open('ab') as stream:
                stream.write(b'tampering')
            with self.assertRaisesRegex(ValueError, 'does not match'):
                verify_descriptor(root/'output/nvidia-runtime.json', archive, commit=commit)


if __name__ == '__main__':
    unittest.main()
