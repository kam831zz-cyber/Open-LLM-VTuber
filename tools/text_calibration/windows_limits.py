"""Private named Job: hard commit/CPU/child-count limits, never a physical-RAM cap."""
import ctypes as c
from ctypes import wintypes as w


def create(api, approval, name):
    handle=api.k.CreateJobObjectW(None,name)
    if not handle:raise OSError('calibration_job_create_failed')
    try:
        # This random per-run name must never attach to another owner's Job.
        if c.get_last_error()==183:raise OSError('calibration_job_already_exists')
        limits=api.Limits()
        limits.basic.flags=0x2000|0x100|0x200|0x8
        limits.basic.active=4  # venv redirector + interpreter + console support; no MCP
        limits.process_memory=approval.job_commit_cap_bytes
        limits.job_memory=approval.job_commit_cap_bytes
        if not api.k.SetInformationJobObject(handle,9,c.byref(limits),c.sizeof(limits)):
            raise OSError('calibration_commit_limit_failed')
        class CPU(c.Structure):_fields_=[('flags',w.DWORD),('rate',w.DWORD)]
        cpu=CPU(1|4,approval.cpu_cap_percent*100)
        if not api.k.SetInformationJobObject(handle,15,c.byref(cpu),c.sizeof(cpu)):
            raise OSError('calibration_cpu_limit_failed')
        return handle
    except BaseException:
        api.k.CloseHandle(handle);raise


def peak_commit(api, handle):
    limits=api.Limits()
    if not api.k.QueryInformationJobObject(handle,9,c.byref(limits),c.sizeof(limits),None):
        raise OSError('job_peak_query_failed')
    return int(limits.peak_job)


def assert_current_job(name):
    kernel=c.WinDLL('kernel32',use_last_error=True)
    kernel.OpenJobObjectW.argtypes=[w.DWORD,w.BOOL,w.LPCWSTR]
    kernel.OpenJobObjectW.restype=w.HANDLE
    kernel.GetCurrentProcess.restype=w.HANDLE
    kernel.IsProcessInJob.argtypes=[w.HANDLE,w.HANDLE,c.POINTER(w.BOOL)]
    kernel.CloseHandle.argtypes=[w.HANDLE]
    handle=kernel.OpenJobObjectW(4,False,name) # query only; close immediately
    if not handle:raise OSError('private_job_not_found')
    try:
        belongs=w.BOOL()
        if not kernel.IsProcessInJob(kernel.GetCurrentProcess(),handle,c.byref(belongs)) or not belongs.value:
            raise OSError('candidate_not_in_private_job')
    finally:kernel.CloseHandle(handle)


def vm_identities(api):
    handle=api.k.CreateToolhelp32Snapshot(2,0)
    if handle==c.c_void_p(-1).value:raise OSError('vm_process_inventory_failed')
    result={}
    try:
        entry=api.ProcessEntry();entry.size=c.sizeof(entry)
        found=api.k.Process32FirstW(handle,c.byref(entry))
        if not found:raise OSError('vm_inventory_empty_or_failed')
        while found:
            if entry.exe.lower() in ('vboxheadless.exe','virtualboxvm.exe'):
                identity=api.identity(entry.pid)
                if identity.get('state')!='running':raise OSError('vm_identity_unconfirmed')
                result[entry.pid]=identity['created_filetime']
            found=api.k.Process32NextW(handle,c.byref(entry))
        if c.get_last_error()!=18:raise OSError('vm_inventory_incomplete')
        return result
    finally:api.k.CloseHandle(handle)
