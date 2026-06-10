"""异步重试装饰器。失败 → 指数退避 → 重试。"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import TypeVar

from loguru import logger

T = TypeVar("T")


def async_retry(
    attempts: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """在异步函数上加上有限次数的重试。

    Args:
        attempts: 总尝试次数（含首次）。3 表示首次失败后再试 2 次。
        base_delay: 第 1 次重试前等待的秒数。
        backoff: 每次失败后延迟倍数。base_delay=1, backoff=2 → 1s, 2s, 4s ...
        exceptions: 哪些异常会触发重试。其他异常直接抛出。
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            delay = base_delay
            last_exc: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt >= attempts:
                        logger.error(
                            f"{func.__name__} 第 {attempt}/{attempts} 次仍失败：{e!r}"
                        )
                        raise
                    logger.warning(
                        f"{func.__name__} 第 {attempt}/{attempts} 次失败：{e!r}，"
                        f"{delay:.1f}s 后重试"
                    )
                    await asyncio.sleep(delay)
                    delay *= backoff
            # 理论不可达
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator
