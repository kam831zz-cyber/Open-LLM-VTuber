from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

import requests
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("weather")

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEZONE = "Asia/Tokyo"

LOCATION_ALIASES = {
    "\u5927\u7530\u539f": {
        "name": "\u5927\u7530\u539f\u5e02",
        "latitude": 36.86667,
        "longitude": 140.03333,
        "country": "\u65e5\u672c",
        "admin1": "\u6803\u6728\u770c",
    },
    "\u5927\u7530\u539f\u5e02": {
        "name": "\u5927\u7530\u539f\u5e02",
        "latitude": 36.86667,
        "longitude": 140.03333,
        "country": "\u65e5\u672c",
        "admin1": "\u6803\u6728\u770c",
    },
    "otawara": {
        "name": "\u5927\u7530\u539f\u5e02",
        "latitude": 36.86667,
        "longitude": 140.03333,
        "country": "\u65e5\u672c",
        "admin1": "\u6803\u6728\u770c",
    },
    "\u90a3\u9808\u5869\u539f": {
        "name": "\u90a3\u9808\u5869\u539f\u5e02",
        "latitude": 36.97952,
        "longitude": 139.99466,
        "country": "\u65e5\u672c",
        "admin1": "\u6803\u6728\u770c",
    },
    "\u90a3\u9808\u5869\u539f\u5e02": {
        "name": "\u90a3\u9808\u5869\u539f\u5e02",
        "latitude": 36.97952,
        "longitude": 139.99466,
        "country": "\u65e5\u672c",
        "admin1": "\u6803\u6728\u770c",
    },
    "nasushiobara": {
        "name": "\u90a3\u9808\u5869\u539f\u5e02",
        "latitude": 36.97952,
        "longitude": 139.99466,
        "country": "\u65e5\u672c",
        "admin1": "\u6803\u6728\u770c",
    },
    "\u6771\u4eac": {
        "name": "\u6771\u4eac",
        "latitude": 35.6895,
        "longitude": 139.6917,
        "country": "\u65e5\u672c",
        "admin1": "\u6771\u4eac\u90fd",
    },
    "\u6771\u4eac\u90fd": {
        "name": "\u6771\u4eac",
        "latitude": 35.6895,
        "longitude": 139.6917,
        "country": "\u65e5\u672c",
        "admin1": "\u6771\u4eac\u90fd",
    },
    "tokyo": {
        "name": "\u6771\u4eac",
        "latitude": 35.6895,
        "longitude": 139.6917,
        "country": "\u65e5\u672c",
        "admin1": "\u6771\u4eac\u90fd",
    },
}

WEATHER_CODES = {
    0: "\u5feb\u6674",
    1: "\u6674\u308c",
    2: "\u4e00\u90e8\u66c7\u308a",
    3: "\u66c7\u308a",
    45: "\u9727",
    48: "\u9727\u6c37",
    51: "\u5f31\u3044\u9727\u96e8",
    53: "\u9727\u96e8",
    55: "\u5f37\u3044\u9727\u96e8",
    56: "\u5f31\u3044\u7740\u6c37\u6027\u306e\u9727\u96e8",
    57: "\u5f37\u3044\u7740\u6c37\u6027\u306e\u9727\u96e8",
    61: "\u5f31\u3044\u96e8",
    63: "\u96e8",
    65: "\u5f37\u3044\u96e8",
    66: "\u5f31\u3044\u7740\u6c37\u6027\u306e\u96e8",
    67: "\u5f37\u3044\u7740\u6c37\u6027\u306e\u96e8",
    71: "\u5f31\u3044\u96ea",
    73: "\u96ea",
    75: "\u5f37\u3044\u96ea",
    77: "\u96ea\u7c92",
    80: "\u5f31\u3044\u306b\u308f\u304b\u96e8",
    81: "\u306b\u308f\u304b\u96e8",
    82: "\u5f37\u3044\u306b\u308f\u304b\u96e8",
    85: "\u5f31\u3044\u306b\u308f\u304b\u96ea",
    86: "\u5f37\u3044\u306b\u308f\u304b\u96ea",
    95: "\u96f7\u96e8",
    96: "\u3072\u3087\u3046\u3092\u4f34\u3046\u96f7\u96e8",
    99: "\u5f37\u3044\u3072\u3087\u3046\u3092\u4f34\u3046\u96f7\u96e8",
}


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    response = requests.get(f"{url}?{urlencode(params)}", timeout=10)
    response.raise_for_status()
    return response.json()


def _find_location(location: str) -> dict[str, Any]:
    alias = LOCATION_ALIASES.get(location) or LOCATION_ALIASES.get(location.lower())
    if alias:
        return alias

    data = _get_json(
        GEOCODING_URL,
        {
            "name": location,
            "count": 5,
            "language": "ja",
            "format": "json",
            "country_code": "JP",
        },
    )
    results = data.get("results") or []
    if not results:
        raise ValueError(f"Location not found: {location}")

    preferred_codes = {"PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLC"}
    for result in results:
        if result.get("feature_code") in preferred_codes:
            return result
    return results[0]


def _target_date(day: str) -> date:
    normalized = (day or "today").strip().lower()
    today = date.today()
    if normalized in {"today", "\u4eca\u65e5", "\u304d\u3087\u3046"}:
        return today
    if normalized in {"tomorrow", "\u660e\u65e5", "\u3042\u3057\u305f"}:
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            "day must be 'today', 'tomorrow', 'today in Japanese', 'tomorrow in Japanese', or YYYY-MM-DD."
        ) from exc


