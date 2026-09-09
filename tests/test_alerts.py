import json
from pathlib import Path

import pytest

from scanner.alerts import Alerter, AlertPolicy, DiscordWebhook, build_card, build_rug_card, _settings
from scanner.config import ChainConfig
from scanner.db import open_db
from scanner.features import TapeFeatures, WindowStats
from scanner.labeler import Labeler
from scanner.ledger import CULedger
from scanner.rugwatch import RugWatch
from scanner.safety import SafetyResult, Check
from scanner.scoring import Component, Decision, persist_decision
from scanner.stage0 import TokenRow
from scanner.wash import WashReport

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
ACFG = CFG["alerts"]
NOW = 1_788_866_000
SOL = ChainConfig("solana", True, "solana", 60, {}, {})
RH = ChainConfig("robinhood", True, "robinhood", 120, {}, {})
ADDR = "TokenMint1111111111111111111111111111111111"


class FakePost:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list[dict] = []
        self.n = 0

    async def __call__(self, url, payload):
        self.calls.append(payload)
        self.n += 1
        if self.script:
            return self.script.pop(0)
        return 200, {"id": f"msg{self.n}"}, {}


class Clock:
    def __init__(self, t=NOW):
        self.t = float(t)

    def __call__(self):
        return self.t


def feats(anchor_ts=NOW - 90, price=0.0012, avwap=0.0011, anchor_low=0.0010):
    f = TapeFeatures(n=200, as_of=NOW, oldest_ts=NOW - 900, newest_ts=NOW, price=price)
    f.ofi30 = WindowStats(n=30, buyers=18, sellers=7, ofi=0.55, new_wallet_share_usd=0.6, buyer_seller_ratio=18 / 7)
    f.recent = WindowStats(n=10, ofi=0.3)
    f.anchor_ts, f.avwap, f.anchor_low = anchor_ts, avwap, anchor_low
    f.price_vs_anchor_pct, f.price_vs_avwap_pct = 20.0, 9.1
    return f


def decision(tier="CONFIRMED", score=88.0, alertable=True, since=90):
    return Decision(chain="solana", address=ADDR, eval_ts=NOW, as_of=NOW, anchor_ts=NOW - since, anchor_source="tape",
                    since_anchor_s=since, score=score, tier=tier, eligible=True, alertable=alertable, hard_vetoes=[],
                    soft_flags=[], components=[Component("participation", 30, 30), Component("orderflow", 25, 25),
                                                Component("efficiency", 13, 15), Component("structure", 15, 15),
                                                Component("holder_growth", 2, 10), Component("safety", 3, 5)])


def wash():
    return WashReport(n=60, wash_score=0.12)


def safety(verdict="SAFE"):
    return SafetyResult("solana", ADDR, NOW, verdict, [Check("top10", True, 18.0, 35.0)], 3.0, [], [],
                        {"mint": {"program": "spl-token"}, "profile": {"top10_pct": 18.0, "holders": 900,
                                                                       "cohorts": {"dev": {"pct": 0.3}, "bundler": {"pct": 4.0}}}})


def row(liq=21_000.0, mcap=84_000.0):
    return TokenRow(chain="solana", cycle_id=1, ts=NOW, address=ADDR, sort_key="x", rank=0, symbol="TICK", price=0.0012,
                    liquidity=liq, market_cap=mcap, holder=212, listing_ts=NOW - 660, pc_5m=23.0)


def make(tmp_path, post=None, url="https://discord.test/hook", settings=None, clock=None):
    conn = open_db(tmp_path / "t.sqlite")
    clock = clock or Clock()
    hook = DiscordWebhook(url, "ua", "bot", post=post or FakePost())
    rug = RugWatch(conn, None, CULedger(conn), None, CFG["rugwatch"], "starter", 100_000, clock=clock)
    lab = Labeler(conn, None, CULedger(conn), {**CFG["labeler"], "control_sample_per_cycle": 0}, "starter", 100_000, clock=clock)
    return conn, Alerter(conn, hook, settings or ACFG, rugwatch=rug, labeler=lab, clock=clock), hook, rug


