from typing import (
    AsyncIterator,
    List,
    Dict,
    Any,
    Callable,
    Literal,
    Union,
    Optional,
)
import asyncio
import uuid
import re
from urllib.parse import urljoin
from loguru import logger
import requests
from .agent_interface import AgentInterface
from ..output_types import SentenceOutput, DisplayText
from ..stateless_llm.stateless_llm_interface import StatelessLLMInterface
from ..stateless_llm.claude_llm import AsyncLLM as ClaudeAsyncLLM
from ..stateless_llm.openai_compatible_llm import AsyncLLM as OpenAICompatibleAsyncLLM
from ...chat_history_manager import get_history
from ..transformers import (
    sentence_divider,
    actions_extractor,
    tts_filter,
    display_processor,
)
from ...config_manager import TTSPreprocessorConfig
from ..input_types import BatchInput, TextSource
from prompts import prompt_loader
from ...mcpp.tool_manager import ToolManager
from ...mcpp.json_detector import StreamJSONDetector
from ...mcpp.types import ToolCallObject
from ...mcpp.tool_executor import ToolExecutor
from ...komugi_fast_path_client import post_komugi_ui_event


class BasicMemoryAgent(AgentInterface):
    """Agent with basic chat memory and tool calling support."""

    _system: str = "You are a helpful assistant."

    def __init__(
        self,
        llm: StatelessLLMInterface,
        system: str,
        live2d_model,
        tts_preprocessor_config: TTSPreprocessorConfig = None,
        faster_first_response: bool = True,
        segment_method: str = "pysbd",
        use_mcpp: bool = False,
        interrupt_method: Literal["system", "user"] = "user",
        tool_prompts: Dict[str, str] = None,
        tool_manager: Optional[ToolManager] = None,
        tool_executor: Optional[ToolExecutor] = None,
        mcp_prompt_string: str = "",
        command_center_base_url: str = "http://127.0.0.1:18000",
    ):
        """Initialize agent with LLM and configuration."""
        super().__init__()
        self._memory = []
        self._live2d_model = live2d_model
        self._tts_preprocessor_config = tts_preprocessor_config
        self._faster_first_response = faster_first_response
        self._segment_method = segment_method
        self._use_mcpp = use_mcpp
        self.interrupt_method = interrupt_method
        self._tool_prompts = tool_prompts or {}
        self._interrupt_handled = False
        self.prompt_mode_flag = False

        self._tool_manager = tool_manager
        self._tool_executor = tool_executor
        self._mcp_prompt_string = mcp_prompt_string
        self._command_center_base_url = command_center_base_url
        self._json_detector = StreamJSONDetector()

        self._formatted_tools_openai = []
        self._formatted_tools_claude = []
        if self._tool_manager:
            self._formatted_tools_openai = self._tool_manager.get_formatted_tools(
                "OpenAI"
            )
            self._formatted_tools_claude = self._tool_manager.get_formatted_tools(
                "Claude"
            )
            logger.debug(
                f"Agent received pre-formatted tools - OpenAI: {len(self._formatted_tools_openai)}, Claude: {len(self._formatted_tools_claude)}"
            )
        else:
            logger.debug(
                "ToolManager not provided, agent will not have pre-formatted tools."
            )

        self._set_llm(llm)
        self.set_system(system if system else self._system)

        if self._use_mcpp and not all(
            [
                self._tool_manager,
                self._tool_executor,
                self._json_detector,
            ]
        ):
            logger.warning(
                "use_mcpp is True, but some MCP components are missing in the agent. Tool calling might not work as expected."
            )
        elif not self._use_mcpp and any(
            [
                self._tool_manager,
                self._tool_executor,
                self._json_detector,
            ]
        ):
            logger.warning(
                "use_mcpp is False, but some MCP components were passed to the agent."
            )

        logger.info("BasicMemoryAgent initialized.")

    def _set_llm(self, llm: StatelessLLMInterface):
        """Set the LLM for chat completion."""
        self._llm = llm
        self.chat = self._chat_function_factory()

    def set_system(self, system: str):
        """Set the system prompt."""
        logger.debug(f"Memory Agent: Setting system prompt: '''{system}'''")

        if self.interrupt_method == "user":
            system = f"{system}\n\nIf you received `[interrupted by user]` signal, you were interrupted."

        self._system = system

    def _add_message(
        self,
        message: Union[str, List[Dict[str, Any]]],
        role: str,
        display_text: DisplayText | None = None,
        skip_memory: bool = False,
    ):
        """Add message to memory."""
        if skip_memory:
            return

        text_content = ""
        if isinstance(message, list):
            for item in message:
                if item.get("type") == "text":
                    text_content += item["text"] + " "
            text_content = text_content.strip()
        elif isinstance(message, str):
            text_content = message
        else:
            logger.warning(
                f"_add_message received unexpected message type: {type(message)}"
            )
            text_content = str(message)

        if not text_content and role == "assistant":
            return

        message_data = {
            "role": role,
            "content": text_content,
        }

        if display_text:
            if display_text.name:
                message_data["name"] = display_text.name
            if display_text.avatar:
                message_data["avatar"] = display_text.avatar

        if (
            self._memory
            and self._memory[-1]["role"] == role
            and self._memory[-1]["content"] == text_content
        ):
            return

        self._memory.append(message_data)

    def set_memory_from_history(self, conf_uid: str, history_uid: str) -> None:
        """Load memory from chat history."""
        messages = get_history(conf_uid, history_uid)

        self._memory = []
        for msg in messages:
            role = "user" if msg["role"] == "human" else "assistant"
            content = msg["content"]
            if isinstance(content, str) and content:
                self._memory.append(
                    {
                        "role": role,
                        "content": content,
                    }
                )
            else:
                logger.warning(f"Skipping invalid message from history: {msg}")
        logger.info(f"Loaded {len(self._memory)} messages from history.")

    def handle_interrupt(self, heard_response: str) -> None:
        """Handle user interruption."""
        if self._interrupt_handled:
            return

        self._interrupt_handled = True

        if self._memory and self._memory[-1]["role"] == "assistant":
            if not self._memory[-1]["content"].endswith("..."):
                self._memory[-1]["content"] = heard_response + "..."
            else:
                self._memory[-1]["content"] = heard_response + "..."
        else:
            if heard_response:
                self._memory.append(
                    {
                        "role": "assistant",
                        "content": heard_response + "...",
                    }
                )

        interrupt_role = "system" if self.interrupt_method == "system" else "user"
        self._memory.append(
            {
                "role": interrupt_role,
                "content": "[Interrupted by user]",
            }
        )
        logger.info(f"Handled interrupt with role '{interrupt_role}'.")

    def _to_text_prompt(self, input_data: BatchInput) -> str:
        """Format input data to text prompt."""
        message_parts = []

        for text_data in input_data.texts:
            if text_data.source == TextSource.INPUT:
                message_parts.append(text_data.content)
            elif text_data.source == TextSource.CLIPBOARD:
                message_parts.append(
                    f"[User shared content from clipboard: {text_data.content}]"
                )

        if input_data.images:
            message_parts.append("\n[User has also provided images]")

        return "\n".join(message_parts).strip()

    def _to_messages(self, input_data: BatchInput) -> List[Dict[str, Any]]:
        """Prepare messages for LLM API call."""
        messages = self._memory.copy()
        user_content = []
        text_prompt = self._to_text_prompt(input_data)
        if text_prompt:
            user_content.append({"type": "text", "text": text_prompt})

        if input_data.images:
            image_added = False
            for img_data in input_data.images:
                if isinstance(img_data.data, str) and img_data.data.startswith(
                    "data:image"
                ):
                    user_content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": img_data.data, "detail": "auto"},
                        }
                    )
                    image_added = True
                else:
                    logger.error(
                        f"Invalid image data format: {type(img_data.data)}. Skipping image."
                    )

            if not image_added and not text_prompt:
                logger.warning(
                    "User input contains images but none could be processed."
                )

        if user_content:
            user_message = {"role": "user", "content": user_content}
            messages.append(user_message)

            skip_memory = False
            if input_data.metadata and input_data.metadata.get("skip_memory", False):
                skip_memory = True

            if not skip_memory:
                self._add_message(
                    text_prompt if text_prompt else "[User provided image(s)]", "user"
                )
        else:
            logger.warning("No content generated for user message.")

        return messages

    def _should_prefetch_komugi_context(self, input_data: BatchInput) -> bool:
        """Return True when household state questions should force Komugi context."""
        if not (self._use_mcpp and self._tool_manager and self._tool_executor):
            return False
        if not self._tool_manager.get_tool("get_komugi_context"):
            return False

        text_prompt = self._to_text_prompt(input_data).lower()
        if not text_prompt:
            return False

        if self._is_local_disaster_question(text_prompt):
            return True

        keywords = [
            "ゴミ",
            "ごみ",
            "通知",
            "command center",
            "home assistant",
            "homeassistant",
            "アプリ",
            "接続",
            "操作",
            "家電",
            "つなが",
            "繋が",
            "voicevox",
            "ollama",
            "japan monitor",
            "ジャパンモニター",
            "状態",
        ]
        return any(keyword in text_prompt for keyword in keywords)

    def _is_local_disaster_question(self, text_prompt: str) -> bool:
        """Return True for registered-area disaster and safety-status questions."""
        educational_keywords = ["仕組み", "どうして", "なぜ", "理由", "とは", "教えて"]
        if any(keyword in text_prompt for keyword in educational_keywords):
            return False

        explicit_disaster_keywords = [
            "地域防災",
            "防災",
            "災害情報",
            "防災情報",
            "登録地域",
            "japan monitor",
            "ジャパンモニター",
        ]
        if any(keyword in text_prompt for keyword in explicit_disaster_keywords):
            return True

        if "気象情報" in text_prompt and any(
            keyword in text_prompt
            for keyword in ["ある", "出てる", "出ている", "発表", "状況", "確認"]
        ):
            return True

        disaster_topics = [
            "警報",
            "注意報",
            "災害",
            "地震",
            "津波",
            "台風",
            "河川",
            "大雨",
            "リスク",
        ]
        status_keywords = [
            "大丈夫",
            "安全",
            "危ない",
            "危険",
            "状況",
            "確認",
            "出てる",
            "出ている",
            "ある",
            "あった",
            "来てる",
            "来ている",
            "どう",
            "発生",
            "起きた",
        ]
        if any(keyword in text_prompt for keyword in disaster_topics) and any(
            keyword in text_prompt for keyword in status_keywords
        ):
            return True

        municipality_keywords = ["市", "町", "村", "区"]
        municipality_safety_keywords = ["大丈夫", "安全", "危険", "リスク"]
        if any(keyword in text_prompt for keyword in municipality_keywords) and any(
            keyword in text_prompt for keyword in municipality_safety_keywords
        ):
            return True

        local_keywords = [
            "この辺",
            "このへん",
            "この地域",
            "近く",
            "周辺",
            "近所",
            "川",
            "家の地域",
            "家の登録地域",
        ]
        safety_keywords = [
            "大丈夫",
            "安全",
            "危ない",
            "危険",
            "状況",
            "確認",
            "出てる",
            "出ている",
            "来てる",
            "来ている",
        ]
        return any(keyword in text_prompt for keyword in local_keywords) and any(
            keyword in text_prompt for keyword in safety_keywords
        )

    def _should_enable_mcp_tools(self, input_data: BatchInput) -> bool:
        """Return True only when the current turn looks like it can benefit from MCP tools."""
        if not (self._use_mcpp and self._tool_manager and self._tool_executor):
            return False

        text_prompt = self._to_text_prompt(input_data).lower()
        if not text_prompt:
            return False

        if self._is_local_disaster_question(text_prompt):
            return True

        strong_tool_keywords = [
            "command center",
            "home assistant",
            "homeassistant",
            "voicevox",
            "ollama",
            "japan monitor",
            "mcp",
            "api",
            "status",
            "error",
            "log",
            "search",
            "lookup",
            "weather",
            "garbage",
            "notification",
            "device",
            "sensor",
            "天気",
            "ゴミ",
            "ごみ",
            "通知",
            "検索",
            "調べ",
            "探し",
            "時刻",
            "何時",
            "状態",
            "接続",
            "操作",
            "家電",
            "アプリ",
            "傘",
            "洗濯",
            "外干し",
            "外出",
            "週間",
            "今週",
            "降水確率",
            "最高気温",
            "最低気温",
        ]
        if any(keyword in text_prompt for keyword in strong_tool_keywords):
            return True

        temporal_keywords = [
            "今日",
            "明日",
            "today",
            "tomorrow",
            "date",
        ]
        temporal_context_keywords = [
            "雨",
            "暑い",
            "寒い",
            "気温",
            "予報",
            "天候",
            "曜日",
            "何日",
            "何曜日",
            "予定",
            "スケジュール",
        ]
        return any(keyword in text_prompt for keyword in temporal_keywords) and any(
            keyword in text_prompt for keyword in temporal_context_keywords
        )


    def _build_weather_forecast_request(
        self, input_data: BatchInput
    ) -> Optional[Dict[str, str]]:
        """Return a deterministic weather MCP request for forecast questions."""
        if not (self._use_mcpp and self._tool_manager and self._tool_executor):
            return None
        if not self._tool_manager.get_tool("get_weather_forecast"):
            return None

        text_prompt = self._to_text_prompt(input_data).lower()
        if not text_prompt:
            return None
        if self._is_local_disaster_question(text_prompt):
            return None

        weather_keywords = [
            "weather",
            "forecast",
            "\u5929\u6c17",
            "\u4e88\u5831",
            "\u964d\u6c34\u78ba\u7387",
            "\u6700\u9ad8\u6c17\u6e29",
            "\u6700\u4f4e\u6c17\u6e29",
            "\u9031\u9593",
            "\u4eca\u9031",
            "\u4e00\u9031\u9593",
        ]
        if not any(keyword in text_prompt for keyword in weather_keywords):
            return None

        tomorrow_keywords = ["tomorrow", "\u660e\u65e5", "\u3042\u3057\u305f"]
        weekly_keywords = ["weekly", "this week", "\u9031\u9593", "\u4eca\u9031", "\u4e00\u9031\u9593"]
        numeric_keywords = ["\u964d\u6c34\u78ba\u7387", "\u6700\u9ad8\u6c17\u6e29", "\u6700\u4f4e\u6c17\u6e29"]
        regional_locations = [
            ("tokyo", "\u6771\u4eac"),
            ("\u6771\u4eac\u90fd", "\u6771\u4eac"),
            ("\u6771\u4eac", "\u6771\u4eac"),
            ("otawara", "\u5927\u7530\u539f"),
            ("\u5927\u7530\u539f\u5e02", "\u5927\u7530\u539f"),
            ("\u5927\u7530\u539f", "\u5927\u7530\u539f"),
            ("nasushiobara", "\u90a3\u9808\u5869\u539f"),
            ("\u90a3\u9808\u5869\u539f\u5e02", "\u90a3\u9808\u5869\u539f"),
            ("\u90a3\u9808\u5869\u539f", "\u90a3\u9808\u5869\u539f"),
        ]

        is_tomorrow = any(keyword in text_prompt for keyword in tomorrow_keywords)
        is_weekly = any(keyword in text_prompt for keyword in weekly_keywords)
        is_numeric = any(keyword in text_prompt for keyword in numeric_keywords)
        location = "\u5927\u7530\u539f"
        is_regional = False
        for marker, resolved in regional_locations:
            if marker in text_prompt:
                location = resolved
                is_regional = True
                break

        # Generic local "today's weather" is handled by the Fast Path before this agent.
        # Only forecast/detail questions are prefetched here.
        if not (is_tomorrow or is_weekly or is_regional or is_numeric):
            return None

        day = "weekly" if is_weekly else "tomorrow" if is_tomorrow else "today"
        return {"location": location, "day": day}

    async def _prefetch_weather_forecast(
        self, location: str, day: str
    ) -> AsyncIterator[Dict[str, Any]]:
        """Run get_weather_forecast before the LLM for forecast questions."""
        tool_id = f"prefetch_weather_{uuid.uuid4().hex[:8]}"
        tool_call = {
            "id": tool_id,
            "name": "get_weather_forecast",
            "input": {"location": location, "day": day},
        }
        tool_executor_iterator = self._tool_executor.execute_tools(
            tool_calls=[tool_call],
            caller_mode="OpenAI",
        )
        forecast_text = ""

        async for update in tool_executor_iterator:
            if update.get("type") == "final_tool_results":
                results = update.get("results", [])
                for result in results:
                    if result.get("tool_call_id") == tool_id:
                        forecast_text = str(result.get("content", ""))
                continue

            if (
                update.get("type") == "tool_call_status"
                and update.get("tool_name") == "get_weather_forecast"
                and update.get("status") == "completed"
            ):
                forecast_text = str(update.get("content", ""))

            yield update

        if forecast_text:
            yield {
                "type": "weather_forecast_prefetch_result",
                "content": forecast_text,
            }

    async def _prefetch_komugi_context(
        self,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Run get_komugi_context before the LLM when local context is required."""
        tool_id = f"prefetch_{uuid.uuid4().hex[:8]}"
        tool_call = {
            "id": tool_id,
            "name": "get_komugi_context",
            "input": {},
        }
        tool_executor_iterator = self._tool_executor.execute_tools(
            tool_calls=[tool_call],
            caller_mode="OpenAI",
        )
        context_text = ""

        async for update in tool_executor_iterator:
            if update.get("type") == "final_tool_results":
                results = update.get("results", [])
                for result in results:
                    if result.get("tool_call_id") == tool_id:
                        context_text = str(result.get("content", ""))
                continue

            if (
                update.get("type") == "tool_call_status"
                and update.get("tool_name") == "get_komugi_context"
                and update.get("status") == "completed"
            ):
                context_text = str(update.get("content", ""))

            yield update

        if context_text:
            yield {
                "type": "komugi_context_prefetch_result",
                "content": context_text,
            }

    def _schedule_komugi_ui_event(
        self,
        view: str,
        period: str | None = None,
        topic: str | None = None,
        local_status: Dict[str, Any] | None = None,
    ) -> None:
        asyncio.create_task(
            post_komugi_ui_event(
                self._command_center_base_url,
                view,
                period,
                topic,
                local_status,
            )
        )

    async def _build_local_status_fallback_answer(
        self, input_data: BatchInput
    ) -> tuple[str, Dict[str, Any] | None]:
        text_prompt = self._to_text_prompt(input_data).lower()
        topic = self._local_disaster_topic(text_prompt)
        if topic not in {"earthquake", "weather_alerts"}:
            return "", None

        try:
            response = await asyncio.to_thread(
                requests.get,
                _join_url(self._command_center_base_url, "/api/komugi/local-status"),
                timeout=(0.8, 12.0),
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning(f"Komugi local-status fallback failed: {type(exc).__name__}")
            message = "現在、地域防災情報の取得先に接続できていません。"
            return message, {"available": False, "message": message}

        if not isinstance(data, dict):
            message = "現在、地域防災情報の取得先に接続できていません。"
            return message, {"available": False, "message": message}
        if data.get("available") is False:
            message = str(
                data.get("message")
                or "現在、地域防災情報の取得先に接続できていません。"
            ).strip()
            return message, data

        if topic == "weather_alerts":
            alerts = data.get("weather_alerts")
            alerts = alerts if isinstance(alerts, list) else []
            area = data.get("area") if isinstance(data.get("area"), dict) else {}
            area_name = str(
                area.get("city")
                or area.get("location_name")
                or " ".join(
                    str(area.get(key) or "") for key in ("prefecture", "city")
                ).strip()
                or "この地域"
            )
            metadata = data.get("metadata")
            stale = bool(
                isinstance(metadata, dict) and metadata.get("stale")
            )
            prefix = "直前に取得した情報では、" if stale else "現在、"
            alert_names = [
                str(item.get("title") or item.get("name") or "").strip()
                for item in alerts
                if isinstance(item, dict)
            ]
            alert_names = [name for name in alert_names if name]
            if alert_names:
                return (
                    f"{prefix}{area_name}には{'、'.join(alert_names)}が出ています。",
                    data,
                )
            return (
                f"{prefix}{area_name}に発表中の気象警報・注意報は確認されていません。",
                data,
            )

        earthquake = data.get("earthquake") if isinstance(data.get("earthquake"), dict) else {}
        area = data.get("area") if isinstance(data.get("area"), dict) else {}
        area_name = str(
            area.get("city")
            or area.get("location_name")
            or " ".join(str(area.get(key) or "") for key in ("prefecture", "city")).strip()
            or "\u3053\u306e\u5730\u57df"
        )
        if earthquake.get("active") is True:
            top = earthquake.get("top") if isinstance(earthquake.get("top"), dict) else {}
            earthquake_area = str(top.get("area") or "").strip()
            summary = str(top.get("summary") or top.get("title") or "").strip()
            title = str(top.get("title") or "").strip()
            intensity = self._extract_intensity_text(title, summary)
            if earthquake_area and intensity:
                answer = f"{earthquake_area}で{intensity}の地震がありました。"
            elif earthquake_area:
                answer = f"{earthquake_area}で地震情報があります。"
            elif summary:
                answer = summary
            else:
                answer = "地震情報があります。"
            return f"{answer}{area_name}への影響は現在確認できていません。", data
        return f"現在、{area_name}の地震情報に大きな異常は確認されていません。", data

    def _extract_context_value(self, context_text: str, label: str) -> str:
        match = re.search(rf"{re.escape(label)}:\s*([^/\n;]+)", context_text)
        return match.group(1).strip() if match else ""

    def _extract_first_context_value(
        self, context_text: str, labels: List[str]
    ) -> str:
        for label in labels:
            value = self._extract_context_value(context_text, label)
            if value:
                return value
        return ""

    def _extract_context_line(self, context_text: str, label: str) -> str:
        for line in context_text.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"- {label}:"):
                return stripped[2:].strip()
        return ""

    def _has_municipality_reference(self, text_prompt: str) -> bool:
        return any(keyword in text_prompt for keyword in ["市", "町", "村", "区"])

    def _local_disaster_topic(self, text_prompt: str) -> str:
        if (
            "警報" in text_prompt
            or "注意報" in text_prompt
            or "気象情報" in text_prompt
        ):
            return "weather_alerts"
        if "地震" in text_prompt:
            return "earthquake"
        if "津波" in text_prompt:
            return "tsunami"
        if "川" in text_prompt or "河川" in text_prompt:
            return "river"
        if "台風" in text_prompt:
            return "typhoon"
        if "大雨" in text_prompt:
            return "heavy_rain"
        return "overall"

    def _extract_semicolon_field(self, value: str, key: str) -> str:
        prefix = f"{key}="
        for part in value.split(";"):
            part = part.strip()
            if part.startswith(prefix):
                return part[len(prefix) :].strip()
        return ""

    def _extract_intensity_text(self, *values: str) -> str:
        for value in values:
            match = re.search(r"(?:\u6700\u5927)?\u9707\u5ea6\s*([0-9\uff10-\uff19]+)", value)
            if match:
                return f"\u9707\u5ea6{match.group(1)}"
        return ""

    def _target_city_from_context(self, context_text: str) -> str:
        area_line = self._extract_context_line(context_text, "\u5bfe\u8c61\u5730\u57df")
        if not area_line:
            return "\u3053\u306e\u5730\u57df"
        area_value = area_line.split(":", 1)[1].strip()
        for part in reversed(area_value.split()):
            if part.endswith(("\u5e02", "\u753a", "\u6751", "\u533a")):
                return part
        return area_value or "\u3053\u306e\u5730\u57df"

    def _earthquake_area_matches_home(self, earthquake_area: str, context_text: str) -> bool:
        area_line = self._extract_context_line(context_text, "\u5bfe\u8c61\u5730\u57df")
        if not area_line or not earthquake_area:
            return False
        area_value = area_line.split(":", 1)[1].strip()
        return any(part and part in earthquake_area for part in area_value.split())

    def _build_earthquake_answer(self, context_text: str) -> str:
        item_line = self._extract_context_line(context_text, "earthquake_item_1")
        if not item_line:
            return ""
        value = item_line.split(":", 1)[1].strip()
        area = self._extract_semicolon_field(value, "area")
        title = self._extract_semicolon_field(value, "title")
        summary = self._extract_semicolon_field(value, "summary")
        intensity = self._extract_intensity_text(title, summary)
        target_city = self._target_city_from_context(context_text)

        if area and intensity:
            first_sentence = f"{area}\u3067{intensity}\u306e\u5730\u9707\u304c\u3042\u308a\u307e\u3057\u305f\u3002"
        elif area:
            first_sentence = f"{area}\u3067\u5730\u9707\u60c5\u5831\u304c\u3042\u308a\u307e\u3059\u3002"
        elif summary:
            first_sentence = summary
        else:
            first_sentence = "\u5730\u9707\u60c5\u5831\u304c\u3042\u308a\u307e\u3059\u3002"

        if self._earthquake_area_matches_home(area, context_text):
            return f"{first_sentence}{target_city}\u5468\u8fba\u3067\u3082\u5f71\u97ff\u304c\u306a\u3044\u304b\u78ba\u8a8d\u3057\u3066\u304f\u3060\u3055\u3044\u3002"
        return f"{first_sentence}{target_city}\u3078\u306e\u5f71\u97ff\u306f\u73fe\u5728\u78ba\u8a8d\u3067\u304d\u3066\u3044\u307e\u305b\u3093\u3002"

    def _build_local_disaster_topic_answer(
        self, topic: str, context_text: str
    ) -> str:
        if topic == "weather_alerts":
            line = self._extract_context_line(context_text, "気象警報注意報")
            if not line:
                return ""
            value = line.split(":", 1)[1].strip()
            if value.startswith("0"):
                return "現在、この地域に気象警報・注意報は確認されていません。"
            return f"現在、この地域に気象警報・注意報が{value}あります。"

        labels = {
            "earthquake": ("地震", "地震情報"),
            "tsunami": ("津波", "津波情報"),
            "river": ("河川", "河川情報"),
            "typhoon": ("台風", "台風情報"),
            "heavy_rain": ("大雨", "大雨関連情報"),
        }
        label_pair = labels.get(topic)
        if not label_pair:
            return ""

        context_label, display_label = label_pair
        line = self._extract_context_line(context_text, context_label)
        if not line:
            return ""
        value = line.split(":", 1)[1].strip()
        if "active=False" in value:
            return f"現在、この地域の{display_label}に大きな異常は確認されていません。"
        if "active=True" in value:
            if topic == "earthquake":
                earthquake_answer = self._build_earthquake_answer(context_text)
                if earthquake_answer:
                    return earthquake_answer
            return f"\u73fe\u5728\u3001\u3053\u306e\u5730\u57df\u306e{display_label}\u304c\u3042\u308a\u307e\u3059\u3002{value}"

    def _context_line_is_ok(self, line: str) -> bool:
        lowered = line.lower()
        return "ok" in lowered or "接続ok" in lowered or "利用可能" in lowered

    def _build_komugi_context_answer(
        self, input_data: BatchInput, context_text: str
    ) -> str:
        """Build short deterministic replies for high-priority home context facts."""
        text_prompt = self._to_text_prompt(input_data).lower()
        if not context_text:
            return ""

        if ("ゴミ" in text_prompt or "ごみ" in text_prompt) and "明日" in text_prompt:
            tomorrow = self._extract_first_context_value(
                context_text, ["tomorrow/明日", "明日", "tomorrow"]
            )
            if tomorrow:
                return f"明日のゴミ出しは「{tomorrow}」です。"
            return "明日のゴミ出し情報は、Command Centerのcontextでは確認できません。"

        if ("ゴミ" in text_prompt or "ごみ" in text_prompt) and "今日" in text_prompt:
            today = self._extract_first_context_value(
                context_text, ["today/今日", "今日", "today"]
            )
            if today:
                return f"今日のゴミ出しは「{today}」です。"
            return "今日のゴミ出し情報は、Command Centerのcontextでは確認できません。"

        if "voicevox" in text_prompt or "ollama" in text_prompt:
            voicevox = self._extract_context_line(context_text, "VOICEVOX")
            ollama = self._extract_context_line(context_text, "Ollama")
            if voicevox or ollama:
                if self._context_line_is_ok(voicevox) and self._context_line_is_ok(
                    ollama
                ):
                    return "VOICEVOXとOllamaはどちらも接続OKです。"
                return "VOICEVOXとOllamaの接続状態は、Command Centerで確認できます。"
            return "VOICEVOXとOllamaの接続状態は確認できません。"

        if "通知" in text_prompt:
            notifications = []
            in_section = False
            for line in context_text.splitlines():
                if line == "notifications:":
                    in_section = True
                    continue
                if in_section and line.endswith(":"):
                    break
                if in_section and line.startswith("- "):
                    notification = line[2:]
                    title = notification.split(":", 1)[0].strip()
                    notifications.append(title or notification)
            if notifications:
                count = len(notifications)
                return f"通知は{count}件あります。詳しくはCommand Centerで確認できます。"
            return "通知は確認できません。"

        if self._is_local_disaster_question(text_prompt):
            available = self._extract_context_line(context_text, "available")
            status_message = self._extract_context_line(context_text, "状況")
            area = self._extract_context_line(context_text, "対象地域")
            risk_level = self._extract_context_line(context_text, "risk_level")
            is_unavailable = "false" in available.lower()
            topic = self._local_disaster_topic(text_prompt)

            if is_unavailable:
                return ""
            if topic == "weather_alerts":
                return ""
            if status_message:
                message = status_message.split(":", 1)[1].strip()
                topic_answer = self._build_local_disaster_topic_answer(
                    topic, context_text
                )
                if topic_answer:
                    return topic_answer

                if area and self._has_municipality_reference(text_prompt):
                    area_name = area.split(":", 1)[1].strip()
                    return f"{area_name}の地域防災情報です。{message}"
                return message
            if risk_level:
                level = risk_level.split(":", 1)[1].strip()
                return f"地域防災情報は取得できました。現在のリスクは{level}です。"
            return "地域防災情報は、Command Centerのcontextでは確認できません。"

        if (
            "home assistant" in text_prompt
            or "homeassistant" in text_prompt
            or "家電" in text_prompt
            or "操作" in text_prompt
        ):
            home_assistant_control = self._extract_context_value(
                context_text, "home_assistant_control"
            )
            device_control = self._extract_context_value(context_text, "device_control")
            home_assistant = self._extract_context_line(context_text, "Home Assistant")
            if home_assistant_control or device_control or home_assistant:
                return "まだ家電操作はできません。今は状態確認だけ対応しています。"
            return "Home Assistantや家電操作の可否は確認できません。"

        if "command center" in text_prompt or "状態" in text_prompt or "アプリ" in text_prompt:
            status = self._extract_context_value(context_text, "status")
            command_center = self._extract_context_line(context_text, "Command Center API")
            open_llm = self._extract_context_line(context_text, "Open-LLM-VTuber")
            voicevox = self._extract_context_line(context_text, "VOICEVOX")
            ollama = self._extract_context_line(context_text, "Ollama")
            main_services_ok = all(
                self._context_line_is_ok(line)
                for line in [command_center, open_llm, voicevox, ollama]
            )
            if status == "ok" and main_services_ok:
                return "Command Centerは正常です。主要サービスも接続OKです。"
            if status:
                return "Command Centerの状態はCommand Centerで確認できます。"
            return "Command Centerの状態は確認できません。"

        if "天気" in text_prompt:
            weather = self._extract_context_line(context_text, "天気")
            if weather:
                return weather
            return "天気情報は、Command Centerのcontextでは確認できません。"

        return ""

    async def _claude_tool_interaction_loop(
        self,
        initial_messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> AsyncIterator[Union[str, Dict[str, Any]]]:
        """Handle Claude interaction loop with tool support."""
        messages = initial_messages.copy()
        current_turn_text = ""
        pending_tool_calls = []
        current_assistant_message_content = []

        while True:
            stream = self._llm.chat_completion(messages, self._system, tools=tools)
            pending_tool_calls.clear()
            current_assistant_message_content.clear()

            async for event in stream:
                if event["type"] == "text_delta":
                    text = event["text"]
                    current_turn_text += text
                    yield text
                    if (
                        not current_assistant_message_content
                        or current_assistant_message_content[-1]["type"] != "text"
                    ):
                        current_assistant_message_content.append(
                            {"type": "text", "text": text}
                        )
                    else:
                        current_assistant_message_content[-1]["text"] += text
                elif event["type"] == "tool_use_complete":
                    tool_call_data = event["data"]
                    logger.info(
                        f"Tool request: {tool_call_data['name']} (ID: {tool_call_data['id']})"
                    )
                    pending_tool_calls.append(tool_call_data)
                    current_assistant_message_content.append(
                        {
                            "type": "tool_use",
                            "id": tool_call_data["id"],
                            "name": tool_call_data["name"],
                            "input": tool_call_data["input"],
                        }
                    )
                # elif event["type"] == "message_delta":
                #     if event["data"]["delta"].get("stop_reason"):
                #         stop_reason = event["data"]["delta"].get("stop_reason")
                elif event["type"] == "message_stop":
                    break
                elif event["type"] == "error":
                    logger.error(f"LLM API Error: {event['message']}")
                    yield f"[Error from LLM: {event['message']}]"
                    return

            if pending_tool_calls:
                filtered_assistant_content = [
                    block
                    for block in current_assistant_message_content
                    if not (
                        block.get("type") == "text"
                        and not block.get("text", "").strip()
                    )
                ]

                if filtered_assistant_content:
                    messages.append(
                        {"role": "assistant", "content": filtered_assistant_content}
                    )
                    assistant_text_for_memory = "".join(
                        [
                            c["text"]
                            for c in filtered_assistant_content
                            if c["type"] == "text"
                        ]
                    ).strip()
                    if assistant_text_for_memory:
                        self._add_message(assistant_text_for_memory, "assistant")

                tool_results_for_llm = []
                if not self._tool_executor:
                    logger.error(
                        "Claude Tool interaction requested but ToolExecutor is not available."
                    )
                    yield "[Error: ToolExecutor not configured]"
                    return

                tool_executor_iterator = self._tool_executor.execute_tools(
                    tool_calls=pending_tool_calls,
                    caller_mode="Claude",
                )
                try:
                    while True:
                        update = await anext(tool_executor_iterator)
                        if update.get("type") == "final_tool_results":
                            tool_results_for_llm = update.get("results", [])
                            break
                        else:
                            yield update
                except StopAsyncIteration:
                    logger.warning(
                        "Tool executor finished without final results marker."
                    )

                if tool_results_for_llm:
                    messages.append({"role": "user", "content": tool_results_for_llm})

                # stop_reason = None
                continue
            else:
                if current_turn_text:
                    self._add_message(current_turn_text, "assistant")
                return

    async def _openai_tool_interaction_loop(
        self,
        initial_messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> AsyncIterator[Union[str, Dict[str, Any]]]:
        """Handle OpenAI interaction with tool support."""
        messages = initial_messages.copy()
        current_turn_text = ""
        pending_tool_calls: Union[List[ToolCallObject], List[Dict[str, Any]]] = []
        current_system_prompt = self._system

        while True:
            if self.prompt_mode_flag:
                if self._mcp_prompt_string:
                    current_system_prompt = (
                        f"{self._system}\n\n{self._mcp_prompt_string}"
                    )
                else:
                    logger.warning("Prompt mode active but mcp_prompt_string is empty!")
                    current_system_prompt = self._system
                tools_for_api = None
            else:
                current_system_prompt = self._system
                tools_for_api = tools

            stream = self._llm.chat_completion(
                messages, current_system_prompt, tools=tools_for_api
            )
            pending_tool_calls.clear()
            current_turn_text = ""
            assistant_message_for_api = None
            detected_prompt_json = None
            goto_next_while_iteration = False

            async for event in stream:
                if self.prompt_mode_flag:
                    if isinstance(event, str):
                        current_turn_text += event
                        if self._json_detector:
                            potential_json = self._json_detector.process_chunk(event)
                            if potential_json:
                                try:
                                    if isinstance(potential_json, list):
                                        detected_prompt_json = potential_json
                                    elif isinstance(potential_json, dict):
                                        detected_prompt_json = [potential_json]

                                    if detected_prompt_json:
                                        break
                                except Exception as e:
                                    logger.error(f"Error parsing detected JSON: {e}")
                                    if self._json_detector:
                                        self._json_detector.reset()
                                    yield f"[Error parsing tool JSON: {e}]"
                                    goto_next_while_iteration = True
                                    break
                        yield event
                else:
                    if isinstance(event, str):
                        current_turn_text += event
                        yield event
                    elif isinstance(event, list) and all(
                        isinstance(tc, ToolCallObject) for tc in event
                    ):
                        pending_tool_calls = event
                        assistant_message_for_api = {
                            "role": "assistant",
                            "content": current_turn_text if current_turn_text else None,
                            "tool_calls": [
                                {
                                    "id": tc.id,
                                    "type": tc.type,
                                    "function": {
                                        "name": tc.function.name,
                                        "arguments": tc.function.arguments,
                                    },
                                }
                                for tc in pending_tool_calls
                            ],
                        }
                        break
                    elif event == "__API_NOT_SUPPORT_TOOLS__":
                        logger.warning(
                            f"LLM {getattr(self._llm, 'model', '')} has no native tool support. Switching to prompt mode."
                        )
                        self.prompt_mode_flag = True
                        if self._tool_manager:
                            self._tool_manager.disable()
                        if self._json_detector:
                            self._json_detector.reset()
                        goto_next_while_iteration = True
                        break
            if goto_next_while_iteration:
                continue

            if detected_prompt_json:
                logger.info("Processing tools detected via prompt mode JSON.")
                self._add_message(current_turn_text, "assistant")

                parsed_tools = self._tool_executor.process_tool_from_prompt_json(
                    detected_prompt_json
                )
                if parsed_tools:
                    tool_results_for_llm = []
                    if not self._tool_executor:
                        logger.error(
                            "Prompt Tool interaction requested but ToolExecutor/MCPClient is not available."
                        )
                        yield "[Error: ToolExecutor/MCPClient not configured for prompt mode]"
                        continue

                    tool_executor_iterator = self._tool_executor.execute_tools(
                        tool_calls=parsed_tools,
                        caller_mode="Prompt",
                    )
                    try:
                        while True:
                            update = await anext(tool_executor_iterator)
                            if update.get("type") == "final_tool_results":
                                tool_results_for_llm = update.get("results", [])
                                break
                            else:
                                yield update
                    except StopAsyncIteration:
                        logger.warning(
                            "Prompt mode tool executor finished without final results marker."
                        )

                    if tool_results_for_llm:
                        result_strings = [
                            res.get("content", "Error: Malformed result")
                            for res in tool_results_for_llm
                        ]
                        combined_results_str = "\n".join(result_strings)
                        messages.append(
                            {"role": "user", "content": combined_results_str}
                        )
                continue

            elif pending_tool_calls and assistant_message_for_api:
                messages.append(assistant_message_for_api)
                if current_turn_text:
                    self._add_message(current_turn_text, "assistant")

                tool_results_for_llm = []
                if not self._tool_executor:
                    logger.error(
                        "OpenAI Tool interaction requested but ToolExecutor/MCPClient is not available."
                    )
                    yield "[Error: ToolExecutor/MCPClient not configured for OpenAI mode]"
                    continue

                tool_executor_iterator = self._tool_executor.execute_tools(
                    tool_calls=pending_tool_calls,
                    caller_mode="OpenAI",
                )
                try:
                    while True:
                        update = await anext(tool_executor_iterator)
                        if update.get("type") == "final_tool_results":
                            tool_results_for_llm = update.get("results", [])
                            break
                        else:
                            yield update
                except StopAsyncIteration:
                    logger.warning(
                        "OpenAI tool executor finished without final results marker."
                    )

                if tool_results_for_llm:
                    messages.extend(tool_results_for_llm)
                continue

            else:
                if current_turn_text:
                    self._add_message(current_turn_text, "assistant")
                return

    def _chat_function_factory(
        self,
    ) -> Callable[[BatchInput], AsyncIterator[Union[SentenceOutput, Dict[str, Any]]]]:
        """Create the chat pipeline function."""

        @tts_filter(self._tts_preprocessor_config)
        @display_processor()
        @actions_extractor(self._live2d_model)
        @sentence_divider(
            faster_first_response=self._faster_first_response,
            segment_method=self._segment_method,
            valid_tags=["think"],
        )
        async def chat_with_memory(
            input_data: BatchInput,
        ) -> AsyncIterator[Union[str, Dict[str, Any]]]:
            """Process chat with memory and tools."""
            self.reset_interrupt()
            self.prompt_mode_flag = False

            messages = self._to_messages(input_data)
            tools = None
            tool_mode = None
            llm_supports_native_tools = False

            if self._use_mcpp and self._tool_manager:
                tools = None
                if isinstance(self._llm, ClaudeAsyncLLM):
                    tool_mode = "Claude"
                    tools = self._formatted_tools_claude
                    llm_supports_native_tools = True
                elif isinstance(self._llm, OpenAICompatibleAsyncLLM):
                    tool_mode = "OpenAI"
                    tools = self._formatted_tools_openai
                    llm_supports_native_tools = True
                else:
                    logger.warning(
                        f"LLM type {type(self._llm)} not explicitly handled for tool mode determination."
                    )

                if llm_supports_native_tools and not tools:
                    logger.warning(
                        f"No tools available/formatted for '{tool_mode}' mode, despite MCP being enabled."
                    )

            weather_request = self._build_weather_forecast_request(input_data)
            if weather_request:
                logger.info(
                    "Prefetching weather forecast before forecast/detail reply: "
                    f"location={weather_request['location']} day={weather_request['day']}"
                )
                prefetched_weather = ""
                async for update in self._prefetch_weather_forecast(
                    weather_request["location"], weather_request["day"]
                ):
                    if update.get("type") == "weather_forecast_prefetch_result":
                        prefetched_weather = update.get("content", "")
                    else:
                        yield update

                if prefetched_weather:
                    logger.info("Answering directly from prefetched weather forecast.")
                    self._add_message(prefetched_weather, "assistant")
                    yield prefetched_weather
                    return

            if self._should_prefetch_komugi_context(input_data):
                logger.info("Prefetching Komugi context before household-state reply.")
                prefetched_context = ""
                async for update in self._prefetch_komugi_context():
                    if update.get("type") == "komugi_context_prefetch_result":
                        prefetched_context = update.get("content", "")
                    else:
                        yield update

                if prefetched_context:
                    context_message = {
                        "role": "system",
                        "content": (
                            "get_komugi_context was called for this turn. "
                            "Use the following Command Center context as the ground truth. "
                            "Do not call get_komugi_context again in this turn. "
                            "If the requested household fact is not present, say it cannot be confirmed. "
                            "Do not ask for date or location unless this context lacks the relevant information.\n\n"
                            f"{prefetched_context}"
                        ),
                    }
                    insert_at = max(len(messages) - 1, 0)
                    messages.insert(insert_at, context_message)
                    if tools:
                        tools = [
                            tool
                            for tool in tools
                            if tool.get("function", {}).get("name")
                            != "get_komugi_context"
                            and tool.get("name") != "get_komugi_context"
                        ]
                    direct_answer = self._build_komugi_context_answer(
                        input_data, prefetched_context
                    )
                    if direct_answer:
                        text_prompt = self._to_text_prompt(input_data).lower()
                        if self._is_local_disaster_question(text_prompt):
                            topic = self._local_disaster_topic(text_prompt)
                            if topic == "earthquake":
                                self._schedule_komugi_ui_event("earthquake")
                            else:
                                self._schedule_komugi_ui_event(
                                    "disaster",
                                    topic=topic,
                                )
                        logger.info("Answering directly from prefetched Komugi context.")
                        self._add_message(direct_answer, "assistant")
                        yield direct_answer
                        return

            fallback_answer, fallback_status = (
                await self._build_local_status_fallback_answer(input_data)
            )
            if fallback_answer:
                logger.info("Answering from Komugi local-status fallback.")
                text_prompt = self._to_text_prompt(input_data).lower()
                topic = self._local_disaster_topic(text_prompt)
                if topic == "earthquake":
                    self._schedule_komugi_ui_event(
                        "earthquake",
                        local_status=fallback_status,
                    )
                else:
                    self._schedule_komugi_ui_event(
                        "disaster",
                        topic=topic,
                        local_status=fallback_status,
                    )
                self._add_message(fallback_answer, "assistant")
                yield fallback_answer
                return

            if self._use_mcpp and tool_mode and not self._should_enable_mcp_tools(
                input_data
            ):
                logger.info(
                    "Skipping MCP tools for this turn; input does not require external context."
                )
                tools = None
                tool_mode = None

            if self._use_mcpp and tool_mode == "Claude":
                logger.debug(
                    f"Starting Claude tool interaction loop with {len(tools)} tools."
                )
                async for output in self._claude_tool_interaction_loop(
                    messages, tools if tools else []
                ):
                    yield output
                return
            elif self._use_mcpp and tool_mode == "OpenAI":
                logger.debug(
                    f"Starting OpenAI tool interaction loop with {len(tools)} tools."
                )
                async for output in self._openai_tool_interaction_loop(
                    messages, tools if tools else []
                ):
                    yield output
                return
            else:
                logger.info("Starting simple chat completion.")
                token_stream = self._llm.chat_completion(messages, self._system)
                complete_response = ""
                async for event in token_stream:
                    text_chunk = ""
                    if isinstance(event, dict) and event.get("type") == "text_delta":
                        text_chunk = event.get("text", "")
                    elif isinstance(event, str):
                        text_chunk = event
                    else:
                        continue
                    if text_chunk:
                        yield text_chunk
                        complete_response += text_chunk
                if complete_response:
                    self._add_message(complete_response, "assistant")

        return chat_with_memory

    async def chat(
        self,
        input_data: BatchInput,
    ) -> AsyncIterator[Union[SentenceOutput, Dict[str, Any]]]:
        """Run chat pipeline."""
        chat_func_decorated = self._chat_function_factory()
        async for output in chat_func_decorated(input_data):
            yield output

    def reset_interrupt(self) -> None:
        """Reset interrupt flag."""
        self._interrupt_handled = False

    def start_group_conversation(
        self, human_name: str, ai_participants: List[str]
    ) -> None:
        """Start a group conversation."""
        if not self._tool_prompts:
            logger.warning("Tool prompts dictionary is not set.")
            return

        other_ais = ", ".join(name for name in ai_participants)
        prompt_name = self._tool_prompts.get("group_conversation_prompt", "")

        if not prompt_name:
            logger.warning("No group conversation prompt name found.")
            return

        try:
            group_context = prompt_loader.load_util(prompt_name).format(
                human_name=human_name, other_ais=other_ais
            )
            self._memory.append({"role": "user", "content": group_context})
        except FileNotFoundError:
            logger.error(f"Group conversation prompt file not found: {prompt_name}")
        except KeyError as e:
            logger.error(f"Missing formatting key in group conversation prompt: {e}")
        except Exception as e:
            logger.error(f"Failed to load group conversation prompt: {e}")


def _join_url(base_url: str, path: str) -> str:
    base = str(base_url or "http://127.0.0.1:18000").rstrip("/") + "/"
    return urljoin(base, path.lstrip("/"))
