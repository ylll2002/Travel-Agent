"""跨平台子进程工具：定位各 Agent 虚拟环境的解释器，并构造子进程环境。

背景：项目原本在 macOS 上开发，各 Agent 的解释器路径写成了 POSIX 布局
（``<component>/.venv/bin/python``）。Windows 的 venv 布局是
``<component>/.venv/Scripts/python.exe``，因此原先的常量在 Windows 上指向
不存在的文件，``subprocess`` 会抛 ``FileNotFoundError: [WinError 2]``。

本模块统一处理三件事：

1. **解释器解析**：按平台顺序探测候选路径，命中即用；都没有时回退到当前
   解释器（``sys.executable``），保证在未建 venv 的组件上仍可运行。
2. **子进程环境**：强制子进程以 UTF-8 收发标准流，避免 Windows 默认使用
   GBK/cp936 导致中文行程内容抛 ``UnicodeDecodeError``；同时清理 macOS venv
   的 ``__PYVENV_LAUNCHER__`` 变量，防止污染子进程的 venv 解析。
3. **统一入口**：``component_python`` / ``component_script`` / ``subprocess_env``
   三个函数，供 backend 路由与各 Agent 脚本共用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "REPO_ROOT",
    "component_dir",
    "component_python",
    "component_script",
    "subprocess_env",
]

#: 仓库根目录（本文件所在目录）。
REPO_ROOT = Path(__file__).resolve().parent


def component_dir(component: str) -> Path:
    """返回组件目录，例如 ``<repo>/SearchAgent``。"""
    return REPO_ROOT / component


def _python_candidates(venv: Path) -> tuple[Path, ...]:
    """按当前平台优先的顺序返回 venv 解释器候选路径。

    两个平台都列出来，而不是靠字符串替换或 ``platform.system()`` 判断——直接看
    文件是否存在最可靠，也能正确处理 WSL、Git Bash 等混用场景。
    """
    windows = venv / "Scripts" / "python.exe"
    posix = venv / "bin" / "python"
    if os.name == "nt":
        return (windows, posix)
    return (posix, windows)


def component_python(component: str, fallback: Path | None = None) -> Path:
    """定位组件的 venv 解释器。

    Args:
        component: 组件目录名，如 ``"SearchAgent"``。
        fallback: 找不到 venv 时使用的解释器；默认 ``sys.executable``
            （即当前正在运行的解释器），保证子进程总能启动。

    Returns:
        存在的解释器路径；所有候选都不存在时返回 ``fallback``。
    """
    venv = component_dir(component) / ".venv"
    for candidate in _python_candidates(venv):
        if candidate.is_file():
            return candidate
    return Path(fallback) if fallback is not None else Path(sys.executable)


def component_script(component: str, relative: str) -> Path:
    """返回组件内脚本的绝对路径，例如 ``component_script("SearchAgent", "search.py")``。"""
    return component_dir(component) / relative


def subprocess_env(extra: dict | None = None) -> dict:
    """构造传给 ``subprocess`` 的环境变量字典。

    在 ``os.environ`` 的基础上：

    * 移除 ``__PYVENV_LAUNCHER__``（macOS venv 启动器遗留变量）；
    * 设置 ``PYTHONIOENCODING=utf-8``，让子进程标准流固定为 UTF-8；
    * 设置 ``PYTHONUTF8=1``，启用 Python 的 UTF-8 模式。

    Args:
        extra: 需要额外覆盖的变量，优先级最高。
    """
    env = dict(os.environ)
    env.pop("__PYVENV_LAUNCHER__", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra:
        env.update(extra)
    return env