# ---- policy ------------------------------------------------------------------------------

def test_policy_cooldown_upgrade_and_hourly_cap():
    p = AlertPolicy(cooldown_s=1200, max_per_hour=2)
    assert p.decide("solana", "A", "IGNITION", NOW) == (True, "ok")
    p.record("solana", "A", "IGNITION", NOW)
    assert p.decide("solana", "A", "IGNITION", NOW + 60) == (False, "cooldown")
    assert p.decide("solana", "A", "CONFIRMED", NOW + 60) == (True, "upgrade")      # upgrade inside cooldown
    p.record("solana", "A", "CONFIRMED", NOW + 60)
    assert p.decide("solana", "A", "CONFIRMED", NOW + 120) == (False, "cooldown")   # same tier again -> cooldown
    assert p.decide("solana", "B", "IGNITION", NOW + 130) == (False, "hourly_cap")  # 2 sent this hour
    assert p.decide("solana", "B", "CONFIRMED", NOW + 130) == (True, "ok")          # CONFIRMED overflows the cap
    assert p.decide("robinhood", "C", "IGNITION", NOW + 130) == (True, "ok")        # caps are per chain
    assert p.decide("solana", "A", "IGNITION", NOW + 1300) == (False, "hourly_cap")  # cooldown over, but 2 sent this hour
    assert p.decide("solana", "A", "CONFIRMED", NOW + 1300) == (True, "ok")         # ...CONFIRMED still passes
    assert p.decide("solana", "B", "IGNITION", NOW + 3700) == (True, "ok")          # hour rolled


