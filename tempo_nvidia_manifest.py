"""Release-pinned NVIDIA downloads shared by setup and the Windows packager."""
from bisect import bisect_right
import io
from pathlib import Path
import re


MAX_ARCHIVE = 8 * 1024**3
MAX_PART = 1024**3  # comfortably below GitHub's individual release-asset limit
MAX_PARTS = 8
MAX_DESCRIPTOR = 16384
_HASH = re.compile(r'^[0-9a-f]{64}$')


def validate_descriptor(value, *, version, commit, allow_legacy=True):
    """Reject unpinned origins, reordered parts, and inconsistent byte counts."""
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value['schema_version'] not in ((1, 2) if allow_legacy else (2,))
            or value.get('profile') != 'nvidia-cu126' or value.get('app_version') != version
            or not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit)
            or value.get('commit') != commit
            or value.get('executable') != 'TEMPO/TEMPO.exe'
            or type(value.get('archive_size')) is not int
            or not 0 < value['archive_size'] <= MAX_ARCHIVE
            or not all(isinstance(value.get(key), str) and _HASH.fullmatch(value[key])
                       for key in ('archive_sha256', 'executable_sha256'))):
        raise ValueError('NVIDIA download does not match this TEMPO release.')
    base = f'https://github.com/tempo-bio/TEMPO/releases/download/v{version}/TEMPO_NVIDIA_Runtime_v{version}.zip'
    if value['schema_version'] == 1:
        if value.get('archive_url') != base:
            raise ValueError('NVIDIA download has an invalid release URL.')
        return value
    parts = value.get('parts')
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
        raise ValueError('NVIDIA download has an invalid part list.')
    total = 0
    for index, part in enumerate(parts, 1):
        if (not isinstance(part, dict) or part.get('url') != f'{base}.part{index:03d}'
                or type(part.get('size')) is not int or not 0 < part['size'] <= MAX_PART
                or not isinstance(part.get('sha256'), str) or not _HASH.fullmatch(part['sha256'])):
            raise ValueError('NVIDIA download has an invalid part, order, or release URL.')
        total += part['size']
    if total != value['archive_size']:
        raise ValueError('NVIDIA download parts do not match the total size.')
    return value


class JoinedFiles(io.RawIOBase):
    """Seek through local parts as one ZIP without a second full-size copy."""
    def __init__(self, paths):
        super().__init__()
        self.paths = [Path(path) for path in paths]
        self.offsets = [0]
        for path in self.paths:
            self.offsets.append(self.offsets[-1] + path.stat().st_size)
        self.position = 0
        self.stream = None
        self.index = None

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        self._checkClosed()
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        self._checkClosed()
        if whence not in (io.SEEK_SET, io.SEEK_CUR, io.SEEK_END):
            raise ValueError('Invalid seek mode.')
        target = offset + (self.position if whence == io.SEEK_CUR else self.offsets[-1]
                           if whence == io.SEEK_END else 0)
        if target < 0:
            raise ValueError('Cannot seek before the archive.')
        self.position = target
        return target

    def readinto(self, buffer):
        self._checkClosed()
        target = memoryview(buffer).cast('B')
        count = min(len(target), max(0, self.offsets[-1] - self.position))
        written = 0
        while written < count:
            index = bisect_right(self.offsets, self.position) - 1
            if self.index != index:
                if self.stream is not None:
                    self.stream.close()
                self.stream = self.paths[index].open('rb')
                self.index = index
            self.stream.seek(self.position - self.offsets[index])
            length = min(count - written, self.offsets[index + 1] - self.position)
            read = self.stream.readinto(target[written:written + length])
            if not read:
                raise OSError('NVIDIA download part changed while reading.')
            written += read
            self.position += read
        return written

    def close(self):
        if self.stream is not None:
            self.stream.close()
        super().close()
