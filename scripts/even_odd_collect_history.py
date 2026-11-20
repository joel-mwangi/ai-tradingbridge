"""Retired: historical tick backfills are disabled for Even/Odd Lab.

The research dataset now accepts only ticks captured by the continuous
Deriv live subscription worker in scripts.even_odd_live_collector.
"""
from __future__ import annotations

import json


def main() -> int:
    print(json.dumps({
        "status": "disabled",
        "reason": "Historical tick collection is retired. Run the live collector worker instead.",
        "replacement": "python -m scripts.even_odd_live_collector",
        "order_execution_enabled": False,
    }, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
