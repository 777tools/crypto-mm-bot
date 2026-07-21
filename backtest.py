#!/usr/bin/env python3
"""
マーケットメイキング バックテスト（シミュレーション）
GMOコインの過去価格を取得し、仮想の10万円でBotを動かしたらどうなったかを再現する。
"""

import requests
import time
from datetime import datetime, timedelta

# ============================================
# 設定
# ============================================
INITIAL_JPY = 100000         # 初期資金（円）
INITIAL_ETH = 0.0            # 初期ETH保有量
SPREAD = 500                 # 片側スプレッド（円）
ORDER_SIZE = 0.01            # 1回の注文量（ETH）
SYMBOL = "ETH"               # 通貨
DAYS = 7                     # 何日分シミュレーションするか

# ============================================
# GMOコインから価格履歴を取得（1分足）
# ============================================
def fetch_price_history(days=7):
    """GMO Public APIで過去の価格を取得"""
    prices = []

    # GMOのKline API（1分足）
    # 日付ごとに取得
    for d in range(days, 0, -1):
        target = datetime.now() - timedelta(days=d)
        date_str = target.strftime("%Y%m%d")

        url = f"https://api.coin.z.com/public/v1/klines?symbol={SYMBOL}&interval=5min&date={date_str}"
        try:
            res = requests.get(url, timeout=15)
            data = res.json()
            if data.get("status") == 0 and data.get("data"):
                for candle in data["data"]:
                    prices.append({
                        "time": datetime.fromtimestamp(int(candle["openTime"]) / 1000),
                        "open": float(candle["open"]),
                        "high": float(candle["high"]),
                        "low": float(candle["low"]),
                        "close": float(candle["close"]),
                    })
        except Exception as e:
            print(f"  {date_str} 取得失敗: {e}")

        time.sleep(0.3)

    return prices

# ============================================
# バックテスト実行
# ============================================
def run_backtest(prices):
    jpy = INITIAL_JPY
    eth = INITIAL_ETH
    total_trades = 0
    wins = 0
    losses = 0
    total_profit = 0
    trade_log = []
    buy_order = None   # 未約定の買い注文
    sell_order = None   # 未約定の売り注文

    print("=" * 60)
    print(f"バックテスト開始")
    print(f"初期資金: {INITIAL_JPY:,.0f}円 + {INITIAL_ETH} ETH")
    print(f"スプレッド: ±{SPREAD:,.0f}円 / 注文量: {ORDER_SIZE} ETH")
    print(f"データ: {len(prices)}本（5分足 × {DAYS}日）")
    print("=" * 60)
    print()

    for i, candle in enumerate(prices):
        price = candle["close"]
        low = candle["low"]
        high = candle["high"]

        # --- 約定チェック ---
        # 買い注文が刺さったか（安値が買い指値以下）
        if buy_order and low <= buy_order["price"]:
            cost = buy_order["price"] * buy_order["size"]
            if jpy >= cost:
                jpy -= cost
                eth += buy_order["size"]
                trade_log.append({
                    "time": candle["time"], "side": "BUY",
                    "price": buy_order["price"], "size": buy_order["size"],
                })
                total_trades += 1
            buy_order = None

        # 売り注文が刺さったか（高値が売り指値以上）
        if sell_order and high >= sell_order["price"]:
            if eth >= sell_order["size"]:
                jpy += sell_order["price"] * sell_order["size"]
                eth -= sell_order["size"]
                trade_log.append({
                    "time": candle["time"], "side": "SELL",
                    "price": sell_order["price"], "size": sell_order["size"],
                })
                total_trades += 1
            sell_order = None

        # --- 新規注文（10本に1回 = 約50分間隔でリバランス） ---
        if i % 10 == 0:
            buy_price = price - SPREAD
            sell_price = price + SPREAD

            # 買い注文（資金があれば）
            if jpy >= buy_price * ORDER_SIZE:
                buy_order = {"price": buy_price, "size": ORDER_SIZE}

            # 売り注文（ETHがあれば）
            if eth >= ORDER_SIZE:
                sell_order = {"price": sell_price, "size": ORDER_SIZE}

    # --- 最終評価 ---
    last_price = prices[-1]["close"] if prices else 0
    eth_value = eth * last_price
    total_value = jpy + eth_value
    profit = total_value - INITIAL_JPY
    roi = (profit / INITIAL_JPY) * 100

    # 往復利益を計算
    buys = [t for t in trade_log if t["side"] == "BUY"]
    sells = [t for t in trade_log if t["side"] == "SELL"]
    round_trips = min(len(buys), len(sells))
    round_trip_profit = 0
    for j in range(round_trips):
        diff = sells[j]["price"] - buys[j]["price"]
        round_trip_profit += diff * ORDER_SIZE
        if diff > 0:
            wins += 1
        else:
            losses += 1

    # --- 結果出力 ---
    print("=" * 60)
    print("【バックテスト結果】")
    print("=" * 60)
    print()
    print(f"期間: {prices[0]['time'].strftime('%Y/%m/%d')} ~ {prices[-1]['time'].strftime('%Y/%m/%d')}")
    print(f"ETH価格: {prices[0]['close']:,.0f}円 → {last_price:,.0f}円")
    print()
    print(f"--- 資産 ---")
    print(f"初期: {INITIAL_JPY:,.0f}円")
    print(f"最終: {total_value:,.0f}円（JPY {jpy:,.0f} + ETH {eth:.4f} × {last_price:,.0f}円 = {eth_value:,.0f}円）")
    print(f"損益: {'+' if profit >= 0 else ''}{profit:,.0f}円（{'+' if roi >= 0 else ''}{roi:.2f}%）")
    print()
    print(f"--- 取引 ---")
    print(f"総取引数: {total_trades}回")
    print(f"往復完了: {round_trips}回")
    print(f"勝ち: {wins}回 / 負け: {losses}回")
    print(f"往復利益: {'+' if round_trip_profit >= 0 else ''}{round_trip_profit:,.0f}円")
    print()

    # 直近の取引ログ
    print(f"--- 直近の取引（最大20件） ---")
    for t in trade_log[-20:]:
        side_mark = "🟢買" if t["side"] == "BUY" else "🔴売"
        print(f"  {t['time'].strftime('%m/%d %H:%M')} {side_mark} {t['price']:,.0f}円 × {t['size']} ETH")

    print()
    print("=" * 60)

    # 日別サマリー
    print("--- 日別損益 ---")
    daily = {}
    for t in trade_log:
        day = t["time"].strftime("%m/%d")
        if day not in daily:
            daily[day] = {"buy": 0, "sell": 0, "count": 0}
        daily[day]["count"] += 1
        if t["side"] == "BUY":
            daily[day]["buy"] += t["price"] * t["size"]
        else:
            daily[day]["sell"] += t["price"] * t["size"]

    for day, d in daily.items():
        day_profit = d["sell"] - d["buy"]
        bar = "+" * int(abs(day_profit) / 50) if day_profit != 0 else ""
        sign = "+" if day_profit >= 0 else "-"
        print(f"  {day}: {d['count']:2}回 {sign}{abs(day_profit):,.0f}円 {'🟢' if day_profit >= 0 else '🔴'} {bar}")

    print("=" * 60)

# ============================================
# 実行
# ============================================
if __name__ == "__main__":
    print("GMOコインから過去価格を取得中...")
    prices = fetch_price_history(DAYS)
    if len(prices) < 10:
        print("価格データが不足しています。")
    else:
        print(f"{len(prices)}本取得完了")
        print()
        run_backtest(prices)
