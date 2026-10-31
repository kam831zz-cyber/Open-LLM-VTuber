"""Private Windows Job Object ownership and lightweight OS measurements."""
import ctypes as c
from ctypes import wintypes as w
import socket
import struct

k = c.WinDLL('kernel32', use_last_error=True)
for name, args, result in (
    ('CreateJobObjectW', [c.c_void_p, w.LPCWSTR], w.HANDLE),
    ('SetInformationJobObject', [w.HANDLE,c.c_int,c.c_void_p,w.DWORD], w.BOOL),
    ('AssignProcessToJobObject',[w.HANDLE,w.HANDLE],w.BOOL),
    ('QueryInformationJobObject',[w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.c_void_p],w.BOOL),
    ('CloseHandle',[w.HANDLE],w.BOOL),
    ('OpenProcess',[w.DWORD,w.BOOL,w.DWORD],w.HANDLE),
    ('QueryFullProcessImageNameW',[w.HANDLE,w.DWORD,w.LPWSTR,c.POINTER(w.DWORD)],w.BOOL),
    ('GetProcessTimes',[w.HANDLE,c.c_void_p,c.c_void_p,c.c_void_p,c.c_void_p],w.BOOL),
    ('GetExitCodeProcess',[w.HANDLE,c.POINTER(w.DWORD)],w.BOOL),
):
    fn=getattr(k,name);fn.argtypes=args;fn.restype=result


class Basic(c.Structure):
    _fields_=[('process_time',c.c_longlong),('job_time',c.c_longlong),('flags',w.DWORD),
              ('min_ws',c.c_size_t),('max_ws',c.c_size_t),('active',w.DWORD),
              ('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
class IO(c.Structure):
    _fields_=[(n,c.c_ulonglong) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
class Limits(c.Structure):
    _fields_=[('basic',Basic),('io',IO)]+[(n,c.c_size_t) for n in ('process_memory','job_memory','peak_process','peak_job')]
class Memory(c.Structure):
    _fields_=[('length',w.DWORD),('load',w.DWORD)]+[(n,c.c_ulonglong) for n in ('physical','available','page_limit','page_available','virtual','virtual_available','extended')]
class ProcessMemory(c.Structure):
    _fields_=[('cb',w.DWORD),('faults',w.DWORD)]+[(n,c.c_size_t) for n in ('peak_ws','ws','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile','private')]
class JobPids(c.Structure):
    _fields_=[('assigned',w.DWORD),('count',w.DWORD),('ids',c.c_size_t*32)]
psapi=c.WinDLL('psapi')
psapi.GetProcessMemoryInfo.argtypes=[w.HANDLE,c.c_void_p,w.DWORD]
class ProcessEntry(c.Structure):
    _fields_=[('size',w.DWORD),('usage',w.DWORD),('pid',w.DWORD),('heap',c.c_size_t),
              ('module',w.DWORD),('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),
              ('flags',w.DWORD),('exe',w.WCHAR*260)]
k.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD]
k.CreateToolhelp32Snapshot.restype=w.HANDLE
k.Process32FirstW.argtypes=[w.HANDLE,c.POINTER(ProcessEntry)]
k.Process32NextW.argtypes=[w.HANDLE,c.POINTER(ProcessEntry)]


def parents():
    handle=k.CreateToolhelp32Snapshot(2,0)
    if handle==c.c_void_p(-1).value:raise OSError('parent_snapshot_failed')
    result={}
    try:
        entry=ProcessEntry();entry.size=c.sizeof(entry)
        found=k.Process32FirstW(handle,c.byref(entry))
        while found:
            result[entry.pid]=entry.parent
            found=k.Process32NextW(handle,c.byref(entry))
        return result
    finally:k.CloseHandle(handle)


def listeners():
    api=c.WinDLL('iphlpapi').GetExtendedTcpTable
    api.argtypes=[c.c_void_p,c.POINTER(w.DWORD),w.BOOL,w.ULONG,c.c_int,w.ULONG]
    size=w.DWORD()
    api(None,c.byref(size),False,2,5,0)
    data=c.create_string_buffer(size.value)
    if api(data,c.byref(size),False,2,5,0):raise OSError('tcp_table_query_failed')
    rows=struct.unpack_from('I',data.raw)[0]
    return {socket.ntohs(port & 0xffff):pid for state,addr,port,remote,rport,pid
            in (struct.unpack_from('6I',data.raw,4+i*24) for i in range(rows)) if state==2}


def job():
    handle=k.CreateJobObjectW(None,None)
    if not handle:raise OSError('job_create_failed')
    limits=Limits();limits.basic.flags=0x2000
    if not k.SetInformationJobObject(handle,9,c.byref(limits),c.sizeof(limits)):
        k.CloseHandle(handle);raise OSError('job_limits_failed')
    return handle


def members(handle):
    data=JobPids()
    if not k.QueryInformationJobObject(handle,3,c.byref(data),c.sizeof(data),None):raise OSError('job_members_failed')
    if data.count>32:raise RuntimeError('unexpected_child_count')
    return list(data.ids[:data.count])


def memory():
    m=Memory();m.length=c.sizeof(m)
    if not k.GlobalMemoryStatusEx(c.byref(m)):raise OSError('memory_query_failed')
    return {'available_bytes':m.available,'commit_headroom_bytes':m.page_available}


def cpu_ticks():
    idle,kernel,user=c.c_ulonglong(),c.c_ulonglong(),c.c_ulonglong()
    if not k.GetSystemTimes(c.byref(idle),c.byref(kernel),c.byref(user)):raise OSError('cpu_query_failed')
    return idle.value,kernel.value+user.value


def identity(pid):
    handle=k.OpenProcess(0x1000|0x10,False,pid)
    if not handle:return {'pid':pid,'exited_or_inaccessible':True}
    try:
        m=ProcessMemory();m.cb=c.sizeof(m)
        psapi.GetProcessMemoryInfo(handle,c.byref(m),c.sizeof(m))
        created,exited,kernel,user=(c.c_ulonglong() for _ in range(4))
        if not k.GetProcessTimes(handle,c.byref(created),c.byref(exited),c.byref(kernel),c.byref(user)):
            raise OSError('identity_time_failed')
        if exited.value:
            return {'pid':pid,'created_filetime':created.value,'exited_or_inaccessible':True}
        name=c.create_unicode_buffer(32768);size=w.DWORD(len(name))
        if not k.QueryFullProcessImageNameW(handle,0,name,c.byref(size)):
            code=w.DWORD()
            if k.GetExitCodeProcess(handle,c.byref(code)) and code.value!=259:
                return {'pid':pid,'created_filetime':created.value,'exited_or_inaccessible':True}
            raise OSError('identity_path_failed')
        return {'pid':pid,'created_filetime':created.value,'executable':name.value,
                'working_set_bytes':m.ws,'private_bytes':m.private,
                'cpu_ticks':kernel.value+user.value}
    finally:k.CloseHandle(handle)