@mcp.tool()
def get_weather_forecast(location: str, day: str = "today") -> str:
    """Get weather forecasts from Open-Meteo.

    Use this tool for tomorrow, weekly, this-week, regional, and numeric weather
    forecast questions, including precipitation probability, high temperature,
    low temperature, and detailed current/future weather. The location can be a
    Japanese city or place name such as Otawara, Nasushiobara, or Tokyo. The day
    can be today, tomorrow, weekly, Japanese today/tomorrow/week, or an ISO date
    like 2026-06-25.

    明日の天気、週間予報、今週の天気、東京など別地域の天気、
    降水確率、最高気温、最低気温などの詳細予報は、
    get_komugi_context ではなく get_weather_forecast を使います。
    """
    if not location or not location.strip():
        return "\u5834\u6240\u304c\u6307\u5b9a\u3055\u308c\u3066\u3044\u307e\u305b\u3093\u3002\u5929\u6c17\u3092\u77e5\u308a\u305f\u3044\u5e02\u533a\u753a\u6751\u540d\u3092\u6307\u5b9a\u3057\u3066\u304f\u3060\u3055\u3044\u3002"

    place = _find_location(location.strip())
    normalized_day = (day or "today").strip().lower()
    is_weekly = normalized_day in {
        "weekly",
        "week",
        "this_week",
        "this week",
        "\u9031\u9593",
        "\u4eca\u9031",
        "\u4e00\u9031\u9593",
    }
    target = None if is_weekly else _target_date(day)
    data = _get_json(
        FORECAST_URL,
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "daily": ",".join(
                [
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_probability_max",
                    "precipitation_sum",
                ]
            ),
            "timezone": TIMEZONE,
            "forecast_days": 7,
        },
    )

    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    resolved_name = place.get("name", location)
    admin = place.get("admin1")
    country = place.get("country")
    resolved = "\u3001".join(part for part in [country, admin, resolved_name] if part)

    if is_weekly:
        if not dates:
            return f"{resolved} \u306e\u9031\u9593\u5929\u6c17\u4e88\u5831\u306f\u53d6\u5f97\u3067\u304d\u307e\u305b\u3093\u3067\u3057\u305f\u3002"
        lines = [f"{resolved} \u306e\u9031\u9593\u5929\u6c17\u4e88\u5831:"]
        for index, forecast_date in enumerate(dates[:7]):
            code = (daily.get("weather_code") or [None] * len(dates))[index]
            weather = WEATHER_CODES.get(code, f"\u5929\u6c17\u30b3\u30fc\u30c9 {code}")
            max_temp = (daily.get("temperature_2m_max") or [None] * len(dates))[index]
            min_temp = (daily.get("temperature_2m_min") or [None] * len(dates))[index]
            precip_prob = (daily.get("precipitation_probability_max") or [None] * len(dates))[index]
            precip_sum = (daily.get("precipitation_sum") or [None] * len(dates))[index]
            lines.append(
                f"{forecast_date}: {weather}\u3002"
                f"\u6700\u9ad8\u6c17\u6e29 {max_temp}\u2103\u3001\u6700\u4f4e\u6c17\u6e29 {min_temp}\u2103\u3002"
                f"\u6700\u5927\u964d\u6c34\u78ba\u7387 {precip_prob}%\u3001\u964d\u6c34\u91cf {precip_sum}mm\u3002"
            )
        lines.append("\u30c7\u30fc\u30bf\u63d0\u4f9b: Open-Meteo\u3002")
        return "\n".join(lines)

    target_str = target.isoformat()
    if target_str not in dates:
        start = dates[0] if dates else "unknown"
        end = dates[-1] if dates else "unknown"
        return f"{target_str} \u306e\u4e88\u5831\u306f\u53d6\u5f97\u3067\u304d\u307e\u305b\u3093\u3067\u3057\u305f\u3002\u53d6\u5f97\u53ef\u80fd\u306a\u7bc4\u56f2\u306f {start} \u304b\u3089 {end} \u3067\u3059\u3002"

    index = dates.index(target_str)
    code = daily.get("weather_code", [None])[index]
    weather = WEATHER_CODES.get(code, f"\u5929\u6c17\u30b3\u30fc\u30c9 {code}")
    max_temp = daily.get("temperature_2m_max", [None])[index]
    min_temp = daily.get("temperature_2m_min", [None])[index]
    precip_prob = daily.get("precipitation_probability_max", [None])[index]
    precip_sum = daily.get("precipitation_sum", [None])[index]

    return (
        f"{resolved} \u306e {target_str} \u306e\u5929\u6c17\u4e88\u5831: {weather}\u3002"
        f"\u6700\u9ad8\u6c17\u6e29 {max_temp}\u2103\u3001\u6700\u4f4e\u6c17\u6e29 {min_temp}\u2103\u3002"
        f"\u6700\u5927\u964d\u6c34\u78ba\u7387 {precip_prob}%\u3001\u964d\u6c34\u91cf {precip_sum}mm\u3002"
        "\u30c7\u30fc\u30bf\u63d0\u4f9b: Open-Meteo\u3002"
    )


if __name__ == "__main__":
    mcp.run()