"""Text-first preparation entry points; deliberately no executable launcher."""
from tools.model_free_trial.candidate import isolated_settings, prepare as prepare_model_free
from tools.model_free_trial.preflight import ServerBudget, TEXT_PROFILE


def settings(original, port):
    return isolated_settings(original, port, audio_enabled=False)


def unmeasured_budget():
    return ServerBudget(profile=TEXT_PROFILE)


async def prepare(original, port, budget, observe, factory, **options):
    # Keep the first trial readiness-only; POST chat is blocked by that wrapper.
    return await prepare_model_free(original, port, budget, observe, factory,
                                    audio_enabled=False, **options)
