"""Stage an opt-in variant from the same maintained pacing sketch."""
from contextlib import contextmanager
from pathlib import Path
import shutil
import tempfile


@contextmanager
def development_sketch():
    root = Path(__file__).resolve().parent.parent / 'firmware' / 'tempo_pacer'
    with tempfile.TemporaryDirectory(prefix='tempo-investigation-firmware-') as directory:
        sketch = Path(directory) / 'stimulation_investigation_1'
        sketch.mkdir()
        source = (root / 'tempo_pacer.ino').read_text(encoding='utf-8')
        (sketch / 'stimulation_investigation_1.ino').write_text(
            '#define TEMPO_INVESTIGATION_1 1\n' + source, encoding='utf-8')
        shutil.copyfile(root / 'investigation_1.h', sketch / 'investigation_1.h')
        yield sketch