def test_policy_loads_state_from_alerts_table(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO alerts(chain,address,ts,tier,status) VALUES('solana','A',?, 'IGNITION','sent')", (NOW - 100,))
    p = AlertPolicy(1200, 6)
    p.load(conn, NOW)
    assert p.decide("solana", "A", "IGNITION", NOW) == (False, "cooldown")


# ---- webhook ------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_webhook_retries_on_429_and_reports_failure():
    post = FakePost([(429, {"retry_after": 0.01}, {"retry-after": "0.01"}), (200, {"id": "m1"}, {})])
    hook = DiscordWebhook("https://x", "ua", "bot", post=post)
    assert await hook.send("hi") == "m1" and post.n == 2 and hook.sent == 1
    bad = DiscordWebhook("https://x", "ua", "bot", post=FakePost([(403, "forbidden", {})]))
    assert await bad.send("hi") is None and bad.failed == 1
    assert await DiscordWebhook(None, "ua", "bot").send("hi") == "dry-run"


# ---- card -------------------------------------------------------------------------------------

def test_card_contains_every_required_field_and_invalidation_rule():
    card = build_card(chain=SOL, address=ADDR, symbol="TICK", decision=decision(), feats=feats(), wash=wash(),
                      safety=safety(), row=row(), s1={"rvol_5m": 6.1, "cohort_z": 2.3, "holder_growth_pct": 9.0},
                      settings=_settings(ACFG), now=NOW)
    d = card["description"]
    assert card["title"] == "[CONFIRMED] SOLANA · $TICK"
    for token in ("mcap **$84,000**", "liq **$21,000**", "age 11m", "holders 212 (+9.0%/5m)", "rVol 6.1x", "z 2.30",
                  "OFI30 +0.55", "buyers/sellers 18/7", "new-wallet 60%", "wash 0.12", "safety **SAFE**", "top10 18.0%",
                  "+23.0% 5m", "+20.0% from anchor", "+9.1% vs aVWAP", "TIME STOP 15m", "size note ≤ $210", "score **88**",
                  "[birdeye]", "[dexscreener]", f"`{ADDR}`"):
        assert token in d, token
    # invalidation = nearer of anchor low / aVWAP below price: aVWAP 0.0011 -> -8.3%
    assert "INVALIDATION 0.0011 (-8.3%)" in d
    # far invalidation is capped at -25%
    card2 = build_card(chain=SOL, address=ADDR, symbol="TICK", decision=decision(), feats=feats(avwap=0.0005, anchor_low=0.0004),
                       wash=wash(), safety=safety(), row=row(), s1=None, settings=_settings(ACFG), now=NOW)
    assert "(-25.0%)" in card2["description"]
    # Robinhood card: blockscout link, sim line, hourly price change label
    sr = SafetyResult("robinhood", "0xabc", NOW, "SAFE", [], 3.0, ["top10_unknown"], [],
                      {"sim": {"paths": [{"name": "sell", "ok": True, "tax_pct": 0.0}, {"name": "buy", "ok": True, "tax_pct": 2.5}]},
                       "owner_state": "none"})
    rrow = TokenRow(chain="robinhood", cycle_id=1, ts=NOW, address="0xabc", sort_key="x", rank=0, symbol="RH", price=1.0,
                    liquidity=5000.0, market_cap=50_000.0, pc_1h=12.0)
    card3 = build_card(chain=RH, address="0xabc", symbol="RH", decision=decision(), feats=feats(price=1.0, avwap=0.95, anchor_low=0.9),
                       wash=wash(), safety=sr, row=rrow, s1={"rvol_dt": 4.5}, settings=_settings(ACFG), now=NOW)
    assert "[blockscout]" in card3["description"] and "sim sell:OK buy:OK(2.5%)" in card3["description"]
    assert "+12.0% 1h" in card3["description"] and "flags: top10_unknown" in card3["description"]


def test_rug_card():
    import sqlite3
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t(alert_ts INTEGER, minute INTEGER, reason TEXT, detail TEXT, address TEXT)")
    conn.execute("INSERT INTO t VALUES(?, 30, 'LIQUIDITY_DROP', 'liquidity 50,000 -> 20,000 (-60%)', 'ADDR')", (NOW - 1800,))
    r = conn.execute("SELECT * FROM t").fetchone()
    c = build_rug_card(r, "TICK", "solana", NOW)
    assert c["title"] == "RUG WARNING · SOLANA · $TICK" and "30m ago" in c["description"] and "-60%" in c["description"]


# ---- alerter end to end ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consider_sends_persists_schedules_and_labels(tmp_path):
    conn, al, hook, rug = make(tmp_path)
    d = decision()
    did = persist_decision(conn, d)
    out = await al.consider(chain=SOL, address=ADDR, symbol="TICK", decision=d, decision_id=did, feats=feats(), wash=wash(),
                            safety=safety(), row=row(), s1={"rvol_5m": 6.1})
    assert out == "sent" and hook.sent == 1 and al.stats["sent"] == 1
    a = conn.execute("SELECT * FROM alerts").fetchone()
    assert a["tier"] == "CONFIRMED" and a["message_id"] == "msg1" and a["liquidity"] == 21_000.0 and a["channel"] == "test"
    assert json.loads(a["card_json"])["title"].startswith("[CONFIRMED]")
    assert conn.execute("SELECT alerted_ts FROM decisions WHERE id=?", (did,)).fetchone()["alerted_ts"] == NOW
    assert conn.execute("SELECT COUNT(*) FROM rug_checks WHERE alert_id=?", (a["id"],)).fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM labels WHERE ref_kind='alert' AND ref_id=?", (a["id"],)).fetchone()[0] == 4
    # posted payload has content + embed
    payload = hook._post.calls[0]
    assert "**CONFIRMED**" in payload["content"] and payload["embeds"][0]["title"].startswith("[CONFIRMED]")
    # same token again -> cooldown, nothing posted
    d2 = decision(); did2 = persist_decision(conn, d2)
    assert await al.consider(chain=SOL, address=ADDR, symbol="TICK", decision=d2, decision_id=did2, feats=feats(),
                             wash=wash(), safety=safety(), row=row(), s1=None) == "cooldown"
    assert hook.sent == 1


@pytest.mark.asyncio
async def test_upgrade_reping_and_hourly_cap(tmp_path):
    clk = Clock()
    conn, al, hook, _ = make(tmp_path, settings={**ACFG, "max_per_hour_per_chain": 1}, clock=clk)
    d1 = decision(tier="IGNITION", score=60); did = persist_decision(conn, d1)
    assert await al.consider(chain=SOL, address=ADDR, symbol="T", decision=d1, decision_id=did, feats=feats(), wash=wash(),
                             safety=safety(), row=row(), s1=None) == "sent"
    clk.t += 120
    d2 = decision(tier="CONFIRMED", score=80); did2 = persist_decision(conn, d2)
    assert await al.consider(chain=SOL, address=ADDR, symbol="T", decision=d2, decision_id=did2, feats=feats(), wash=wash(),
                             safety=safety(), row=row(), s1=None) == "sent"
    assert "UPGRADE" in hook._post.calls[1]["content"] and al.stats["upgrades"] == 1
    # a different token, IGNITION, cap of 1 already used this hour -> hourly_cap; CONFIRMED still passes
    d3 = decision(tier="IGNITION"); d3.address = "OTHER"; did3 = persist_decision(conn, d3)
    assert await al.consider(chain=SOL, address="OTHER", symbol="O", decision=d3, decision_id=did3, feats=feats(), wash=wash(),
                             safety=safety(), row=row(), s1=None) == "hourly_cap"
    d4 = decision(tier="CONFIRMED"); d4.address = "OTHER2"; did4 = persist_decision(conn, d4)
    assert await al.consider(chain=SOL, address="OTHER2", symbol="O2", decision=d4, decision_id=did4, feats=feats(), wash=wash(),
                             safety=safety(), row=row(), s1=None) == "sent"


@pytest.mark.asyncio
async def test_dry_run_without_webhook_still_persists(tmp_path):
    conn, al, hook, _ = make(tmp_path, url=None)
    d = decision(); did = persist_decision(conn, d)
    assert await al.consider(chain=SOL, address=ADDR, symbol="T", decision=d, decision_id=did, feats=feats(), wash=wash(),
                             safety=safety(), row=row(), s1=None) == "sent"
    a = conn.execute("SELECT channel, message_id FROM alerts").fetchone()
    assert a["channel"] == "dry-run" and a["message_id"] == "dry-run" and hook.sent == 0


@pytest.mark.asyncio
async def test_failed_post_is_not_recorded(tmp_path):
    conn, al, hook, _ = make(tmp_path, post=FakePost([(500, "err", {}), (500, "err", {}), (500, "err", {})]))
    d = decision(); did = persist_decision(conn, d)
    assert await al.consider(chain=SOL, address=ADDR, symbol="T", decision=d, decision_id=did, feats=feats(), wash=wash(),
                             safety=safety(), row=row(), s1=None) == "failed"
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_rug_warning_delivery_and_heartbeat(tmp_path):
    clk = Clock()
    conn, al, hook, rug = make(tmp_path, clock=clk)
    conn.execute("INSERT INTO candidates(chain,address,first_seen_ts,last_seen_ts,status,state_json) VALUES('solana',?,?,?,'active',?)",
                 (ADDR, NOW, NOW, json.dumps({"symbol": "TICK"})))
    rug.schedule(1, "solana", ADDR, NOW - 1800, 50_000.0, "SAFE")
    conn.execute("UPDATE rug_checks SET status='done', warned=1, reason='LIQUIDITY_DROP', detail='liquidity 50,000 -> 10,000 (-80%)', done_ts=? WHERE minute=10", (NOW,))
    assert await al.deliver_rug_warnings({"solana": SOL}) == 1
    assert "RUG WARNING" in hook._post.calls[-1]["content"] and "$TICK" in hook._post.calls[-1]["content"]
    assert await al.deliver_rug_warnings({"solana": SOL}) == 0          # delivered once
    assert await al.heartbeat_if_due(lambda: "x=1") is True and "alive" in hook._post.calls[-1]["content"]
    assert await al.heartbeat_if_due(lambda: "x=2") is False            # not due again
    clk.t += ACFG["heartbeat_interval_s"] + 1
    assert await al.heartbeat_if_due(lambda: "x=3") is True
