#!/usr/bin/env python3
"""
GMO×bitbank マーケットメイキングBot v2
4段階判定 + シミュレーションモード + CSV出力
"""

import time
import csv
import os
from dotenv import load_dotenv
load_dotenv()
import math
import hmac
import hashlib
import json
import requests
from datetime import datetime
from collections import deque
from urllib.parse import urlencode

# ============================================
# 設定値
# ============================================
BITBANK_API_KEY    = os.getenv("BITBANK_API_KEY", "")
BITBANK_API_SECRET = os.getenv("BITBANK_API_SECRET", "")
SYMBOL             = "eth_jpy"
ORDER_SIZE         = 0.05
INTERVAL           = 10

# スプレッド設定
SPREAD_RANGE       = 300    # レンジ相場時のスプレッド（円）
SPREAD_TREND       = 800    # トレンド相場時のスプレッド（円）
SPREAD_THRESHOLD   = 300    # GMO-bitbank乖離のしきい値（円）

# 判定設定
VOLATILITY_WINDOW  = 20
VOLATILITY_MAX     = 1000
RANGE_WINDOW       = 20
RANGE_THRESHOLD    = 2000
CRASH_THRESHOLD    = 0.03   # 3%

# リスク管理
STOP_LOSS_RATE     = 0.02   # 2%
MAX_POSITION       = 0.2

# シミュレーション
SIMULATION         = True   # True=デモ False=本番

# ============================================
# bitbank Private API（直接実装）
# ============================================
class BitbankAPI:
    BASE = "https://api.bitbank.cc/v1"

    def __init__(self, key, secret):
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

    def _get(self, path):
        nonce = str(int(time.time() * 1000))
        message = nonce + path
        sig = self._sign(message)
        res = requests.get(self.BASE + path, headers=self._headers(nonce, sig), timeout=10)
        return res.json()

    def _post(self, path, body):
        nonce = str(int(time.time() * 1000))
        message = nonce + json.dumps(body)
        sig = self._sign(message)
        res = requests.post(self.BASE + path, headers=self._headers(nonce, sig), json=body, timeout=10)
        return res.json()

    def get_active_orders(self, pair):
        return self._get(f"/user/spot/active_orders?pair={pair}")

    def order(self, pair, price, amount, side, order_type):
        body = {"pair": pair, "price": str(price), "amount": str(amount), "side": side, "type": order_type}
        return self._post("/user/spot/order", body)

    def cancel_order(self, pair, order_id):
        body = {"pair": pair, "order_id": order_id}
        return self._post("/user/spot/cancel_order", body)

# ============================================
# グローバル状態
# ============================================
gmo_history = deque(maxlen=max(VOLATILITY_WINDOW, RANGE_WINDOW))
prev_gmo_price = None

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

# ============================================
# ログ
# ============================================
def log(msg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}")

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
def get_bitbank_price():
    res = requests.get("https://public.bitbank.cc/eth_jpy/ticker", timeout=10)
    res.raise_for_status()
    data = res.json()
    if data.get("success") != 1:
        raise Exception(f"bitbank API: {data}")
    return float(data["data"]["last"])

# ============================================
# 判定①：乖離チェック
# ============================================
def check_spread(gmo_price, bb_price):
    gap = abs(gmo_price - bb_price)
    direction = "GMO高" if gmo_price > bb_price else "bitbank高"
    ok = gap >= SPREAD_THRESHOLD
    return ok, gap, direction

# ============================================
# 判定②：ボラティリティチェック
# ============================================
def check_volatility():
    if len(gmo_history) < 3:
        return True, 0
    prices = list(gmo_history)
    mean = sum(prices) / len(prices)
    variance = sum((p - mean) ** 2 for p in prices) / len(prices)
    std = math.sqrt(variance)
    ok = std <= VOLATILITY_MAX
    return ok, round(std)

# ============================================
# 判定③：レンジ/トレンド判定
# ============================================
def check_range():
    if len(gmo_history) < 3:
        return "レンジ", 0, SPREAD_RANGE
    prices = list(gmo_history)[-RANGE_WINDOW:]
    price_range = max(prices) - min(prices)
    if price_range <= RANGE_THRESHOLD:
        return "レンジ", round(price_range), SPREAD_RANGE
    else:
        return "トレンド", round(price_range), SPREAD_TREND

