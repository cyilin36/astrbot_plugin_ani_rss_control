"""指令参数解析与基础类型校验。"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field

from .ani_rss_errors import AniRssValidationError

_CONFIRM_WORDS = {"确认", "確認", "確定", "确定", "confirm"}
_KV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*=")
_TRUE_WORDS = {"1", "true", "yes", "on", "开", "是"}
_FALSE_WORDS = {"0", "false", "no", "off", "关", "否"}


@dataclass
class ParsedArgs:
    """解析后的指令参数。"""

    positionals: list[str] = field(default_factory=list)
    options: dict[str, list[str]] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)

    def get(self, key: str, default: str | None = None) -> str | None:
        values = self.options.get(key)
        if not values:
            return default
        return values[-1]

    def get_all(self, key: str) -> list[str]:
        return list(self.options.get(key, []))

    def has(self, key: str) -> bool:
        return key in self.options or key in self.flags

    def has_flag(self, flag: str) -> bool:
        return flag in self.flags

    def bool(self, key: str, default: bool | None = None) -> bool | None:
        value = self.get(key)
        if value is None:
            return default
        return parse_bool(value, key)

    def int(
        self,
        key: str,
        default: int | None = None,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int | None:
        value = self.get(key)
        if value is None:
            return default
        return parse_int(value, key, minimum=minimum, maximum=maximum)


def parse_args(
    text: str,
    *,
    allowed_options: Iterable[str] = (),
    allowed_flags: Iterable[str] = (),
) -> ParsedArgs:
    """解析形如 ``key=value --flag positional`` 的参数字符串。"""

    options = {name.replace("-", "_") for name in allowed_options}
    flags = {name.replace("-", "_") for name in allowed_flags}
    result = ParsedArgs()

    raw = (text or "").strip()
    if not raw:
        return result

    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise AniRssValidationError("参数引号不匹配，请检查引号是否成对。") from exc

    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1

        if token in _CONFIRM_WORDS:
            result.flags.add("confirm")
            continue

        if token == "--":
            result.positionals.extend(tokens[index:])
            break

        if token.startswith("--"):
            body = token[2:]
            if not body:
                continue
            if "=" in body:
                key, value = body.split("=", 1)
            else:
                key, value = body, None
            key = key.replace("-", "_").lower()
            if key == "confirm":
                result.flags.add("confirm")
                continue
            if key in options:
                if value is None:
                    if key in flags:
                        result.flags.add(key)
                        result.options.setdefault(key, []).append("true")
                        continue
                    raise AniRssValidationError(f"参数 --{key} 需要值。")
                result.options.setdefault(key, []).append(value)
                continue
            if key in flags:
                result.flags.add(key)
                result.options.setdefault(key, []).append("true")
                continue
            raise AniRssValidationError(f"不支持的参数：--{key}")

        if "=" in token and _KV_KEY_RE.match(token):
            key, value = token.split("=", 1)
            normalized = key.replace("-", "_").lower()
            if normalized == "confirm":
                result.flags.add("confirm")
                continue
            if normalized in options:
                result.options.setdefault(normalized, []).append(value)
                continue
            if normalized in flags:
                result.flags.add(normalized)
                result.options.setdefault(normalized, []).append(value or "true")
                continue
            raise AniRssValidationError(f"不支持的参数：{key}")

        result.positionals.append(token)

    return result


def parse_bool(value: str, name: str) -> bool:
    """解析布尔值。"""

    normalized = str(value).strip().lower()
    if normalized in _TRUE_WORDS:
        return True
    if normalized in _FALSE_WORDS:
        return False
    raise AniRssValidationError(f"{name} 必须是布尔值（true/false）。")


def parse_int(
    value: str,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """解析整数并检查范围。"""

    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise AniRssValidationError(f"{name} 必须是整数。") from exc

    if minimum is not None and parsed < minimum:
        raise AniRssValidationError(f"{name} 不能小于 {minimum}。")
    if maximum is not None and parsed > maximum:
        raise AniRssValidationError(f"{name} 不能大于 {maximum}。")
    return parsed


def parse_list(value: str) -> list[str]:
    """解析列表。

    默认按 ``|`` 分隔；如果以 ``[`` 开头，则按 JSON 字符串数组解析，
    便于包含逗号或竖线的匹配规则。
    """

    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AniRssValidationError("列表不是合法的 JSON 数组。") from exc
        if not isinstance(parsed, list):
            raise AniRssValidationError("列表必须是 JSON 数组。")
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in text.split("|") if item.strip()]


def parse_standby_specs(values: Iterable[str]) -> list[dict[str, object]]:
    """解析 ``标签|URL|偏移`` 形式的备用 RSS 规格。"""

    specs: list[dict[str, object]] = []
    for value in values:
        parts = str(value).split("|")
        if len(parts) > 3:
            raise AniRssValidationError(
                "备用 RSS 格式应为 标签|URL|偏移；如果标签或 URL 含有竖线，请改用 standby 参数分开填写。",
            )

        if len(parts) >= 2:
            label = parts[0].strip()
            url = parts[1].strip()
        else:
            label = ""
            url = parts[0].strip()

        if not url:
            raise AniRssValidationError("备用 RSS 地址不能为空。")

        offset: int | None = None
        if len(parts) == 3 and parts[2].strip():
            offset = parse_int(parts[2], "备用 RSS 偏移")

        specs.append(
            {
                "label": label or None,
                "url": url,
                "offset": offset,
            },
        )
    return specs


def expand_selectors(positionals: Iterable[str]) -> list[str]:
    """把位置参数展开为订阅选择器列表，支持逗号分隔。"""

    selectors: list[str] = []
    for item in positionals:
        token = str(item).strip()
        if not token:
            continue
        if "," in token:
            selectors.extend(part.strip() for part in token.split(",") if part.strip())
        else:
            selectors.append(token)
    return selectors


def join_keyword(positionals: Iterable[str]) -> str:
    """把位置参数合并为关键词。"""

    return " ".join(str(item) for item in positionals).strip()
