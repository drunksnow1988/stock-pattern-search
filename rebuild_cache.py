#!/usr/bin/env python3
"""
每日重建股票缓存。
用法：python rebuild_cache.py
建议在收盘后（如每天 16:30）通过 cron 执行。
"""
import json, os, pickle, sys, time
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
LIST_FILE  = os.path.join(BASE_DIR, "stocks_list.json")
CACHE_FILE = os.path.join(BASE_DIR, "stock_cache.pkl")
N_DAYS     = 60
MAX_WORKERS = 20  # 并发线程数，避免被限速


def z_norm(arr):
    a = np.asarray(arr, dtype=float)
    s = a.std()
    return (a - a.mean()) / (s if s > 1e-8 else 1.0)


def fetch_one(stock):
    """获取单只股票近 N_DAYS 个交易日的前复权收盘价。"""
    code = stock["code"]
    try:
        import yfinance as yf
        suffix = ".SS" if code.startswith("6") else ".SZ"
        df = yf.Ticker(code + suffix).history(period="4mo", interval="1d", auto_adjust=True)
        if df is None or len(df) < N_DAYS:
            return None
        prices = df["Close"].tolist()[-N_DAYS:]
        dates  = [str(d)[:10] for d in df.index.tolist()[-N_DAYS:]]
        return {
            "code":   code,
            "name":   stock["name"],
            "prices": prices,
            "dates":  dates,
            "znorm":  z_norm(prices).tolist(),
        }
    except Exception:
        return None


def main():
    with open(LIST_FILE, encoding="utf-8") as f:
        all_stocks = json.load(f)

    total = len(all_stocks)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 开始重建缓存，共 {total} 只股票")

    results, done, failed = [], 0, 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_one, s): s for s in all_stocks}
        for fut in as_completed(futures):
            done += 1
            data = fut.result()
            if data:
                results.append(data)
            else:
                failed += 1
            if done % 200 == 0 or done == total:
                print(f"  进度 {done}/{total}，成功 {len(results)}，失败 {failed}", flush=True)

    if len(results) < 100:
        print(f"✗ 成功数据太少（{len(results)}），放弃写入缓存", file=sys.stderr)
        sys.exit(1)

    stock_list   = [{"code": r["code"], "name": r["name"],
                     "prices": r["prices"], "dates": r["dates"]} for r in results]
    stock_matrix = np.array([r["znorm"] for r in results], dtype=np.float32)

    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"stock_list": stock_list, "stock_matrix": stock_matrix}, f)
    os.replace(tmp, CACHE_FILE)  # 原子替换，不中断正在运行的服务

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ✓ 缓存已更新：{len(results)} 只股票 → {CACHE_FILE}")


if __name__ == "__main__":
    main()
