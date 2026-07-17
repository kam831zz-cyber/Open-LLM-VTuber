from __future__ import annotations

import os
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("komugi-weather-ai")

DEFAULT_WEATHER_AI_URL = "http://127.0.0.1:18000/api/komugi/ai/weather"
WEATHER_AI_URL_ENV = "KOMUGI_WEATHER_AI_URL"
TIMEOUT_SECONDS = 6
FACT_KEYS = (
    "umbrella_level",
    "rain_start",
    "rain_end",
    "bring_in_by",
    "heat_level",
    "cold_level",
    "uv_level",
    "wind_level",
    "outing_start",
    "outing_end",
)


def _weather_ai_url() -> str:
    return os.environ.get(WEATHER_AI_URL_ENV, DEFAULT_WEATHER_AI_URL).strip() or DEFAULT_WEATHER_AI_URL


def _as_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _format_facts(facts: Any) -> list[str]:
    if not isinstance(facts, dict):
        return []
    lines = []
    for key in FACT_KEYS:
        value = facts.get(key)
        if value is None or value == "":
            continue
        lines.append(f"{key}: {value}")
    return lines


def _format_payload(data: dict[str, Any]) -> str:
    message = _as_text(data.get("message"))
    if not message:
        return "天気判断の結果が空でした。"

    priority = _as_int(data.get("priority"))
    mood = _as_text(data.get("mood"), "normal")
    expression = _as_text(data.get("expression"), "neutral")
    lines = [
        "Komugi Weather AI:",
        message,
        "",
        f"priority: {priority}",
        f"mood: {mood}",
        f"expression: {expression}",
    ]
    fact_lines = _format_facts(data.get("facts"))
    if fact_lines:
        lines.append("facts:")
        lines.extend(f"- {line}" for line in fact_lines)
    return "\n".join(lines)


def fetch_komugi_weather_advice(url: str | None = None) -> str:
    target_url = (url or _weather_ai_url()).strip() or DEFAULT_WEATHER_AI_URL
    try:
        response = requests.get(target_url, timeout=TIMEOUT_SECONDS)
    except requests.Timeout:
        return "Command Centerの天気判断がタイムアウトしました。通常のWeather MCPを利用するか、少し時間をおいて確認してください。"
    except requests.RequestException:
        return "Command Centerの天気判断に接続できませんでした。通常のWeather MCPを利用するか、少し時間をおいて確認してください。"

    if response.status_code >= 400:
        return "Command Centerの天気判断を取得できませんでした。"

    try:
        data = response.json()
    except ValueError:
        return "Command Centerから受け取った天気情報を読み取れませんでした。"

    if not isinstance(data, dict):
        return "Command Centerから受け取った天気情報を読み取れませんでした。"
    return _format_payload(data)


@mcp.tool()
def get_komugi_weather_advice() -> str:
    """Get short life-oriented weather advice from Home AI Command Center.

    Use this tool first for household current-location questions such as
    today's weather, whether an umbrella is needed, whether laundry can be
    dried outside, heat/cold/UV/wind cautions, and good outing timing. The
    returned message is already prioritized and shortened by Command Center's
    Weather AI and Komugi AI, so answer mainly from that message without adding
    unnecessary numbers or repeating the same content.
    """
    return fetch_komugi_weather_advice()


if __name__ == "__main__":
    mcp.run()
