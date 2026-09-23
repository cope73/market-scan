#!/usr/bin/env python3
"""Equities trading experiment harness.

Commands:
  verify                 Re-verify every universe ticker (listed, price, market cap, dollar volume) against its screen.
  scan                   Morning scan: leader moves for H1, prior-session moves for every universe, H11 earnings window.
  snapshot               Save closing prices for every ticker plus references to data/prices.csv (append, dedup).
  control SLEEVE START END
                         Equal-weight untimed control-basket return for SLEEVE between two dates (close to close).
  score                  Compute SPY and control returns for every closed trade in data/journal.csv and print the sleeve scorecard.
  intraday               Midday snapshot: last price and move vs prior close for every tracked ticker (out/intraday.txt, data/intraday.csv).
  calendar               Refresh the H11 earnings calendar cache (data/earnings.csv) for the S&P 400 pool. Slow; run nightly.

All output is plain text or CSV so a Claude session can read it and write the brief and the Notion records.
"""
import json, os, sys, time, math, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
U = json.load(open(os.path.join(HERE, "universes.json")))
SLEEVES = ["H1", "H2", "H3"]


def all_tickers():
    t = set(U["references"])
    for s in SLEEVES:
        t.update(U[s]["tickers"])
    t.update(U["H1"]["leaders"])
    cal = os.path.join(DATA, "earnings.csv")
    if os.path.exists(cal):
        e = pd.read_csv(cal, parse_dates=["earnings_date"])
        today = pd.Timestamp(dt.date.today())
        win = e[(e.earnings_date >= today - pd.Timedelta(days=15)) & (e.earnings_date <= today + pd.Timedelta(days=10))]
        t.update(win.ticker.tolist())
    return sorted(t)


def history(tickers, period="3mo"):
    df = yf.download(tickers, period=period, interval="1d", auto_adjust=True, progress=False, threads=True, group_by="column")
    close = df["Close"] if "Close" in df else df
    vol = df["Volume"] if "Volume" in df else None
    if isinstance(close, pd.Series):
        close = close.to_frame(tickers[0])
        vol = vol.to_frame(tickers[0]) if vol is not None else None
    return close.dropna(how="all"), vol


def fast_caps(tickers):
    def one(t):
        try:
            fi = yf.Ticker(t).fast_info
            return t, float(fi.get("marketCap") or float("nan")), float(fi.get("lastPrice") or float("nan"))
        except Exception as e:
            return t, float("nan"), float("nan")
    with ThreadPoolExecutor(8) as ex:
        return {t: (m, p) for t, m, p in ex.map(one, tickers)}


def cmd_verify():
    rows = []
    for s in SLEEVES:
        tick = U[s]["tickers"]
        close, vol = history(tick, "2mo")
        caps = fast_caps(tick)
        scr = U[s]["screen"]
        for t in tick:
            flags = []
            if t in U["exclude"]:
                flags.append("EXCLUDED")
            if t not in close.columns or close[t].dropna().empty:
                rows.append((s, t, "", "", "", "NO DATA"))
                continue
            px = float(close[t].dropna().iloc[-1])
            dv = float((close[t] * vol[t]).dropna().tail(20).mean()) if vol is not None else float("nan")
            mc = caps.get(t, (float("nan"), 0))[0]
            last_day = close[t].dropna().index[-1].date()
            if (dt.date.today() - last_day).days > 5:
                flags.append("STALE")
            if "mcap_min" in scr and mc == mc and mc < scr["mcap_min"]: flags.append("MCAP<MIN")
            if "mcap_max" in scr and mc == mc and mc > scr["mcap_max"]: flags.append("MCAP>MAX")
            if "price_min" in scr and px < scr["price_min"]: flags.append("PRICE<MIN")
            if "dollar_vol_min" in scr and dv == dv and dv < scr["dollar_vol_min"]: flags.append("DOLLARVOL<MIN")
            if "dollar_vol_max" in scr and dv == dv and dv > scr["dollar_vol_max"]: flags.append("DOLLARVOL>MAX")
            rows.append((s, t, f"{px:.2f}", f"{mc/1e9:.1f}B" if mc == mc else "?", f"{dv/1e6:.0f}M" if dv == dv else "?", ",".join(flags) or "OK"))
    out = pd.DataFrame(rows, columns=["sleeve", "ticker", "price", "mcap", "avg_dollar_vol_20d", "flags"])
    out.to_csv(os.path.join(DATA, "verify_latest.csv"), index=False)
    print(out.to_string(index=False))
    bad = out[out["flags"] != "OK"]
    print(f"\n{len(out)} names checked, {len(bad)} flagged.")


