"""备份/恢复：sqlite3 backup API（WAL 下不能直接拷文件，DESIGN 决策4）+ 配置副本。

自动备份分层（祖父-父-子简化版）：
- 月备：每月最后一天生成 chaimi-ledger-YYYYMM.db（同月重复执行则覆盖）；
- 年备：每年 12 月 31 日生成 chaimi-ledger-YYYY.db（当天年备优先，跳过月备）；
- 保留策略只清理上述周期文件（按文件名位数识别），手动备份不受影响。
"""
from __future__ import annotations

import calendar
import re
import sqlite3
import threading
import time
import zipfile
from datetime import datetime

import applog
import config
import db

_log = applog.get("backup")

_SIDE_SUFFIXES = (".conf.json", ".receipts.zip")

def _valid_name(name: str) -> bool:
    """备份文件名安全校验（restore/delete/download 共用）：
    chaimi- 前缀白名单 + .db 后缀限定——副文件（.conf.json 含明文 API Key）
    与任意改名文件一律不可经这三个入口触达（副文件只随主文件联动，见 _SIDE_SUFFIXES）。"""
    return (name.startswith("chaimi-") and name.endswith(".db")
            and "/" not in name and "\\" not in name and ".." not in name)

def _locate(name: str):
    """校验文件名安全并定位备份主文件（restore/delete/download 共用）；不存在抛 FileNotFoundError。"""
    if not _valid_name(name):
        raise ValueError("非法备份文件名")
    src_file = config.backup_dir() / name
    if not src_file.exists():
        raise FileNotFoundError("备份不存在")
    return src_file

def create_backup(period: str = "") -> dict:
    """period: ""=手动(完整时间戳) "monthly"=YYYYMM "yearly"=YYYY。
    文件名统一 chaimi-ledger-<时间戳>.db/.conf.json，手动与自动仅时间戳粒度不同。
    小票一并备份：打包为同名 .receipts.zip（目录为空则不生成）。"""
    bdir = config.backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    if period == "monthly":
        stamp = now.strftime("%Y%m")
    elif period == "yearly":
        stamp = now.strftime("%Y")
    else:
        stamp = now.strftime("%Y%m%d_%H%M%S")
    dest = bdir / f"chaimi-ledger-{stamp}.db"
    src = sqlite3.connect(config.db_path())
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    # 配置副本
    conf = config.conf_path()
    if conf.exists():
        (bdir / f"chaimi-ledger-{stamp}.conf.json").write_bytes(conf.read_bytes())
    rdir = config.receipts_dir()
    rfiles = [f for f in rdir.iterdir() if f.is_file()] if rdir.exists() else []
    if rfiles:
        with zipfile.ZipFile(bdir / f"chaimi-ledger-{stamp}.receipts.zip", "w", zipfile.ZIP_STORED) as z:
            for f in rfiles:
                z.write(f, f.name)
    return {"file": str(dest), "size": dest.stat().st_size}

