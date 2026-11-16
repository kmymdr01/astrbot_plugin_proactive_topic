"""主动话题插件（AstrBot）。

让机器人在私聊 / 群聊安静一段时间后主动开口：
- 触发频率、每日上限、活跃时段可调；
- 话题在「结合最近聊天记录」与「符合人设的全新话题」之间按概率随机；
- 可分别控制是否在私聊、群聊生效，并支持群白名单 / 黑名单。

实现要点：
- 通过监听所有消息，自动学习「机器人和谁聊过天」，无需手工填会话 ID；
- 每个会话独立计时，后台协程按间隔调度，只使用公开插件 API；
- 状态保存在 `data/plugin_data/<插件名>/state.json`，重启后继续。
"""

from __future__ import annotations

import asyncio
import difflib
import inspect
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:  # astrbot >= 4.x
    from astrbot.api import AstrBotConfig, logger
except ImportError:  # pragma: no cover - 兼容旧版本
    import logging

    from astrbot.core.config.astrbot_config import AstrBotConfig  # type: ignore[no-redef]

    logger = logging.getLogger("astrbot")

try:  # 插件数据目录（data/plugin_data/<插件名>）
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover - 极旧版本兜底

    def get_astrbot_data_path() -> str:  # type: ignore[misc]
        return "data"


from astrbot.api.event import AstrMessageEvent, filter

try:
    from astrbot.api.event import MessageChain
except ImportError:  # pragma: no cover - 兼容旧版本
    from astrbot.core.message.message_event_result import MessageChain  # type: ignore[no-redef]

from astrbot.api.star import Context, Star

try:  # 用于把指令消息排除在聊天上下文之外
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.filter.command_group import CommandGroupFilter

    _COMMAND_FILTER_TYPES: tuple[type, ...] = (CommandFilter, CommandGroupFilter)
except Exception:  # noqa: BLE001 - 极旧版本没有这些模块时退化为不区分
    _COMMAND_FILTER_TYPES = ()

PLUGIN_ID = "astrbot_plugin_proactive_topic"
STATE_FILENAME = "state.json"

TICK_SECONDS = 20
"""调度循环的最小检查间隔（秒）。"""

MAX_PER_TICK = 3
"""单次检查最多处理的会话数，避免同时向大量会话发消息。"""

