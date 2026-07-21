#!/usr/bin/env python3
"""
GMO×bitbank マーケットメイキングBot v2
4段階判定 + シミュレーションモード + 本番モード（安全装置付き）

起動:
  python3 bot_v2.py --mode sim    # シミュレーション（デフォルト・注文を出さない）
  python3 bot_v2.py --mode live   # 本番（実際に注文。起動条件あり・後述）

live 起動条件（全て満たさないと起動しない・フェイルクローズ）:
  1. .env に BITBANK_API_KEY / BITBANK_API_SECRET が設定済み
  2. bot_config.json に "live_confirmed": true がある（管理画面で明示確認）
  3. 安全上限（max_position / daily_loss_limit / max_order_jpy 等）が範囲内
"""

import argparse
import csv
import hmac
import hashlib
import json
import math
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "bot_config.json")
STATE_FILE = os.path.join(HERE, "sim_state.json")
OWN_ORDERS_FILE = os.path.join(HERE, "own_orders.json")

# ============================================
# 設定値（bot_config.json で上書き可能）
# ============================================
BITBANK_API_KEY    = os.getenv("BITBANK_API_KEY", "")
BITBANK_API_SECRET = os.getenv("BITBANK_API_SECRET", "")

DEFAULT_CONFIG = {
    "symbol":            "eth_jpy",
    "order_size":        0.05,
    "interval":          10,
    "spread_range":      300,
    "spread_trend":      800,
    "spread_threshold":  300,
    "volatility_window": 20,
    "volatility_max":    1000,
    "range_window":      20,
    "range_threshold":   2000,
    "crash_threshold":   3.0,    # %
    # リスク管理
    "stop_loss_rate":    0.02,   # 2%
    "max_position":      0.2,    # ETH
    "daily_loss_limit":  10000,  # 円/日
    "max_order_jpy":     100000, # 1注文の最大金額（円）
    "max_open_orders":   4,      # 同時に出す自Bot注文の最大数
    "post_only":         True,   # メーカー注文のみ
    "live_confirmed":    False,  # 管理画面での明示確認
}

# 厳格な上下限検証（キー: (最小, 最大)）
CONFIG_LIMITS = {
    "order_size":        (0.001, 1.0),
    "interval":          (3, 300),
    "spread_range":      (10, 100000),
    "spread_trend":      (10, 100000),
    "spread_threshold":  (0, 100000),
    "volatility_window": (3, 200),
    "volatility_max":    (10, 100000),
    "range_window":      (3, 200),
    "range_threshold":   (10, 1000000),
    "crash_threshold":   (0.1, 50.0),
    "stop_loss_rate":    (0.001, 0.5),
    "max_position":      (0.001, 10.0),
    "daily_loss_limit":  (100, 10000000),
    "max_order_jpy":     (100, 10000000),
    "max_open_orders":   (1, 20),
}

ALLOWED_SYMBOLS = {"eth_jpy", "btc_jpy", "xrp_jpy", "ltc_jpy", "mona_jpy", "bcc_jpy"}


class ConfigError(Exception):
    pass


