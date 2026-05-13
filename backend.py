#!/usr/bin/env python3
"""A股形态搜索后端 —— 完全基于新浪财经直接 HTTP 请求，不依赖 mini-racer"""

import os
# 清除系统代理环境变量（代理断开时会导致所有请求失败）
os.environ.pop("HTTP_PROXY",  None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy",  None)
os.environ.pop("https_proxy", None)
os.environ["NO_PROXY"] = "*"

import pickle, threading, json
import numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

# patch requests：强制忽略代理（必须在任何 requests 使用前执行）
import requests as _rq
_orig_send = _rq.adapters.HTTPAdapter.send
def _no_proxy_send(self, req, **kw):
    kw["proxies"] = {}
    return _orig_send(self, req, **kw)
_rq.adapters.HTTPAdapter.send = _no_proxy_send

app = Flask(__name__)
CORS(app)

CACHE_FILE = os.path.join(os.path.dirname(__file__), "stock_cache.pkl")
N_DAYS = 60

state = {"status": "idle", "progress": 0, "total": 0,
         "message": "等待启动", "last_updated": None}
state_lock = threading.Lock()

stock_list   = []
stock_matrix = None

SESSION = _rq.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn",
})

# ──────────────────── 数值工具 ────────────────────

def z_norm(arr):
    a = np.asarray(arr, dtype=float)
    s = a.std()
    return (a - a.mean()) / (s if s > 1e-8 else 1.0)

def resample(arr, n):
    a = np.asarray(arr, dtype=float)
    return np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(a)), a)

def dtw_distance(s1, s2):
    n = len(s1)
    band = max(3, n // 10)
    INF = float("inf")
    dp = [[INF] * (n + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(1, n + 1):
        for j in range(max(1, i - band), min(n + 1, i + band + 1)):
            cost = (s1[i-1] - s2[j-1]) ** 2
            dp[i][j] = cost + min(dp[i-1][j], dp[i][j-1], dp[i-1][j-1])
    return dp[n][n] ** 0.5

def set_state(**kw):
    with state_lock:
        state.update(kw)

# ──────────────────── 新浪财经直接接口 ────────────────────

def fetch_stock_list_sina():
    """从新浪获取全量 A 股代码列表（不使用 akshare，避免 mini-racer 子线程崩溃）。"""
    stocks, seen = [], set()
    for node in ("hs_a", "sh_a", "sz_a"):
        page = 1
        while True:
            try:
                r = SESSION.get(
                    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
                    "/Market_Center.getHQNodeDataSimple",
                    params={"num": 100, "page": page, "sort": "symbol",
                            "asc": 1, "node": node},
                    timeout=15, proxies={}
                )
                data = r.json()
                if not data:
                    break
                for s in data:
                    sym = s.get("symbol", "")
                    if sym not in seen:
                        seen.add(sym)
                        # symbol = sh600519 / sz000001，提取纯数字 code
                        code = s.get("code", sym[2:])
                        stocks.append({"code": str(code).zfill(6),
                                       "name": s.get("name", code)})
                if len(data) < 100:
                    break
                page += 1
            except Exception:
                break
    return stocks


def fetch_history_tencent(code):
    """
    腾讯财经前复权日线接口，格式：[date, open, close, high, low, volume]
    返回 (prices, dates) 或 (None, None)。
    """
    prefix = "sh" if code.startswith("6") else "sz"
    symbol = prefix + code
    try:
        r = SESSION.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"_var": "kline_dayfq",
                    "param": f"{symbol},day,,,{N_DAYS + 10},qfq"},
            timeout=15, proxies={}
        )
        import json as _json
        text = r.text
        # 响应格式: kline_dayfq={...}
        raw = _json.loads(text[text.index("=") + 1:])
        klines = (raw.get("data", {}).get(symbol, {}).get("qfqday")
                  or raw.get("data", {}).get(symbol, {}).get("day"))
        if not klines or len(klines) < 10:
            return None, None
        prices = [float(k[2]) for k in klines]   # index 2 = close
        dates  = [k[0][:10] for k in klines]
        return prices[-N_DAYS:], dates[-N_DAYS:]
    except Exception:
        return None, None

# ──────────────────── 缓存构建 ────────────────────

def build_cache():
    global stock_list, stock_matrix
    set_state(status="loading", progress=0, total=0, message="获取股票列表…")
    try:
        codes_info = fetch_stock_list_sina()
        if not codes_info:
            set_state(status="error", message="无法获取股票列表")
            return

        set_state(total=len(codes_info),
                  message=f"开始下载 {len(codes_info)} 只股票历史数据…")

        done  = [0]
        lock  = threading.Lock()
        raw   = []

        def worker(info):
            code = info["code"]
            prices, dates = fetch_history_tencent(code)
            with lock:
                done[0] += 1
                if done[0] % 300 == 0:
                    set_state(progress=done[0],
                              message=f"已下载 {done[0]}/{len(codes_info)} 只")
            if prices:
                return {"code": code, "name": info["name"],
                        "prices": prices, "dates": dates}
            return None

        with ThreadPoolExecutor(max_workers=30) as ex:
            for res in ex.map(worker, codes_info):
                if res:
                    raw.append(res)

        stock_list   = raw
        mat = [z_norm(resample(s["prices"], N_DAYS)) for s in stock_list]
        stock_matrix = np.array(mat, dtype=float)

        with open(CACHE_FILE, "wb") as f:
            pickle.dump({"stock_list": stock_list, "stock_matrix": stock_matrix}, f)

        set_state(
            status="ready", progress=len(stock_list), total=len(stock_list),
            message=f"就绪，共 {len(stock_list)} 只股票",
            last_updated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
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

# ──────────────────── 路由 ────────────────────

@app.route("/")
def index():
    path = os.path.join(os.path.dirname(__file__), "index.html")
    return Response(open(path, encoding="utf-8").read(), mimetype="text/html")

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

    y_raw = np.array([1.0 - p["y"] for p in points], dtype=float)
    query = z_norm(resample(y_raw, N_DAYS))

    mat         = stock_matrix
    pearson     = (mat @ query) / N_DAYS
    euclid      = np.sqrt(((mat - query) ** 2).mean(axis=1))
    euclid_norm = euclid / (euclid.max() + 1e-8)
    pre_score   = 0.6 * pearson - 0.4 * euclid_norm
    n_cand      = min(100, len(stock_list))
    cand_idx    = np.argsort(pre_score)[-n_cand:][::-1]

    dtw_vals = [(int(i), dtw_distance(query.tolist(), mat[i].tolist()))
                for i in cand_idx]
    max_dtw  = max(d for _, d in dtw_vals) + 1e-8

    results = []
    for (idx, dtw) in dtw_vals:
        corr  = float(pearson[idx])
        score = 0.5 * corr + 0.5 * (1.0 - dtw / max_dtw)
        s = stock_list[idx]
        results.append({"code": s["code"], "name": s["name"],
                        "score": round(score, 4), "pearson": round(corr, 4),
                        "prices": s["prices"], "dates": s["dates"]})

    results.sort(key=lambda x: -x["score"])
    return jsonify({"results": results[:top_k], "total": len(stock_list)})

# ──────────────────── 启动 ────────────────────

if __name__ == "__main__":
    if load_cache():
        print(f"✅ 已从缓存加载 {len(stock_list)} 只股票")
    else:
        print("⚠️  未找到缓存，开始后台下载（约 5-10 分钟）…")
        threading.Thread(target=build_cache, daemon=True).start()
    print("🚀  http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False)
