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
def _file_lock(path, *, shared=False):
    path = Path(os.path.abspath(path))
    root = path.parent
    for part in (root, *root.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Update lock storage contains a link or reparse point.')
    root.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
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
            handle = kernel.CreateFileW(str(path), 0xC0000000, 3 if shared else 0, None, 4, 0x80, None)
            if handle == wintypes.HANDLE(-1).value:
                handle = None
                raise BlockingIOError('Another application owns the Windows update lock.')
        else:
            import fcntl
            stream = open(path, 'a+b')
            try:
                fcntl.lockf(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except OSError as exc:
                raise BlockingIOError('Another application owns the update lock.') from exc
        yield
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
        if stream is not None:
            stream.close()
        local.release()


@contextmanager
def update_lock(packages_dir):
    """Serialize preparation and native replacement with Velopack's own lock."""
    with _file_lock(Path(packages_dir) / '.velopack_lock'):
        yield


@contextmanager
def application_lock(profile_root, *, shared=False):
    """Hold shared while an application/worker runs, exclusive during replacement.

    Callers keep the context alive for the entire process lifetime. This is
    separate from the package lock, which preparation needs while TEMPO runs.
    """
    with _file_lock(Path(profile_root) / 'updates' / '.application-lock', shared=shared):
        yield


@contextmanager
def startup_guard(profile_root):
    """Exclude replacement across startup's exclusive-to-shared lease change."""
    with _file_lock(Path(profile_root) / 'updates' / '.application-start-lock'):
        yield