# ============================================
# 判定④：急落/急騰検知
# ============================================
def check_crash(gmo_price):
    global prev_gmo_price
    if prev_gmo_price is None:
        prev_gmo_price = gmo_price
        return False, 0
    change = abs(gmo_price - prev_gmo_price) / prev_gmo_price
    crash = change >= CRASH_THRESHOLD
    prev_gmo_price = gmo_price
    return crash, round(change * 100, 2)

# ============================================
# bitbank: 未決済注文キャンセル
# ============================================
def cancel_all(prv):
    try:
        result = prv.get_active_orders(SYMBOL)
        orders = result.get("data", {}).get("orders", [])
        count = 0
        for o in orders:
            try:
                prv.cancel_order(SYMBOL, o["order_id"])
                count += 1
            except:
                pass
        return count
    except:
        return 0

# ============================================
# bitbank: 指値注文
# ============================================
def place_order(prv, side, price):
    return prv.order(SYMBOL, int(price), ORDER_SIZE, side, "limit")

# ============================================
# シミュレーション: 約定チェック
# ============================================
def sim_check_fills(bb_price):
    filled = []

    # 買い約定
    if sim["buy_order"] and bb_price <= sim["buy_order"]:
        cost = sim["buy_order"] * ORDER_SIZE
        if sim["jpy"] >= cost:
            sim["jpy"] -= cost
            sim["eth"] += ORDER_SIZE
            sim["avg_buy_price"] = sim["buy_order"]
            sim["total_trades"] += 1
            # Maker報酬（-0.02%）
            reward = cost * 0.0002
            sim["maker_reward"] += reward
            sim["jpy"] += reward
            filled.append(("BUY", sim["buy_order"]))
            sim["trades"].append({"time": datetime.now().isoformat(), "side": "BUY", "price": sim["buy_order"], "size": ORDER_SIZE})
            log(f"  🟢 [SIM] 買い約定! {sim['buy_order']:,.0f}円 × {ORDER_SIZE} ETH (報酬+{reward:.0f}円)")
        sim["buy_order"] = None

    # 売り約定
    if sim["sell_order"] and bb_price >= sim["sell_order"]:
        if sim["eth"] >= ORDER_SIZE:
            revenue = sim["sell_order"] * ORDER_SIZE
            sim["jpy"] += revenue
            profit = (sim["sell_order"] - sim["avg_buy_price"]) * ORDER_SIZE
            sim["total_profit"] += profit
            sim["eth"] -= ORDER_SIZE
            sim["total_trades"] += 1
            reward = revenue * 0.0002
            sim["maker_reward"] += reward
            sim["jpy"] += reward
            filled.append(("SELL", sim["sell_order"]))
            sim["trades"].append({"time": datetime.now().isoformat(), "side": "SELL", "price": sim["sell_order"], "size": ORDER_SIZE})
            log(f"  🔴 [SIM] 売り約定! {sim['sell_order']:,.0f}円 × {ORDER_SIZE} ETH (利益{'+' if profit>=0 else ''}{profit:,.0f}円 報酬+{reward:.0f}円)")
        sim["sell_order"] = None

    # 損切り約定
    if sim["stop_order"] and bb_price <= sim["stop_order"]:
        if sim["eth"] >= ORDER_SIZE:
            revenue = sim["stop_order"] * ORDER_SIZE
            sim["jpy"] += revenue
            loss = (sim["stop_order"] - sim["avg_buy_price"]) * ORDER_SIZE
            sim["total_profit"] += loss
            sim["eth"] -= ORDER_SIZE
            sim["total_trades"] += 1
            filled.append(("STOP", sim["stop_order"]))
            sim["trades"].append({"time": datetime.now().isoformat(), "side": "STOP", "price": sim["stop_order"], "size": ORDER_SIZE})
            log(f"  ⚠️ [SIM] 損切り約定! {sim['stop_order']:,.0f}円 × {ORDER_SIZE} ETH (損失{loss:,.0f}円)")
        sim["stop_order"] = None

    return filled

