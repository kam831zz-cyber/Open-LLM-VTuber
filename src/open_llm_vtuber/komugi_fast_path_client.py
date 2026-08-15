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

        if _is_weather_alert_request(text):
            return KomugiFastPathResult(
                handled=False,
                request_id=request_id,
                http_roundtrip_ms=_elapsed_ms(start),
            )

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
        if _is_home_operation_request(text):
            return KomugiFastPathResult(
                handled=True,
                response=_jp(r"\u4eca\u306f\u5bb6\u96fb\u306e\u72b6\u614b\u78ba\u8a8d\u3060\u3051\u5bfe\u5fdc\u3057\u3066\u3044\u307e\u3059\u3002"),
                intent="home_read_only_operation",
                request_id=request_id,
                http_roundtrip_ms=_elapsed_ms(start),
            )

        home_topic, home_scope = _detect_home_intent(text)
        if home_topic:
            return await self._komugi_home_response(home_topic, request_id, home_scope)

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

    async def _komugi_home_response(
        self,
        topic: str,
        request_id: str,
        scope: str | None = None,
    ) -> KomugiFastPathResult:
        start = time.perf_counter()
        try:
            response = await asyncio.to_thread(
                requests.get,
                self._url("/api/komugi/home"),
                timeout=(0.5, min(8.0, max(3.0, self.config.timeout_seconds))),
            )
            response.raise_for_status()
            snapshot = response.json()
        except Exception as exc:
            logger.warning(f"Komugi Home unavailable: {type(exc).__name__}")
            snapshot = {"available": False, "groups": {}, "error": "home_assistant_unavailable"}

        if not isinstance(snapshot, dict):
            snapshot = {"available": False, "groups": {}, "error": "invalid_home_snapshot"}

        answer = _build_home_answer(topic, snapshot, scope)
        if self.config.ui_event_enabled:
            await post_komugi_ui_event(
                self.config.base_url,
                "home",
                topic=topic,
                home_snapshot=snapshot,
                scope=scope,
            )
        return KomugiFastPathResult(
            handled=True,
            response=answer,
            intent=f"home_{topic}",
            request_id=request_id,
            timing={"home_snapshot": snapshot.get("snapshot_id"), "home_scope": scope},
            error=None if snapshot.get("available") is True else "home_assistant_unavailable",
            http_roundtrip_ms=_elapsed_ms(start),
        )

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
    topic: str | None = None,
    local_status: dict[str, Any] | None = None,
    home_snapshot: dict[str, Any] | None = None,
    scope: str | None = None,
) -> None:
    payload: dict[str, Any] = {"view": view}
    if period:
        payload["period"] = period
    if topic:
        payload["topic"] = topic
    if local_status is not None:
        payload["local_status"] = local_status
    if home_snapshot is not None:
        payload["home_snapshot"] = home_snapshot
    if scope:
        payload["scope"] = scope
    try:
        response = await asyncio.to_thread(
            requests.post,
            _join_url(base_url, "/api/komugi/ui/event"),
            json=payload,
            timeout=(0.3, 1.0),
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning(
            "Komugi UI event post failed: "
            f"view={view} topic={topic or '-'} error={type(exc).__name__}"
        )


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
    if _is_weather_alert_request(normalized):
        return None

    detail_or_location_markers = tuple(
        _jp(value)
        for value in (
            r"\u6771\u4eac",  # ??
            r"\u5927\u962a",  # ??
            r"\u5225\u5730\u57df",  # ???
            r"\u8a73\u3057\u304f",  # ???
            r"\u8a73\u7d30",  # ??
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

    if any(
        marker in normalized
        for marker in (
            _jp(r"\u964d\u6c34\u78ba\u7387"),
            _jp(r"\u6700\u9ad8\u6c17\u6e29"),
            _jp(r"\u6700\u4f4e\u6c17\u6e29"),
        )
    ):
        return (
            "weather_tomorrow"
            if _jp(r"\u660e\u65e5") in normalized
            else "weather_today"
        )

    day_marker = next(
        (marker for marker in (_jp(r"\u4eca\u65e5"), _jp(r"\u660e\u65e5")) if marker in normalized),
        None,
    )
    if day_marker and any(
        marker in normalized
        for marker in (
            _jp(r"\u6691\u3044"), _jp(r"\u5bd2\u3044"), _jp(r"\u6691\u3055"),
            _jp(r"\u5bd2\u3055"), _jp(r"\u6e7f\u5ea6"),
            _jp(r"\u6c17\u6e29"),
        )
    ):
        if day_marker == _jp(r"\u660e\u65e5"):
            return "weather_tomorrow"
        return "weather_heat" if any(
            marker in normalized
            for marker in (_jp(r"\u6691\u3044"), _jp(r"\u5bd2\u3044"), _jp(r"\u6691\u3055"), _jp(r"\u5bd2\u3055"))
        ) else "weather_today"

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


def _detect_home_intent(text: str) -> tuple[str | None, str | None]:
    normalized = "".join(str(text or "").lower().split())
    if not normalized or _is_home_operation_request(normalized):
        return None, None

    scoped_rules = (
        ("humidity", "outdoor", (r"\u5c4b\u5916\u306e\u6e7f\u5ea6", r"\u5916\u306e\u6e7f\u5ea6", r"\u5916\u6c17\u306e\u6e7f\u5ea6")),
        ("temperature", "outdoor", (r"\u5c4b\u5916\u6e29\u5ea6", r"\u5916\u306e\u6e29\u5ea6", r"\u5916\u4f55\u5ea6", r"\u5916\u6c17\u6e29", r"\u5916\u306f\u4f55\u5ea6")),
        ("humidity", "indoor", (r"\u90e8\u5c4b\u306e\u6e7f\u5ea6", r"\u5ba4\u5185\u306e\u6e7f\u5ea6", r"\u30ea\u30d3\u30f3\u30b0\u306e\u6e7f\u5ea6")),
        ("temperature", "indoor", (r"\u5ba4\u6e29", r"\u90e8\u5c4b\u4f55\u5ea6", r"\u90e8\u5c4b\u306e\u6e29\u5ea6", r"\u30ea\u30d3\u30f3\u30b0\u4f55\u5ea6", r"\u90e8\u5c4b\u6691\u3044", r"\u90e8\u5c4b\u5bd2\u3044")),
    )
    for topic, scope, escaped_markers in scoped_rules:
        if any(_jp(marker) in normalized for marker in escaped_markers):
            return topic, scope

    rules = (
        ("lights", (r"\u96fb\u6c17\u3064\u3044\u3066\u308b", r"\u7167\u660e\u3064\u3044\u3066\u308b", r"\u30ea\u30d3\u30f3\u30b0\u306e\u96fb\u6c17\u3064\u3044\u3066\u308b", r"\u5bdd\u5ba4\u306e\u30e9\u30a4\u30c8\u3064\u3044\u3066\u308b")),
        ("climate", (r"\u30a8\u30a2\u30b3\u30f3\u3064\u3044\u3066\u308b", r"\u5bdd\u5ba4\u306e\u30a8\u30a2\u30b3\u30f3\u3069\u3046", r"\u30ea\u30d3\u30f3\u30b0\u306e\u30a8\u30a2\u30b3\u30f3\u3069\u3046")),
        ("media", (r"\u30c6\u30ec\u30d3\u3064\u3044\u3066\u308b", r"\u30c6\u30ec\u30d3\u3069\u3046")),
        ("overview", (r"\u5bb6\u306e\u72b6\u614b", r"\u5bb6\u306e\u69d8\u5b50", r"\u5bb6\u3069\u3046\u306a\u3063\u3066\u308b", r"homeassistant\u3069\u3046", r"\u30db\u30fc\u30e0\u30a2\u30b7\u30b9\u30bf\u30f3\u30c8\u3069\u3046")),
    )
    for topic, escaped_markers in rules:
        if any(_jp(marker) in normalized for marker in escaped_markers):
            return topic, None
    return None, None


def _detect_home_topic(text: str) -> str | None:
    return _detect_home_intent(text)[0]


def _is_home_operation_request(text: str) -> bool:
    normalized = "".join(str(text or "").lower().split())
    operation_phrases = (
        r"\u96fb\u6c17\u3064\u3051\u3066", r"\u96fb\u6c17\u6d88\u3057\u3066", r"\u30e9\u30a4\u30c8\u6d88\u3057\u3066",
        r"\u30a8\u30a2\u30b3\u30f3\u3064\u3051\u3066", r"\u30a8\u30a2\u30b3\u30f3\u6d88\u3057\u3066", r"26\u5ea6\u306b\u3057\u3066",
        r"\u30c6\u30ec\u30d3\u3064\u3051\u3066", r"\u30c6\u30ec\u30d3\u6d88\u3057\u3066",
    )
    return any(_jp(phrase) in normalized for phrase in operation_phrases)


def _build_home_answer(topic: str, snapshot: dict[str, Any], scope: str | None = None) -> str:
    if snapshot.get("available") is not True:
        return _jp(r"Home Assistant\u306b\u63a5\u7d9a\u3067\u304d\u307e\u305b\u3093\u3002")
    groups = snapshot.get("groups") if isinstance(snapshot.get("groups"), dict) else {}
    temperature_id = "sensor.shi_wai_wen_du_temperature" if scope == "outdoor" else "sensor.env_sensor_room_temperature"
    humidity_id = "sensor.shi_wai_wen_du_humidity" if scope == "outdoor" else "sensor.env_sensor_room_humidity"
    temperature = _entity_by_id(groups.get("temperature"), temperature_id)
    humidity = _entity_by_id(groups.get("humidity"), humidity_id)
    if topic == "temperature":
        label = _jp(r"\u5916\u6c17\u6e29") if scope == "outdoor" else _jp(r"\u5ba4\u6e29")
        return _measurement_answer(temperature, label, f"{label}は取得できません。")
    if topic == "humidity":
        label = _jp(r"\u5c4b\u5916\u306e\u6e7f\u5ea6") if scope == "outdoor" else _jp(r"\u5ba4\u5185\u306e\u6e7f\u5ea6")
        return _measurement_answer(humidity, label, f"{label}は取得できません。")
    if topic == "lights":
        light = _first_entity(groups.get("lighting"))
        if not light or light.get("available") is not True:
            return _jp(r"\u5bdd\u5ba4\u306e\u30e9\u30a4\u30c8\u306e\u72b6\u614b\u306f\u53d6\u5f97\u3067\u304d\u307e\u305b\u3093\u3002")
        return f"{light.get('name') or light.get('entity_id')}は{_home_state_label(light.get('state'))}です。"
    if topic == "climate":
        items = [item for item in _entities(groups.get("climate")) if item.get("available") is True]
        if not items:
            return _jp(r"\u30a8\u30a2\u30b3\u30f3\u306e\u72b6\u614b\u306f\u53d6\u5f97\u3067\u304d\u307e\u305b\u3093\u3002")
        return "、".join(_climate_answer_part(item) for item in items) + "です。"
    if topic == "media":
        media = _first_available(groups.get("media_player"))
        if not media:
            return _jp(r"Home Assistant\u304b\u3089\u30c6\u30ec\u30d3\u306e\u72b6\u614b\u306f\u53d6\u5f97\u3067\u304d\u307e\u305b\u3093\u3002")
        return f"{media.get('name') or media.get('entity_id')}は{_home_state_label(media.get('state'))}です。"
    parts = [_jp(r"Home Assistant\u306f\u6b63\u5e38\u3067\u3059\u3002")]
    if temperature:
        parts.append(_measurement_answer(temperature, _jp(r"\u5ba4\u6e29"), ""))
    if humidity:
        parts.append(_measurement_answer(humidity, _jp(r"\u6e7f\u5ea6"), ""))
    return "".join(parts)


def _entities(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _first_entity(value: Any) -> dict[str, Any] | None:
    items = _entities(value)
    return items[0] if items else None


def _first_available(value: Any) -> dict[str, Any] | None:
    return next((item for item in _entities(value) if item.get("available") is True), None)


def _entity_by_id(value: Any, entity_id: str) -> dict[str, Any] | None:
    return next((item for item in _entities(value) if item.get("entity_id") == entity_id and item.get("available") is True), None)


def _measurement_answer(entity: dict[str, Any] | None, label: str, fallback: str) -> str:
    if not entity:
        return fallback
    try:
        value = f"{float(entity.get('state')):.1f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        value = str(entity.get("state") or "")
    unit = str(entity.get("unit") or "").replace("°C", _jp(r"\u5ea6"))
    return f"{label}は{value}{unit}です。"


def _home_state_label(value: Any) -> str:
    state = str(value or "unknown").lower()
    return {
        "on": _jp(r"\u30aa\u30f3"),
        "off": _jp(r"\u30aa\u30d5"),
        "fan_only": _jp(r"\u9001\u98a8"),
        "heat": _jp(r"\u6696\u623f"),
        "cool": _jp(r"\u51b7\u623f"),
        "dry": _jp(r"\u9664\u6e7f"),
    }.get(state, str(value or _jp(r"\u4e0d\u660e")))


def _climate_answer_part(entity: dict[str, Any]) -> str:
    names = {
        "climate.qin_shi_eakon": _jp(r"\u5bdd\u5ba4\u306e\u30a8\u30a2\u30b3\u30f3"),
        "climate.rihinkueakon": _jp(r"\u30ea\u30d3\u30f3\u30b0\u306e\u30a8\u30a2\u30b3\u30f3"),
    }
    name = names.get(str(entity.get("entity_id"))) or entity.get("name") or entity.get("entity_id")
    state = str(entity.get("display_state") or _home_state_label(entity.get("state")))
    target = entity.get("target_temperature")
    if entity.get("effective_mode") != "off" and target is not None:
        try:
            target_text = f"{float(target):g}"
        except (TypeError, ValueError):
            target_text = str(target)
        return f"{name}は{target_text}度で{state}"
    return f"{name}は{state}"


def _is_weather_alert_request(text: str) -> bool:
    normalized = "".join(str(text or "").lower().split())
    if not normalized:
        return False

    educational_markers = tuple(
        _jp(value)
        for value in (
            r"\u4ed5\u7d44\u307f",
            r"\u3069\u3046\u3057\u3066",
            r"\u306a\u305c",
            r"\u7406\u7531",
            r"\u3068\u306f",
            r"\u6559\u3048\u3066",
        )
    )
    if any(marker in normalized for marker in educational_markers):
        return False

    alert_markers = (_jp(r"\u8b66\u5831"), _jp(r"\u6ce8\u610f\u5831"))
    if any(marker in normalized for marker in alert_markers):
        return True

    weather_information = _jp(r"\u6c17\u8c61\u60c5\u5831")
    status_markers = tuple(
        _jp(value)
        for value in (
            r"\u3042\u308b",
            r"\u51fa\u3066",
            r"\u767a\u8868",
            r"\u72b6\u6cc1",
            r"\u78ba\u8a8d",
        )
    )
    return weather_information in normalized and any(
        marker in normalized for marker in status_markers
    )

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
