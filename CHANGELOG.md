# Changelog

本文件记录插件版本变更。

## 0.1.0-beta4 - 2026-10-22

### 变更

- 鉴权方式统一为 ani-rss API Key，`api_key` 改为必填项。
- 移除 `token`、`username`、`password` 配置，以及 Token 鉴权、账号密码登录与自动重试逻辑。
- 删除订阅不再需要二次确认，`/ani delete` 与 `/ani main delete` 会立即执行。
- 保留 `override=true` 的确认机制，以及 `files=true` 删除本地文件时的 `allow_delete_files` 配置保护。
- 同步更新 README 中的配置与删除指令说明。

## 0.1.0-beta3 - 2026-10-22

### 新增

- 新增 `/ani search`，按关键词或季度搜索 Mikan、AniBT、AnimeGarden 片源，结果带 `ref` 引用。
- 新增 `/ani groups`，按 `ref` 查看字幕组、RSS 与剧集列表。
- 新增 `/ani magnet`，查询指定字幕组某一集的磁力链与种子链接。
- 新增 `/ani sub`，直接订阅搜索结果中的指定字幕组，复用现有 RSS 解析与添加流程。
- `ani_rss_query` 新增 `search`、`groups`、`magnet` 动作；`ani_rss_subscription` 新增 `add_group` 动作。
- 新增 `search_limit` 配置项，控制片源搜索结果条数。

### 变更

- `rss_timeout` 同时用于片源搜索请求。

## 0.1.0-beta2 - 2026-10-21

### 新增

- 注册 3 个 AstrBot LLM 工具：`ani_rss_query`、`ani_rss_subscription`、`ani_rss_standby`，覆盖全部指令能力。
- 新增 README 与 CHANGELOG。

### 修复

- 修复 `llm_executor` 直接调用指令组处理器时的 TypeError 与空回复。
- 单订阅指令支持标题包含空格。
- 修复 `/ani standby move ... to=N` 的参数解析。
- 写操作前检测缺少 `title` / `season` 的订阅并给出提示。
- `setAni` 支持 `move` 参数；补充 RSS 类型前置校验与下载状态映射。

### 变更

- `metadata.yaml` 的 `astrbot_version` 下限调整为 `>=4.26.8`。
- 补充帮助文本与配置提示。

## 0.1.0-beta1 - 2026-10-21

### 新增

- 首个版本：ani-rss 订阅、主 RSS、备用 RSS 的查询与增删改、启用停用、刷新、总集数更新、RSS 解析预览、下载路径与下载任务查询。

### 修复

- 修复嵌套指令组注册方式。
