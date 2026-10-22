"""AstrBot ani-rss 控制插件入口。"""

from __future__ import annotations

import inspect
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .ani_rss_args import (
    ParsedArgs,
    expand_selectors,
    join_keyword,
    parse_args,
    parse_list,
    parse_standby_specs,
)
from .ani_rss_client import AniRssClient
from .ani_rss_errors import (
    AniRssAPIError,
    AniRssAuthError,
    AniRssConfigError,
    AniRssConnectionError,
    AniRssDuplicateError,
    AniRssError,
    AniRssValidationError,
)
from .ani_rss_format import (
    chunk_text,
    format_add_result,
    format_delete_result,
    format_downloads,
    format_edit_result,
    format_enable_result,
    format_episode_result,
    format_help,
    format_main_show,
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
from .ani_rss_service import EDITABLE_FIELDS, AniRssService
from .ani_rss_tools import ALL_TOOL_CLASSES

PLUGIN_VERSION = "0.1.0-beta3"
_TOOL_RESULT_MAX_CHARS = 6000

_ADD_OPTIONS = {
    "type",
    "bgm",
    "subgroup",
    "title",
    "season",
    "offset",
    "enable",
    "match",
    "exclude",
    "standby",
    "override",
}
_DELETE_OPTIONS = {"files"}
_TOTAL_OPTIONS = {"force"}
_SEARCH_OPTIONS = {"source", "season", "bgm", "limit"}
_GROUP_OPTIONS = {"source", "limit"}
_MAGNET_OPTIONS = {"source", "idx"}
_GROUP_ADD_OPTIONS = {
    "source",
    "match",
    "exclude",
    "enable",
    "title",
    "season",
    "offset",
    "override",
}

# AstrBot 的平台类型名与用户习惯叫法不同，这里兼容常见别名。
_PLATFORM_ALIASES = {
    "aiocqhttp": ("qq", "onebot"),
    "qq_official": ("qq",),
    "lark": ("feishu",),
}


class AniRssControlPlugin(Star):
    """通过 AstrBot 控制 ani-rss。

    支持订阅列表与详情、主 RSS 与备用 RSS 的增删改、启用停用、刷新、
    总集数更新、RSS 解析预览、下载路径与下载任务查询，以及片源搜索、
    字幕组订阅和单集磁力查询。

    使用 /ani help 查看完整指令。
    """

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config

        self.base_url = self._cfg_str("base_url", "http://127.0.0.1:7789")
        self.api_key = self._cfg_str("api_key", "")
        self.request_timeout = self._cfg_int(
            "request_timeout",
            15,
            minimum=1,
            maximum=120,
        )
        self.rss_timeout = self._cfg_int(
            "rss_timeout",
            60,
            minimum=5,
            maximum=300,
        )
        self.verify_tls = self._cfg_bool("verify_tls", True)
        self.default_rss_type = self._cfg_str("default_rss_type", "mikan")
        self.allow_delete_files = self._cfg_bool("allow_delete_files", False)
        self.page_size = self._cfg_int("page_size", 10, minimum=1, maximum=50)
        self.search_limit = self._cfg_int("search_limit", 10, minimum=1, maximum=50)
        self.max_message_chars = self._cfg_int(
            "max_message_chars",
            1500,
            minimum=500,
            maximum=4000,
        )
        self.mask_rss_urls = self._cfg_bool("mask_rss_urls", False)
        self.authorized_uids = self._normalize_uids(
            self.config.get("authorized_uids", []),
        )

        self.client = AniRssClient(
            self.base_url,
            api_key=self.api_key,
            timeout=self.request_timeout,
            rss_timeout=self.rss_timeout,
            verify_tls=self.verify_tls,
            logger=logger,
        )
        self.service = AniRssService(
            self.client,
            allow_delete_files=self.allow_delete_files,
            page_size=self.page_size,
            search_limit=self.search_limit,
            default_rss_type=self.default_rss_type,
            logger=logger,
        )
        self.context.add_llm_tools(
            *(tool_cls(plugin=self) for tool_cls in ALL_TOOL_CLASSES),
        )

    async def terminate(self) -> None:
        """插件卸载或重载时关闭 HTTP 会话。"""

        try:
            await self.client.close()
        except Exception:  # noqa: BLE001 - 退出阶段只记录日志
            logger.exception("关闭 ani-rss HTTP 会话失败")

    # ------------------------------------------------------------------
    # 指令组
    # ------------------------------------------------------------------
    @filter.command_group("ani", alias={"anirss", "ar"})
    async def ani_group(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """ani-rss 控制指令。

        AstrBot 的消息管线只会把指令组当作过滤器使用，不会调用本函数；
        但部分插件（例如 llm_executor）会直接调用已注册的指令组处理器。
        因此这里接受 event，并在被直接调用时把请求转交给对应子指令。
        """

        async for result in self._dispatch_group(
            event,
            self.ani_group.parent_group,
            format_help(PLUGIN_VERSION),
        ):
            yield result

    @ani_group.command("help", alias={"帮助"})
    async def ani_help(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """显示帮助。"""

        for chunk in chunk_text(
            self._help_text(),
            self.max_message_chars,
        ):
            yield event.plain_result(chunk)

    @ani_group.command("status", alias={"状态"})
    async def ani_status(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """查看 ani-rss 连接状态。"""

        async for result in self._execute(
            event,
            "status",
            lambda: self.service.status(),
            format_status,
        ):
            yield result

    @ani_group.command("list", alias={"ls", "列表"})
    async def ani_list(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """列出 ani-rss 订阅。"""

        async for result in self._execute(
            event,
            "list",
            lambda: self._do_list(args),
            format_subscription_list,
        ):
            yield result

    @ani_group.command("info", alias={"详情"})
    async def ani_info(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """查看订阅详情。"""

        async for result in self._execute(
            event,
            "info",
            lambda: self._do_info(args),
            lambda ani: format_subscription_detail(
                ani,
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    @ani_group.command("add", alias={"添加"})
    async def ani_add(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """添加主 RSS 订阅，可同时添加备用 RSS。"""

        async for result in self._execute(
            event,
            "add",
            lambda: self._do_add(args),
            lambda data: format_add_result(
                data,
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    @ani_group.command("edit", alias={"set", "修改"})
    async def ani_edit(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """修改订阅字段或主 RSS。"""

        async for result in self._execute(
            event,
            "edit",
            lambda: self._do_edit(args),
            lambda data: format_edit_result(
                data,
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    @ani_group.command("delete", alias={"del", "删除"})
    async def ani_delete(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """删除订阅，需追加「确认」。"""

        async for result in self._execute(
            event,
            "delete",
            lambda: self._do_delete(args),
            format_delete_result,
        ):
            yield result

    @ani_group.command("enable", alias={"启用"})
    async def ani_enable(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """批量启用订阅。"""

        async for result in self._execute(
            event,
            "enable",
            lambda: self._do_enabled(args, True),
            lambda data: format_enable_result(data, enabled=True),
        ):
            yield result

    @ani_group.command("disable", alias={"停用"})
    async def ani_disable(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """批量停用订阅。"""

        async for result in self._execute(
            event,
            "disable",
            lambda: self._do_enabled(args, False),
            lambda data: format_enable_result(data, enabled=False),
        ):
            yield result

    @ani_group.command("refresh", alias={"刷新"})
    async def ani_refresh(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """刷新单个或全部订阅。"""

        async for result in self._execute(
            event,
            "refresh",
            lambda: self._do_refresh(args),
            format_refresh_result,
        ):
            yield result

    @ani_group.command("total", alias={"总集数"})
    async def ani_total(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """更新订阅总集数。"""

        async for result in self._execute(
            event,
            "total",
            lambda: self._do_total(args),
            format_total_result,
        ):
            yield result

    @ani_group.command("rss", alias={"解析"})
    async def ani_rss(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """解析 RSS，仅预览不添加。"""

        async for result in self._execute(
            event,
            "rss",
            lambda: self._do_rss(args),
            lambda data: format_rss_preview(
                data["ani"],
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    @ani_group.command("path", alias={"路径"})
    async def ani_path(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """查询订阅的下载路径。"""

        async for result in self._execute(
            event,
            "path",
            lambda: self._do_path(args),
            format_path_result,
        ):
            yield result

    @ani_group.command("downloads", alias={"下载"})
    async def ani_downloads(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """查看下载任务列表。"""

        async for result in self._execute(
            event,
            "downloads",
            lambda: self._do_downloads(args),
            format_downloads,
        ):
            yield result

    # ------------------------------------------------------------------
    # 片源搜索
    # ------------------------------------------------------------------
    @ani_group.command("search", alias={"搜索"})
    async def ani_search(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """搜索 Mikan、AniBT 或 AnimeGarden 番剧。"""

        async for result in self._execute(
            event,
            "search",
            lambda: self._do_search(args),
            format_search_results,
        ):
            yield result

    @ani_group.command("groups", alias={"组", "字幕组"})
    async def ani_groups(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """查看片源的字幕组与剧集。"""

        async for result in self._execute(
            event,
            "groups",
            lambda: self._do_groups(args),
            lambda data: format_source_groups(
                data,
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    @ani_group.command("magnet", alias={"磁力"})
    async def ani_magnet(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """查询某字幕组某集的磁力链。"""

        async for result in self._execute(
            event,
            "magnet",
            lambda: self._do_magnet(args),
            lambda data: format_episode_result(
                data,
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    @ani_group.command("sub", alias={"订阅"})
    async def ani_sub(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """订阅指定字幕组。"""

        async for result in self._execute(
            event,
            "add",
            lambda: self._do_sub(args),
            lambda data: format_add_result(
                data,
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    # ------------------------------------------------------------------
    # 主 RSS
    # ------------------------------------------------------------------
    @ani_group.group("main", alias={"主"})
    async def ani_main_group(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """主 RSS 操作。"""

        async for result in self._dispatch_group(
            event,
            self.ani_main_group.parent_group,
            format_help(PLUGIN_VERSION),
        ):
            yield result

    @ani_main_group.command("add")
    async def ani_main_add(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """添加主 RSS 订阅，可同时添加备用 RSS。"""

        async for result in self._execute(
            event,
            "main add",
            lambda: self._do_add(args),
            lambda data: format_add_result(
                data,
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    @ani_main_group.command("show")
    async def ani_main_show(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """查看主 RSS。"""

        async for result in self._execute(
            event,
            "main show",
            lambda: self._do_main_show(args),
            lambda ani: format_main_show(
                ani,
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    @ani_main_group.command("set")
    async def ani_main_set(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """修改主 RSS。"""

        async for result in self._execute(
            event,
            "main set",
            lambda: self._do_main_set(args),
            lambda data: format_edit_result(
                data,
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    @ani_main_group.command("delete")
    async def ani_main_delete(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """删除订阅；ani-rss 没有只删主 RSS 保留订阅的模式。"""

        async for result in self._execute(
            event,
            "main delete",
            lambda: self._do_delete(args),
            format_delete_result,
        ):
            yield result

    # ------------------------------------------------------------------
    # 备用 RSS
    # ------------------------------------------------------------------
    @ani_group.group("standby", alias={"备"})
    async def ani_standby_group(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """备用 RSS 操作。"""

        async for result in self._dispatch_group(
            event,
            self.ani_standby_group.parent_group,
            format_help(PLUGIN_VERSION),
        ):
            yield result

    @ani_standby_group.command("list", alias={"ls"})
    async def ani_standby_list(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """查看备用 RSS 列表。"""

        async for result in self._execute(
            event,
            "standby list",
            lambda: self._do_standby_list(args),
            lambda ani: format_standby_list(
                ani,
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    @ani_standby_group.command("add")
    async def ani_standby_add(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """添加备用 RSS。"""

        async for result in self._execute(
            event,
            "standby add",
            lambda: self._do_standby_add(args),
            lambda data: format_standby_change(
                data,
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    @ani_standby_group.command("set")
    async def ani_standby_set(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """修改备用 RSS。"""

        async for result in self._execute(
            event,
            "standby set",
            lambda: self._do_standby_set(args),
            lambda data: format_standby_change(
                data,
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    @ani_standby_group.command("delete", alias={"del"})
    async def ani_standby_delete(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """删除备用 RSS。"""

        async for result in self._execute(
            event,
            "standby delete",
            lambda: self._do_standby_delete(args),
            lambda data: format_standby_change(
                data,
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    @ani_standby_group.command("move")
    async def ani_standby_move(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[Any, None]:
        """调整备用 RSS 顺序。"""

        async for result in self._execute(
            event,
            "standby move",
            lambda: self._do_standby_move(args),
            lambda data: format_standby_change(
                data,
                mask_urls=self.mask_rss_urls,
            ),
        ):
            yield result

    # ------------------------------------------------------------------
    # 参数处理
    # ------------------------------------------------------------------
    async def _do_list(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(raw, allowed_options={"page", "size"})
        page = parsed.int("page", 1, minimum=1) or 1
        size = parsed.int("size", self.page_size, minimum=1, maximum=50)
        keyword = join_keyword(parsed.positionals)
        return await self.service.search_subscriptions(keyword, page, size)

    async def _do_info(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(raw)
        if not parsed.positionals:
            raise AniRssValidationError("用法：/ani info <订阅>")
        return await self.service.get_subscription(_selector_text(parsed))

    async def _do_add(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(
            raw,
            allowed_options=_ADD_OPTIONS,
            allowed_flags={"override", "confirm"},
        )
        _require_positionals(parsed, 1, "/ani add <RSS地址> [参数]")
        return await self.service.add_subscription(
            url=parsed.positionals[0],
            rss_type=parsed.get("type"),
            bgm_url=parsed.get("bgm"),
            subgroup=parsed.get("subgroup"),
            title=parsed.get("title"),
            season=parsed.int("season", minimum=1, maximum=99),
            offset=parsed.int("offset"),
            enable=parsed.bool("enable"),
            match=_optional_list(parsed, "match"),
            exclude=_optional_list(parsed, "exclude"),
            standby_entries=parse_standby_specs(parsed.get_all("standby")),
            override=bool(parsed.bool("override", False)),
            confirmed=parsed.has_flag("confirm"),
        )

    async def _do_edit(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(
            raw,
            allowed_options=EDITABLE_FIELDS,
            allowed_flags={"move"},
        )
        if not parsed.positionals:
            raise AniRssValidationError(
                "用法：/ani edit <订阅> [字段=值 ...] [move=true]",
            )
        changes = {
            field: value
            for field in EDITABLE_FIELDS
            if (value := parsed.get(field)) is not None
        }
        return await self.service.edit_subscription(
            _selector_text(parsed),
            changes,
            move=bool(parsed.bool("move", False)),
        )

    async def _do_delete(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(
            raw,
            allowed_options=_DELETE_OPTIONS,
            allowed_flags={"files"},
        )
        selectors = expand_selectors(parsed.positionals)
        if not selectors:
            raise AniRssValidationError(
                "用法：/ani delete <订阅...> [files=true]",
            )
        return await self.service.delete_subscriptions(
            selectors,
            delete_files=bool(parsed.bool("files", False)),
        )

    async def _do_enabled(self, raw: str, enabled: bool) -> dict[str, Any]:
        parsed = parse_args(raw)
        selectors = expand_selectors(parsed.positionals)
        if not selectors:
            raise AniRssValidationError(
                f"用法：/ani {'enable' if enabled else 'disable'} <订阅...>",
            )
        return await self.service.set_enabled(selectors, enabled=enabled)

    async def _do_refresh(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(raw)
        selectors = expand_selectors(parsed.positionals)
        return await self.service.refresh(selectors or None)

    async def _do_total(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(
            raw,
            allowed_options=_TOTAL_OPTIONS,
            allowed_flags={"force"},
        )
        selectors = expand_selectors(parsed.positionals)
        if not selectors:
            raise AniRssValidationError(
                "用法：/ani total <all|订阅...> [force=true]",
            )
        return await self.service.update_total(
            selectors,
            force=bool(parsed.bool("force", False)),
        )

    async def _do_rss(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(
            raw,
            allowed_options={"type", "bgm", "subgroup", "standby"},
        )
        _require_positionals(parsed, 1, "/ani rss <RSS地址> [参数]")
        return await self.service.rss_preview(
            url=parsed.positionals[0],
            rss_type=parsed.get("type"),
            bgm_url=parsed.get("bgm"),
            subgroup=parsed.get("subgroup"),
            standby_entries=parse_standby_specs(parsed.get_all("standby")),
        )

    async def _do_path(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(raw)
        if not parsed.positionals:
            raise AniRssValidationError("用法：/ani path <订阅>")
        return await self.service.get_download_path(_selector_text(parsed))

    async def _do_downloads(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(raw, allowed_options={"limit"})
        keyword = join_keyword(parsed.positionals)
        limit = parsed.int("limit", 10, minimum=1, maximum=50) or 10
        return await self.service.list_downloads(keyword, limit)

    async def _do_search(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(raw, allowed_options=_SEARCH_OPTIONS)
        keyword = join_keyword(parsed.positionals)
        return await self.service.search_source(
            keyword,
            source=parsed.get("source"),
            season=parsed.get("season"),
            bgm_url=parsed.get("bgm"),
            limit=parsed.int("limit", minimum=1, maximum=50),
        )

    async def _do_groups(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(raw, allowed_options=_GROUP_OPTIONS)
        _require_positionals(parsed, 1, "/ani groups <ref> [limit=3]")
        return await self.service.source_groups(
            parsed.positionals[0],
            source=parsed.get("source"),
            limit=parsed.int("limit", minimum=1, maximum=10),
        )

    async def _do_magnet(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(raw, allowed_options=_MAGNET_OPTIONS)
        _require_at_least(
            parsed,
            2,
            "/ani magnet <ref> <组> <集数> 或 /ani magnet <ref> <组> idx=<序号>",
        )
        index = parsed.int("idx", minimum=1)
        episode: float | None = None
        if index is None:
            _require_at_least(
                parsed,
                3,
                "/ani magnet <ref> <组> <集数> 或 /ani magnet <ref> <组> idx=<序号>",
            )
            episode = _parse_episode(parsed.positionals[2])
        elif len(parsed.positionals) > 2:
            raise AniRssValidationError("集数与 idx 只能指定一个。")
        return await self.service.source_episode(
            parsed.positionals[0],
            parsed.positionals[1],
            source=parsed.get("source"),
            episode=episode,
            index=index,
        )

    async def _do_sub(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(
            raw,
            allowed_options=_GROUP_ADD_OPTIONS,
            allowed_flags={"override", "confirm"},
        )
        _require_at_least(parsed, 2, "/ani sub <ref> <组> [参数]")
        return await self.service.subscribe_group(
            parsed.positionals[0],
            parsed.positionals[1],
            source=parsed.get("source"),
            match=_optional_list(parsed, "match"),
            exclude=_optional_list(parsed, "exclude"),
            enable=parsed.bool("enable"),
            title=parsed.get("title"),
            season=parsed.int("season", minimum=1, maximum=99),
            offset=parsed.int("offset"),
            override=bool(parsed.bool("override", False)),
            confirmed=parsed.has_flag("confirm"),
        )

    async def _do_main_show(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(raw)
        if not parsed.positionals:
            raise AniRssValidationError("用法：/ani main show <订阅>")
        return await self.service.main_show(_selector_text(parsed))

    async def _do_main_set(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(raw)
        _require_at_least(
            parsed,
            2,
            "/ani main set <订阅> <RSS地址>",
        )
        return await self.service.set_main_rss(
            _selector_text(parsed, drop_last=1),
            parsed.positionals[-1],
        )

    async def _do_standby_list(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(raw)
        if not parsed.positionals:
            raise AniRssValidationError("用法：/ani standby list <订阅>")
        return await self.service.standby_list(_selector_text(parsed))

    async def _do_standby_add(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(raw, allowed_options={"label", "offset"})
        _require_at_least(
            parsed,
            2,
            "/ani standby add <订阅> <RSS地址> [label=] [offset=]",
        )
        return await self.service.standby_add(
            _selector_text(parsed, drop_last=1),
            parsed.positionals[-1],
            label=parsed.get("label"),
            offset=parsed.int("offset"),
        )

    async def _do_standby_set(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(
            raw,
            allowed_options={"url", "label", "offset"},
        )
        _require_at_least(
            parsed,
            2,
            "/ani standby set <订阅> <序号|标签> [url=] [label=] [offset=]",
        )
        if not (parsed.has("url") or parsed.has("label") or parsed.has("offset")):
            raise AniRssValidationError(
                "请至少提供 url、label 或 offset 中的一项。",
            )
        return await self.service.standby_set(
            _selector_text(parsed, drop_last=1),
            parsed.positionals[-1],
            url=parsed.get("url"),
            label=parsed.get("label"),
            offset=parsed.int("offset"),
        )

    async def _do_standby_delete(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(raw)
        _require_at_least(
            parsed,
            2,
            "/ani standby delete <订阅> <序号|标签>",
        )
        return await self.service.standby_delete(
            _selector_text(parsed, drop_last=1),
            parsed.positionals[-1],
        )

    async def _do_standby_move(self, raw: str) -> dict[str, Any]:
        parsed = parse_args(raw, allowed_options={"to"})
        to_value = parsed.get("to")
        if to_value is not None:
            _require_at_least(
                parsed,
                2,
                "/ani standby move <订阅> <序号|标签> to=N",
            )
            direction = f"to={to_value}"
            selector = _selector_text(parsed, drop_last=1)
            target = parsed.positionals[-1]
        else:
            _require_at_least(
                parsed,
                3,
                "/ani standby move <订阅> <序号|标签> <up|down>",
            )
            direction = parsed.positionals[-1]
            selector = _selector_text(parsed, drop_last=2)
            target = parsed.positionals[-2]
        return await self.service.standby_move(
            selector,
            target,
            direction,
        )

    # ------------------------------------------------------------------
    # 通用处理
    # ------------------------------------------------------------------
    async def _dispatch_group(
        self,
        event: AstrMessageEvent,
        group_filter: Any,
        help_text: str,
    ) -> AsyncGenerator[Any, None]:
        """把直接调用指令组处理器的请求转交给对应子指令。

        AstrBot 正常消息管线不会调用指令组函数；这里仅为兼容会直接调用
        注册处理器的第三方插件（例如 llm_executor）。无法匹配子指令时
        返回帮助文本，避免出现空回复。
        """

        text = _normalize_command_text(event.get_message_str())
        matched = _match_group_command(group_filter, text)
        if matched is None:
            for chunk in chunk_text(help_text, self.max_message_chars):
                yield event.plain_result(chunk)
            return

        name, command_filter = matched
        remainder = text[len(name) :].strip()
        handler_params = getattr(command_filter, "handler_params", None) or {}
        try:
            params = (
                command_filter.validate_and_convert_params(
                    [part for part in remainder.split(" ") if part],
                    handler_params,
                )
                if handler_params
                else {}
            )
        except ValueError as exc:
            yield event.plain_result(f"参数错误：{exc}")
            return

        handler_md = getattr(command_filter, "handler_md", None)
        handler = getattr(handler_md, "handler", None)
        if not callable(handler):
            for chunk in chunk_text(help_text, self.max_message_chars):
                yield event.plain_result(chunk)
            return

        result = handler(event, **params)
        if inspect.isasyncgen(result):
            async for item in result:
                yield item
        elif inspect.isawaitable(result):
            await result

    def _help_text(self) -> str:
        """返回完整帮助文本。"""

        return format_help(PLUGIN_VERSION)

    async def _run_tool(
        self,
        event: AstrMessageEvent,
        operation: str,
        action: Callable[[], Awaitable[Any]],
        render: Callable[[Any], str],
    ) -> str:
        """执行 LLM 工具请求，统一处理鉴权、异常与长度限制。"""

        auth_error = self._auth_error(event)
        if auth_error:
            return auth_error

        try:
            data = await action()
            text = render(data)
        except AniRssError as exc:
            logger.warning("ani-rss 工具 %s 失败：%s", operation, exc)
            return self._error_text(exc, operation)
        except Exception:
            logger.exception("ani-rss 工具 %s 发生未预期异常", operation)
            return "操作失败，请查看 AstrBot 日志。"

        if not isinstance(text, str) or not text.strip():
            text = "操作完成。"
        return _truncate_tool_text(text, _TOOL_RESULT_MAX_CHARS)

    async def _execute(
        self,
        event: AstrMessageEvent,
        operation: str,
        action: Callable[[], Awaitable[Any]],
        render: Callable[[Any], str],
    ) -> AsyncGenerator[Any, None]:
        auth_error = self._auth_error(event)
        if auth_error:
            yield event.plain_result(auth_error)
            return

        try:
            data = await action()
            text = render(data)
        except AniRssError as exc:
            logger.warning("ani-rss %s 失败：%s", operation, exc)
            yield event.plain_result(self._error_text(exc, operation))
            return
        except Exception:
            logger.exception("ani-rss %s 发生未预期异常", operation)
            yield event.plain_result("操作失败，请查看 AstrBot 日志。")
            return

        if not isinstance(text, str) or not text.strip():
            text = "操作完成。"
        for chunk in chunk_text(text, self.max_message_chars):
            yield event.plain_result(chunk)

    def _auth_error(self, event: AstrMessageEvent) -> str | None:
        if event.is_admin():
            return None

        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            # 部分适配器的 sender.user_id 不是 str，AstrBot 会返回空字符串。
            sender = getattr(getattr(event, "message_obj", None), "sender", None)
            raw_sender_id = getattr(sender, "user_id", None)
            if raw_sender_id is not None:
                sender_id = str(raw_sender_id).strip()
        if not sender_id:
            return "无权限：无法识别发送者 ID。"

        candidates = {sender_id}
        get_platform_name = getattr(event, "get_platform_name", None)
        if callable(get_platform_name):
            platform = str(get_platform_name() or "").strip().lower()
            if platform:
                candidates.add(f"{platform}:{sender_id}")
                candidates.update(
                    f"{alias}:{sender_id}"
                    for alias in _PLATFORM_ALIASES.get(platform, ())
                )

        if candidates & self.authorized_uids:
            return None
        return "无权限：仅 AstrBot 管理员或配置 authorized_uids 中的用户可使用。"

    def _error_text(self, exc: AniRssError, operation: str = "") -> str:
        if isinstance(exc, AniRssConfigError):
            return f"配置错误：{exc}"
        if isinstance(exc, AniRssValidationError):
            return str(exc)
        if isinstance(exc, AniRssAuthError):
            return f"ani-rss 鉴权失败：{exc}\n请检查 api_key 配置。"
        if isinstance(exc, AniRssDuplicateError):
            if operation in {"add", "main add"}:
                return (
                    f"ani-rss 返回错误：{exc}\n"
                    "如需覆盖已有订阅，请追加 override=true 确认。"
                )
            return (
                f"ani-rss 返回错误：{exc}\n"
                "该订阅与已有订阅的标题和季重复，请修改标题或季。"
            )
        if isinstance(exc, AniRssConnectionError):
            return (
                f"连接 ani-rss 失败：{exc}\n请检查 base_url、网络与 ani-rss 服务状态。"
            )
        if isinstance(exc, AniRssAPIError):
            message = f"ani-rss 返回错误：{exc}"
            if exc.status == 404 or exc.code == 404:
                message += (
                    "\n接口不存在：请检查 base_url（含反向代理子路径）是否正确，"
                    "以及 ani-rss 版本是否兼容。"
                )
                if operation in {"search", "groups", "magnet"}:
                    message += (
                        "\n当前 ani-rss 版本可能不支持片源搜索接口，"
                        "请升级到包含 Mikan/AniBT/AnimeGarden 搜索的 3.x 版本。"
                    )
            if _looks_like_upstream_npe(str(exc)):
                message += (
                    "\n这通常表示 ani-rss 中存在缺少 title 或 season 字段的订阅。"
                    "请先删除这些订阅（不要带 files=true），再重新添加。"
                )
            return message
        return f"ani-rss 操作失败：{exc}"

    # ------------------------------------------------------------------
    # 配置工具
    # ------------------------------------------------------------------
    def _cfg_str(self, key: str, default: str) -> str:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value
        return default if value is None else str(value)

    def _cfg_int(
        self,
        key: str,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        value = self.config.get(key, default)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        if minimum is not None:
            parsed = max(minimum, parsed)
        if maximum is not None:
            parsed = min(maximum, parsed)
        return parsed

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return default

    def _normalize_uids(self, value: Any) -> set[str]:
        if isinstance(value, str):
            items = value.replace("\n", ",").split(",")
        elif isinstance(value, (list, tuple, set)):
            items = list(value)
        else:
            items = []
        return {
            text for item in items if item is not None and (text := str(item).strip())
        }


def _truncate_tool_text(text: str, limit: int) -> str:
    """截断过长的工具结果，避免占用过多模型上下文。"""

    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...（结果过长，已截断；请缩小查询范围后重试）"


def _looks_like_upstream_npe(message: str) -> bool:
    """判断 ani-rss 错误信息是否为空值导致的空指针异常。"""

    normalized = message.lower()
    return any(
        marker in normalized
        for marker in (
            "cannot invoke",
            "nullpointerexception",
            "cannot read the array length",
            "is null",
        )
    )


def _normalize_command_text(raw: str) -> str:
    """规范化可能带唤醒前缀的指令文本。"""

    text = re.sub(r"\s+", " ", str(raw or "").strip())
    return text.lstrip("/~～!！").strip()


def _iter_group_commands(
    group_filter: Any,
) -> Iterator[tuple[list[str], Any]]:
    """递归遍历指令组下的所有子指令过滤器。"""

    for sub_filter in getattr(group_filter, "sub_command_filters", []) or []:
        if hasattr(sub_filter, "handler_md"):
            yield list(sub_filter.get_complete_command_names()), sub_filter
        else:
            yield from _iter_group_commands(sub_filter)


def _match_group_command(
    group_filter: Any,
    text: str,
) -> tuple[str, Any] | None:
    """按最长指令名匹配子指令。"""

    best_name = ""
    best_filter = None
    for names, command_filter in _iter_group_commands(group_filter):
        for name in names:
            if len(name) <= len(best_name):
                continue
            if text == name or text.startswith(f"{name} "):
                best_name = name
                best_filter = command_filter
    if best_filter is None:
        return None
    return best_name, best_filter


def _require_positionals(parsed: ParsedArgs, count: int, usage: str) -> None:
    if len(parsed.positionals) != count:
        raise AniRssValidationError(f"用法：{usage}")


def _require_at_least(parsed: ParsedArgs, count: int, usage: str) -> None:
    if len(parsed.positionals) < count:
        raise AniRssValidationError(f"用法：{usage}")


def _selector_text(parsed: ParsedArgs, *, drop_last: int = 0) -> str:
    """把位置参数合并为订阅选择器，允许标题包含空格。"""

    positionals = (
        parsed.positionals[: len(parsed.positionals) - drop_last]
        if drop_last
        else parsed.positionals
    )
    selector = " ".join(positionals).strip()
    if not selector:
        raise AniRssValidationError("请指定订阅的 id、id 前缀或标题。")
    return selector


def _optional_list(parsed: ParsedArgs, key: str) -> list[str] | None:
    value = parsed.get(key)
    if value is None:
        return None
    return parse_list(value)


def _parse_episode(raw: str) -> float:
    text = str(raw or "").strip()
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise AniRssValidationError("集数必须是数字，例如 5 或 12.5。") from exc
    if value <= 0:
        raise AniRssValidationError("集数必须大于 0。")
    return value
