#!/usr/bin/env python3
"""
crypto-mm-bot テスト
ネットワークは使わず mock/fixture で検証する。
  1. bitbank Private GET 署名が nonce + "/v1" + path であること
  2. API失敗（HTTP異常/success!=1）で ApiError になること
  3. 取消失敗時は失敗IDが返る（=新規発注禁止の判定材料）こと
  4. 自Bot注文IDだけが取消され、手動/他Bot注文を触らないこと
  5. bot_config.json の範囲外値を拒否すること
  6. live 起動条件（APIキー + live_confirmed + 安全設定）のチェック
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
# 3+4. 自Bot注文のみ取消し / 取消失敗
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
        self.cancelled.append(order_id)
        return {}


def test_cancel_only_own_orders(tmp_path):
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    own.ids = {101, 303}
    api = FakeAPI()
    cancelled, failed = bot_v2.cancel_own_orders(api, "eth_jpy", own)
    assert sorted(api.cancelled) == [101, 303]
    assert cancelled == 2
    assert failed == []
    assert 202 not in api.cancelled  # 手動/他Bot注文は触らない
    assert own.ids == set()


def test_cancel_failure_reported_and_order_kept(tmp_path):
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    own.ids = {101, 303}
    api = FakeAPI(fail_on={303})
    cancelled, failed = bot_v2.cancel_own_orders(api, "eth_jpy", own)
    assert cancelled == 1
    assert failed == [303]          # 失敗IDが返る → 呼び出し側は新規発注禁止にする
    assert own.ids == {303}         # 失敗した注文は台帳に残る


def test_stale_own_id_dropped_without_cancel(tmp_path):
    """active に無い自注文IDは約定済みとして台帳から外れ、取消APIは呼ばれない"""
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    own.ids = {999}
    api = FakeAPI()
    cancelled, failed = bot_v2.cancel_own_orders(api, "eth_jpy", own)
    assert cancelled == 0 and failed == []
    assert api.cancelled == []
    assert own.ids == set()


def test_active_orders_fetch_failure_blocks(tmp_path):
    """active_orders 自体が取れない場合は全ID失敗扱い（=新規発注禁止）"""
    own = bot_v2.OwnOrders(str(tmp_path / "own.json"))
    own.ids = {101}

    class FailAPI:
        def get_active_orders(self, pair):
            raise bot_v2.ApiError("network down")

    cancelled, failed = bot_v2.cancel_own_orders(FailAPI(), "eth_jpy", own)
    assert cancelled == 0
    assert failed == [101]


# -------------------------------------------
# 5. 設定検証
# -------------------------------------------
def _write_cfg(tmp_path, **over):
    cfg = dict(bot_v2.DEFAULT_CONFIG)
    cfg.update(over)
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


def test_config_bad_symbol_rejected(tmp_path):
    with pytest.raises(bot_v2.ConfigError):
        bot_v2.load_config(_write_cfg(tmp_path, symbol="doge_jpy"))


def test_config_broken_json_rejected(tmp_path):
    p = tmp_path / "bot_config.json"
    p.write_text("{broken")
    with pytest.raises(bot_v2.ConfigError):
        bot_v2.load_config(str(p))


# -------------------------------------------
# 6. live 起動条件
# -------------------------------------------
def _live_cfg(**over):
    cfg = dict(bot_v2.DEFAULT_CONFIG)
    cfg.update(over)
    return cfg


def test_live_requires_keys():
    reasons = bot_v2.check_live_preconditions(_live_cfg(live_confirmed=True), "", "")
    assert any("APIキー" in r for r in reasons)


def test_live_requires_confirmation():
    reasons = bot_v2.check_live_preconditions(_live_cfg(live_confirmed=False), "k", "s")
    assert any("明示確認" in r for r in reasons)


def test_live_ok_when_all_conditions_met():
    reasons = bot_v2.check_live_preconditions(_live_cfg(live_confirmed=True), "k", "s")
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