def cmd_snapshot():
    tick = all_tickers()
    close, _ = history(tick, "1mo")
    long = close.reset_index().melt(id_vars=close.index.name or "Date", var_name="ticker", value_name="close").dropna()
    long.columns = ["date", "ticker", "close"]
    long["date"] = pd.to_datetime(long["date"]).dt.date
    path = os.path.join(DATA, "prices.csv")
    if os.path.exists(path):
        old = pd.read_csv(path, parse_dates=["date"])
        old["date"] = old["date"].dt.date
        long = pd.concat([old, long]).drop_duplicates(["date", "ticker"], keep="last")
    long.sort_values(["date", "ticker"]).to_csv(path, index=False)
    print(f"prices.csv now has {len(long)} rows through {long.date.max()}")


def ret_between(close, t, start, end):
    s = close[t].dropna()
    s = s[(s.index.date >= start) & (s.index.date <= end)]
    if len(s) < 2:
        return float("nan")
    return float(s.iloc[-1] / s.iloc[0] - 1)


def h11_cohort(report_date, days=5):
    e = pd.read_csv(os.path.join(DATA, "earnings.csv"), parse_dates=["earnings_date"])
    rd = pd.Timestamp(report_date)
    return e[(e.earnings_date >= rd - pd.Timedelta(days=days)) & (e.earnings_date <= rd + pd.Timedelta(days=days))].ticker.tolist()


def control_return(sleeve, start, end, close=None, cohort=None):
    tick = cohort if cohort else (U[sleeve]["tickers"] if sleeve in U and "tickers" in U[sleeve] else [])
    if close is None:
        close, _ = history(tick + ["SPY"], "6mo")
    rets = [ret_between(close, t, start, end) for t in tick if t in close.columns]
    rets = [r for r in rets if r == r]
    return sum(rets) / len(rets) if rets else float("nan")


def cmd_control(sleeve, start, end):
    start, end = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    close, _ = history(U[sleeve]["tickers"] + ["SPY"], "6mo")
    print(f"{sleeve} equal-weight control {start}..{end}: {control_return(sleeve, start, end, close):+.2%}")
    print(f"SPY {start}..{end}: {ret_between(close, 'SPY', start, end):+.2%}")


def cmd_scan():
    tick = all_tickers()
    close, vol = history(tick, "1mo")
    last, prev = close.iloc[-1], close.iloc[-2]
    chg = (last / prev - 1).sort_values(ascending=False)
    asof = close.index[-1].date()
    print(f"=== SCAN as of close {asof} ===\n")
    print("Leaders (H1 trigger is a move of %.1f%% or more):" % U["H1"]["leader_move_pct"])
    for t in U["H1"]["leaders"]:
        if t in chg:
            flag = "  <-- TRIGGER" if abs(chg[t]) * 100 >= U["H1"]["leader_move_pct"] else ""
            print(f"  {t:6s} {chg[t]:+.2%}{flag}")
    for s in SLEEVES:
        print(f"\n{s} {U[s]['name']} (prior session move, avg 20d dollar vol):")
        sub = chg[[t for t in U[s]["tickers"] if t in chg]]
        for t, c in sub.items():
            dv = float((close[t] * vol[t]).dropna().tail(20).mean()) / 1e6
            print(f"  {t:6s} {c:+.2%}  {dv:6.0f}M")
        print(f"  equal-weight basket: {sub.mean():+.2%}")
    print("\nReferences:")
    for t in U["references"]:
        if t in chg:
            print(f"  {t:5s} {chg[t]:+.2%}")
    cal = os.path.join(DATA, "earnings.csv")
    if os.path.exists(cal):
        e = pd.read_csv(cal, parse_dates=["earnings_date"])
        today = pd.Timestamp(dt.date.today())
        win = e[(e.earnings_date >= today) & (e.earnings_date <= today + pd.Timedelta(days=7))].sort_values("earnings_date")
        print(f"\nH11 earnings in the next 7 days ({len(win)} names, from cache dated {dt.date.fromtimestamp(os.path.getmtime(cal))}):")
        print(win.to_string(index=False) if len(win) else "  none")
    else:
        print("\nH11: no earnings cache yet. Run: python3 harness.py calendar")


