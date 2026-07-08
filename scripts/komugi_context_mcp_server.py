from __future__ import annotations

from typing import Any

import requests
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("komugi-context")

DEFAULT_CONTEXT_URL = "http://127.0.0.1:8000/api/komugi/context"
TIMEOUT_SECONDS = 12
MAX_ITEMS = 8
MAX_NOTIFICATIONS = 5
MAX_TEXT_LENGTH = 4000


def _as_text(value: Any, fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def _format_rows(rows: list[dict[str, Any]]) -> str:
    parts = []
    for row in rows[:MAX_ITEMS]:
        label = _as_text(row.get("label"), "")
        value = _as_text(row.get("value"), "")
        if label and value:
            parts.append(f"{label}: {value}")
        elif value:
            parts.append(value)
    return " / ".join(parts)


def _format_summary(data: dict[str, Any]) -> list[str]:
    summary = data.get("summary") or {}
    cards = summary.get("cards") or []
    lines = []
    for card in cards[:MAX_ITEMS]:
        title = _as_text(card.get("title"), card.get("id", "summary"))
        status = _as_text(card.get("status"))
        rows = _format_rows(card.get("rows") or [])
        updated = _as_text(card.get("updated_at"), "")
        line = f"- {title}: status={status}"
        if rows:
            line += f"; {rows}"
        if updated:
            line += f"; updated={updated}"
        lines.append(line)
    return lines


def _format_garbage_calendar(data: dict[str, Any]) -> list[str]:
    summary = data.get("summary") or {}
    cards = summary.get("cards") or []
    for card in cards:
        title = _as_text(card.get("title"), "")
        card_id = _as_text(card.get("id"), "")
        haystack = f"{title} {card_id}".lower()
        if not any(word in haystack for word in ["ゴミ", "ごみ", "garbage"]):
            continue

        values: dict[str, str] = {}
        for row in card.get("rows") or []:
            label = _as_text(row.get("label"), "")
            value = _as_text(row.get("value"), "")
            if label and value:
                values[label] = value

        lines = []
        if values.get("今日"):
            lines.append(f"- today/今日: {values['今日']}")
        if values.get("明日"):
            lines.append(f"- tomorrow/明日: {values['明日']}")
        if values.get("今週"):
            lines.append(f"- this_week/今週: {values['今週']}")
        if values.get("メモ"):
            lines.append(f"- memo/メモ: {values['メモ']}")
        return lines
    return []


def _format_apps(data: dict[str, Any]) -> list[str]:
    apps = ((data.get("apps") or {}).get("apps")) or []
    lines = []
    for app in apps[:MAX_ITEMS]:
        name = _as_text(app.get("name"), app.get("id", "app"))
        status = _as_text(app.get("statusLabel"), app.get("status"))
        message = _as_text(app.get("message"), "")
        if message:
            lines.append(f"- {name}: {status} ({message})")
        else:
            lines.append(f"- {name}: {status}")
    return lines


def _format_notifications(data: dict[str, Any]) -> list[str]:
    notifications = data.get("notifications") or []
    lines = []
    for notification in notifications[:MAX_NOTIFICATIONS]:
        title = _as_text(notification.get("title"), "notification")
        level = _as_text(notification.get("level"))
        message = _as_text(notification.get("message"), "")
        read = notification.get("read")
        read_state = "read" if read else "unread"
        lines.append(f"- {title}: level={level}, {read_state}; {message}")
    return lines


def _format_system(data: dict[str, Any]) -> list[str]:
    system = data.get("system") or {}
    capabilities = data.get("capabilities") or {}
    lines = [
        f"- service: {_as_text(system.get('service'))}",
        f"- status: {_as_text(system.get('status'))}",
        f"- timestamp: {_as_text(system.get('timestamp'))}",
        f"- apps_registered: {_as_text(system.get('apps_registered'))}",
        (
            "- capabilities: read_only="
            f"{capabilities.get('read_only', True)}, "
            f"home_assistant_control={capabilities.get('home_assistant_control', False)}, "
            f"device_control={capabilities.get('device_control', False)}"
        ),
    ]
    integrations = system.get("integrations") or {}
    if integrations:
        parts = [f"{key}={value}" for key, value in integrations.items()]
        lines.append("- integrations: " + ", ".join(parts))
    return lines


def _format_bridge(data: dict[str, Any]) -> list[str]:
    bridge = data.get("bridge") or {}
    lines = []
    for key, value in list(bridge.items())[:MAX_ITEMS]:
        label = _as_text(value.get("label"), key)
        status = _as_text(value.get("status"))
        message = _as_text(value.get("message"), "")
        if message:
            lines.append(f"- {label}: {status} ({message})")
        else:
            lines.append(f"- {label}: {status}")
    return lines


def _format_response_rules(data: dict[str, Any]) -> list[str]:
    rules = data.get("response_rules") or []
    lines = [f"- {rule}" for rule in rules[:MAX_ITEMS]]
    lines.append("- Home Assistant and device control are not available from this tool.")
    lines.append("- Do not infer unavailable household state. Say when the API does not provide it.")
    return lines


def _section(title: str, lines: list[str]) -> str:
    if not lines:
        return f"{title}:\n- no data"
    return title + ":\n" + "\n".join(lines)


def _format_context(data: dict[str, Any]) -> str:
    generated_at = _as_text(data.get("generated_at"))
    sections = [
        f"Home AI Command Center context generated_at={generated_at}",
        _section("summary", _format_summary(data)),
        _section("garbage_calendar", _format_garbage_calendar(data)),
        _section("notifications", _format_notifications(data)),
        _section("apps", _format_apps(data)),
        _section("system", _format_system(data)),
        _section("bridge", _format_bridge(data)),
        _section("response_rules", _format_response_rules(data)),
    ]
    text = "\n\n".join(sections)
    if len(text) > MAX_TEXT_LENGTH:
        return text[:MAX_TEXT_LENGTH].rstrip() + "\n...(truncated)"
    return text


def fetch_komugi_context(url: str = DEFAULT_CONTEXT_URL) -> str:
    """Fetch and compact Home AI Command Center context for Komugi."""
    try:
        response = requests.get(url, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return (
            "Home AI Command Center context is currently unavailable. "
            f"Reason: {exc}. Continue the conversation without assuming household state. "
            "Home Assistant and device control are not available from this tool."
        )

    if not isinstance(data, dict):
        return (
            "Home AI Command Center returned an unexpected response. "
            "Continue without assuming household state. "
            "Home Assistant and device control are not available from this tool."
        )

    return _format_context(data)


@mcp.tool()
def get_komugi_context() -> str:
    """Get a compact read-only status summary from Home AI Command Center.

    Always use this tool first when the user asks about garbage collection
    (including today/tomorrow), notifications, weather, Command Center status,
    registered home apps, bridge status, connection status, VOICEVOX, Ollama,
    Japan Monitor, or what household/system state is currently known.
    Answer from the returned context when it contains the requested fact.
    Do not tell the user to use get_komugi_context; call this tool yourself
    before answering those household questions.
    Do not invent garbage weekday rules or household state that is absent from
    the context. The tool is read-only and cannot operate Home Assistant or
    devices.
    """
    return fetch_komugi_context()


if __name__ == "__main__":
    mcp.run()