# ============================================
# メインループ
# ============================================
def main():
    global csv_file, csv_writer

    log("=" * 55)
    log(f"マーケットメイキングBot v2 {'【シミュレーション】' if SIMULATION else '【本番】'}")
    log(f"銘柄: {SYMBOL} / 注文量: {ORDER_SIZE} ETH")
    log(f"スプレッド: レンジ±{SPREAD_RANGE} / トレンド±{SPREAD_TREND}")
    log(f"乖離しきい値: {SPREAD_THRESHOLD}円 / ボラ上限: {VOLATILITY_MAX}円")
    log(f"損切り: {STOP_LOSS_RATE*100}% / 最大保有: {MAX_POSITION} ETH")
    if SIMULATION:
        log(f"仮想資金: {sim['jpy']:,.0f}円")
    log("=" * 55)

    # CSV準備
    if SIMULATION:
        csv_name = f"simulation_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        csv_path = os.path.join(os.path.dirname(__file__), csv_name)
        csv_file = open(csv_path, "w", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(["timestamp", "gmo_price", "bitbank_price", "乖離", "判定状態", "仮想約定", "累計損益", "累計報酬", "保有ETH", "合計資産"])
        log(f"CSV: {csv_name}")

    # bitbank Private API
    prv = None
    if not SIMULATION:
        prv = BitbankAPI(BITBANK_API_KEY, BITBANK_API_SECRET)

    while True:
        try:
            # --- 価格取得 ---
            gmo_price = get_gmo_price()
            bb_price = get_bitbank_price()
            gmo_history.append(gmo_price)

            log("=" * 55)
            log(f"GMO価格     : {gmo_price:,.0f}円")
            log(f"bitbank価格 : {bb_price:,.0f}円")

            # --- 判定①：乖離 ---
            spread_ok, gap, direction = check_spread(gmo_price, bb_price)
            log(f"乖離        : {gap:,.0f}円（{direction}）{'✅' if spread_ok else '❌ しきい値未満'}")

            # --- 判定②：ボラティリティ ---
            vol_ok, vol_std = check_volatility()
            log(f"ボラティリティ: {vol_std:,}円{'（正常）✅' if vol_ok else '（過大）❌ 停止'}")

            # --- 判定③：レンジ/トレンド ---
            market_type, price_range, spread = check_range()
            log(f"相場判定    : {market_type}相場（変動幅{price_range:,}円）")
            log(f"スプレッド  : ±{spread:,}円")

            # --- 判定④：急落/急騰 ---
            crash, crash_pct = check_crash(gmo_price)
            if crash:
                log(f"⚠️ 急変検知   : {crash_pct}% → 全注文キャンセル・停止")
                if not SIMULATION and prv:
                    cancel_all(prv)
                sim["buy_order"] = None
                sim["sell_order"] = None
                sim["stop_order"] = None
                write_csv(gmo_price, bb_price, gap, "急変停止", "")
                time.sleep(INTERVAL)
                continue

            # --- 総合判定 ---
            if not spread_ok:
                log(f"→ 乖離不足 → 待機")
                if not SIMULATION and prv:
                    c = cancel_all(prv)
                    if c > 0: log(f"  注文キャンセル: {c}件")
                sim["buy_order"] = None
                sim["sell_order"] = None
                write_csv(gmo_price, bb_price, gap, "乖離不足", "")
                show_summary(bb_price)
                time.sleep(INTERVAL)
                continue

            if not vol_ok:
                log(f"→ ボラ過大 → 待機")
                if not SIMULATION and prv:
                    c = cancel_all(prv)
                    if c > 0: log(f"  注文キャンセル: {c}件")
                sim["buy_order"] = None
                sim["sell_order"] = None
                write_csv(gmo_price, bb_price, gap, "ボラ停止", "")
                show_summary(bb_price)
                time.sleep(INTERVAL)
                continue

            # --- シミュレーション約定チェック ---
            filled_str = ""
            if SIMULATION:
                fills = sim_check_fills(bb_price)
                filled_str = ",".join(f"{s}@{p:,.0f}" for s, p in fills)

            # --- 注文ロジック ---
            if SIMULATION:
                # 買い指値
                if gmo_price > bb_price:
                    buy_price = bb_price - spread
                    if sim["jpy"] >= buy_price * ORDER_SIZE and sim["eth"] < MAX_POSITION:
                        sim["buy_order"] = buy_price
                        log(f"買い指値    : {buy_price:,.0f}円 [SIM]発注")
                    else:
                        log(f"買い指値    : 資金不足 or 保有上限")

                # 売り指値（ETH保有時のみ）
                if sim["eth"] >= ORDER_SIZE:
                    sell_price = sim["avg_buy_price"] + spread
                    sim["sell_order"] = sell_price
                    log(f"売り指値    : {sell_price:,.0f}円 [SIM]発注（保有ETH:{sim['eth']:.4f}）")

                    # 損切りセット
                    stop_price = sim["avg_buy_price"] * (1 - STOP_LOSS_RATE)
                    sim["stop_order"] = stop_price
                    log(f"損切り      : {stop_price:,.0f}円 [SIM]セット")

            else:
                # 本番
                cancelled = cancel_all(prv)
                if cancelled > 0:
                    log(f"既存注文キャンセル: {cancelled}件")

                # 買い
                if gmo_price > bb_price:
                    buy_price = bb_price - spread
                    try:
                        place_order(prv, "buy", buy_price)
                        log(f"買い指値    : {buy_price:,.0f}円 発注完了")
                    except Exception as e:
                        log(f"買い発注失敗: {e}")

                # 売り（要保有確認は省略 — bitbankがエラーで弾く）
                sell_price = bb_price + spread
                try:
                    place_order(prv, "sell", sell_price)
                    log(f"売り指値    : {sell_price:,.0f}円 発注完了")
                except Exception as e:
                    log(f"売り発注失敗: {e}")

            # --- CSV書き込み ---
            write_csv(gmo_price, bb_price, gap, market_type, filled_str)

            # --- サマリー ---
            show_summary(bb_price, gmo_price, bb_price, gap, direction, spread_ok, vol_ok, vol_std, market_type, spread, False)

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"エラー: {e}")
            time.sleep(INTERVAL)

