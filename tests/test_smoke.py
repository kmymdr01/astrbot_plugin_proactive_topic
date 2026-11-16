"""用轻量桩模块对 main.py 做冒烟测试（不依赖真实 AstrBot）。

运行：python tests/test_smoke.py
"""

from __future__ import annotations

import asyncio
import enum
import importlib.util
import pathlib
import sys
import tempfile
import time
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "main.py"


# ---------------------------------------------------------------------------
# 构造 astrbot 桩
# ---------------------------------------------------------------------------


class PermissionType(enum.Flag):
    # 模拟 AstrBot 4.5.x：只有 ADMIN / MEMBER，没有 GROUP_ADMIN。
    ADMIN = enum.auto()
    MEMBER = enum.auto()


class RegisteringCommandable:
    def __init__(self, group_name):
        self.group_name = group_name

    def command(self, name, alias=None, **kwargs):
        def decorator(func):
            func.__command__ = (self.group_name, name)
            return func

        return decorator

    def group(self, name, **kwargs):
        return RegisteringCommandable(f"{self.group_name} {name}")


class _Filter:
    PermissionType = PermissionType

    class EventMessageType(enum.Flag):
        GROUP_MESSAGE = enum.auto()
        PRIVATE_MESSAGE = enum.auto()
        OTHER_MESSAGE = enum.auto()
        ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE

    def command(self, name, alias=None, **kwargs):
        def decorator(func):
            func.__command__ = (name,)
            return func

        return decorator

    def command_group(self, name, alias=None, **kwargs):
        def decorator(func):
            return RegisteringCommandable(name)

        return decorator

    def permission_type(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def event_message_type(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def on_llm_request(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def after_message_sent(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator


class MessageChain:
    def __init__(self, chain=None):
        self.chain = chain or []

    def message(self, text):
        self.chain.append(types.SimpleNamespace(text=text))
        return self


class Star:
    """模拟 AstrBot 4.5.x 的 Star：没有 logger，也没有插件 KV。"""

    def __init__(self, context, config=None):
        self.context = context
        self.config = config


def install_stubs():
    import logging

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("test")

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = object
    event_mod.MessageChain = MessageChain
    event_mod.filter = _Filter()

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = Star

    class TextPart:
        def __init__(self, text):
            self.text = text

        def mark_as_temp(self):
            self.temp = True
            return self

    core_mod = types.ModuleType("astrbot.core")
    agent_mod = types.ModuleType("astrbot.core.agent")
    message_mod = types.ModuleType("astrbot.core.agent.message")
    message_mod.TextPart = TextPart

    utils_mod = types.ModuleType("astrbot.core.utils")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_data_path = lambda: str(pathlib.Path(tempfile.gettempdir()) / "dsh_astrbot_test")

    star_pkg = types.ModuleType("astrbot.core.star")
    filter_pkg = types.ModuleType("astrbot.core.star.filter")
    command_mod = types.ModuleType("astrbot.core.star.filter.command")
    command_group_mod = types.ModuleType("astrbot.core.star.filter.command_group")

    class CommandFilter:
        pass

    class CommandGroupFilter:
        pass

    command_mod.CommandFilter = CommandFilter
    command_group_mod.CommandGroupFilter = CommandGroupFilter

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_mod,
            "astrbot.api.star": star_mod,
            "astrbot.core": core_mod,
            "astrbot.core.agent": agent_mod,
            "astrbot.core.agent.message": message_mod,
            "astrbot.core.utils": utils_mod,
            "astrbot.core.utils.astrbot_path": path_mod,
            "astrbot.core.star": star_pkg,
            "astrbot.core.star.filter": filter_pkg,
            "astrbot.core.star.filter.command": command_mod,
            "astrbot.core.star.filter.command_group": command_group_mod,
        }
    )


def load_plugin_module():
    spec = importlib.util.spec_from_file_location("proactive_topic_main", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------


class FakePersona:
    def __init__(self, system_prompt="你是猫娘，说话轻声细语", persona_id="cat"):
        self.system_prompt = system_prompt
        self.persona_id = persona_id


class FakePersonaManager:
    async def get_default_persona_v3(self, umo=None):
        return {"prompt": "你是猫娘，说话轻声细语", "name": "cat"}

    async def get_persona(self, persona_id):
        if persona_id == "cat":
            return FakePersona()
        raise ValueError(persona_id)


class FakeConversationManager:
    async def get_curr_conversation_id(self, umo):
        return "cid-1"

    async def get_conversation(self, umo, cid):
        return types.SimpleNamespace(persona_id=None)


class FakeProvider:
    def meta(self):
        return types.SimpleNamespace(id="fake-provider")


class FakeContext:
    def __init__(self):
        self.persona_manager = FakePersonaManager()
        self.conversation_manager = FakeConversationManager()
        self.sent = []
        self.prompts = []
        self.llm_text = "今天的风好大，你那边降温了吗？"
        self.send_result = True

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return self.send_result

    async def get_current_chat_provider_id(self, umo=None):
        return "fake-provider"

    async def llm_generate(self, **kwargs):
        self.prompts.append(kwargs)
        return types.SimpleNamespace(completion_text=self.llm_text)

    def get_all_providers(self):
        return [FakeProvider()]


class FakeEvent:
    def __init__(
        self,
        umo="test-1:GroupMessage:100200",
        text="今天加班到十点，好累",
        name="小明",
        sender="1001",
        group="100200",
        private=False,
    ):
        self.unified_msg_origin = umo
        self.message_str = text
        self._name = name
        self._sender = sender
        self._group = group
        self._private = private

    def get_sender_id(self):
        return self._sender

    def get_self_id(self):
        return "9999"

    def get_sender_name(self):
        return self._name

    def get_group_id(self):
        return self._group

    def get_session_id(self):
        return self.unified_msg_origin.split(":")[-1]

    def get_platform_id(self):
        return "test-1"

    def is_private_chat(self):
        return self._private

    def get_message_outline(self):
        return self.message_str

    def plain_result(self, text):
        return ("plain", text)


def base_config(**overrides):
    config = {
        "enabled": True,
        "enable_private": True,
        "enable_group": True,
        "group_whitelist": [],
        "group_blacklist": [],
        "interval_minutes": 90,
        "interval_jitter_percent": 0,
        "context_probability": 50,
        "idle_minutes": 0,
        "session_active_days": 7,
        "active_hours": "",
        "max_per_day": 12,
        "topic_max_chars": 120,
        "context_message_count": 20,
        "context_min_messages": 3,
        "context_max_age_hours": 24,
        "provider_id": "",
        "persona_id": "",
        "extra_system_prompt": "",
        "inject_proactive_context": True,
        "inject_context_minutes": 180,
        "llm_timeout_seconds": 10,
        "session_ttl_days": 30,
    }
    config.update(overrides)
    return config


TEST_DATA_DIR = pathlib.Path(tempfile.mkdtemp(prefix="proactive_topic_test_"))


async def make_plugin(module, fresh=True, **overrides):
    plugin = module.ProactiveTopicPlugin(FakeContext(), base_config(**overrides))
    plugin._data_dir = TEST_DATA_DIR
    if fresh:
        (TEST_DATA_DIR / "state.json").unlink(missing_ok=True)
    await plugin._load_state()
    return plugin


def force_due(plugin, umo="test-1:GroupMessage:100200"):
    session = plugin._sessions[umo]
    session.next_due_at = time.time() - 1
    session.last_user_at = time.time() - 3600
    session.last_seen = time.time() - 3600
    return session


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


async def test_helpers(module):
    assert module.parse_active_hours("08:00-12:00,14:00-23:30") == [(480, 720), (840, 1410)]
    assert module.parse_active_hours("8-23") == [(480, 1380)]
    assert module.parse_active_hours("") == []
    assert module.in_active_window("", time.time()) is True
    assert module.in_active_window("08:00-23:30", time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, -1))) is True
    assert module.in_active_window("08:00-23:30", time.mktime((2026, 1, 1, 2, 0, 0, 0, 0, -1))) is False
    # 跨天
    assert module.in_active_window("22:00-02:00", time.mktime((2026, 1, 1, 1, 0, 0, 0, 0, -1))) is True
    assert module.in_active_window("22:00-02:00", time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, -1))) is False
    # 下一次窗口
    two_am = time.mktime((2026, 1, 1, 2, 0, 0, 0, 0, -1))
    nxt = module.next_window_open("08:00-23:30", two_am)
    assert module.in_active_window("08:00-23:30", nxt)
    assert module.split_umo("a:GroupMessage:b") == ("a", "GroupMessage", "b")
    assert module.humanize(90) == "1 分钟"
    assert module.humanize(3700).startswith("1 小时")
    print("helpers ok")


