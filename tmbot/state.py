"""Local, per-machine state (the `state/` folder is git-ignored).

- InstanceLock: one bot per machine, and a list of the order ids THIS machine placed. The
  Trading API has no client-order-id field, so "ours" = an id in that list. Any other active
  order on the account means something else is trading it (e.g. the Mac while the PC runs),
  which could lead to self-trading, so the bot refuses to start.
- RiskState: daily / round loss limits that survive restarts, the taker-assist cost
  budget, and accumulated running time for pace stats.
"""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .sessions import ET

D = Decimal


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str))
    os.replace(tmp, path)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return code.value == 259  # STILL_ACTIVE
        except Exception:
            return True  # can't tell: be safe and assume it's running
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class InstanceLockError(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, state_dir: str | Path = "state"):
        self.dir = Path(state_dir)
        self.lock_path = self.dir / "instance.lock"
        self.orders_path = self.dir / "owned_orders.json"
        self.machine = socket.gethostname()
        self.held = False

    # -- one process per machine --
    def acquire(self) -> None:
        cur = _read(self.lock_path, None)
        if cur and cur.get("machine") == self.machine and _pid_alive(int(cur.get("pid", 0))) \
                and int(cur.get("pid", 0)) != os.getpid():
            raise InstanceLockError(
                f"another tmbot (pid {cur['pid']}, started {cur.get('started')}) is already running on "
                f"this machine. Stop it first; two bots on one account can trade against each other.")
        _write(self.lock_path, {"machine": self.machine, "pid": os.getpid(),
                                "started": datetime.now(ET).isoformat(timespec="seconds")})
        self.held = True

    def release(self) -> None:
        if self.held:
            cur = _read(self.lock_path, None)
            if cur and cur.get("pid") == os.getpid():
                self.lock_path.unlink(missing_ok=True)
            self.held = False

    # -- orders this machine owns --
    def owned(self) -> set[str]:
        return set(_read(self.orders_path, []))

    def add(self, order_id: str) -> None:
        ids = self.owned()
        ids.add(order_id)
        _write(self.orders_path, sorted(ids))

    def discard(self, order_ids) -> None:
        ids = self.owned() - set(order_ids)
        _write(self.orders_path, sorted(ids))

    def foreign(self, active_orders: list[dict]) -> list[dict]:
        """Active orders on the account this machine did not place. Platform-managed
        (strategy) orders are ignored: they can't be cancelled and aren't the bot."""
        mine = self.owned()
        return [o for o in active_orders
                if o.get("order_id") not in mine and not o.get("strategy_managed")
                and o.get("placed_by", "user") != "service"]


def _et_midnight_after(t: datetime) -> datetime:
    d = t.astimezone(ET).date() + timedelta(days=1)
    return datetime(d.year, d.month, d.day, tzinfo=ET)


class RiskState:
    """Loss limits that persist across restarts.

    day PnL   = (realized now - realized at day start) + (unrealized now - unrealized at day start)
    round PnL = realized + unrealized from the fill ledger (which covers the round)
    """

    def __init__(self, path: str | Path = "state/risk.json"):
        self.path = Path(path)
        self.data = _read(self.path, {})

    def save(self) -> None:
        _write(self.path, self.data)

    # -- halts --
    def halted(self, now: datetime | None = None) -> str | None:
        h = self.data.get("halted")
        if not h:
            return None
        if h.get("until") == "round":
            return h["reason"]
        now = now or datetime.now(ET)
        if now < datetime.fromisoformat(h["until"]):
            return f"{h['reason']} (until {h['until']})"
        self.data.pop("halted", None)
        self.save()
        return None

    def halt(self, reason: str, until: str | datetime) -> None:
        self.data["halted"] = {"reason": reason,
                               "until": until if isinstance(until, str) else until.isoformat()}
        self.save()

    def reset(self) -> None:
        self.data.pop("halted", None)
        self.save()

    # -- daily baseline --
    def roll_day(self, realized: Decimal, unrealized: Decimal, now: datetime | None = None) -> None:
        today = (now or datetime.now(ET)).astimezone(ET).date().isoformat()
        if self.data.get("day") != today:
            self.data.update({"day": today, "day_realized0": str(realized), "day_unrealized0": str(unrealized),
                              "taker_cost_today": "0"})
            self.save()

    def day_pnl(self, realized: Decimal, unrealized: Decimal) -> Decimal:
        return (realized - D(self.data.get("day_realized0", "0"))) + \
               (unrealized - D(self.data.get("day_unrealized0", "0")))

    def check(self, realized: Decimal, unrealized: Decimal, daily_max: Decimal, round_max: Decimal,
              now: datetime | None = None) -> str | None:
        """Returns a reason string (and persists a halt) if a limit is breached."""
        now = now or datetime.now(ET)
        self.roll_day(realized, unrealized, now)
        day = self.day_pnl(realized, unrealized)
        rnd = realized + unrealized
        if rnd <= -round_max:
            reason = f"round max loss hit: PnL {rnd:.2f} <= -{round_max}"
            self.halt(reason, "round")
            return reason
        if day <= -daily_max:
            reason = f"daily max loss hit: today's PnL {day:.2f} <= -{daily_max}"
            self.halt(reason, _et_midnight_after(now))
            return reason
        return None

    # -- taker-assist budget --
    def taker_cost_today(self) -> Decimal:
        return D(self.data.get("taker_cost_today", "0"))

    def add_taker_cost(self, usd: Decimal) -> None:
        self.data["taker_cost_today"] = str(self.taker_cost_today() + usd)
        self.save()

    # -- running time (for pts/hour) --
    def add_run_seconds(self, secs: float) -> None:
        self.data["run_seconds"] = float(self.data.get("run_seconds", 0.0)) + secs
        self.save()

    def run_hours(self) -> float:
        return float(self.data.get("run_seconds", 0.0)) / 3600

    # -- allocations already known (wallet-movement detection) --
    def known_allocations(self) -> set[str]:
        return set(self.data.get("allocations_seen", []))

    def remember_allocations(self, ids) -> None:
        self.data["allocations_seen"] = sorted(self.known_allocations() | set(ids))
        self.save()


def now_ts() -> float:
    return time.time()
