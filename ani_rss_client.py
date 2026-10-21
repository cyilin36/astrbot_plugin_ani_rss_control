"""ani-rss Web API 客户端。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .ani_rss_errors import (
    AniRssAPIError,
    AniRssAuthError,
    AniRssConfigError,
    AniRssConnectionError,
    AniRssDuplicateError,
)

_NO_BODY = object()
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_DUPLICATE_MESSAGES = ("此订阅已存在", "订阅标题重复", "已存在", "标题重复")


def normalize_base_url(raw: str) -> str:
    """规范化 ani-rss 地址。"""

    value = (raw or "").strip()
    if not value:
        raise AniRssConfigError("未配置 ani-rss 地址 base_url。")

    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise AniRssConfigError("base_url 不是合法的 URL。") from exc

    if parts.scheme not in {"http", "https"}:
        raise AniRssConfigError("base_url 必须以 http:// 或 https:// 开头。")
    if not parts.hostname:
        raise AniRssConfigError("base_url 缺少主机名。")
    if any(ch.isspace() for ch in value) or any(ord(ch) < 32 for ch in value):
        raise AniRssConfigError("base_url 不能包含空白或控制字符。")
    if parts.username or parts.password:
        raise AniRssConfigError("base_url 不允许包含用户名或密码。")
    if parts.query or parts.fragment:
        raise AniRssConfigError("base_url 不允许包含查询参数或片段。")

    path = parts.path.rstrip("/")
    if path.endswith("/api"):
        path = path[:-4].rstrip("/")

    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def build_api_url(base_url: str, endpoint: str) -> str:
    """拼接 ani-rss API 地址。"""

    return f"{base_url.rstrip('/')}/api/{endpoint.lstrip('/')}"


class AniRssClient:
    """通过 ani-rss Web API 执行请求。"""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        token: str = "",
        username: str = "",
        password: str = "",
        timeout: int = 15,
        rss_timeout: int = 60,
        verify_tls: bool = True,
        logger: Any | None = None,
        session: Any | None = None,
    ) -> None:
        self._raw_base_url = (base_url or "").strip()
        self._config_error: AniRssConfigError | None = None
        try:
            self._base_url = normalize_base_url(self._raw_base_url)
        except AniRssConfigError as exc:
            self._base_url = ""
            self._config_error = exc

        self._api_key = (api_key or "").strip()
        self._token = (token or "").strip()
        self._username = (username or "").strip()
        self._password = password or ""
        self._auth_mode = self._resolve_auth_mode()

        if self._auth_mode == "password" and (not self._username or not self._password):
            self._config_error = AniRssConfigError(
                "账号登录需要同时配置 username 和 password。",
            )

        self._timeout = max(1, min(120, int(timeout)))
        self._rss_timeout = max(5, min(300, int(rss_timeout)))
        self._verify_tls = bool(verify_tls)
        self._logger = logger
        self._external_session = session
        self._session = session
        self._owns_session = session is None
        self._login_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()

    def _resolve_auth_mode(self) -> str:
        if self._api_key:
            return "api_key"
        if self._token:
            return "token"
        if self._username or self._password:
            return "password"
        return "none"

    @property
    def base_url(self) -> str:
        """返回规范化后的地址；配置错误时返回原始输入。"""

        return self._base_url or self._raw_base_url

    @property
    def auth_mode(self) -> str:
        """返回当前鉴权方式。"""

        return self._auth_mode

    @property
    def is_configured(self) -> bool:
        """返回配置是否可用。"""

        return self._config_error is None

    async def close(self) -> None:
        """关闭内部创建的 HTTP 会话。"""

        session = self._session
        self._session = None
        if (
            self._owns_session
            and session is not None
            and not getattr(session, "closed", False)
        ):
            try:
                await session.close()
            except Exception:  # noqa: BLE001 - 关闭阶段不再向上抛出
                self._debug("关闭 ani-rss HTTP 会话失败")
        self._owns_session = False

    async def ping(self) -> Any:
        """存活检查，不需要鉴权。"""

        return await self._request("ping", authenticate=False)

    async def about(self) -> Any:
        """获取 ani-rss 版本与更新信息。"""

        return await self._request("about")

    async def list_ani(self) -> Any:
        """获取订阅列表原始数据。"""

        return await self._request("listAni")

    async def add_ani(self, ani: Mapping[str, Any], *, override: bool = False) -> Any:
        """添加订阅。"""

        return await self._request(
            "addAni",
            payload=dict(ani),
            params={"override": override},
        )

    async def set_ani(
        self,
        ani: Mapping[str, Any],
        *,
        move: bool = False,
    ) -> Any:
        """修改订阅。

        ``move`` 对应 ani-rss 的 ``move`` 查询参数，仅在为 true 时移动
        已下载的视频文件。
        """

        return await self._request(
            "setAni",
            payload=dict(ani),
            params={"move": move},
        )

    async def delete_ani(
        self,
        ids: list[str],
        *,
        delete_files: bool = False,
    ) -> Any:
        """删除订阅。"""

        return await self._request(
            "deleteAni",
            payload=list(ids),
            params={"deleteFiles": delete_files},
        )

    async def batch_enable(self, ids: list[str], *, value: bool) -> Any:
        """批量启用或停用订阅。"""

        return await self._request(
            "batchEnable",
            payload=list(ids),
            params={"value": value},
        )

    async def refresh_ani(self, ani_id: str) -> Any:
        """刷新单个订阅。"""

        return await self._request("refreshAni", payload={"id": ani_id})

    async def refresh_all(self) -> Any:
        """刷新全部订阅。"""

        return await self._request("refreshAll")

    async def update_total_episode_number(
        self,
        ids: list[str],
        *,
        force: bool = False,
    ) -> Any:
        """更新总集数。"""

        return await self._request(
            "updateTotalEpisodeNumber",
            payload=list(ids),
            params={"force": force},
        )

    async def rss_to_ani(self, dto: Mapping[str, Any]) -> Any:
        """把 RSS 解析为订阅对象。"""

        return await self._request(
            "rssToAni",
            payload=dict(dto),
            timeout=self._rss_timeout,
        )

    async def download_path(self, ani: Mapping[str, Any]) -> Any:
        """查询订阅的下载路径。"""

        return await self._request("downloadPath", payload=dict(ani))

    async def torrents_infos(self) -> Any:
        """获取下载任务列表。"""

        return await self._request("torrentsInfos")

    # ------------------------------------------------------------------
    # 片源搜索
    # ------------------------------------------------------------------
    async def search_mikan(
        self,
        text: str,
        season: Mapping[str, Any] | None = None,
    ) -> Any:
        """按关键词与季度搜索 Mikan 番剧。"""

        return await self._request(
            "mikan",
            payload=dict(season or {}),
            params={"text": text},
            timeout=self._rss_timeout,
        )

    async def mikan_groups(self, url: str) -> Any:
        """获取 Mikan 番剧页的字幕组与剧集。"""

        return await self._request(
            "mikanGroup",
            params={"url": url},
            timeout=self._rss_timeout,
        )

    async def search_anibt(
        self,
        *,
        title: str = "",
        season: str = "",
        bgm_url: str = "",
    ) -> Any:
        """搜索 AniBT 番剧。"""

        payload = {
            "title": title,
            "season": season,
            "bgmUrl": bgm_url,
        }
        return await self._request(
            "aniBT",
            payload=payload,
            timeout=self._rss_timeout,
        )

    async def anibt_groups(self, bgm_id: str) -> Any:
        """获取 AniBT 番剧的字幕组与剧集。"""

        return await self._request(
            "aniBTGroup",
            params={"bgmId": bgm_id},
            timeout=self._rss_timeout,
        )

    async def anime_garden_list(self, bgm_url: str = "") -> Any:
        """获取 AnimeGarden 番剧列表或单部番剧。"""

        return await self._request(
            "animeGardenList",
            params={"bgmUrl": bgm_url},
            timeout=self._rss_timeout,
        )

    async def anime_garden_groups(self, bgm_id: str) -> Any:
        """获取 AnimeGarden 番剧的字幕组与剧集。"""

        return await self._request(
            "animeGardenGroup",
            params={"bgmId": bgm_id},
            timeout=self._rss_timeout,
        )

    async def get_config(self) -> Any:
        """获取 ani-rss 配置。

        仅用于读取少量非敏感开关；调用方不得记录或展示完整配置。
        """

        return await self._request("config")

    async def is_standby_rss_enabled(self) -> bool | None:
        """读取备用 RSS 全局开关。"""

        data = await self.get_config()
        if not isinstance(data, dict):
            return None
        value = data.get("standbyRss")
        return value if isinstance(value, bool) else None

    async def _request(
        self,
        endpoint: str,
        *,
        payload: Any = _NO_BODY,
        params: Mapping[str, Any] | None = None,
        timeout: int | None = None,
        authenticate: bool = True,
        auth_retry: bool = True,
    ) -> Any:
        if self._config_error is not None:
            raise self._config_error

        try:
            return await self._request_once(
                endpoint,
                payload=payload,
                params=params,
                timeout=timeout,
                authenticate=authenticate,
            )
        except AniRssAuthError:
            if (
                authenticate
                and auth_retry
                and self._auth_mode == "password"
                and self._token
            ):
                self._token = ""
                return await self._request_once(
                    endpoint,
                    payload=payload,
                    params=params,
                    timeout=timeout,
                    authenticate=authenticate,
                )
            raise

    async def _request_once(
        self,
        endpoint: str,
        *,
        payload: Any,
        params: Mapping[str, Any] | None,
        timeout: int | None,
        authenticate: bool,
    ) -> Any:
        session = await self._ensure_session()
        url = build_api_url(self._base_url, endpoint)
        headers = {"Accept": "application/json"}
        if authenticate:
            headers.update(await self._auth_headers())

        kwargs: dict[str, Any] = {
            "headers": headers,
            "allow_redirects": False,
        }
        if params:
            kwargs["params"] = {
                key: _param_value(value) for key, value in params.items()
            }
        if payload is not _NO_BODY:
            kwargs["json"] = payload
        kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout or self._timeout)

        try:
            async with session.post(url, **kwargs) as response:
                status = int(response.status)
                text = await response.text(errors="replace")
        except asyncio.TimeoutError as exc:
            raise AniRssConnectionError(
                f"连接 ani-rss 超时（{timeout or self._timeout} 秒）。",
            ) from exc
        except aiohttp.ClientError as exc:
            raise AniRssConnectionError(f"连接 ani-rss 失败：{exc}") from exc
        except OSError as exc:
            raise AniRssConnectionError(f"连接 ani-rss 失败：{exc}") from exc

        if len(text.encode("utf-8", errors="replace")) > _MAX_RESPONSE_BYTES:
            raise AniRssAPIError(
                "ani-rss 响应体过大，已停止处理。",
                status=status,
            )

        body: Any
        try:
            body = json.loads(text) if text.strip() else None
        except json.JSONDecodeError:
            body = None

        if status in {301, 302, 303, 307, 308}:
            raise AniRssAPIError(
                "ani-rss 返回了重定向响应；为避免泄露鉴权头，插件不跟随跳转。",
                status=status,
            )

        if status in {401, 403}:
            raise AniRssAuthError(_extract_message(body) or "ani-rss 鉴权失败。")

        if status >= 400:
            message = _extract_message(body)
            if not message:
                message = f"HTTP {status}"
                if text.strip():
                    message += f"：{_snippet(text)}"
            raise AniRssAPIError(message, status=status)

        if body is None:
            if not text.strip():
                return None
            raise AniRssAPIError(
                f"ani-rss 返回了非 JSON 响应（HTTP {status}）：{_snippet(text)}",
                status=status,
            )

        if not isinstance(body, dict):
            return body

        code_raw = body.get("code")
        if code_raw is None:
            return body.get("data") if "data" in body else body

        try:
            code = int(code_raw)
        except (TypeError, ValueError):
            return body

        if 200 <= code < 300:
            return body.get("data")

        message = str(body.get("message") or "未知错误")
        if code in {401, 403}:
            raise AniRssAuthError(message)

        if any(marker in message for marker in _DUPLICATE_MESSAGES):
            raise AniRssDuplicateError(message, code=code, status=status)

        raise AniRssAPIError(message, code=code, status=status)

    async def _ensure_session(self) -> Any:
        session = self._session
        if session is not None and not getattr(session, "closed", False):
            return session

        async with self._session_lock:
            session = self._session
            if session is not None and not getattr(session, "closed", False):
                return session
            connector = aiohttp.TCPConnector(ssl=self._verify_tls)
            self._session = aiohttp.ClientSession(connector=connector)
            self._owns_session = True
            return self._session

    async def _auth_headers(self) -> dict[str, str]:
        if self._api_key:
            return {"api-key": self._api_key}
        if self._token:
            return {"Authorization": self._token}
        if self._auth_mode == "password":
            token = await self._login()
            return {"Authorization": token}
        return {}

    async def _login(self) -> str:
        async with self._login_lock:
            if self._token:
                return self._token

            payload = {
                "username": self._username,
                "password": hashlib.md5(
                    self._password.encode("utf-8"),
                ).hexdigest(),
            }
            try:
                data = await self._request(
                    "login",
                    payload=payload,
                    authenticate=False,
                    auth_retry=False,
                )
            except AniRssAPIError as exc:
                raise AniRssAuthError(f"登录失败：{exc.message}") from exc

            if not isinstance(data, str) or not data.strip():
                raise AniRssAuthError("登录失败：ani-rss 未返回 Token。")
            self._token = data.strip()
            return self._token

    def _debug(self, message: str) -> None:
        if self._logger is not None:
            self._logger.debug(message)


def _param_value(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _extract_message(body: Any) -> str:
    if isinstance(body, dict):
        message = body.get("message")
        if message:
            return str(message)
    return ""


def _snippet(text: str, limit: int = 200) -> str:
    compact = " ".join(str(text).split())
    if not compact:
        return "（空响应）"
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."