async def test_record_and_send(module):
    plugin = await make_plugin(module, context_probability=0)
    event = FakeEvent()
    plugin._record_message(event)
    session = plugin._sessions[event.unified_msg_origin]
    assert session.is_group and session.group_id == "100200"
    assert len(session.messages) == 1

    force_due(plugin)
    await plugin._tick()
    assert len(plugin.context.sent) == 1, "应该发送了一条主动消息"
    umo, chain = plugin.context.sent[0]
    assert umo == event.unified_msg_origin
    assert chain.chain[0].text == plugin.context.llm_text == session.topics[-1]
    assert session.sent_count == 1
    assert session.next_due_at > time.time()

    # 持久化 + 重新加载
    await plugin._save_state()
    reloaded = await make_plugin(module, fresh=False)
    assert event.unified_msg_origin in reloaded._sessions
    assert reloaded._sessions[event.unified_msg_origin].topics[-1] == plugin.context.llm_text
    assert event.unified_msg_origin in reloaded._pending
    print("record/send/persist ok")


async def test_group_toggle(module):
    plugin = await make_plugin(module, enable_group=False)
    plugin._record_message(FakeEvent())
    force_due(plugin)
    await plugin._tick()
    assert plugin.context.sent == [], "群聊关闭时不应发送"

    # 本会话单独关闭
    plugin = await make_plugin(module, enable_group=True)
    plugin._record_message(FakeEvent())
    plugin._sessions["test-1:GroupMessage:100200"].enabled = False
    force_due(plugin)
    await plugin._tick()
    assert plugin.context.sent == [], "本会话关闭时不应发送"

    # 白名单外的群
    plugin = await make_plugin(module, group_whitelist=["999999"])
    plugin._record_message(FakeEvent())
    force_due(plugin)
    await plugin._tick()
    assert plugin.context.sent == [], "白名单外的群不应发送"
    print("group toggle ok")


