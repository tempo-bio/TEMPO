"""Model transfer events without console progress bars or global tqdm locks."""
from dataclasses import dataclass
import time


@dataclass(frozen=True)
class ModelTransferProgress:
    stage: str
    filename: str = ''
    index: int = 0
    count: int = 0
    received: int = 0
    total: int | None = None
    transferred: int | None = None


class ModelTransferReporter:
    def __init__(self, callback, filenames):
        self.callback = callback
        self.filenames = list(filenames)

    def emit(self, stage, filename='', received=0, total=None, transferred=None):
        if self.callback is None:
            return
        event = ModelTransferProgress(stage, filename,
            self.filenames.index(filename)+1 if filename in self.filenames else 0,
            len(self.filenames), max(0, int(received)), total, transferred)
        try:
            self.callback(event)
        except Exception:
            # A progress display cannot interrupt a verified installation.
            pass

    def download_class(self, filename):
        reporter = self

        class DownloadProgress:
            """The Hub's supported tqdm_class interface, with no tqdm ancestry."""
            def __init__(self, *, total=None, initial=0, **kwargs):
                self.n = initial
                self.total = total
                self.transferred = None
                self.desc = kwargs.get('desc', '')
                self._last_report = 0
                self.refresh()

            def update(self, amount=1):
                self.n += amount
                if time.monotonic()-self._last_report >= .1:
                    self.refresh()

            def refresh(self, **kwargs):
                self._last_report = time.monotonic()
                reporter.emit('downloading', filename, self.n, self.total, self.transferred)

            def update_transfer(self, amount=1):
                # Xet distinguishes reconstructed file bytes from network bytes.
                # Supplying this method also prevents it creating a second bar
                # that would report conflicting byte counters for the same file.
                self.transferred = (self.transferred or 0) + amount
                if time.monotonic()-self._last_report >= .1:
                    self.refresh()

            def set_postfix_str(self, text, refresh=True):
                if refresh:
                    self.refresh()

            set_transfer_postfix_str = set_postfix_str

            def set_description(self, text, refresh=True):
                self.desc = text
                if refresh:
                    self.refresh()

            set_description_str = set_description

            @property
            def format_dict(self):
                return {'rate': None}

            def close(self):
                self.refresh()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        return DownloadProgress
