#!/usr/bin/env python3
"""隔离模式入口：以包模块方式运行采集器 CLI。

用 ``python -I run_collector.py ...`` 运行：``-I`` 忽略环境变量且不把
当前工作目录加入 sys.path，避免在数据目录里被植入的同名模块（如
``json.py``）劫持导入；这里显式加入的是脚本所在的项目根目录，即受信任
的代码目录，再以 ``src.collector`` 包模块身份启动，保留包内相对导入。
"""
import os
import runpy
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

if __name__ == "__main__":
    runpy.run_module("src.collector", run_name="__main__")
