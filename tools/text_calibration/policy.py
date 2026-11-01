"""Single-use calibration authorization; never produces a measured model budget."""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time
from tools.model_free_trial.preflight import TEXT_PROFILE
from open_llm_vtuber.model_loading_policy import AdmissionDenied, GIB

SCOPE = TEXT_PROFILE + ':calibration-asgi-one-session-no-listener'
PHYSICAL_RESERVE = int(1.75*GIB)
COMMIT_RESERVE = int(.25*GIB)


@dataclass(frozen=True)
class Approval:
    run_id: str
    source_sha: str
    physical_cap_bytes: int
    job_commit_cap_bytes: int
    rapid_drop_bytes: int
    cpu_cap_percent: int
    deadline_seconds: int
    rationale: str
    expires_at: str

    @classmethod
    def parse(cls, data, *, source_sha, now=None):
        if (data.get('authorized') is not True or data.get('scope') != SCOPE
                or data.get('max_runs') != 1 or data.get('source_sha') != source_sha
                or data.get('budget_evidence') != 'provisional_not_measured'):
            raise AdmissionDenied('calibration_not_authorized_for_scope')
        names=cls.__dataclass_fields__
        try: value=cls(**{name:data[name] for name in names})
        except (KeyError,TypeError):raise AdmissionDenied('approval_incomplete') from None
        if (not isinstance(value.run_id,str) or not re.fullmatch('[a-f0-9]{32}',value.run_id)
                or not isinstance(value.source_sha,str) or not re.fullmatch('[a-f0-9]{40}',value.source_sha)
                or not isinstance(value.rationale,str) or len(value.rationale.strip())<20):
            raise AdmissionDenied('approval_identity_or_rationale_invalid')
        for name in ('physical_cap_bytes','job_commit_cap_bytes','rapid_drop_bytes'):
            if type(getattr(value,name)) is not int or getattr(value,name)<=0:
                raise AdmissionDenied('provisional_cap_missing')
        if (value.rapid_drop_bytes>value.physical_cap_bytes
                or type(value.cpu_cap_percent) is not int or not 1<=value.cpu_cap_percent<=50
                or type(value.deadline_seconds) is not int or not 1<=value.deadline_seconds<=120):
            raise AdmissionDenied('calibration_limit_invalid')
        now=now or datetime.now(timezone.utc)
        try: expiry=datetime.fromisoformat(value.expires_at)
        except (TypeError,ValueError):raise AdmissionDenied('approval_expiry_invalid') from None
        if expiry.tzinfo is None or not 0<(expiry-now).total_seconds()<=3600:
            raise AdmissionDenied('approval_expired_or_too_long')
        return value


def admit_calibration(approval, observation, *, clock=time.monotonic):
    """Separate from admit_server: a reviewed provisional envelope, not normal readiness."""
    now=clock(); reading=observation.memory
    if (not 0<=now-observation.captured_at<=2 or not 0<=now-reading.captured_at<=2):
        raise AdmissionDenied('calibration_observation_stale')
    if observation.runner_pids is None or observation.runner_pids:
        raise AdmissionDenied('runner_unknown_or_present')
    if type(reading.available_bytes) is not int or type(reading.commit_headroom_bytes) is not int:
        raise AdmissionDenied('calibration_memory_unknown')
    if reading.available_bytes < PHYSICAL_RESERVE+approval.physical_cap_bytes:
        raise AdmissionDenied('calibration_physical_headroom_insufficient')
    if reading.commit_headroom_bytes < COMMIT_RESERVE+approval.job_commit_cap_bytes:
        raise AdmissionDenied('calibration_commit_headroom_insufficient')
    return {'scope':SCOPE,'admission':'calibration_only','budget_evidence':'provisional_not_measured',
            'normal_startup_admitted':False,'models_admitted':False}


def stop_causes(approval, sample, *, baseline, previous=None):
    reasons=[]
    for field in ('available_bytes','commit_headroom_bytes','working_set_upper_bound_bytes',
                  'private_bytes_sum','job_peak_commit_bytes','monotonic_ns'):
        if type(sample.get(field)) is not int:reasons.append('measurement_missing')
    if sample.get('errors'):reasons.append('measurement_failed')
    if sample.get('runner_pids') is None or sample.get('runner_pids'):reasons.append('runner_unknown_or_appeared')
    if sample.get('protection_ok') is not True:reasons.append('protected_state_changed_or_unknown')
    if reasons:return list(dict.fromkeys(reasons))
    if sample['available_bytes']<PHYSICAL_RESERVE:reasons.append('physical_reserve_reached')
    if sample['commit_headroom_bytes']<COMMIT_RESERVE:reasons.append('commit_reserve_reached')
    if (baseline['available_bytes']-sample['available_bytes']>approval.physical_cap_bytes
            or sample['working_set_upper_bound_bytes']>approval.physical_cap_bytes):
        reasons.append('provisional_physical_cap_reached')
    if sample['private_bytes_sum']>=approval.job_commit_cap_bytes or sample['job_peak_commit_bytes']>=approval.job_commit_cap_bytes:
        reasons.append('provisional_job_commit_cap_reached')
    if previous is not None:
        elapsed=(sample['monotonic_ns']-previous['monotonic_ns'])/1e9
        if not 0<elapsed<=1:reasons.append('monitor_gap_or_clock_error')
        if previous['available_bytes']-sample['available_bytes']>=approval.rapid_drop_bytes:
            reasons.append('rapid_available_memory_drop')
    return reasons
