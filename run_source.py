#!/usr/bin/env python3
"""隔离模式入口：以包模块方式运行本机演练 IMAP 服务器。"""
import os
import runpy
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

if __name__ == "__main__":
    runpy.run_module("src.source", run_name="__main__")
