"""The same package lock used by the native Velopack updater.

Windows uses a share-mode-zero file handle; Unix uses the same lockf operation
as Velopack. A Python thread guard is also needed for process-scoped POSIX locks.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import threading

_guard = threading.Lock()
_locks = {}


@contextmanager
def update_lock(packages_dir):
    root = Path(os.path.abspath(packages_dir))
    for part in (root, *root.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Update lock storage contains a link or reparse point.')
    root.mkdir(parents=True, exist_ok=True)
    path = root / '.velopack_lock'
    if path.is_symlink():
        raise ValueError('Update lock cannot be a link.')
    with _guard:
        local = _locks.setdefault(str(path), threading.Lock())
    if not local.acquire(blocking=False):
        raise BlockingIOError('Another TEMPO update operation is active.')
    handle = None
    stream = None
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                          ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            kernel.CreateFileW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.CreateFileW(str(path), 0xC0000000, 0, None, 4, 0x80, None)
            if handle == wintypes.HANDLE(-1).value:
                handle = None
                raise BlockingIOError('Another application owns the Windows update lock.')
        else:
            import fcntl
            stream = open(path, 'a+b')
            try:
                fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BlockingIOError('Another application owns the update lock.') from exc
        yield
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
        if stream is not None:
            stream.close()
        local.release()
