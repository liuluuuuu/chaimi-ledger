"""配置：JSON 文件（飞牛 PKGETC 持久化目录）+ 环境变量覆盖。

业务配置（LLM Key 等）走配置文件 chaimi.conf；路径来自 TRIM_* 环境变量，
本地开发回退到源码目录。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS: dict = {
    # 单平台配置：文本模型(model) + 视觉模型(vl_model，图片记账用，留空则图片记账不可用)。
    "llm": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "vl_model": "qwen3-vl-plus",
        "api_key": "",
        "vl_base_url": "",
        "vl_api_key": "",
    },
    # 自动备份（月末/年末分层）：hour 点检查；月末生成本月月备，12-31 年备优先；
    # keep_months/keep_years 为保留的月数/年数，0=关闭该周期
    "backup": {
        "enabled": True,
        "hour": 3,
        "keep_months": 3,
        "keep_years": 3,
    },
    # 微信机器人（仅 A 通道 iLink 直连；B 通道网关/MCP 已下线）。allowed_users=微信ID
    # 白名单（扫码自动加入）；ilink 为 A 通道扫码凭证（一账本一用户一机器人）
    "bot": {
        "mode": "off",
        "allowed_users": [],
        "ilink": {"account_id": "", "token": "", "base_url": ""}
    },
    "theme": "light",
    # 自定义导入模板（向导写入/内置模板复制；import_bill.user_templates 消费，路由优先于内置）
    "import_templates": [],
    # Web 界面可选 Basic Auth（评估 R-04 中期项；默认关——内网自托管依赖 fnOS 网关边界，见 README 部署前提）。
    # enabled=true 时除 /healthz 外全站过门（浏览器原生弹窗，输一次即记）；用户名/密码常量时间比较。
    "web_auth": {"enabled": False, "username": "chaimi", "password": ""},
}

TRIM_PKGETC = os.environ.get("TRIM_PKGETC", "")
TRIM_PKGVAR = os.environ.get("TRIM_PKGVAR", "")
TRIM_GATEWAY_PREFIX = os.environ.get("TRIM_GATEWAY_PREFIX", "/app/chaimi-ledger")


def conf_path() -> Path:
    if os.environ.get("CHAIMI_CONF"):
        return Path(os.environ["CHAIMI_CONF"])
    if TRIM_PKGETC:
        return Path(TRIM_PKGETC) / "chaimi.conf"
    return Path(__file__).resolve().parent / "chaimi.conf"


def db_path() -> Path:
    if os.environ.get("CHAIMI_DB"):
        return Path(os.environ["CHAIMI_DB"])
    return _pkgvar_or_data("chaimi.db")


def _pkgvar_or_data(rel: str) -> Path:
    """@var 下运行时产物（飞牛持久化目录），本地开发回落 app/data/。"""
    if TRIM_PKGVAR:
        return Path(TRIM_PKGVAR) / rel
    return Path(__file__).resolve().parent.parent / "data" / rel

def receipts_dir() -> Path:
    return _pkgvar_or_data("receipts")

def log_path() -> Path:
    """应用自身运行日志（applog 轮转文件，logs_download 一并收集）。"""
    return _pkgvar_or_data("chaimi-ledger.log")


def app_share_dir() -> Path | None:
    """应用文件夹（data-share 声明，见 config/resource）定位，参考 r1toolbox：
    环境变量 TRIM_DATA_SHARE_PATHS——fnOS 由 data-share 声明注入的实际路径
    （冒号分隔，取第一个存在的目录）。未注入（本地开发）返回 None，
    backup_dir 回退 @appdata。"""
    for p in os.environ.get("TRIM_DATA_SHARE_PATHS", "").split(":"):
        p = p.strip()
        if p and Path(p).is_dir():
            return Path(p)
    return None


def backup_dir() -> Path:
    """备份目录：应用文件夹下的 backups/（用户在文件中心可见、可直取）；
    无应用文件夹（本地开发/未授权）时回退 @appdata。"""
    share = app_share_dir()
    if share:
        bdir = share / "backups"
        try:
            bdir.mkdir(parents=True, exist_ok=True)
            return bdir
        except OSError:
            pass
    if TRIM_PKGVAR:
        return Path(TRIM_PKGVAR) / "backups"
    return Path(__file__).resolve().parent.parent / "data" / "backups"


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    p = conf_path()
    if p.exists():
        try:
            saved = json.loads(p.read_text(encoding="utf-8"))
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save(cfg: dict) -> None:
    p = conf_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
