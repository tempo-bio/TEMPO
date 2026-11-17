"""Stage an opt-in variant from the same maintained pacing sketch."""
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
import tempfile
from tempo_firmware import pacing_sketch_path


@contextmanager
def development_sketch():
    with tempfile.TemporaryDirectory(prefix='tempo-investigation-firmware-') as directory:
        sketch = Path(directory) / 'stimulation_investigation_1'
        sketch.mkdir()
        source = pacing_sketch_path().read_text(encoding='utf-8')
        (sketch / 'stimulation_investigation_1.ino').write_text(
            '#define TEMPO_INVESTIGATION_1 1\n' + source, encoding='utf-8')
        (sketch / 'investigation_1.h').write_bytes(files(__package__).joinpath('investigation_1.h').read_bytes())
        yield sketch