async def test_private_toggle(module):
    plugin = await make_plugin(module, enable_private=False)
    event = FakeEvent(
        umo="test-1:FriendMessage:1001",
        name="小红",
        sender="1001",
        group="",
        private=True,
    )
    plugin._record_message(event)
    assert not plugin._sessions[event.unified_msg_origin].is_group
    force_due(plugin, event.unified_msg_origin)
    await plugin._tick()
    assert plugin.context.sent == []

    plugin = await make_plugin(module, enable_private=True)
    plugin._record_message(event)
    force_due(plugin, event.unified_msg_origin)
    await plugin._tick()
    assert len(plugin.context.sent) == 1
    print("private toggle ok")


async def test_mode_selection(module):
    plugin = await make_plugin(module, context_probability=0)
    plugin._record_message(FakeEvent())
    session = plugin._sessions["test-1:GroupMessage:100200"]
    assert plugin._pick_mode(session) == "persona"

    plugin = await make_plugin(module, context_probability=100)
    for index in range(3):
        plugin._record_message(FakeEvent(text=f"第 {index} 条消息"))
    session = plugin._sessions["test-1:GroupMessage:100200"]
    assert plugin._pick_mode(session) == "context"
    prompt = plugin._build_user_prompt(session, "context")
    assert "最近的聊天记录" in prompt and "第 2 条消息" in prompt
    assert "最近的聊天记录" not in plugin._build_user_prompt(session, "persona")

    # 上下文太少时退回人设模式
    plugin = await make_plugin(module, context_probability=100)
    plugin._record_message(FakeEvent(text="只有一条"))
    session = plugin._sessions["test-1:GroupMessage:100200"]
    assert plugin._pick_mode(session) == "persona"
    print("mode selection ok")


