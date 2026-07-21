#!/usr/bin/env python3
"""
マーケットメイキング デモ起動ラッパー
bot_v2.py をシミュレーションモードで起動する。
状態ファイル(sim_state.json)の契約はbot_v2.pyと統一されている。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

if __name__ == "__main__":
    os.execv(sys.executable, [sys.executable, os.path.join(HERE, "bot_v2.py"), "--mode", "sim"])
