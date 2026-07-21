import os
from dotenv import load_dotenv
load_dotenv()
#!/usr/bin/env python3
"""
GMO価格監視 × bitbank自動発注 マーケットメイキングBot

GMOコインでETH/JPY価格を取得し、
bitbankでスプレッド付き指値注文を自動で繰り返す。
"""

import time
import requests
import python_bitbankcc
from datetime import datetime

# ============================================
# 設定
# ============================================
BITBANK_API_KEY    = os.getenv("BITBANK_API_KEY", "")
BITBANK_API_SECRET = os.getenv("BITBANK_API_SECRET", "")
SYMBOL = "eth_jpy"           # bitbankの通貨ペア
SPREAD = 500                 # 片側のスプレッド幅（円）
ORDER_SIZE = 0.01            # 注文数量（ETH）
INTERVAL = 10                # ループ間隔（秒）

# ============================================
# ログ出力
# ============================================
def log(msg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}")

# ============================================
# GMOコインから現在価格を取得
# ============================================
def get_gmo_price():
    url = "https://api.coin.z.com/public/v1/ticker?symbol=ETH"
    res = requests.get(url, timeout=10)
    res.raise_for_status()
    data = res.json()

    if data.get("status") != 0:
        raise Exception(f"GMO APIエラー: {data}")

    last = float(data["data"][0]["last"])
    return last

# ============================================
# bitbank: 未決済注文を全てキャンセル
# ============================================
def cancel_all_orders(prv):
    try:
        orders = prv.get_active_orders(SYMBOL)
        active = orders.get("orders", [])

        if not active:
            return 0

        count = 0
        for order in active:
            order_id = order["order_id"]
            try:
                prv.cancel_order(SYMBOL, order_id)
                count += 1
            except Exception as e:
                log(f"  キャンセル失敗 (ID:{order_id}): {e}")

        return count

    except Exception as e:
        log(f"  注文一覧取得失敗: {e}")
        return 0

# ============================================
# bitbank: 指値注文を発注
# ============================================
def place_order(prv, side, price):
    price_str = str(int(price))
    size_str = str(ORDER_SIZE)

    result = prv.order(
        SYMBOL,
        price_str,
        size_str,
        side,       # "buy" or "sell"
        "limit"     # 指値
    )

    return result

# ============================================
# メインループ
# ============================================
def main():
    log("=" * 50)
    log("マーケットメイキングBot 起動")
    log(f"通貨ペア: {SYMBOL}")
    log(f"スプレッド: ±{SPREAD:,}円")
    log(f"注文数量: {ORDER_SIZE} ETH")
    log(f"ループ間隔: {INTERVAL}秒")
    log("=" * 50)

    # bitbank APIクライアント初期化
    prv = python_bitbankcc.private(BITBANK_API_KEY, BITBANK_API_SECRET)

    while True:
        try:
            # 1. GMO価格取得
            gmo_price = get_gmo_price()
            log(f"GMO価格: {gmo_price:,.0f}円")

            # 2. 未決済注文を全キャンセル
            cancelled = cancel_all_orders(prv)
            if cancelled > 0:
                log(f"注文キャンセル完了（{cancelled}件）")

            # 3. 買い指値（GMO価格 - スプレッド）
            buy_price = gmo_price - SPREAD
            try:
                place_order(prv, "buy", buy_price)
                log(f"買い指値: {buy_price:,.0f}円 発注完了")
            except Exception as e:
                log(f"買い発注失敗: {e}")

            # 4. 売り指値（GMO価格 + スプレッド）
            sell_price = gmo_price + SPREAD
            try:
                place_order(prv, "sell", sell_price)
                log(f"売り指値: {sell_price:,.0f}円 発注完了")
            except Exception as e:
                log(f"売り発注失敗: {e}")

            # 5. 待機
            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"エラー: {e}")
            time.sleep(INTERVAL)

# ============================================
# 起動 / 終了処理
# ============================================
if __name__ == "__main__":
    prv = None
    try:
        main()
    except KeyboardInterrupt:
        log("")
        log("停止シグナル受信 → 全注文キャンセル中...")
        try:
            prv = python_bitbankcc.private(BITBANK_API_KEY, BITBANK_API_SECRET)
            cancelled = cancel_all_orders(prv)
            log(f"全注文キャンセル完了（{cancelled}件）")
        except Exception as e:
            log(f"終了時キャンセル失敗: {e}")
        log("Bot停止")
