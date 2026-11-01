"""Preparation only. No CLI/start/listen/uvicorn entry point in this phase."""
from copy import deepcopy
from .preflight import admit_server


def isolated_settings(original, port, *, audio_enabled=True):
    if type(port) is not int or not 1024 <= port <= 65535 or port in (
            12393, 18000, 18081, 18873, 18874, 18875, 18876):
        raise ValueError('trial_port_invalid_or_protected')
    config = deepcopy(original)
    system = config['system_config']
    system.update(host='127.0.0.1', port=port, default_character_config='', enable_proxy=False)
    system['staged_loading'] = dict(enabled=True, text_only=True, asr_mode='disabled', eager_preload=False)
    system['komugi_fast_path'] = dict(enabled=False, performance_record_enabled=False)
    character = config['character_config']
    agent = character['agent_config']
    if agent['conversation_agent_choice'] != 'basic_memory_agent':
        raise ValueError('model_free_agent_not_supported')
    basic = agent['agent_settings']['basic_memory_agent']
    if basic['llm_provider'] != 'ollama_llm':
        raise ValueError('model_free_provider_not_supported')
    basic.update(use_mcpp=False, mcp_enabled_servers=[])
    agent['llm_configs']['ollama_llm'].update(eager_preload=False, unload_at_exit=False)
    if not audio_enabled:
        system['staged_loading'].update(audio_enabled=False, text_requests_enabled=False)
    if audio_enabled and character['tts_config']['tts_model'] != 'voicevox_tts':
        raise ValueError('model_free_tts_not_supported')
    character['vad_config']['vad_model'] = None
    return config


async def prepare(original, port, budget, observe, factory, *, offline=False, clock=None, audio_enabled=True):
    """Admission precedes *any* engine/server factory. Returns an unstarted server.

    The future supervisor must install guard.py before importing the actual server
    and must perform a second fresh admission immediately before creating the Job.
    This helper itself does not start a listener or install global process hooks.
    """
    config = isolated_settings(original, port, audio_enabled=audio_enabled)
    from .preflight import TEXT_PROFILE, PROFILE
    options = dict(offline=offline, profile=PROFILE if audio_enabled else TEXT_PROFILE)
    if clock is not None: options['clock'] = clock
    admission = admit_server(budget, observe(), **options)
    candidate = factory(config)
    from .guard import ReadinessOnly
    candidate.app = ReadinessOnly(candidate.app, text_status=not audio_enabled)
    await candidate.initialize()
    return candidate, admission
