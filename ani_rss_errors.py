"""插件使用的异常类型。

所有异常文本都应能直接展示给用户，且不得包含 API Key、密码、Token 或请求头。
"""

from __future__ import annotations


class AniRssError(Exception):
    """ani-rss 操作异常基类。"""


class AniRssConfigError(AniRssError):
    """配置缺失或格式不合法。"""


class AniRssValidationError(AniRssError):
    """用户输入不合法。"""


class AniRssConnectionError(AniRssError):
    """无法连接 ani-rss。"""


class AniRssAuthError(AniRssError):
    """ani-rss 鉴权失败。"""


class AniRssAPIError(AniRssError):
    """ani-rss 返回业务错误。"""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class AniRssDuplicateError(AniRssAPIError):
    """订阅已存在或标题重复。"""