def load_config(path=CONFIG_FILE):
    """bot_config.json を読み、厳格に検証する。範囲外・型不正は例外（フェイルクローズ）"""
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise ConfigError(f"bot_config.json が壊れています: {e}")
        if not isinstance(user, dict):
            raise ConfigError("bot_config.json の形式が不正です")
        cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})

    if cfg["symbol"] not in ALLOWED_SYMBOLS:
        raise ConfigError(f"symbol が不正です: {cfg['symbol']}")
    for key, (lo, hi) in CONFIG_LIMITS.items():
        v = cfg[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError(f"{key} が数値ではありません: {v!r}")
        if not (lo <= v <= hi):
            raise ConfigError(f"{key}={v} は許容範囲外です（{lo}〜{hi}）")
    cfg["post_only"] = bool(cfg["post_only"])
    cfg["live_confirmed"] = bool(cfg["live_confirmed"])
    return cfg


# ============================================
# bitbank Private API（直接実装）
# ============================================
class ApiError(Exception):
    """bitbank API 呼び出し失敗（HTTP異常 / success!=1）"""


class BitbankAPI:
    BASE = "https://api.bitbank.cc/v1"

    def __init__(self, key, secret):
        if not key or not secret:
            raise ApiError("APIキー/シークレットが未設定です")
        self.key = key
        self.secret = secret

    def _headers(self, nonce, signature):
        return {
            "ACCESS-KEY": self.key,
            "ACCESS-NONCE": nonce,
            "ACCESS-SIGNATURE": signature,
            "Content-Type": "application/json",
        }

    def _sign(self, message):
        return hmac.new(self.secret.encode(), message.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _check(res):
        if res.status_code != 200:
            raise ApiError(f"HTTP {res.status_code}: {res.text[:200]}")
        try:
            data = res.json()
        except ValueError:
            raise ApiError(f"JSONではない応答: {res.text[:200]}")
        if data.get("success") != 1:
            raise ApiError(f"APIエラー: {data.get('data')}")
        return data["data"]

    def _get(self, path):
        # bitbank Private GET の署名は nonce + "/v1" + path
        nonce = str(int(time.time() * 1000))
        message = nonce + "/v1" + path
        sig = self._sign(message)
        res = requests.get(self.BASE + path, headers=self._headers(nonce, sig), timeout=10)
        return self._check(res)

    def _post(self, path, body):
        nonce = str(int(time.time() * 1000))
        message = nonce + json.dumps(body)
        sig = self._sign(message)
        res = requests.post(self.BASE + path, headers=self._headers(nonce, sig), json=body, timeout=10)
        return self._check(res)

    def get_assets(self):
        return self._get("/user/spot/assets")

    def get_active_orders(self, pair):
        return self._get(f"/user/spot/active_orders?pair={pair}")

    def order(self, pair, price, amount, side, order_type, post_only=False):
        body = {"pair": pair, "price": str(price), "amount": str(amount),
                "side": side, "type": order_type}
        if post_only:
            body["post_only"] = True
        return self._post("/user/spot/order", body)

    def cancel_order(self, pair, order_id):
        body = {"pair": pair, "order_id": int(order_id)}
        return self._post("/user/spot/cancel_order", body)


# ============================================
# 自Bot注文IDの管理（手動/他Bot注文は絶対に触らない）
# ============================================
class OwnOrders:
    def __init__(self, path=OWN_ORDERS_FILE):
        self.path = path
        self.ids = set()
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.ids = {int(x) for x in data}
        except (OSError, json.JSONDecodeError, ValueError):
            self.ids = set()

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(sorted(self.ids), f)
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def add(self, order_id):
        self.ids.add(int(order_id))
        self.save()

    def discard(self, order_id):
        self.ids.discard(int(order_id))
        self.save()


def cancel_own_orders(prv, pair, own):
    """自Bot注文だけを取消す。戻り値: (取消成功数, 失敗した注文IDリスト)
    active_orders に無い自注文IDは約定/取消済みとして台帳から外す。"""
    try:
        result = prv.get_active_orders(pair)
    except ApiError as e:
        log(f"⚠️ アクティブ注文の取得に失敗: {e} → 取消不能（新規発注は禁止）")
        return 0, sorted(own.ids)
    active_ids = {int(o["order_id"]) for o in result.get("orders", [])}
    cancelled = 0
    failed = []
    for oid in sorted(own.ids):
        if oid not in active_ids:
            own.discard(oid)  # 既に約定/取消済み
            continue
        try:
            prv.cancel_order(pair, oid)
            own.discard(oid)
            cancelled += 1
        except ApiError as e:
            log(f"⚠️ 注文 {oid} の取消失敗: {e}")
            failed.append(oid)
    own.save()
    return cancelled, failed


def place_live_order(prv, cfg, side, price, own):
    """自Bot注文として発注し、注文IDを台帳に記録する"""
    data = prv.order(cfg["symbol"], int(price), cfg["order_size"], side, "limit",
                     post_only=cfg["post_only"])
    oid = data.get("order_id")
    if oid is not None:
        own.add(oid)
    return oid


# ============================================
# グローバル状態
# ============================================
gmo_history = deque(maxlen=200)
prev_gmo_price = None
STOP = False  # SIGTERM/SIGINT で True

# シミュレーション状態
sim = {
    "jpy": 100000,
    "eth": 0.0,
    "avg_buy_price": 0.0,
    "buy_order": None,
    "sell_order": None,
    "stop_order": None,
    "total_trades": 0,
    "total_profit": 0.0,
    "maker_reward": 0.0,
    "trades": [],
}

csv_file = None
csv_writer = None


def request_stop(signum, frame):
    global STOP
    STOP = True
    log(f"停止シグナル受信 (signal {signum})")


# ============================================
# ログ
# ============================================
def log(msg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


# ============================================
# GMO価格取得
# ============================================
def get_gmo_price():
    res = requests.get("https://api.coin.z.com/public/v1/ticker?symbol=ETH", timeout=10)
    res.raise_for_status()
    data = res.json()
    if data.get("status") != 0:
        raise Exception(f"GMO API: {data}")
    return float(data["data"][0]["last"])


# ============================================
# bitbank価格取得（Public API）
# ============================================
def get_bitbank_price(symbol="eth_jpy"):
    res = requests.get(f"https://public.bitbank.cc/{symbol}/ticker", timeout=10)
    res.raise_for_status()
    data = res.json()
    if data.get("success") != 1:
        raise Exception(f"bitbank API: {data}")
    return float(data["data"]["last"])


# ============================================
# 判定①：乖離チェック
# ============================================
def check_spread(gmo_price, bb_price, threshold):
    gap = abs(gmo_price - bb_price)
    direction = "GMO高" if gmo_price > bb_price else "bitbank高"
    ok = gap >= threshold
    return ok, gap, direction


# ============================================
# 判定②：ボラティリティチェック
# ============================================
def check_volatility(window, vol_max):
    prices = list(gmo_history)[-window:]
    if len(prices) < 3:
        return True, 0
    mean = sum(prices) / len(prices)
    variance = sum((p - mean) ** 2 for p in prices) / len(prices)
    std = math.sqrt(variance)
    ok = std <= vol_max
    return ok, round(std)


# ============================================
# 判定③：レンジ/トレンド判定
# ============================================
def check_range(window, threshold, spread_range, spread_trend):
    prices = list(gmo_history)[-window:]
    if len(prices) < 3:
        return "レンジ", 0, spread_range
    price_range = max(prices) - min(prices)
    if price_range <= threshold:
        return "レンジ", round(price_range), spread_range
    else:
        return "トレンド", round(price_range), spread_trend


# ============================================
# 判定④：急落/急騰検知
# ============================================
def check_crash(gmo_price, threshold_pct):
    global prev_gmo_price
    if prev_gmo_price is None:
        prev_gmo_price = gmo_price
        return False, 0
    change = abs(gmo_price - prev_gmo_price) / prev_gmo_price
    crash = change >= threshold_pct / 100.0
    prev_gmo_price = gmo_price
    return crash, round(change * 100, 2)


# ============================================
# 本番: 残高・日次損失
# ============================================
def get_balances(prv):
    """戻り値: (JPYフリー, ETHフリー+ロック合計)。失敗時は ApiError（フェイルクローズ）"""
    data = prv.get_assets()
    jpy = eth = 0.0
    for a in data.get("assets", []):
        try:
            amount = float(a.get("free_amount", 0)) + float(a.get("locked_amount", 0))
        except (TypeError, ValueError):
            continue
        if a.get("asset") == "jpy":
            jpy = amount
        elif a.get("asset") == "eth":
            eth = amount
    return jpy, eth


def check_daily_loss(state, equity_now, limit):
    """日次損失が上限を超えたら True（停止すべき）"""
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("equity_date") != today:
        state["equity_date"] = today
        state["equity_start"] = equity_now
    loss = state["equity_start"] - equity_now
    state["daily_loss"] = round(loss)
    return loss >= limit


# ============================================
# シミュレーション: 約定チェック
# ============================================
def sim_check_fills(bb_price, cfg):
    filled = []
    size = cfg["order_size"]

    # 買い約定
    if sim["buy_order"] and bb_price <= sim["buy_order"]:
        cost = sim["buy_order"] * size
        if sim["jpy"] >= cost:
            sim["jpy"] -= cost
            sim["eth"] += size
            sim["avg_buy_price"] = sim["buy_order"]
            sim["total_trades"] += 1
            # Maker報酬（-0.02%）
            reward = cost * 0.0002
            sim["maker_reward"] += reward
            sim["jpy"] += reward
            filled.append(("BUY", sim["buy_order"]))
            sim["trades"].append({"time": datetime.now().isoformat(), "side": "BUY", "price": sim["buy_order"], "size": size})
            log(f"  🟢 [SIM] 買い約定! {sim['buy_order']:,.0f}円 × {size} ETH (報酬+{reward:.0f}円)")
        sim["buy_order"] = None

    # 売り約定
    if sim["sell_order"] and bb_price >= sim["sell_order"]:
        if sim["eth"] >= size:
            revenue = sim["sell_order"] * size
            sim["jpy"] += revenue
            profit = (sim["sell_order"] - sim["avg_buy_price"]) * size
            sim["total_profit"] += profit
            sim["eth"] -= size
            sim["total_trades"] += 1
            reward = revenue * 0.0002
            sim["maker_reward"] += reward
            sim["jpy"] += reward
            filled.append(("SELL", sim["sell_order"]))
            sim["trades"].append({"time": datetime.now().isoformat(), "side": "SELL", "price": sim["sell_order"], "size": size})
            log(f"  🔴 [SIM] 売り約定! {sim['sell_order']:,.0f}円 × {size} ETH (利益{'+' if profit>=0 else ''}{profit:,.0f}円 報酬+{reward:.0f}円)")
        sim["sell_order"] = None

    # 損切り約定
    if sim["stop_order"] and bb_price <= sim["stop_order"]:
        if sim["eth"] >= size:
            revenue = sim["stop_order"] * size
            sim["jpy"] += revenue
            loss = (sim["stop_order"] - sim["avg_buy_price"]) * size
            sim["total_profit"] += loss
            sim["eth"] -= size
            sim["total_trades"] += 1
            filled.append(("STOP", sim["stop_order"]))
            sim["trades"].append({"time": datetime.now().isoformat(), "side": "STOP", "price": sim["stop_order"], "size": size})
            log(f"  ⚠️ [SIM] 損切り約定! {sim['stop_order']:,.0f}円 × {size} ETH (損失{loss:,.0f}円)")
        sim["stop_order"] = None

    return filled


# ============================================
# 状態ファイル書き出し（demo/ダッシュボード共通契約）
# ============================================
def write_state(mode, gmo_price=0, bb_price=0, gap=0, gap_dir="", spread_ok=False,
                vol_ok=False, vol_std=0, market_type="", spread=0, crash=False,
                halted=False, message="", open_orders=0, daily_loss=0,
                jpy=None, eth=None, total_asset=None):
    eth_val = (eth if eth is not None else sim["eth"]) * bb_price
    total = total_asset if total_asset is not None else round((jpy if jpy is not None else sim["jpy"]) + eth_val)
    state_data = {
        "updated_at": int(time.time()),
        "mode": mode,
        "gmo_price": gmo_price,
        "bb_price": bb_price,
        "gap": gap,
        "gap_dir": gap_dir,
        "spread_ok": spread_ok,
        "vol_ok": vol_ok,
        "volatility": vol_std,
        "market_type": market_type,
        "spread": spread,
        "crash": crash,
        "halted": halted,
        "message": message,
        "open_orders": open_orders,
        "daily_loss": daily_loss,
        "total_profit": round(sim["total_profit"]),
        "maker_reward": round(sim["maker_reward"]),
        "total_trades": sim["total_trades"],
        "eth": eth if eth is not None else sim["eth"],
        "jpy": round(jpy) if jpy is not None else round(sim["jpy"]),
        "total_asset": total,
        "buy_order": sim["buy_order"],
        "sell_order": sim["sell_order"],
        "stop_order": sim["stop_order"],
        "trades": sim["trades"][-30:],
    }
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state_data, f, ensure_ascii=False, default=str)
        os.replace(tmp, STATE_FILE)
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass


# ============================================
# CSV書き込み
# ============================================
def write_csv(gmo, bb, gap, state, filled):
    if csv_writer is None:
        return
    eth_val = sim["eth"] * bb
    total = sim["jpy"] + eth_val
    csv_writer.writerow([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        int(gmo), int(bb), int(gap), state, filled,
        round(sim["total_profit"]), round(sim["maker_reward"]),
        round(sim["eth"], 4), round(total),
    ])
    csv_file.flush()


# ============================================
# live 起動条件チェック（フェイルクローズ）
# ============================================
def check_live_preconditions(cfg, api_key, api_secret):
    """満たさない場合は理由のリストを返す。空リストなら起動可"""
    reasons = []
    if not api_key or not api_secret:
        reasons.append("APIキー/シークレットが未設定です（管理画面で登録してください）")
    if not cfg["live_confirmed"]:
        reasons.append("管理画面で本番モードの明示確認（live_confirmed）がされていません")
    for key in ("max_position", "daily_loss_limit", "max_order_jpy", "max_open_orders", "stop_loss_rate"):
        if key not in CONFIG_LIMITS:
            reasons.append(f"安全設定 {key} がありません")
    return reasons


# ============================================
# メインループ
# ============================================
def main():
    global csv_file, csv_writer

    parser = argparse.ArgumentParser(description="GMO×bitbank マーケットメイキングBot v2")
    parser.add_argument("--mode", choices=["sim", "live"], default="sim",
                        help="sim=シミュレーション（デフォルト） / live=本番")
    args = parser.parse_args()
    mode = args.mode

    try:
        cfg = load_config()
    except ConfigError as e:
        log(f"❌ 設定エラー（起動中止）: {e}")
        sys.exit(2)

    SYMBOL = cfg["symbol"]

    log("=" * 55)
    log(f"マーケットメイキングBot v2 【{'シミュレーション' if mode == 'sim' else '本番ライブ'}】")
    log(f"銘柄: {SYMBOL} / 注文量: {cfg['order_size']} ETH")
    log(f"スプレッド: レンジ±{cfg['spread_range']} / トレンド±{cfg['spread_trend']}")
    log(f"乖離しきい値: {cfg['spread_threshold']}円 / ボラ上限: {cfg['volatility_max']}円")
    log(f"損切り: {cfg['stop_loss_rate']*100}% / 最大保有: {cfg['max_position']} ETH")
    log(f"日次損失上限: {cfg['daily_loss_limit']}円 / 1注文上限: {cfg['max_order_jpy']}円 / post_only: {cfg['post_only']}")
    if mode == "sim":
        log(f"仮想資金: {sim['jpy']:,.0f}円")
    log("=" * 55)

    prv = None
    own = None
    live_state = {"equity_date": None, "equity_start": 0.0, "daily_loss": 0}
    halted = False

    if mode == "live":
        reasons = check_live_preconditions(cfg, BITBANK_API_KEY, BITBANK_API_SECRET)
        if reasons:
            for r in reasons:
                log(f"❌ live起動条件未達: {r}")
            write_state(mode, halted=True, message="live起動条件未達: " + " / ".join(reasons))
            sys.exit(3)
        try:
            prv = BitbankAPI(BITBANK_API_KEY, BITBANK_API_SECRET)
            jpy, eth = get_balances(prv)  # 起動時に残高確認（失敗=起動中止）
            log(f"残高確認: JPY {jpy:,.0f} / ETH {eth:.4f}")
        except ApiError as e:
            log(f"❌ 残高取得に失敗（起動中止）: {e}")
            write_state(mode, halted=True, message=f"残高取得失敗: {e}")
            sys.exit(4)
        own = OwnOrders()
        if own.ids:
            log(f"前回の自Bot注文ID {len(own.ids)}件を引き継ぎました（手動/他Bot注文は触りません）")
    else:
        # CSV準備
        csv_name = f"simulation_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        csv_path = os.path.join(HERE, csv_name)
        csv_file = open(csv_path, "w", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(["timestamp", "gmo_price", "bitbank_price", "乖離", "判定状態", "仮想約定", "累計損益", "累計報酬", "保有ETH", "合計資産"])
        log(f"CSV: {csv_name}")

    while not STOP:
        try:
            # --- 価格取得 ---
            gmo_price = get_gmo_price()
            bb_price = get_bitbank_price(SYMBOL)
            gmo_history.append(gmo_price)

            log("=" * 55)
            log(f"GMO価格     : {gmo_price:,.0f}円")
            log(f"bitbank価格 : {bb_price:,.0f}円")

            # --- 判定①：乖離 ---
            spread_ok, gap, direction = check_spread(gmo_price, bb_price, cfg["spread_threshold"])
            log(f"乖離        : {gap:,.0f}円（{direction}）{'✅' if spread_ok else '❌ しきい値未満'}")

            # --- 判定②：ボラティリティ ---
            vol_ok, vol_std = check_volatility(cfg["volatility_window"], cfg["volatility_max"])
            log(f"ボラティリティ: {vol_std:,}円{'（正常）✅' if vol_ok else '（過大）❌ 停止'}")

            # --- 判定③：レンジ/トレンド ---
            market_type, price_range, spread = check_range(
                cfg["range_window"], cfg["range_threshold"], cfg["spread_range"], cfg["spread_trend"])
            log(f"相場判定    : {market_type}相場（変動幅{price_range:,}円）")
            log(f"スプレッド  : ±{spread:,}円")

            # --- 判定④：急落/急騰 ---
            crash, crash_pct = check_crash(gmo_price, cfg["crash_threshold"])

            if halted:
                log("⛔ 日次損失上限により停止中。自Bot注文のみ取消済み。")
                write_state(mode, gmo_price, bb_price, gap, direction, spread_ok, vol_ok,
                            vol_std, market_type, spread, crash, halted=True,
                            message="日次損失上限で停止中", daily_loss=live_state.get("daily_loss", 0))
                time.sleep(cfg["interval"])
                continue

            if crash:
                log(f"⚠️ 急変検知   : {crash_pct}% → 自Bot注文キャンセル・停止")
                if mode == "live":
                    c, failed = cancel_own_orders(prv, SYMBOL, own)
                    log(f"  自Bot注文キャンセル: {c}件" + (f" / 失敗{len(failed)}件 {failed}" if failed else ""))
                sim["buy_order"] = None
                sim["sell_order"] = None
                sim["stop_order"] = None
                write_csv(gmo_price, bb_price, gap, "急変停止", "")
                write_state(mode, gmo_price, bb_price, gap, direction, spread_ok, vol_ok,
                            vol_std, market_type, spread, crash=True)
                time.sleep(cfg["interval"])
                continue

            # --- 総合判定（待機条件） ---
            if not spread_ok or not vol_ok:
                reason = "乖離不足" if not spread_ok else "ボラ過大"
                log(f"→ {reason} → 待機")
                if mode == "live":
                    c, failed = cancel_own_orders(prv, SYMBOL, own)
                    if c > 0 or failed:
                        log(f"  自Bot注文キャンセル: {c}件" + (f" / 失敗{len(failed)}件 {failed}" if failed else ""))
                sim["buy_order"] = None
                sim["sell_order"] = None
                write_csv(gmo_price, bb_price, gap, reason, "")
                write_state(mode, gmo_price, bb_price, gap, direction, spread_ok, vol_ok,
                            vol_std, market_type, spread, crash)
                time.sleep(cfg["interval"])
                continue

            # --- 注文ロジック ---
            filled_str = ""
            if mode == "sim":
                fills = sim_check_fills(bb_price, cfg)
                filled_str = ",".join(f"{s}@{p:,.0f}" for s, p in fills)

                # 買い指値
                if gmo_price > bb_price:
                    buy_price = bb_price - spread
                    if sim["jpy"] >= buy_price * cfg["order_size"] and sim["eth"] < cfg["max_position"]:
                        sim["buy_order"] = buy_price
                        log(f"買い指値    : {buy_price:,.0f}円 [SIM]発注")
                    else:
                        log(f"買い指値    : 資金不足 or 保有上限")

                # 売り指値（ETH保有時のみ）
                if sim["eth"] >= cfg["order_size"]:
                    sell_price = sim["avg_buy_price"] + spread
                    sim["sell_order"] = sell_price
                    log(f"売り指値    : {sell_price:,.0f}円 [SIM]発注（保有ETH:{sim['eth']:.4f}）")

                    # 損切りセット
                    stop_price = sim["avg_buy_price"] * (1 - cfg["stop_loss_rate"])
                    sim["stop_order"] = stop_price
                    log(f"損切り      : {stop_price:,.0f}円 [SIM]セット")

                write_csv(gmo_price, bb_price, gap, market_type, filled_str)
                write_state(mode, gmo_price, bb_price, gap, direction, spread_ok, vol_ok,
                            vol_std, market_type, spread, crash)

            else:
                # ===== 本番 =====
                # 残高確認（失敗=フェイルクローズ：取消して新規発注しない）
                try:
                    jpy_free, eth_total = get_balances(prv)
                except ApiError as e:
                    log(f"⚠️ 残高取得失敗: {e} → 新規発注禁止（自Bot注文は取消）")
                    c, failed = cancel_own_orders(prv, SYMBOL, own)
                    write_state(mode, gmo_price, bb_price, gap, direction, spread_ok, vol_ok,
                                vol_std, market_type, spread, crash,
                                message=f"残高取得失敗: {e}", open_orders=len(own.ids))
                    time.sleep(cfg["interval"])
                    continue

                equity = jpy_free + eth_total * bb_price

                # 日次損失チェック
                if check_daily_loss(live_state, equity, cfg["daily_loss_limit"]):
                    halted = True
                    log(f"⛔ 日次損失 {live_state['daily_loss']:,}円 が上限 {cfg['daily_loss_limit']:,}円 に到達 → 全停止")
                    c, failed = cancel_own_orders(prv, SYMBOL, own)
                    log(f"  自Bot注文キャンセル: {c}件" + (f" / 失敗{len(failed)}件 {failed}" if failed else ""))
                    write_state(mode, gmo_price, bb_price, gap, direction, spread_ok, vol_ok,
                                vol_std, market_type, spread, crash, halted=True,
                                message="日次損失上限に到達", daily_loss=live_state["daily_loss"],
                                jpy=jpy_free, eth=eth_total, total_asset=round(equity))
                    time.sleep(cfg["interval"])
                    continue

                # 既存の自Bot注文を取消し（失敗があれば新規発注しない）
                cancelled, failed = cancel_own_orders(prv, SYMBOL, own)
                if cancelled > 0:
                    log(f"自Bot注文キャンセル: {cancelled}件")
                if failed:
                    log(f"⚠️ 取消失敗 {len(failed)}件 {failed} → 新規発注禁止")
                    write_state(mode, gmo_price, bb_price, gap, direction, spread_ok, vol_ok,
                                vol_std, market_type, spread, crash,
                                message=f"取消失敗のため発注停止: {failed}", open_orders=len(own.ids),
                                jpy=jpy_free, eth=eth_total, total_asset=round(equity))
                    time.sleep(cfg["interval"])
                    continue

                open_count = 0

                # 買い（残高・保有上限・注文額上限・注文数上限を全部確認）
                if gmo_price > bb_price:
                    buy_price = bb_price - spread
                    order_jpy = buy_price * cfg["order_size"]
                    if eth_total >= cfg["max_position"]:
                        log(f"買い指値    : 保有上限({cfg['max_position']} ETH)のため見送り")
                    elif order_jpy > cfg["max_order_jpy"]:
                        log(f"買い指値    : 注文額上限({cfg['max_order_jpy']:,}円)超過のため見送り")
                    elif jpy_free < order_jpy:
                        log(f"買い指値    : JPY残高不足（{jpy_free:,.0f}円）のため見送り")
                    elif open_count >= cfg["max_open_orders"]:
                        log(f"買い指値    : 注文数上限のため見送り")
                    else:
                        try:
                            oid = place_live_order(prv, cfg, "buy", buy_price, own)
                            open_count += 1
                            log(f"買い指値    : {buy_price:,.0f}円 発注完了 (ID:{oid})")
                        except ApiError as e:
                            log(f"買い発注失敗: {e}")

                # 売り（ETH残高がある時のみ）
                if eth_total >= cfg["order_size"]:
                    sell_price = bb_price + spread
                    if open_count >= cfg["max_open_orders"]:
                        log(f"売り指値    : 注文数上限のため見送り")
                    else:
                        try:
                            oid = place_live_order(prv, cfg, "sell", sell_price, own)
                            open_count += 1
                            log(f"売り指値    : {sell_price:,.0f}円 発注完了 (ID:{oid})")
                        except ApiError as e:
                            log(f"売り発注失敗: {e}")

                write_state(mode, gmo_price, bb_price, gap, direction, spread_ok, vol_ok,
                            vol_std, market_type, spread, crash, open_orders=len(own.ids),
                            daily_loss=live_state.get("daily_loss", 0),
                            jpy=jpy_free, eth=eth_total, total_asset=round(equity))

            time.sleep(cfg["interval"])

        except Exception as e:
            log(f"エラー: {e}")
            time.sleep(cfg["interval"])

    # ===== 停止処理（SIGTERM/SIGINT） =====
    log("=" * 55)
    log("停止処理を開始します")

    if mode == "live" and prv and own:
        log("自Bot未約定注文をキャンセル中（手動/他Bot注文は触りません）...")
        try:
            c, failed = cancel_own_orders(prv, SYMBOL, own)
            log(f"キャンセル完了: {c}件")
            if failed:
                log(f"⚠️ キャンセル失敗: {len(failed)}件 {failed} → 取引所画面で手動確認してください")
        except Exception as e:
            log(f"⚠️ キャンセル処理でエラー: {e} → 取引所画面で手動確認してください")

    log("【最終結果】")
    if mode == "sim":
        try:
            bb = get_bitbank_price(SYMBOL)
        except Exception:
            bb = 0
        eth_val = sim["eth"] * bb
        total = sim["jpy"] + eth_val
        log(f"初期資金    : 100,000円")
        log(f"最終資産    : {total:,.0f}円")
        log(f"累計損益    : {'+' if sim['total_profit']>=0 else ''}{sim['total_profit']:,.0f}円")
        log(f"累計Maker報酬: +{sim['maker_reward']:,.0f}円")
        log(f"約定回数    : {sim['total_trades']}回")
        log(f"保有ETH     : {sim['eth']:.4f}")
        write_state(mode, halted=True, message="停止")
    else:
        write_state(mode, halted=True, message="停止", open_orders=len(own.ids) if own else 0)
    log("=" * 55)

    if csv_file:
        csv_file.close()
        log("CSV保存完了")

    log("Bot停止")


# ============================================
# 起動
# ============================================
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    main()
