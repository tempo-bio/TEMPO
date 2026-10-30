"""Windows job ownership for model workers and all of their descendants.

https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
"""
import ctypes
from ctypes import wintypes
import os
import signal
import subprocess
import time


def stop_tree(process):
    if os.name == 'nt':
        if process.poll() is not None:
            return  # Its Windows job is closed first, including orphan helpers.
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


def job_for_child():
    return WindowsJob() if os.name == 'nt' else None


def run_model_worker(command, *, timeout):
    """Run a headless helper, owning descendants through exit and timeout."""
    options = ({'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)} if os.name == 'nt'
               else {'start_new_session': True})
    job = job_for_child()
    if job is not None:
        options['creationflags'] |= 0x4  # CREATE_SUSPENDED
    process = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
        if job is not None:
            job.start(process)
        process.wait(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode)
    finally:
        try:
            if job is not None:
                job.close()
        finally:
            if process is not None:
                stop_tree(process)


class WindowsJob:
    def __init__(self):
        size = ctypes.c_size_t
        uint32 = ctypes.c_uint32
        class BasicLimits(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', uint32), ('minimum', size), ('maximum', size),
                        ('active_processes', uint32), ('affinity', size),
                        ('priority', uint32), ('scheduling', uint32)]
        class IOCounts(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('basic', BasicLimits), ('io', IOCounts), ('process_memory', size),
                        ('job_memory', size), ('peak_process_memory', size), ('peak_job_memory', size)]
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in
                        ('user_time', 'kernel_time', 'period_user_time', 'period_kernel_time')]
            _fields_ += [(name, uint32) for name in
                         ('page_faults', 'total_processes', 'active_processes', 'terminated_processes')]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                     wintypes.DWORD, ctypes.c_void_p]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        self.kernel = kernel
        self.accounting_type = Accounting
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def start(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        # The process is created suspended so it cannot create an unowned child
        # between creation and job assignment. psutil uses NtResumeProcess.
        import psutil
        psutil.Process(process.pid).resume()

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            try:
                if not self.kernel.TerminateJobObject(handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
                deadline = time.monotonic()+15
                while True:
                    accounting = self.accounting_type()
                    if not self.kernel.QueryInformationJobObject(handle, 1, ctypes.byref(accounting),
                                                                  ctypes.sizeof(accounting), None):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if not accounting.active_processes:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Model helpers did not finish terminating.')
                    time.sleep(.01)
            finally:
                if not self.kernel.CloseHandle(handle):
                    raise ctypes.WinError(ctypes.get_last_error())
