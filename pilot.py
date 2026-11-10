#!/usr/bin/env python3
"""Strategy-Pilot: paper trading engine cho chiến lược sell OTM BTC options (Deribit).

Engine lo phần tính toán và các rule bắt buộc (exit, roll, entry ở khung cuối).
Phần phán đoán (chọn khung entry, suggestion ngoài rule) do Claude làm qua lệnh `trade` / `note`.
Spec: RULES.md. Schema: README.md. Chỉ dùng Deribit public API, không đặt lệnh thật.

  python pilot.py run [--dry]                  # mỗi lần routine chạy
  python pilot.py trade open --side call --qty auto --source claude --rule R-E2 --reason "..."
  python pilot.py trade close --lot L-3 --source user --reason "..."
  python pilot.py note --type decision --text "..."
  python pilot.py set paused.call true
  python pilot.py status
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
API = "https://www.deribit.com/api/v2/public/"
VN = timezone(timedelta(hours=7))
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

STATE_PATH = os.path.join(ROOT, "state.json")
CONFIG_PATH = os.path.join(ROOT, "config.json")
LOG_PATH = os.path.join(ROOT, "logs", "trade-log.jsonl")

NOW = None  # datetime UTC, set in main (--now chỉ dùng để test logic khung giờ)
DRY = False


# ---------------------------------------------------------------- helpers

def api(method, **params):
    url = API + method + "?" + urllib.parse.urlencode(params)
    err = None
    for _ in range(4):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.load(r)["result"]
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(0.6)
    raise RuntimeError(f"Deribit {method} lỗi: {err}")


def now_ms():
    return int(NOW.timestamp() * 1000)


def ts_fields():
    return {"ts_utc": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ts_vn": NOW.astimezone(VN).strftime("%Y-%m-%d %H:%M")}


def expiry_code(d):
    return f"{d.day}{MONTHS[d.month - 1]}{d.strftime('%y')}"


def floor_step(x, step):
    return max(0.0, math.floor(x / step + 1e-9) * step)


def r4(x):
    return None if x is None else round(x, 4)


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_delta(F, K, T, sigma, is_call):
    if T <= 0 or sigma <= 0:
        return 1.0 if (is_call and F > K) else 0.0
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return ncdf(d1) if is_call else ncdf(d1) - 1


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    if DRY:
        return
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
        f.write("\n")


class Log:
    def __init__(self, run_id):
        self.run_id = run_id
        self.records = []

    def add(self, rtype, **data):
        rec = {"type": rtype, "run_id": self.run_id, **ts_fields(), **data}
        self.records.append(rec)
        return rec

    def flush(self):
        if DRY or not self.records:
            return
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            for rec in self.records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def new_state(cfg):
    return {
        "version": 1,
        "created_utc": ts_fields()["ts_utc"],
        "initial_equity_usd": cfg["initial_equity_usd"],
        "cash_usd": cfg["initial_equity_usd"],
        "fees_paid_usd": 0.0,
        "realized_pnl_usd": 0.0,
        "next_lot": 1,
        "positions": [],
        "cycles": {},
        "entries": {},
        "pending_suggestions": [],
        "overrides": {"paused": {"call": False, "put": False}},
        "last_run_id": None,
        "last_run_utc": None,
        "last_obs": None,
    }


def effective_cfg(cfg, state):
    out = dict(cfg)
    for k, v in state.get("overrides", {}).items():
        if k != "paused":
            out[k] = v
    return out


# ---------------------------------------------------------------- market

class Market:
    """Snapshot data Deribit cho một lần chạy (lazy, cache theo instrument)."""

    def __init__(self):
        self.index = api("get_index_price", index_name="btc_usd")["index_price"]
        self.instruments = {i["instrument_name"]: i for i in
                            api("get_instruments", currency="BTC", kind="option", expired="false")}
        self._book = None
        self._tick = {}
        self._candles = None

    def book(self):
        if self._book is None:
            self._book = {b["instrument_name"]: b for b in
                          api("get_book_summary_by_currency", currency="BTC", kind="option")}
        return self._book

    def ticker(self, name):
        if name not in self._tick:
            t = api("ticker", instrument_name=name)
            bid = t.get("best_bid_price") if t.get("best_bid_amount") else None
            ask = t.get("best_ask_price") if t.get("best_ask_amount") else None
            self._tick[name] = {"bid": bid, "ask": ask, "mark": t.get("mark_price"),
                                "mark_iv": t.get("mark_iv"), "greeks": t.get("greeks") or {},
                                "index": t.get("index_price")}
        return self._tick[name]

    def expiries(self):
        return {n.split("-")[1] for n in self.instruments}

    def expiry_ts(self, code):
        for n, i in self.instruments.items():
            if n.split("-")[1] == code:
                return i["expiration_timestamp"]
        return None

    def candles_1h(self):
        if self._candles is None:
            end = now_ms()
            c = api("get_tradingview_chart_data", instrument_name="BTC-PERPETUAL",
                    resolution="60", start_timestamp=end - 6 * 3600 * 1000, end_timestamp=end)
            self._candles = [{"t": t, "o": o, "h": h, "l": lo, "c": cl}
                             for t, o, h, lo, cl in zip(c["ticks"], c["open"], c["high"], c["low"], c["close"])]
        return self._candles

    def last_closed_1h(self):
        cur = now_ms() // 3600000 * 3600000
        closed = [c for c in self.candles_1h() if c["t"] < cur]
        return closed[-1] if closed else None

    def dvol(self):
        try:
            end = now_ms()
            d = api("get_volatility_index_data", currency="BTC", resolution="3600",
                    start_timestamp=end - 3 * 3600 * 1000, end_timestamp=end)["data"]
            return d[-1][4] if d else None
        except RuntimeError:
            return None

    def candidates(self, side, code, band, farther_than=None, n_check=4):
        """Strike OTM cùng expiry, |delta| gần giữa band. Lọc sơ bằng BS delta từ book summary, chốt bằng delta Deribit."""
        is_call = side == "call"
        target = (band[0] + band[1]) / 2
        pool = []
        for name, b in self.book().items():
            parts = name.split("-")
            if parts[1] != code or parts[3] != ("C" if is_call else "P"):
                continue
            K = float(parts[2])
            if (is_call and K <= self.index) or (not is_call and K >= self.index):
                continue
            if farther_than is not None and ((is_call and K <= farther_than) or (not is_call and K >= farther_than)):
                continue
            T = (self.instruments[name]["expiration_timestamp"] - now_ms()) / 1000 / (365 * 86400)
            iv = (b.get("mark_iv") or 0) / 100
            F = b.get("underlying_price") or self.index
            pool.append((abs(abs(bs_delta(F, K, T, iv, is_call)) - target), name, K))
        pool.sort()
        out = []
        for _, name, K in pool[:n_check]:
            t = self.ticker(name)
            d = abs(t["greeks"].get("delta") or 0)
            out.append({"instrument": name, "strike": K, "delta": r4(d), "bid": t["bid"], "ask": t["ask"],
                        "mark": r4(t["mark"]), "mark_iv": t["mark_iv"],
                        "in_band": band[0] <= d <= band[1]})
        out.sort(key=lambda c: (not c["in_band"], abs(c["delta"] - target)))
        return out


# ---------------------------------------------------------------- core logic

def dte_days(expiry_ts):
    return (expiry_ts - now_ms()) / 86400000


def exit2_pct(cfg, dte):
    for th in cfg["exit2_thresholds"]:
        if th["dte_below"] is None or dte < th["dte_below"]:
            return th["pct"]
    return cfg["exit2_thresholds"][-1]["pct"]


def exit2_trigger(pos, cfg, index):
    pct = exit2_pct(cfg, dte_days(pos["expiry_ts"]))
    K = pos["strike"]
    level = K * (1 - pct) if pos["side"] == "call" else K * (1 + pct)
    hit = index >= level if pos["side"] == "call" else index <= level
    return hit, pct, level


def fee_usd(cfg, price_btc, qty, index):
    per = min(cfg["fee_btc_per_contract"], cfg["fee_cap_pct_of_price"] * price_btc)
    return per * qty * index


def target_cycle(now_vn):
    """Thứ 6 đầu tiên cách hôm nay ≥ 7 ngày: thứ 4 → +9, thứ 5 → +8."""
    d = now_vn.date() + timedelta(days=7)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return expiry_code(d)


def cycle_side(state, cycle, side):
    c = state["cycles"].setdefault(cycle, {})
    return c.setdefault(side, {"exit2_count": 0, "stopped": False, "pending_roll": None})


def equity(state, mkt):
    liab = 0.0
    for p in state["positions"]:
        t = mkt.ticker(p["instrument"])
        liab += (t["mark"] or 0) * mkt.index * p["qty"]
    return state["cash_usd"] - liab


def side_qty(state, side):
    return sum(p["qty"] for p in state["positions"] if p["side"] == side)


def open_short(state, mkt, cfg, log, instrument, qty, cycle, leg, rule, source, reason, ref=None):
    t = mkt.ticker(instrument)
    price, basis = (t["bid"], "bid") if t["bid"] else (t["mark"], "mark")
    inst = mkt.instruments[instrument]
    fee = fee_usd(cfg, price, qty, mkt.index)
    premium = price * mkt.index * qty
    lot = {"lot_id": f"L-{state['next_lot']}", "instrument": instrument, "side": inst["option_type"],
           "strike": inst["strike"], "expiry": instrument.split("-")[1], "expiry_ts": inst["expiration_timestamp"],
           "qty": round(qty, 4), "entry_price_btc": price, "entry_index": mkt.index,
           "premium_usd": round(premium, 2), "entry_fee_usd": round(fee, 2), "entry_delta": r4(t["greeks"].get("delta")),
           "opened_utc": ts_fields()["ts_utc"], "cycle": cycle, "leg": leg, "rule": rule, "source": source}
    state["next_lot"] += 1
    state["positions"].append(lot)
    state["cash_usd"] += premium - fee
    state["fees_paid_usd"] += fee
    log.add("trade", action="open", lot_id=lot["lot_id"], instrument=instrument, side=lot["side"], qty=lot["qty"],
            price_btc=price, price_basis=basis, index=mkt.index, premium_usd=lot["premium_usd"],
            fee_usd=lot["entry_fee_usd"], delta=lot["entry_delta"], mark_iv=t["mark_iv"], cycle=cycle, leg=leg,
            rule=rule, source=source, reason=reason, ref=ref)
    return lot


def close_lot(state, mkt, cfg, log, lot, rule, source, reason, qty=None, ref=None, price_override=None):
    qty = lot["qty"] if qty is None or qty >= lot["qty"] - 1e-9 else qty
    t = mkt.ticker(lot["instrument"])
    if price_override is not None:
        price, basis = price_override, ("ask" if t["ask"] else "exit1_level")
    elif t["ask"]:
        price, basis = t["ask"], "ask"
    else:
        price, basis = t["mark"], "mark"
    fee = fee_usd(cfg, price, qty, mkt.index)
    cost = price * mkt.index * qty
    frac = qty / lot["qty"]
    pnl = lot["premium_usd"] * frac - lot["entry_fee_usd"] * frac - cost - fee
    state["cash_usd"] -= cost + fee
    state["fees_paid_usd"] += fee
    state["realized_pnl_usd"] += pnl
    if frac >= 1 - 1e-9:
        state["positions"].remove(lot)
    else:
        lot["qty"] = round(lot["qty"] - qty, 4)
        lot["premium_usd"] = round(lot["premium_usd"] * (1 - frac), 2)
        lot["entry_fee_usd"] = round(lot["entry_fee_usd"] * (1 - frac), 2)
    return log.add("trade", action="close", lot_id=lot["lot_id"], instrument=lot["instrument"], side=lot["side"],
                   qty=round(qty, 4), price_btc=price, price_basis=basis, index=mkt.index, cost_usd=round(cost, 2),
                   fee_usd=round(fee, 2), pnl_usd=round(pnl, 2), cycle=lot["cycle"], leg=lot["leg"],
                   rule=rule, source=source, reason=reason, ref=ref)


def make_room(state, mkt, cfg, log, side, want_qty, cycle, eq):
    """R-CAP: tổng short mỗi side ≤ leverage × equity. Thiếu room thì đóng bớt lot của cycle cũ (ask thấp nhất trước)."""
    step = cfg["qty_step"]
    cap = cfg["leverage_per_side"] * eq / mkt.index
    room = floor_step(cap - side_qty(state, side), step)
    if room >= want_qty - 1e-9:
        return want_qty
    old = [p for p in state["positions"] if p["side"] == side and p["cycle"] != cycle]
    old.sort(key=lambda p: (mkt.ticker(p["instrument"])["ask"] or mkt.ticker(p["instrument"])["mark"] or 0))
    for p in old:
        need = round(want_qty - room, 4)
        if need <= 1e-9:
            break
        q = min(p["qty"], math.ceil(need / step - 1e-9) * step)
        close_lot(state, mkt, cfg, log, p, "R-CAP", "rule",
                  f"Đóng bớt lot cycle cũ để có room cho cycle {cycle} (cap {cfg['leverage_per_side']}x/side)", qty=q)
        room = floor_step(cap - side_qty(state, side), step)
    return min(want_qty, room)


def daily_remaining(state, cfg, side, eq, index, now_vn):
    quota = cfg["daily_entry_fraction"] * cfg["leverage_per_side"] * eq / index
    done = state["entries"].get(now_vn.strftime("%Y-%m-%d"), {}).get(side, 0.0)
    return floor_step(quota - done, cfg["qty_step"])


def record_entry(state, side, qty, now_vn):
    day = state["entries"].setdefault(now_vn.strftime("%Y-%m-%d"), {})
    day[side] = round(day.get(side, 0.0) + qty, 4)


def entry_window(cfg, now_vn):
    hours = cfg["entry_windows_vn"].get(str(now_vn.weekday()), [])
    return (now_vn.hour in hours), (bool(hours) and now_vn.hour == max(hours)), hours


def side_blocked(state, cycle, side):
    cs = cycle_side(state, cycle, side)
    if state["overrides"]["paused"].get(side):
        return "paused"
    if cs["stopped"]:
        return "stopped"
    if cs["exit2_count"] >= 1:
        return "đã exit 2 trong cycle, chỉ còn roll"
    return None


def do_entry(state, mkt, cfg, log, side, qty, cycle, rule, source, reason, instrument=None, ref=None, band=None,
             leg="initial"):
    """Mở lệnh sell entry: chọn strike theo delta (hoặc instrument chỉ định), kiểm cap, ghi quota ngày."""
    now_vn = NOW.astimezone(VN)
    eq = equity(state, mkt)
    if qty == "auto":
        qty = daily_remaining(state, cfg, side, eq, mkt.index, now_vn)
    qty = make_room(state, mkt, cfg, log, side, float(qty), cycle, eq)
    if qty < cfg["qty_step"] - 1e-9:
        log.add("flag", code="no_room", side=side, cycle=cycle, text="Hết room theo cap leverage, không entry")
        return None
    flag = None
    if instrument is None:
        cands = mkt.candidates(side, cycle, band or cfg["entry_delta"])
        if not cands:
            log.add("flag", code="no_candidate", side=side, cycle=cycle, text="Không tìm được strike phù hợp")
            return None
        instrument = cands[0]["instrument"]
        if not cands[0]["in_band"]:
            flag = f"Không có strike trong band delta {band or cfg['entry_delta']}, dùng gần nhất {instrument} Δ{cands[0]['delta']}"
    lot = open_short(state, mkt, cfg, log, instrument, qty, cycle, leg, rule, source, reason, ref)
    if rule.startswith("R-E"):
        record_entry(state, side, qty, now_vn)
    if flag:
        log.add("flag", code="delta_out_of_band", side=side, text=flag)
    return lot


def process_exits(state, mkt, cfg, log):
    # Expiry: settle theo delivery price
    expired = [p for p in state["positions"] if p["expiry_ts"] <= now_ms()]
    if expired:
        dp = {d["date"]: d["delivery_price"] for d in api("get_delivery_prices", index_name="btc_usd", count=10)["data"]}
        for p in expired:
            day = datetime.fromtimestamp(p["expiry_ts"] / 1000, timezone.utc).strftime("%Y-%m-%d")
            S = dp.get(day)
            if S is None:
                log.add("flag", code="settlement_pending", lot_id=p["lot_id"], text="Chưa có delivery price")
                continue
            K = p["strike"]
            payoff = max(0.0, S - K) if p["side"] == "call" else max(0.0, K - S)
            fee = min(cfg["delivery_fee_pct"] * S, cfg["fee_cap_pct_of_price"] * payoff) * p["qty"] if payoff else 0.0
            cost = payoff * p["qty"]
            pnl = p["premium_usd"] - p["entry_fee_usd"] - cost - fee
            state["cash_usd"] -= cost + fee
            state["fees_paid_usd"] += fee
            state["realized_pnl_usd"] += pnl
            state["positions"].remove(p)
            log.add("expiry", lot_id=p["lot_id"], instrument=p["instrument"], qty=p["qty"], delivery_price=S,
                    itm=payoff > 0, cost_usd=round(cost, 2), fee_usd=round(fee, 2), pnl_usd=round(pnl, 2),
                    cycle=p["cycle"], leg=p["leg"])

    # Exit 1 (R-X1) và Exit 2 (R-X2)
    exit2_events = {}  # (cycle, side) -> [close records]
    for p in list(state["positions"]):
        t = mkt.ticker(p["instrument"])
        lvl = cfg["exit1_price_btc"]
        if (t["ask"] is not None and t["ask"] <= lvl + 1e-12) or (t["ask"] is None and (t["mark"] or 1) <= lvl + 1e-12):
            close_lot(state, mkt, cfg, log, p, "R-X1", "rule", f"Premium về {lvl} BTC", price_override=t["ask"] or lvl)
            continue
        hit, pct, level = exit2_trigger(p, cfg, mkt.index)
        if hit:
            rec = close_lot(state, mkt, cfg, log, p, "R-X2", "rule",
                            f"Spot {mkt.index:,.0f} vượt ngưỡng {pct:.1%} của strike {p['strike']:,.0f} "
                            f"(level {level:,.0f}, DTE {dte_days(p['expiry_ts']):.2f})")
            exit2_events.setdefault((p["cycle"], p["side"]), []).append((rec, p))

    for (cycle, side), recs in exit2_events.items():
        cs = cycle_side(state, cycle, side)
        cs["exit2_count"] += 1
        qty = round(sum(r["qty"] for r, _ in recs), 4)
        if side == "call":
            far = max(p["strike"] for _, p in recs)
        else:
            far = min(p["strike"] for _, p in recs)
        expiry = recs[0][1]["expiry"]
        if cs["exit2_count"] >= cfg["max_exit2_per_cycle"]:
            cs["stopped"] = True
            cs["pending_roll"] = None
            log.add("flag", code="stop_roll", cycle=cycle, side=side,
                    text=f"Đã exit {cs['exit2_count']} lần, dừng roll side {side} cycle {cycle} (R-SL)")
        else:
            cs["pending_roll"] = {"qty": qty, "from_strike": far, "expiry": expiry,
                                  "expiry_ts": recs[0][1]["expiry_ts"], "created_utc": ts_fields()["ts_utc"], "waits": 0}


def roll_expiry_options(mkt, cfg, cycle, pr):
    """Expiry được phép roll: thứ 6 của cycle + các offset ngày trong cfg (mặc định 0, 1, 2 → T6, T7, CN).
    Không roll về expiry sớm hơn expiry của lot vừa exit. ts = None nếu Deribit chưa list."""
    base = datetime.strptime(cycle, "%d%b%y").date()
    out = []
    for off in cfg.get("roll_expiry_offsets_days", [0]):
        d = base + timedelta(days=off)
        exp_dt = datetime(d.year, d.month, d.day, 8, tzinfo=timezone.utc)  # Deribit đáo hạn 08:00 UTC
        if exp_dt.timestamp() * 1000 < pr["expiry_ts"] - 1:
            continue
        code = expiry_code(d)
        ts = mkt.expiry_ts(code)
        out.append({"code": code, "ts": ts, "dte": dte_days(ts or exp_dt.timestamp() * 1000)})
    return out


def process_rolls(state, mkt, cfg, log):
    for cycle, sides in state["cycles"].items():
        for side, cs in sides.items():
            pr = cs.get("pending_roll")
            if not pr:
                continue
            if state["overrides"]["paused"].get(side):
                continue
            # R-R1/R-R3: expiry roll là thứ 6 của cycle, hoặc +1 (thứ 7), +2 (CN) ngày, chỉ những expiry còn DTE ≥ roll_min_dte
            opts = roll_expiry_options(mkt, cfg, cycle, pr)
            eligible = [o for o in opts if o["dte"] >= cfg["roll_min_dte"]]
            if not eligible:
                cs["pending_roll"] = None
                log.add("flag", code="roll_cancelled", cycle=cycle, side=side,
                        text=f"Bỏ roll: mọi expiry roll ({', '.join(o['code'] for o in opts)}) đều có "
                             f"DTE < {cfg['roll_min_dte']} (R-R3)")
                continue
            listed = [o for o in eligible if o["ts"] is not None]
            if not listed:
                log.add("flag", code="cycle_not_listed", cycle=cycle, side=side,
                        text=f"Chờ roll: expiry {', '.join(o['code'] for o in eligible)} chưa list trên Deribit")
                continue
            c = mkt.last_closed_1h()
            rng = (c["h"] - c["l"]) / c["l"] if c else 0
            if rng >= cfg["roll_block_1h_range"]:
                pr["waits"] += 1
                rec = dict(code="roll_wait", cycle=cycle, side=side, range_1h=r4(rng),
                           text=f"Chờ roll: nến 1H gần nhất H-L {rng:.2%} ≥ {cfg['roll_block_1h_range']:.0%} (R-R2)")
                if pr["waits"] == 1:
                    log.add("flag", **rec)
                else:
                    log.add("roll_wait", **rec)
                continue
            # Ưu tiên expiry gần nhất có strike trong band delta; không có thì lấy strike gần band nhất của expiry gần nhất
            cands, pick = [], None
            for o in listed:
                c_o = mkt.candidates(side, o["code"], cfg["roll_delta"], farther_than=pr["from_strike"])
                if c_o and (not cands or (c_o[0]["in_band"] and not cands[0]["in_band"])):
                    cands, pick = c_o, o
                if cands and cands[0]["in_band"]:
                    break
            if not cands:
                log.add("flag", code="no_candidate", cycle=cycle, side=side, text="Không có strike để roll")
                continue
            eq = equity(state, mkt)
            qty = make_room(state, mkt, cfg, log, side, pr["qty"], cycle, eq)
            if qty < cfg["qty_step"] - 1e-9:
                log.add("flag", code="no_room", cycle=cycle, side=side, text="Hết room, không roll được")
                continue
            lot = open_short(state, mkt, cfg, log, cands[0]["instrument"], qty, cycle, "roll", "R-R1", "rule",
                             f"Roll sau exit 2 từ strike {pr['from_strike']:,.0f} sang expiry {pick['code']} "
                             f"(DTE {pick['dte']:.2f}), nến 1H H-L {rng:.2%}")
            if not cands[0]["in_band"]:
                log.add("flag", code="delta_out_of_band", side=side,
                        text=f"Roll dùng {lot['instrument']} Δ{cands[0]['delta']} (ngoài band {cfg['roll_delta']})")
            cs["pending_roll"] = None


def process_entries(state, mkt, cfg, log):
    now_vn = NOW.astimezone(VN)
    in_win, is_last, hours = entry_window(cfg, now_vn)
    if not in_win:
        return {"open": False}
    cycle = target_cycle(now_vn)
    info = {"open": True, "is_last_window_today": is_last, "windows_today": hours, "cycle": cycle, "sides": {}}
    if cycle not in mkt.expiries():
        log.add("flag", code="cycle_not_listed", cycle=cycle, text=f"Expiry {cycle} chưa list trên Deribit")
        info["error"] = "cycle_not_listed"
        return info
    eq = equity(state, mkt)
    for side in ("call", "put"):
        blocked = side_blocked(state, cycle, side)
        rem = daily_remaining(state, cfg, side, eq, mkt.index, now_vn)
        s = {"remaining_qty_today": rem, "blocked": blocked}
        if blocked or rem < cfg["qty_step"] - 1e-9:
            info["sides"][side] = s
            continue
        if is_last:
            lot = do_entry(state, mkt, cfg, log, side, "auto", cycle, "R-E3", "rule",
                           "Khung entry cuối ngày: vào phần quota còn lại")
            s["auto_entered"] = lot["lot_id"] if lot else None
        else:
            s["candidates"] = mkt.candidates(side, cycle, cfg["entry_delta"])[:3]
        info["sides"][side] = s
    return info


def flags_and_obs(state, mkt, cfg, log):
    for p in state["positions"]:
        hit, pct, level = exit2_trigger(p, cfg, mkt.index)
        buf = cfg["flag_near_exit2_buffer"]
        near = mkt.index >= level * (1 - buf) if p["side"] == "call" else mkt.index <= level * (1 + buf)
        if near and not hit:
            log.add("flag", code="near_exit2", lot_id=p["lot_id"], instrument=p["instrument"],
                    text=f"Spot {mkt.index:,.0f} cách level exit 2 ({level:,.0f}) dưới {buf:.0%}")
    lo = state.get("last_obs")
    if lo and lo.get("index"):
        mv = mkt.index / lo["index"] - 1
        if abs(mv) >= cfg["flag_spot_move_pct"]:
            log.add("flag", code="spot_move", move_pct=round(mv * 100, 2),
                    text=f"Spot {mv:+.2%} kể từ lần chạy trước ({lo['index']:,.0f} → {mkt.index:,.0f})")


def positions_view(state, mkt, cfg):
    rows = []
    for p in state["positions"]:
        t = mkt.ticker(p["instrument"])
        hit, pct, level = exit2_trigger(p, cfg, mkt.index)
        mtm = p["premium_usd"] - (t["mark"] or 0) * mkt.index * p["qty"]
        rows.append({"lot_id": p["lot_id"], "instrument": p["instrument"], "side": p["side"], "qty": p["qty"],
                     "leg": p["leg"], "cycle": p["cycle"], "entry_btc": p["entry_price_btc"], "mark_btc": r4(t["mark"]),
                     "bid": t["bid"], "ask": t["ask"], "delta": r4(t["greeks"].get("delta")), "mark_iv": t["mark_iv"],
                     "dte": round(dte_days(p["expiry_ts"]), 2), "spot_to_strike_pct": round((p["strike"] / mkt.index - 1) * 100, 2),
                     "exit2_pct": pct, "exit2_level": round(level), "unrealized_usd": round(mtm, 2)})
    return rows


def std_margin_usd(state, mkt):
    """Ước tính initial margin kiểu standard của Deribit (chỉ để log, không chặn lệnh)."""
    tot = 0.0
    for p in state["positions"]:
        t = mkt.ticker(p["instrument"])
        otm = max(0.0, (p["strike"] - mkt.index) if p["side"] == "call" else (mkt.index - p["strike"])) / mkt.index
        tot += (max(0.15 - otm, 0.10) + (t["mark"] or 0)) * mkt.index * p["qty"]
    return tot


def summary(state, mkt, cfg):
    eq = equity(state, mkt)
    cap = cfg["leverage_per_side"] * eq
    return {"index": mkt.index, "equity_usd": round(eq, 2), "cash_usd": round(state["cash_usd"], 2),
            "realized_pnl_usd": round(state["realized_pnl_usd"], 2), "fees_paid_usd": round(state["fees_paid_usd"], 2),
            "return_pct": round((eq / state["initial_equity_usd"] - 1) * 100, 2),
            "notional_usd": {s: round(side_qty(state, s) * mkt.index) for s in ("call", "put")},
            "leverage_used": {s: round(side_qty(state, s) * mkt.index / eq, 2) if eq > 0 else None for s in ("call", "put")},
            "cap_notional_per_side_usd": round(cap),
            "std_margin_est_pct": round(std_margin_usd(state, mkt) / eq * 100, 1) if eq > 0 else None}


def context_run_id(state):
    """Lệnh trade/note gắn vào run gần nhất (≤ 60 phút), ngoài ra là phiên user."""
    last = state.get("last_run_utc")
    if last and (NOW - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() < 3600:
        return state["last_run_id"]
    return "U-" + NOW.astimezone(VN).strftime("%y%m%d-%H%M")


def prune(state):
    today = NOW.astimezone(VN).date()
    state["entries"] = {d: v for d, v in state["entries"].items()
                        if (today - datetime.strptime(d, "%Y-%m-%d").date()).days <= 14}
    live = {p["cycle"] for p in state["positions"]}
    for cyc in list(state["cycles"]):
        sides = state["cycles"][cyc]
        pend = any(s.get("pending_roll") for s in sides.values())
        if cyc not in live and not pend:
            # giữ cycle tới khi qua expiry 3 ngày để còn chặn entry lại
            try:
                d = datetime.strptime(cyc, "%d%b%y").date()
            except ValueError:
                continue
            if (today - d).days > 3:
                del state["cycles"][cyc]


# ---------------------------------------------------------------- commands

def cmd_run(args, cfg_base):
    state = load_json(STATE_PATH) or new_state(cfg_base)
    cfg = effective_cfg(cfg_base, state)
    run_id = "R-" + NOW.astimezone(VN).strftime("%y%m%d-%H%M")
    log = Log(run_id)
    mkt = Market()

    for s in state["pending_suggestions"]:
        log.add("suggestion_expired", id=s["id"])
    state["pending_suggestions"] = []

    process_exits(state, mkt, cfg, log)
    process_rolls(state, mkt, cfg, log)
    entry = process_entries(state, mkt, cfg, log)
    flags_and_obs(state, mkt, cfg, log)

    c1h = mkt.last_closed_1h()
    summ = summary(state, mkt, cfg)
    obs = {"index": mkt.index, "dvol": mkt.dvol(),
           "last_1h": {k: c1h[k] for k in ("o", "h", "l", "c")} if c1h else None,
           "last_1h_range_pct": round((c1h["h"] - c1h["l"]) / c1h["l"] * 100, 2) if c1h else None,
           "positions": positions_view(state, mkt, cfg), **{k: summ[k] for k in ("equity_usd", "leverage_used", "std_margin_est_pct")}}
    log.add("obs", **obs)

    state["last_obs"] = {"index": mkt.index, "ts_utc": ts_fields()["ts_utc"], "equity_usd": summ["equity_usd"]}
    state["last_run_id"] = run_id
    state["last_run_utc"] = ts_fields()["ts_utc"]
    prune(state)
    save_state(state)
    log.flush()

    events = [r for r in log.records if r["type"] in ("trade", "expiry", "flag")]
    report = {"run_id": run_id, **ts_fields(), "dry": DRY, "notify": bool(events),
              "events": events, "entry_window": entry, "summary": summ, "positions": obs["positions"],
              "pending_rolls": {f"{c}/{s}": v["pending_roll"] for c, ss in state["cycles"].items()
                                for s, v in ss.items() if v.get("pending_roll")},
              "market": {k: obs[k] for k in ("index", "dvol", "last_1h", "last_1h_range_pct")}}
    print(json.dumps(report, ensure_ascii=False, indent=1))


def cmd_trade(args, cfg_base):
    state = load_json(STATE_PATH) or new_state(cfg_base)
    cfg = effective_cfg(cfg_base, state)
    log = Log(context_run_id(state))
    mkt = Market()
    if args.op == "open":
        if not args.side:
            sys.exit("cần --side")
        cycle = args.cycle or target_cycle(NOW.astimezone(VN))
        if args.rule.startswith("R-E"):
            blocked = side_blocked(state, cycle, args.side)
            if blocked:
                sys.exit(f"Side {args.side} cycle {cycle} đang bị chặn: {blocked}")
        band = [float(x) for x in args.delta_band] if args.delta_band else None
        do_entry(state, mkt, cfg, log, args.side, args.qty if args.qty == "auto" else float(args.qty), cycle,
                 args.rule, args.source, args.reason, instrument=args.instrument, ref=args.ref, band=band,
                 leg=args.leg or "initial")
    else:
        lots = [p for p in state["positions"] if (args.lot and p["lot_id"] == args.lot)
                or (args.instrument and p["instrument"] == args.instrument) or args.all]
        if not lots:
            sys.exit("Không tìm thấy lot")
        for p in lots:
            close_lot(state, mkt, cfg, log, p, args.rule, args.source, args.reason,
                      qty=None if args.qty in (None, "auto") else float(args.qty), ref=args.ref)
    save_state(state)
    log.flush()
    print(json.dumps({"records": log.records, "summary": summary(state, mkt, cfg),
                      "positions": positions_view(state, mkt, cfg)}, ensure_ascii=False, indent=1))


def cmd_note(args, cfg_base):
    state = load_json(STATE_PATH) or new_state(cfg_base)
    log = Log(context_run_id(state))
    data = json.loads(args.data) if args.data else {}
    rec = log.add(args.type, text=args.text, **data)
    if args.type == "suggestion":
        if "id" not in rec:
            n = len(state["pending_suggestions"]) + 1
            rec["id"] = f"S-{NOW.astimezone(VN).strftime('%y%m%d-%H')}-{n}"
        state["pending_suggestions"].append({"id": rec["id"], "text": args.text, **data})
    save_state(state)
    log.flush()
    print(json.dumps(rec, ensure_ascii=False))


def cmd_set(args, cfg_base):
    state = load_json(STATE_PATH) or new_state(cfg_base)
    val = json.loads(args.value) if args.value not in ("true", "false") else args.value == "true"
    if args.key.startswith("paused."):
        state["overrides"]["paused"][args.key.split(".", 1)[1]] = val
    elif args.key in cfg_base:
        state["overrides"][args.key] = val
    else:
        sys.exit(f"Key không hợp lệ: {args.key}")
    log = Log(context_run_id(state))
    log.add("user_cmd", text=f"set {args.key}={args.value}", reason=args.reason)
    save_state(state)
    log.flush()
    print(json.dumps(state["overrides"], ensure_ascii=False))


def cmd_status(args, cfg_base):
    state = load_json(STATE_PATH) or new_state(cfg_base)
    cfg = effective_cfg(cfg_base, state)
    mkt = Market()
    print(json.dumps({"summary": summary(state, mkt, cfg), "positions": positions_view(state, mkt, cfg),
                      "cycles": state["cycles"], "pending_suggestions": state["pending_suggestions"],
                      "overrides": state["overrides"]}, ensure_ascii=False, indent=1))


def main():
    global NOW, DRY, STATE_PATH, LOG_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", help="ISO time (chỉ để test logic khung giờ)")
    ap.add_argument("--dry", action="store_true", help="không ghi state/log")
    ap.add_argument("--data-dir", help="thư mục state/log thay thế (test)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    t = sub.add_parser("trade")
    t.add_argument("op", choices=["open", "close"])
    t.add_argument("--side", choices=["call", "put"])
    t.add_argument("--qty", default=None, help="số contract hoặc 'auto' (quota còn lại trong ngày)")
    t.add_argument("--instrument")
    t.add_argument("--lot")
    t.add_argument("--all", action="store_true")
    t.add_argument("--cycle")
    t.add_argument("--leg", choices=["initial", "roll"])
    t.add_argument("--delta-band", nargs=2)
    t.add_argument("--rule", default="DISCRETION")
    t.add_argument("--source", required=True, choices=["claude", "user", "user_approved", "rule"])
    t.add_argument("--reason", required=True)
    t.add_argument("--ref")
    n = sub.add_parser("note")
    n.add_argument("--type", required=True, choices=["decision", "suggestion", "user_cmd", "flag", "answer"])
    n.add_argument("--text", required=True)
    n.add_argument("--data")
    s = sub.add_parser("set")
    s.add_argument("key")
    s.add_argument("value")
    s.add_argument("--reason", default="")
    args = ap.parse_args()

    NOW = datetime.fromisoformat(args.now).astimezone(timezone.utc) if args.now else datetime.now(timezone.utc)
    DRY = args.dry
    if args.data_dir:
        STATE_PATH = os.path.join(args.data_dir, "state.json")
        LOG_PATH = os.path.join(args.data_dir, "logs", "trade-log.jsonl")
    if args.cmd == "trade" and args.op == "open" and args.qty is None:
        args.qty = "auto"
    cfg = load_json(CONFIG_PATH)
    {"run": cmd_run, "trade": cmd_trade, "note": cmd_note, "set": cmd_set, "status": cmd_status}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