DEFAULT_ACTIVE_HOURS = "08:00-23:30"
DEFAULT_PERSONA = "你是一个友善、自然、有点幽默感的聊天伙伴。"

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_TIME_RANGE_RE = re.compile(
    r"^(\d{1,2})(?:[:：](\d{1,2}))?\s*[-~—到至]\s*(\d{1,2})(?:[:：](\d{1,2}))?$"
)
_QUOTE_PAIRS = (
    ("“", "”"),
    ('"', '"'),
    ("「", "」"),
    ("『", "』"),
    ("'", "'"),
    ("‘", "’"),
)
_TOPIC_PREFIX_RE = re.compile(
    r"^(?:新?话题|回复|消息|内容|助手|assistant|bot|ai)\s*[:：]\s*",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _clock_to_minutes(hour: str, minute: str | None) -> int | None:
    """把 "8"/"8:30" 转成当天的分钟数，非法值返回 None。"""
    try:
        h = int(hour)
        m = int(minute) if minute else 0
    except (TypeError, ValueError):
        return None
    if not (0 <= h <= 24 and 0 <= m < 60):
        return None
    if h == 24 and m != 0:
        return None
    return h * 60 + m


def parse_active_hours(spec: str | None) -> list[tuple[int, int]]:
    """解析活跃时段，例如 "08:00-12:00,14:00-23:30"、"8-23"。

    返回 [(开始分钟, 结束分钟), ...]；空字符串表示全天生效。
    支持跨天区间（如 "22:00-02:00"）。
    """
    if not spec or not str(spec).strip():
        return []
    ranges: list[tuple[int, int]] = []
    for chunk in re.split(r"[,，;；\s]+", str(spec).strip()):
        if not chunk:
            continue
        match = _TIME_RANGE_RE.match(chunk)
        if not match:
            continue
        start = _clock_to_minutes(match.group(1), match.group(2))
        end = _clock_to_minutes(match.group(3), match.group(4))
        if start is None or end is None:
            continue
        ranges.append((start, end))
    return ranges


def in_active_window(spec: str | None, ts: float | None = None) -> bool:
    """判断某个时间点是否落在活跃时段内。"""
    ranges = parse_active_hours(spec)
    if not ranges:
        return True
    moment = datetime.fromtimestamp(ts if ts is not None else time.time())
    current = moment.hour * 60 + moment.minute
    for start, end in ranges:
        if start == end:
            return True
        if start < end:
            if start <= current < end:
                return True
        elif current >= start or current < end:
            return True
    return False


def next_window_open(spec: str | None, ts: float) -> float:
    """返回 ts 之后最近一个活跃时段的开始时间；没有时段限制时原样返回。"""
    ranges = parse_active_hours(spec)
    if not ranges:
        return ts
    if in_active_window(spec, ts):
        return ts
    moment = datetime.fromtimestamp(ts)
    best: float | None = None
    for offset in (0, 1):
        day = moment + timedelta(days=offset)
        for start, _ in ranges:
            candidate = day.replace(
                hour=start // 60,
                minute=start % 60,
                second=0,
                microsecond=0,
            ).timestamp()
            if candidate > ts and (best is None or candidate < best):
                best = candidate
    return best if best is not None else ts


def split_umo(umo: str) -> tuple[str, str, str]:
    """拆分 unified_msg_origin: platform_id:message_type:session_id。"""
    parts = str(umo or "").split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return str(umo or ""), "", ""


def humanize(seconds: float) -> str:
    """把秒数转成中文可读时长。"""
    total = max(0, int(seconds))
    if total < 60:
        return "不到 1 分钟"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时" + (f" {minutes} 分钟" if minutes else "")
    days, hours = divmod(hours, 24)
    return f"{days} 天" + (f" {hours} 小时" if hours else "")


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def normalize_text(text: str) -> str:
    """用于查重的归一化：去掉空白和标点。"""
    return re.sub(r"[\W_]+", "", str(text or "").lower())


# ---------------------------------------------------------------------------
# 会话状态
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    """一个会话（私聊或群聊）的运行时状态，可被 JSON 序列化。"""

    umo: str
    platform_id: str = ""
    session_id: str = ""
    is_group: bool = False
    group_id: str = ""
    title: str = ""
    enabled: bool | None = None
    """None 表示跟随全局配置；True/False 表示本会话单独开关。"""
    last_seen: float = 0.0
    last_user_at: float = 0.0
    last_sent_at: float = 0.0
    next_due_at: float = 0.0
    sent_day: str = ""
    sent_count: int = 0
    topics: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "umo": self.umo,
            "platform_id": self.platform_id,
            "session_id": self.session_id,
            "is_group": bool(self.is_group),
            "group_id": self.group_id,
            "title": self.title,
            "enabled": self.enabled,
            "last_seen": self.last_seen,
            "last_user_at": self.last_user_at,
            "last_sent_at": self.last_sent_at,
            "next_due_at": self.next_due_at,
            "sent_day": self.sent_day,
            "sent_count": int(self.sent_count),
            "topics": self.topics[-8:],
            "messages": self.messages[-20:],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        state = cls(umo=str(data.get("umo") or ""))
        state.platform_id = str(data.get("platform_id") or "")
        state.session_id = str(data.get("session_id") or "")
        state.is_group = bool(data.get("is_group"))
        state.group_id = str(data.get("group_id") or "")
        state.title = str(data.get("title") or "")
        enabled = data.get("enabled")
        state.enabled = enabled if isinstance(enabled, bool) else None
        state.last_seen = _as_float(data.get("last_seen"))
        state.last_user_at = _as_float(data.get("last_user_at"))
        state.last_sent_at = _as_float(data.get("last_sent_at"))
        state.next_due_at = _as_float(data.get("next_due_at"))
        state.sent_day = str(data.get("sent_day") or "")
        state.sent_count = max(0, int(_as_float(data.get("sent_count"))))
        topics = data.get("topics")
        if isinstance(topics, list):
            state.topics = [str(item) for item in topics if str(item).strip()][-8:]
        messages = data.get("messages")
        if isinstance(messages, list):
            state.messages = [
                item
                for item in messages
                if isinstance(item, dict) and str(item.get("x") or "").strip()
            ][-20:]
        return state


def _as_float(value: Any) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class ProactiveTopicPlugin(Star):
    """主动发起话题的插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._sessions: dict[str, SessionState] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        """刚发出的主动消息，用于在下一次 LLM 请求里补充上下文。"""
        self._task: asyncio.Task | None = None
        self._dirty = False
        self._last_save = 0.0
        self._stopping = False
        self._sending: set[str] = set()
        """正在生成话题的会话，避免后台调度和手动指令同时发送。"""
        self._data_dir: Path | None = None
        """状态文件目录；测试或需要时可在外部覆盖。"""

    # -- 生命周期 ---------------------------------------------------------

    async def initialize(self) -> None:
        """插件启用时调用：恢复状态并启动后台调度。"""
        await self._load_state()
        self._task = asyncio.create_task(
            self._scheduler_loop(),
            name=f"{PLUGIN_ID}-scheduler",
        )
        private_on = self._cfg_bool("enable_private", True)
        group_on = self._cfg_bool("enable_group", True)
        logger.info(
            "主动话题插件已启动：私聊=%s 群聊=%s 已记录会话=%d 个",
            "开" if private_on else "关",
            "开" if group_on else "关",
            len(self._sessions),
        )

    async def terminate(self) -> None:
        """插件禁用 / 重载时调用：停止后台任务并保存状态。"""
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        try:
            await self._save_state()
        except Exception:  # noqa: BLE001
            logger.warning("主动话题插件退出时保存状态失败", exc_info=True)

    # -- 消息监听 ---------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        """记录会话和最近消息，供生成上下文相关话题使用。"""
        try:
            self._record_message(event)
        except Exception:  # noqa: BLE001
            logger.debug("记录消息失败", exc_info=True)

    def _record_message(self, event: AstrMessageEvent) -> None:
        # 即使插件总开关关闭也记录会话，方便开启后立即生效。
        sender_id = str(event.get_sender_id() or "")
        self_id = str(event.get_self_id() or "")
        if sender_id and self_id and sender_id == self_id:
            return  # 忽略机器人自己发出的消息

        umo = str(event.unified_msg_origin or "")
        if not umo:
            return

        text = str(event.message_str or "").strip()
        if not text:
            outline = ""
            try:
                outline = str(event.get_message_outline() or "").strip()
            except Exception:  # noqa: BLE001
                outline = ""
            text = outline
        if not text:
            return
        if len(text) > 500:
            text = text[:500]

        now = time.time()
        session = self._sessions.get(umo)
        if session is None:
            session = self._new_session(event)
            self._sessions[umo] = session
            logger.info("发现新会话 %s（%s）", umo, "群聊" if session.is_group else "私聊")

        session.last_seen = now
        session.last_user_at = now
        if session.next_due_at <= 0:
            session.next_due_at = now + max(
                self._cfg_int("idle_minutes", 10) * 60,
                random.uniform(0.3, 1.0) * self._base_interval(),
            )

        name = str(event.get_sender_name() or "").strip() or sender_id or "某人"
        if not session.is_group and not session.title:
            session.title = name[:24]

        # 指令内容不进上下文，避免“话题”指令本身被当作聊天内容。
        # AstrBot 会在进入插件前去掉唤醒前缀，因此通过 activated_handlers 判断是否是指令事件。
        if not self._is_command_event(event):
            session.messages.append({"t": now, "n": name[:24], "x": text})
            cap = max(4, self._cfg_int("context_message_count", 20))
            if len(session.messages) > cap:
                session.messages = session.messages[-cap:]

        self._dirty = True

    @staticmethod
    def _is_command_event(event: AstrMessageEvent) -> bool:
        """当前消息是否命中了任意插件的指令（用于把它排除在聊天上下文外）。"""
        if not _COMMAND_FILTER_TYPES:
            return False
        try:
            handlers = event.get_extra("activated_handlers", []) or []
        except Exception:  # noqa: BLE001
            return False
        for handler in handlers:
            for event_filter in getattr(handler, "event_filters", []) or []:
                if isinstance(event_filter, _COMMAND_FILTER_TYPES):
                    return True
        return False

    @filter.after_message_sent()
    async def on_bot_message_sent(self, event: AstrMessageEvent) -> None:
        """记录机器人自己最近发言的时间，避免刚回复完又马上主动开话题。"""
        try:
            session = self._sessions.get(str(event.unified_msg_origin or ""))
            if session is not None:
                session.last_sent_at = time.time()
                self._dirty = True
        except Exception:  # noqa: BLE001
            logger.debug("记录机器人发言时间失败", exc_info=True)

    def _new_session(self, event: AstrMessageEvent) -> SessionState:
        umo = str(event.unified_msg_origin or "")
        platform_id, _, session_id = split_umo(umo)
        is_group = not event.is_private_chat()
        group_id = str(event.get_group_id() or "") if is_group else ""
        name = str(event.get_sender_name() or "").strip()
        return SessionState(
            umo=umo,
            platform_id=platform_id or str(event.get_platform_id() or ""),
            session_id=str(event.get_session_id() or "") or session_id,
            is_group=is_group,
            group_id=group_id,
            title=(f"群聊 {group_id}" if is_group and group_id else name[:24]),
        )

    # -- 调度 -------------------------------------------------------------

    async def _scheduler_loop(self) -> None:
        await asyncio.sleep(5)
        while not self._stopping:
            try:
                await self._tick()
                if self._dirty and time.time() - self._last_save >= 30:
                    await self._save_state()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("主动话题调度出现异常")
            try:
                await asyncio.sleep(TICK_SECONDS)
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        if not self._cfg_bool("enabled", True):
            return

        now = time.time()
        self._prune_pending(now)
        self._prune_sessions(now)

        hours = self._cfg_str("active_hours", DEFAULT_ACTIVE_HOURS)
        if not in_active_window(hours, now):
            return

        due: list[SessionState] = []
        for session in self._sessions.values():
            if not self._session_allowed(session):
                continue
            if not self._session_recently_active(session, now):
                continue
            if session.umo in self._sending:
                continue
            if session.next_due_at <= 0:
                session.next_due_at = now + random.uniform(0.3, 1.0) * self._base_interval()
                self._dirty = True
            if session.next_due_at > now:
                continue

            self._prepare_day_counter(session, now)
            limit = self._cfg_int("max_per_day", 12)
            if limit > 0 and session.sent_count >= limit:
                # 今日已达上限：排到明天，或下一个活跃时段。
                tomorrow = (
                    datetime.fromtimestamp(now) + timedelta(days=1)
                ).replace(hour=0, minute=5, second=0, microsecond=0)
                target = max(now + 3600, tomorrow.timestamp())
                session.next_due_at = next_window_open(hours, target) + random.uniform(0, 300)
                self._dirty = True
                continue

            idle = max(0, self._cfg_int("idle_minutes", 10)) * 60
            quiet_for = now - max(
                session.last_user_at,
                session.last_seen,
                session.last_sent_at,
            )
            if idle and quiet_for < idle:
                session.next_due_at = now + (idle - quiet_for) + random.uniform(5, 60)
                self._dirty = True
                continue

            due.append(session)

        due.sort(key=lambda item: item.next_due_at)
        for session in due[:MAX_PER_TICK]:
            await self._send_topic(session)

    def _session_allowed(self, session: SessionState) -> bool:
        """是否允许在这个会话里主动发言（不含时间相关条件）。"""
        if session.enabled is False:
            return False
        if not self._cfg_bool("enabled", True):
            return False
        if session.is_group:
            if not self._cfg_bool("enable_group", True):
                return False
            whitelist = self._cfg_list("group_whitelist")
            blacklist = self._cfg_list("group_blacklist")
            if whitelist and not self._match_any(session, whitelist):
                return False
            if blacklist and self._match_any(session, blacklist):
                return False
        elif not self._cfg_bool("enable_private", True):
            return False
        return True

    def _session_recently_active(self, session: SessionState, now: float) -> bool:
        """会话最近是否有人说过话；长期无互动的会话不再主动打扰。"""
        days = self._cfg_int("session_active_days", 7)
        if days <= 0:
            return True
        last = max(session.last_user_at, session.last_seen)
        if last <= 0:
            return False
        return now - last <= days * 86400

    def _match_any(self, session: SessionState, entries: list[str]) -> bool:
        keys = {
            session.umo,
            session.session_id,
            session.group_id,
            f"{session.platform_id}:{session.group_id}" if session.group_id else "",
        }
        keys.discard("")
        return any(entry in keys for entry in entries)

    def _prepare_day_counter(self, session: SessionState, now: float) -> None:
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if session.sent_day != today:
            session.sent_day = today
            session.sent_count = 0
            self._dirty = True

    def _schedule_next(self, session: SessionState, now: float) -> None:
        hours = self._cfg_str("active_hours", DEFAULT_ACTIVE_HOURS)
        next_at = now + self._random_delay()
        if not in_active_window(hours, next_at):
            next_at = next_window_open(hours, next_at) + random.uniform(0, 120)
        session.next_due_at = next_at
        self._dirty = True

    # -- 生成与发送 -------------------------------------------------------

    async def _send_topic(self, session: SessionState) -> tuple[bool, str]:
        """生成并发送一条主动话题，返回 (是否成功, 说明)。"""
        if session.umo in self._sending:
            return False, "上一条消息还在生成中，请稍候。"
        self._sending.add(session.umo)
        try:
            return await self._send_topic_inner(session)
        finally:
            self._sending.discard(session.umo)

    async def _send_topic_inner(self, session: SessionState) -> tuple[bool, str]:
        now = time.time()
        self._prepare_day_counter(session, now)
        mode = self._pick_mode(session)

        text = ""
        try:
            for _ in range(2):
                candidate = await self._generate_topic(session, mode)
                if not candidate:
                    continue
                text = candidate
                if not self._looks_repeat(candidate, session.topics):
                    break
            if text and self._looks_repeat(text, session.topics):
                text = ""
        except asyncio.TimeoutError:
            session.next_due_at = now + 10 * 60 + random.uniform(0, 120)
            self._dirty = True
            return False, "生成话题超时，稍后再试。"
        except Exception as exc:  # noqa: BLE001
            logger.warning("主动话题生成失败（%s）：%s", session.umo, exc)
            session.next_due_at = now + 15 * 60 + random.uniform(0, 300)
            self._dirty = True
            return False, f"生成话题失败：{exc}"

        if not text:
            session.next_due_at = now + 15 * 60 + random.uniform(0, 300)
            self._dirty = True
            return False, "没有生成可用的话题，稍后再试。"

        try:
            delivered = await self.context.send_message(
                session.umo,
                MessageChain().message(text),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("主动话题发送失败（%s）：%s", session.umo, exc)
            session.next_due_at = now + max(600.0, self._base_interval()) + random.uniform(0, 120)
            self._dirty = True
            return False, f"发送失败：{exc}"

        if delivered is False:
            logger.warning("主动话题发送失败（%s）：找不到对应平台", session.umo)
            session.next_due_at = now + max(600.0, self._base_interval()) + random.uniform(0, 120)
            self._dirty = True
            return False, "发送失败：当前平台不可用。"

        session.last_sent_at = now
        session.sent_count += 1
        session.topics.append(text)
        session.topics = session.topics[-8:]
        self._pending[session.umo] = {"text": text, "ts": now}
        self._schedule_next(session, now)
        self._dirty = True
        # 发送成功立即落盘，避免重启后重复发送同一条话题。
        await self._save_state()

        scene = "群聊" if session.is_group else "私聊"
        logger.info("[主动话题] %s（%s，%s）%s", session.umo, scene, mode, text)
        return True, text

    def _pick_mode(self, session: SessionState) -> str:
        probability = clamp(self._cfg_int("context_probability", 50), 0, 100)
        if probability <= 0:
            return "persona"
        if not self._usable_context(session):
            return "persona"
        return "context" if random.uniform(0, 100) < probability else "persona"

    def _usable_context(self, session: SessionState) -> list[dict[str, Any]]:
        max_age = max(1, self._cfg_int("context_max_age_hours", 24)) * 3600
        cutoff = time.time() - max_age
        messages = [
            message
            for message in session.messages
            if isinstance(message, dict)
            and str(message.get("x") or "").strip()
            and _as_float(message.get("t")) >= cutoff
        ]
        minimum = max(1, self._cfg_int("context_min_messages", 3))
        if len(messages) < minimum:
            return []
        return messages[-max(4, self._cfg_int("context_message_count", 20)) :]

    async def _generate_topic(self, session: SessionState, mode: str) -> str:
        persona, _ = await self._resolve_persona(session)
        provider_id = await self._resolve_provider_id(session.umo)
        if not provider_id:
            raise RuntimeError("没有可用的对话模型，请先在 AstrBot 中配置并启用模型提供商")

        timeout = max(5, self._cfg_int("llm_timeout_seconds", 60))
        response = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=self._build_user_prompt(session, mode),
                system_prompt=self._build_system_prompt(session, persona, mode),
            ),
            timeout=timeout,
        )
        text = getattr(response, "completion_text", None) or ""
        return self._sanitize_topic(str(text))

    # -- 提示词 -----------------------------------------------------------

    def _build_system_prompt(self, session: SessionState, persona: str, mode: str) -> str:
        persona_text = (persona or "").strip() or DEFAULT_PERSONA
        scene = "群聊" if session.is_group else "私聊"
        limit = max(20, self._cfg_int("topic_max_chars", 120))
        rules = (
            "【当前任务】\n"
            f"你正在一个{scene}里和人聊天。现在聊天安静了一会儿，需要你主动开口，挑起一个话题。\n"
            "要求：\n"
            "1. 只输出你要发送的那一条消息本身，不要任何解释、前缀、引号、括号说明或 Markdown 标记。\n"
            f"2. 像真人聊天一样自然，1~2 句话，不超过 {limit} 字。\n"
            "3. 严格符合你的人设和说话风格。\n"
            "4. 不要提到“主动发起话题”“系统提示”“AI”“机器人”等字眼。\n"
        )
        if mode == "context":
            rules += "5. 不要说“我们刚才聊到”之类的元话术，直接把话题自然地接下去。\n"
        else:
            rules += "5. 不要照搬或改写最近已经聊过的话题。\n"

        extra = self._cfg_str("extra_system_prompt", "").strip()
        if extra:
            rules += f"\n【额外要求】\n{extra}\n"
        return f"{persona_text}\n\n{rules}"

    def _build_user_prompt(self, session: SessionState, mode: str) -> str:
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        weekday = WEEKDAYS[now.weekday()]

        if mode == "context":
            transcript = self._build_transcript(self._usable_context(session))
            return (
                f"现在是 {stamp}（{weekday}）。\n\n"
                f"【最近的聊天记录】\n{transcript}\n\n"
                "请从上面这段聊天里挑一个还没聊完、或者值得追问/回应的点，"
                "自然地接着说一句话，让对话继续下去。不要复述原话，也不要总结聊天内容。"
            )

        recent = "、".join(f"“{item}”" for item in session.topics[-5:]) or "（无）"
        scene = "群聊" if session.is_group else "私聊"
        return (
            f"现在是 {stamp}（{weekday}）。\n"
            f"场景：{scene}。\n"
            f"你最近已经聊过的话题：{recent}\n\n"
            "请发起一个与你人设相符、并且和上面这些话题都不同的新话题。"
            "可以是你的近况、正在做的事、一个好奇的问题，或者生活里的小事。"
            "直接说出那句话。"
        )

    def _build_transcript(self, messages: list[dict[str, Any]], budget: int = 1800) -> str:
        lines: list[str] = []
        used = 0
        for message in reversed(messages):
            text = str(message.get("x") or "").strip().replace("\n", " ")
            if not text:
                continue
            name = str(message.get("n") or "某人")[:16]
            moment = datetime.fromtimestamp(_as_float(message.get("t")))
            line = f"[{moment.strftime('%H:%M')}] {name}: {text}"
            if used + len(line) > budget:
                break
            lines.append(line)
            used += len(line)
        lines.reverse()
        return "\n".join(lines) or "（暂无）"

    def _sanitize_topic(self, text: str) -> str:
        content = str(text or "").strip()
        if not content:
            return ""
        for left, right in _QUOTE_PAIRS:
            if content.startswith(left) and content.endswith(right) and len(content) > len(left) + len(right):
                content = content[len(left) : -len(right)].strip()
                break
        content = _TOPIC_PREFIX_RE.sub("", content)
        content = re.sub(r"\s*\n+\s*", " ", content)
        content = re.sub(r"^[#>*\-•\s]+", "", content)
        limit = max(20, self._cfg_int("topic_max_chars", 120))
        if len(content) > limit:
            content = content[:limit].rstrip("，,、；;：:。. 　") + "…"
        return content.strip()

    def _looks_repeat(self, text: str, topics: list[str]) -> bool:
        normalized = normalize_text(text)
        if not normalized:
            return True
        for topic in topics[-5:]:
            other = normalize_text(topic)
            if not other:
                continue
            if normalized == other:
                return True
            if difflib.SequenceMatcher(None, normalized, other).ratio() >= 0.85:
                return True
        return False

    # -- 人格与模型 -------------------------------------------------------

    async def _resolve_persona(self, session: SessionState) -> tuple[str, str]:
        """按「插件指定人格 -> 会话人格 -> 全局默认人格」的顺序取人设。"""
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return "", ""

        pinned = self._cfg_str("persona_id", "").strip()
        if pinned:
            loaded = await self._load_persona(manager, pinned)
            if loaded:
                return loaded

        try:
            conv_manager = self.context.conversation_manager
            conversation_id = await self._maybe_await(
                conv_manager.get_curr_conversation_id(session.umo)
            )
            if conversation_id:
                conversation = await self._maybe_await(
                    conv_manager.get_conversation(session.umo, conversation_id)
                )
                persona_id = getattr(conversation, "persona_id", None) if conversation else None
                if persona_id and persona_id not in ("[%None]", "none"):
                    loaded = await self._load_persona(manager, persona_id)
                    if loaded:
                        return loaded
        except Exception:  # noqa: BLE001
            logger.debug("读取会话人格失败", exc_info=True)

        try:
            default = await self._maybe_await(manager.get_default_persona_v3(session.umo))
            if isinstance(default, dict):
                return (
                    str(default.get("prompt") or "").strip(),
                    str(default.get("name") or ""),
                )
        except Exception:  # noqa: BLE001
            logger.debug("读取默认人格失败", exc_info=True)
        return "", ""

    async def _load_persona(self, manager: Any, persona_id: str) -> tuple[str, str] | None:
        try:
            persona = await self._maybe_await(manager.get_persona(persona_id))
        except Exception:  # noqa: BLE001
            return None
        prompt = str(getattr(persona, "system_prompt", "") or "").strip()
        if not prompt:
            return None
        return prompt, str(getattr(persona, "persona_id", persona_id))

    async def _resolve_provider_id(self, umo: str) -> str | None:
        pinned = self._cfg_str("provider_id", "").strip()
        if pinned:
            return pinned
        try:
            provider_id = await self._maybe_await(
                self.context.get_current_chat_provider_id(umo=umo)
            )
            if provider_id:
                return str(provider_id)
        except Exception:  # noqa: BLE001
            logger.debug("获取会话模型失败", exc_info=True)
        try:
            provider = await self._maybe_await(self.context.get_using_provider_async(umo))
            if provider is not None:
                return str(provider.meta().id)
        except Exception:  # noqa: BLE001
            pass
        try:
            providers = self.context.get_all_providers() or []
            if providers:
                return str(providers[0].meta().id)
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        """兼容同步 / 异步两种实现。"""
        if inspect.isawaitable(value):
            return await value
        return value

    # -- 上下文注入 -------------------------------------------------------

    @filter.on_llm_request()
    async def inject_recent_topic(self, event: AstrMessageEvent, req: Any) -> None:
        """用户回复主动话题时，把那条消息作为临时背景告诉模型。"""
        try:
            if not self._cfg_bool("inject_proactive_context", True):
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            record = self._pending.get(umo)
            if not isinstance(record, dict):
                return
            age = time.time() - _as_float(record.get("ts"))
            if age > max(1, self._cfg_int("inject_context_minutes", 180)) * 60:
                self._pending.pop(umo, None)
                return
            text = str(record.get("text") or "").strip()
            self._pending.pop(umo, None)
            self._dirty = True
            if not text:
                return

            hint = (
                "（补充背景：你刚刚主动发过一条消息："
                f"「{text}」。如果用户这句话是在回应它，请结合它来理解。）"
            )
            part = self._make_text_part(hint)
            if part is not None and hasattr(req, "extra_user_content_parts"):
                req.extra_user_content_parts.append(part)
            else:
                req.system_prompt = (getattr(req, "system_prompt", "") or "") + "\n" + hint
        except Exception:  # noqa: BLE001
            logger.debug("注入主动消息背景失败", exc_info=True)

    @staticmethod
    def _make_text_part(text: str) -> Any:
        try:
            from astrbot.core.agent.message import TextPart

            part = TextPart(text=text)
            if hasattr(part, "mark_as_temp"):
                try:
                    part = part.mark_as_temp()
                except Exception:  # noqa: BLE001
                    pass
            return part
        except Exception:  # noqa: BLE001
            return None

    def _prune_pending(self, now: float) -> None:
        limit = max(1, self._cfg_int("inject_context_minutes", 180)) * 60
        for umo in [
            key
            for key, value in self._pending.items()
            if now - _as_float(value.get("ts")) > limit
        ]:
            self._pending.pop(umo, None)
            self._dirty = True

    def _prune_sessions(self, now: float) -> None:
        ttl = max(1, self._cfg_int("session_ttl_days", 30)) * 86400
        expired = [
            umo
            for umo, session in self._sessions.items()
            if session.last_seen and now - session.last_seen > ttl
        ]
        for umo in expired:
            self._sessions.pop(umo, None)
        if expired:
            self._dirty = True

    # -- 持久化 -----------------------------------------------------------

    def _storage_file(self) -> Path:
        """状态文件路径：data/plugin_data/<插件名>/state.json。"""
        if self._data_dir is None:
            self._data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_ID
        self._data_dir.mkdir(parents=True, exist_ok=True)
        return self._data_dir / STATE_FILENAME

    async def _load_state(self) -> None:
        path = self._storage_file()
        data: Any = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                logger.warning("读取主动话题状态失败：%s", path, exc_info=True)
                data = {}

        sessions = data.get("sessions") if isinstance(data, dict) else None
        if isinstance(sessions, dict):
            for umo, value in sessions.items():
                if not isinstance(value, dict):
                    continue
                value.setdefault("umo", umo)
                session = SessionState.from_dict(value)
                if session.umo:
                    self._sessions[session.umo] = session

        pending = data.get("pending") if isinstance(data, dict) else None
        if isinstance(pending, dict):
            self._pending = {
                str(key): value
                for key, value in pending.items()
                if isinstance(value, dict) and str(value.get("text") or "").strip()
            }

        self._normalize_schedule()

    def _normalize_schedule(self) -> None:
        now = time.time()
        base = self._base_interval()
        for session in self._sessions.values():
            if session.next_due_at <= 0:
                session.next_due_at = now + random.uniform(0.25, 1.0) * base
            elif session.next_due_at < now - 600:
                # 离线太久：不要一上线就集中补发。
                session.next_due_at = now + random.uniform(30, 300)

    async def _save_state(self) -> None:
        payload = {
            "sessions": {
                umo: session.to_dict() for umo, session in self._sessions.items()
            },
            "pending": {
                umo: value
                for umo, value in self._pending.items()
                if isinstance(value, dict) and str(value.get("text") or "").strip()
            },
        }
        path = self._storage_file()
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            self._dirty = False
            self._last_save = time.time()
        except Exception:  # noqa: BLE001
            logger.warning("保存主动话题状态失败", exc_info=True)

    # -- 指令 -------------------------------------------------------------

    @filter.command_group("话题")
    def topic(self):
        """主动话题管理指令组"""

    @topic.command("状态", alias={"status"})
    async def topic_status(self, event: AstrMessageEvent):
        session = self._ensure_session(event)
        yield event.plain_result(self._status_text(session))

    @topic.command("开启", alias={"on"})
    async def topic_on(self, event: AstrMessageEvent):
        if not self._can_manage_session(event):
            yield event.plain_result("只有机器人管理员才能在群里修改这个设置。")
            return
        session = self._ensure_session(event)
        session.enabled = True
        if session.next_due_at <= 0 or session.next_due_at < time.time():
            session.next_due_at = time.time() + random.uniform(0.3, 1.0) * self._base_interval()
        self._dirty = True
        await self._save_state()
        if not self._cfg_bool("enabled", True):
            yield event.plain_result(
                "已为本会话开启，但插件总开关处于关闭状态，请先在插件配置中启用。"
            )
            return
        yield event.plain_result("好，之后我会在这个会话里主动找话题聊。")

    @topic.command("关闭", alias={"off"})
    async def topic_off(self, event: AstrMessageEvent):
        if not self._can_manage_session(event):
            yield event.plain_result("只有机器人管理员才能在群里修改这个设置。")
            return
        session = self._ensure_session(event)
        session.enabled = False
        self._dirty = True
        await self._save_state()
        yield event.plain_result("好，这个会话里我不会再主动开口了。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @topic.command("立即", alias={"now"})
    async def topic_now(self, event: AstrMessageEvent):
        session = self._ensure_session(event)
        if not self._session_allowed(session):
            if not self._cfg_bool("enabled", True):
                yield event.plain_result("插件总开关已关闭，请先在插件配置中启用。")
            else:
                yield event.plain_result("这个会话的主动话题已关闭，请先发送「/话题 开启」。")
            return
        if not in_active_window(self._cfg_str("active_hours", DEFAULT_ACTIVE_HOURS)):
            yield event.plain_result("当前不在活跃时段内，已忽略（可修改配置中的「活跃时段」）。")
            return
        ok, result = await self._send_topic(session)
        if not ok:
            yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @topic.command("间隔", alias={"interval"})
    async def topic_interval(self, event: AstrMessageEvent, minutes: int):
        value = clamp(int(minutes), 1, 1440)
        self.config["interval_minutes"] = value
        await self._save_config()
        self._reschedule_all()
        yield event.plain_result(f"好，之后大约每 {value} 分钟主动开一次话题（带随机浮动）。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @topic.command("概率", alias={"probability"})
    async def topic_probability(self, event: AstrMessageEvent, percent: int):
        value = clamp(int(percent), 0, 100)
        self.config["context_probability"] = value
        await self._save_config()
        yield event.plain_result(
            f"好，之后的话题里约 {value}% 结合最近聊天，{100 - value}% 是全新话题。"
        )

    @staticmethod
    def _can_manage_session(event: AstrMessageEvent) -> bool:
        """私聊任何人都能改；群聊只允许机器人管理员。"""
        return event.is_private_chat() or event.is_admin()

    def _ensure_session(self, event: AstrMessageEvent) -> SessionState:
        umo = str(event.unified_msg_origin or "")
        session = self._sessions.get(umo)
        if session is None:
            session = self._new_session(event)
            session.next_due_at = time.time() + random.uniform(0.3, 1.0) * self._base_interval()
            self._sessions[umo] = session
            self._dirty = True
        return session

    def _status_text(self, session: SessionState) -> str:
        enabled = self._cfg_bool("enabled", True)
        allowed = self._session_allowed(session)
        lines = ["📣 主动话题 · 状态", ""]
        lines.append(f"总开关：{'已开启' if enabled else '已关闭'}")
        lines.append(
            "生效范围：私聊 {} / 群聊 {}".format(
                "开" if self._cfg_bool("enable_private", True) else "关",
                "开" if self._cfg_bool("enable_group", True) else "关",
            )
        )
        if session.enabled is None:
            lines.append(f"本会话：跟随全局（当前{'开启' if allowed else '关闭'}）")
        else:
            lines.append(f"本会话：{'开启' if session.enabled else '关闭'}")

        base = self._cfg_int("interval_minutes", 90)
        jitter = self._cfg_int("interval_jitter_percent", 30)
        lines.append(f"触发频率：约每 {base} 分钟一次（浮动 ±{jitter}%）")
        probability = self._cfg_int("context_probability", 50)
        lines.append(f"话题比例：上下文相关 {probability}% / 人设新话题 {100 - probability}%")
        hours = self._cfg_str("active_hours", DEFAULT_ACTIVE_HOURS)
        lines.append(f"活跃时段：{hours.strip() or '全天'}")

        active_days = self._cfg_int("session_active_days", 7)
        active = self._session_recently_active(session, time.time())
        if allowed and not active:
            lines.append(f"下次触发：已暂停（超过 {active_days} 天无互动）")
        elif allowed and session.next_due_at > 0:
            lines.append(f"下次触发：约 {humanize(session.next_due_at - time.time())} 后")
        else:
            lines.append("下次触发：—")

        limit = self._cfg_int("max_per_day", 12)
        self._prepare_day_counter(session, time.time())
        lines.append(f"今日已发：{session.sent_count} / {limit if limit > 0 else '不限'}")
        if session.topics:
            lines.append("最近话题：" + "；".join(session.topics[-3:]))
        return "\n".join(lines)

    async def _save_config(self) -> None:
        try:
            saver = getattr(self.config, "save_config", None)
            if saver is not None:
                await self._maybe_await(saver())
        except Exception:  # noqa: BLE001
            logger.warning("保存插件配置失败", exc_info=True)

    def _reschedule_all(self) -> None:
        now = time.time()
        for session in self._sessions.values():
            session.next_due_at = now + random.uniform(0.3, 1.0) * self._base_interval()
        self._dirty = True

    # -- 配置读取 ---------------------------------------------------------

    def _cfg_raw(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            value = default
        return default if value is None else value

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg_raw(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
        return bool(value)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(float(self._cfg_raw(key, default)))
        except (TypeError, ValueError):
            return int(default)

    def _cfg_str(self, key: str, default: str) -> str:
        value = self._cfg_raw(key, default)
        return str(value) if value is not None else default

    def _cfg_list(self, key: str, default: list[str] | None = None) -> list[str]:
        value = self._cfg_raw(key, default if default is not None else [])
        if isinstance(value, str):
            return [item.strip() for item in re.split(r"[,，\s]+", value) if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    def _base_interval(self) -> float:
        return max(1, self._cfg_int("interval_minutes", 90)) * 60.0

    def _random_delay(self) -> float:
        jitter = clamp(self._cfg_int("interval_jitter_percent", 30), 0, 80) / 100.0
        return self._base_interval() * random.uniform(1 - jitter, 1 + jitter)
