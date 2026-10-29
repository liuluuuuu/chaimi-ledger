"""统一日志（评估 R-09）：stdout（fnOS 捕获为 run/info.log）+ 本地轮转文件双写。

原先全后端 print（30 处）：无级别、无轮转、无法区分来源。改造后：
  - getLogger("chaimi.xxx") 按 INFO 级输出，格式带时间/级别/模块；
  - 轮转文件落在 config.log_path()（飞牛 @var / 本地 app/data），2MB×3 份；
  - /api/logs/download 一并打包该文件（见 server.logs_download）。
"""
from __future__ import annotations

import logging
import logging.handlers
import sys

import config

_fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")


def setup() -> logging.Logger:
    """初始化 "chaimi" 根 logger（幂等，进程内首个 import 方调用即可）。"""
    log = logging.getLogger("chaimi")
    log.setLevel(logging.INFO)
    if not log.handlers:
        sh = logging.StreamHandler(sys.stdout)   # fnOS 捕获 stdout → info.log，收集链路不变
        sh.setFormatter(_fmt)
        log.addHandler(sh)
        try:
            p = config.log_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(p, maxBytes=2_000_000, backupCount=2,
                                                      encoding="utf-8")
            fh.setFormatter(_fmt)
            log.addHandler(fh)
        except OSError:
            pass                                 # 日志目录不可写：退化为仅 stdout，不阻断启动
    return log


def get(name: str) -> logging.Logger:
    """业务模块取子 logger：applog.get("bot-ilink") → "chaimi.bot-ilink"。"""
    return logging.getLogger("chaimi." + name)
