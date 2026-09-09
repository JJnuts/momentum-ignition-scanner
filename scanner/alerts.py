"""Discord alerts (T13) - the signaling-phase output.

  card         one embed per alert (SPEC s7): market context, ignition
               features, wash / safety line, price vs anchor / aVWAP,
               INVALIDATION + TIME STOP + SIZE NOTE, links, contract address.
  policy       per-token cooldown (20 min), re-ping ONLY on a tier upgrade
               inside the cooldown, hourly cap per chain (6) with a
               CONFIRMED-only overflow. State is rebuilt from `alerts` on start.
  side effects on send: `alerts` row, decisions.alerted_ts, rug-watch
               schedule, outcome labels (ref_kind 'alert').
  rug warnings queued by the rug watch are posted and marked delivered.
  heartbeat    once per heartbeat_interval_s ("alive" + counters).
  webhook      browser-like User-Agent (Discord 403s the default aiohttp UA),
               ?wait=true to get the message id, Retry-After on 429.
No webhook configured -> DRY RUN: everything is decided and persisted, nothing
is posted, and the log says so once.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp

from .config import ChainConfig

log = logging.getLogger("alerts")

DEFAULTS: dict[str, Any] = {
    "cooldown_min": 20,
    "max_per_hour_per_chain": 6,
    "heartbeat_interval_s": 86400,
    "invalidation_cap_pct": 25.0,
    "time_stop_min": 15,
    "size_note_liq_frac": 0.01,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) momentum-ignition-scanner/1.0",
    "username": "Momentum Ignition Scanner",
}

TIER_RANK = {"WATCH": 0, "IGNITION": 1, "CONFIRMED": 2}
TIER_COLOR = {"IGNITION": 0xF5A623, "CONFIRMED": 0x2ECC71, "RUG": 0xE74C3C, "INFO": 0x95A5A6}

# (status, body_json_or_text, headers)
PostFn = Callable[[str, dict[str, Any]], Awaitable[tuple[int, Any, dict[str, str]]]]


def _settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    s = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if not k.startswith("_"):
            s[k] = v
    return s


def _f(v: Any, fmt: str = "{:,.0f}", none: str = "-") -> str:
    try:
        return none if v is None else fmt.format(float(v))
    except (TypeError, ValueError):
        return none


def _pct(v: Any, none: str = "-") -> str:
    return none if v is None else f"{float(v):+.1f}%"


def _age(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"
    return f"{seconds // 86400}d"


def _price(p: Any) -> str:
    if p is None:
        return "-"
    p = float(p)
    if p == 0:
        return "0"
    if p >= 1:
        return f"{p:,.4f}"
    return f"{p:.10f}".rstrip("0")


class DiscordWebhook:
    def __init__(self, url: str | None, user_agent: str, username: str, post: PostFn | None = None,
                 session: aiohttp.ClientSession | None = None) -> None:
        self.url = url
        self.ua = user_agent
        self.username = username
        self._post = post or self._aiohttp_post
        self._session = session
        self.sent = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def _aiohttp_post(self, url: str, payload: dict[str, Any]) -> tuple[int, Any, dict[str, str]]:
        if self._session is None:
            self._session = aiohttp.ClientSession(headers={"user-agent": self.ua})
        async with self._session.post(url, json=payload, params={"wait": "true"},
                                      timeout=aiohttp.ClientTimeout(total=20)) as r:
            text = await r.text()
            try:
                body: Any = json.loads(text) if text else {}
            except json.JSONDecodeError:
                body = text
            return r.status, body, {k.lower(): v for k, v in r.headers.items()}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def send(self, content: str | None = None, embeds: list[dict[str, Any]] | None = None) -> str | None:
        """Returns the Discord message id (or 'dry-run' when disabled), None on failure."""
        if not self.enabled:
            return "dry-run"
        payload: dict[str, Any] = {"username": self.username}
        if content:
            payload["content"] = content[:1900]
        if embeds:
            payload["embeds"] = embeds[:10]
        for attempt in range(3):
            try:
                status, body, headers = await self._post(self.url or "", payload)
            except (aiohttp.ClientError, TimeoutError, OSError) as e:
                log.warning("discord post failed: %s", e)
                status, body, headers = 0, None, {}
            if status in (200, 204):
                self.sent += 1
                return str(body.get("id")) if isinstance(body, dict) and body.get("id") else "ok"
            if status == 429:
                delay = 2.0
                try:
                    delay = float(headers.get("retry-after") or (body or {}).get("retry_after") or 2.0)
                except (TypeError, ValueError, AttributeError):
                    pass
                import asyncio as _asyncio
                await _asyncio.sleep(min(delay, 10.0))
                continue
            log.warning("discord post HTTP %s: %s", status, str(body)[:200])
            break
        self.failed += 1
        return None


@dataclass
class AlertPolicy:
    cooldown_s: int
    max_per_hour: int
    last: dict[tuple[str, str], tuple[int, str]] = field(default_factory=dict)   # (chain,addr) -> (ts, tier)
    sent_ts: dict[str, list[int]] = field(default_factory=dict)                  # chain -> ts list (rolling hour)

    def load(self, conn: sqlite3.Connection, now: int) -> None:
        for r in conn.execute("SELECT chain, address, ts, tier FROM alerts WHERE status='sent' AND ts>=? ORDER BY ts",
                              (now - max(self.cooldown_s, 3600),)):
            self.last[(r["chain"], r["address"])] = (int(r["ts"]), r["tier"])
            self.sent_ts.setdefault(r["chain"], []).append(int(r["ts"]))

    def decide(self, chain: str, address: str, tier: str, now: int) -> tuple[bool, str]:
        """-> (send?, reason). Cooldown applies unless the tier UPGRADED; hourly cap lets only CONFIRMED through."""
        prev = self.last.get((chain, address))
        if prev is not None and now - prev[0] < self.cooldown_s:
            if TIER_RANK.get(tier, 0) > TIER_RANK.get(prev[1], 0):
                pass  # upgrade inside cooldown -> allowed
            else:
                return False, "cooldown"
        window = [t for t in self.sent_ts.get(chain, []) if now - t < 3600]
        self.sent_ts[chain] = window
        if len(window) >= self.max_per_hour and tier != "CONFIRMED":
            return False, "hourly_cap"
        return True, "upgrade" if prev is not None and now - prev[0] < self.cooldown_s else "ok"

    def record(self, chain: str, address: str, tier: str, now: int) -> None:
        self.last[(chain, address)] = (now, tier)
        self.sent_ts.setdefault(chain, []).append(now)


def build_card(*, chain: ChainConfig, address: str, symbol: str | None, decision: Any, feats: Any, wash: Any,
               safety: Any, row: Any, s1: dict[str, Any] | None, settings: dict[str, Any], now: int) -> dict[str, Any]:
    """Discord embed for an alert. `row` = latest TokenRow-like (market_cap, liquidity, holder, pc_5m, price, age_s)."""
    tier = decision.tier
    sym = symbol or address[:6]
    mcap = getattr(row, "market_cap", None)
    liq = getattr(row, "liquidity", None)
    holders = getattr(row, "holder", None)
    pc5 = getattr(row, "pc_5m", None) if getattr(row, "pc_5m", None) is not None else getattr(row, "pc_1h", None)
    pc_label = "5m" if getattr(row, "pc_5m", None) is not None else "1h"
    price = feats.price if feats.price is not None else getattr(row, "price", None)
    age = getattr(row, "age_s", None)
    hg = (s1 or {}).get("holder_growth_pct")
    rvol5 = (s1 or {}).get("rvol_5m") if (s1 or {}).get("rvol_5m") is not None else (s1 or {}).get("rvol_dt")
    z = (s1 or {}).get("cohort_z")
    w30 = feats.ofi30
    # invalidation: nearer of anchor low / aVWAP below price, capped at -cap%
    cap = float(settings["invalidation_cap_pct"])
    inv_candidates = [x for x in (feats.anchor_low, feats.avwap) if x is not None and price and x < price]
    inv = max(inv_candidates) if inv_candidates else None
    inv_pct = (inv / price - 1) * 100 if inv and price else None
    if price and (inv_pct is None or inv_pct < -cap):
        inv, inv_pct = price * (1 - cap / 100), -cap
    clip = liq * float(settings["size_note_liq_frac"]) if liq else None
    # safety line
    sr = safety
    if sr is not None:
        safe_line = f"safety **{sr.verdict}**"
        if sr.reasons:
            safe_line += " (" + ", ".join(sr.reasons) + ")"
        if sr.flags:
            safe_line += " flags: " + ", ".join(sr.flags)
        src = sr.sources or {}
        prof = (src.get("profile") or {}) if isinstance(src, dict) else {}
        if prof:
            co = prof.get("cohorts") or {}
            safe_line += (f"\ntop10 {_f(prof.get('top10_pct'), '{:.1f}')}% · dev {_f((co.get('dev') or {}).get('pct'), '{:.2f}')}% "
                          f"· bundler {_f((co.get('bundler') or {}).get('pct'), '{:.1f}')}% · sniper {_f((co.get('sniper') or {}).get('pct'), '{:.1f}')}% "
                          f"· smart {_f((co.get('smart_trader') or {}).get('pct'), '{:.1f}')}%")
        sim = (src.get("sim") or {}) if isinstance(src, dict) else {}
        if sim.get("paths"):
            safe_line += "\nsim " + " ".join(f"{p['name']}:{'OK' if p['ok'] else 'FAIL'}"
                                            + (f"({p['tax_pct']:.1f}%)" if p.get('tax_pct') else "") for p in sim["paths"])
            safe_line += f" · owner {src.get('owner_state')}"
    else:
        safe_line = "safety -"
    comps = " ".join(f"{c.name[:4]} {c.points:.0f}/{c.max_points:.0f}" for c in decision.components)
    lines = [
        f"mcap **${_f(mcap)}** · liq **${_f(liq)}** · age {_age(age)} · holders {_f(holders)}"
        + (f" ({_pct(hg)}/5m)" if hg is not None else ""),
        f"rVol {_f(rvol5, '{:.1f}')}x · z {_f(z, '{:.2f}')} · OFI30 {_f(w30.ofi, '{:+.2f}')} · recent {_f(feats.recent.ofi, '{:+.2f}')} "
        f"· buyers/sellers {w30.buyers}/{w30.sellers} · new-wallet {_f((w30.new_wallet_share_usd or 0) * 100, '{:.0f}')}%",
        f"wash {_f(wash.wash_score, '{:.2f}')}" + (f" · flags {', '.join(wash.soft_flags)}" if wash.soft_flags else "")
        + f" · {safe_line}",
        f"price {_price(price)} · {_pct(pc5)} {pc_label} · {_pct(feats.price_vs_anchor_pct)} from anchor · {_pct(feats.price_vs_avwap_pct)} vs aVWAP "
        f"· anchor {_age(decision.since_anchor_s)} ago ({decision.anchor_source})",
        f"**INVALIDATION {_price(inv)} ({_pct(inv_pct)})** · **TIME STOP {settings['time_stop_min']}m no new high**"
        + (f" · size note ≤ ${_f(clip)} per clip (~{float(settings['size_note_liq_frac']) * 100:.0f}% of liq)" if clip else ""),
        f"score **{decision.score:.0f}** [{comps}]",
    ]
    links = [f"[birdeye](https://birdeye.so/token/{address}?chain={chain.birdeye_chain})"]
    if chain.birdeye_chain == "solana":
        links.append(f"[dexscreener](https://dexscreener.com/solana/{address})")
    elif chain.name == "robinhood":
        links.append(f"[blockscout](https://robinhoodchain.blockscout.com/token/{address})")
    lines.append(" · ".join(links))
    lines.append(f"`{address}`")
    return {"title": f"[{tier}] {chain.name.upper()} · ${sym}", "description": "\n".join(lines),
            "color": TIER_COLOR.get(tier, TIER_COLOR["INFO"]), "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "footer": {"text": f"{tier} · score {decision.score:.0f} · {decision.anchor_source} anchor · test channel"}}


def build_rug_card(check: sqlite3.Row, symbol: str | None, chain_name: str, now: int) -> dict[str, Any]:
    ago = now - int(check["alert_ts"])
    return {"title": f"RUG WARNING · {chain_name.upper()} · ${symbol or str(check['address'])[:6]}",
            "description": f"Alerted **{_age(ago)} ago** (re-check at +{check['minute']}m): **{check['reason']}**\n"
                           f"{check['detail'] or ''}\n`{check['address']}`",
            "color": TIER_COLOR["RUG"], "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}


class Alerter:
    def __init__(self, conn: sqlite3.Connection, webhook: DiscordWebhook, settings: dict[str, Any],
                 rugwatch: Any = None, labeler: Any = None, clock: Callable[[], float] = time.time) -> None:
        self.conn = conn
        self.webhook = webhook
        self.s = _settings(settings)
        self.rugwatch = rugwatch
        self.labeler = labeler
        self._clock = clock
        self.policy = AlertPolicy(int(self.s["cooldown_min"]) * 60, int(self.s["max_per_hour_per_chain"]))
        self.policy.load(conn, int(clock()))
        self.stats: dict[str, int] = {"sent": 0, "cooldown": 0, "hourly_cap": 0, "upgrades": 0, "rug_sent": 0, "failed": 0}
        if not webhook.enabled:
            log.warning("no Discord webhook configured -> DRY RUN (alerts decided and persisted, nothing posted)")

    async def consider(self, *, chain: ChainConfig, address: str, symbol: str | None, decision: Any, decision_id: int,
                       feats: Any, wash: Any, safety: Any, row: Any, s1: dict[str, Any] | None) -> str:
        """Called for every ALERTABLE decision. Returns sent | cooldown | hourly_cap | failed."""
        now = int(self._clock())
        ok, why = self.policy.decide(chain.name, address, decision.tier, now)
        if not ok:
            self.stats[why] += 1
            return why
        card = build_card(chain=chain, address=address, symbol=symbol, decision=decision, feats=feats, wash=wash,
                          safety=safety, row=row, s1=s1, settings=self.s, now=now)
        content = f"{'⬆️ UPGRADE ' if why == 'upgrade' else ''}**{decision.tier}** {chain.name} ${symbol or address[:6]} score {decision.score:.0f}"
        msg_id = await self.webhook.send(content=content, embeds=[card])
        if msg_id is None:
            self.stats["failed"] += 1
            return "failed"
        liq = getattr(row, "liquidity", None)
        price = feats.price if feats.price is not None else getattr(row, "price", None)
        self.conn.execute("BEGIN")
        try:
            cur = self.conn.execute(
                "INSERT INTO alerts(nomination_id, chain, address, ts, tier, score, price, liquidity, card_json, channel, message_id, status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'sent')",
                (None, chain.name, address, now, decision.tier, decision.score, price, liq,
                 json.dumps(card, separators=(",", ":"), default=str), "test" if self.webhook.enabled else "dry-run", msg_id))
            alert_id = int(cur.lastrowid)
            self.conn.execute("UPDATE decisions SET alerted_ts=? WHERE id=?", (now, decision_id))
            if self.rugwatch is not None:
                self.rugwatch.schedule(alert_id, chain.name, address, now, liq, safety.verdict if safety else None)
            if self.labeler is not None:
                self.labeler.enqueue("alert", alert_id, chain.name, address, now, price, liq)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self.policy.record(chain.name, address, decision.tier, now)
        self.stats["sent"] += 1
        if why == "upgrade":
            self.stats["upgrades"] += 1
        log.info("ALERT sent: %s %s %s score=%.0f (%s) msg=%s", decision.tier, chain.name, symbol or address[:8],
                 decision.score, why, msg_id)
        return "sent"

    async def deliver_rug_warnings(self, chains: dict[str, ChainConfig]) -> int:
        if self.rugwatch is None:
            return 0
        n = 0
        for r in self.rugwatch.pending_warnings():
            now = int(self._clock())
            sym = self.conn.execute("SELECT state_json FROM candidates WHERE chain=? AND address=?",
                                    (r["chain"], r["address"])).fetchone()
            symbol = None
            if sym and sym["state_json"]:
                try:
                    symbol = json.loads(sym["state_json"]).get("symbol")
                except json.JSONDecodeError:
                    symbol = None
            card = build_rug_card(r, symbol, r["chain"], now)
            msg_id = await self.webhook.send(content=f"🚨 **RUG WARNING** {r['chain']} ${symbol or str(r['address'])[:6]}: {r['reason']}",
                                             embeds=[card])
            if msg_id is None:
                self.stats["failed"] += 1
                continue
            self.rugwatch.mark_delivered(int(r["id"]), now)
            self.stats["rug_sent"] += 1
            n += 1
        return n

    async def heartbeat_if_due(self, summary: Callable[[], str]) -> bool:
        now = int(self._clock())
        r = self.conn.execute("SELECT value FROM meta WHERE key='last_heartbeat_ts'").fetchone()
        last = int(r["value"]) if r else 0
        if now - last < int(self.s["heartbeat_interval_s"]):
            return False
        text = summary()
        msg_id = await self.webhook.send(content=f"💓 alive · {text}")
        if msg_id is None:
            return False
        self.conn.execute("INSERT INTO meta(key, value) VALUES('last_heartbeat_ts', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (str(now),))
        return True
