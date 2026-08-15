import asyncio
import unittest
from unittest.mock import patch

from open_llm_vtuber.komugi_fast_path_client import (
    KomugiFastPathClient, KomugiFastPathConfig, _detect_home_topic,
    _detect_home_intent, _detect_weather_ai_intent, _is_home_operation_request, _is_weather_alert_request,
)

SNAPSHOT = {
    "available": True, "snapshot_id": "home-1", "updated_at": "2026-08-14T21:00:00+09:00",
    "groups": {
        "temperature": [{"entity_id": "sensor.env_sensor_room_temperature", "name": "Room Temperature", "state": "28.4", "unit": "°C", "available": True}, {"entity_id": "sensor.shi_wai_wen_du_temperature", "state": "23.6", "unit": "°C", "available": True}],
        "humidity": [{"entity_id": "sensor.env_sensor_room_humidity", "name": "Room Humidity", "state": "59", "unit": "%", "available": True}, {"entity_id": "sensor.shi_wai_wen_du_humidity", "state": "93", "unit": "%", "available": True}],
        "lighting": [{"entity_id": "switch.qin_shi_raito", "name": "寝室ライト", "state": "unknown", "available": False}],
        "climate": [{"entity_id": "climate.qin_shi_eakon", "name": "寝室エアコン", "state": "fan_only", "available": True}, {"entity_id": "climate.rihinkueakon", "name": "リビングエアコン", "state": "off", "available": True}],
        "media_player": [],
    },
}


class Response:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): return None
    def json(self): return self.payload