def cmd_intraday():
    tick = all_tickers()
    close, _ = history(tick, "7d")
    import zoneinfo
    today_et = dt.datetime.now(zoneinfo.ZoneInfo("America/New_York")).date()
    completed = close[[d.date() < today_et for d in close.index]]
    prev = completed.iloc[-1]
    prev_day = completed.index[-1].date()
    intra = yf.download(tick, period="1d", interval="5m", progress=False, threads=True, group_by="column")
    last = intra["Close"].ffill().iloc[-1] if "Close" in intra else intra.ffill().iloc[-1]
    rows = []
    for t in tick:
        px = float(last.get(t, float("nan")))
        if px != px or t not in prev or prev[t] != prev[t]:
            continue
        rows.append(dict(ticker=t, last=round(px, 2), prev_close=round(float(prev[t]), 2), chg=px / float(prev[t]) - 1))
    df = pd.DataFrame(rows).sort_values("chg", ascending=False)
    df["asof_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    df.to_csv(os.path.join(DATA, "intraday.csv"), index=False)
    print(f"=== INTRADAY as of {df.asof_utc.iloc[0]} (prev close {prev_day}) ===\n")
    print("Leaders (H1 midday trigger is a move of %.1f%% or more vs prior close):" % U["H1"]["leader_move_pct"])
    for t in U["H1"]["leaders"]:
        r = df[df.ticker == t]
        if len(r):
            c = float(r.chg.iloc[0]); flag = "  <-- TRIGGER" if abs(c) * 100 >= U["H1"]["leader_move_pct"] else ""
            print(f"  {t:6s} {c:+.2%}  last {float(r.last.iloc[0]):.2f}{flag}")
    for s_ in ["H1", "H3"]:
        print(f"\n{s_} {U[s_]['name']} (move vs prior close, last price):")
        sub = df[df.ticker.isin(U[s_]["tickers"])]
        for _, r in sub.iterrows():
            print(f"  {r.ticker:6s} {r.chg:+.2%}  {r.last:.2f}")
        print(f"  equal-weight basket: {sub.chg.mean():+.2%}")
    print("\nReferences:")
    for t in U["references"]:
        r = df[df.ticker == t]
        if len(r): print(f"  {t:5s} {float(r.chg.iloc[0]):+.2%}")


def cmd_calendar():
    src = U["H11"]["pool_source"]
    pool_path = os.path.join(DATA, f"pool_{src}.csv")
    if not os.path.exists(pool_path):
        url = {"sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
               "sp600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"}[src]
        import urllib.request, io
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) trading-harness/1.0"})
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
        tables = pd.read_html(io.StringIO(html))
        tab = next(t for t in tables if "Symbol" in t.columns)
        pool = tab[["Symbol", "Security"]].rename(columns={"Symbol": "ticker", "Security": "name"})
        pool["ticker"] = pool["ticker"].str.replace(".", "-", regex=False)
        pool.to_csv(pool_path, index=False)
    pool = pd.read_csv(pool_path)
    excl = set(U["exclude"]) | set(U["H1"]["tickers"]) | set(U["H2"]["tickers"]) | set(U["H3"]["tickers"])
    pool = pool[~pool.ticker.isin(excl)]

    def one(t):
        try:
            c = yf.Ticker(t).calendar
            d = c.get("Earnings Date") if isinstance(c, dict) else None
            if d:
                d = d[0] if isinstance(d, (list, tuple)) else d
                return t, pd.Timestamp(d).normalize()
        except Exception:
            pass
        return t, pd.NaT
    t0 = time.time()
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, pool.ticker.tolist()))
    e = pd.DataFrame(res, columns=["ticker", "earnings_date"]).dropna()
    e = e.merge(pool, on="ticker", how="left")
    e.to_csv(os.path.join(DATA, "earnings.csv"), index=False)
    print(f"earnings.csv: {len(e)} dated names of {len(pool)} in pool, {time.time()-t0:.0f}s")


