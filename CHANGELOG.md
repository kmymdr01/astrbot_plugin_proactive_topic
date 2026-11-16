# Changelog

## v1.1.1 — 2026-11-02

### Added

- 新增 `session_active_days`：超过 N 天没有任何人发言的会话自动暂停主动话题，避免在废弃会话里持续消耗 token。
- `/话题 状态` 会显示「已暂停（超过 N 天无互动）」。

### Fixed

- 修正 README 中关于状态存储方式的过时说明（KV → JSON 文件）。

## v1.1.0 — 2026-11-02

### Fixed

- 兼容 AstrBot 4.5.x：
  - 移除 `PermissionType.GROUP_ADMIN`（该版本不存在），群聊开关改为手动权限判断；
  - 不再依赖 `Star.logger`，改用 `astrbot.api.logger`；
  - 不再依赖插件 KV 存储，状态改存 `data/plugin_data/astrbot_plugin_proactive_topic/state.json`。

### Added

- 状态文件原子写入；发送成功后立即落盘，避免重启后重复发送同一条话题。

## v1.0.0 — 2026-11-02

### Added

- 主动发起话题：每个会话独立计时，支持随机浮动、每日上限、活跃时段；
- 「结合最近聊天」与「符合人设的全新话题」两种模式，概率可调；
- 私聊 / 群聊分别开关，支持群白名单 / 黑名单；
- 自动学习会话、读取人格、话题相似度去重；
- 用户回复主动消息时补充上下文；
- `/话题` 指令组与 WebUI 配置面板。
