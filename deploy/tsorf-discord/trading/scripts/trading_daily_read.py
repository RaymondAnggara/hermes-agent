#!/usr/bin/env python3
"""Daily forecast-log cron shim (Phase 5b step 4c data-generation).

Registered as a ``no_agent`` cron job (deterministic, no model turn). Once a day
it records a read — signals + a directional prediction when the read leans — for
each allowed symbol, so the edge measurement (`strategy_report`) accrues an
unbiased forecast history. It places NO orders and moves no money.

Deploy: copy to ``$HERMES_HOME/scripts/trading_daily_read.py`` (cron scripts must
live there) and register a daily ``no_agent`` job pointing at it. Runs inside the
locked container under the venv python; the real logic is in
``plugins/trading/daily.py`` (unit-tested) — this shim just wires config → store.
"""

from __future__ import annotations

import os
import sys

# hermes-agent package root (container path); override with HERMES_AGENT_ROOT.
sys.path.insert(0, os.environ.get("HERMES_AGENT_ROOT", "/opt/hermes"))
# Cron may sanitize the subprocess env; ensure the egress proxy is set so the
# CoinGecko fetch has a route (no-op if the parent already exported it).
os.environ.setdefault("HTTPS_PROXY", "http://egress-proxy:8888")
os.environ.setdefault("HTTP_PROXY", "http://egress-proxy:8888")


def main() -> int:
    from plugins.trading import TradingStore, record_daily_reads
    from tools.trading_tool import _load_caps  # config-aware, clamped caps

    caps = _load_caps()
    if caps is None:
        print("trading caps not configured — nothing recorded")
        return 0
    store = TradingStore.open()
    try:
        result = record_daily_reads(store, caps, list(caps.allowed_symbols))
    finally:
        store.close()
    print("daily forecast reads:", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
