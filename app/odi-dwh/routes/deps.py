from __future__ import annotations

from types import ModuleType


_app_module: ModuleType | None = None


def set_app_module(module: ModuleType) -> None:
    global _app_module
    _app_module = module


def get_app_module() -> ModuleType:
    if _app_module is None:
        raise RuntimeError("Route dependencies have not been registered.")
    return _app_module
