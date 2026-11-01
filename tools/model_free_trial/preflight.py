"""Model-free *server* admission, distinct from ASR/LLM model budgets.

No server/model/API imports. Observation and budget provenance must match the
exact trial profile; previous trials that loaded models are not measurements of it.
"""
from dataclasses import dataclass
import time
from open_llm_vtuber.model_loading_policy import AdmissionDenied, GIB, Reading

PROFILE = 'model-free-v1:mcp-off:asr-off:ollama-preload-off:voicevox-wrapper'
TEXT_PROFILE = 'text-only-v1:mcp-off:asr-off:tts-off:ollama-preload-off'


@dataclass(frozen=True)
class ServerBudget:
    physical_peak_bytes: int | None = None
    commit_peak_bytes: int | None = None
    evidence: str = 'unknown'
    evidence_id: str = ''
    profile: str = PROFILE


@dataclass(frozen=True)
class Observation:
    memory: Reading
    runner_pids: tuple | None
    captured_at: float


def admit_server(budget, observation, *, clock=time.monotonic, offline=False, profile=PROFILE):
    now = clock()
    allowed = ('measured', 'fixture') if offline else ('measured',)
    if (budget.evidence not in allowed or not budget.evidence_id or budget.profile != profile
            or type(budget.physical_peak_bytes) is not int or type(budget.commit_peak_bytes) is not int
            or budget.physical_peak_bytes <= 0 or budget.commit_peak_bytes <= 0):
        raise AdmissionDenied('server_budget_unmeasured_or_wrong_profile')
    if (not 0 <= now-observation.captured_at <= 2
            or not 0 <= now-observation.memory.captured_at <= 2):
        raise AdmissionDenied('preflight_observation_stale')
    if observation.runner_pids is None:
        raise AdmissionDenied('runner_state_unknown')
    if observation.runner_pids:
        raise AdmissionDenied('other_ollama_runner_present')
    reading = observation.memory
    if type(reading.available_bytes) is not int or type(reading.commit_headroom_bytes) is not int:
        raise AdmissionDenied('memory_unknown')
    if reading.available_bytes < int(1.75*GIB) + budget.physical_peak_bytes:
        raise AdmissionDenied('server_physical_budget_insufficient')
    if reading.commit_headroom_bytes < int(.25*GIB) + budget.commit_peak_bytes:
        raise AdmissionDenied('server_commit_budget_insufficient')
    return {'profile': profile, 'admitted': True, 'models_admitted': False,
            'mcp_children_admitted': False}


def observe_windows():
    """Fresh OS-only resource/runner inventory; no HTTP or command lines."""
    import ctypes
    from ctypes import wintypes
    from open_llm_vtuber.staged_loading import windows_memory_reading

    class Entry(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD),
            ('pid', wintypes.DWORD), ('heap', ctypes.c_size_t), ('module', wintypes.DWORD),
            ('threads', wintypes.DWORD), ('parent', wintypes.DWORD),
            ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
            ('image', wintypes.WCHAR*260)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ('Process32FirstW', 'Process32NextW'):
        getattr(kernel, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        getattr(kernel, name).restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise AdmissionDenied('process_snapshot_failed')
    runners = []
    try:
        entry = Entry(); entry.size = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise AdmissionDenied('process_inventory_failed')
        while True:
            if entry.image.lower() in ('llama-server.exe', 'ollama_llama_server.exe'):
                runners.append(entry.pid)
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18: # ERROR_NO_MORE_FILES
                    raise AdmissionDenied('process_inventory_incomplete')
                break
    finally:
        kernel.CloseHandle(snapshot)
    return Observation(windows_memory_reading(), tuple(runners), time.monotonic())
