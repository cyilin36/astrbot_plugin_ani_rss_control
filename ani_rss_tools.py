"""ani-rss 的 AstrBot LLM 工具定义。

三个聚合工具分别负责查询、订阅写操作和备用 RSS 写操作，用 action 区分
具体操作，避免注册过多工具。工具只做参数收敛与格式化，业务逻辑复用
``AniRssService``。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent

from .ani_rss_args import expand_selectors, parse_standby_specs
from .ani_rss_errors import AniRssValidationError
from .ani_rss_format import (
    format_add_result,
    format_delete_result,
    format_downloads,
    format_edit_result,
    format_enable_result,
    format_episode_result,
    format_path_result,
    format_refresh_result,
    format_rss_preview,
    format_search_results,
    format_source_groups,
    format_standby_change,
    format_standby_list,
    format_status,
    format_subscription_detail,
    format_subscription_list,
    format_total_result,
)
from .ani_rss_service import EDITABLE_FIELDS

_QUERY_ACTIONS = (
    "help",
    "status",
    "list",
    "info",
    "path",
    "downloads",
    "preview",
    "standby_list",
    "search",
    "groups",
    "magnet",
)
_SUBSCRIPTION_ACTIONS = (
    "add",
    "add_group",
    "edit",
    "delete",
    "set_enabled",
    "refresh",
    "update_total",
)
_STANDBY_ACTIONS = ("add", "set", "delete", "move")

# 工具参数名与服务层字段名的差异。
_EDIT_PARAM_FIELDS = {
    "title": "title",
    "url": "url",
    "rss_type": "type",
    "bgm": "bgm",
    "subgroup": "subgroup",
    "season": "season",
    "offset": "offset",
    "enable": "enable",
    "total": "total",
    "match": "match",
    "exclude": "exclude",
    "download_path": "download_path",
    "custom_download_path": "custom_download_path",
    "download_new": "download_new",
    "omit": "omit",
    "global_exclude": "global_exclude",
    "upload": "upload",
    "message": "message",
}

_OBJECT_SCHEMA = "object"
_STRING_SCHEMA = "string"
_NUMBER_SCHEMA = "number"
_BOOLEAN_SCHEMA = "boolean"
_ARRAY_SCHEMA = "array"


def _identity(value: Any) -> Any:
    return value


def _normalize_action(action: Any, allowed: tuple[str, ...], tool: str) -> str:
    value = str(action or "").strip().lower()
    if not value:
        raise AniRssValidationError(
            f"{tool} 缺少参数 action。可用值：{', '.join(allowed)}。",
        )
    if value not in allowed:
        raise AniRssValidationError(
            f"{tool} 不支持 action={value}。可用值：{', '.join(allowed)}。",
        )
    return value


def _require_value(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AniRssValidationError(f"缺少参数 {name}。")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _as_bool(value: Any, name: str, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "开", "是"}:
        return True
    if text in {"0", "false", "no", "off", "关", "否"}:
        return False
    raise AniRssValidationError(f"{name} 必须是布尔值（true/false）。")


def _as_int(
    value: Any,
    name: str,
    default: int | None = None,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if value is None or str(value).strip() == "":
        return default
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
        if not number.is_integer():
            raise ValueError
        parsed = int(number)
    except (TypeError, ValueError) as exc:
        raise AniRssValidationError(f"{name} 必须是整数。") from exc
    if minimum is not None and parsed < minimum:
        raise AniRssValidationError(f"{name} 不能小于 {minimum}。")
    if maximum is not None and parsed > maximum:
        raise AniRssValidationError(f"{name} 不能大于 {maximum}。")
    return parsed


def _as_float(
    value: Any,
    name: str,
    default: float | None = None,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if value is None or str(value).strip() == "":
        return default
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise AniRssValidationError(f"{name} 必须是数字。") from exc
    if minimum is not None and parsed < minimum:
        raise AniRssValidationError(f"{name} 不能小于 {minimum}。")
    if maximum is not None and parsed > maximum:
        raise AniRssValidationError(f"{name} 不能大于 {maximum}。")
    return parsed


def _as_str_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise AniRssValidationError(f"{name} 不是合法的 JSON 数组。") from exc
            if not isinstance(parsed, list):
                raise AniRssValidationError(f"{name} 必须是数组。")
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [text]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise AniRssValidationError(f"{name} 必须是字符串数组。")


def _as_standby_entries(value: Any) -> list[dict[str, Any]] | None:
    items = _as_str_list(value, "standby")
    if not items:
        return None
    return parse_standby_specs(items)


def _edit_raw_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return json.dumps([str(item) for item in value], ensure_ascii=False)
    return str(value)


def _edit_changes(values: dict[str, Any]) -> dict[str, str]:
    changes: dict[str, str] = {}
    for param, field_name in _EDIT_PARAM_FIELDS.items():
        if param not in values:
            continue
        value = values[param]
        if value is None:
            continue
        if field_name not in EDITABLE_FIELDS:
            continue
        changes[field_name] = _edit_raw_value(value)
    return changes


@dataclass
class _AniRssTool(FunctionTool):
    """工具基类，统一委托插件的鉴权、异常与截断逻辑。"""

    plugin: Any = None

    async def _run(
        self,
        event: AstrMessageEvent,
        operation: str,
        action: Callable[[], Awaitable[Any]],
        render: Callable[[Any], str],
    ) -> str:
        return await self.plugin._run_tool(event, operation, action, render)


@dataclass
class AniRssQueryTool(_AniRssTool):
    name: str = "ani_rss_query"
    description: str = (
        "查询 ani-rss 的连接状态、订阅列表、订阅详情、下载路径、下载任务、"
        "RSS 解析预览、备用 RSS 列表，或搜索片源、查看字幕组、查询单集磁力。"
        "action 必填，取值：help、status、list、info、path、downloads、preview、"
        "standby_list、search、groups、magnet。"
        "info/path/standby_list 需要 selector；list 可用 keyword/page/size；"
        "downloads 可用 keyword/limit；preview 需要 url，可选 rss_type/bgm/subgroup/standby。"
        "search 可用 keyword/source/season/bgm/limit，返回带 ref 的番剧列表；"
        "groups 需要 search 返回的 ref；magnet 需要 ref、group（序号或名称）"
        "以及 episode（集数）或 index（条目序号）。"
        "仅在用户明确询问 ani-rss 数据或需要搜索/预览时调用，不要主动轮询。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": _OBJECT_SCHEMA,
            "properties": {
                "action": {
                    "type": _STRING_SCHEMA,
                    "enum": list(_QUERY_ACTIONS),
                    "description": "要执行的查询类型。",
                },
                "selector": {
                    "type": _STRING_SCHEMA,
                    "description": "订阅完整 id、唯一 id 前缀或标题；标题可含空格。",
                },
                "keyword": {
                    "type": _STRING_SCHEMA,
                    "description": "订阅列表关键词，或 search 的番剧搜索关键词。",
                },
                "page": {
                    "type": _NUMBER_SCHEMA,
                    "description": "订阅列表页码，从 1 开始。",
                },
                "size": {
                    "type": _NUMBER_SCHEMA,
                    "description": "订阅列表每页条数，1-50，默认取插件配置。",
                },
                "limit": {
                    "type": _NUMBER_SCHEMA,
                    "description": "下载任务条数、搜索结果条数或字幕组剧集预览条数。",
                },
                "url": {
                    "type": _STRING_SCHEMA,
                    "description": "preview 时要解析的 RSS 地址。",
                },
                "rss_type": {
                    "type": _STRING_SCHEMA,
                    "enum": ["mikan", "ani-bt", "anime-garden", "other"],
                    "description": "preview 时的 RSS 类型。",
                },
                "bgm": {
                    "type": _STRING_SCHEMA,
                    "description": "preview 的 BGM 地址，或 search 时按 BGM 精确查找。",
                },
                "source": {
                    "type": _STRING_SCHEMA,
                    "enum": ["mikan", "ani-bt", "anime-garden"],
                    "description": "search 时的片源类型，默认取插件配置；"
                    "groups/magnet 传入裸标识时也用该参数指定来源。",
                },
                "season": {
                    "type": _STRING_SCHEMA,
                    "description": "search 时的季度，例如 2026春。",
                },
                "ref": {
                    "type": _STRING_SCHEMA,
                    "description": "search 返回的片源引用，例如 mikan:https://...。",
                },
                "group": {
                    "type": _STRING_SCHEMA,
                    "description": "magnet 时的字幕组序号（从 1 开始）或名称。",
                },
                "episode": {
                    "type": _NUMBER_SCHEMA,
                    "description": "magnet 时要查询的集数，例如 5 或 12.5。",
                },
                "index": {
                    "type": _NUMBER_SCHEMA,
                    "description": "magnet 时的剧集条目序号（从 1 开始），与 episode 二选一。",
                },
                "subgroup": {
                    "type": _STRING_SCHEMA,
                    "description": "preview 时的字幕组。",
                },
                "standby": {
                    "type": _ARRAY_SCHEMA,
                    "items": {"type": _STRING_SCHEMA},
                    "description": "preview 时的备用 RSS，每项格式为 标签|URL|偏移。",
                },
            },
            "required": ["action"],
        },
    )

    async def run(self, event: AstrMessageEvent, **kwargs: Any) -> str:
        action = str(kwargs.get("action") or "").strip()
        return await self._run(
            event,
            action or "query",
            lambda: self._dispatch(action, kwargs),
            _identity,
        )

    async def _dispatch(self, action: str, values: dict[str, Any]) -> str:
        plugin = self.plugin
        act = _normalize_action(action, _QUERY_ACTIONS, self.name)

        if act == "help":
            return plugin._help_text()
        if act == "status":
            return format_status(await plugin.service.status())
        if act == "list":
            page = _as_int(values.get("page"), "page", 1, minimum=1) or 1
            size = _as_int(values.get("size"), "size", minimum=1, maximum=50)
            data = await plugin.service.search_subscriptions(
                str(values.get("keyword") or "").strip(),
                page,
                size or plugin.page_size,
            )
            return format_subscription_list(data)
        if act == "info":
            selector = _require_value(values.get("selector"), "selector")
            ani = await plugin.service.get_subscription(selector)
            return format_subscription_detail(
                ani,
                mask_urls=plugin.mask_rss_urls,
            )
        if act == "path":
            selector = _require_value(values.get("selector"), "selector")
            data = await plugin.service.get_download_path(selector)
            return format_path_result(data)
        if act == "downloads":
            limit = _as_int(values.get("limit"), "limit", 10, minimum=1, maximum=50)
            data = await plugin.service.list_downloads(
                str(values.get("keyword") or "").strip(),
                limit or 10,
            )
            return format_downloads(data)
        if act == "preview":
            url = _require_value(values.get("url"), "url")
            data = await plugin.service.rss_preview(
                url=url,
                rss_type=_optional_text(values.get("rss_type")),
                bgm_url=_optional_text(values.get("bgm")),
                subgroup=_optional_text(values.get("subgroup")),
                standby_entries=_as_standby_entries(values.get("standby")),
            )
            return format_rss_preview(
                data["ani"],
                mask_urls=plugin.mask_rss_urls,
            )

        if act == "search":
            data = await plugin.service.search_source(
                str(values.get("keyword") or "").strip(),
                source=_optional_text(values.get("source")),
                season=_optional_text(values.get("season")),
                bgm_url=_optional_text(values.get("bgm")),
                limit=_as_int(values.get("limit"), "limit", minimum=1, maximum=50),
            )
            return format_search_results(data)

        if act == "groups":
            ref = _require_value(values.get("ref"), "ref")
            data = await plugin.service.source_groups(
                ref,
                source=_optional_text(values.get("source")),
                limit=_as_int(
                    values.get("limit"),
                    "limit",
                    minimum=1,
                    maximum=10,
                ),
            )
            return format_source_groups(
                data,
                mask_urls=plugin.mask_rss_urls,
            )

        if act == "magnet":
            ref = _require_value(values.get("ref"), "ref")
            group = _require_value(values.get("group"), "group")
            index = _as_int(values.get("index"), "index", minimum=1)
            episode = _as_float(values.get("episode"), "episode", minimum=0.1)
            if index is not None and episode is not None:
                raise AniRssValidationError(
                    "episode 与 index 只能指定一个。",
                )
            if index is None and episode is None:
                raise AniRssValidationError(
                    "magnet 需要 episode（集数）或 index（条目序号）。",
                )
            data = await plugin.service.source_episode(
                ref,
                group,
                source=_optional_text(values.get("source")),
                episode=episode,
                index=index,
            )
            return format_episode_result(
                data,
                mask_urls=plugin.mask_rss_urls,
            )

        if act == "standby_list":
            selector = _require_value(values.get("selector"), "selector")
            ani = await plugin.service.standby_list(selector)
            return format_standby_list(ani, mask_urls=plugin.mask_rss_urls)

        raise AniRssValidationError(f"{self.name} 不支持 action={act}。")


@dataclass
class AniRssSubscriptionTool(_AniRssTool):
    name: str = "ani_rss_subscription"
    description: str = (
        "管理 ani-rss 订阅。action 必填，取值：add、add_group、edit、delete、"
        "set_enabled、refresh、update_total。"
        "add 需要 url；add_group 需要 ref（ani_rss_query 的 search 返回）和"
        "group（字幕组序号或名称），用于直接订阅搜索结果中的字幕组；"
        "edit 需要 selector 和至少一个待修改字段；delete/set_enabled 需要 targets；"
        "refresh 可传 targets（为空表示全部）；update_total 可传 targets（默认 all）和 force。"
        "危险操作：delete 会立即删除订阅，执行前先向用户确认目标；"
        "files=true 仅当用户明确要求删除本地文件且插件配置允许时使用；"
        "add/add_group 的 override=true 必须同时传 confirm=true。"
        "修改标题、季或下载路径时默认不移动已下载视频，只有用户明确要求移动时才传 move=true。"
        "主 RSS 的添加、修改、删除分别对应 add、edit、delete；查看主 RSS 请用 ani_rss_query 的 info。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": _OBJECT_SCHEMA,
            "properties": {
                "action": {
                    "type": _STRING_SCHEMA,
                    "enum": list(_SUBSCRIPTION_ACTIONS),
                    "description": "要执行的写操作。",
                },
                "selector": {
                    "type": _STRING_SCHEMA,
                    "description": "edit 时的订阅 id、唯一前缀或标题。",
                },
                "ref": {
                    "type": _STRING_SCHEMA,
                    "description": "add_group 时 ani_rss_query 的 search 返回的片源引用。",
                },
                "group": {
                    "type": _STRING_SCHEMA,
                    "description": "add_group 时的字幕组序号（从 1 开始）或名称。",
                },
                "source": {
                    "type": _STRING_SCHEMA,
                    "enum": ["mikan", "ani-bt", "anime-garden"],
                    "description": "add_group 时 ref 未带来源前缀则用该参数指定。",
                },
                "targets": {
                    "type": _ARRAY_SCHEMA,
                    "items": {"type": _STRING_SCHEMA},
                    "description": "delete/set_enabled/refresh/update_total 的目标订阅列表；"
                    "refresh 为空表示全部，update_total 默认 all。",
                },
                "url": {
                    "type": _STRING_SCHEMA,
                    "description": "add/edit 的 RSS 地址。",
                },
                "rss_type": {
                    "type": _STRING_SCHEMA,
                    "enum": ["mikan", "ani-bt", "anime-garden", "other"],
                    "description": "add/edit 的 RSS 类型。",
                },
                "bgm": {
                    "type": _STRING_SCHEMA,
                    "description": "add/edit 的 BGM 地址。",
                },
                "subgroup": {
                    "type": _STRING_SCHEMA,
                    "description": "add/edit 的字幕组。",
                },
                "title": {
                    "type": _STRING_SCHEMA,
                    "description": "add/edit 的标题。",
                },
                "season": {
                    "type": _NUMBER_SCHEMA,
                    "description": "add/edit 的季，1-99。",
                },
                "offset": {
                    "type": _NUMBER_SCHEMA,
                    "description": "add/edit 的剧集偏移。",
                },
                "enable": {
                    "type": _BOOLEAN_SCHEMA,
                    "description": "add/edit 时是否启用订阅。",
                },
                "match": {
                    "type": _ARRAY_SCHEMA,
                    "items": {"type": _STRING_SCHEMA},
                    "description": "add/edit 的匹配规则。",
                },
                "exclude": {
                    "type": _ARRAY_SCHEMA,
                    "items": {"type": _STRING_SCHEMA},
                    "description": "add/edit 的排除规则。",
                },
                "standby": {
                    "type": _ARRAY_SCHEMA,
                    "items": {"type": _STRING_SCHEMA},
                    "description": "add 时的备用 RSS，每项格式为 标签|URL|偏移。",
                },
                "total": {
                    "type": _NUMBER_SCHEMA,
                    "description": "edit 时的总集数。",
                },
                "download_path": {
                    "type": _STRING_SCHEMA,
                    "description": "edit 时的下载路径模板，需配合 custom_download_path=true。",
                },
                "custom_download_path": {
                    "type": _BOOLEAN_SCHEMA,
                    "description": "edit 时是否启用自定义下载路径。",
                },
                "download_new": {
                    "type": _BOOLEAN_SCHEMA,
                    "description": "edit 时是否只下载最新集。",
                },
                "omit": {
                    "type": _BOOLEAN_SCHEMA,
                    "description": "edit 时是否启用遗漏检测。",
                },
                "global_exclude": {
                    "type": _BOOLEAN_SCHEMA,
                    "description": "edit 时是否启用全局排除。",
                },
                "upload": {
                    "type": _BOOLEAN_SCHEMA,
                    "description": "edit 时是否自动上传。",
                },
                "message": {
                    "type": _BOOLEAN_SCHEMA,
                    "description": "edit 时是否启用消息通知。",
                },
                "move": {
                    "type": _BOOLEAN_SCHEMA,
                    "description": "edit 时是否移动已下载视频，默认 false。",
                },
                "override": {
                    "type": _BOOLEAN_SCHEMA,
                    "description": "add 时是否覆盖已有订阅，默认 false。",
                },
                "confirm": {
                    "type": _BOOLEAN_SCHEMA,
                    "description": "add/add_group 覆盖已有订阅时的确认，默认 false。",
                },
                "files": {
                    "type": _BOOLEAN_SCHEMA,
                    "description": "delete 时是否同时删除本地文件，默认 false。",
                },
                "enabled": {
                    "type": _BOOLEAN_SCHEMA,
                    "description": "set_enabled 时启用或停用订阅。",
                },
                "force": {
                    "type": _BOOLEAN_SCHEMA,
                    "description": "update_total 时是否强制刷新，默认 false。",
                },
            },
            "required": ["action"],
        },
    )

    async def run(self, event: AstrMessageEvent, **kwargs: Any) -> str:
        action = str(kwargs.get("action") or "").strip()
        return await self._run(
            event,
            action or "subscription",
            lambda: self._dispatch(action, kwargs),
            _identity,
        )

    async def _dispatch(self, action: str, values: dict[str, Any]) -> str:
        plugin = self.plugin
        act = _normalize_action(action, _SUBSCRIPTION_ACTIONS, self.name)

        if act == "add":
            data = await plugin.service.add_subscription(
                url=_require_value(values.get("url"), "url"),
                rss_type=_optional_text(values.get("rss_type")),
                bgm_url=_optional_text(values.get("bgm")),
                subgroup=_optional_text(values.get("subgroup")),
                title=_optional_text(values.get("title")),
                season=_as_int(values.get("season"), "season", minimum=1, maximum=99),
                offset=_as_int(values.get("offset"), "offset"),
                enable=(
                    _as_bool(values.get("enable"), "enable")
                    if values.get("enable") is not None
                    else None
                ),
                match=_as_str_list(values.get("match"), "match") or None,
                exclude=_as_str_list(values.get("exclude"), "exclude") or None,
                standby_entries=_as_standby_entries(values.get("standby")),
                override=_as_bool(values.get("override"), "override"),
                confirmed=_as_bool(values.get("confirm"), "confirm"),
            )
            return format_add_result(data, mask_urls=plugin.mask_rss_urls)

        if act == "add_group":
            data = await plugin.service.subscribe_group(
                _require_value(values.get("ref"), "ref"),
                _require_value(values.get("group"), "group"),
                source=_optional_text(values.get("source")),
                match=_as_str_list(values.get("match"), "match") or None,
                exclude=_as_str_list(values.get("exclude"), "exclude") or None,
                enable=(
                    _as_bool(values.get("enable"), "enable")
                    if values.get("enable") is not None
                    else None
                ),
                title=_optional_text(values.get("title")),
                season=_as_int(values.get("season"), "season", minimum=1, maximum=99),
                offset=_as_int(values.get("offset"), "offset"),
                override=_as_bool(values.get("override"), "override"),
                confirmed=_as_bool(values.get("confirm"), "confirm"),
            )
            return format_add_result(data, mask_urls=plugin.mask_rss_urls)

        if act == "edit":
            selector = _require_value(values.get("selector"), "selector")
            changes = _edit_changes(values)
            if not changes:
                raise AniRssValidationError(
                    "edit 至少需要提供一个待修改字段，例如 title、url、season、enable。",
                )
            data = await plugin.service.edit_subscription(
                selector,
                changes,
                move=_as_bool(values.get("move"), "move"),
            )
            return format_edit_result(data, mask_urls=plugin.mask_rss_urls)

        if act == "delete":
            selectors = expand_selectors(
                _as_str_list(values.get("targets"), "targets"),
            )
            if not selectors:
                raise AniRssValidationError("delete 至少需要一个目标订阅。")
            data = await plugin.service.delete_subscriptions(
                selectors,
                delete_files=_as_bool(values.get("files"), "files"),
            )
            return format_delete_result(data)

        if act == "set_enabled":
            selectors = expand_selectors(
                _as_str_list(values.get("targets"), "targets"),
            )
            if not selectors:
                raise AniRssValidationError("set_enabled 至少需要一个目标订阅。")
            if values.get("enabled") is None:
                raise AniRssValidationError(
                    "set_enabled 缺少参数 enabled（true 表示启用，false 表示停用）。",
                )
            enabled = _as_bool(values.get("enabled"), "enabled")
            data = await plugin.service.set_enabled(selectors, enabled=enabled)
            return format_enable_result(data, enabled=enabled)

        if act == "refresh":
            targets = expand_selectors(
                _as_str_list(values.get("targets"), "targets"),
            )
            data = await plugin.service.refresh(targets or None)
            return format_refresh_result(data)

        if act == "update_total":
            targets = expand_selectors(
                _as_str_list(values.get("targets"), "targets"),
            )
            data = await plugin.service.update_total(
                targets or ["all"],
                force=_as_bool(values.get("force"), "force"),
            )
            return format_total_result(data)

        raise AniRssValidationError(f"{self.name} 不支持 action={act}。")


@dataclass
class AniRssStandbyTool(_AniRssTool):
    name: str = "ani_rss_standby"
    description: str = (
        "管理 ani-rss 订阅的备用 RSS。action 必填，取值：add、set、delete、move。"
        "所有 action 都需要 selector（主订阅 id、唯一前缀或标题）。"
        "add 需要 url，可选 label/offset；set 需要 target（备用项序号或标签）"
        "且至少提供 url/label/offset 之一；delete 需要 target；"
        "move 需要 target 和 direction（up、down 或 to=N）。"
        "列出备用 RSS 请调用 ani_rss_query 的 standby_list。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": _OBJECT_SCHEMA,
            "properties": {
                "action": {
                    "type": _STRING_SCHEMA,
                    "enum": list(_STANDBY_ACTIONS),
                    "description": "要执行的备用 RSS 操作。",
                },
                "selector": {
                    "type": _STRING_SCHEMA,
                    "description": "主订阅 id、唯一 id 前缀或标题；标题可含空格。",
                },
                "url": {
                    "type": _STRING_SCHEMA,
                    "description": "add/set 的备用 RSS 地址。",
                },
                "label": {
                    "type": _STRING_SCHEMA,
                    "description": "add/set 的字幕组标签。",
                },
                "offset": {
                    "type": _NUMBER_SCHEMA,
                    "description": "add/set 的剧集偏移。",
                },
                "target": {
                    "type": _STRING_SCHEMA,
                    "description": "set/delete/move 时的备用项序号（从 1 开始）或标签。",
                },
                "direction": {
                    "type": _STRING_SCHEMA,
                    "description": "move 时的方向：up、down 或 to=N。",
                },
            },
            "required": ["action", "selector"],
        },
    )

    async def run(self, event: AstrMessageEvent, **kwargs: Any) -> str:
        action = str(kwargs.get("action") or "").strip()
        return await self._run(
            event,
            f"standby {action}".strip(),
            lambda: self._dispatch(action, kwargs),
            _identity,
        )

    async def _dispatch(self, action: str, values: dict[str, Any]) -> str:
        plugin = self.plugin
        act = _normalize_action(action, _STANDBY_ACTIONS, self.name)
        selector = _require_value(values.get("selector"), "selector")

        if act == "add":
            data = await plugin.service.standby_add(
                selector,
                _require_value(values.get("url"), "url"),
                label=_optional_text(values.get("label")),
                offset=_as_int(values.get("offset"), "offset"),
            )
            return format_standby_change(data, mask_urls=plugin.mask_rss_urls)

        target = _require_value(values.get("target"), "target")

        if act == "set":
            url = _optional_text(values.get("url"))
            label = _optional_text(values.get("label"))
            offset = _as_int(values.get("offset"), "offset")
            if url is None and label is None and offset is None:
                raise AniRssValidationError(
                    "set 至少需要提供 url、label 或 offset 中的一项。",
                )
            data = await plugin.service.standby_set(
                selector,
                target,
                url=url,
                label=label,
                offset=offset,
            )
            return format_standby_change(data, mask_urls=plugin.mask_rss_urls)

        if act == "delete":
            data = await plugin.service.standby_delete(selector, target)
            return format_standby_change(data, mask_urls=plugin.mask_rss_urls)

        if act == "move":
            data = await plugin.service.standby_move(
                selector,
                target,
                _require_value(values.get("direction"), "direction"),
            )
            return format_standby_change(data, mask_urls=plugin.mask_rss_urls)

        raise AniRssValidationError(f"{self.name} 不支持 action={act}。")


ALL_TOOL_CLASSES = (
    AniRssQueryTool,
    AniRssSubscriptionTool,
    AniRssStandbyTool,
)