async def test_duplicate_skip(module):
    plugin = await make_plugin(module, context_probability=0)
    plugin._record_message(FakeEvent())
    session = plugin._sessions["test-1:GroupMessage:100200"]
    session.topics = ["今天的风好大，你那边降温了吗？"]
    force_due(plugin)
    ok, message = await plugin._send_topic(session)
    assert ok is False and "没有生成" in message
    assert plugin.context.sent == []
    print("duplicate skip ok")


async def test_injection(module):
    plugin = await make_plugin(module, context_probability=0)
    plugin._record_message(FakeEvent())
    force_due(plugin)
    await plugin._tick()
    assert len(plugin._pending) == 1

    event = FakeEvent(text="是啊，好冷")
    request = types.SimpleNamespace(system_prompt="base", extra_user_content_parts=[])
    await plugin.inject_recent_topic(event, request)
    assert plugin._pending == {}
    assert len(request.extra_user_content_parts) == 1
    part = request.extra_user_content_parts[0]
    assert "你刚刚主动发过一条消息" in part.text

    # 过期的 pending 应被丢弃
    plugin._pending[event.unified_msg_origin] = {"text": "旧消息", "ts": time.time() - 99999}
    await plugin.inject_recent_topic(event, request)
    assert plugin._pending == {}
    assert len(request.extra_user_content_parts) == 1
    print("injection ok")


async def test_active_days(module):
    plugin = await make_plugin(module, context_probability=0, session_active_days=7)
    plugin._record_message(FakeEvent())
    session = force_due(plugin)
    session.last_user_at = session.last_seen = time.time() - 10 * 86400
    await plugin._tick()
    assert plugin.context.sent == [], "超过 N 天无互动时应暂停"

    plugin = await make_plugin(module, context_probability=0, session_active_days=0)
    plugin._record_message(FakeEvent())
    session = force_due(plugin)
    session.last_user_at = session.last_seen = time.time() - 10 * 86400
    await plugin._tick()
    assert len(plugin.context.sent) == 1, "0 表示不限制"
    print("active days ok")


async def test_command_status(module):
    plugin = await make_plugin(module)
    plugin._record_message(FakeEvent())
    session = plugin._sessions["test-1:GroupMessage:100200"]
    text = plugin._status_text(session)
    assert "主动话题" in text and "下次触发" in text and "90" in text
    print("status ok")


async def test_prompt_sanitize(module):
    plugin = await make_plugin(module)
    assert plugin._sanitize_topic("“话题：今天吃了火锅，你呢？”") == "今天吃了火锅，你呢？"
    assert plugin._sanitize_topic("好\n\n今天天气不错") == "好 今天天气不错"
    plugin.config["topic_max_chars"] = 20
    assert len(plugin._sanitize_topic("啊" * 50)) <= 21
    print("sanitize ok")


async def main():
    install_stubs()
    module = load_plugin_module()
    await test_helpers(module)
    await test_record_and_send(module)
    await test_group_toggle(module)
    await test_private_toggle(module)
    await test_mode_selection(module)
    await test_duplicate_skip(module)
    await test_injection(module)
    await test_active_days(module)
    await test_command_status(module)
    await test_prompt_sanitize(module)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
