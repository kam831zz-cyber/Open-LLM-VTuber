import atexit
import requests
from loguru import logger
from .openai_compatible_llm import AsyncLLM


class OllamaLLM(AsyncLLM):
    def __init__(
        self,
        model: str,
        base_url: str,
        llm_api_key: str = "z",
        organization_id: str = "z",
        project_id: str = "z",
        temperature: float = 1.0,
        keep_alive: float = -1,
        unload_at_exit: bool = True,
        eager_preload: bool = True,
        staged_controller=None,
    ):
        self.staged_controller = staged_controller
        self._preload_attempted = False
        self.keep_alive = keep_alive
        self.unload_at_exit = unload_at_exit
        self.cleaned = False
        super().__init__(
            model=model,
            base_url=base_url,
            llm_api_key=llm_api_key,
            organization_id=organization_id,
            project_id=project_id,
            temperature=temperature,
        )
        if staged_controller is not None:
            staged_controller.bind_llm(self)
        if eager_preload and staged_controller is None:
            self.preload_model()
        if unload_at_exit and staged_controller is None and self._preload_attempted:
            atexit.register(self.cleanup)

    def preload_model(self):
        if self.staged_controller is not None:
            raise RuntimeError("staged_load_requires_verified_adapter")
        self.preload_succeeded = False
        try:
            response = requests.post(
                self.base_url.rstrip("/").removesuffix("/v1") + "/api/chat",
                json={"model": self.model, "keep_alive": self.keep_alive},
                timeout=(3, 30),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("done") is not True or payload.get("error"):
                raise ValueError("preload_completion_not_verified")
        except Exception as exc:
            # Neither request bodies/URLs nor raw exception messages enter logs.
            logger.error("Ollama preload failed ({})", type(exc).__name__)
            raise RuntimeError("ollama_preload_failed") from None
        self.preload_succeeded = True
        self._preload_attempted = True  # cleanup is not enabled by a failed constructor

    async def chat_completion(self, *args, **kwargs):
        if self.staged_controller is not None:
            self.staged_controller.require_llm_ready()
        async for chunk in super().chat_completion(*args, **kwargs):
            yield chunk

    def __del__(self):
        """Destructor to unload the model"""
        self.cleanup()

    def cleanup(self):
        """Clean up function to unload the model when exitting"""
        if (not getattr(self, "cleaned", True) and self.unload_at_exit
                and self._preload_attempted and self.staged_controller is None):
            logger.info(f"Ollama: Unloading model: {self.model}")
            # Unload the model
            # unloading is just the same as preload, but with keep alive set to 0
            logger.debug(
                requests.post(
                    self.base_url.replace("/v1", "") + "/api/chat",
                    json={
                        "model": self.model,
                        "keep_alive": 0,
                    },
                    timeout=(3, 5),
                )
            )
            self.cleaned = True
