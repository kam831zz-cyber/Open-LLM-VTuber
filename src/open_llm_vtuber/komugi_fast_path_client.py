import asyncio
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import requests
from loguru import logger


@dataclass
class KomugiFastPathConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:18000"
    timeout_seconds: float = 1.5
    performance_record_enabled: bool = True
    ui_event_enabled: bool = True


@dataclass
class KomugiFastPathResult:
    handled: bool = False
    response: str | None = None
    intent: str | None = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cached: bool | None = None
    stale: bool | None = None
    timing: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    http_roundtrip_ms: int | None = None


class KomugiFastPathClient:
    def __init__(self, config: KomugiFastPathConfig):
        self.config = config

    async def quick_response(self, text: str) -> KomugiFastPathResult:
        start = time.perf_counter()
        request_id = str(uuid.uuid4())
        if not self.config.enabled:
            return KomugiFastPathResult(handled=False, request_id=request_id)

        weather_intent = _detect_weather_ai_intent(text)
        if weather_intent:
            weather_result = await self._komugi_weather_ai_response(weather_intent, request_id)
            if weather_result.handled:
                if weather_intent in {"weather_today", "weather_tomorrow", "weather_weekly"}:
                    self.schedule_ui_event(
                        "weather",
                        period=_weather_period_for_intent(weather_intent),
                    )
                return weather_result
            logger.warning(
                f"Komugi Weather AI fallback intent={weather_intent} error={weather_result.error}"
            )
            if weather_intent not in {"weather_today", "weather_tomorrow", "weather_weekly"}:
                weather_result.handled = True
                weather_result.response = weather_result.response or _jp(r"\u5929\u6c17\u60c5\u5831\u3092\u78ba\u8a8d\u3067\u304d\u307e\u305b\u3093\u3067\u3057\u305f\u3002")
                return weather_result

        weather_fallback_result = weather_result if weather_intent in {"weather_today", "weather_tomorrow", "weather_weekly"} and 'weather_result' in locals() else None
        if _should_skip_weather_fast_path(text):
            return KomugiFastPathResult(
                handled=False,
                request_id=request_id,
                http_roundtrip_ms=_elapsed_ms(start),
            )

        url = self._url("/api/komugi/quick-response")
        try:
            response = await asyncio.to_thread(
                requests.get,
                url,
                params={"text": text},
                timeout=(
                    min(0.5, max(0.1, self.config.timeout_seconds)),
                    max(0.2, self.config.timeout_seconds),
                ),
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning(f"Komugi Fast Path unavailable: {type(exc).__name__}")
            if weather_fallback_result is not None:
                weather_fallback_result.handled = True
                weather_fallback_result.response = weather_fallback_result.response or _jp(r"\u5929\u6c17\u60c5\u5831\u3092\u78ba\u8a8d\u3067\u304d\u307e\u305b\u3093\u3067\u3057\u305f\u3002")
                weather_fallback_result.http_roundtrip_ms = _elapsed_ms(start)
                self.schedule_ui_event(
                    "weather",
                    period=_weather_period_for_intent(weather_intent),
                )
                return weather_fallback_result
            return KomugiFastPathResult(
                handled=False,
                request_id=request_id,
                error="quick_response_failed",
                http_roundtrip_ms=_elapsed_ms(start),
            )

        result = self._parse_quick_response(data, request_id)
        result.http_roundtrip_ms = _elapsed_ms(start)
        if weather_fallback_result is not None and not result.handled:
            weather_fallback_result.handled = True
            weather_fallback_result.response = weather_fallback_result.response or _jp(r"\u5929\u6c17\u60c5\u5831\u3092\u78ba\u8a8d\u3067\u304d\u307e\u305b\u3093\u3067\u3057\u305f\u3002")
            weather_fallback_result.http_roundtrip_ms = result.http_roundtrip_ms
            self.schedule_ui_event(
                "weather",
                period=_weather_period_for_intent(weather_intent),
            )
            return weather_fallback_result
        return result

    async def _komugi_weather_ai_response(
        self,
        intent: str,
        request_id: str,
    ) -> KomugiFastPathResult:
        start = time.perf_counter()
        try:
            response = await asyncio.to_thread(
                requests.get,
                self._url("/api/komugi/ai/weather"),
                params={"period": _weather_period_for_intent(intent)},
                timeout=(0.5, min(8.0, max(5.0, self.config.timeout_seconds))),
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning(f"Komugi Weather AI unavailable: {type(exc).__name__}")
            return KomugiFastPathResult(
                handled=False,
                intent=intent,
                request_id=request_id,
                response=_jp(r"\u5929\u6c17\u60c5\u5831\u3092\u78ba\u8a8d\u3067\u304d\u307e\u305b\u3093\u3067\u3057\u305f\u3002"),
                error="komugi_weather_ai_failed",
                http_roundtrip_ms=_elapsed_ms(start),
            )

        if not isinstance(data, dict):
            return KomugiFastPathResult(
                handled=False,
                intent=intent,
                request_id=request_id,
                response=_jp(r"\u5929\u6c17\u60c5\u5831\u3092\u78ba\u8a8d\u3067\u304d\u307e\u305b\u3093\u3067\u3057\u305f\u3002"),
                error="invalid_komugi_weather_ai_response",
                http_roundtrip_ms=_elapsed_ms(start),
            )

        message = str(data.get("message") or "").strip()
        if not message:
            return KomugiFastPathResult(
                handled=False,
                intent=intent,
                request_id=request_id,
                response=_jp(r"\u5929\u6c17\u60c5\u5831\u3092\u78ba\u8a8d\u3067\u304d\u307e\u305b\u3093\u3067\u3057\u305f\u3002"),
                error="empty_komugi_weather_ai_message",
                http_roundtrip_ms=_elapsed_ms(start),
            )

        return KomugiFastPathResult(
            handled=True,
            response=message,
            intent=intent,
            request_id=request_id,
            timing={"komugi_weather_ai": data.get("priority")},
            http_roundtrip_ms=_elapsed_ms(start),
        )

    async def record_performance(self, payload: dict[str, Any]) -> None:
        if not self.config.performance_record_enabled:
            return

        safe_payload = self._sanitize_performance_payload(payload)
        try:
            await asyncio.to_thread(
                requests.post,
                self._url("/api/komugi/performance/record"),
                json=safe_payload,
                timeout=(0.4, 1.2),
            )
        except Exception as exc:
            logger.warning(f"Komugi performance record failed: {type(exc).__name__}")

    def schedule_performance_record(self, payload: dict[str, Any]) -> None:
        if not self.config.performance_record_enabled:
            return
        asyncio.create_task(self.record_performance(payload))

    async def send_ui_event(self, view: str, period: str | None = None) -> None:
        if not self.config.ui_event_enabled:
            return
        await post_komugi_ui_event(self.config.base_url, view, period)

    def schedule_ui_event(self, view: str, period: str | None = None) -> None:
        if not self.config.ui_event_enabled:
            return
        asyncio.create_task(self.send_ui_event(view, period))

    def _parse_quick_response(
        self,
        data: Any,
        fallback_request_id: str,
    ) -> KomugiFastPathResult:
        if not isinstance(data, dict):
            return KomugiFastPathResult(
                handled=False,
                request_id=fallback_request_id,
                error="invalid_quick_response",
            )

        request_id = str(data.get("request_id") or fallback_request_id).strip()
        if not request_id or len(request_id) > 80:
            request_id = fallback_request_id

        handled = bool(data.get("handled"))
        response_text = data.get("response")
        response = str(response_text).strip() if response_text is not None else ""
        if handled and not response:
            return KomugiFastPathResult(
                handled=False,
                request_id=request_id,
                error="empty_fast_path_response",
            )

        return KomugiFastPathResult(
            handled=handled,
            response=response if handled else None,
            intent=str(data.get("intent") or "").strip() or None,
            request_id=request_id,
            cached=_optional_bool(data.get("cached", data.get("cache_hit"))),
            stale=_optional_bool(data.get("stale")),
            timing=data.get("timing") if isinstance(data.get("timing"), dict) else {},
        )

    def _sanitize_performance_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = {
            "request_id": str(payload.get("request_id") or uuid.uuid4())[:80],
            "route": str(payload.get("route") or "ollama"),
            "error": payload.get("error"),
        }
        for key in (
            "ollama_first_token_ms",
            "ollama_total_ms",
            "voicevox_generation_ms",
            "audio_start_ms",
            "total_ms",
        ):
            result[key] = _safe_ms(payload.get(key))
        if result["error"] is not None:
            result["error"] = str(result["error"])[:160]
        return result

    def _url(self, path: str) -> str:
        base = self.config.base_url.rstrip("/") + "/"
        return urljoin(base, path.lstrip("/"))


def _jp(escaped: str) -> str:
    return escaped.encode("ascii").decode("unicode_escape")


async def post_komugi_ui_event(
    base_url: str,
    view: str,
    period: str | None = None,
) -> None:
    payload: dict[str, Any] = {"view": view}
    if period:
        payload["period"] = period
    try:
        await asyncio.to_thread(
            requests.post,
            _join_url(base_url, "/api/komugi/ui/event"),
            json=payload,
            timeout=(0.3, 1.0),
        )
    except Exception as exc:
        logger.warning(f"Komugi UI event post failed: {type(exc).__name__}")


def _join_url(base_url: str, path: str) -> str:
    base = str(base_url or "http://127.0.0.1:18000").rstrip("/") + "/"
    return urljoin(base, path.lstrip("/"))


def _weather_period_for_intent(intent: str) -> str:
    if intent == "weather_tomorrow":
        return "tomorrow"
    if intent == "weather_weekly":
        return "weekly"
    return "today"


def _detect_weather_ai_intent(text: str) -> str | None:
    normalized = "".join(str(text or "").split())
    if not normalized:
        return None

    detail_or_location_markers = tuple(
        _jp(value)
        for value in (
            r"\u6771\u4eac",  # ??
            r"\u5927\u962a",  # ??
            r"\u5225\u5730\u57df",  # ???
            r"\u8a73\u3057\u304f",  # ???
            r"\u8a73\u7d30",  # ??
            r"\u964d\u6c34\u78ba\u7387",  # ????
            r"\u6700\u9ad8\u6c17\u6e29",  # ????
            r"\u6700\u4f4e\u6c17\u6e29",  # ????
            r"\u6c17\u6e29\u306f",  # ???
        )
    )
    if any(marker in normalized for marker in detail_or_location_markers):
        return None

    if _jp(r"\u5929\u6c17") in normalized and any(
        marker in normalized
        for marker in (
            _jp(r"\u9031\u9593"),  # weekly
            _jp(r"\u4eca\u9031"),  # this week
            _jp(r"\u4e00\u9031\u9593"),  # one week
        )
    ):
        return "weather_weekly"

    if _jp(r"\u5929\u6c17") in normalized and _jp(r"\u660e\u65e5") in normalized:
        return "weather_tomorrow"

    if _jp(r"\u5098") in normalized and any(
        marker in normalized
        for marker in (
            _jp(r"\u5fc5\u8981"),  # ??
            _jp(r"\u3044\u308b"),  # ??
            _jp(r"\u8981\u308b"),  # ??
            _jp(r"\u6301"),  # ?
        )
    ):
        return "weather_umbrella"

    if _jp(r"\u6d17\u6fef") in normalized and any(
        marker in normalized
        for marker in (
            _jp(r"\u5916"),  # ?
            _jp(r"\u5e72\u305b"),  # ??
            _jp(r"\u5e72\u3059"),  # ??
            _jp(r"\u5e72\u3057"),  # ??
        )
    ):
        return "weather_laundry"

    if _jp(r"\u4eca\u65e5") in normalized and any(
        marker in normalized
        for marker in (
            _jp(r"\u6691\u3044"),  # ??
            _jp(r"\u5bd2\u3044"),  # ??
            _jp(r"\u6691\u3055"),  # ??
            _jp(r"\u5bd2\u3055"),  # ??
        )
    ):
        return "weather_heat"

    if _jp(r"\u5916\u51fa") in normalized and any(
        marker in normalized
        for marker in (
            _jp(r"\u3044\u3064"),  # ??
            _jp(r"\u6642\u9593"),  # ??
            _jp(r"\u3044\u3044"),  # ??
            _jp(r"\u3088\u3044"),  # ??
            _jp(r"\u826f\u3044"),  # ??
            _jp(r"\u5411\u304f"),  # ??
            _jp(r"\u5411\u3044"),  # ??
        )
    ):
        return "weather_outing"

    if _jp(r"\u4eca\u65e5") in normalized and _jp(r"\u5929\u6c17") in normalized:
        if any(
            marker in normalized
            for marker in (
                _jp(r"\u670d\u88c5"),  # ??
                _jp(r"\u8003\u3048\u3066"),  # ???
                _jp(r"\u5408\u308f\u305b"),  # ???
                _jp(r"\u5408\u3046"),  # ??
            )
        ):
            return None
        return "weather_today"

    return None

def _should_skip_weather_fast_path(text: str) -> bool:
    normalized = "".join(str(text or "").split())
    if not normalized:
        return False

    weather_markers = (
        _jp(r"\u5929\u6c17"),  # weather
        _jp(r"\u964d\u6c34\u78ba\u7387"),  # precipitation probability
        _jp(r"\u6700\u9ad8\u6c17\u6e29"),  # high temperature
        _jp(r"\u6700\u4f4e\u6c17\u6e29"),  # low temperature
    )
    if not any(marker in normalized for marker in weather_markers):
        return False

    skip_markers = (
        _jp(r"\u660e\u65e5"),  # tomorrow
        _jp(r"\u9031\u9593"),  # weekly
        _jp(r"\u4eca\u9031"),  # this week
        _jp(r"\u6771\u4eac"),  # Tokyo
        _jp(r"\u5927\u962a"),  # Osaka
        _jp(r"\u5225\u5730\u57df"),  # other area
        _jp(r"\u8a73\u3057\u304f"),  # in detail
        _jp(r"\u8a73\u7d30"),  # details
        _jp(r"\u964d\u6c34\u78ba\u7387"),  # precipitation probability
        _jp(r"\u6700\u9ad8\u6c17\u6e29"),  # high temperature
        _jp(r"\u6700\u4f4e\u6c17\u6e29"),  # low temperature
        _jp(r"\u6c17\u6e29\u306f"),  # temperature is
    )
    return any(marker in normalized for marker in skip_markers)

def _elapsed_ms(start: float) -> int:
    return max(0, round((time.perf_counter() - start) * 1000))


def _safe_ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or number > 600000:
        return None
    return number


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)
