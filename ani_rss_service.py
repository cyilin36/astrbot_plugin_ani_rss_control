"""ani-rss 订阅领域服务。

服务层只依赖 HTTP 客户端与参数工具，不导入 AstrBot，便于离线测试。
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .ani_rss_args import parse_bool, parse_int, parse_list
from .ani_rss_client import AniRssClient
from .ani_rss_errors import (
    AniRssAPIError,
    AniRssAuthError,
    AniRssConfigError,
    AniRssConnectionError,
    AniRssError,
    AniRssValidationError,
)

RSS_TYPES = {"mikan", "ani-bt", "anime-garden", "other"}

EDITABLE_FIELDS: dict[str, str] = {
    "title": "title",
    "url": "url",
    "type": "type",
    "bgm": "bgmUrl",
    "subgroup": "subgroup",
    "season": "season",
    "offset": "offset",
    "enable": "enable",
    "total": "totalEpisodeNumber",
    "match": "match",
    "exclude": "exclude",
    "download_path": "customDownloadPathTemplate",
    "custom_download_path": "customDownloadPath",
    "download_new": "downloadNew",
    "omit": "omit",
    "global_exclude": "globalExclude",
    "upload": "upload",
    "message": "message",
}

# 这些字段变化会影响 ani-rss 计算出的下载路径。
_PATH_AFFECTING_FIELDS = {"title", "season", "download_path", "custom_download_path"}
# ani-rss 在写操作中会直接对 title/season 调用 equals，缺失时会抛空指针异常。
_REQUIRED_METADATA_FIELDS = ("title", "season")


class AniRssService:
    """订阅、主 RSS 与备用 RSS 的业务操作。"""

    def __init__(
        self,
        client: AniRssClient,
        *,
        allow_delete_files: bool = False,
        page_size: int = 10,
        default_rss_type: str = "mikan",
        logger: Any | None = None,
    ) -> None:
        self.client = client
        self.allow_delete_files = bool(allow_delete_files)
        self.page_size = max(1, min(50, int(page_size)))
        normalized_type = str(default_rss_type or "mikan").strip().lower()
        self.default_rss_type = (
            normalized_type if normalized_type in RSS_TYPES else "mikan"
        )
        self._logger = logger
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def list_subscriptions(self) -> tuple[list[dict[str, Any]], int]:
        """返回扁平化后的订阅列表与总数。"""

        data = await self.client.list_ani()
        return _flatten_list_ani(data)

    async def search_subscriptions(
        self,
        keyword: str = "",
        page: int = 1,
        size: int | None = None,
    ) -> dict[str, Any]:
        """本地过滤与分页。"""

        items, total = await self.list_subscriptions()
        needle = (keyword or "").strip().lower()
        filtered = (
            items
            if not needle
            else [item for item in items if _matches_keyword(item, needle)]
        )

        current_page = max(1, int(page))
        page_size = max(1, min(50, int(size or self.page_size)))
        pages = max(1, (len(filtered) + page_size - 1) // page_size)
        start = (current_page - 1) * page_size
        page_items = filtered[start : start + page_size]

        return {
            "items": page_items,
            "total": len(filtered),
            "all_total": total,
            "page": current_page,
            "size": page_size,
            "pages": pages,
            "keyword": keyword.strip(),
        }

    async def get_subscription(self, selector: str) -> dict[str, Any]:
        """按选择器获取一个订阅。"""

        items, _ = await self.list_subscriptions()
        return _resolve_subscription(selector, items)

    async def main_show(self, selector: str) -> dict[str, Any]:
        """获取订阅，用于展示主 RSS。"""

        return await self.get_subscription(selector)

    # ------------------------------------------------------------------
    # 添加与修改
    # ------------------------------------------------------------------
    async def add_subscription(
        self,
        *,
        url: str,
        rss_type: str | None = None,
        bgm_url: str | None = None,
        subgroup: str | None = None,
        title: str | None = None,
        season: int | None = None,
        offset: int | None = None,
        enable: bool | None = None,
        match: list[str] | None = None,
        exclude: list[str] | None = None,
        standby_entries: list[dict[str, Any]] | None = None,
        override: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """解析 RSS 并添加订阅。"""

        if override and not confirmed:
            raise AniRssValidationError("覆盖已有订阅需要追加「确认」。")

        await self._require_writable_metadata()

        dto, entries = self._build_rss_dto(
            url=url,
            rss_type=rss_type,
            bgm_url=bgm_url,
            subgroup=subgroup,
            enable=enable,
            standby_entries=standby_entries,
        )

        ani = await self.client.rss_to_ani(dto)
        if not isinstance(ani, dict):
            raise AniRssAPIError("ani-rss 返回的订阅数据无法识别。")

        if title is not None:
            ani["title"] = _require_text(title, "标题", 200)
        if season is not None:
            ani["season"] = _validate_season(season)
        if offset is not None:
            ani["offset"] = _validate_offset(offset)
        if enable is not None:
            ani["enable"] = bool(enable)
        if match is not None:
            ani["match"] = list(match)
        if exclude is not None:
            ani["exclude"] = list(exclude)

        _validate_ani_urls(ani)
        await self.client.add_ani(ani, override=override)

        standby_enabled = await self._standby_enabled_safe() if entries else None
        return {
            "ani": ani,
            "override": override,
            "standby_enabled": standby_enabled,
        }

    async def rss_preview(
        self,
        *,
        url: str,
        rss_type: str | None = None,
        bgm_url: str | None = None,
        subgroup: str | None = None,
        standby_entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """解析 RSS 但不添加。"""

        dto, _ = self._build_rss_dto(
            url=url,
            rss_type=rss_type,
            bgm_url=bgm_url,
            subgroup=subgroup,
            enable=None,
            standby_entries=standby_entries,
        )
        ani = await self.client.rss_to_ani(dto)
        if not isinstance(ani, dict):
            raise AniRssAPIError("ani-rss 返回的订阅数据无法识别。")
        return {"ani": ani}

    async def edit_subscription(
        self,
        selector: str,
        changes: Mapping[str, str],
        *,
        move: bool = False,
    ) -> dict[str, Any]:
        """修改订阅字段。传入的是指令原始字符串。

        ``move`` 为 true 时要求 ani-rss 同时移动已下载的视频文件；该参数
        只在标题、季或下载路径发生变化时才有意义。
        """

        selected = {
            field: raw for field, raw in changes.items() if field in EDITABLE_FIELDS
        }
        unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
        if unknown:
            raise AniRssValidationError(
                f"不支持的字段：{', '.join(unknown)}。",
            )
        if not selected:
            raise AniRssValidationError(
                "未提供要修改的字段。可用字段：" + ", ".join(sorted(EDITABLE_FIELDS)),
            )

        async with self._lock:
            items, _ = await self.list_subscriptions()
            target = _resolve_subscription(selector, items)
            _ensure_writable_metadata(_other_items(items, target))
            before = copy.deepcopy(target)
            updated = copy.deepcopy(target)
            changed: dict[str, tuple[Any, Any]] = {}

            for field, raw in selected.items():
                key = EDITABLE_FIELDS[field]
                value = _normalize_edit_value(field, raw)
                if before.get(key) == value:
                    continue
                updated[key] = value
                changed[field] = (before.get(key), value)

            if not changed:
                return {
                    "ani": before,
                    "changed": {},
                    "move": move,
                    "path_changed": False,
                }

            _validate_ani_urls(updated)
            path_changed = any(field in _PATH_AFFECTING_FIELDS for field in changed)
            await self.client.set_ani(updated, move=move)
            return {
                "ani": updated,
                "changed": changed,
                "move": move,
                "path_changed": path_changed,
            }

    async def set_main_rss(self, selector: str, url: str) -> dict[str, Any]:
        """修改主 RSS。"""

        return await self.edit_subscription(selector, {"url": url})

    # ------------------------------------------------------------------
    # 删除、启停、刷新与总集数
    # ------------------------------------------------------------------
    async def delete_subscriptions(
        self,
        selectors: Iterable[str],
        *,
        delete_files: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """删除订阅。未确认时只返回预览。"""

        selector_list = [item.strip() for item in selectors if item.strip()]
        if not selector_list:
            raise AniRssValidationError("请至少指定一个订阅。")
        if delete_files and not self.allow_delete_files:
            raise AniRssValidationError(
                "配置未允许删除本地文件；本次未执行删除。请先开启 allow_delete_files。",
            )

        async with self._lock:
            items, _ = await self.list_subscriptions()
            targets: list[dict[str, Any]] = []
            seen: set[str] = set()
            for selector in selector_list:
                ani = _resolve_subscription(selector, items)
                ani_id = str(ani.get("id") or "").strip()
                if not ani_id:
                    raise AniRssValidationError(
                        f"订阅「{selector}」缺少 id，无法删除。",
                    )
                if ani_id in seen:
                    continue
                seen.add(ani_id)
                targets.append(ani)

            if not confirmed:
                return {
                    "preview": True,
                    "items": targets,
                    "delete_files": delete_files,
                }

            if delete_files:
                _ensure_files_deletable(targets)
            await self.client.delete_ani(
                [str(ani.get("id")) for ani in targets],
                delete_files=delete_files,
            )
            return {
                "preview": False,
                "items": targets,
                "delete_files": delete_files,
            }

    async def set_enabled(
        self,
        selectors: Iterable[str],
        *,
        enabled: bool,
    ) -> dict[str, Any]:
        """批量启用或停用订阅。"""

        selector_list = [item.strip() for item in selectors if item.strip()]
        if not selector_list:
            raise AniRssValidationError("请至少指定一个订阅。")

        async with self._lock:
            items, _ = await self.list_subscriptions()
            targets = _resolve_many(selector_list, items)
            ids = _ids_for_targets(targets)
            await self.client.batch_enable(ids, value=enabled)
            return {"items": targets, "enabled": enabled}

    async def refresh(self, selectors: Iterable[str] | None = None) -> dict[str, Any]:
        """刷新单个或全部订阅。"""

        selector_list = [item.strip() for item in (selectors or []) if item.strip()]
        if not selector_list or (
            len(selector_list) == 1 and selector_list[0].lower() == "all"
        ):
            await self.client.refresh_all()
            return {"scope": "all"}

        if any(item.lower() == "all" for item in selector_list):
            raise AniRssValidationError(
                "all 不能与其他订阅选择器同时使用。",
            )

        async with self._lock:
            items, _ = await self.list_subscriptions()
            targets = _resolve_many(selector_list, items)
            for ani in targets:
                ani_id = str(ani.get("id") or "").strip()
                if not ani_id:
                    raise AniRssValidationError(
                        f"订阅「{ani.get('title') or '未知'}」缺少 id，无法刷新。",
                    )
                await self.client.refresh_ani(ani_id)
            return {"items": targets}

    async def update_total(
        self,
        selectors: Iterable[str],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """更新总集数。"""

        selector_list = [item.strip() for item in selectors if item.strip()]
        if not selector_list:
            raise AniRssValidationError(
                "请指定 all 或至少一个订阅。",
            )

        async with self._lock:
            items, _ = await self.list_subscriptions()
            if len(selector_list) == 1 and selector_list[0].lower() == "all":
                targets = list(items)
            else:
                if any(item.lower() == "all" for item in selector_list):
                    raise AniRssValidationError(
                        "all 不能与其他订阅选择器同时使用。",
                    )
                targets = _resolve_many(selector_list, items)

            ids = _ids_for_targets(targets)
            if not ids:
                raise AniRssValidationError("没有可更新的订阅。")
            await self.client.update_total_episode_number(ids, force=force)
            return {"count": len(ids), "force": force}

    # ------------------------------------------------------------------
    # 备用 RSS
    # ------------------------------------------------------------------
    async def standby_list(self, selector: str) -> dict[str, Any]:
        """获取订阅，用于展示备用 RSS。"""

        return await self.get_subscription(selector)

    async def standby_add(
        self,
        selector: str,
        url: str,
        *,
        label: str | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """添加一条备用 RSS。"""

        rss_url = validate_rss_url(url)
        label_text = (
            _require_text(label, "字幕组", 100)
            if label is not None and str(label).strip()
            else "未知字幕组"
        )

        async with self._lock:
            items, _ = await self.list_subscriptions()
            target = _resolve_subscription(selector, items)
            _ensure_writable_metadata(_other_items(items, target))
            updated = copy.deepcopy(target)
            entries = _normalized_standby(updated)
            _check_duplicate_url(updated, rss_url)
            offset_value = (
                _validate_offset(offset)
                if offset is not None
                else _default_offset(updated)
            )
            entry = {
                "label": label_text,
                "url": rss_url,
                "offset": offset_value,
            }
            entries.append(entry)
            updated["standbyRssList"] = entries
            await self.client.set_ani(updated)
            standby_enabled = await self._standby_enabled_safe()
            return {
                "action": "add",
                "entry": entry,
                "index": len(entries) - 1,
                "total": len(entries),
                "ani": updated,
                "standby_enabled": standby_enabled,
            }

    async def standby_set(
        self,
        selector: str,
        target: str,
        *,
        url: str | None = None,
        label: str | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """修改一条备用 RSS。"""

        if url is None and label is None and offset is None:
            raise AniRssValidationError(
                "请至少提供 url、label 或 offset 中的一项。",
            )

        async with self._lock:
            items, _ = await self.list_subscriptions()
            ani = _resolve_subscription(selector, items)
            _ensure_writable_metadata(_other_items(items, ani))
            updated = copy.deepcopy(ani)
            entries = _normalized_standby(updated)
            index = _resolve_standby_index(entries, target)
            entry = dict(entries[index])

            if url is not None:
                rss_url = validate_rss_url(url)
                _check_duplicate_url(updated, rss_url, ignore_index=index)
                entry["url"] = rss_url
            if label is not None:
                entry["label"] = _require_text(label, "字幕组", 100)
            if offset is not None:
                entry["offset"] = _validate_offset(offset)

            entries[index] = entry
            updated["standbyRssList"] = entries
            await self.client.set_ani(updated)
            standby_enabled = await self._standby_enabled_safe()
            return {
                "action": "set",
                "entry": entry,
                "index": index,
                "total": len(entries),
                "ani": updated,
                "standby_enabled": standby_enabled,
            }

    async def standby_delete(
        self,
        selector: str,
        target: str,
    ) -> dict[str, Any]:
        """删除一条备用 RSS。"""

        async with self._lock:
            items, _ = await self.list_subscriptions()
            ani = _resolve_subscription(selector, items)
            _ensure_writable_metadata(_other_items(items, ani))
            updated = copy.deepcopy(ani)
            entries = _normalized_standby(updated)
            index = _resolve_standby_index(entries, target)
            removed = entries.pop(index)
            updated["standbyRssList"] = entries
            await self.client.set_ani(updated)
            return {
                "action": "delete",
                "removed": removed,
                "index": index,
                "total": len(entries),
                "ani": updated,
            }

    async def standby_move(
        self,
        selector: str,
        target: str,
        direction: str,
    ) -> dict[str, Any]:
        """调整备用 RSS 顺序。"""

        async with self._lock:
            items, _ = await self.list_subscriptions()
            ani = _resolve_subscription(selector, items)
            _ensure_writable_metadata(_other_items(items, ani))
            updated = copy.deepcopy(ani)
            entries = _normalized_standby(updated)
            index = _resolve_standby_index(entries, target)
            new_index = _resolve_move_target(entries, index, direction)
            if new_index == index:
                return {
                    "action": "move",
                    "entry": entries[index],
                    "index": index,
                    "total": len(entries),
                    "ani": updated,
                    "moved": False,
                }

            entry = entries.pop(index)
            entries.insert(new_index, entry)
            updated["standbyRssList"] = entries
            await self.client.set_ani(updated)
            return {
                "action": "move",
                "entry": entry,
                "index": new_index,
                "total": len(entries),
                "ani": updated,
                "moved": True,
            }

    # ------------------------------------------------------------------
    # 下载与状态
    # ------------------------------------------------------------------
    async def get_download_path(self, selector: str) -> dict[str, Any]:
        """查询订阅的下载路径。"""

        ani = await self.get_subscription(selector)
        data = await self.client.download_path(ani)
        if not isinstance(data, dict):
            raise AniRssAPIError("downloadPath 返回结构无法识别。")
        return {
            "ani": ani,
            "path": str(data.get("downloadPath") or ""),
            "change": bool(data.get("change")),
        }

    async def list_downloads(
        self,
        keyword: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        """读取下载任务列表。"""

        data = await self.client.torrents_infos()
        if data is None:
            data = []
        if not isinstance(data, list):
            raise AniRssAPIError("torrentsInfos 返回结构无法识别。")

        items = [item for item in data if isinstance(item, dict)]
        needle = (keyword or "").strip().lower()
        filtered = (
            items
            if not needle
            else [
                item for item in items if needle in str(item.get("name") or "").lower()
            ]
        )
        page_size = max(1, min(50, int(limit or 10)))
        return {
            "items": filtered[:page_size],
            "matched": len(filtered),
            "total": len(items),
            "keyword": keyword.strip(),
        }

    async def status(self) -> dict[str, Any]:
        """汇总连接、鉴权、版本与备用 RSS 开关。

        以带鉴权的 ``/api/about`` 作为主探测；部分 ani-rss 版本或反向代理
        不提供该接口时，回退到核心的 ``/api/listAni``。``/api/ping`` 不是
        所有部署都可用，且无法验证鉴权，因此不再作为唯一判据。
        """

        result: dict[str, Any] = {
            "base_url": getattr(self.client, "base_url", ""),
            "auth_mode": getattr(self.client, "auth_mode", "none"),
            "ping_ok": False,
            "auth_ok": None,
            "probe": None,
            "about": None,
            "about_error": None,
            "auth_error": None,
            "standby_rss": None,
            "error": None,
        }

        about_error: AniRssError | None = None
        try:
            about = await self.client.about()
        except AniRssAuthError as exc:
            result["ping_ok"] = True
            result["auth_ok"] = False
            result["auth_error"] = str(exc)
            result["about_error"] = str(exc)
            result["probe"] = "/api/about"
            return result
        except AniRssConfigError as exc:
            result["error"] = str(exc)
            return result
        except AniRssError as exc:
            about_error = exc
        else:
            result["ping_ok"] = True
            result["auth_ok"] = True
            result["probe"] = "/api/about"
            if isinstance(about, dict):
                result["about"] = about

        if about_error is not None:
            result["about_error"] = str(about_error)
            try:
                await self.client.list_ani()
            except AniRssAuthError as exc:
                result["ping_ok"] = True
                result["auth_ok"] = False
                result["auth_error"] = str(exc)
                result["probe"] = "/api/listAni"
                return result
            except AniRssConfigError as exc:
                result["error"] = str(exc)
                return result
            except AniRssError as exc:
                result["error"] = str(exc)
                result["ping_ok"] = not isinstance(exc, AniRssConnectionError)
                return result
            else:
                result["ping_ok"] = True
                result["auth_ok"] = True
                result["probe"] = "/api/listAni"

        result["standby_rss"] = await self._standby_enabled_safe()
        return result

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    async def _require_writable_metadata(self) -> list[dict[str, Any]]:
        """读取订阅列表并确认现有数据可安全执行写操作。"""

        items, _ = await self.list_subscriptions()
        _ensure_writable_metadata(items)
        return items

    def _build_rss_dto(
        self,
        *,
        url: str,
        rss_type: str | None,
        bgm_url: str | None,
        subgroup: str | None,
        enable: bool | None,
        standby_entries: list[dict[str, Any]] | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        rss_url = validate_rss_url(url)
        normalized_type = _normalize_rss_type(rss_type, self.default_rss_type)
        _validate_rss_type_constraints(normalized_type, rss_url, bgm_url, subgroup)
        dto: dict[str, Any] = {
            "url": rss_url,
            "type": normalized_type,
        }
        if bgm_url:
            dto["bgmUrl"] = validate_http_url(bgm_url, "BGM 地址")
        if subgroup:
            dto["subgroup"] = _require_text(subgroup, "字幕组", 100)
        if enable is not None:
            dto["enable"] = bool(enable)

        entries = _normalize_new_standby_entries(standby_entries, rss_url)
        if entries:
            dto["standbyRssList"] = entries
        return dto, entries

    async def _standby_enabled_safe(self) -> bool | None:
        checker = getattr(self.client, "is_standby_rss_enabled", None)
        if checker is None:
            return None
        try:
            return await checker()
        except AniRssError:
            return None


def _missing_metadata_fields(ani: dict[str, Any]) -> list[str]:
    """返回 ani-rss 写操作会读取但当前缺失的字段。"""

    return [field for field in _REQUIRED_METADATA_FIELDS if ani.get(field) is None]


def _metadata_label(ani: dict[str, Any]) -> str:
    """生成带 id 与标题状态的订阅简述。"""

    title = ani.get("title")
    if title is None:
        title_text = "标题缺失"
    elif not str(title).strip():
        title_text = "标题为空"
    else:
        title_text = str(title)
    return f"{ani.get('id') or '无 id'} | {title_text}"


def _other_items(
    items: list[dict[str, Any]],
    target: dict[str, Any],
) -> list[dict[str, Any]]:
    """返回除目标订阅外的其他订阅；ani-rss 写操作不会比较目标自身。"""

    target_id = str(target.get("id") or "")
    if target_id:
        return [item for item in items if str(item.get("id") or "") != target_id]
    return [item for item in items if item is not target]


def _ensure_writable_metadata(items: list[dict[str, Any]]) -> None:
    """阻止在缺少 title/season 的数据上执行 ani-rss 写操作。

    上游 addAni/setAni 会直接调用 title.equals 与 season.equals，任一订阅
    缺少这些字段都会抛空指针异常；提前失败可给出可操作的提示。
    """

    issues = [
        (ani, fields) for ani in items if (fields := _missing_metadata_fields(ani))
    ]
    if not issues:
        return

    lines = [
        f"检测到 {len(issues)} 个订阅缺少 title 或 season 字段。"
        "ani-rss 在执行添加或修改订阅时会因空值报错，本次操作已取消。",
        "请先删除这些订阅（使用完整 id，且不要带 files=true），再重新添加：",
    ]
    lines.extend(
        f"- {_metadata_label(ani)} | 缺失：{', '.join(fields)}"
        for ani, fields in issues[:10]
    )
    if len(issues) > 10:
        lines.append(f"... 另有 {len(issues) - 10} 项")
    raise AniRssValidationError("\n".join(lines))


def _ensure_files_deletable(items: list[dict[str, Any]]) -> None:
    """删除本地文件前确认标题存在，避免上游空指针导致静默失败。"""

    issues = [ani for ani in items if ani.get("title") is None]
    if not issues:
        return

    lines = [
        "以下订阅缺少 title 字段，ani-rss 在删除本地文件时会触发空指针错误，"
        "本次未执行删除。",
        "可先不带 files=true 删除这些订阅，再重新添加：",
    ]
    lines.extend(f"- {_metadata_label(ani)}" for ani in issues[:10])
    if len(issues) > 10:
        lines.append(f"... 另有 {len(issues) - 10} 项")
    raise AniRssValidationError("\n".join(lines))


def _validate_rss_type_constraints(
    rss_type: str,
    url: str,
    bgm_url: str | None,
    subgroup: str | None,
) -> None:
    """提前校验各类 RSS 的必填参数，避免上游模糊的解析失败提示。"""

    has_bgm = bool(str(bgm_url or "").strip())
    params = parse_qs(urlsplit(url).query, keep_blank_values=True)

    if rss_type == "other" and not has_bgm:
        raise AniRssValidationError(
            "type=other 时必须同时提供 bgm=<BGM 地址>，否则 ani-rss 会拒绝解析。",
        )
    if rss_type == "mikan" and subgroup and not has_bgm:
        raise AniRssValidationError(
            "mikan 类型单独指定 subgroup 时必须同时提供 bgm；"
            "不指定 subgroup 时可由 ani-rss 自动解析。",
        )
    if rss_type == "ani-bt" and "bgmId" not in params:
        raise AniRssValidationError(
            "ani-bt 类型的 RSS 地址必须包含 bgmId 参数。",
        )
    if rss_type == "anime-garden" and "subject" not in params:
        raise AniRssValidationError(
            "anime-garden 类型的 RSS 地址必须包含 subject 参数。",
        )


def _flatten_list_ani(data: Any) -> tuple[list[dict[str, Any]], int]:
    total = 0

    if isinstance(data, list):
        raw_items = data
        total = len(data)
    elif isinstance(data, dict):
        raw_items = []
        week_list = data.get("weekList")
        if isinstance(week_list, list):
            for week in week_list:
                if isinstance(week, dict) and isinstance(week.get("items"), list):
                    raw_items.extend(
                        item for item in week["items"] if isinstance(item, dict)
                    )
        elif isinstance(data.get("items"), list):
            raw_items.extend(item for item in data["items"] if isinstance(item, dict))
        elif isinstance(data.get("aniList"), list):
            raw_items.extend(item for item in data["aniList"] if isinstance(item, dict))
        elif isinstance(data.get("list"), list):
            raw_items.extend(item for item in data["list"] if isinstance(item, dict))

        total_raw = data.get("total")
        try:
            total = int(total_raw) if total_raw is not None else 0
        except (TypeError, ValueError):
            total = 0
    else:
        raise AniRssAPIError("listAni 返回结构无法识别。")

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_items:
        item_id = str(item.get("id") or "").strip()
        if item_id:
            if item_id in seen:
                continue
            seen.add(item_id)
        unique.append(item)

    if total < len(unique):
        total = len(unique)
    return unique, total


def _resolve_subscription(
    selector: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    if not items:
        raise AniRssValidationError("当前没有订阅。可先用 /ani add <RSS地址> 添加。")

    raw = str(selector or "").strip()
    if not raw:
        raise AniRssValidationError("请指定订阅的 id、id 前缀或标题。")

    title_part, season = _split_selector(raw)

    exact_id = [
        item for item in items if str(item.get("id") or "").lower() == raw.lower()
    ]
    if len(exact_id) == 1:
        return exact_id[0]

    if len(raw) >= 4:
        prefix = [
            item
            for item in items
            if str(item.get("id") or "").lower().startswith(raw.lower())
        ]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise _ambiguous_error(raw, prefix)

    base_title = title_part if title_part is not None else raw
    title_matches = _filter_by_title(items, base_title, season, exact=True)
    if len(title_matches) == 1:
        return title_matches[0]
    if len(title_matches) > 1:
        raise _ambiguous_error(raw, title_matches)

    fuzzy_matches = _filter_by_title(items, base_title, season, exact=False)
    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0]
    if len(fuzzy_matches) > 1:
        raise _ambiguous_error(raw, fuzzy_matches)

    raise AniRssValidationError(
        f"未找到订阅：{raw}\n可先用 /ani list 查看订阅列表。",
    )


def _split_selector(raw: str) -> tuple[str | None, int | None]:
    if "#" not in raw:
        return None, None
    title, suffix = raw.rsplit("#", 1)
    suffix = suffix.strip()
    if suffix.isdigit():
        return title.strip(), int(suffix)
    return None, None


def _filter_by_title(
    items: list[dict[str, Any]],
    title: str,
    season: int | None,
    *,
    exact: bool,
) -> list[dict[str, Any]]:
    needle = title.lower()
    matches: list[dict[str, Any]] = []
    for item in items:
        item_title = str(item.get("title") or "")
        if exact:
            matched = item_title.lower() == needle
        else:
            matched = needle in item_title.lower()
        if not matched:
            continue
        item_season = item.get("season")
        if season is not None and item_season != season:
            continue
        matches.append(item)
    return matches


def _ambiguous_error(
    selector: str,
    matches: list[dict[str, Any]],
) -> AniRssValidationError:
    lines = [f"选择器「{selector}」匹配到多个订阅，请改用完整 id 或更精确的标题："]
    for item in matches[:10]:
        lines.append(_describe_ani(item))
    if len(matches) > 10:
        lines.append(f"... 另有 {len(matches) - 10} 项")
    return AniRssValidationError("\n".join(lines))


def _describe_ani(ani: dict[str, Any]) -> str:
    raw_title = ani.get("title")
    if raw_title is None:
        title = "（标题缺失）"
    else:
        title = str(raw_title).strip() or "（标题为空）"
    season = ani.get("season")
    season_text = f" 第{season}季" if isinstance(season, int) else ""
    return f"- {ani.get('id') or '无 id'} | {title}{season_text}"


def _ids_for_targets(targets: Iterable[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for ani in targets:
        ani_id = str(ani.get("id") or "").strip()
        if not ani_id:
            title = ani.get("title") or "未知订阅"
            raise AniRssValidationError(f"订阅「{title}」缺少 id，无法操作。")
        ids.append(ani_id)
    return ids


def _resolve_many(
    selectors: Iterable[str],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for selector in selectors:
        ani = _resolve_subscription(selector, items)
        ani_id = str(ani.get("id") or "")
        if ani_id and ani_id in seen:
            continue
        if ani_id:
            seen.add(ani_id)
        targets.append(ani)
    return targets


def _normalize_new_standby_entries(
    entries: list[dict[str, Any]] | None,
    main_url: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = {main_url.lower()}
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        url = validate_rss_url(str(raw.get("url") or ""))
        if url.lower() in seen:
            raise AniRssValidationError(f"备用 RSS 地址重复或与主 RSS 相同：{url}")
        seen.add(url.lower())
        label = str(raw.get("label") or "").strip() or "未知字幕组"
        label = _require_text(label, "字幕组", 100)
        offset_raw = raw.get("offset")
        offset = 0 if offset_raw is None else _validate_offset(offset_raw)
        normalized.append({"label": label, "url": url, "offset": offset})
    return normalized


def _normalized_standby(ani: dict[str, Any]) -> list[dict[str, Any]]:
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
        label = str(item.get("label") or "").strip() or "未知字幕组"
        offset_raw = item.get("offset", 0)
        offset = 0 if offset_raw is None else offset_raw
        if not isinstance(offset, int) or isinstance(offset, bool):
            offset = 0
        entries.append({"label": label, "url": url, "offset": offset})
    return entries


def _validate_ani_urls(ani: dict[str, Any]) -> None:
    main_url = str(ani.get("url") or "").strip().lower()
    seen: set[str] = set()
    for index, entry in enumerate(_normalized_standby(ani), start=1):
        url = str(entry.get("url") or "").strip().lower()
        if not url:
            continue
        if main_url and url == main_url:
            raise AniRssValidationError(
                f"备用 RSS 第 {index} 项与主 RSS 地址相同：{entry.get('url')}",
            )
        if url in seen:
            raise AniRssValidationError(
                f"备用 RSS 第 {index} 项地址重复：{entry.get('url')}",
            )
        seen.add(url)


def _check_duplicate_url(
    ani: dict[str, Any],
    url: str,
    *,
    ignore_index: int | None = None,
) -> None:
    target = url.strip().lower()
    main_url = str(ani.get("url") or "").strip().lower()
    if main_url and target == main_url:
        raise AniRssValidationError(f"该 RSS 已是主 RSS：{url}")

    for index, entry in enumerate(_normalized_standby(ani)):
        if index == ignore_index:
            continue
        if str(entry.get("url") or "").strip().lower() == target:
            raise AniRssValidationError(
                f"该 RSS 已存在为第 {index + 1} 项备用 RSS：{url}",
            )


def _resolve_standby_index(entries: list[dict[str, Any]], target: str) -> int:
    if not entries:
        raise AniRssValidationError("该订阅没有备用 RSS。")

    raw = str(target or "").strip()
    if not raw:
        raise AniRssValidationError("请指定备用 RSS 的序号或标签。")

    if raw.isdigit():
        index = int(raw) - 1
        if 0 <= index < len(entries):
            return index
        raise AniRssValidationError(
            f"备用 RSS 序号超出范围，应为 1 到 {len(entries)}。",
        )

    needle = raw.lower()
    exact = [
        index
        for index, entry in enumerate(entries)
        if str(entry.get("label") or "").lower() == needle
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise _standby_ambiguous_error(raw, entries, exact)

    fuzzy = [
        index
        for index, entry in enumerate(entries)
        if needle in str(entry.get("label") or "").lower()
    ]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        raise _standby_ambiguous_error(raw, entries, fuzzy)

    raise AniRssValidationError(f"未找到备用 RSS：{raw}")


def _standby_ambiguous_error(
    target: str,
    entries: list[dict[str, Any]],
    indexes: list[int],
) -> AniRssValidationError:
    lines = [f"备用 RSS 标签「{target}」匹配到多项，请改用序号："]
    for index in indexes:
        entry = entries[index]
        lines.append(f"- {index + 1}. {entry.get('label')} | {entry.get('url')}")
    return AniRssValidationError("\n".join(lines))


def _resolve_move_target(
    entries: list[dict[str, Any]],
    index: int,
    direction: str,
) -> int:
    raw = str(direction or "").strip().lower()
    if raw in {"up", "上移"}:
        if index == 0:
            raise AniRssValidationError("已经是第一个备用 RSS。")
        return index - 1
    if raw in {"down", "下移"}:
        if index >= len(entries) - 1:
            raise AniRssValidationError("已经是最后一个备用 RSS。")
        return index + 1
    if raw.startswith("to="):
        position = parse_int(raw[3:], "目标位置")
        if not 1 <= position <= len(entries):
            raise AniRssValidationError(
                f"目标位置超出范围，应为 1 到 {len(entries)}。",
            )
        return position - 1
    raise AniRssValidationError("移动方向支持 up、down 或 to=序号。")


def _default_offset(ani: dict[str, Any]) -> int:
    value = ani.get("offset")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _normalize_edit_value(field: str, raw: str) -> Any:
    if field == "title":
        return _require_text(raw, "标题", 200)
    if field == "url":
        return validate_rss_url(raw)
    if field == "type":
        return _normalize_rss_type(raw, "mikan")
    if field == "bgm":
        text = str(raw or "").strip()
        return validate_http_url(text, "BGM 地址") if text else ""
    if field == "subgroup":
        return _require_text(raw, "字幕组", 100)
    if field == "season":
        return _validate_season(parse_int(raw, "季", minimum=1, maximum=99))
    if field == "offset":
        return _validate_offset(parse_int(raw, "偏移"))
    if field == "enable":
        return parse_bool(raw, "启用")
    if field == "total":
        return parse_int(raw, "总集数", minimum=0, maximum=100000)
    if field in {"match", "exclude"}:
        return parse_list(raw)
    if field == "download_path":
        text = str(raw or "").strip()
        if len(text) > 1000:
            raise AniRssValidationError("下载路径模板不能超过 1000 个字符。")
        return text
    if field in {
        "custom_download_path",
        "download_new",
        "omit",
        "global_exclude",
        "upload",
        "message",
    }:
        return parse_bool(raw, field)
    raise AniRssValidationError(f"不支持的字段：{field}")


def _normalize_rss_type(raw: str | None, default: str) -> str:
    value = str(raw or default).strip().lower()
    if value not in RSS_TYPES:
        raise AniRssValidationError(
            f"RSS 类型必须是：{', '.join(sorted(RSS_TYPES))}。",
        )
    return value


def _validate_season(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise AniRssValidationError("季必须是整数。")
    if not 1 <= value <= 99:
        raise AniRssValidationError("季必须在 1 到 99 之间。")
    return value


def _validate_offset(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise AniRssValidationError("偏移必须是整数。")
    if not -10000 <= value <= 10000:
        raise AniRssValidationError("偏移必须在 -10000 到 10000 之间。")
    return value


def _require_text(value: Any, name: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise AniRssValidationError(f"{name}不能为空。")
    if len(text) > max_length:
        raise AniRssValidationError(f"{name}不能超过 {max_length} 个字符。")
    return text


def validate_rss_url(raw: str) -> str:
    """校验 RSS 地址。"""

    value = str(raw or "").strip()
    if not value:
        raise AniRssValidationError("RSS 地址不能为空。")
    if len(value) > 4096:
        raise AniRssValidationError("RSS 地址过长。")
    if any(ch.isspace() for ch in value) or any(ord(ch) < 32 for ch in value):
        raise AniRssValidationError("RSS 地址不能包含空白或控制字符。")

    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise AniRssValidationError("RSS 地址不是合法的 URL。") from exc

    if parts.scheme not in {"http", "https"}:
        raise AniRssValidationError("RSS 地址必须以 http:// 或 https:// 开头。")
    if not parts.netloc:
        raise AniRssValidationError("RSS 地址缺少主机名。")
    if parts.username or parts.password:
        raise AniRssValidationError("RSS 地址不允许包含用户名或密码。")
    return value


def validate_http_url(raw: str, field: str) -> str:
    """校验 BGM 等普通 HTTP 地址。"""

    value = str(raw or "").strip()
    if not value:
        raise AniRssValidationError(f"{field}不能为空。")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise AniRssValidationError(f"{field}不是合法的 URL。") from exc
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise AniRssValidationError(f"{field}必须以 http:// 或 https:// 开头。")
    if parts.username or parts.password:
        raise AniRssValidationError(f"{field}不允许包含用户名或密码。")
    return value


def _matches_keyword(ani: dict[str, Any], needle: str) -> bool:
    fields = ("title", "id", "subgroup", "type")
    return any(needle in str(ani.get(field) or "").lower() for field in fields)
