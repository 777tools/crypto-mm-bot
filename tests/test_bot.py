#!/usr/bin/env python3
"""
crypto-mm-bot テスト
ネットワークは使わず mock/fixture で検証する。
  1. bitbank Private GET 署名が nonce + "/v1" + path であること
  2. API失敗（HTTP異常/success!=1）で ApiError になること
  3. 取消失敗時は失敗IDが返る（=新規発注禁止の判定材料）こと
  4. 自Bot注文IDだけが取消され、手動/他Bot注文を触らないこと（pair単位）
  5. bot_config.json の範囲外値・型不正・非ETH symbolを拒否すること
  6. live 起動条件（APIキー + live_confirmed + 安全設定）のチェック
  7. post_only が注文に載ること
  8. legacy危険経路（bot.py / start.bat）が存在しないこと
  9. 台帳: pair別永続化・書込失敗時の即時cancel・order_id欠落は成功扱いしない
 10. 日次損失baselineの永続化（再起動でリセットしない）
 11. max_position境界（保有+新規 <= 上限）
 12. 発注失敗時はcycle中断（買い失敗後に売りを出さない）・売りにも注文額上限
 13. bool設定の厳格検証（文字列 "false" を True にしない）
"""

import hashlib
import hmac
import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_v2

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg(**over):
    cfg = dict(bot_v2.DEFAULT_CONFIG)
    cfg.update(over)
    return cfg


