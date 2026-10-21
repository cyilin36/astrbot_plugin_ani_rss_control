"""ani-rss 片源搜索的数据归一化与选择工具。

本模块只处理 ani-rss 返回的 JSON 结构与用户输入的引用，不依赖 AstrBot，
也不发起网络请求，便于离线测试。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from .ani_rss_errors import AniRssValidationError

SOURCE_MIKAN = "mikan"
SOURCE_ANIBT = "ani-bt"
SOURCE_ANIME_GARDEN = "anime-garden"

SEARCH_SOURCES = (SOURCE_MIKAN, SOURCE_ANIBT, SOURCE_ANIME_GARDEN)
SOURCE_LABELS = {
    SOURCE_MIKAN: "Mikan",
    SOURCE_ANIBT: "AniBT",
    SOURCE_ANIME_GARDEN: "AnimeGarden",
}

_SOURCE_ALIASES = {
    "mikan": SOURCE_MIKAN,
    "mikanani": SOURCE_MIKAN,
    "ani-bt": SOURCE_ANIBT,
    "anibt": SOURCE_ANIBT,
    "ani_bt": SOURCE_ANIBT,
    "anime-garden": SOURCE_ANIME_GARDEN,
    "animegarden": SOURCE_ANIME_GARDEN,
    "animes-garden": SOURCE_ANIME_GARDEN,
    "anime_garden": SOURCE_ANIME_GARDEN,
}

_SEASON_RE = re.compile(r"^(\d{4})\s*[-/ ]?\s*(春|夏|秋|冬)$")
_SEASON_RE_REVERSED = re.compile(r"^(春|夏|秋|冬)\s*[-/ ]?\s*(\d{4})$")
_BGM_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_EPISODE_PATTERNS = (
    re.compile(r"第\s*(\d{1,4}(?:\.\d+)?)\s*[集话話]"),
    re.compile(r"(?i)\b(?:episode|ep)\s*(\d{1,4}(?:\.\d+)?)\b"),
    re.compile(r"(?i)\bS\d{1,2}E(\d{1,4}(?:\.\d+)?)\b"),
    re.compile(r"\s-\s*(\d{1,4}(?:\.\d+)?)(?:v\d+)?(?=\s|\[|$|\.)"),
)
_BRACKET_EPISODE_RE = re.compile(r"\[(\d{1,4}(?:\.\d+)?)(?:v\d+)?\]")
_YEAR_RANGE = range(1900, 2101)


def normalize_source(raw: str | None, default: str = SOURCE_MIKAN) -> str:
    """把来源名称或别名规范化为标准来源。"""

    value = str(raw or "").strip().lower()
    if not value:
        value = str(default or SOURCE_MIKAN).strip().lower()
    normalized = _SOURCE_ALIASES.get(value)
    if normalized is None:
        raise AniRssValidationError(
            f"不支持的来源：{raw}。可用来源：{', '.join(SEARCH_SOURCES)}。",
        )
    return normalized


def default_search_source(default_rss_type: str | None) -> str:
    """从 RSS 类型配置推导默认搜索来源。"""

    try:
        return normalize_source(default_rss_type, SOURCE_MIKAN)
    except AniRssValidationError:
        return SOURCE_MIKAN


def source_label(source: str) -> str:
    """返回来源的展示名称。"""

    return SOURCE_LABELS.get(source, source)


def make_ref(source: str, target: str) -> str:
    """生成 ``来源:标识`` 形式的引用。"""

    return f"{source}:{target}"


def parse_ref(raw: str, default_source: str = SOURCE_MIKAN) -> tuple[str, str]:
    """解析 ``ref``，返回来源与标识。"""

    value = str(raw or "").strip()
    if not value:
        raise AniRssValidationError("缺少片源引用 ref。")

    prefix, separator, rest = value.partition(":")
    if separator and prefix.strip().lower() in _SOURCE_ALIASES:
        source = normalize_source(prefix)
        target = rest.strip()
    else:
        source = normalize_source(default_source)
        target = value

    if not target:
        raise AniRssValidationError("片源引用缺少标识，例如 mikan:<番剧页地址>。")
    if any(ch.isspace() for ch in target) or any(ord(ch) < 32 for ch in target):
        raise AniRssValidationError("片源引用的标识不能包含空白或控制字符。")
    if len(target) > 2048:
        raise AniRssValidationError("片源引用的标识过长。")

    if source == SOURCE_MIKAN:
        _validate_mikan_target(target)
    else:
        _validate_bgm_id(target)
    return source, target


def parse_mikan_season(raw: str | None) -> dict[str, Any]:
    """解析 ``2026春`` 形式的季度，返回 ani-rss 需要的季度对象。"""

    text = str(raw or "").strip()
    if not text:
        return {}

    match = _SEASON_RE.match(text) or _SEASON_RE_REVERSED.match(text)
    if match is None:
        raise AniRssValidationError(
            "季度格式应为“2026春”或“春2026”，例如 season=2026春。",
        )

    groups = match.groups()
    if groups[0].isdigit():
        year, season = int(groups[0]), groups[1]
    else:
        season, year = groups[0], int(groups[1])

    return {
        "year": year,
        "season": season,
        "seasonLabel": f"{year} {season}",
    }


def normalize_search(source: str, data: Any) -> list[dict[str, Any]]:
    """把三种来源的搜索结果归一化为统一列表。"""

    if source == SOURCE_MIKAN:
        items = _normalize_mikan_search(data)
    elif source == SOURCE_ANIBT:
        items = _normalize_anibt_search(data)
    elif source == SOURCE_ANIME_GARDEN:
        items = _normalize_anime_garden_search(data)
    else:
        raise AniRssValidationError(f"不支持的来源：{source}。")

    return _dedupe(items, key=lambda item: item["target"])


def normalize_groups(
    source: str,
    data: Any,
    target: str = "",
) -> list[dict[str, Any]]:
    """把三种来源的字幕组列表归一化为统一列表。"""

    if source == SOURCE_MIKAN:
        groups = _normalize_mikan_groups(data)
    elif source == SOURCE_ANIBT:
        groups = _normalize_anibt_groups(data, target)
    elif source == SOURCE_ANIME_GARDEN:
        groups = _normalize_anime_garden_groups(data, target)
    else:
        raise AniRssValidationError(f"不支持的来源：{source}。")

    normalized: list[dict[str, Any]] = []
    for group in _dedupe(
        groups,
        key=lambda item: (item["label"].casefold(), item["rss"]),
    ):
        group["index"] = len(normalized) + 1
        normalized.append(group)
    return normalized


def filter_search_items(
    items: Iterable[dict[str, Any]],
    keyword: str,
) -> list[dict[str, Any]]:
    """在本地按标题与关键词过滤搜索结果。"""

    needle = str(keyword or "").strip().casefold()
    if not needle:
        return list(items)

    matched: list[dict[str, Any]] = []
    for item in items:
        title = str(item.get("title") or "").casefold()
        keywords = " ".join(str(value) for value in item.get("keywords") or [])
        if needle in title or needle in keywords.casefold():
            matched.append(item)
    return matched


def select_group(groups: Iterable[dict[str, Any]], selector: str) -> dict[str, Any]:
    """按序号、名称或唯一包含匹配选择字幕组。"""

    items = list(groups)
    if not items:
        raise AniRssValidationError("未找到字幕组。")

    text = str(selector or "").strip()
    if not text:
        raise AniRssValidationError("请提供字幕组序号或名称。")

    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(items):
            return items[index - 1]
        raise AniRssValidationError(
            f"字幕组序号超出范围：{index}，当前共 {len(items)} 组。",
        )

    exact = [item for item in items if str(item["label"]).casefold() == text.casefold()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise _ambiguous_group_error(items)

    matched = [
        item for item in items if text.casefold() in str(item["label"]).casefold()
    ]
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        raise _ambiguous_group_error(items)
    raise AniRssValidationError(
        f"未找到字幕组「{text}」。可用字幕组：{_group_names(items)}。",
    )


def select_episode_items(
    items: Iterable[dict[str, Any]],
    *,
    episode: float | None = None,
    index: int | None = None,
) -> list[dict[str, Any]]:
    """按集数或条目序号选择剧集。"""

    entries = list(items)
    if not entries:
        raise AniRssValidationError("该字幕组没有可用剧集。")

    if index is not None and episode is not None:
        raise AniRssValidationError("集数与条目序号只能指定一个。")

    if index is not None:
        if index < 1 or index > len(entries):
            raise AniRssValidationError(
                f"条目序号超出范围：{index}，当前共 {len(entries)} 条。",
            )
        return [entries[index - 1]]

    if episode is None:
        raise AniRssValidationError("请提供集数，或使用 idx=<条目序号>。")

    target = float(episode)
    matched = [
        item
        for item in entries
        if item.get("episode") is not None
        and abs(float(item["episode"]) - target) < 1e-9
    ]
    if matched:
        return matched

    available = sorted(
        {float(item["episode"]) for item in entries if item.get("episode") is not None},
    )
    if not available:
        raise AniRssValidationError(
            "无法从剧集标题识别集数，请使用 idx=<条目序号> 指定。",
        )
    raise AniRssValidationError(
        f"未找到第 {_number_text(target)} 集。可用集数：{_episode_text(available)}。",
    )


def parse_episode_number(title: str) -> float | None:
    """从剧集标题中解析集数。"""

    text = str(title or "")
    for pattern in _EPISODE_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        value = float(match.group(1))
        if int(value) in _YEAR_RANGE:
            continue
        return value

    for raw in reversed(_BRACKET_EPISODE_RE.findall(text)):
        value = float(raw)
        if int(value) in _YEAR_RANGE:
            continue
        return value
    return None


def _validate_mikan_target(target: str) -> None:
    try:
        parts = urlsplit(target)
    except ValueError as exc:
        raise AniRssValidationError("Mikan 片源地址不是合法的 URL。") from exc
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise AniRssValidationError(
            "Mikan 片源标识必须是番剧页地址；可先用 /ani search 获取 ref。",
        )
    if parts.username or parts.password:
        raise AniRssValidationError("Mikan 片源地址不允许包含用户名或密码。")


def _validate_bgm_id(target: str) -> None:
    if _BGM_ID_RE.fullmatch(target) is None:
        raise AniRssValidationError(
            "BGM 标识只能包含字母、数字、点、下划线和连字符；"
            "可先用 /ani search 获取 ref。",
        )


def _normalize_mikan_search(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, Mapping):
        return []
    items: list[dict[str, Any]] = []
    for week in _as_list(data.get("weeks")):
        if not isinstance(week, Mapping):
            continue
        subtitle = str(week.get("weekLabel") or "").strip()
        for raw in _as_list(week.get("items")):
            if not isinstance(raw, Mapping):
                continue
            target = str(raw.get("url") or "").strip()
            title = str(raw.get("title") or "").strip()
            if not target or not title:
                continue
            items.append(
                {
                    "source": SOURCE_MIKAN,
                    "ref": make_ref(SOURCE_MIKAN, target),
                    "target": target,
                    "title": title,
                    "subtitle": subtitle,
                    "cover": raw.get("cover"),
                    "score": _as_score(raw.get("score")),
                    "exists": raw.get("exists"),
                    "bgm_url": raw.get("bgmUrl"),
                },
            )
    return items


def _normalize_anibt_search(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, Mapping):
        return []
    items: list[dict[str, Any]] = []
    for weekday in _as_list(data.get("byWeekday")):
        if not isinstance(weekday, Mapping):
            continue
        subtitle = str(weekday.get("weekdayLabel") or "").strip()
        for raw in _as_list(weekday.get("animes")):
            if not isinstance(raw, Mapping):
                continue
            target = str(raw.get("bgmId") or "").strip()
            title = _anibt_title(raw.get("title"))
            if not target or not title:
                continue
            items.append(
                {
                    "source": SOURCE_ANIBT,
                    "ref": make_ref(SOURCE_ANIBT, target),
                    "target": target,
                    "title": title,
                    "subtitle": subtitle,
                    "cover": raw.get("cover"),
                    "score": _as_score(raw.get("rating")),
                    "exists": raw.get("exists"),
                    "bgm_url": f"https://bgm.tv/subject/{target}",
                },
            )
    return items


def _normalize_anime_garden_search(data: Any) -> list[dict[str, Any]]:
    weeks = data if isinstance(data, list) else []
    items: list[dict[str, Any]] = []
    for week in weeks:
        if not isinstance(week, Mapping):
            continue
        subtitle = str(week.get("weekLabel") or "").strip()
        for raw in _as_list(week.get("subjects")):
            if not isinstance(raw, Mapping):
                continue
            target = str(raw.get("id") or "").strip()
            title = str(raw.get("name") or "").strip()
            if not target or not title:
                continue
            items.append(
                {
                    "source": SOURCE_ANIME_GARDEN,
                    "ref": make_ref(SOURCE_ANIME_GARDEN, target),
                    "target": target,
                    "title": title,
                    "subtitle": subtitle,
                    "cover": raw.get("cover"),
                    "score": _as_score(raw.get("score")),
                    "exists": raw.get("exists"),
                    "bgm_url": f"https://bgm.tv/subject/{target}",
                    "keywords": [
                        str(keyword)
                        for keyword in _as_list(raw.get("keywords"))
                        if str(keyword).strip()
                    ],
                },
            )
    return items


def _normalize_mikan_groups(data: Any) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for raw in _as_list(data):
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("label") or "").strip()
        rss = str(raw.get("rss") or "").strip()
        if not label or not rss:
            continue
        groups.append(
            {
                "label": label,
                "rss": rss,
                "bgm_url": raw.get("bgmUrl"),
                "bgm_id": _bgm_id_from_url(raw.get("bgmUrl")),
                "update_day": str(raw.get("updateDay") or "").strip(),
                "items": _normalize_items(raw.get("items")),
            },
        )
    return groups


def _normalize_anibt_groups(data: Any, target: str) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for raw in _as_list(data):
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("name") or "").strip()
        rss = str(raw.get("rss") or "").strip()
        if not label or not rss:
            continue
        bgm_id = str(raw.get("bgmId") or target or "").strip()
        groups.append(
            {
                "label": label,
                "rss": rss,
                "bgm_url": f"https://bgm.tv/subject/{bgm_id}" if bgm_id else None,
                "bgm_id": bgm_id,
                "update_day": "",
                "items": _normalize_items(raw.get("items")),
            },
        )
    return groups


def _normalize_anime_garden_groups(
    data: Any,
    target: str,
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for raw in _as_list(data):
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("name") or "").strip()
        rss = str(raw.get("rss") or "").strip()
        if not label or not rss:
            continue
        bgm_id = str(raw.get("bgmId") or target or "").strip()
        groups.append(
            {
                "label": label,
                "rss": rss,
                "bgm_url": f"https://bgm.tv/subject/{bgm_id}" if bgm_id else None,
                "bgm_id": bgm_id,
                "update_day": "",
                "items": _normalize_items(raw.get("items")),
            },
        )
    return groups


def _normalize_items(raw_items: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in _as_list(raw_items):
        if not isinstance(raw, Mapping):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        items.append(
            {
                "index": len(items) + 1,
                "title": title,
                "episode": parse_episode_number(title),
                "magnet": str(raw.get("magnet") or "").strip(),
                "torrent": str(raw.get("torrent") or "").strip(),
                "size": raw.get("formatSize") or _human_size(raw.get("size")),
                "published_at": raw.get("createdAt")
                or raw.get("publishedAt")
                or raw.get("lastUpdatedAt"),
            },
        )
    return items


def _anibt_title(raw: Any) -> str:
    if isinstance(raw, Mapping):
        for key in ("chinese", "primary", "chineseTraditional", "english", "romaji"):
            value = str(raw.get(key) or "").strip()
            if value:
                return value
        return ""
    return str(raw or "").strip()


def _bgm_id_from_url(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    match = re.search(r"/subject/([A-Za-z0-9._-]+)", parts.path)
    return match.group(1) if match else None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_score(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _human_size(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _dedupe(
    items: Iterable[dict[str, Any]],
    *,
    key: Callable[[dict[str, Any]], Any],
) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        marker = key(item)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(item)
    return unique


def _ambiguous_group_error(groups: list[dict[str, Any]]) -> AniRssValidationError:
    return AniRssValidationError(
        f"字幕组匹配到多个结果，请改用序号：{_group_names(groups)}。",
    )


def _group_names(groups: Iterable[dict[str, Any]]) -> str:
    return "、".join(f"{item.get('index')}. {item.get('label')}" for item in groups)


def _number_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _episode_text(values: list[float]) -> str:
    return "、".join(_number_text(value) for value in values[:20]) + (
        " 等" if len(values) > 20 else ""
    )
