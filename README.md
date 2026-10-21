# astrbot_plugin_ani_rss_control

AstrBot 插件，用于通过 ani-rss 3.x 的 Web API 管理订阅、主 RSS、备用 RSS 与下载任务，并为 AstrBot 的 AI 注册 LLM 工具，使其可在对话中直接调用。

## 安装

将本仓库放入 AstrBot 的 `data/plugins/` 目录，或在 AstrBot WebUI 的插件页面通过仓库地址安装。安装后重载插件。

要求 AstrBot 4.26.8 及以上版本，ani-rss 3.x。

## 配置

在 AstrBot 插件配置页填写：

- `base_url`：ani-rss 服务地址，例如 `http://127.0.0.1:7789`。
- `api_key`：ani-rss API Key，推荐使用。
- `token`：ani-rss 登录 Token，可选。
- `username` / `password`：ani-rss 账号密码，可选。
- `authorized_uids`：允许使用本插件的用户，留空时仅 AstrBot 管理员可用。
- `allow_delete_files`：是否允许删除订阅时同时删除本地文件，默认关闭。

其余配置项见插件配置页的说明。

## 指令

订阅：

- `/ani list [关键词] [page=1] [size=10]`
- `/ani info <订阅>`
- `/ani add <RSS> [参数]`
- `/ani edit <订阅> [字段=值 ...] [move=true]`
- `/ani delete <订阅...> [files=true] [确认]`

主 RSS：

- `/ani main add <RSS> [参数]`
- `/ani main show <订阅>`
- `/ani main set <订阅> <RSS>`
- `/ani main delete <订阅...> [files=true] [确认]`

备用 RSS：

- `/ani standby list <订阅>`
- `/ani standby add <订阅> <RSS> [label=] [offset=]`
- `/ani standby set <订阅> <序号|标签> [url=] [label=] [offset=]`
- `/ani standby delete <订阅> <序号|标签>`
- `/ani standby move <订阅> <序号|标签> <up|down|to=N>`

维护：

- `/ani enable <订阅...>`
- `/ani disable <订阅...>`
- `/ani refresh [all|<订阅...>]`
- `/ani total <all|<订阅...>> [force=true]`
- `/ani rss <RSS> [type=] [bgm=] [subgroup=]`
- `/ani path <订阅>`
- `/ani downloads [关键词] [limit=10]`
- `/ani status`
- `/ani help`

订阅可用完整 id、唯一 id 前缀或标题指定；标题可含空格。删除订阅需追加「确认」。

## LLM 工具

插件注册 `ani_rss_query`、`ani_rss_subscription`、`ani_rss_standby` 三个工具，覆盖上述指令能力。使用需要模型支持 function calling；工具默认返回文本给模型组织回复。

## 许可证

MIT
