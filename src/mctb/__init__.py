"""MCTB — a distributed memecoin sniping and copy-trading system.

Three cooperating processes, normally on three machines:

    scout    (RPi 1)  ingests pump.fun + social data, screens tokens on-chain,
                      tracks profitable wallets, emits candidates
    brain    (RPi 2)  scores candidates deterministically in milliseconds,
                      consults a local LLM off the hot path, sizes and
                      executes trades, manages exits
    console  (Host PC) dashboard, supervisory stop-loss, alerts, kill switch

They communicate over Redis, so any of them can restart without taking the
others down.
"""

__version__ = "1.0.0"