class HomeRoutingTest(unittest.TestCase):
    def test_home_topics(self):
        cases = {"家の状態は？": "overview", "家の様子は？": "overview", "室温は？": "temperature", "部屋暑い？": "temperature", "部屋の湿度は？": "humidity", "電気ついてる？": "lights", "エアコンついてる？": "climate", "テレビついてる？": "media"}
        for text, topic in cases.items():
            with self.subTest(text=text): self.assertEqual(_detect_home_topic(text), topic)

    def test_weather_and_disaster_do_not_route_home(self):
        for text in ("今日暑い？", "明日暑い？", "今日の湿度は？", "雷注意報ある？", "この辺大丈夫？", "地震あった？", "家大丈夫？"):
            with self.subTest(text=text): self.assertIsNone(_detect_home_topic(text))
        self.assertEqual(_detect_weather_ai_intent("今日暑い？"), "weather_heat")
        self.assertEqual(_detect_weather_ai_intent("明日暑い？"), "weather_tomorrow")
        self.assertEqual(_detect_weather_ai_intent("今日の湿度は？"), "weather_today")
        self.assertTrue(_is_weather_alert_request("雷注意報ある？"))

    def test_indoor_and_outdoor_measurement_scopes(self):
        cases = {
            "室温は？": ("temperature", "indoor"),
            "屋外温度は？": ("temperature", "outdoor"),
            "外気温は？": ("temperature", "outdoor"),
            "部屋の湿度は？": ("humidity", "indoor"),
            "外の湿度は？": ("humidity", "outdoor"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text): self.assertEqual(_detect_home_intent(text), expected)
        self.assertEqual(_detect_weather_ai_intent("今日の気温は？"), "weather_today")
        self.assertEqual(_detect_weather_ai_intent("明日の気温は？"), "weather_tomorrow")
        self.assertEqual(_detect_weather_ai_intent("今日の湿度は？"), "weather_today")

    @patch("open_llm_vtuber.komugi_fast_path_client.requests.post")
    @patch("open_llm_vtuber.komugi_fast_path_client.requests.get")
    def test_outdoor_answers_and_event_scope_use_same_snapshot(self, get, post):
        get.return_value = Response(SNAPSHOT); post.return_value = Response({"ok": True})
        client = KomugiFastPathClient(KomugiFastPathConfig(enabled=True))
        temperature = asyncio.run(client.quick_response("屋外温度は？"))
        self.assertIn("外気温は23.6度", temperature.response)
        event = post.call_args.kwargs["json"]
        self.assertEqual((event["topic"], event["scope"]), ("temperature", "outdoor"))
        self.assertIs(event["home_snapshot"], SNAPSHOT)
        humidity = asyncio.run(client.quick_response("外の湿度は？"))
        self.assertIn("屋外の湿度は93%", humidity.response)
        self.assertEqual(post.call_args.kwargs["json"]["scope"], "outdoor")

    def test_operations_are_read_only_denials(self):
        for text in ("電気つけて", "電気消して", "エアコンつけて", "26度にして", "テレビつけて"):
            with self.subTest(text=text):
                self.assertTrue(_is_home_operation_request(text)); self.assertIsNone(_detect_home_topic(text))

    @patch("open_llm_vtuber.komugi_fast_path_client.requests.post")
    @patch("open_llm_vtuber.komugi_fast_path_client.requests.get")
    def test_answer_and_ui_event_share_one_snapshot(self, get, post):
        get.return_value = Response(SNAPSHOT); post.return_value = Response({"ok": True})
        result = asyncio.run(KomugiFastPathClient(KomugiFastPathConfig(enabled=True)).quick_response("室温は？"))
        self.assertTrue(result.handled); self.assertEqual(result.intent, "home_temperature"); self.assertIn("28.4", result.response)
        self.assertEqual(get.call_count, 1)
        event = post.call_args.kwargs["json"]
        self.assertEqual((event["view"], event["topic"]), ("home", "temperature")); self.assertIs(event["home_snapshot"], SNAPSHOT)
        self.assertNotIn("Authorization", event)

    @patch("open_llm_vtuber.komugi_fast_path_client.requests.post")
    @patch("open_llm_vtuber.komugi_fast_path_client.requests.get")
    def test_operation_never_calls_command_center_or_service(self, get, post):
        result = asyncio.run(KomugiFastPathClient(KomugiFastPathConfig(enabled=True)).quick_response("電気つけて"))
        self.assertTrue(result.handled); self.assertEqual(result.intent, "home_read_only_operation")
        get.assert_not_called(); post.assert_not_called()

    @patch("open_llm_vtuber.komugi_fast_path_client.requests.post")
    @patch("open_llm_vtuber.komugi_fast_path_client.requests.get")
    def test_unavailable_snapshot_is_shared(self, get, post):
        snapshot = {"available": False, "snapshot_id": "down-1", "groups": {}}
        get.return_value = Response(snapshot); post.return_value = Response({"ok": True})
        result = asyncio.run(KomugiFastPathClient(KomugiFastPathConfig(enabled=True)).quick_response("家の状態は？"))
        self.assertIn("接続できません", result.response); self.assertIs(post.call_args.kwargs["json"]["home_snapshot"], snapshot)

    @patch("open_llm_vtuber.komugi_fast_path_client.requests.post")
    @patch("open_llm_vtuber.komugi_fast_path_client.requests.get")
    def test_topic_answers_use_real_groups(self, get, post):
        get.return_value = Response(SNAPSHOT); post.return_value = Response({"ok": True})
        client = KomugiFastPathClient(KomugiFastPathConfig(enabled=True))
        cases = {
            "家の状態は？": ("home_overview", "28.4"),
            "部屋の湿度は？": ("home_humidity", "59%"),
            "電気ついてる？": ("home_lights", "取得できません"),
            "エアコンついてる？": ("home_climate", "送風"),
            "テレビついてる？": ("home_media", "取得できません"),
        }
        for text, (intent, answer_part) in cases.items():
            with self.subTest(text=text):
                result = asyncio.run(client.quick_response(text))
                self.assertEqual(result.intent, intent); self.assertIn(answer_part, result.response)

    @patch("open_llm_vtuber.komugi_fast_path_client.requests.post")
    @patch("open_llm_vtuber.komugi_fast_path_client.requests.get")
    def test_climate_answer_uses_backend_normalization(self, get, post):
        snapshot = {**SNAPSHOT, "groups": {**SNAPSHOT["groups"], "climate": [{
            "entity_id": "climate.qin_shi_eakon", "state": "fan_only", "available": True,
            "display_state": "冷房", "effective_mode": "cool", "target_temperature": 26,
        }]}}
        get.return_value = Response(snapshot); post.return_value = Response({"ok": True})
        result = asyncio.run(KomugiFastPathClient(KomugiFastPathConfig(enabled=True)).quick_response("エアコンついてる？"))
        self.assertIn("26度で冷房", result.response); self.assertNotIn("送風", result.response)


if __name__ == "__main__": unittest.main()
