import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway project dir with the real config.json and a dummy .env.

    Environment secrets are cleared so tests never pick up the real key.
    """
    for key in ("BIRDEYE_API_KEY", "DISCORD_WEBHOOK_TEST", "DISCORD_WEBHOOK_LIVE",
                "SOLANA_RPC_URL", "ROBINHOOD_RPC_URL"):
        monkeypatch.delenv(key, raising=False)
    shutil.copy(ROOT / "config.json", tmp_path / "config.json")
    (tmp_path / ".env").write_text("BIRDEYE_API_KEY=dummy-key-for-tests\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def config_data(project: Path) -> dict:
    return json.loads((project / "config.json").read_text(encoding="utf-8"))
