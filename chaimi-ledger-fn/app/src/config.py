"""配置：JSON 文件（飞牛 PKGETC 持久化目录）+ 环境变量覆盖。

业务配置（LLM Key 等）走配置文件 chaimi.conf；路径来自 TRIM_* 环境变量，
本地开发回退到源码目录。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
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
    # 白名单（扫码自动加入）；ilink 为 A 通道扫码凭证列表（一账本一用户一机器人），
    # 消费方只见列表形态——旧单 dict 由读侧 _norm_ilink 归一。
    # confirm_mode=记账确认模式：reply=回草稿卡等「确认」入库（默认，有错先改再入）；
    # auto=识别即直接入库（错账靠「撤销/撤销N笔」兜底）
    "bot": {
        "mode": "off",
        "confirm_mode": "reply",
        "allowed_users": [],
        "ilink": []
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
    backup_dir 回退 @appdata。该变量仅 fnOS(Linux) 注入；Windows 上按 ':' 切分
    会把 C:\\share 劈成盘符+相对路径导致永远定位失败，直接跳过。"""
    if os.name != "posix":
        return None
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
    return _pkgvar_or_data("backups")   # 本地开发回退，与 db_path 等同源


# load() 结果按文件签名缓存：鉴权中间件每请求、批处理 worker 每批都会 load()，免重复读盘
# 解析。签名含 inode——save() 的 os.replace 必然换签名，外部手改也会变 mtime/size
_cache: dict[str, tuple] = {}


def _norm_ilink(raw) -> list:
    """bot.ilink 读侧归一（单点）：旧版单 dict（多账本功能之前的形式）升为单元素列表，
    缺 ledger 补 1——老版本单机器人绑定的就是当时的唯一账本，此缺省仅存在于该
    迁移路径，现行写入方（扫码落凭证）一律显式带账本；无 token 条目滤除
    （出厂默认/手改残留不得凭形态豁免成为"幽灵绑定"）。归一在读取边界完成后，
    消费方（ilink_entries/get_config/apply）不再各自兼容 dict 形态。"""
    if isinstance(raw, dict):
        raw = [dict(raw, ledger=raw.get("ledger") or 1)]
    return [e for e in raw or [] if isinstance(e, dict) and e.get("token")]


def _read(p: Path) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if p.exists():
        try:
            saved = json.loads(p.read_text(encoding="utf-8"))
            for k, v in saved.items():
                if isinstance(cfg.get(k), dict):
                    if isinstance(v, dict):
                        cfg[k].update(v)
                    # 坏形态（手改把 llm/bot 等写成字符串/列表）：该节整体保默认——
                    # 原样混入会让下游 .get/.update 抛 AttributeError（/api/config 500、AI 链路全断）
                else:
                    cfg[k] = v
        except (json.JSONDecodeError, OSError) as e:
            # 结果按文件签名缓存，每个坏版本只告警一次；此时在设置页保存会用默认值覆盖原配置
            logging.getLogger("chaimi.config").warning(f"配置文件读取失败，按默认值运行：{e}")
    bot = cfg.get("bot")
    if isinstance(bot, dict):
        bot["ilink"] = _norm_ilink(bot.get("ilink"))
        # confirm_mode 只认两个枚举值：手改坏值回落 reply（静默按确认模式跑，
        # 比按未知值炸掉/误入 auto 直入都安全）
        if bot.get("confirm_mode") not in ("reply", "auto"):
            bot["confirm_mode"] = "reply"
    bk = cfg.get("backup")
    if isinstance(bk, dict):
        # 旧版保留策略字段（现行是 keep_months/keep_years）：读取边界清出，
        # 下一次任意 save 即不再落盘
        bk.pop("keep", None)
    return cfg


def load() -> dict:
    p = conf_path()
    try:
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        sig = None
    hit = _cache.get(str(p))
    if hit is None or hit[0] != sig:
        hit = _cache[str(p)] = (sig, _read(p))
    return copy.deepcopy(hit[1])   # 调用方会就地改写再 save()，必须给副本


_save_lock = threading.Lock()


def write_raw(data: bytes) -> None:
    """原子写配置文件（save / 备份恢复共用）：tmp + os.replace，并发 load() 不会读到半截
    JSON 而静默回退 DEFAULTS。Windows 上目标正被读句柄占用时 os.replace 抛 PermissionError
    （读句柄无 FILE_SHARE_DELETE），短重试；仍失败则直写兜底（非原子，但不丢这次保存）。"""
    p = conf_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _save_lock:
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        for i in range(10):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                time.sleep(0.02 * (i + 1))
        tmp.unlink(missing_ok=True)
        # 直写失去原子性（并发 load 可能读到半截 JSON 回退默认值）：留痕可排查，
        # 但不丢这次保存——生产环境（fnOS/Linux）不会走到这里
        logging.getLogger("chaimi.config").warning("配置原子写重试失败，改为直写（非原子，本次保存不丢）")
        p.write_bytes(data)


def save(cfg: dict) -> None:
    write_raw(json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8"))


# 读-改-写全程锁：save() 只保证单次写原子，两个并发的「load→改→save」交错时，后写者会用
# 自己手里的旧快照整体覆盖前者（丢刚落的扫码凭证/刚保存的 Key/刚切的开关，静默无告警）。
# 业务的 conf 写入方一律走 edit()；save() 仅供备份恢复等「整份覆写」场景
_edit_lock = threading.RLock()   # 可重入：嵌套 edit() 不自锁


@contextmanager
def edit():
    """conf 读-改-写事务：进入时 load 最新副本，正常退出落盘，异常退出不写。
    with config.edit() as cfg: cfg["llm"]["model"] = ...   # 退出即保存"""
    with _edit_lock:
        cfg = load()
        try:
            yield cfg
        except BaseException:
            raise                      # 业务异常：不落盘
        save(cfg)


def replace_raw(data: bytes) -> None:
    """整份覆写（备份恢复 conf 回填专用）：持 _edit_lock 与 edit() 互斥——裸 write_raw
    只保证单次写原子，restore 落盘与在途 edit() 的 load→save 交错时，edit() 退出会用
    旧快照整体覆盖刚恢复的凭证/配置（注释 177-179 描述的事故形态，restore 是唯一旁路）。"""
    with _edit_lock:
        write_raw(data)
