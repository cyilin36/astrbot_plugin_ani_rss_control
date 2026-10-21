"""ani-rss 指令回复的文本格式化。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_FIELD_LABELS = {
    "title": "标题",
    "url": "主 RSS",
    "type": "类型",
    "bgm": "BGM",
    "bgmUrl": "BGM",
    "subgroup": "字幕组",
    "season": "季",
    "offset": "偏移",
    "enable": "启用",
    "total": "总集数",
    "totalEpisodeNumber": "总集数",
    "match": "匹配",
    "exclude": "排除",
    "download_path": "下载路径模板",
    "customDownloadPathTemplate": "下载路径模板",
    "custom_download_path": "自定义下载路径",
    "customDownloadPath": "自定义下载路径",
    "download_new": "只下载最新集",
    "downloadNew": "只下载最新集",
    "omit": "遗漏检测",
    "global_exclude": "全局排除",
    "globalExclude": "全局排除",
    "upload": "自动上传",
    "message": "消息通知",
}

_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:token|key|api_?key|password|passwd|pwd|secret|sign|signature|auth|access_token)=)[^&#]*",
)

_EDITABLE_FIELDS_TEXT = (
    "title, url, type, bgm, subgroup, season, offset, enable, total, "
    "match, exclude, download_path, custom_download_path, download_new, "
    "omit, global_exclude, upload, message"
)

_AUTH_MODE_LABELS = {
    "api_key": "API Key",
    "token": "Token",
    "password": "账号密码",
    "none": "无（依赖 IP 白名单）",
}

_STATE_LABELS = {
    "downloading": "下载中",
    "seeding": "做种",
    "stoppedUP": "已停止",
    "stoppedDL": "已停止",
    "queuedUP": "排队",
    "queuedDL": "排队",
    "pausedUP": "已暂停",
    "pausedDL": "已暂停",
    "error": "错误",
    "missingFiles": "文件缺失",
    "unknown": "未知",
}


def format_help(version: str) -> str:
    """生成帮助文本。"""

    return f"""ani-rss 控制插件 v{version}
用法：/ani <子命令> [参数]

订阅
  /ani list [关键词] [page=1] [size=10]
  /ani info <订阅>
  /ani add <RSS> [type=] [bgm=] [subgroup=] [title=] [season=] [offset=] [enable=] [match=] [exclude=] [standby=标签|URL|偏移 ...] [override=true 确认]
  /ani edit <订阅> [字段=值 ...]
  /ani delete <订阅...> [files=true] [确认]

主 RSS
  /ani main add <RSS> [同上]
  /ani main show <订阅>
  /ani main set <订阅> <RSS>
  /ani main delete <订阅> [files=true] [确认]

备用 RSS
  /ani standby list <订阅>
  /ani standby add <订阅> <RSS> [label=] [offset=]
  /ani standby set <订阅> <序号|标签> [url=] [label=] [offset=]
  /ani standby delete <订阅> <序号|标签>
  /ani standby move <订阅> <序号|标签> <up|down|to=N>

其他
  /ani enable <订阅...>
  /ani disable <订阅...>
  /ani refresh [all|<订阅...>]
  /ani total <all|<订阅...>> [force=true]
  /ani rss <RSS> [type=] [bgm=] [subgroup=]
  /ani path <订阅>
  /ani downloads [关键词] [limit=10]
  /ani status
  /ani help