def list_backups() -> list[dict]:
    bdir = config.backup_dir()
    out = []
    if bdir.exists():
        for f in sorted(bdir.glob("chaimi-*.db"), reverse=True):
            out.append({"file": f.name, "size": f.stat().st_size,
                        "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
    return out

def restore_backup(name: str) -> None:
    """恢复：校验文件名安全 + integrity_check 通过才覆盖。"""
    src_file = _locate(name)
    # 先校验备份完整性；非 SQLite 文件统一转 ValueError（server 映射 400，不再裸 500）
    chk = sqlite3.connect(src_file)
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError as e:
        raise ValueError(f"备份文件不是有效的 SQLite 数据库: {e}") from e
    finally:
        chk.close()
    if not ok:
        raise ValueError("备份文件完整性校验失败")
    # 旧实现"临时文件替换 db"在 Linux 下各线程缓存的旧连接仍读旧 inode（恢复"看似没生效"的根因），
    # 在 Windows 下直接被旧句柄锁死无法替换；backup 写同一文件，所有连接立即可见
    target = config.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(src_file)
    dst = sqlite3.connect(target, timeout=10)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    db.reset_connections()   # 防御：作废线程缓存连接（清各自的 schema/page 缓存）
    # 配置副本一并恢复（AI API Key 等随备份回到备份时点）
    conf_backup = src_file.with_suffix(".conf.json")
    if conf_backup.exists():
        try:
            # 原子写（tmp+os.replace）：并发 load() 不会读到半截 JSON 而静默回退默认值；
            # 配置缓存按文件签名自动失效，无需额外处理
            config.write_raw(conf_backup.read_bytes())
        except OSError as e:
            _log.warning(f"配置恢复失败(忽略): {conf_backup.name}: {e}")
    receipts_backup = config.backup_dir() / (src_file.stem + ".receipts.zip")
    if receipts_backup.exists():
        try:
            rdir = config.receipts_dir()
            rdir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(receipts_backup) as z:
                for n in z.namelist():
                    if "/" not in n and ".." not in n:   # 只收平铺文件名，防路径逃逸
                        z.extract(n, rdir)
        # 与 conf 恢复同款 best-effort：BadZipFile 不是 OSError 子类，漏捕会让恢复在
        # db/conf 已生效后裸抛 500（部分恢复）；坏 zip 记 warning 后继续
        except (OSError, zipfile.BadZipFile) as e:
            _log.warning(f"小票恢复失败(忽略): {receipts_backup.name}: {e}")

def delete_backup(name: str) -> None:
    """删除备份（.db 与同名副文件一起删）。"""
    src_file = _locate(name)
    src_file.unlink()
    for suffix in _SIDE_SUFFIXES:
        src_file.with_suffix(suffix).unlink(missing_ok=True)

_MONTH_FILE = re.compile(r"^chaimi-ledger-(\d{6})\.db$")
_YEAR_FILE = re.compile(r"^chaimi-ledger-(\d{4})\.db$")

def _cleanup_old(keep: int, now: datetime, *, yearly: bool) -> None:
    """周期备份按保留数清理（月备按月数、年备按年数）：
    删除过期的主文件与全部同名副文件（.conf.json/.receipts.zip，两侧同一口径）。"""
    if keep <= 0:
        return
    pat = _YEAR_FILE if yearly else _MONTH_FILE
    for f in config.backup_dir().glob("chaimi-ledger-*.db"):
        m = pat.match(f.name)
        if not m:
            continue
        n = int(m.group(1))
        age = (now.year - n) if yearly else (now.year * 12 + now.month) - (n // 100 * 12 + n % 100)
        if age >= keep:
            f.unlink(missing_ok=True)
            for suffix in _SIDE_SUFFIXES:
                f.with_suffix(suffix).unlink(missing_ok=True)

def _is_last_day_of_month(now: datetime) -> bool:
    return now.day == calendar.monthrange(now.year, now.month)[1]

class AutoBackup:
    """自动备份：到配置 hour 点检查——年末(12-31)优先生成年备并跳过月备；
    其余月末生成本月月备。保留按月数/年数分层清理，只清周期文件。
    每轮重读配置，设置页改参数即时生效；备份成功才记当日已备（失败下一轮重试）。"""

    def __init__(self):
        self._last_date = ""
        self.last_result: dict = {"error": "尚未备份"}

    def start(self) -> None:
        threading.Thread(target=self._loop, name="autobackup", daemon=True).start()

    def _loop(self) -> None:
        while True:
            self._check(datetime.now())
            time.sleep(60)

    def _check(self, now: datetime) -> None:
        """到点检查一轮（_loop 每分钟调一次，每轮重读配置，设置页改参数即时生效）：
        enabled 且 hour 点且今天未备过且月末 → 年末优先生成年备，否则生成本月月备。
        备份成功才置 _last_date：瞬时失败（如撞库锁）本轮记 error，下一分钟重试，
        不会整月丢月备；_last_date 在 create_backup 之后、_cleanup_old 之前置位，
        清理失败也不会同一天重复备份（周期文件名同戳，重复执行即整份重写）。"""
        cfg = config.load().get("backup", {})
        try:
            today = now.strftime("%Y-%m-%d")
            if (cfg.get("enabled") and int(cfg.get("hour", 3)) == now.hour
                    and self._last_date != today
                    and _is_last_day_of_month(now)):
                if now.month == 12 and now.day == 31:
                    # 12-31 年备优先（年备已覆盖当月），跳过月备
                    if int(cfg.get("keep_years", 3)) > 0:
                        self.last_result = create_backup("yearly")
                        self._last_date = today
                        _cleanup_old(int(cfg.get("keep_years", 3)), now, yearly=True)
                elif int(cfg.get("keep_months", 3)) > 0:
                    self.last_result = create_backup("monthly")
                    self._last_date = today
                    _cleanup_old(int(cfg.get("keep_months", 3)), now, yearly=False)
        except Exception as e:  # 备份失败不中断循环
            self.last_result = {"error": str(e)[:300]}
