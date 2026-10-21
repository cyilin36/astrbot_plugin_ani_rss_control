# Changelog

本文件记录插件版本变更。

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
