"""
MT5 Trade Analytics Portal  (no database)

Setup (Windows PC where MT5 is installed and logged in):
    pip install MetaTrader5 pandas flask
    python app.py
Open http://127.0.0.1:5000

Default mode: attaches to your already-running, logged-in MT5 terminal.
Optional: type login + INVESTOR password + server in the page to switch account.
Nothing is stored: data is fetched, analysed in memory and sent to your browser.
"""
import math
import os
import time
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, Response

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
REVENGE_MIN = 15  # a trade opened within N minutes after a losing close is flagged

ALLOWED_ORIGINS = {origin.strip() for origin in os.getenv(
    "MT5_ALLOWED_ORIGINS",
    "http://127.0.0.1:5000,http://localhost:5000,https://trade-shadowfax29.vercel.app",
).split(",") if origin.strip()}


@app.after_request
def add_cors_headers(resp):
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "600"
        resp.headers["Vary"] = "Origin"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ---------------------------------------------------------------- MT5 access
def connect(login, password, server):
    if login and password and server:
        ok = mt5.initialize(login=int(login), password=password, server=server)
    else:
        ok = mt5.initialize()  # attach to the running terminal
    if not ok:
        raise RuntimeError(f"Could not connect to MT5: {mt5.last_error()}. "
                           "Make sure the MT5 terminal is open and logged in.")


def fetch_deals(days):
    end = datetime.now() + timedelta(days=1)  # +1 day covers broker server-time offset
    deals = mt5.history_deals_get(end - timedelta(days=days + 1), end)
    return list(deals) if deals else []


def fetch_deals_range(start, end):
    # pad a day either side so boundary deals and entry legs are included
    deals = mt5.history_deals_get(start - timedelta(days=1), end + timedelta(days=1))
    return list(deals) if deals else []


DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def offset_label(hours):
    if hours is None:
        return None
    minutes = int(round(hours * 60))
    sign = "+" if minutes >= 0 else "-"
    h, m = divmod(abs(minutes), 60)
    return f"GMT{sign}{h}:{m:02d}" if m else f"GMT{sign}{h}"


def detect_server_offset(symbols):
    """Infer the broker server's UTC offset from the freshest live tick."""
    now = time.time()
    latest = 0
    for symbol in list(symbols)[:10]:
        tick = mt5.symbol_info_tick(symbol)
        if tick and tick.time:
            latest = max(latest, int(tick.time))
    if not latest:
        return None
    raw = (latest - now) / 3600
    quarter = round(raw * 4) / 4
    # accept only a plausible offset whose tick is close to a 15-minute boundary
    if -12 <= quarter <= 14 and abs(raw - quarter) <= 0.2:
        return quarter
    return None


