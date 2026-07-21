#!/usr/bin/env python3
"""
マーケットメイキング デモモード
注文は出さず、仮想10万円でリアルタイム価格を使ってシミュレーション。
"""

import time
import json
import os
import requests
from datetime import datetime

# ============================================
# 設定
# ============================================
INITIAL_JPY = 100000         # 初期資金（円）
INITIAL_ETH = 0.0            # 初期ETH
SPREAD = 500                 # 片側スプレッド（円）
ORDER_SIZE = 0.01            # 注文量（ETH）
INTERVAL = 10                # 更新間隔（秒）
LOG_FILE = os.path.join(os.path.dirname(__file__), "demo_log.json")

# ============================================
# 状態
# ============================================
state = {
    "jpy": INITIAL_JPY,
    "eth": INITIAL_ETH,
    "buy_order": None,     # {"price": float}
    "sell_order": None,    # {"price": float}
    "trades": [],          # 約定履歴
    "total_trades": 0,
    "start_time": datetime.now().isoformat(),
}

def log(msg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}")

def save_state():
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=str)

# ============================================
# GMO価格取得
# ============================================
def get_price():
    res = requests.get("https://api.coin.z.com/public/v1/ticker?symbol=ETH", timeout=10)
    res.raise_for_status()
    data = res.json()
    if data.get("status") != 0:
        raise Exception(f"GMO APIエラー: {data}")
    return float(data["data"][0]["last"])

# ============================================
# メインループ
# ============================================
def main():
    log("=" * 55)
    log("デモモード 起動（注文は出しません）")
    log(f"仮想資金: {INITIAL_JPY:,.0f}円 + {INITIAL_ETH} ETH")
    log(f"スプレッド: ±{SPREAD:,.0f}円 / 注文量: {ORDER_SIZE} ETH")
    log("=" * 55)

    loop_count = 0

    while True:
        try:
            price = get_price()
            loop_count += 1

            # --- 約定チェック ---
            # 買い注文が刺さったか
            if state["buy_order"] and price <= state["buy_order"]["price"]:
                cost = state["buy_order"]["price"] * ORDER_SIZE
                if state["jpy"] >= cost:
                    state["jpy"] -= cost
                    state["eth"] += ORDER_SIZE
                    state["total_trades"] += 1
                    trade = {"time": datetime.now().isoformat(), "side": "BUY", "price": state["buy_order"]["price"], "size": ORDER_SIZE}
                    state["trades"].append(trade)
                    log(f"  🟢 買い約定! {state['buy_order']['price']:,.0f}円 × {ORDER_SIZE} ETH")
                state["buy_order"] = None

            # 売り注文が刺さったか
            if state["sell_order"] and price >= state["sell_order"]["price"]:
                if state["eth"] >= ORDER_SIZE:
                    state["jpy"] += state["sell_order"]["price"] * ORDER_SIZE
                    state["eth"] -= ORDER_SIZE
                    state["total_trades"] += 1
                    trade = {"time": datetime.now().isoformat(), "side": "SELL", "price": state["sell_order"]["price"], "size": ORDER_SIZE}
                    state["trades"].append(trade)
                    log(f"  🔴 売り約定! {state['sell_order']['price']:,.0f}円 × {ORDER_SIZE} ETH")
                state["sell_order"] = None

            # --- 新規注文 ---
            buy_price = price - SPREAD
            sell_price = price + SPREAD

            # 買い注文
            if state["jpy"] >= buy_price * ORDER_SIZE:
                state["buy_order"] = {"price": buy_price}
            else:
                state["buy_order"] = None

            # 売り注文
            if state["eth"] >= ORDER_SIZE:
                state["sell_order"] = {"price": sell_price}
            else:
                state["sell_order"] = None

            # --- 資産表示 ---
            eth_value = state["eth"] * price
            total = state["jpy"] + eth_value
            profit = total - INITIAL_JPY
            roi = (profit / INITIAL_JPY) * 100

            buy_str = f"買{state['buy_order']['price']:,.0f}円" if state["buy_order"] else "買なし"
            sell_str = f"売{state['sell_order']['price']:,.0f}円" if state["sell_order"] else "売なし"

            log(f"ETH {price:,.0f}円 | {buy_str} / {sell_str} | "
                f"JPY {state['jpy']:,.0f} + ETH {state['eth']:.4f}({eth_value:,.0f}円) = "
                f"合計 {total:,.0f}円 ({'+' if profit >= 0 else ''}{profit:,.0f}円 / {'+' if roi >= 0 else ''}{roi:.2f}%) "
                f"| 約定{state['total_trades']}回")

            # 定期保存
            if loop_count % 6 == 0:  # 1分ごと
                save_state()

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"エラー: {e}")
            time.sleep(INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("")
        log("停止")

        # 最終結果
        try:
            price = get_price()
        except:
            price = 0

        eth_value = state["eth"] * price
        total = state["jpy"] + eth_value
        profit = total - INITIAL_JPY

        log("=" * 55)
        log("【最終結果】")
        log(f"初期: {INITIAL_JPY:,.0f}円")
        log(f"最終: {total:,.0f}円 (JPY {state['jpy']:,.0f} + ETH {state['eth']:.4f})")
        log(f"損益: {'+' if profit >= 0 else ''}{profit:,.0f}円")
        log(f"約定: {state['total_trades']}回")
        log("=" * 55)

        # 取引履歴
        if state["trades"]:
            log("--- 取引履歴 ---")
            for t in state["trades"][-30:]:
                side = "🟢買" if t["side"] == "BUY" else "🔴売"
                log(f"  {t['time'][:19]} {side} {t['price']:,.0f}円 × {t['size']} ETH")

        save_state()
        log(f"ログ保存: {LOG_FILE}")
