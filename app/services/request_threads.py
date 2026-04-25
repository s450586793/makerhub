import asyncio
import functools
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


def _worker_count(env_name: str, default: int) -> int:
    try:
        value = int(os.environ.get(env_name, "") or default)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, 32))


WEB_IO_EXECUTOR = ThreadPoolExecutor(
    max_workers=_worker_count("MAKERHUB_WEB_IO_WORKERS", 4),
    thread_name_prefix="makerhub-web-io",
)
UI_IO_EXECUTOR = ThreadPoolExecutor(
    max_workers=_worker_count("MAKERHUB_UI_IO_WORKERS", 2),
    thread_name_prefix="makerhub-ui-io",
)
TASK_API_EXECUTOR = ThreadPoolExecutor(
    max_workers=_worker_count("MAKERHUB_TASK_API_WORKERS", 2),
    thread_name_prefix="makerhub-task-api",
)


async def run_ui_io(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(UI_IO_EXECUTOR, functools.partial(func, *args, **kwargs))


async def run_web_io(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(WEB_IO_EXECUTOR, functools.partial(func, *args, **kwargs))


async def run_task_api(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(TASK_API_EXECUTOR, functools.partial(func, *args, **kwargs))


def shutdown_request_threads() -> None:
    UI_IO_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    WEB_IO_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    TASK_API_EXECUTOR.shutdown(wait=False, cancel_futures=True)
