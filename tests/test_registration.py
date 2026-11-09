"""register(ctx) wiring: the platform and the CLI go through the public plugin API with the
kwargs core expects, and newer PlatformEntry fields are only passed when core has them."""

from __future__ import annotations

import dataclasses

import pytest

import dialup_plugin
from dialup_plugin.hint import PLATFORM_HINT

OPTIONAL_KWARGS = set(dialup_plugin._OPTIONAL_ENTRY_KWARGS)


class RecordingCtx:
    """Minimal stand-in for ``PluginContext`` that records the public registration calls."""

    def __init__(self):
        self.platforms = []
        self.cli_commands = []

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)

    def register_cli_command(self, **kwargs):
        self.cli_commands.append(kwargs)


@pytest.fixture
def ctx():
    c = RecordingCtx()
    dialup_plugin.register(c)
    return c


def test_registers_one_platform_with_core_kwargs(ctx):
    assert len(ctx.platforms) == 1
    kw = ctx.platforms[0]
    assert kw["name"] == "dialup"
    assert kw["label"] == "DialUp (USSD + SMS)"
    assert kw["adapter_factory"] is dialup_plugin.make_adapter
    assert kw["check_fn"] is dialup_plugin.check_requirements
    assert kw["validate_config"] is dialup_plugin.validate_config
    assert kw["standalone_sender_fn"] is dialup_plugin.standalone_send
    assert kw["cron_deliver_env_var"] == "DIALUP_HOME_CHANNEL"
    assert kw["allowed_users_env"] == "DIALUP_ALLOWED_USERS"
    assert kw["allow_all_env"] == "DIALUP_ALLOW_ALL_USERS"
    assert kw["max_message_length"] == 0
    assert kw["pii_safe"] is True
    assert kw["allow_update_command"] is False
    assert kw["platform_hint"] == PLATFORM_HINT
    assert kw["emoji"] == "📟"


def test_kwargs_build_a_real_platform_entry(ctx):
    from gateway.platform_registry import PlatformEntry

    entry = PlatformEntry(source="plugin", **ctx.platforms[0])
    assert entry.name == "dialup"


def test_current_core_gets_minimal_display_and_no_pairing(ctx):
    kw = ctx.platforms[0]
    assert kw["display_tier"] == "minimal"
    assert kw["allowlist_suppresses_pairing"] is True


def test_optional_kwargs_dropped_when_core_lacks_them(monkeypatch):
    import gateway.platform_registry as reg

    known = {f.name for f in dataclasses.fields(reg.PlatformEntry)} - OPTIONAL_KWARGS
    older = dataclasses.make_dataclass(
        "OlderEntry", [(f.name, f.type, f) for f in dataclasses.fields(reg.PlatformEntry) if f.name in known])
    monkeypatch.setattr(reg, "PlatformEntry", older)
    c = RecordingCtx()
    dialup_plugin.register(c)
    assert not OPTIONAL_KWARGS & set(c.platforms[0])
    older(source="plugin", **c.platforms[0])  # constructs cleanly on the older core


def test_registers_the_cli(ctx):
    assert [c["name"] for c in ctx.cli_commands] == ["dialup"]
    assert callable(ctx.cli_commands[0]["setup_fn"])
    assert callable(ctx.cli_commands[0]["handler_fn"])


def test_check_fn_needs_local_transport_or_credentials(monkeypatch):
    assert dialup_plugin.check_requirements() is False
    monkeypatch.setenv("DIALUP_TRANSPORT", "local")
    assert dialup_plugin.check_requirements() is True
    monkeypatch.setenv("DIALUP_TRANSPORT", "africastalking")
    monkeypatch.setenv("AFRICASTALKING_USERNAME", "sandbox")
    monkeypatch.setenv("AFRICASTALKING_API_KEY", "test-key")
    assert dialup_plugin.check_requirements() is True


def test_env_enablement_seeds_home_channel(monkeypatch):
    assert dialup_plugin._env_enablement() is None
    monkeypatch.setenv("DIALUP_TRANSPORT", "local")
    monkeypatch.setenv("DIALUP_HOME_CHANNEL", "+2348031234567")
    seeded = dialup_plugin._env_enablement()
    assert seeded["transport"] == "local"
    assert seeded["home_channel"]["chat_id"] == "+2348031234567"


def test_validate_config_requires_secret_for_africastalking(monkeypatch):
    from gateway.config import PlatformConfig

    monkeypatch.setenv("AFRICASTALKING_USERNAME", "sandbox")
    monkeypatch.setenv("AFRICASTALKING_API_KEY", "test-key")
    assert dialup_plugin.validate_config(PlatformConfig(enabled=True)) is False
    monkeypatch.setenv("DIALUP_WEBHOOK_SECRET", "s" * 43)
    assert dialup_plugin.validate_config(PlatformConfig(enabled=True)) is True


def test_target_ref_parsing(monkeypatch):
    assert dialup_plugin.parse_target_ref("0803 123 4567") == ("+2348031234567", None)
    assert dialup_plugin.parse_target_ref("not a number") is None
    assert dialup_plugin.validate_target_ref("+2348031234567") is True
    assert isinstance(dialup_plugin.validate_target_ref("hello"), str)