订阅可用完整 id、唯一 id 前缀或标题指定；标题后可加 #季，例如 葬送的芙莉莲#2。
删除订阅需追加「确认」。删除本地文件还需配置 allow_delete_files=true 并追加 files=true。
覆盖已有订阅需 override=true 与「确认」。
match/exclude 用 | 分隔；规则本身含竖线时可用 JSON 数组，例如 match=["a|b"]。
编辑可用字段：{_EDITABLE_FIELDS_TEXT}。"""


def format_subscription_list(data: dict[str, Any]) -> str:
    """格式化订阅列表。"""

    items = list(data.get("items") or [])
    total = int(data.get("total") or 0)
    all_total = int(data.get("all_total") or 0)
    page = int(data.get("page") or 1)
    pages = max(1, int(data.get("pages") or 1))
    keyword = str(data.get("keyword") or "").strip()

    if not items:
        if all_total == 0:
            return "当前没有订阅。可先用 /ani add <RSS地址> 添加。"
        if keyword:
            return f"未找到匹配「{keyword}」的订阅。"
        return "当前页没有订阅。"

    lines = [f"订阅列表（共 {total} 项，第 {page}/{pages} 页）"]
    if keyword:
        lines.append(f"关键词：{keyword}")
    lines.append("")

    start = (page - 1) * int(data.get("size") or len(items))
    for offset, ani in enumerate(items):
        lines.append(_format_subscription_line(ani, start + offset + 1))

    return "\n".join(lines)


def format_subscription_detail(ani: dict[str, Any], *, mask_urls: bool = False) -> str:
    """格式化订阅详情。"""

    lines = [
        f"标题：{_title(ani)}",
        f"ID：{ani.get('id') or '未知'}",
        f"状态：{_enable_label(ani.get('enable'))}",
        f"类型：{ani.get('type') or '未知'}",
        f"字幕组：{ani.get('subgroup') or '未知'}",
        f"进度：{_progress(ani)}",
        f"偏移：{_value(ani.get('offset'), 0)}",
        f"主 RSS：{_mask_url(str(ani.get('url') or ''), mask_urls)}",
    ]

    standby = _standby_entries(ani)
    lines.append(f"备用 RSS：{len(standby)} 项")
    for index, entry in enumerate(standby, start=1):
        lines.append(f"  {index}. {_format_standby_entry(entry, index, mask_urls)}")

    if ani.get("bgmUrl"):
        lines.append(f"BGM：{ani.get('bgmUrl')}")
    if ani.get("match"):
        lines.append(f"匹配：{_list_summary(ani.get('match'))}")
    if ani.get("exclude"):
        lines.append(f"排除：{_list_summary(ani.get('exclude'))}")
    if ani.get("customDownloadPathTemplate"):
        lines.append(f"下载路径模板：{ani.get('customDownloadPathTemplate')}")
    if ani.get("releaseDate"):
        lines.append(f"发布日期：{ani.get('releaseDate')}")

    return "\n".join(lines)


def format_main_show(ani: dict[str, Any], *, mask_urls: bool = False) -> str:
    """格式化主 RSS 信息。"""

    return "\n".join(
        [
            f"标题：{_title(ani)}",
            f"ID：{ani.get('id') or '未知'}",
            f"主 RSS：{_mask_url(str(ani.get('url') or ''), mask_urls)}",
            f"备用 RSS：{len(_standby_entries(ani))} 项",
        ],
    )


def format_standby_list(ani: dict[str, Any], *, mask_urls: bool = False) -> str:
    """格式化备用 RSS 列表。"""

    standby = _standby_entries(ani)
    lines = [f"订阅：{_title(ani)}", f"备用 RSS（{len(standby)} 项）"]
    if not standby:
        lines.append("（无）")
        return "\n".join(lines)
    for index, entry in enumerate(standby, start=1):
        lines.append(f"{index}. {_format_standby_entry(entry, index, mask_urls)}")
    return "\n".join(lines)


def format_standby_change(
    data: dict[str, Any],
    *,
    mask_urls: bool = False,
) -> str:
    """格式化备用 RSS 变更结果。"""

    action = str(data.get("action") or "update")
    entry = data.get("entry") or data.get("removed") or {}
    index = int(data.get("index") or 0) + 1
    total = int(data.get("total") or 0)

    headers = {
        "add": f"已添加备用 RSS：第 {index} 项",
        "set": f"已修改备用 RSS：第 {index} 项",
        "delete": "已删除备用 RSS",
        "move": f"已调整备用 RSS 顺序：第 {index} 项",
    }
    if action == "move" and data.get("moved") is False:
        header = "备用 RSS 顺序未变化"
    else:
        header = headers.get(action, "已更新备用 RSS")
    lines = [header]
    if entry:
        lines.append(_format_standby_entry(entry, index, mask_urls))
    if data.get("moved") is False:
        lines.append("位置未变化。")
    lines.append(f"当前共 {total} 项。")
    if data.get("standby_enabled") is False and action in {"add", "set"}:
        lines.append(
            "注意：ani-rss 的备用 RSS 功能当前未开启，请到 ani-rss 设置中启用。"
        )
    return "\n".join(lines)


def format_add_result(data: dict[str, Any], *, mask_urls: bool = False) -> str:
    """格式化添加订阅结果。"""

    ani = data.get("ani") or {}
    standby = _standby_entries(ani)
    lines = [
        "已覆盖并添加订阅" if data.get("override") else "已添加订阅",
        f"标题：{_title(ani)}",
        f"ID：{ani.get('id') or '未知'}",
        f"主 RSS：{_mask_url(str(ani.get('url') or ''), mask_urls)}",
        f"备用 RSS：{len(standby)} 项",
    ]
    if standby and data.get("standby_enabled") is False:
        lines.append(
            "注意：ani-rss 的备用 RSS 功能当前未开启，请到 ani-rss 设置中启用。"
        )
    return "\n".join(lines)


def format_edit_result(data: dict[str, Any], *, mask_urls: bool = False) -> str:
    """格式化修改订阅结果。"""

    ani = data.get("ani") or {}
    changed = data.get("changed") or {}
    if not changed:
        return "没有字段发生变化。"

    lines = [f"已修改订阅：{_title(ani)}"]
    for field, values in changed.items():
        try:
            old, new = values
        except (TypeError, ValueError):
            old, new = None, values
        label = _FIELD_LABELS.get(str(field), str(field))
        lines.append(
            f"{label}：{_display_value(old, mask_urls)} -> {_display_value(new, mask_urls)}",
        )
    return "\n".join(lines)


def format_delete_preview(data: dict[str, Any]) -> str:
    """格式化删除预览。"""

    items = list(data.get("items") or [])
    delete_files = bool(data.get("delete_files"))
    file_text = "同时删除" if delete_files else "保留"
    lines = [
        f"将删除 {len(items)} 个订阅；本地文件：{file_text}",
        "",
    ]
    for ani in items:
        lines.append(f"- {_title(ani)} | ID：{ani.get('id') or '未知'}")
    lines.append("")
    lines.append("如确认删除，请追加「确认」。")
    if not delete_files:
        lines.append(
            "如需同时删除本地文件，请先开启插件配置 allow_delete_files，再追加 files=true 与「确认」。",
        )
    return "\n".join(lines)


def format_delete_result(data: dict[str, Any]) -> str:
    """格式化删除结果。"""

    items = list(data.get("items") or [])
    delete_files = bool(data.get("delete_files"))
    suffix = "本地文件已请求删除" if delete_files else "本地文件已保留"
    lines = [f"已删除 {len(items)} 个订阅；{suffix}。"]
    for ani in items:
        lines.append(f"- {_title(ani)} | ID：{ani.get('id') or '未知'}")
    return "\n".join(lines)


def format_enable_result(data: dict[str, Any], *, enabled: bool) -> str:
    """格式化批量启用/停用结果。"""

    items = list(data.get("items") or [])
    action = "启用" if enabled else "停用"
    lines = [f"已{action} {len(items)} 个订阅："]
    for ani in items:
        lines.append(f"- {_title(ani)} | ID：{ani.get('id') or '未知'}")
    return "\n".join(lines)


def format_refresh_result(data: dict[str, Any]) -> str:
    """格式化刷新结果。"""

    if data.get("scope") == "all":
        return "已提交刷新全部订阅。"
    items = list(data.get("items") or [])
    lines = [f"已提交刷新 {len(items)} 个订阅："]
    for ani in items:
        lines.append(f"- {_title(ani)} | ID：{ani.get('id') or '未知'}")
    return "\n".join(lines)


def format_total_result(data: dict[str, Any]) -> str:
    """格式化总集数更新结果。"""

    count = int(data.get("count") or 0)
    force = bool(data.get("force"))
    return f"已提交更新总集数：{count} 个订阅；force={'是' if force else '否'}。"


def format_rss_preview(ani: dict[str, Any], *, mask_urls: bool = False) -> str:
    """格式化 RSS 解析预览。"""

    standby = _standby_entries(ani)
    lines = [
        "RSS 解析结果（尚未添加）",
        f"标题：{_title(ani)}",
        f"类型：{ani.get('type') or '未知'}",
        f"字幕组：{ani.get('subgroup') or '未知'}",
        f"进度：{_progress(ani)}",
        f"偏移：{_value(ani.get('offset'), 0)}",
        f"主 RSS：{_mask_url(str(ani.get('url') or ''), mask_urls)}",
        f"备用 RSS：{len(standby)} 项",
    ]
    for index, entry in enumerate(standby, start=1):
        lines.append(f"  {index}. {_format_standby_entry(entry, index, mask_urls)}")
    if ani.get("bgmUrl"):
        lines.append(f"BGM：{ani.get('bgmUrl')}")
    return "\n".join(lines)


def format_path_result(data: dict[str, Any]) -> str:
    """格式化下载路径查询结果。"""

    ani = data.get("ani") or {}
    lines = [
        f"订阅：{_title(ani)}",
        f"ID：{ani.get('id') or '未知'}",
        f"下载路径：{data.get('path') or '未知'}",
        f"位置会变化：{'是' if data.get('change') else '否'}",
    ]
    return "\n".join(lines)


def format_downloads(data: dict[str, Any]) -> str:
    """格式化下载任务列表。"""

    items = list(data.get("items") or [])
    keyword = str(data.get("keyword") or "").strip()
    matched = int(data.get("matched") or 0)
    total = int(data.get("total") or 0)

    if not items:
        if keyword:
            return f"未找到匹配「{keyword}」的下载任务。"
        return "暂无下载任务。"

    lines = [f"下载任务（共 {total} 项，匹配 {matched} 项，显示 {len(items)} 项）"]
    for index, item in enumerate(items, start=1):
        lines.append(_format_download_line(item, index))
    return "\n".join(lines)


def format_status(data: dict[str, Any]) -> str:
    """格式化连接状态。"""

    auth_mode = str(data.get("auth_mode") or "none")
    lines = [
        "ani-rss 状态",
        f"地址：{data.get('base_url') or '未配置'}",
        f"鉴权：{_AUTH_MODE_LABELS.get(auth_mode, auth_mode)}",
    ]

    if not data.get("ping_ok"):
        lines.append("连接：失败")
        if data.get("error"):
            lines.append(f"原因：{data.get('error')}")
        return "\n".join(lines)

    lines.append("连接：正常")
    about = data.get("about")
    if isinstance(about, dict):
        if about.get("version"):
            lines.append(f"版本：{about.get('version')}")
        if about.get("latest"):
            latest = about.get("latest")
            update = "可更新" if about.get("update") else "已是最新"
            lines.append(f"最新：{latest}（{update}）")
    elif data.get("about_error"):
        lines.append(f"版本信息：获取失败（{data.get('about_error')}）")

    standby = data.get("standby_rss")
    if standby is True:
        lines.append("备用 RSS 功能：已开启")
    elif standby is False:
        lines.append("备用 RSS 功能：未开启")
    else:
        lines.append("备用 RSS 功能：未知")
    return "\n".join(lines)


def chunk_text(text: str, max_chars: int) -> list[str]:
    """按行把长文本拆分为多条消息。"""

    content = (text or "").strip()
    if not content:
        return []

    limit = max(1, int(max_chars))
    if len(content) <= limit:
        return [content]

    chunks: list[str] = []
    current = ""
    for line in content.splitlines():
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def mask_url(url: str, enabled: bool) -> str:
    """遮蔽 URL 中的敏感查询参数值。"""

    if not enabled or not url:
        return url
    return _SENSITIVE_QUERY_RE.sub(r"\1***", url)


def _format_subscription_line(ani: dict[str, Any], index: int) -> str:
    standby_count = len(_standby_entries(ani))
    return (
        f"{index}. {_title(ani)} [{_enable_label(ani.get('enable'))}] "
        f"{_progress(ani)} 备用{standby_count}\n"
        f"   ID：{_short_id(ani.get('id'))}"
    )


def _format_download_line(item: dict[str, Any], index: int) -> str:
    name = str(item.get("name") or "未命名任务")
    state = _STATE_LABELS.get(
        str(item.get("state") or "unknown"), str(item.get("state") or "未知")
    )
    progress = _format_progress(item.get("progress"))
    size = item.get("formatSize") or _human_size(item.get("size"))
    size_text = f" {size}" if size else ""
    return f"{index}. {name} [{state}] {progress}{size_text}"


def _format_standby_entry(
    entry: dict[str, Any],
    index: int,
    mask_urls: bool,
) -> str:
    label = str(entry.get("label") or "未知字幕组")
    offset = entry.get("offset", 0)
    url = _mask_url(str(entry.get("url") or ""), mask_urls)
    return f"{label} | 偏移 {offset} | {url}"


def _standby_entries(ani: dict[str, Any]) -> list[dict[str, Any]]:
    raw = ani.get("standbyRssList")
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        entries.append(
            {
                "label": str(item.get("label") or "未知字幕组"),
                "url": url,
                "offset": item.get("offset", 0),
            },
        )
    return entries


def _title(ani: dict[str, Any]) -> str:
    title = str(ani.get("title") or "（无标题）")
    season = ani.get("season")
    if isinstance(season, int) and not isinstance(season, bool):
        return f"{title} 第{season}季"
    return title


def _progress(ani: dict[str, Any]) -> str:
    current = ani.get("currentEpisodeNumber")
    total = ani.get("totalEpisodeNumber")
    if isinstance(total, int) and not isinstance(total, bool) and total > 0:
        return f"{current if current is not None else 0}/{total} 集"
    if isinstance(current, int) and not isinstance(current, bool):
        return f"{current} 集"
    return "集数未知"


def _enable_label(value: Any) -> str:
    if value is True:
        return "启用"
    if value is False:
        return "停用"
    return "未知"


def _short_id(value: Any) -> str:
    text = str(value or "未知")
    return text[:8] if len(text) > 8 else text


def _value(value: Any, default: Any = "") -> Any:
    return default if value is None else value


def _list_summary(value: Any, limit: int = 5) -> str:
    if not isinstance(value, list):
        return str(value)
    shown = [str(item) for item in value[:limit]]
    text = "、".join(shown) if shown else "（空）"
    if len(value) > limit:
        text += f" 等 {len(value)} 项"
    return text


def _display_value(value: Any, mask_urls: bool) -> str:
    if value is None:
        return "（空）"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        return "、".join(str(item) for item in value) if value else "（空）"
    text = str(value)
    if mask_urls and text.startswith(("http://", "https://")):
        return _mask_url(text, True)
    return text or "（空）"


def _mask_url(url: str, enabled: bool) -> str:
    return mask_url(url, enabled)


def _format_progress(value: Any) -> str:
    try:
        progress = float(value)
    except (TypeError, ValueError):
        return "进度未知"
    progress = min(100.0, max(0.0, progress))
    return f"{progress:.1f}%"


def _human_size(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    units: Iterable[str] = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return ""
