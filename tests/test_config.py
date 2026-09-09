import json
from pathlib import Path

import pytest

from scanner.config import ConfigError, load_config, load_env, mask_secret


def test_loads_real_config_with_dummy_env(project: Path):
    cfg = load_config(project / "config.json", project / ".env")
    assert cfg.birdeye_plan in ("standard", "lite", "starter", "premium", "business")
    assert set(cfg.chains) == {"solana", "robinhood"}
    assert [c.name for c in cfg.enabled_chains] == ["solana", "robinhood"]
    assert cfg.secret("BIRDEYE_API_KEY") == "dummy-key-for-tests"
    # optional defaults are filled
    assert cfg.secret("SOLANA_RPC_URL").startswith("https://")
    assert cfg.secret("DISCORD_WEBHOOK_TEST") is None
    # relative paths resolve under the project root
    assert cfg.db_path == project / "data" / "scanner.sqlite"
    assert cfg.raw_dir == project / "raw"


def test_missing_env_file_and_no_environ_fails_loudly(project: Path):
    (project / ".env").unlink()
    with pytest.raises(ConfigError, match="BIRDEYE_API_KEY"):
        load_config(project / "config.json", project / ".env")


def test_env_file_without_required_key_fails(project: Path):
    (project / ".env").write_text("DISCORD_WEBHOOK_TEST=x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="BIRDEYE_API_KEY"):
        load_env(project / ".env")


def test_environ_overrides_env_file(project: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BIRDEYE_API_KEY", "from-environ")
    env = load_env(project / ".env")
    assert env["BIRDEYE_API_KEY"] == "from-environ"


def test_environ_alone_is_enough(project: Path, monkeypatch: pytest.MonkeyPatch):
    (project / ".env").unlink()
    monkeypatch.setenv("BIRDEYE_API_KEY", "from-environ")
    env = load_env(project / ".env")
    assert env["BIRDEYE_API_KEY"] == "from-environ"


def test_env_parsing_quotes_comments_export(project: Path):
    (project / ".env").write_text(
        "# comment\n\nexport BIRDEYE_API_KEY=\"quoted\"\nDISCORD_WEBHOOK_TEST='single'\n",
        encoding="utf-8",
    )
    env = load_env(project / ".env")
    assert env["BIRDEYE_API_KEY"] == "quoted"
    assert env["DISCORD_WEBHOOK_TEST"] == "single"


def test_malformed_env_line_fails(project: Path):
    (project / ".env").write_text("BIRDEYE_API_KEY=x\nthis is not a pair\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="line 2"):
        load_env(project / ".env")


def test_unknown_plan_fails(project: Path, config_data: dict):
    config_data["birdeye"]["plan"] = "platinum"
    (project / "config.json").write_text(json.dumps(config_data), encoding="utf-8")
    with pytest.raises(ConfigError, match="plan"):
        load_config(project / "config.json", project / ".env")


def test_no_enabled_chain_fails(project: Path, config_data: dict):
    for c in config_data["chains"].values():
        c["enabled"] = False
    (project / "config.json").write_text(json.dumps(config_data), encoding="utf-8")
    with pytest.raises(ConfigError, match="no chain"):
        load_config(project / "config.json", project / ".env")


def test_missing_chain_key_fails(project: Path, config_data: dict):
    del config_data["chains"]["solana"]["stage1"]
    (project / "config.json").write_text(json.dumps(config_data), encoding="utf-8")
    with pytest.raises(ConfigError, match="stage1"):
        load_config(project / "config.json", project / ".env")


def test_invalid_json_fails(project: Path):
    (project / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="valid JSON"):
        load_config(project / "config.json", project / ".env")


def test_secrets_txt_fallback_is_accepted(project: Path):
    (project / ".env").unlink()
    (project / "secrets.txt").write_text("BIRDEYE_API_KEY=from-txt\nDISCORD_WEBHOOK_TEST=hook\n", encoding="utf-8")
    cfg = load_config(project / "config.json", project / ".env")
    assert cfg.secret("BIRDEYE_API_KEY") == "from-txt"
    assert cfg.secret("DISCORD_WEBHOOK_TEST") == "hook"


def test_dot_env_wins_over_fallbacks(project: Path):
    (project / "secrets.txt").write_text("BIRDEYE_API_KEY=from-txt\n", encoding="utf-8")
    cfg = load_config(project / "config.json", project / ".env")
    assert cfg.secret("BIRDEYE_API_KEY") == "dummy-key-for-tests"


def test_mask_secret():
    assert mask_secret(None) == "<unset>"
    assert mask_secret("abcdefgh") == "abcd...(8 chars)"
