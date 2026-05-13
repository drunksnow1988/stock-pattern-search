#!/usr/bin/env python3
"""
A股形态搜索后端
- 股票列表：内置 stocks_list.json（无需外部接口）
- 历史数据：Yahoo Finance（全球可访问）
"""

import os, pickle, threading, json
import numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "stock_cache.pkl")
LIST_FILE  = os.path.join(BASE_DIR, "stocks_list.json")
N_DAYS     = 60

state = {"status": "idle", "progress": 0, "total": 0,
         "message": "等待启动", "last_updated": None}
state_lock   = threading.Lock()
stock_list   = []
stock_matrix = None

# ──────────────── 数值工具 ────────────────

def z_norm(arr):
    a = np.asarray(arr, dtype=float)
    s = a.std()
    return (a - a.mean()) / (s if s > 1e-8 else 1.0)

def resample(arr, n):
    a = np.asarray(arr, dtype=float)
    return np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(a)), a)

def dtw_distance(s1, s2):
    n, band = len(s1), max(3, len(s1) // 10)
    dp = [[float("inf")] * (n + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(1, n + 1):
        for j in range(max(1, i - band), min(n + 1, i + band + 1)):
            cost = (s1[i-1] - s2[j-1]) ** 2
            dp[i][j] = cost + min(dp[i-1][j], dp[i][j-1], dp[i-1][j-1])
    return dp[n][n] ** 0.5

def set_state(**kw):
    with state_lock:
        state.update(kw)

# ──────────────── 数据加载（Yahoo Finance） ────────────────

def fetch_history_yf(code):
    """用 Yahoo Finance 获取前复权日线收盘价，全球均可访问。"""
    try:
        import yfinance as yf
        suffix = ".SS" if code.startswith("6") else ".SZ"
        ticker = yf.Ticker(code + suffix)
        df = ticker.history(period="4mo", interval="1d", auto_adjust=True)
        if df is None or len(df) < N_DAYS:
            return None, None
        prices = df["Close"].tolist()[-N_DAYS:]
        dates  = [str(d)[:10] for d in df.index.tolist()[-N_DAYS:]]
        return prices, dates
    except Exception:
        return None, None


def build_cache():
    global stock_list, stock_matrix
    set_state(status="loading", progress=0, message="读取股票列表…")
    try:
        with open(LIST_FILE, encoding="utf-8") as f:
            all_stocks = json.load(f)

        set_state(total=len(all_stocks),
                  message=f"开始下载 {len(all_stocks)} 只股票历史数据…")

        done = [0]
        lock = threading.Lock()
        raw  = []

        def worker(info):
            prices, dates = fetch_history_yf(info["code"])
            with lock:
                done[0] += 1
                if done[0] % 300 == 0:
                    set_state(progress=done[0],
                              message=f"已下载 {done[0]}/{len(all_stocks)} 只")
            if prices:
                return {"code": info["code"], "name": info["name"],
                        "prices": prices, "dates": dates}
            return None

        # Yahoo Finance 限速较严，并发不宜过高
        with ThreadPoolExecutor(max_workers=8) as ex:
            for res in ex.map(worker, all_stocks):
                if res:
                    raw.append(res)

        stock_list   = raw
        mat = [z_norm(resample(s["prices"], N_DAYS)) for s in stock_list]
        stock_matrix = np.array(mat, dtype=float)

        with open(CACHE_FILE, "wb") as f:
            pickle.dump({"stock_list": stock_list, "stock_matrix": stock_matrix}, f)

        set_state(status="ready", progress=len(stock_list), total=len(stock_list),
                  message=f"就绪，共 {len(stock_list)} 只股票",
                  last_updated=datetime.now().strftime("%Y-%m-%d %H:%M"))
    except Exception as e:
        import traceback; traceback.print_exc()
        set_state(status="error", message=str(e))


def load_cache():
    global stock_list, stock_matrix
    try:
        with open(CACHE_FILE, "rb") as f:
            data = pickle.load(f)
        stock_list   = data["stock_list"]
        stock_matrix = data["stock_matrix"]
        n = len(stock_list)
        set_state(status="ready", progress=n, total=n,
                  message=f"缓存就绪，共 {n} 只股票")
        return True
    except Exception:
        return False

# ──────────────── 路由 ────────────────

@app.route("/")
def index():
    return Response(open(os.path.join(BASE_DIR, "index.html"), encoding="utf-8").read(),
                    mimetype="text/html")

@app.route("/api/status")
def api_status():
    with state_lock:
        return jsonify(dict(state))

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    if state["status"] == "loading":
        return jsonify({"error": "正在加载中，请稍候"}), 400
    threading.Thread(target=build_cache, daemon=True).start()
    return jsonify({"message": "已开始刷新数据"})

@app.route("/api/search", methods=["POST"])
def api_search():
    if state["status"] != "ready":
        return jsonify({"error": f"数据未就绪（{state['message']}）"}), 503

    body   = request.get_json(force=True)
    points = body.get("points", [])
    top_k  = min(int(body.get("topK", 20)), 50)

    if len(points) < 6:
        return jsonify({"error": "曲线太短，请多画一些"}), 400

    y_raw       = np.array([1.0 - p["y"] for p in points], dtype=float)
    query       = z_norm(resample(y_raw, N_DAYS))
    mat         = stock_matrix
    pearson     = (mat @ query) / N_DAYS
    euclid      = np.sqrt(((mat - query) ** 2).mean(axis=1))
    pre_score   = 0.6 * pearson - 0.4 * euclid / (euclid.max() + 1e-8)
    cand_idx    = np.argsort(pre_score)[-min(100, len(stock_list)):][::-1]
    dtw_vals    = [(int(i), dtw_distance(query.tolist(), mat[i].tolist()))
                   for i in cand_idx]
    max_dtw     = max(d for _, d in dtw_vals) + 1e-8

    results = []
    for idx, dtw in dtw_vals:
        corr  = float(pearson[idx])
        score = 0.5 * corr + 0.5 * (1.0 - dtw / max_dtw)
        s = stock_list[idx]
        results.append({"code": s["code"], "name": s["name"],
                        "score": round(score, 4), "pearson": round(corr, 4),
                        "prices": s["prices"], "dates": s["dates"]})

    results.sort(key=lambda x: -x["score"])
    return jsonify({"results": results[:top_k], "total": len(stock_list)})

# ──────────────── 启动（gunicorn 和直接运行均触发） ────────────────

def _init():
    if load_cache():
        print(f"✅ 缓存已加载 {len(stock_list)} 只股票")
    else:
        print("⚠️  开始后台下载（约 10-20 分钟）…")
        threading.Thread(target=build_cache, daemon=True).start()

_init()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"🚀  http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
