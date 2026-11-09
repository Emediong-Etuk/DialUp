"""Test bootstrap: import the plugin directory as the package ``dialup_plugin``.

The Hermes plugin loader imports a directory plugin as a package so the relative imports in
``__init__.py`` resolve; tests mirror that under a stable alias (same pattern as the official
Home Assistant plugin). Hermes core must be importable: run with a hermes-agent checkout on
``PYTHONPATH``, e.g. ``PYTHONPATH=../hermes-agent python -m pytest -q``.

``Platform("dialup")`` only resolves for a platform the registry knows, so the plugin's real
``PlatformEntry`` is registered up front exactly as the loader would.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE = "dialup_plugin"
_ROOT = Path(__file__).resolve().parents[1]

# ``python -m pytest`` from the repo root puts the root on sys.path, where one of our modules could
# shadow a core module of the same name. Keep the root off it; the package alias is enough.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _ROOT]


def _load_plugin_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def _ensure_platform_registered(plugin) -> None:
    from gateway.platform_registry import PlatformEntry, platform_registry

    if platform_registry.is_registered(plugin.PLATFORM_NAME):
        return
    platform_registry.register(PlatformEntry(source="plugin", **plugin._platform_kwargs()))


_ensure_platform_registered(_load_plugin_package())


_DIALUP_ENV = (
    "AFRICASTALKING_USERNAME", "AFRICASTALKING_API_KEY", "DIALUP_TRANSPORT", "DIALUP_SERVICE_CODE",
    "DIALUP_SMS_FROM", "DIALUP_AT_API_BASE", "DIALUP_WEBHOOK_HOST", "DIALUP_WEBHOOK_PORT",
    "DIALUP_WEBHOOK_SECRET", "DIALUP_ALLOWED_USERS", "DIALUP_ALLOW_ALL_USERS", "DIALUP_HOME_CHANNEL",
    "DIALUP_DEFAULT_COUNTRY_CODE", "DIALUP_SMS_MAX_CHARS", "DIALUP_USSD_PAGE_CHARS",
    "DIALUP_QUICK_PROMPTS", "DIALUP_SMS_HOURLY_CAP", "DIALUP_TURNS_HOURLY_CAP",
    "DIALUP_STORE_REPLIES", "DIALUP_REPLY_TTL_HOURS",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Every test starts with no DialUp env and its own HERMES_HOME (so plugin-data is private)."""
    for name in _DIALUP_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    yield


class _RepoRootIsNotATestPackage:
    """The repo root *is* the plugin package (its ``__init__.py`` uses relative imports), so pytest
    would collect it as a test ``Package`` and import that file standalone, where the relative
    imports fail. Collect the root as a plain directory instead (same fix as Home Assistant)."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("dialup-repo-root"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "dialup-repo-root")
