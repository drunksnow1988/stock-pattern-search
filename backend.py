#!/usr/bin/env python3
"""
A股形态搜索后端
- 股票列表：内置 stocks_list.json（无需外部接口）
- 历史数据：Yahoo Finance（全球可访问）
"""

import os, pickle, threading, json, sqlite3, secrets, string, argparse
from functools import wraps
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
DB_FILE    = os.path.join(BASE_DIR, "licenses.db")
N_DAYS     = 60

DISABLE_AUTH = os.environ.get("DISABLE_AUTH", "0") == "1"

state = {"status": "idle", "progress": 0, "total": 0,
         "message": "等待启动", "last_updated": None}
state_lock   = threading.Lock()
stock_list   = []
stock_matrix = None

# ──────────────── 授权系统 ────────────────

def init_db():
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        CREATE TABLE IF NOT EXISTS license_keys (
            key          TEXT PRIMARY KEY,
            expiry_date  TEXT NOT NULL,
            fingerprint  TEXT,
            activated_at TEXT,
            note         TEXT,
            created_at   TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()


def check_license(key: str, fingerprint: str):
    """Returns (ok, message, expiry). Locks fingerprint on first use."""
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM license_keys WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return False, "授权码无效", None
        expiry = row["expiry_date"]
        if datetime.now().strftime("%Y-%m-%d") > expiry:
            return False, f"授权码已于 {expiry} 到期", expiry
        stored_fp = row["fingerprint"]
        if stored_fp is None:
            con.execute(
                "UPDATE license_keys SET fingerprint=?, activated_at=? WHERE key=?",
                (fingerprint, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), key)
            )
            con.commit()
            return True, "激活成功", expiry
        if stored_fp != fingerprint:
            return False, "该授权码已绑定其他设备，如需换绑请联系客服", expiry
        return True, "授权有效", expiry
    finally:
        con.close()


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if DISABLE_AUTH:
            return f(*args, **kwargs)
        key = request.headers.get("X-License-Key", "").strip()
        fp  = request.headers.get("X-Fingerprint", "").strip()
        if not key or not fp:
            return jsonify({"error": "未提供授权信息，请先激活"}), 401
        ok, msg, _ = check_license(key, fp)
        if not ok:
            return jsonify({"error": msg}), 401
        return f(*args, **kwargs)
    return decorated

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


CACHE_URL = ("https://github.com/drunksnow1988/stock-pattern-search"
             "/releases/download/v1.0/stock_cache.pkl.gz")

def build_cache():
    """从 GitHub Release 下载预构建缓存（3MB），无需调用任何股票 API。"""
    global stock_list, stock_matrix
    set_state(status="loading", progress=0, total=100,
              message="正在下载股票数据缓存（约 3MB）…")
    try:
        import gzip
        import requests as _req
        set_state(message="连接 GitHub Release…")

        resp = _req.get(CACHE_URL, timeout=120, stream=True,
                        headers={"Accept-Encoding": "identity"})
        resp.raise_for_status()

        chunks = []
        downloaded = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                chunks.append(chunk)
                downloaded += len(chunk)
                set_state(message=f"下载中… {downloaded//1024} KB")

        set_state(progress=80, message="解压缓存文件…")
        gz_data = b"".join(chunks)
        raw     = gzip.decompress(gz_data)
        data    = pickle.loads(raw)

        stock_list   = data["stock_list"]
        stock_matrix = data["stock_matrix"]

        with open(CACHE_FILE, "wb") as f:
            f.write(raw)

        n = len(stock_list)
        set_state(status="ready", progress=100, total=100,
                  message=f"就绪，共 {n} 只股票",
                  last_updated=datetime.now().strftime("%Y-%m-%d %H:%M"))
        print(f"✅ 缓存下载完成，共 {n} 只股票")
    except Exception as e:
        import traceback; traceback.print_exc()
        set_state(status="error", message=f"缓存下载失败：{e}")


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

@app.route("/api/verify", methods=["POST"])
def api_verify():
    body = request.get_json(force=True) or {}
    key  = body.get("key", "").strip().upper()
    fp   = body.get("fingerprint", "").strip()
    if not key or not fp:
        return jsonify({"ok": False, "message": "参数缺失", "expiry": None}), 400
    ok, msg, expiry = check_license(key, fp)
    return jsonify({"ok": ok, "message": msg, "expiry": expiry})

@app.route("/api/refresh", methods=["POST"])
@require_auth
def api_refresh():
    if state["status"] == "loading":
        return jsonify({"error": "正在加载中，请稍候"}), 400
    threading.Thread(target=build_cache, daemon=True).start()
    return jsonify({"message": "已开始刷新数据"})

@app.route("/api/search", methods=["POST"])
@require_auth
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

    DTW_REF = float(N_DAYS) ** 0.5 * 2  # ≈ 15.5

    results = []
    for idx, dtw in dtw_vals:
        corr      = float(pearson[idx])
        dtw_score = max(0.0, 1.0 - dtw / DTW_REF)
        score     = 0.5 * max(0.0, corr) + 0.5 * dtw_score
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

init_db()
_init()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", nargs="?", default="serve")
    parser.add_argument("--days",  type=int, default=30)
    parser.add_argument("--note",  default="")
    parser.add_argument("key_arg", nargs="?", default=None)
    args, _ = parser.parse_known_args()

    if args.command == "genkey":
        alphabet = string.ascii_uppercase + string.digits
        raw = "".join(secrets.choice(alphabet) for _ in range(16))
        key = "-".join(raw[i:i+4] for i in range(0, 16, 4))
        expiry = (datetime.now() + timedelta(days=args.days)).strftime("%Y-%m-%d")
        con = sqlite3.connect(DB_FILE)
        con.execute(
            "INSERT INTO license_keys (key, expiry_date, note, created_at) VALUES (?,?,?,?)",
            (key, expiry, args.note, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        con.commit(); con.close()
        print(f"授权码: {key}")
        print(f"到期日: {expiry}")
        if args.note:
            print(f"备注:   {args.note}")

    elif args.command == "listkeys":
        con = sqlite3.connect(DB_FILE)
        rows = con.execute(
            "SELECT key, expiry_date, fingerprint, activated_at, note FROM license_keys ORDER BY created_at DESC"
        ).fetchall()
        con.close()
        print(f"{'授权码':<22} {'到期日':>10}  {'状态':>6}  {'激活时间':>19}  备注")
        print("-" * 76)
        for r in rows:
            status = "已激活" if r[2] else "未激活"
            print(f"{r[0]:<22} {r[1]:>10}  {status:>6}  {r[3] or '':>19}  {r[4] or ''}")

    elif args.command == "resetkey":
        key = args.key_arg
        if not key:
            print("用法: python backend.py resetkey <授权码>")
        else:
            con = sqlite3.connect(DB_FILE)
            con.execute(
                "UPDATE license_keys SET fingerprint=NULL, activated_at=NULL WHERE key=?", (key,)
            )
            con.commit(); con.close()
            print(f"已重置: {key}（下次使用时可在新设备激活）")

    else:
        port = int(os.environ.get("PORT", 5001))
        print(f"🚀  http://localhost:{port}")
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