def coach_report(t, kpis, by_symbol, by_dow, by_hour, by_side,
                 server_offset, local_offset, currency):
    """Turn the stats into a plain-language read: avoid, mistakes, edge."""
    cur = f" {currency}" if currency else ""

    def money(v):
        return f"{v:+.2f}{cur}"

    def hour_label(h):
        return f"{int(round(h)) % 24:02d}:00"

    def local_label(h):
        if server_offset is None or local_offset is None:
            return None
        total = int(round(((h + local_offset - server_offset) % 24) * 60))
        hh, mm = divmod(total, 60)
        return f"{hh:02d}:{mm:02d}"

    def best_worst(rows, min_trades=1):
        pool = [r for r in rows if (r.get("trades") or 0) >= min_trades] or list(rows)
        if not pool:
            return None, None
        return max(pool, key=lambda r: r["net"]), min(pool, key=lambda r: r["net"])

    def hour_window(rows, length=3, min_trades=5):
        hourly = {int(r["k"]): r for r in rows}
        best = worst = None
        for start in range(24):
            net = trades = 0
            wins = 0.0
            for h in range(start, start + length):
                r = hourly.get(h % 24)
                if r:
                    net += r["net"] or 0
                    trades += r["trades"]
                    wins += (r["win"] or 0) * r["trades"] / 100
            if trades < min_trades:
                continue
            end = (start + length - 1) % 24
            item = dict(
                start=start, end=end, server=f"{hour_label(start)}–{hour_label(end)}",
                local=(f"{local_label(start)}–{local_label(end)}" if local_label(start) else None),
                net=round(net, 2), trades=trades,
                win=round(wins / trades * 100, 1) if trades else 0)
            if best is None or item["net"] > best["net"]:
                best = item
            if worst is None or item["net"] < worst["net"]:
                worst = item
        return best, worst

    best_sym, worst_sym = best_worst(by_symbol, 3)
    best_day, worst_day = best_worst(by_dow, 3)
    best_side, worst_side = best_worst(by_side, 1)
    min_window = max(1, min(5, len(t) // 4))
    best_w, worst_w = hour_window(by_hour, 3, min_window)
    if best_w is None:
        best_w, worst_w = hour_window(by_hour, 3, 1)

    ratio = abs(kpis["avg_win"] / kpis["avg_loss"]) if kpis.get("avg_loss") else None
    breakeven = 100 / (1 + ratio) if ratio else None
    net = kpis["net"] or 0
    if net > 0:
        result = f"up {net:.2f}{cur}"
    elif net < 0:
        result = f"down {abs(net):.2f}{cur}"
    else:
        result = f"flat at 0.00{cur}"

    parts = [f"Across {kpis['trades']} closed trades you are {result} "
             f"with a {kpis['win_rate']:.0f}% win rate."]
    if ratio and breakeven:
        parts.append(f"Wins average {kpis['avg_win']:+.2f} against losses of "
                     f"{kpis['avg_loss']:+.2f}, so this reward-to-risk needs about "
                     f"{breakeven:.0f}% to break even.")
    if len(by_side) > 1:
        if best_side["net"] > 0:
            parts.append(f"{best_side['k']} trades made {money(best_side['net'])} while "
                         f"{worst_side['k']} trades lost {abs(worst_side['net']):.2f}{cur}.")
        elif worst_side["net"] < 0:
            parts.append(f"Both directions lost money, led by {worst_side['k']} "
                         f"({money(worst_side['net'])}).")
    if best_w and worst_w:
        best_loc = f" ({best_w['local']} your time)" if best_w["local"] else ""
        worst_loc = f" ({worst_w['local']} your time)" if worst_w["local"] else ""
        parts.append(f"Your best window is {best_w['server']} server time{best_loc} at "
                     f"{money(best_w['net'])}; losses cluster in {worst_w['server']} server "
                     f"time{worst_loc} at {money(worst_w['net'])}.")
    summary = " ".join(parts)

    avoid = []
    if len(by_side) > 1 and worst_side["net"] < 0:
        avoid.append(f"{worst_side['k']} trades: {money(worst_side['net'])} over "
                     f"{worst_side['trades']} trades ({worst_side['win']:.0f}% win).")
    if worst_sym and worst_sym["net"] < 0:
        avoid.append(f"{worst_sym['k']}: {money(worst_sym['net'])} over "
                     f"{worst_sym['trades']} trades ({worst_sym['win']:.0f}% win).")
    if worst_day and worst_day["net"] < 0:
        avoid.append(f"{DOW_NAMES[int(worst_day['k']) % 7]}: {money(worst_day['net'])} over "
                     f"{worst_day['trades']} trades.")
    if worst_w and worst_w["net"] < 0:
        loc = f" / {worst_w['local']} your time" if worst_w["local"] else ""
        avoid.append(f"{worst_w['server']} server time{loc}: {money(worst_w['net'])} over "
                     f"{worst_w['trades']} trades ({worst_w['win']:.0f}% win).")
    if kpis["revenge_trades"] and kpis["revenge_net"] < 0:
        avoid.append(f"Revenge entries within {REVENGE_MIN} min of a loss: "
                     f"{money(kpis['revenge_net'])} over {kpis['revenge_trades']} trades.")
    if not avoid:
        avoid.append("No symbol, weekday or window is clearly toxic in this sample.")

    mistakes = []
    if ratio and breakeven and kpis["win_rate"] < breakeven:
        mistakes.append(f"Reward-to-risk is {ratio:.2f}; you need {breakeven:.0f}% wins "
                        f"to break even but win {kpis['win_rate']:.0f}%.")
    if kpis["revenge_trades"] and kpis["revenge_net"] < 0:
        mistakes.append(f"You re-entered within {REVENGE_MIN} min of a loss "
                        f"{kpis['revenge_trades']} times for {money(kpis['revenge_net'])}.")
    daily = t.groupby(t.close_time.dt.date).agg(net=("net", "sum"), trades=("net", "size"))
    if len(daily) >= 8:
        cut = daily.trades.quantile(0.75)
        busy = daily[daily.trades >= cut]
        calm = daily[daily.trades < cut]
        if len(busy) and len(calm) and busy.net.mean() < calm.net.mean():
            mistakes.append(f"On your busiest days you average {busy.net.mean():+.2f}/day "
                            f"versus {calm.net.mean():+.2f} on calmer days: you may be overtrading.")
    vol = t.volume
    if len(vol) and vol.mean() > 0 and vol.std() / vol.mean() > 0.75:
        mistakes.append("Lot size varies a lot between trades, which makes risk and results "
                        "hard to judge.")
    if kpis.get("overtrader_trades"):
        trades = kpis["overtrader_trades"]
        groups = kpis["overtrader_groups"]
        net = kpis["overtrader_net"] or 0
        mistakes.append(f"Overtrading clusters: {groups} same-day cluster{'s' if groups != 1 else ''} where one symbol was traded the same direction 3+ times; {trades} trades netted {money(net)}. Do not press the same idea without a fresh reason.")
    if kpis["max_loss_streak"] >= 5:
        mistakes.append(f"You had a {kpis['max_loss_streak']}-trade losing streak. "
                        "A hard stop after 3 losses would cap the damage.")
    if len(wins := t[t.net > 0]) and len(losses := t[t.net <= 0]) and wins.hold_min.mean() > 0:
        a, b = wins.hold_min.mean(), losses.hold_min.mean()
        if b > a * 1.5:
            mistakes.append(f"You hold losers {b:.0f} min versus winners {a:.0f} min: "
                            "you are hoping instead of cutting.")
    gp = t[t.net > 0].net.sum()
    if gp > 0 and t.cost.sum() < 0 and abs(t.cost.sum()) / gp > 0.1:
        mistakes.append(f"Costs eat {abs(t.cost.sum()) / gp * 100:.0f}% of gross profit.")
    if not mistakes:
        mistakes.append("No obvious behavioural leak in this sample; keep the process consistent.")

    edge = []
    if len(by_side) > 1 and best_side["net"] > 0:
        edge.append(f"{best_side['k']} trades: {money(best_side['net'])} over "
                    f"{best_side['trades']} trades ({best_side['win']:.0f}% win).")
    if best_sym and best_sym["net"] > 0:
        edge.append(f"{best_sym['k']}: {money(best_sym['net'])} over "
                    f"{best_sym['trades']} trades ({best_sym['win']:.0f}% win).")
    if best_day and best_day["net"] > 0:
        edge.append(f"{DOW_NAMES[int(best_day['k']) % 7]}: {money(best_day['net'])} over "
                    f"{best_day['trades']} trades.")
    if best_w and best_w["net"] > 0:
        loc = f" / {best_w['local']} your time" if best_w["local"] else ""
        edge.append(f"{best_w['server']} server time{loc}: {money(best_w['net'])} over "
                    f"{best_w['trades']} trades ({best_w['win']:.0f}% win).")
    if not edge:
        edge.append("No repeatable profitable pattern stands out yet. Trade smaller while "
                    "you gather more data.")

    return dict(summary=summary, avoid=avoid, mistakes=mistakes, edge=edge,
                best_window=best_w, worst_window=worst_w)


def build_trades(deals):
    """Pair entry/exit deals by position_id into full trades."""
    if not deals:
        return pd.DataFrame(), 0
    df = pd.DataFrame([d._asdict() for d in deals])
    df = df[df["type"].isin([0, 1])].copy()  # 0=BUY, 1=SELL (drops deposits/credits)
    df["net"] = df.profit + df.commission + df.swap + df.fee
    df["dt"] = pd.to_datetime(df.time, unit="s")  # broker SERVER time
    rows, skipped = [], 0
    for pid, g in df.groupby("position_id"):
        ins, outs = g[g.entry == 0], g[g.entry.isin([1, 2, 3])]
        if ins.empty or outs.empty:  # still open, or opened before the period
            skipped += 1
            continue
        first = ins.sort_values("time").iloc[0]
        rows.append(dict(
            position_id=int(pid), symbol=first.symbol,
            side="Long" if first.type == 0 else "Short",
            volume=float(ins.volume.sum()),
            open_time=ins.dt.min(), close_time=outs.dt.max(),
            net=float(g.net.sum()), gross=float(g.profit.sum()),
            cost=float((g.commission + g.swap + g.fee).sum())))
    return pd.DataFrame(rows), skipped


# ------------------------------------------------------------------ analysis
def clean(x):
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) or math.isinf(x) else round(x, 2)


def grp(df, key):
    g = df.groupby(key).agg(trades=("net", "size"), net=("net", "sum"),
                            win=("net", lambda s: (s > 0).mean() * 100))
    out = []
    for k, r in g.iterrows():
        k = k.item() if hasattr(k, "item") else k
        out.append({"k": k, "trades": int(r.trades), "net": clean(r.net), "win": clean(r.win)})
    return out


def analyze(t, server_offset=None, local_offset=None, currency=""):
    t = t.sort_values("close_time").reset_index(drop=True)
    t["hold_min"] = (t.close_time - t.open_time).dt.total_seconds() / 60
    t["cum"] = t.net.cumsum()
    dd = t.cum - t.cum.cummax().clip(lower=0)

    wins, losses = t[t.net > 0], t[t.net <= 0]
    gp, gl = wins.net.sum(), abs(losses.net.sum())
    avg_win = wins.net.mean() if len(wins) else 0
    avg_loss = losses.net.mean() if len(losses) else 0

    mx_l = mx_w = cl = cw = 0
    for v in t.net:
        if v > 0:
            cw += 1; cl = 0; mx_w = max(mx_w, cw)
        else:
            cl += 1; cw = 0; mx_l = max(mx_l, cl)

    # revenge trades: opened soon after a losing close
    closes = t.close_time.values
    rev = []
    for i, r in t.sort_values("open_time").iterrows():
        j = closes.searchsorted(r.open_time.to_datetime64(), side="right") - 1
        if j >= 0 and t.net[j] < 0 and (r.open_time - t.close_time[j]) <= timedelta(minutes=REVENGE_MIN):
            rev.append(i)
    rev_df = t.loc[rev]

    t["dow"] = t.open_time.dt.day_of_week
    t["hour"] = t.open_time.dt.hour
    daily = t.groupby(t.close_time.dt.date).agg(net=("net", "sum"), trades=("net", "size"))

    trade_day = t.close_time.dt.date.astype(str)
    setup_key = trade_day + "|" + t.symbol.fillna("").astype(str) + "|" + t.side.fillna("").astype(str)
    setup_counts = setup_key.map(setup_key.value_counts())
    t["setup_trades"] = setup_counts.astype(int)
    t["overtrade"] = t.setup_trades > 2
    overtrade_mask = t.overtrade
    overtrader_trades = int(overtrade_mask.sum())
    overtrader_groups = int(setup_key[overtrade_mask].nunique())
    overtrader_net = clean(t.loc[overtrade_mask, "net"].sum())

    # ---- plain-language insights
    ins = []
    rr = abs(avg_win / avg_loss) if avg_loss else None
    if rr is not None:
        ins.append(f"Average win is {avg_win:.2f} vs average loss {avg_loss:.2f} "
                   f"(reward:risk {rr:.2f}). You need a win rate above {100 / (1 + rr):.0f}% to break even at this ratio; yours is {len(wins) / len(t) * 100:.0f}%.")
    if len(wins) and len(losses) and wins.hold_min.mean() > 0:
        a, b = wins.hold_min.mean(), losses.hold_min.mean()
        if b > a * 1.5:
            ins.append(f"You hold losers ({b:.0f} min) much longer than winners ({a:.0f} min): a classic sign of hoping instead of cutting.")
        elif a > b * 1.5:
            ins.append(f"Winners ({a:.0f} min) are held longer than losers ({b:.0f} min). Good discipline.")
    if len(rev_df):
        ins.append(f"{len(rev_df)} trades were opened within {REVENGE_MIN} min of a losing trade; they netted {rev_df.net.sum():+.2f}.")
    if overtrader_trades:
        ins.append(f"{overtrader_groups} same-day symbol-direction cluster{'s' if overtrader_groups != 1 else ''} (3+ trades) detected; "
                   f"{overtrader_trades} trades netted {overtrader_net:+.2f}.")
    if gp > 0 and t.cost.sum() < 0:
        ins.append(f"Commission + swap + fees cost {abs(t.cost.sum()):.2f}, i.e. {abs(t.cost.sum()) / gp * 100:.0f}% of gross profit.")
    sym = t.groupby("symbol").net.sum().sort_values()
    if len(sym) > 1:
        ins.append(f"Best symbol: {sym.index[-1]} ({sym.iloc[-1]:+.2f}). Worst symbol: {sym.index[0]} ({sym.iloc[0]:+.2f}).")
    if len(daily) >= 8:
        busy = daily[daily.trades >= daily.trades.quantile(0.75)]
        calm = daily[daily.trades < daily.trades.quantile(0.75)]
        if len(calm) and busy.net.mean() < calm.net.mean():
            ins.append(f"On your busiest days (≥{busy.trades.min()} trades) you average {busy.net.mean():.2f}/day vs {calm.net.mean():.2f} on calmer days: possible overtrading.")
    vol = t.volume
    if vol.mean() > 0 and vol.std() / vol.mean() > 0.75:
        ins.append("Lot size varies a lot between trades. Inconsistent position sizing makes results hard to evaluate.")
    if mx_l >= 5:
        ins.append(f"Longest losing streak: {mx_l}. Consider a daily stop rule after 3 losses in a row.")

    kpis = dict(
        trades=len(t), net=clean(t.net.sum()), gross=clean(t.gross.sum()), costs=clean(t.cost.sum()),
        win_rate=clean(len(wins) / len(t) * 100), profit_factor=clean(gp / gl) if gl else None,
        expectancy=clean(t.net.mean()), avg_win=clean(avg_win), avg_loss=clean(avg_loss),
        best=clean(t.net.max()), worst=clean(t.net.min()), max_dd=clean(dd.min()),
        max_loss_streak=mx_l, max_win_streak=mx_w,
        avg_hold_win=clean(wins.hold_min.mean()) if len(wins) else None,
        avg_hold_loss=clean(losses.hold_min.mean()) if len(losses) else None,
        revenge_trades=len(rev_df), revenge_net=clean(rev_df.net.sum()) if len(rev_df) else 0,
        overtrader_groups=overtrader_groups, overtrader_trades=overtrader_trades,
        overtrader_net=overtrader_net)

    by_symbol = sorted(grp(t, "symbol"), key=lambda d: d["net"])
    by_dow = grp(t, "dow")
    by_hour = grp(t, "hour")
    by_side = grp(t, "side")
    coach = coach_report(t, kpis, by_symbol, by_dow, by_hour, by_side,
                         server_offset, local_offset, currency)

    last = t.tail(100).iloc[::-1]
    return dict(
        kpis=kpis, insights=ins, coach=coach,
        equity=dict(x=t.close_time.dt.strftime("%Y-%m-%d %H:%M").tolist(),
                    y=[clean(v) for v in t.cum], dd=[clean(v) for v in dd]),
        by_symbol=by_symbol, by_dow=by_dow, by_hour=by_hour, by_side=by_side,
        daily=dict(x=[str(d) for d in daily.index], y=[clean(v) for v in daily.net]),
        table=[dict(open=r.open_time.strftime("%Y-%m-%d %H:%M"), close=r.close_time.strftime("%Y-%m-%d %H:%M"),
                    symbol=r.symbol, side=r.side, volume=r.volume, net=clean(r.net),
                    hold=clean(r.hold_min), setup=("Bullish" if r.side == "Long" else "Bearish"),
                    setup_trades=int(r.setup_trades), overtrade=bool(r.overtrade)) for r in last.itertuples()])


# -------------------------------------------------------------------- routes
@app.post("/api/analyze")
def api_analyze():
    p = request.get_json(silent=True) or {}
    try:
        connect(p.get("login"), p.get("password"), p.get("server"))
        acc = mt5.account_info()

        from_s = str(p.get("from") or "").strip()
        to_s = str(p.get("to") or "").strip()
        range_from = range_to = None
        if from_s and to_s:
            try:
                range_from = datetime.strptime(from_s, "%Y-%m-%d")
                range_to = datetime.strptime(to_s, "%Y-%m-%d")
            except ValueError:
                return jsonify(error="That date range could not be read. Pick both dates again."), 200
            if range_to < range_from:
                return jsonify(error="The end date must be on or after the start date."), 200
            deals = fetch_deals_range(range_from, range_to)
        else:
            deals = fetch_deals(int(p.get("days", 90)))

        trades, skipped = build_trades(deals)
        if range_from and not trades.empty:
            keep = ((trades.close_time.dt.date >= range_from.date())
                    & (trades.close_time.dt.date <= range_to.date()))
            trades = trades[keep].reset_index(drop=True)

        if trades.empty:
            return jsonify(error="No closed trades found in this period. Try a longer range."), 200
        symbols = trades.symbol.dropna().unique().tolist()
        server_offset = detect_server_offset(symbols)
        local_offset = datetime.now().astimezone().utcoffset().total_seconds() / 3600
        res = analyze(trades, server_offset=server_offset, local_offset=local_offset,
                      currency=(acc.currency if acc else ""))
        res["tz"] = dict(server_offset=server_offset, local_offset=round(local_offset, 2),
                         server_label=offset_label(server_offset),
                         local_label=offset_label(local_offset),
                         detected=server_offset is not None)
        res["account"] = dict(login=acc.login, server=acc.server, balance=acc.balance,
                              equity=acc.equity, currency=acc.currency, name=acc.name) if acc else {}
        res["skipped"] = skipped
        return jsonify(res)
    except Exception as e:
        return jsonify(error=str(e)), 200
    finally:
        mt5.shutdown()  # disconnects Python only; your terminal stays open


@app.get("/")
def index():
    html = (BASE_DIR / "dashboard.html").read_text(encoding="utf-8")
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