# ============================================
# サマリー表示
# ============================================
def show_summary(current_price, gmo_price=0, bb_price=0, gap=0, gap_dir="", spread_ok=False, vol_ok=False, vol_std=0, market_type="", spread=0, crash=False):
    if not SIMULATION:
        return
    log("-" * 40)
    eth_val = sim["eth"] * current_price
    total = sim["jpy"] + eth_val
    log(f"累計損益    : {'+' if sim['total_profit']>=0 else ''}{sim['total_profit']:,.0f}円")
    log(f"累計Maker報酬: +{sim['maker_reward']:,.0f}円")
    log(f"約定回数    : {sim['total_trades']}回")
    log(f"保有        : JPY {sim['jpy']:,.0f} + ETH {sim['eth']:.4f}({eth_val:,.0f}円)")
    log(f"合計資産    : {total:,.0f}円")

    # Web管理画面用JSON書き出し
    state_file = os.path.join(os.path.dirname(__file__), "sim_state.json")
    state_data = {
        "updated_at": int(time.time()),
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
        "total_profit": round(sim["total_profit"]),
        "maker_reward": round(sim["maker_reward"]),
        "total_trades": sim["total_trades"],
        "eth": sim["eth"],
        "jpy": round(sim["jpy"]),
        "total_asset": round(total),
        "buy_order": sim["buy_order"],
        "sell_order": sim["sell_order"],
        "stop_order": sim["stop_order"],
        "trades": sim["trades"][-30:],
    }
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state_data, f, ensure_ascii=False, default=str)
    except:
        pass

# ============================================
# CSV書き込み
# ============================================
def write_csv(gmo, bb, gap, state, filled):
    if not SIMULATION or csv_writer is None:
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
# 起動/終了
# ============================================
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("")
        log("停止シグナル受信")

        if not SIMULATION:
            log("全注文キャンセル中...")
            try:
                prv = BitbankAPI(BITBANK_API_KEY, BITBANK_API_SECRET)
                c = cancel_all(prv)
                log(f"キャンセル完了: {c}件")
            except Exception as e:
                log(f"キャンセル失敗: {e}")

        # 最終結果
        log("=" * 55)
        log("【最終結果】")
        if SIMULATION:
            try:
                bb = get_bitbank_price()
            except:
                bb = 0
            eth_val = sim["eth"] * bb
            total = sim["jpy"] + eth_val
            log(f"初期資金    : 100,000円")
            log(f"最終資産    : {total:,.0f}円")
            log(f"累計損益    : {'+' if sim['total_profit']>=0 else ''}{sim['total_profit']:,.0f}円")
            log(f"累計Maker報酬: +{sim['maker_reward']:,.0f}円")
            log(f"約定回数    : {sim['total_trades']}回")
            log(f"保有ETH     : {sim['eth']:.4f}")

            if sim["trades"]:
                log("--- 取引履歴 ---")

        log("=" * 55)

        if csv_file:
            csv_file.close()
            log(f"CSV保存完了")

        log("Bot停止")