# -------------------------------------------
# 1. 署名テスト
# -------------------------------------------
def test_get_signature_contains_v1():
    api = bot_v2.BitbankAPI("testkey", "testsecret")
    captured = {}

    class FakeRes:
        status_code = 200
        text = '{"success":1,"data":{"assets":[]}}'
        def json(self):
            return {"success": 1, "data": {"assets": []}}

    def fake_get(url, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        return FakeRes()

    with mock.patch("bot_v2.requests.get", side_effect=fake_get):
        api._get("/user/spot/assets")

    nonce = captured["headers"]["ACCESS-NONCE"]
    expected = hmac.new(
        b"testsecret", (nonce + "/v1/user/spot/assets").encode(), hashlib.sha256
    ).hexdigest()
    assert captured["headers"]["ACCESS-SIGNATURE"] == expected
    assert captured["headers"]["ACCESS-KEY"] == "testkey"
    assert captured["url"] == "https://api.bitbank.cc/v1/user/spot/assets"


# -------------------------------------------
# 2. API失敗の検証
# -------------------------------------------
def _res(status, payload, text=""):
    r = mock.Mock()
    r.status_code = status
    r.text = text or json.dumps(payload)
    r.json.return_value = payload
    return r


def test_http_error_raises():
    api = bot_v2.BitbankAPI("k", "s")
    with mock.patch("bot_v2.requests.get", return_value=_res(500, {}, "server error")):
        with pytest.raises(bot_v2.ApiError):
            api._get("/user/spot/assets")


def test_success_not_1_raises():
    api = bot_v2.BitbankAPI("k", "s")
    with mock.patch("bot_v2.requests.get", return_value=_res(200, {"success": 0, "data": {"code": 10001}})):
        with pytest.raises(bot_v2.ApiError):
            api._get("/user/spot/assets")


def test_empty_keys_rejected():
    with pytest.raises(bot_v2.ApiError):
        bot_v2.BitbankAPI("", "")


# -------------------------------------------
# 3+4. 自Bot注文のみ取消し / 取消失敗（pair単位）
# -------------------------------------------
class FakeAPI:
    """active_orders に [101(自), 202(手動), 303(自)] を返す fake"""
    def __init__(self, fail_on=None):
        self.cancelled = []
        self.fail_on = fail_on or set()

    def get_active_orders(self, pair):
        return {"orders": [{"order_id": 101}, {"order_id": 202}, {"order_id": 303}]}

    def cancel_order(self, pair, order_id):
        if order_id in self.fail_on:
            raise bot_v2.ApiError("cancel failed")
        self.cancelled.append((pair, order_id))
        return {}


def test_cancel_only_own_orders(tmp_path):
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    own.by_pair = {"eth_jpy": {101, 303}}
    api = FakeAPI()
    cancelled, failed = bot_v2.cancel_own_orders(api, "eth_jpy", own)
    assert sorted(oid for _, oid in api.cancelled) == [101, 303]
    assert cancelled == 2
    assert failed == []
    assert 202 not in [oid for _, oid in api.cancelled]  # 手動/他Bot注文は触らない
    assert own.by_pair == {}


def test_cancel_failure_reported_and_order_kept(tmp_path):
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    own.by_pair = {"eth_jpy": {101, 303}}
    api = FakeAPI(fail_on={303})
    cancelled, failed = bot_v2.cancel_own_orders(api, "eth_jpy", own)
    assert cancelled == 1
    assert failed == [303]          # 失敗IDが返る → 呼び出し側は新規発注禁止にする
    assert own.by_pair == {"eth_jpy": {303}}  # 失敗した注文は台帳に残る


def test_stale_own_id_dropped_without_cancel(tmp_path):
    """active に無い自注文IDは約定済みとして台帳から外れ、取消APIは呼ばれない"""
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    own.by_pair = {"eth_jpy": {999}}
    api = FakeAPI()
    cancelled, failed = bot_v2.cancel_own_orders(api, "eth_jpy", own)
    assert cancelled == 0 and failed == []
    assert api.cancelled == []
    assert own.by_pair == {}


def test_active_orders_fetch_failure_blocks(tmp_path):
    """active_orders 自体が取れない場合は全ID失敗扱い（=新規発注禁止）"""
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    own.by_pair = {"eth_jpy": {101}}

    class FailAPI:
        def get_active_orders(self, pair):
            raise bot_v2.ApiError("network down")

    cancelled, failed = bot_v2.cancel_own_orders(FailAPI(), "eth_jpy", own)
    assert cancelled == 0
    assert failed == [101]


def test_pair_isolation(tmp_path):
    """別pairの注文はキャンセル対象にならない。旧pairの注文は全pair取消で追跡できる"""
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    own.by_pair = {"eth_jpy": {101}, "btc_jpy": {777}}
    api = FakeAPI()
    bot_v2.cancel_own_orders(api, "eth_jpy", own)
    assert ("btc_jpy", 777) not in api.cancelled
    assert own.by_pair.get("btc_jpy") == {777}  # 旧pair注文は追跡されたまま

    # 全pair取消（停止時）。777 が旧pairで active として残っている想定
    class FakeAPI2(FakeAPI):
        def get_active_orders(self, pair):
            if pair == "btc_jpy":
                return {"orders": [{"order_id": 777}]}
            return super().get_active_orders(pair)

    api2 = FakeAPI2()
    own.by_pair = {"eth_jpy": {101}, "btc_jpy": {777}}
    c, f = bot_v2.cancel_own_orders_all_pairs(api2, own)
    assert c == 2
    assert ("btc_jpy", 777) in api2.cancelled


def test_own_orders_old_format_migrated(tmp_path):
    """旧形式（pairなしlist）の台帳は eth_jpy として引き継ぐ"""
    p = tmp_path / "own.json"
    p.write_text(json.dumps([101, 303]))
    own = bot_v2.OwnOrders(str(p))
    assert own.by_pair == {"eth_jpy": {101, 303}}


def test_own_orders_broken_ledger_raises(tmp_path):
    """壊れた台帳は握り潰さず例外（自注文を区別できないため）"""
    p = tmp_path / "own.json"
    p.write_text("{broken")
    with pytest.raises(bot_v2.OwnOrdersError):
        bot_v2.OwnOrders(str(p))


def test_own_orders_save_persists_pair(tmp_path):
    p = str(tmp_path / "own.json")
    own = bot_v2.OwnOrders(p)
    own.add("eth_jpy", 12345)
    own2 = bot_v2.OwnOrders(p)
    assert own2.by_pair == {"eth_jpy": {12345}}


# -------------------------------------------
# 5. 設定検証
# -------------------------------------------
def _write_cfg(tmp_path, **over):
    cfg = _cfg(**over)
    p = tmp_path / "bot_config.json"
    p.write_text(json.dumps(cfg))
    return str(p)


def test_config_valid_passes(tmp_path):
    cfg = bot_v2.load_config(_write_cfg(tmp_path))
    assert cfg["order_size"] == bot_v2.DEFAULT_CONFIG["order_size"]


def test_config_out_of_range_rejected(tmp_path):
    with pytest.raises(bot_v2.ConfigError):
        bot_v2.load_config(_write_cfg(tmp_path, order_size=999))


def test_config_negative_interval_rejected(tmp_path):
    with pytest.raises(bot_v2.ConfigError):
        bot_v2.load_config(_write_cfg(tmp_path, interval=0))


def test_config_bad_type_rejected(tmp_path):
    with pytest.raises(bot_v2.ConfigError):
        bot_v2.load_config(_write_cfg(tmp_path, spread_range="abc"))


def test_config_non_eth_symbol_rejected(tmp_path):
    """ETH/JPY 以外は厳格に拒否（GMO価格・残高がETH固定で安全装置が無効になるため）"""
    with pytest.raises(bot_v2.ConfigError):
        bot_v2.load_config(_write_cfg(tmp_path, symbol="btc_jpy"))


def test_config_broken_json_rejected(tmp_path):
    p = tmp_path / "bot_config.json"
    p.write_text("{broken")
    with pytest.raises(bot_v2.ConfigError):
        bot_v2.load_config(str(p))


# -------------------------------------------
# 13. bool設定の厳格検証（文字列 "false" を True にしない）
# -------------------------------------------
def test_config_string_false_post_only_rejected(tmp_path):
    with pytest.raises(bot_v2.ConfigError):
        bot_v2.load_config(_write_cfg(tmp_path, post_only="false"))


def test_config_string_false_live_confirmed_rejected(tmp_path):
    with pytest.raises(bot_v2.ConfigError):
        bot_v2.load_config(_write_cfg(tmp_path, live_confirmed="false"))


def test_config_true_bools_accepted(tmp_path):
    cfg = bot_v2.load_config(_write_cfg(tmp_path, post_only=True, live_confirmed=True))
    assert cfg["post_only"] is True and cfg["live_confirmed"] is True


# -------------------------------------------
# 6. live 起動条件
# -------------------------------------------
def test_live_requires_keys():
    reasons = bot_v2.check_live_preconditions(_cfg(live_confirmed=True), "", "")
    assert any("APIキー" in r for r in reasons)


def test_live_requires_confirmation():
    reasons = bot_v2.check_live_preconditions(_cfg(live_confirmed=False), "k", "s")
    assert any("明示確認" in r for r in reasons)


def test_live_ok_when_all_conditions_met():
    reasons = bot_v2.check_live_preconditions(_cfg(live_confirmed=True), "k", "s")
    assert reasons == []


# -------------------------------------------
# 7. post_only が注文に載ること
# -------------------------------------------
def test_order_post_only_flag():
    api = bot_v2.BitbankAPI("k", "s")
    bodies = []

    def fake_post(url, headers, json, timeout):
        bodies.append(json)
        return _res(200, {"success": 1, "data": {"order_id": 555}})

    with mock.patch("bot_v2.requests.post", side_effect=fake_post):
        api.order("eth_jpy", "500000", "0.05", "buy", "limit", post_only=True)
    assert bodies[0]["post_only"] is True


# -------------------------------------------
# 8. legacy危険経路が存在しないこと
# -------------------------------------------
def test_legacy_bot_py_removed():
    assert not os.path.exists(os.path.join(REPO, "bot.py"))


def test_legacy_start_bat_removed():
    assert not os.path.exists(os.path.join(REPO, "start.bat"))


# -------------------------------------------
# 9. 発注→台帳の整合（order_id欠落/保存失敗）
# -------------------------------------------
class OrderFakeAPI:
    def __init__(self, order_ret=None, cancel_fail=False):
        self.cancelled = []
        self.order_ret = order_ret if order_ret is not None else {"order_id": 555}
        self.cancel_fail = cancel_fail

    def order(self, pair, price, amount, side, order_type, post_only=False):
        return dict(self.order_ret)

    def cancel_order(self, pair, order_id):
        if self.cancel_fail:
            raise bot_v2.ApiError("cancel failed")
        self.cancelled.append((pair, order_id))
        return {}


def test_place_order_missing_order_id_rejected(tmp_path):
    """order_id欠落は成功扱いしない"""
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    api = OrderFakeAPI(order_ret={"unexpected": 1})
    with pytest.raises(bot_v2.ApiError):
        bot_v2.place_live_order(api, _cfg(), "buy", 500000, own)


def test_place_order_ledger_save_failure_cancels(tmp_path):
    """台帳保存失敗時は即その注文をcancelする"""
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    with mock.patch.object(bot_v2.OwnOrders, "save",
                           side_effect=bot_v2.OwnOrdersError("disk full")):
        api = OrderFakeAPI()
        with pytest.raises(bot_v2.OwnOrdersError):
            bot_v2.place_live_order(api, _cfg(), "buy", 500000, own)
        assert ("eth_jpy", 555) in api.cancelled  # 即時cancelされた


def test_place_order_ledger_failure_and_cancel_failure_critical(tmp_path):
    """台帳保存失敗かつcancelも失敗 → 致命的エラー（haltさせる）"""
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    with mock.patch.object(bot_v2.OwnOrders, "save",
                           side_effect=bot_v2.OwnOrdersError("disk full")):
        api = OrderFakeAPI(cancel_fail=True)
        with pytest.raises(bot_v2.OwnOrdersError) as ei:
            bot_v2.place_live_order(api, _cfg(), "buy", 500000, own)
        assert "手動取消" in str(ei.value)


# -------------------------------------------
# 10. 日次損失baselineの永続化
# -------------------------------------------
def test_daily_loss_persisted_across_restart(tmp_path):
    p = str(tmp_path / "risk.json")
    s1 = bot_v2.load_risk_state(p)
    hit = bot_v2.check_daily_loss(s1, 100000, 10000, path=p)
    assert hit is False
    # 損失発生
    hit = bot_v2.check_daily_loss(s1, 95000, 10000, path=p)
    assert hit is False
    assert s1["daily_loss"] == 5000
    # 「再起動」: ファイルから復元し、同日ならbaselineがリセットされない
    s2 = bot_v2.load_risk_state(p)
    assert s2["equity_date"] == s1["equity_date"]
    assert s2["equity_start"] == 100000
    hit = bot_v2.check_daily_loss(s2, 89000, 10000, path=p)
    assert hit is True  # 100000-89000=11000 >= 10000 で停止


def test_daily_loss_resets_only_on_new_day(tmp_path):
    p = str(tmp_path / "risk.json")
    s = bot_v2.load_risk_state(p)
    bot_v2.check_daily_loss(s, 100000, 10000, path=p)
    s["equity_date"] = "2000-01-01"  # 別日にする
    bot_v2.check_daily_loss(s, 80000, 10000, path=p)
    assert s["equity_start"] == 80000  # 新しい日のbaselineに更新


# -------------------------------------------
# 11+12. run_order_cycle（境界・cycle中断・売り上限）
# -------------------------------------------
class CycleAPI:
    def __init__(self, fail_sides=()):
        self.calls = []
        self.fail_sides = set(fail_sides)

    def order(self, pair, price, amount, side, order_type, post_only=False):
        self.calls.append(side)
        if side in self.fail_sides:
            raise bot_v2.ApiError(f"{side} failed")
        return {"order_id": 900 + len(self.calls)}

    def cancel_order(self, pair, order_id):
        return {}


def test_cycle_buy_failure_skips_sell(tmp_path):
    """買い発注が失敗したら同じcycleで売りを出さない"""
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    api = CycleAPI(fail_sides={"buy"})
    n, err = bot_v2.run_order_cycle(api, _cfg(max_position=10.0), 510000, 500000, 300,
                                    jpy_free=1000000, eth_total=1.0, own=own)
    assert api.calls == ["buy"]  # sell は呼ばれない
    assert err is not None


def test_cycle_sell_failure_reported(tmp_path):
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    api = CycleAPI(fail_sides={"sell"})
    n, err = bot_v2.run_order_cycle(api, _cfg(max_position=10.0), 510000, 500000, 300,
                                    jpy_free=1000000, eth_total=1.0, own=own)
    assert api.calls == ["buy", "sell"]
    assert "売り発注失敗" in err


def test_cycle_max_position_boundary(tmp_path):
    """保有+新規 <= 上限 なら発注、超えるなら見送り"""
    cfg = _cfg(max_position=0.10, order_size=0.05)
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    api = CycleAPI()
    # 0.05 + 0.05 = 0.10 <= 0.10 → 境界OK（買いが出る）
    bot_v2.run_order_cycle(api, cfg, 510000, 500000, 300,
                           jpy_free=1000000, eth_total=0.05, own=own)
    assert "buy" in api.calls
    # 0.06 + 0.05 = 0.11 > 0.10 → 買い見送り
    own2 = bot_v2.OwnOrders(str(tmp_path / "own2.json"))
    api2 = CycleAPI()
    bot_v2.run_order_cycle(api2, cfg, 510000, 500000, 300,
                           jpy_free=1000000, eth_total=0.06, own=own2)
    assert "buy" not in api2.calls


def test_cycle_sell_max_order_jpy(tmp_path):
    """売りにも1注文上限（max_order_jpy）を適用"""
    cfg = _cfg(max_order_jpy=1000)  # 500300*0.05=25015円 > 1000円
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    api = CycleAPI()
    bot_v2.run_order_cycle(api, cfg, 510000, 500000, 300,
                           jpy_free=1000000, eth_total=1.0, own=own)
    assert "sell" not in api.calls


def test_cycle_buy_max_order_jpy(tmp_path):
    cfg = _cfg(max_order_jpy=1000)
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    api = CycleAPI()
    bot_v2.run_order_cycle(api, cfg, 510000, 500000, 300,
                           jpy_free=1000000, eth_total=0.0, own=own)
    assert api.calls == []