def cmd_score():
    path = os.path.join(DATA, "journal.csv")
    if not os.path.exists(path):
        pd.DataFrame(columns=["trade_id", "ticker", "sleeve", "mode", "entry_date", "entry_price", "exit_date", "exit_price", "status", "rule_followed"]).to_csv(path, index=False)
        print("Created empty data/journal.csv. Mirror closed trades here from Notion (or the reverse) and rerun.")
        return
    j = pd.read_csv(path)
    closed = j[j.status.str.lower() == "closed"].copy()
    if closed.empty:
        print("No closed trades yet."); return
    need = sorted(set(closed.ticker) | {"SPY"} | set(sum([U[s]["tickers"] for s in SLEEVES if s in set(closed.sleeve)], [])))
    close, _ = history(need, "6mo")
    rows = []
    for _, r in closed.iterrows():
        a, b = dt.date.fromisoformat(str(r.entry_date)), dt.date.fromisoformat(str(r.exit_date))
        tr = float(r.exit_price) / float(r.entry_price) - 1
        spy = ret_between(close, "SPY", a, b)
        ctl = control_return(r.sleeve, a, b, close) if r.sleeve in SLEEVES else float("nan")
        rows.append(dict(trade_id=r.trade_id, sleeve=r.sleeve, ticker=r.ticker, mode=r.mode, ret=tr, spy=spy, control=ctl, edge=tr - ctl, rule=r.get("rule_followed", True)))
    out = pd.DataFrame(rows)
    out.to_csv(os.path.join(DATA, "scored_latest.csv"), index=False)
    pd.set_option("display.float_format", lambda x: f"{x:+.2%}")
    print(out.to_string(index=False))
    print("\n=== SLEEVE SCORECARD ===")
    for s, g in out.groupby("sleeve"):
        wins = (g.ret > 0).mean()
        aw = g.ret[g.ret > 0].mean() if (g.ret > 0).any() else 0
        al = g.ret[g.ret <= 0].mean() if (g.ret <= 0).any() else 0
        eq = (1 + g.ret).cumprod()
        dd = float((eq / eq.cummax() - 1).min())
        print(f"{s}: trades={len(g)} hit={wins:.0%} avg_win={aw:+.2%} avg_loss={al:+.2%} cum={eq.iloc[-1]-1:+.2%} "
              f"edge_vs_control={g.edge.mean():+.2%} vs_spy={(g.ret-g.spy).mean():+.2%} max_dd={dd:+.2%} adherence={g.rule.astype(bool).mean():.0%}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a: print(__doc__); sys.exit(0)
    c = a[0]
    if c == "verify": cmd_verify()
    elif c == "scan": cmd_scan()
    elif c == "snapshot": cmd_snapshot()
    elif c == "control": cmd_control(a[1], a[2], a[3])
    elif c == "score": cmd_score()
    elif c == "calendar": cmd_calendar()
    elif c == "intraday": cmd_intraday()
    else: print(__doc__)
