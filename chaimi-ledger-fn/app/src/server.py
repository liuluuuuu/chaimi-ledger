"""chaimi-server：FastAPI 入口（记账 CRUD / 统计 / 导入 / 映射 / 备份 / AI，DESIGN 五章）。

运行形态：
  飞牛：  server.py --uds ${TRIM_APPDEST}/app.sock    经统一网关 /app/chaimi-ledger 访问
  本地：  server.py --host 127.0.0.1 --port 18710     开发调试
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
from contextlib import asynccontextmanager
import base64
import csv
import io
import os
import re
import secrets
import time
from urllib.parse import quote
import threading
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from decimal import Decimal, InvalidOperation

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response, StreamingResponse)
from pydantic import BaseModel

import ai
import applog
import backup
import bot_core
import bot_ilink
import config
import db as dbm
import import_bill as ib
import import_classify as ic
import platform_templates as pt

applog.setup()   # 进程内首个初始化点：后续 get() 的子 logger 统一走双 handler
log = applog.get("server")

PREFIX = config.TRIM_GATEWAY_PREFIX
_STATIC = Path(__file__).resolve().parent.parent / "static"
# 应用图片（头像等，与桌面图标同目录）：后期更换头像直接替换文件即可（路由禁缓存，替换后刷新生效）
_IMAGES = Path(__file__).resolve().parent.parent / "server" / "images"

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/停机钩子（lifespan，替代已弃用的 on_event）。"""
    dbm.init_db()
    _auto_backup.start()
    _bot_apply()
    # 应用文件夹定位诊断：找不到时备份回退 @appdata，日志留痕便于排查
    bot_core._on_change = _bump_rev          # bot 记账/改动 → 唤醒长轮询 → 前端自刷新
    share = config.app_share_dir()
    log.info(f"应用文件夹: {share or '未找到（备份回退 @appdata/backups）'}")
    log.info(f"chaimi-ledger v{_app_version()}")   # 版本留痕:确认线上跑的是哪个包
    log.info(f"booking rules v{ai.RULES_VERSION}")  # 记账规则版本留痕（ai.py 模块级打印早于
    # applog.setup 不落盘，故由这里补打；见 ai.py RULES_VERSION 注释）
    # 模型自检走后台线程:服务商下线旧模型(如百炼一代 qwen-vl-*)时自动换新,不阻塞启动
    def _model_check():
        try:
            for line in ai.ensure_models():
                log.info(line)
        except Exception as e:
            log.warning(f"模型自检失败: {e}")
    threading.Thread(target=_model_check, daemon=True).start()
    # 小票孤儿文件兜底清理:启动即扫一次(顺带清历史遗留),之后每日一次
    def _sweep():
        while True:
            try:
                sweep_orphan_receipts()
            except Exception as e:
                log.warning(f"小票兜底清理异常: {e}")
            time.sleep(24 * 3600)
    threading.Thread(target=_sweep, daemon=True, name="receipt-sweep").start()
    yield
    _ilink.stop_all()

app = FastAPI(title="柴米记账", lifespan=lifespan)

_AUTH_EXEMPT = ("/healthz",)   # NAS 探活放行；其余（含静态资源，浏览器记凭证后自动携带）一律过门

def _basic_auth_ok(authorization: str, cfg: dict) -> bool:
    """Basic Auth 常量时间校验（评估 R-04 中期项，默认关闭，见 README 部署前提）。"""
    if not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, sep, pwd = decoded.partition(":")
    cfg_user = str(cfg.get("username") or "")
    cfg_pwd = str(cfg.get("password") or "")
    if not cfg_user or not cfg_pwd:
        return False   # enabled 但未配置凭证：恒拒绝（fail-closed，测试抓出的空凭证匹配漏洞）
    return sep == ":" and _ct_eq(user, cfg_user) and _ct_eq(pwd, cfg_pwd)

@app.middleware("http")
async def web_auth_guard(request: Request, call_next):
    """可选 Web 鉴权门（评估 R-04）：chaimi.conf 的 web_auth.enabled=true 时启用。
    定义于 strip_gateway_prefix 之前 → 作为内层中间件拿到剥完网关前缀的路径。
    凭证为空视为未配置：拒绝并挑战，强制管理员补齐配置（fail-closed）。"""
    cfg = config.load().get("web_auth") or {}
    if cfg.get("enabled") and request.url.path not in _AUTH_EXEMPT:
        auth = request.headers.get("authorization") or ""
        if not _basic_auth_ok(auth, cfg):
            return Response(content=b'{"detail":"unauthorized"}', status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="chaimi-ledger"'},
                            media_type="application/json")
    return await call_next(request)

@app.middleware("http")
async def strip_gateway_prefix(request: Request, call_next):
    p = request.scope["path"]
    if PREFIX and p.startswith(PREFIX):
        p = p[len(PREFIX):] or "/"
        request.scope["path"] = p
    return await call_next(request)

# ---------- 飞牛用户身份（多用户隔离，MULTIUSER-PLAN.md §2） ----------

# 请求级用户上下文：None=系统上下文（机器人 A 通道轮询 / 本地开发 / 旧版 fnOS，不走 ACL）
_CTX = contextvars.ContextVar("chaimi_user", default=None)

def _user() -> dict | None:
    return _CTX.get()

def _uid() -> int | None:
    u = _CTX.get()
    return u["uid"] if u else None

def _isadmin() -> bool:
    u = _CTX.get()
    return bool(u and u["isadmin"])

@app.middleware("http")
async def trim_identity(request: Request, call_next):
    """网关身份解析：X-Trim-Userid/Username/Isadmin → 请求级 ContextVar（官方文档
    developer.fnnas.com 统一网关章；应用只监听飞牛网关 UDS + 本地开发入口，头不可被
    外部伪造）。无头=系统上下文；uid 非正整数一律 401（防脏值落库），不静默当系统上下文。"""
    raw = (request.headers.get("x-trim-userid") or "").strip()
    if not raw:
        return await call_next(request)
    if not raw.isdigit() or int(raw) <= 0:
        return JSONResponse({"detail": "非法用户身份"}, status_code=401)
    token = _CTX.set({"uid": int(raw),
                      "username": (request.headers.get("x-trim-username") or "").strip(),
                      "isadmin": (request.headers.get("x-trim-isadmin") or "").lower() == "true"})
    try:
        return await call_next(request)
    finally:
        _CTX.reset(token)

def _admin_or_403() -> None:
    """应用级操作门（配置写入/备份恢复，MULTIUSER-PLAN §3.3）：
    系统上下文放行（机器人/本地开发/测试），web 用户须 NAS 管理员。
    机器人扫码/解绑不在此门——A 通道按账本 ACL 收口（见 qrcode/unbind）。"""
    if _uid() is not None and not _isadmin():
        raise HTTPException(403, "该操作需要 NAS 管理员账号（应用级设置由管理员统一管理）")

_auto_backup = backup.AutoBackup()
_ilink = bot_ilink.ILinkManager()   # 通道 A：多凭证管理(一账本一用户一机器人)
_REV = {"n": 0}                     # 数据变更计数(写库时 +1,长轮询 /api/rev/wait 按此唤醒前端刷新)
_LREV = {"n": 0}                    # 账本列表代数(建/删/改账本、共享/移除成员 +1)：他人共享给我/
                                   # 被移除共享时各端据此重拉账本下拉——rev 只刷当前账本数据不够
_REV_COND = threading.Condition()  # Condition+wait_for：锁内原子判定谓词，杜绝 Event「检查与挂起之间
                                   # set() 被先醒者 clear() 吃掉」的丢失唤醒（该竞态会让某端变更后
                                   # 延迟整整一个 hold 周期才刷新）

def _bump_rev() -> None:
    with _REV_COND:
        _REV["n"] += 1
        _REV_COND.notify_all()

def _bump_lrev() -> None:
    """账本列表代数 +1（create/update/delete_ledger、共享/移除成员、恢复备份）。"""
    with _REV_COND:
        _LREV["n"] += 1
        _REV_COND.notify_all()

def _bot_cfg() -> dict:
    """bot 配置段便捷读取（段缺失回退空 dict）；写路径各自 load/save 整份配置。"""
    return config.load().get("bot") or {}

def _bot_apply() -> None:
    """机器人热切换（DESIGN 十章）：direct=按凭证列表差量启停，其余全停。"""
    cfg = config.load()
    if (cfg.get("bot") or {}).get("mode") == "direct":
        _ilink.apply(bot_ilink.ilink_entries(cfg))
    else:
        _ilink.stop_all()

def _app_version() -> str:
    """应用版本号:优先 build 时生成的 static/version.txt(真机部署树无 manifest),
    开发环境回落向上找 manifest;都找不到给 ?。启动留痕与页面注入共用。"""
    try:
        vf = _STATIC / "version.txt"
        if vf.is_file():
            m = re.search(r"version\s*=\s*(\S+)", vf.read_text(encoding="utf-8"))
            if m:
                return m.group(1)
        mani = next((p / "manifest" for p in Path(__file__).resolve().parents
                     if (p / "manifest").is_file()), None)
        if mani:
            m = re.search(r"^version=(.+)$", mani.read_text(encoding="utf-8"), re.M)
            if m:
                return m.group(1).strip()
    except OSError:
        pass
    return "?"

def _page(name: str) -> HTMLResponse:
    text = (_STATIC / name).read_text(encoding="utf-8")
    base = f'<base href="{PREFIX}/">'
    v = _app_version()
    # 版本号注入:前端在长轮询请求上带 cv 信标,日志一眼可见各客户端跑的哪个版本
    text = text.replace("<head>",
                        f'<head><script>window.CHAIMI_VER="{v}"</script>' + base, 1)
    # 静态引用一律带 ?v=版本:版本一变 URL 即变,任何缓存层(浏览器/fnOS 网关)必取新文件。
    # 单靠 no-cache 不够——网关可能按扩展名缓存 .js,升级后出现"新 HTML 引用旧 JS"
    # (曾出现新 HTML 引用旧 JS 导致按钮无反应),HTML 里的引用链必须随版本整体换血
    text = re.sub(r'((?:src|href)="static/[^"?]+)(?=")', rf'\1?v={v}', text)
    # no-store:HTML 本体不允许任何层缓存——网关若缓存了旧 HTML,里面的旧 JS 引用会一直
    # 命中旧缓存,版本角标/?v= 都救不回来;JS/CSS 靠 ?v= 换 URL,不依赖此头
    return HTMLResponse(text, headers={"Cache-Control": "no-store"})

def conn():
    return dbm.connect()

def _to_end(s: str, *, default: str = "") -> str:
    """区间末端：10 位日期（YYYY-MM-DD）补 23:59:59 含当天，否则按 default 兜底。"""
    return (s + " 23:59:59") if len(s) == 10 else default

def _date_where(frm: str, to: str, col: str = "t.date", end: str = "") -> tuple[str, list]:
    """日期区间过滤三行式（流水/账户/归属/导出 5 处共用）：frm 一律原样 >=；
    to 端按 end——''=原样排他（to 已是完整时刻或调用方保证排他）；
    '<='=10 位膨胀当天末尾后包含（小票导出）；'<'=膨胀后排他（流水列表默认，
    to_exclusive 场景传 ''）。"""
    frag, params = "", []
    if frm:
        frag += f" AND {col}>=?"
        params.append(frm)
    if to:
        frag += f" AND {col}{end or '<'}?"
        params.append(_to_end(to, default=to) if end else to)
    return frag, params

def _safe_name(name: str) -> str:
    """图片/小票文件名校验（app_image/get_receipt 共用）：拒绝路径穿越与分隔符。"""
    if not name or ".." in name or "/" in name or "\\" in name:
        raise HTTPException(400)
    return name

def _ledger_access(ledger_id: int) -> None:
    """账本访问判定（MULTIUSER-PLAN §2.3）：系统上下文放行（机器人/本地开发）；
    飞牛用户须为 owner 或共享成员。不存在 404，无权 403。
    所有带 ledger 参数/可反查 ledger_id 的端点都经此咽喉点。"""
    uid = _uid()
    if uid is None:
        return
    row = conn().execute("SELECT owner_uid FROM ledgers WHERE id=?", (ledger_id,)).fetchone()
    if not row:
        raise HTTPException(404, "账本不存在")
    if row["owner_uid"] == uid or conn().execute(
            "SELECT 1 FROM ledger_members WHERE ledger_id=? AND uid=?", (ledger_id, uid)).fetchone():
        return
    raise HTTPException(403, "无权访问该账本（可请所有者共享给你）")

def _my_ledger_ids(uid: int) -> set:
    """uid 可访问的账本 id 集（owner+共享成员，与 _ledger_access 同口径）。"""
    return {r["id"] for r in conn().execute(
        "SELECT id FROM ledgers WHERE owner_uid=? OR id IN "
        "(SELECT ledger_id FROM ledger_members WHERE uid=?)", (uid, uid))}

def _mine_only(entries: list) -> list:
    """非管理员视角过滤：机器人绑定只留自己有权账本的（与 qrcode/unbind 的按账本
    收口同口径）；admin/系统上下文全量。status 成员表 / bot_test 绑定选取共用。"""
    uid = _uid()
    if not entries or uid is None or _isadmin():
        return entries
    allowed = _my_ledger_ids(uid)
    return [e for e in entries if e.get("ledger") in allowed]

def _owner_or_403(ledger_id: int) -> None:
    """账本级管理操作门（改名/删除/共享成员）：仅 owner；系统上下文放行。"""
    uid = _uid()
    if uid is None:
        return
    row = conn().execute("SELECT owner_uid FROM ledgers WHERE id=?", (ledger_id,)).fetchone()
    if not row:
        raise HTTPException(404, "账本不存在")
    if row["owner_uid"] != uid:
        raise HTTPException(403, "仅账本所有者可操作")

def _row_ledger(table: str, rid: int) -> int:
    """按资源 id 反查所属账本并过 ACL（id 类端点的越权防护：流水/分类/账户/映射/
    归属等 id 全库自增，不反查就等于跳过隔离）。行不存在 404。"""
    row = conn().execute(f"SELECT ledger_id FROM {table} WHERE id=?", (rid,)).fetchone()
    if not row:
        raise HTTPException(404)
    _ledger_access(row["ledger_id"])
    return row["ledger_id"]

# ---------- 页面 / 健康 ----------

@app.get("/", include_in_schema=False)
def index():
    """应用入口。飞牛用户先落身份档再判账本：一个可见账本都没有（全新安装家人首开 /
    未被共享的新用户）直接 302 到 home 建账选人——此前先渲染主应用、再由前端 loadAll
    发现无账本跳走，用户会先看到应用界面闪一下。系统上下文（机器人/本地开发）照常出应用。
    302 前必须 _touch_fn_user：admin 认领无主账本要先于判定（否则升级后首开会被误判
    无账本），家人「打开过一次应用」的共享选人前提也不能因直达 home 而丢档。"""
    u = _user()
    if u:
        _touch_fn_user(u)
        if not _my_ledger_ids(u["uid"]):
            return RedirectResponse(f"{PREFIX}/static/home.html", status_code=302,
                                    headers={"Cache-Control": "no-store"})
    return _page("app.html")

@app.get("/home", include_in_schema=False)
def home():
    """登录/用户选择页：服务端入口（无账本用户）/主应用（无当前用户）跳转至此。"""
    return _page("home.html")

def _no_cache_file(base: Path, name: str) -> FileResponse:
    """目录内文件下发（/static 与 /images 共用）：名校验 + 404 + 禁缓存。"""
    _safe_name(name)   # 与 /receipts 对称的纵深防护（路径穿越不可利用但保持一致）
    p = base / name
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})

@app.get("/static/{name}", include_in_schema=False)
def static_file(name: str):
    # 单文件应用拆分后避免升级后浏览器沿用旧 CSS/JS：与 _page 一致禁缓存
    return _no_cache_file(_STATIC, name)

@app.get("/images/{name}", include_in_schema=False)
def app_image(name: str):
    """应用图片：app/server/images 目录。
    禁缓存——后期直接替换同名文件（如换新头像）刷新即生效，无需改代码。"""
    return _no_cache_file(_IMAGES, name)

@app.get("/healthz")
def healthz():
    return {"ok": True, "version": os.environ.get("TRIM_APPVER", "?")}

# ---------- 账本 ----------

class LedgerIn(BaseModel):
    name: str
    icon: str = ""
    currency: str = "CNY"
    budget: Optional[int] = None

@app.get("/api/ledgers")
def list_ledgers(all: bool = False):
    """账本列表：飞牛用户=我拥有+共享给我（附 mine 标记，false=共享来的）；
    ?all=1 仅 admin（设置页机器人绑定的账本下拉需全库账本名）；系统上下文返回全部。"""
    uid = _uid()
    if uid is None or (all and _isadmin()):
        return [dict(r) for r in conn().execute("SELECT * FROM ledgers ORDER BY sort, id")]
    return [{**dict(r), "mine": r["owner_uid"] == uid} for r in conn().execute(
        "SELECT * FROM ledgers WHERE owner_uid=? OR id IN "
        "(SELECT ledger_id FROM ledger_members WHERE uid=?) ORDER BY sort, id", (uid, uid))]

@app.post("/api/ledgers")
def create_ledger(body: LedgerIn):
    with conn() as c:
        lid = dbm.create_ledger(c, body.name, body.icon, body.currency, _uid())
    _bump_lrev()   # 各端账本下拉即时出现新账本
    return {"id": lid}

@app.put("/api/ledgers/{lid}")
def update_ledger(lid: int, body: LedgerIn):
    _owner_or_403(lid)
    with conn() as c:
        c.execute("UPDATE ledgers SET name=?, icon=?, currency=?, "
                  "budget=COALESCE(?, budget) WHERE id=?",
                  (body.name, body.icon, body.currency, body.budget, lid))
    _bump_lrev()   # 名字/币种变了，各端下拉标签同步
    _bump_rev()    # 预算变了，统计与设置页预算行跨端同步
    return {"ok": True}

class BudgetIn(BaseModel):
    budget: Optional[int] = None

@app.put("/api/ledgers/{lid}/budget")
def update_ledger_budget(lid: int, body: BudgetIn):
    """预算是账本级共享设置：owner 与共享成员都可改（2026-10-29 修订；
    名称/币种/图标仍 owner-only，走 update_ledger）。"""
    _ledger_access(lid)
    with conn() as c:
        c.execute("UPDATE ledgers SET budget=COALESCE(?, budget) WHERE id=?",
                  (body.budget, lid))
    _bump_rev()    # 预算行/统计跨端刷新
    _bump_lrev()   # 各端 ledgersCache 随账本列表代数换血——预算展示读该缓存，只 bump rev 对端数字不更新
    return {"ok": True}

@app.delete("/api/ledgers/{lid}")
def delete_ledger(lid: int):
    _owner_or_403(lid)
    with conn() as c:
        # import_batches/ledger_members 一并清（审计发现的历史残余：漏清会留孤儿行）
        for t in ("transactions", "import_batches", "category_mappings", "categories",
                  "accounts", "attributions", "attribution_groups", "ledger_members"):
            c.execute(f"DELETE FROM {t} WHERE ledger_id=?", (lid,))
        c.execute("DELETE FROM ledgers WHERE id=?", (lid,))
    _bump_lrev()   # 正在看该账本的端由 lrev 触发回落选择页
    return {"ok": True}

# ---------- 飞牛用户 / 账本共享（MULTIUSER-PLAN §3） ----------

@app.get("/api/whoami")
def whoami():
    """当前飞牛用户（网关头解析结果），顺带落打开记录（_touch_fn_user）。"""
    u = _user()
    if not u:
        return {"uid": None, "username": "", "isadmin": False}
    _touch_fn_user(u)
    return {"uid": u["uid"], "username": u["username"], "isadmin": u["isadmin"]}

# 落档节流（进程内）：同一用户身份 5 分钟内重复打开不再写库（GET / + whoami 每次整页
# 加载各落一次档，多标签/高频刷新即是写放大；admin 认领每进程只执行一次）
_TOUCH_THROTTLE_S = 300
_touch_state: dict = {}          # uid -> (username, isadmin, 上次写库 time.time())
_admin_claimed = False

def _touch_fn_user(u: dict) -> None:
    """打开应用落档（whoami 与入口 / 共用）：known_users UPSERT（共享选人器数据源，
    fnOS 无用户列表 API）；admin 打开时把升级前的无主账本一次性归属自己
    （MULTIUSER-PLAN §3.5 定案：旧数据固定给 admin）。
    落档失败（如 DB 被长写事务锁住）静默放行：入口判定不因此 500，下次打开再落。"""
    global _admin_claimed
    ts = time.time()
    prev = _touch_state.get(u["uid"])
    unchanged = (prev and prev[0] == u["username"] and prev[1] == bool(u["isadmin"])
                 and ts - prev[2] < _TOUCH_THROTTLE_S)
    if unchanged and (_admin_claimed or not u["isadmin"]):
        return
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with conn() as c:
            if not unchanged:
                c.execute(
                    "INSERT INTO known_users(uid, username, isadmin, first_seen, last_seen) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(uid) DO UPDATE SET username=excluded.username, "
                    "isadmin=excluded.isadmin, last_seen=excluded.last_seen",
                    (u["uid"], u["username"], 1 if u["isadmin"] else 0, now, now))
            if u["isadmin"] and not _admin_claimed:
                n = c.execute("UPDATE ledgers SET owner_uid=? WHERE owner_uid IS NULL",
                              (u["uid"],)).rowcount
                if n:
                    log.info(f"多用户迁移：{n} 个无主账本归属 admin「{u['username']}」(uid={u['uid']})")
                _admin_claimed = True   # 0 行也算完成：无主账本只在升级后首开出现
        _touch_state[u["uid"]] = (u["username"], bool(u["isadmin"]), ts)
    except Exception:
        pass

@app.get("/api/users")
def list_users():
    """打开过应用的飞牛用户（共享选人器数据源；家庭内网应用，用户名互通无隐私边界）。"""
    return [dict(r) for r in conn().execute(
        "SELECT uid, username, isadmin, last_seen FROM known_users ORDER BY last_seen DESC")]

@app.get("/api/ledgers/{lid}/members")
def list_members(lid: int):
    _owner_or_403(lid)
    return [dict(r) for r in conn().execute(
        "SELECT m.uid, m.added_at, k.username, k.last_seen FROM ledger_members m "
        "LEFT JOIN known_users k ON k.uid=m.uid WHERE m.ledger_id=? ORDER BY m.uid", (lid,))]

@app.put("/api/ledgers/{lid}/members")
def add_member(lid: int, body: dict):
    """共享账本给飞牛用户：对方须打开过应用（known_users 有档）。只授权不动账本数据——
    不自动建记账用户（用户反馈定案）：对方记账时自选账本里的记账用户，归属口径由
    owner 在「用户与预算」自理；若存在与对方飞牛用户名同名的记账用户，前端仍会
    默认选中它（core.restoreCurUser 的同名匹配，机会性生效）。"""
    _owner_or_403(lid)
    try:
        uid = int(body.get("uid") or 0)
    except (TypeError, ValueError):
        uid = 0
    if not conn().execute("SELECT 1 FROM known_users WHERE uid=?", (uid,)).fetchone():
        raise HTTPException(400, "该用户还没打开过柴米记账，请对方先登录飞牛打开一次应用")
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO ledger_members(ledger_id, uid) VALUES(?,?)", (lid, uid))
    _bump_lrev()   # 被共享方在线即见新账本（lrev 触发重拉账本下拉）
    return {"ok": True}

@app.delete("/api/ledgers/{lid}/members")
def del_member(lid: int, uid: int):
    _owner_or_403(lid)
    with conn() as c:
        c.execute("DELETE FROM ledger_members WHERE ledger_id=? AND uid=?", (lid, uid))
    _bump_lrev()   # 被移除方正看着该账本 → lrev 触发回落选择页，不再对着 403 空转
    return {"ok": True}

# ---------- 分类 / 账户 ----------

@app.get("/api/categories")
def list_categories(ledger: int):
    _ledger_access(ledger)
    rows = [dict(r) for r in conn().execute(
        "SELECT c.*, p.name AS parent_name FROM categories c "
        "LEFT JOIN categories p ON c.parent_id=p.id WHERE c.ledger_id=? ORDER BY c.sort, c.id", (ledger,))]
    return rows

class CategoryIn(BaseModel):
    name: str
    type: str = "expense"
    parent_id: Optional[int] = None
    icon: str = ""

@app.post("/api/categories")
def create_category(ledger: int, body: CategoryIn):
    _ledger_access(ledger)
    with conn() as c:
        cur = c.execute(
            "INSERT INTO categories(ledger_id, type, name, parent_id, icon) VALUES(?,?,?,?,?)",
            (ledger, body.type, body.name, body.parent_id, body.icon))
    _bump_rev()   # 账本级设置跨端同步：共享账本任一方改动即唤醒对端刷新（账户/映射/归属分组同）
    return {"id": cur.lastrowid}

@app.put("/api/categories/{cid}")
def update_category(cid: int, body: CategoryIn):
    _row_ledger("categories", cid)
    with conn() as c:
        c.execute("UPDATE categories SET name=?, icon=? WHERE id=?", (body.name, body.icon, cid))
        # v6 起映射走 category_id 外键，改名自动联动，无需同步映射表
    _bump_rev()
    return {"ok": True}

@app.delete("/api/categories/{cid}")
def delete_category(cid: int):
    """删分类必须回炉关联流水（无分类的行因 INNER JOIN 不进任何列表，只能从
    待确认队列重新归类）——但留审计日志，静默大面积改数据必须可追溯。"""
    _row_ledger("categories", cid)
    with conn() as c:
        # 连子分类流水一并回炉：只回父分类会留下指向已删子分类的悬空 category_id
        # （无 FK，INNER JOIN 下静默失类）；映射也按同集清理
        kids = [r[0] for r in c.execute("SELECT id FROM categories WHERE parent_id=?", (cid,))]
        ids = (cid, *kids)
        marks = ",".join("?" * len(ids))
        n = c.execute(f"UPDATE transactions SET category_id=NULL, need_confirm=1 "
                      f"WHERE category_id IN ({marks})", ids).rowcount
        c.execute(f"DELETE FROM category_mappings WHERE category_id IN ({marks})", ids)
        c.execute("DELETE FROM categories WHERE parent_id=?", (cid,))
        c.execute("DELETE FROM categories WHERE id=?", (cid,))
    if n:
        log.info(f"删除分类 #{cid}：{n} 笔流水置空分类并进入待确认（重新归类入口）")
    _bump_rev()
    return {"ok": True, "affected": n}

# 存储不动（tags.attribution 仍存原始名），两条归组路径：
#   ① 名单路径：归属名查 attributions.group_id（导入的 liulu·支付宝 → 组「老公」，导入自动映射）
#   ② 同名路径：归属名恰与组名相同（编辑弹窗选分组保存的组名）→ 直接归该组
_ATTR_RESOLVE = (
    "LEFT JOIN attributions _ax ON _ax.ledger_id=t.ledger_id "
    "AND _ax.name=json_extract(t.tags,'$.attribution') "
    "LEFT JOIN attribution_groups _gx ON _gx.id=_ax.group_id "
    "LEFT JOIN attribution_groups _g2 ON _g2.ledger_id=t.ledger_id "
    "AND _g2.name=json_extract(t.tags,'$.attribution') ")
_ATTR_EXPR = "COALESCE(_gx.name, _g2.name, json_extract(t.tags,'$.attribution'))"
_ATTR_DISPLAY = (_ATTR_EXPR + " AS attr_display")

def _attr_group_absorb(c, ledger: int, name: str) -> None:
    """建组/改组时吸收同名归属行：其流水的归属名==组名，经同名路径仍归本组，名单不保留孤行。"""
    c.execute("DELETE FROM attributions WHERE ledger_id=? AND name=?", (ledger, name))

def _clear_user_attribution(c, ledger: int, name: str) -> None:
    """删组时清掉用户侧（手动/AI）流水存的该组名；导入流水的原始归属保留（账单真实数据）。"""
    ph = ",".join("?" * len(dbm.IMPORT_SOURCES))
    c.execute(f"UPDATE transactions SET tags=json_remove(tags,'$.attribution') "
              f"WHERE ledger_id=? AND json_extract(tags,'$.attribution')=? "
              f"AND COALESCE(source,'manual') NOT IN ({ph})",
              (ledger, name, *dbm.IMPORT_SOURCES))

def _check_attr_group_name(c, ledger: int, name: str, gid: int | None = None) -> None:
    """组名唯一（改名传 gid 排除自身）；与归属名同名的场景走吸收而非报错（见 _attr_group_absorb）。"""
    row = c.execute("SELECT id FROM attribution_groups WHERE ledger_id=? AND name=?",
                    (ledger, name)).fetchone()
    if row and row["id"] != gid:
        raise HTTPException(400, f"分组「{name}」已存在")

@app.get("/api/attribution-groups")
def list_attr_groups(ledger: int):
    _ledger_access(ledger)
    return [dict(r) for r in conn().execute(
        "SELECT g.*, (SELECT COUNT(*) FROM attributions a WHERE a.group_id=g.id) AS member_count, "
        "(SELECT group_concat(a.name, '、') FROM attributions a WHERE a.group_id=g.id) AS members "
        "FROM attribution_groups g WHERE g.ledger_id=? ORDER BY g.sort, g.id", (ledger,))]

class AttrGroupIn(BaseModel):
    name: str
    avatar: str = "f"

def _check_avatar(v: str) -> str:
    if v not in ("m", "f"):
        raise HTTPException(400, "头像无效")
    return v

def _group_body(body: AttrGroupIn) -> tuple[str, str]:
    """归属分组请求体校验（新建/改名共用）：名称去空格非空 + 头像合法。"""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "用户名不能为空")
    return name, _check_avatar(body.avatar)

@app.post("/api/attribution-groups")
def create_attr_group(ledger: int, body: AttrGroupIn):
    _ledger_access(ledger)
    name, avatar = _group_body(body)
    with conn() as c:
        _check_attr_group_name(c, ledger, name)
        _attr_group_absorb(c, ledger, name)   # 同名归属行吸收（其流水经同名路径仍归本组）
        cur = c.execute("INSERT INTO attribution_groups(ledger_id, name, avatar) VALUES(?,?,?)",
                        (ledger, name, avatar))
    _bump_rev()
    return {"ok": True, "id": cur.lastrowid}

@app.put("/api/attribution-groups/{gid}")
def rename_attr_group(gid: int, body: AttrGroupIn):
    name, avatar = _group_body(body)
    with conn() as c:
        g = c.execute("SELECT ledger_id, name FROM attribution_groups WHERE id=?", (gid,)).fetchone()
        if not g:
            raise HTTPException(404, "分组不存在")
        _ledger_access(g["ledger_id"])
        _check_attr_group_name(c, g["ledger_id"], name, gid)
        # 手动账存的是组名（编辑弹窗选分组）：改名同步改写已存旧组名的流水，否则会「掉组」
        c.execute("UPDATE transactions SET tags=json_set(tags,'$.attribution',?) "
                  "WHERE ledger_id=? AND json_extract(tags,'$.attribution')=?",
                  (name, g["ledger_id"], g["name"]))
        _attr_group_absorb(c, g["ledger_id"], name)
        c.execute("UPDATE attribution_groups SET name=?, avatar=? WHERE id=?", (name, avatar, gid))
    _bump_rev()
    return {"ok": True}

@app.delete("/api/attribution-groups/{gid}")
def delete_attr_group(gid: int):
    # FK ON DELETE SET NULL：名单成员自动回未分组；导入流水的原始归属名不动；
    with conn() as c:
        g = c.execute("SELECT ledger_id, name FROM attribution_groups WHERE id=?", (gid,)).fetchone()
        if not g:
            raise HTTPException(404, "用户不存在")
        _ledger_access(g["ledger_id"])
        n = c.execute("SELECT COUNT(*) FROM attribution_groups WHERE ledger_id=?",
                      (g["ledger_id"],)).fetchone()[0]
        if n <= 1:
            raise HTTPException(400, "至少保留一个用户")
        _clear_user_attribution(c, g["ledger_id"], g["name"])
        c.execute("DELETE FROM attribution_groups WHERE id=?", (gid,))
    _bump_rev()
    return {"ok": True}

@app.get("/api/attributions")
def list_attributions(ledger: int):
    _ledger_access(ledger)
    return [dict(r) for r in conn().execute(
        "SELECT a.*, g.name AS group_name FROM attributions a "
        "LEFT JOIN attribution_groups g ON a.group_id=g.id "
        "WHERE a.ledger_id=? ORDER BY a.id", (ledger,))]

class AttrGroupSetIn(BaseModel):
    group_id: int | None = None

@app.put("/api/attributions/{aid}")
def update_attribution(aid: int, body: AttrGroupSetIn):
    """归属设分组/移出组（group_id=null 移出）。只改名单表，历史流水不动，统计口径即时生效。"""
    with conn() as c:
        a = c.execute("SELECT ledger_id FROM attributions WHERE id=?", (aid,)).fetchone()
        if not a:
            raise HTTPException(404, "归属不存在")
        _ledger_access(a["ledger_id"])
        if body.group_id is not None:
            g = c.execute("SELECT ledger_id FROM attribution_groups WHERE id=?",
                          (body.group_id,)).fetchone()
            if not g or g["ledger_id"] != a["ledger_id"]:
                raise HTTPException(400, "分组不存在")
        c.execute("UPDATE attributions SET group_id=? WHERE id=?", (body.group_id, aid))
    _bump_rev()   # 归属分组变化影响统计口径（蝴蝶图/归属下钻），跨端同步刷新
    return {"ok": True}

@app.get("/api/accounts")
def list_accounts(ledger: int, frm: str = Query("", alias="from"), to: str = ""):
    # frm：形参不能叫 from（Python 关键字），query 名用 alias 保持 ?from=
    _ledger_access(ledger)
    rows = [dict(r) for r in conn().execute(
        "SELECT * FROM accounts WHERE ledger_id=? ORDER BY sort, id", (ledger,))]
    # spend 口径（毛支出-退款转账）在 db.account_spend_map 唯一实现，与 AI kind=account 共用
    spend_map = dbm.account_spend_map(conn(), ledger, frm, to)
    # 全量流水笔数（不受日期范围影响）：删账户确认框提示"将置空 N 笔"用，
    # 口径与 delete_account 的 UPDATE ... WHERE account_id=? 一致（不分类型）
    cnt = {r[0]: r[1] for r in conn().execute(
        "SELECT account_id, COUNT(*) FROM transactions "
        "WHERE ledger_id=? AND account_id IS NOT NULL GROUP BY account_id", (ledger,))}
    for r in rows:
        g, rf = spend_map.get(r["id"], (0, 0))
        r["spend"] = g - rf
        r["tx_count"] = cnt.get(r["id"], 0)
    # 含未选账户的支出、退款按原消费日归因；与按账户拆分的 spend 列因口径差异可能不相等
    if frm or to:
        s = dbm.stats_summary_range(conn(), ledger, frm, to or "9999-12-31")
        for r in rows:
            r["net_expense"] = s["net_expense"]   # 同值平铺到每个账户，前端取一次即可
    return rows

class AccountIn(BaseModel):
    name: str
    type: str = "cash"

@app.post("/api/accounts")
def create_account(ledger: int, body: AccountIn):
    _ledger_access(ledger)
    with conn() as c:
        cur = c.execute("INSERT INTO accounts(ledger_id, name, type) VALUES(?,?,?)",
                        (ledger, body.name, body.type))
    _bump_rev()
    return {"id": cur.lastrowid}

@app.put("/api/accounts/{aid}")
def update_account(aid: int, body: AccountIn):
    _row_ledger("accounts", aid)
    with conn() as c:
        c.execute("UPDATE accounts SET name=?, type=? WHERE id=?",
                  (body.name, body.type, aid))
    _bump_rev()
    return {"ok": True}

@app.delete("/api/accounts/{aid}")
def delete_account(aid: int):
    """1.0.1 起不再把关联流水打回待确认——旧版 `need_confirm=1` 会把该账户全部
    历史流水瞬间塞满待确认队列（真机事故：删一个旧账户，次日发现待确认爆满）。
    账户置空即可：分类/统计不受影响，流水照常显示为「未指定账户」。"""
    _row_ledger("accounts", aid)
    c = conn()
    name = c.execute("SELECT name FROM accounts WHERE id=?", (aid,)).fetchone()["name"]
    with c:
        n = c.execute("UPDATE transactions SET account_id=NULL WHERE account_id=?",
                      (aid,)).rowcount
        c.execute("DELETE FROM accounts WHERE id=?", (aid,))
    log.info(f"删除账户 #{aid}「{name}」：{n} 笔流水账户置空（分类与确认状态不动）")
    _bump_rev()
    return {"ok": True, "affected": n}

# ---------- 流水 ----------

class TxIn(BaseModel):
    type: str
    amount: int                      # 分
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    to_account_id: Optional[int] = None
    date: str
    name: Optional[str] = None       # 交易名称（导入流水带；可编辑）
    merchant: Optional[str] = None   # 交易对方（导入流水带；可编辑）
    note: str = ""
    tags: dict = {}
    receipt_path: Optional[str] = None   # None=保持；""=清空；"path"=覆盖
    source: str = "app"
    ai_meta: dict = {}
    order_no: Optional[str] = None   # 订单号（退款匹配键；可编辑，null=保持原值）
    need_confirm: bool = False
    refund_for: Optional[int] = None

@app.get("/api/transactions")
def list_transactions(ledger: int, _from: str = Query("", alias="from"), to: str = "", type: str = "",
                      category: int = 0, need_confirm: bool = False, q: str = "",
                      renqing_name: str = "", attribution: str = "",
                      to_exclusive: bool = False, with_refunds: bool = False,
                      page: int = 1, page_size: int = 50):
    _ledger_access(ledger)
    sql = ["SELECT t.*, c.name AS category_name, a.name AS account_name, "
           "ro.id AS ro_id, ro.date AS ro_date, ro.name AS ro_name, ro.note AS ro_note, "
           "ro.amount AS ro_amount, ro.type AS ro_type, ro.category_id AS ro_category_id, "
           "ro.account_id AS ro_account_id, ro.order_no AS ro_order_no, ro.merchant AS ro_merchant, "
           "ro.source AS ro_source, ro.tags AS ro_tags, ro.need_confirm AS ro_need_confirm, "
           "rc.name AS ro_category_name, ra.name AS ro_account_name, "
           + _ATTR_DISPLAY + " "
           "FROM transactions t",
           "LEFT JOIN categories c ON t.category_id=c.id",
           "LEFT JOIN accounts a ON t.account_id=a.id "
           "LEFT JOIN transactions ro ON ro.id=t.refund_for "
           "LEFT JOIN categories rc ON ro.category_id=rc.id "
           "LEFT JOIN accounts ra ON ro.account_id=ra.id "
           + _ATTR_RESOLVE +
           "WHERE t.ledger_id=?"]
    args: list = [ledger]
    if q.strip():
        # 关键词命中 name/note/订单号/交易对方；归属按显示口径匹配（组名优先，_ATTR_RESOLVE 已 JOIN）；
        # 金额单独走精确匹配——LIKE 无法表达小数点，输入可解析为数字时按「分」比对。
        # 退款对只看主行（t），原交易由 ro_* 提供
        kw = f"%{q.strip()}%"
        or_conds = ["t.name LIKE ?", "t.note LIKE ?", "t.order_no LIKE ?",
                    "t.merchant LIKE ?",
                    "c.name LIKE ?", f"{_ATTR_EXPR} LIKE ?"]
        or_args = [kw] * 6
        try:
            cents = int(Decimal(q.strip()) * 100)
            or_conds.append("t.amount=?")
            or_args.append(cents)
        except (InvalidOperation, ValueError, OverflowError):
            # InvalidOperation=普通非数字；ValueError/OverflowError="nan"/"inf"
            # （Decimal 可解析但 int() 换算溢出，漏捕曾让搜索框输 nan 直接 500）
            pass
        sql.append("AND (" + " OR ".join(or_conds) + ")")
        args += or_args
    if _from or to:
        # to_exclusive=1：to 为排他上界原样使用（裸日期对 10 位纯日期行和 19 位带时间行都正确排他；
        # 补 00:00:00 反而会把 10 位的边界日手工流水包含进来）。
        # 默认：10 位日期膨胀为当天 23:59:59（含当天，供日视图/自定义区间用）
        frag, params = _date_where(_from, to, end="" if to_exclusive else "<")
        sql.append(frag)
        args += params
    if type:
        if with_refunds and type == "expense":
            # 汇总条「支出」为净额(毛−退款)：明细须带退款行（关联后 type=transfer，
            # 前端渲染退款双卡片）才能与条上数字对账；未关联退款仍是收入行不计入
            sql.append("AND (t.type=? OR t.refund_for IS NOT NULL)")
            args.append(type)
        else:
            sql.append("AND t.type=?")
            args.append(type)
    if category:
        # 连带取「退款行指向本分类消费」的对（退款行 type=transfer 且 category_id 常不落本分类，不连带取会丢失退款双卡片）
        cat_sql = "SELECT id FROM categories WHERE ledger_id=? AND (id=? OR parent_id=?)"
        sql.append(f"AND (t.category_id IN ({cat_sql}) "
                   f"OR t.refund_for IN (SELECT id FROM transactions WHERE ledger_id=? AND category_id IN ({cat_sql})))")
        args += [ledger, category, category, ledger, ledger, category, category]
    if need_confirm:
        sql.append("AND t.need_confirm=1")
    if renqing_name:
        # 人情明细：分类=人情往来 + 名称精确匹配（与 /api/stats/renqing 的分组键同一表达式）
        sql.append(f"AND {dbm.RENQING_CAT_SQL}")
        sql.append(f"AND {dbm.RENQING_NAME_SQL} = ?")
        args += [ledger, renqing_name]
    if attribution:
        # 归属下钻（统计页蝴蝶图条形）：与 /api/stats/attribution 同口径。
        # 分组行传组名（_ATTR_EXPR 双路径：名单映射/直名建组）；未分组行传
        # 「归属名·平台」统计键，剥后缀反解回 source 一并过滤
        base, _, plat = attribution.rpartition("·")
        src = {cn: s for s, cn in dbm.PLATFORM_CN.items()}.get(plat)
        if not src:
            base = attribution
        sql.append(f"AND {_ATTR_EXPR}=?")
        args.append(base)
        if src:
            sql.append("AND t.source=?")
            args.append(src)
    where = " ".join(sql)
    total = conn().execute(f"SELECT COUNT(*) FROM ({where})", args).fetchone()[0]
    rows = conn().execute(f"{where} ORDER BY t.date DESC, t.id DESC LIMIT ? OFFSET ?",
                          args + [page_size, (page - 1) * page_size]).fetchall()
    items = []
    for r in rows:
        d = dbm.tx_to_dict(r)
        # 退款对：附带原交易摘要（前端用来在退款行下渲染第二张卡片）
        if d.get("refund_for") and r["ro_id"] is not None:
            ro_tags = dbm.jloads(r["ro_tags"]) or {}
            d["refund_origin"] = {
                "id": r["ro_id"], "date": r["ro_date"] or "", "name": r["ro_name"] or "",
                "note": r["ro_note"] or "", "amount": r["ro_amount"] or 0,
                "type": r["ro_type"] or "expense", "category_id": r["ro_category_id"],
                "account_id": r["ro_account_id"], "order_no": r["ro_order_no"] or "",
                "merchant": r["ro_merchant"] or "", "source": r["ro_source"] or "manual",
                "tags": ro_tags, "need_confirm": bool(r["ro_need_confirm"]),
                "category_name": r["ro_category_name"] or "", "account_name": r["ro_account_name"] or "",
            }
        else:
            d["refund_origin"] = None
        items.append(d)
    return {"items": items, "total": total, "page": page, "page_size": page_size}

def _tx_date(raw: str, old: str = "") -> str:
    """TxIn 日期 → 入库口径（db.norm_date）；认不出/日历非法给 400 而非 500。"""
    try:
        return dbm.norm_date(raw, old=old)
    except ValueError as e:
        raise HTTPException(400, str(e))

def _insert_tx(c, ledger: int, t: TxIn):
    return dbm.insert_tx(
        c, ledger, type=t.type, amount=t.amount, category_id=t.category_id,
        account_id=t.account_id, to_account_id=t.to_account_id, date=_tx_date(t.date),
        name=t.name, merchant=t.merchant, note=t.note, tags=t.tags,
        receipt_path=t.receipt_path or None, source=t.source, ai_meta=t.ai_meta,
        order_no=t.order_no, need_confirm=t.need_confirm,
        manual_cat=bool(t.category_id))   # App 记一笔带分类=用户手选

@app.post("/api/transactions")
def create_transaction(ledger: int, body: TxIn):
    _ledger_access(ledger)
    with conn() as c:
        tid = _insert_tx(c, ledger, body)
        if body.refund_for:
            _link_refund(c, tid, body.refund_for, body.amount, confidence=None)
        if body.type == "transfer" and body.order_no:
            ib._rescan_unmatched_refunds(c, ledger)
    _bump_rev()   # App 侧写库同推刷新:唤醒所有页面的长轮询(与 bot 写库同源)
    return {"id": tid}

@app.put("/api/transactions/{tid}")
def update_transaction(tid: int, body: TxIn):
    with conn() as c:
        # name/merchant/order_no 允许单独更新：传 null 保持原值（COALESCE），不传不会被清空
        # receipt_path：None=保持原值；""=清空(NULL)；非空=覆盖。SQLite 无「参数是否为 NULL」的
        # 内省，原 COALESCE(NULLIF(?, ''), receipt_path) 会把 "" 与 None 同视（都保留原值），
        # 故用 CASE WHEN 显式三分。
        old = c.execute("SELECT ledger_id, receipt_path, date, category_id FROM transactions WHERE id=?",
                        (tid,)).fetchone()
        if not old:
            raise HTTPException(404, "流水不存在")
        _ledger_access(old["ledger_id"])
        old_path = old["receipt_path"]
        c.execute(
            """UPDATE transactions SET type=?, amount=?, category_id=?, account_id=?, to_account_id=?,
               date=?, name=COALESCE(?, name), merchant=COALESCE(?, merchant),
               order_no=COALESCE(?, order_no),
               note=?, tags=?,
               receipt_path=CASE WHEN ? IS NULL THEN receipt_path ELSE NULLIF(?, '') END,
               need_confirm=?,
               manual_cat=CASE WHEN ? IS NOT ? THEN 1 ELSE manual_cat END
               WHERE id=?""",
            (body.type, body.amount, body.category_id, body.account_id, body.to_account_id,
             _tx_date(body.date, old=old["date"]), body.name, body.merchant, body.order_no,
             body.note, dbm._j(body.tags), body.receipt_path, body.receipt_path,
             1 if body.need_confirm else 0,
             body.category_id, old["category_id"], tid))
        # （income 自动转 transfer：不再计入收入，统计在原消费日扣减，见 DESIGN 8.5.1）
        if body.refund_for is not None:
            _link_refund(c, tid, body.refund_for, body.amount, confidence=None)
        # 旧文件清理：路径发生变更（含清空为 NULL）即删旧文件；失败不影响主流程，与 DELETE 策略一致
        new = c.execute("SELECT receipt_path FROM transactions WHERE id=?", (tid,)).fetchone()
        new_path = new["receipt_path"]
    if old_path and old_path != new_path:
        bot_core.unlink_receipt(old_path)   # 失败不影响主流程，与 DELETE 策略一致
    _bump_rev()
    return {"ok": True}

def _reject_late_origin(origin_date, refund_date) -> None:
    """不变式：原始订单日期须 ≤ 退款日期（ISO 前 10 位比，同日含当天）。
    仅换关联时调用——原样重报已有关联不拦，编辑退款日期等操作不被卡。"""
    if (origin_date or "")[:10] > (refund_date or "")[:10]:
        raise HTTPException(400, "原始订单日期晚于退款日期，不能关联")

def _link_refund(c, tid: int, refund_for: int, amount: int, *, confidence: str | None) -> None:
    """退款关联统一入口（手动编辑 / 待确认队列共用）：0=取消关联；>0=关联到该支出
    （须同账本且为 expense）。金额相等→full，否则 partial。
    原消费扣减、不计收支，从任何入口（前端/API/批量/队列）关联都不会出现口径分叉。
    换关联时校验原始订单日期（见 _reject_late_origin）；confidence 由入口决定
    （手动编辑=人工清空 NULL，队列确认=人工已核对 high）。"""
    row = c.execute("SELECT ledger_id, date, refund_for FROM transactions WHERE id=?", (tid,)).fetchone()
    if not row:
        raise HTTPException(404, "流水不存在")
    if refund_for == 0:
        c.execute(f"UPDATE transactions SET {dbm.UNLINK_REFUND_SET} WHERE id=?", (tid,))
        return
    o = c.execute("SELECT ledger_id, type, amount, date FROM transactions WHERE id=?", (refund_for,)).fetchone()
    if not o or o["ledger_id"] != row["ledger_id"] or o["type"] != "expense":
        raise HTTPException(400, "退款只能关联本账本的支出流水")
    if refund_for != row["refund_for"]:
        _reject_late_origin(o["date"], row["date"])
    status = dbm.refund_status(o["amount"], amount)
    c.execute("UPDATE transactions SET refund_for=?, refund_status=?, refund_confidence=?, type='transfer' "
              "WHERE id=?", (refund_for, status, confidence, tid))

class TxBatchIn(BaseModel):
    """批量修改：ids=目标流水；三字段缺省=不改。
    attribution：""=清除归属，非空=写入 tags.attribution（与原 tags 其余键合并）"""
    ids: list[int]
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    attribution: Optional[str] = None

@app.post("/api/transactions/batch_update")
def batch_update_transactions(ledger: int, body: TxBatchIn):
    _ledger_access(ledger)
    if not body.ids:
        raise HTTPException(400, "未选择流水")
    updated = 0
    with conn() as c:
        for tid in body.ids:
            row = c.execute("SELECT ledger_id, tags FROM transactions WHERE id=?", (tid,)).fetchone()
            if not row or row["ledger_id"] != ledger:
                continue   # 跨账本/已删的 id 直接跳过（全选后他端删数据的竞态）
            sets, args = [], []
            if body.category_id is not None:
                sets.append("category_id=?")
                args.append(body.category_id)
                # 批量改分类=用户手选，与单笔编辑/待确认确认同打 manual_cat（真机 1.0.4 事故：
                # 漏打导致批量归类的停车费被修复预览翻出来 second-guess）
                sets.append("manual_cat=1")
            if body.account_id is not None:
                sets.append("account_id=?")
                args.append(body.account_id)
            if body.attribution is not None:
                tags = dbm.jloads(row["tags"]) or {}
                if body.attribution:
                    tags["attribution"] = body.attribution
                else:
                    tags.pop("attribution", None)
                sets.append("tags=?")
                args.append(dbm._j(tags))
            if sets:
                args.append(tid)
                c.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id=?", args)
                updated += 1
    if updated:
        _bump_rev()
    return {"updated": updated}

class TxBatchDelIn(BaseModel):
    ids: list[int]

@app.post("/api/transactions/batch_delete")
def batch_delete_transactions(ledger: int, body: TxBatchDelIn):
    """批量删除：跳过规则与批量修改同款（跨账本/已删 id 直接跳过，全选后他端删数据的竞态）。
    逐笔走 delete_tx——删原消费自动解链其退款行；关联小票文件一并清理。"""
    _ledger_access(ledger)
    if not body.ids:
        raise HTTPException(400, "未选择流水")
    paths = []
    with conn() as c:
        for tid in body.ids:
            row = c.execute("SELECT ledger_id FROM transactions WHERE id=?", (tid,)).fetchone()
            if not row or row["ledger_id"] != ledger:
                continue
            paths.append(dbm.delete_tx(c, tid))
    for p in paths:
        bot_core.unlink_receipt(p)   # 无小票路径时 no-op
    if paths:
        log.info(f"批量删除 {len(paths)} 笔流水（用户确认触发）")
        _bump_rev()
    return {"deleted": len(paths)}

@app.delete("/api/transactions/{tid}")
def delete_transaction(tid: int):
    _row_ledger("transactions", tid)
    with conn() as c:
        old_path = dbm.delete_tx(c, tid)
    bot_core.unlink_receipt(old_path)   # 无小票路径时 no-op
    _bump_rev()
    return {"ok": True}

# ---------- 待确认队列 ----------

@app.get("/api/pending")
def pending(ledger: int, kind: str = ""):
    _ledger_access(ledger)
    sql = ("SELECT t.*, c.name AS category_name, " + _ATTR_DISPLAY + " "
           "FROM transactions t "
           "LEFT JOIN categories c ON t.category_id=c.id "
           + _ATTR_RESOLVE +
           "WHERE t.ledger_id=? AND t.need_confirm=1")
    args = [ledger]
    if kind == "refund":
        sql += " AND t.refund_for IS NULL AND t.type='transfer' AND t.tags LIKE '%退款%'"
    return [dbm.tx_to_dict(r) for r in conn().execute(sql + " ORDER BY t.date DESC", args)]

class ResolveIn(BaseModel):
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    refund_for: Optional[int] = None

@app.post("/api/pending/{tid}/resolve")
def resolve_pending(tid: int, body: ResolveIn):
    """处理待确认：改分类 / 关联退款。改分类只对当笔负责（manual_cat 保护），
    不回写学习映射——1.0.2 前这里会把用户手选泛化成该源分类的全局规则。"""
    _row_ledger("transactions", tid)
    c = conn()
    tx_amount = c.execute("SELECT amount FROM transactions WHERE id=?", (tid,)).fetchone()["amount"]
    with c:
        if body.category_id is not None:
            # 待确认里选分类=用户手选 → 打 manual_cat 标，数据修复不再翻动
            #（need_confirm 由下方无条件 UPDATE 统一清零）
            c.execute("UPDATE transactions SET category_id=?, manual_cat=1 WHERE id=?",
                      (body.category_id, tid))
        if body.account_id is not None:
            c.execute("UPDATE transactions SET account_id=? WHERE id=?", (body.account_id, tid))
        if body.refund_for is not None:
            # 队列确认的退款关联与手动编辑同一套校验/口径（_link_refund），high=人工已核对
            _link_refund(c, tid, body.refund_for, tx_amount, confidence="high")
        c.execute("UPDATE transactions SET need_confirm=0 WHERE id=?", (tid,))
    _bump_rev()
    return {"ok": True}

# ---------- 统计 ----------

@app.get("/api/stats/summary")
def stats_summary(ledger: int, month: str):
    _ledger_access(ledger)
    return dbm.stats_summary(conn(), ledger, month)

@app.get("/api/stats/calendar")
def stats_calendar(ledger: int, month: str):
    _ledger_access(ledger)
    return dbm.stats_calendar(conn(), ledger, month)

@app.get("/api/stats/category")
def stats_category(ledger: int, month: str, type: str = "expense"):
    _ledger_access(ledger)
    return dbm.stats_category(conn(), ledger, month, type)

@app.get("/api/stats/monthly")
def stats_monthly(ledger: int, year: str):
    _ledger_access(ledger)
    try:
        nxt = int(year) + 1
    except ValueError:
        raise HTTPException(400, "year 必须是数字年份（如 2026）")
    return dbm.stats_trend(conn(), ledger, f"{year}-01-01", f"{nxt}-01-01")

@app.get("/api/stats/yearly")
def stats_yearly(ledger: int):
    _ledger_access(ledger)
    return dbm.stats_yearly(conn(), ledger)

def _span(_from: str, to: str) -> tuple[str, str]:
    """统计区间（from 含端、to 排他上界）：任一端传空即不限该端，与导出/流水列表口径一致。"""
    return _from or "0000-01-01", to or "9999-12-31"

@app.get("/api/stats/trend")
def stats_trend_range(ledger: int, _from: str = Query("", alias="from"), to: str = ""):
    """任意区间的按月趋势。"""
    _ledger_access(ledger)
    return dbm.stats_trend(conn(), ledger, *_span(_from, to))

@app.get("/api/stats/range")
def stats_range(ledger: int, _from: str = Query("", alias="from"), to: str = ""):
    _ledger_access(ledger)
    return dbm.stats_summary_range(conn(), ledger, *_span(_from, to))

@app.get("/api/stats/category-range")
def stats_category_range(ledger: int, _from: str = Query("", alias="from"), to: str = "",
                         type: str = "expense"):
    _ledger_access(ledger)
    return dbm.stats_category_range(conn(), ledger, *_span(_from, to), type)

def _attribution_rows(ledger: int, _from: str, to: str) -> list[dict]:
    """归属汇总：db.attribution_rows 唯一实现（统计页 /api/stats/attribution、导出 xlsx
    「归属统计」sheet、AI 对话查账 kind=attribution 共用）。"""
    return dbm.attribution_rows(conn(), ledger, *_span(_from, to))

@app.get("/api/stats/attribution")
def stats_attribution(ledger: int, _from: str = Query("", alias="from"), to: str = ""):
    _ledger_access(ledger)
    return _attribution_rows(ledger, _from, to)

@app.get("/api/stats/renqing")
def stats_renqing(ledger: int):
    """人情往来汇总：db.stats_renqing 唯一实现（统计页与 AI 查账 kind=renqing 共用）。
    given=我给出（支出），received=我收到（收入），balance=given-received>0 即对方尚未还礼。"""
    _ledger_access(ledger)
    return dbm.stats_renqing(conn(), ledger)

# ---------- 导入 / 导出 ----------

MAX_UPLOAD = 20 * 1024 * 1024   # 导入/小票上传统一上限；读取用 file.read(MAX_UPLOAD + 1)，超限判定不必整文件进内存
_PREVIEW_LOCK = threading.Lock()   # 导入预览串行（见 import_parse）
ALLOWED_EXT = (".csv", ".xlsx")

async def _read_upload(file: UploadFile) -> bytes:
    """有界读取上传体（三个上传口共用）：多读 1 字节判超限即 400，不必整文件进内存。
    报错文案从 MAX_UPLOAD 派生——改上限只动常量一处。"""
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(400, f"文件超过 {MAX_UPLOAD // 1048576}MB 上限")
    return data

def _upload_guard(filename: str | None, data: bytes) -> None:
    """导入上传安全闸（防恶意文件）：扩展名白名单 + xlsx 魔数校验
    （大小上限由 _read_upload 的有界读取把关）。
    行数熔断由 import_bill._capped（解析期）与本函数调用后的 len 校验（权威上限）接力。"""
    fn = (filename or "").lower()
    if not fn.endswith(ALLOWED_EXT):
        raise HTTPException(400, "仅支持 .csv / .xlsx 文件")
    if fn.endswith(".xlsx") and data[:2] != b"PK":
        raise HTTPException(400, "文件内容不是有效 xlsx")

@app.post("/api/import/parse")
async def import_parse(ledger: int, file: UploadFile = File(...), use_ai: bool = True,
                       template: str = Form("")):
    _ledger_access(ledger)
    data = await _read_upload(file)
    _upload_guard(file.filename, data)
    def _parse():
        if template:
            # 向导"试解析"：未保存的临时模板直接解析（保存后走 parse_bill 自动分流）
            tpl = json.loads(template)
            if not (isinstance(tpl, dict) and tpl.get("headers") and tpl.get("col")):
                raise ValueError("模板格式不完整")
            return ib.parse_generic(ib.text_from_upload(data, file.filename), tpl)
        return ib.parse_bill(data, file.filename)

    # 解码（xlsx 解 zip+XML）与逐行引擎是 CPU 密集（20MB/5 万行级），挪线程池防卡事件循环
    try:
        flows = await asyncio.to_thread(_parse)
    except Exception as e:
        raise HTTPException(400, f"解析失败: {e}")
    if len(flows) > ib.MAX_PARSE_ROWS:
        raise HTTPException(400, f"行数超过上限 {ib.MAX_PARSE_ROWS // 10000} 万行")
    # build_previews 逐行查/写库，同样挪线程；conn() 必须在 worker 内取（线程本地连接
    # 禁止跨线程传）。持 _PREVIEW_LOCK 串行：预览期 _ensure_account 是 check-then-insert，
    # 并发导入同一账本会建出同名账户
    def _previews():
        with _PREVIEW_LOCK:
            return ib.build_previews(conn(), ledger, flows)
    previews = await asyncio.to_thread(_previews)
    if use_ai:
        # 同步 LLM 兜底挪线程池（async 里直接调卡死事件循环）；保序串行不 gather——
        # 分类兜底读 name 与名称兜底写 name 并行会交错，且各自内部已 6 路并发
        await asyncio.to_thread(ai.resolve_subcategories_batch, previews, ledger)
        await asyncio.to_thread(ai.resolve_names_batch, previews)
    n_dup = sum(1 for p in previews if p.get("duplicate"))
    n_xdup = sum(1 for p in previews if p.get("pay_dupe"))
    n_new = sum(1 for p in previews if ib._is_new_row(p))
    return {"filename": file.filename, "total": len(previews), "new": n_new,
            "duplicate": n_dup, "cross_dupe": n_xdup,
            "skip": len(previews) - n_new - n_dup - n_xdup,
            "need_confirm": sum(1 for p in previews if p.get("need_confirm") and not p.get("skip")),
            "previews": previews}

class ConfirmIn(BaseModel):
    previews: list[dict]
    filename: str
    source: str

@app.post("/api/import/confirm")
def import_confirm(ledger: int, body: ConfirmIn):
    _ledger_access(ledger)
    batch_id = ib.apply_import(conn(), ledger, body.previews, body.filename, body.source)
    n = conn().execute("SELECT count FROM import_batches WHERE id=?", (batch_id,)).fetchone()[0]
    with conn() as c:
        group_names = {r["name"] for r in c.execute(
            "SELECT name FROM attribution_groups WHERE ledger_id=?", (ledger,))}
        for p in body.previews:
            attr = ((p.get("tags") or {}).get("attribution") or "").strip()
            if attr and attr not in group_names:
                c.execute("INSERT OR IGNORE INTO attributions(ledger_id, name) VALUES(?,?)", (ledger, attr))
    _bump_rev()
    return {"batch_id": batch_id, "imported": n}

@app.get("/api/import/batches")
def list_batches(ledger: int):
    _ledger_access(ledger)
    return [dict(r) for r in conn().execute(
        "SELECT * FROM import_batches WHERE ledger_id=? ORDER BY id DESC", (ledger,))]

@app.delete("/api/import/batches/{batch_id}")
def undo_import(batch_id: int):
    _row_ledger("import_batches", batch_id)
    n = ib.undo_batch(conn(), batch_id)
    _bump_rev()
    return {"deleted": n}

# ---------- 数据修复（原导入自愈的确认版，1.0.1） ----------
# 旧版在预览阶段静默改写已有数据（用户手动整理的分类会在下一次预览被翻回词典结果），
# 现改为只读预览 + 显式应用。

@app.get("/api/repair/preview")
def repair_preview(ledger: int):
    _ledger_access(ledger)
    return ic.repair_preview(conn(), ledger)

@app.post("/api/repair/apply")
def repair_apply(ledger: int):
    _ledger_access(ledger)
    r = ic.repair_apply(conn(), ledger)
    _bump_rev()
    return r

# ---------- 通用模板向导（P1）：嗅探 + 模板 CRUD ----------

@app.post("/api/import/sniff")
async def import_sniff(file: UploadFile = File(...)):
    """向导嗅探：表头定位 + 样例 + 列映射猜测（"无法识别"的文件从这里进入模板向导）。"""
    data = await _read_upload(file)
    _upload_guard(file.filename, data)
    # 同步 CPU 解析（xlsx 解 zip+XML，20MB 内百 ms 级）挪线程池，防卡事件循环；
    # 坏文件统一 400（与 import_parse 口径一致，而非 500）
    try:
        text = await asyncio.to_thread(ib.text_from_upload, data, file.filename)
    except Exception as e:
        raise HTTPException(400, f"解析失败: {e}")
    return await asyncio.to_thread(ib.sniff_template, text)

# 保存白名单从引擎常量派生（引擎新增 col 键即自动放行，不再两处手抄）；
# 引擎顶层 schema（_TPL_ALLOWED_KEYS）仍由 import_bill 单独维护
_TPL_FIELDS = tuple(sorted(set(ib._STANDARD_COL_KEYS) | {"origin_source", "export_id", "export_ref"}))

@app.get("/api/import/templates")
def list_import_templates():
    return config.load().get("import_templates") or []

@app.post("/api/import/templates")
def save_import_template(body: dict):
    """新建/更新模板（按 name 覆盖）。最小校验 + 字段白名单（import_bill.user_templates 消费）。
    白名单覆盖引擎全 schema：复制内置模板改列名/状态词存为自定义即接管路由（覆盖机制）。"""
    _admin_or_403()   # 写应用级配置（chaimi.conf），与 put_config 同门
    name = str(body.get("name") or "").strip()
    headers, col = body.get("headers"), body.get("col") or {}
    if not name or not isinstance(headers, list) or not headers or not col:
        raise HTTPException(400, "模板需要名称、表头列与列映射")
    col = {k: v for k, v in col.items() if k in _TPL_FIELDS and v}
    if "date" not in col or "amount" not in col:
        raise HTTPException(400, "交易时间与金额两列必须映射")
    rules = body.get("refund_rules")
    if rules is not None:
        if (not isinstance(rules, list)
                or not all(isinstance(x, dict) and x.get("col") and x.get("op")
                           and isinstance(x.get("values"), list) for x in rules)):
            raise HTTPException(400, "refund_rules 格式不合法")
        rules = [{"col": str(x["col"]), "op": str(x["op"]),
                  "values": [str(v) for v in x["values"]],
                  **({"contains_none": [str(v) for v in x["contains_none"]]}
                     if x.get("contains_none") else {}),
                  **({"guard": {"flow_not": str(x["guard"]["flow_not"])}}
                     if isinstance(x.get("guard"), dict) and x["guard"].get("flow_not") else {})}
                 for x in rules]
    ttypes = body.get("transfer_types")
    if ttypes is not None and not isinstance(ttypes, list):
        raise HTTPException(400, "transfer_types 格式不合法")
    nf = body.get("name_fallback")
    if nf is not None:
        if (not isinstance(nf, dict) or not isinstance(nf.get("cols"), list)):
            raise HTTPException(400, "name_fallback 格式不合法")
        nf = {"cols": [str(c) for c in nf["cols"]],
              **({"generic_set": [str(s) for s in nf["generic_set"]]}
                 if isinstance(nf.get("generic_set"), list) else {})}
    rf = body.get("row_filter")
    if rf is not None:
        if not isinstance(rf, dict) or not rf.get("col") or not isinstance(rf.get("skip_in"), list):
            raise HTTPException(400, "row_filter 格式不合法")
        rf = {"col": str(rf["col"]), "skip_in": [str(s) for s in rf["skip_in"]]}
    owner_re = str(body.get("owner_re") or "")
    if owner_re:
        try:
            re.compile(owner_re)
        except re.error:
            raise HTTPException(400, "owner_re 不是合法正则")
    try:
        skip_n = int(body.get("skip_first_n") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "skip_first_n 必须是数字")
    cfg = config.load()
    tpls = [t for t in (cfg.get("import_templates") or []) if t.get("name") != name]
    t = {"id": "custom", "name": name, "headers": [str(h) for h in headers], "col": col,
         "flow_map": {str(k): v for k, v in (body.get("flow_map") or {}).items()
                      if v in ("expense", "income", "transfer")},
         "refund_type": str(body.get("refund_type") or "").strip(),
         "name_strip": [str(s) for s in (body.get("name_strip") or []) if s],
         "owner": str(body.get("owner") or "").strip(),
         "skip_first_n": skip_n}
    if rules is not None:
        t["refund_rules"] = rules
    if ttypes is not None:
        t["transfer_types"] = [str(s) for s in ttypes]
    if nf is not None:
        t["name_fallback"] = nf
    if rf is not None:
        t["row_filter"] = rf
    if body.get("hash_fields") is not None and isinstance(body["hash_fields"], list):
        t["hash_fields"] = [str(s) for s in body["hash_fields"]]
    for flag in ("expand", "two_pass", "note_fallback_last"):
        if body.get(flag):
            t[flag] = True
    for key in ("pay_split", "attribution_col", "skip_zero_reason", "note_strip",
                "amount_mode", "owner_re", "derive_base_as", "category_split"):
        val = body.get(key)
        if val:
            t[key] = [str(s) for s in val] if key == "note_strip" else str(val)
    tpls.append(t)
    cfg["import_templates"] = tpls
    config.save(cfg)
    return {"ok": True}

@app.delete("/api/import/templates/{name}")
def delete_import_template(name: str):
    _admin_or_403()   # 写应用级配置（chaimi.conf），与 save_import_template 同门
    cfg = config.load()
    cfg["import_templates"] = [t for t in (cfg.get("import_templates") or []) if t.get("name") != name]
    config.save(cfg)
    return {"ok": True}

def _csv_download_headers(ledger_id: int, ext: str = "csv") -> dict:
    """导出文件名：chaimi-ledger-{账本名}-{导出日期}.{ext}；中文走 RFC 5987 filename*，
    同时给 ASCII 回退（老浏览器丢弃中文名也不至于拿到空文件名）。"""
    name = conn().execute("SELECT name FROM ledgers WHERE id=?", (ledger_id,)).fetchone()
    stem = f"chaimi-ledger-{name['name'] if name else ledger_id}-{time.strftime('%Y%m%d')}"
    quoted = quote(f"{stem}.{ext}")
    return {"Content-Disposition": f"attachment; filename=ledger-{time.strftime('%Y%m%d')}.{ext}; filename*=UTF-8''{quoted}"}

def _neut(v) -> str:
    """公式注入中和（OWASP 惯例）：账单/用户可控文本以 = + - @ 或 TAB/CR 开头时前缀单引号，
    防"导入恶意账单 → 导出 CSV/xlsx → Excel/WPS 打开"时单元格被当公式执行；
    代价是此类开头的正常文本（如 -5折）导出带前导 '，Excel 显示时还原为原文。"""
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s

def _export_table(rows, ledger_id: int) -> list:
    """导出表（CSV/xlsx 共用）：15 列全量格式，流水卡片上的数据全列出。
    金额保留分→元的换算由调用方格式化；交易名称=name（回落备注/分类，与卡片标题同口径）；
    流水归属=解析后名字（有组显组名）；转账两端账户分「支付账户/收款账户」两列；
    流水ID/退款关联——导出回环：导入时按 ID 直连退款对，无需重新匹配。
    用户/账单可控的文本列一律 _neut 中和；日期/金额/ID 等本源列不涉及。"""
    type_map = {"expense": "支出", "income": "收入", "transfer": "不计收支"}
    cur_row = conn().execute("SELECT currency FROM ledgers WHERE id=?", (ledger_id,)).fetchone()
    cur_code = (cur_row["currency"] if cur_row else None) or "CNY"
    C = pt.EXPORT_COLUMNS   # 列名与 export 模板单源（platform_templates），改列名只动一处
    return [[C["date"], C["name"], C["amount"], "币种", C["flow"], C["type"],
             C["pay"], C["to_account"], "小票", C["attribution"], C["note"], C["order"],
             C["origin_source"], C["export_id"], C["export_ref"]]] + [
        [r["date"], _neut(r["name"] or r["note"] or r["category"] or ""), r["amount"] / 100, cur_code,
         type_map[r["type"]], _neut(r["category"] or ""), _neut(r["account"] or ""), _neut(r["to_account"] or ""),
         "", _neut(r["attr_display"] or ""), _neut(r["note"] or ""), _neut(r["order_no"] or ""), _neut(r["source"] or ""),
         r["id"], r["refund_for"] or ""] for r in rows]

@app.get("/api/receipts/export")
def export_receipts(ledger: int, _from: str = Query("", alias="from"), to: str = ""):
    """小票打包下载：按所选时间范围把流水带的小票打成 zip。
    文件名 = 日期_交易名.jpeg（重名加交易 id 前缀）；范围/账本内无小票返回 404 提示。"""
    _ledger_access(ledger)
    sql = ("SELECT t.id, t.date, t.name, t.receipt_path FROM transactions t "
           "WHERE t.ledger_id=? AND t.receipt_path IS NOT NULL AND t.receipt_path!=''")
    frag, params = _date_where(_from, to, end="<=")
    sql += frag
    args: list = [ledger, *params]
    rows = conn().execute(sql + " ORDER BY t.date", args).fetchall()
    rdir = config.receipts_dir()
    # 行与文件成对过滤（文件缺失剔除该对）：zip(rows, files) 在中间文件缺失时会错位配对
    pairs = [(r, rdir / Path(r["receipt_path"]).name) for r in rows]
    pairs = [(r, f) for r, f in pairs if f.is_file()]
    if not pairs:
        raise HTTPException(404, "所选范围内没有小票")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        used: set[str] = set()
        for r, f in pairs:
            safe = "".join(c for c in (r["name"] or "小票") if c not in '\\/:*?"<>|')[:30] or "小票"
            name = f"{(r['date'] or '')[:10]}_{safe}{f.suffix}"
            while name in used:   # 同日同名：加交易 id 防覆盖
                name = f"{(r['date'] or '')[:10]}_{r['id']}_{safe}{f.suffix}"
            used.add(name)
            z.write(f, name)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition":
                                      f'attachment; filename="receipts-{ledger}.zip"'})

@app.get("/api/export")
def export(ledger: int, _from: str = Query("", alias="from"), to: str = "", format: str = "json"):
    _ledger_access(ledger)
    sql = ("SELECT t.id, t.refund_for, t.date, t.type, t.amount, t.name, c.name AS category, "
           "a.name AS account, a2.name AS to_account, t.note, "
           "t.order_no, t.source, t.tags, " + _ATTR_DISPLAY + " "
           "FROM transactions t "
           "LEFT JOIN categories c ON t.category_id=c.id "
           "LEFT JOIN accounts a ON t.account_id=a.id "
           "LEFT JOIN accounts a2 ON t.to_account_id=a2.id "
           + _ATTR_RESOLVE + "WHERE t.ledger_id=?")
    frag, params = _date_where(_from, to, end="<")
    sql += frag
    args: list = [ledger, *params]
    rows = conn().execute(sql + " ORDER BY t.date", args).fetchall()
    if format in ("csv", "xlsx"):
        table = _export_table(rows, ledger)
        lg_row = conn().execute("SELECT name, currency FROM ledgers WHERE id=?", (ledger,)).fetchone()
        range_txt = f"{_from} 至 {to}" if _from or to else "全部"
        meta = [f"# 账本：{lg_row['name'] if lg_row else ledger}",
                f"# 币种：{(lg_row['currency'] if lg_row else None) or 'CNY'}",
                f"# 导出范围：{range_txt}",
                f"# 导出时间：{time.strftime('%Y-%m-%d %H:%M')}",
                f"# 流水笔数：{len(rows)}"]
        meta_rows = [[m] for m in meta] + [[]]   # 每条说明一行单格 + 空行（csv.writer/ws.append 对字符串会逐字符迭代，必须包成列表）
        if format == "csv":
            # 文本格式金额需自带两位小数
            for row in table[1:]:
                row[2] = f"{row[2]:.2f}"
            buf = io.StringIO()
            csv.writer(buf).writerows(meta_rows + table)
            return StreamingResponse(iter([("\ufeff" + buf.getvalue()).encode("utf-8")]),
                                     media_type="text/csv",
                                     headers=_csv_download_headers(ledger))
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "流水"
        for row in meta_rows + table:
            ws.append(row)
        ws.column_dimensions["A"].width = 12   # 日期列宽，避免 2026-09-01 显示成 ####
        ws2 = wb.create_sheet("归属统计")
        ws2.append(["流水归属", "笔数", "支出", "收入", "包含归属"])
        for r in _attribution_rows(ledger, _from, _to_end(to) if to else ""):
            ws2.append([_neut(r["person"]), r["cnt"], r["expense"] / 100, r["income"] / 100,
                        _neut("、".join(m["person"] for m in r.get("members", [])))])
        buf = io.BytesIO()
        wb.save(buf)
        return StreamingResponse(iter([buf.getvalue()]),
                                 media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers=_csv_download_headers(ledger, "xlsx"))
    items = []
    for r in rows:
        d = dict(r)
        d["amount"] = d["amount"] / 100
        items.append(d)
    return JSONResponse(items)

# ---------- 映射配置 ----------

class MappingIn(BaseModel):
    flow_type: str
    source: str
    category_id: int

@app.get("/api/mappings")
def list_mappings(ledger: int):
    _ledger_access(ledger)
    return [dict(r) for r in conn().execute(
        "SELECT m.id, m.flow_type, m.source, m.category_id, m.learned, c.name AS target "
        "FROM category_mappings m JOIN categories c ON m.category_id=c.id "
        "WHERE m.ledger_id=? ORDER BY m.source", (ledger,))]

@app.post("/api/mappings")
def create_mapping(ledger: int, body: MappingIn):
    """v6：目标必须是已存在的分类（category_id 外键），悬空映射从结构上不可能。"""
    _ledger_access(ledger)
    if not dbm.top_cat_by_id(conn(), ledger, body.category_id):
        raise HTTPException(400, "目标分类不存在或不属于当前账本")
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO category_mappings(ledger_id, flow_type, source, category_id) VALUES(?,?,?,?)",
            (ledger, body.flow_type, body.source.strip(), body.category_id))
    _bump_rev()
    return {"ok": True}

@app.delete("/api/mappings/{mid}")
def delete_mapping(mid: int):
    _row_ledger("category_mappings", mid)
    with conn() as c:
        c.execute("DELETE FROM category_mappings WHERE id=?", (mid,))
    _bump_rev()
    return {"ok": True}

@app.put("/api/mappings/{mid}")
def update_mapping(mid: int, body: dict):
    """编辑模式改名：只改本条映射的匹配词；目标 = 分类本身（改名走分类接口，外键自动联动）。"""
    _row_ledger("category_mappings", mid)
    source = (body.get("source") or "").strip()
    if not source:
        raise HTTPException(400, "无可更新字段")
    with conn() as c:
        c.execute("UPDATE category_mappings SET source=? WHERE id=?", (source[:60], mid))
    _bump_rev()
    return {"ok": True}

# ---------- 备份 ----------

@app.post("/api/backup")
def do_backup():
    _admin_or_403()   # 整库备份含所有用户的账本（MULTIUSER-PLAN §3.3）
    return backup.create_backup()

@app.get("/api/backups")
def get_backups():
    _admin_or_403()
    return backup.list_backups()

def _backup_call(op, name: str) -> None:
    """备份操作（restore/delete）公共异常映射：文件名非法/非数据库/不存在统一 400。"""
    try:
        op(name)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))

@app.post("/api/restore")
def do_restore(body: dict):
    _admin_or_403()   # 恢复整库影响所有用户
    _backup_call(backup.restore_backup, body.get("file", ""))
    dbm.init_db()
    # chaimi.conf 随备份回滚：直连轮询线程必须按新模式差量对齐，否则旧监听残留
    _bot_apply()
    _bump_rev()    # 整库被替换：各端全量刷新（数据 + 账本列表）
    _bump_lrev()
    return {"ok": True}

@app.post("/api/backups/delete")
def backup_delete(body: dict):
    _admin_or_403()
    _backup_call(backup.delete_backup, body.get("file", ""))
    return {"ok": True}

@app.get("/api/backups/download")
def backup_download(file: str = Query(...)):
    """下载备份文件到本地（文件名校验同 restore）。"""
    _admin_or_403()
    try:
        p = backup._locate(file)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))
    return FileResponse(p, filename=file)

@app.get("/api/logs/download")
def logs_download():
    """日志收集打包下载（参考 liuku）：运行日志 + 飞牛安装日志，ZIP 内存打包。"""
    _admin_or_403()
    sources: list[tuple[str, Path]] = []          # (zip内路径, 源文件)
    sources.append(("app/chaimi-ledger.log", config.log_path()))   # applog 轮转文件（本地开发也有）
    if config.TRIM_PKGVAR:
        sources.append(("run/info.log", Path(config.TRIM_PKGVAR) / "info.log"))
    for p in sorted(Path("/var/log/apps").glob("chaimi-ledger*")):
        sources.append((f"varlog/{p.name}", p))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = (
            f"chaimi-ledger 日志包\n"
            f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"TRIM_PKGVAR: {config.TRIM_PKGVAR or '(本地开发)'}\n"
            f"收集文件数: {sum(1 for _, p in sources if p.is_file())}\n"
            "run/=运行日志 varlog/=飞牛捕获的安装日志\n"
        )
        zf.writestr("manifest.txt", manifest)
        for rel, p in sources:
            if p.is_file():
                try:
                    # 二进制原样入包：read_text 会把轮转的 *.gz 压缩内容按 UTF-8 读坏成乱码
                    zf.writestr(rel, p.read_bytes())
                except OSError:
                    pass
    name = f"chaimi-ledger-logs-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={name}"},
    )

# ---------- 配置 ----------

@app.get("/api/llm/models")
def get_llm_models():
    """设置页「模型列表」：服务商 /models 按 文本/视觉 分组（可失败，error 带说明）"""
    return ai.list_models_grouped()

def _mask(d: dict, *keys: str) -> None:
    """凭证字段就地掩码（明文只存 chaimi.conf 与备份副本，出接口一律 ***）。"""
    for k in keys:
        if d.get(k):
            d[k] = "***"

@app.get("/api/config")
def get_config():
    cfg = config.load()
    _mask(cfg["llm"], "api_key", "vl_api_key")
    # A 通道凭证掩码；兼容旧版单 dict 与列表两种形态
    bot = cfg.get("bot") or {}
    ilink = bot.get("ilink")
    for e in ([ilink] if isinstance(ilink, dict) else (ilink or [])):
        if isinstance(e, dict):
            _mask(e, "token")
    _mask(cfg.get("web_auth") or {}, "password")
    return cfg

# LLM 配置键白名单从 DEFAULTS 派生（config.py 是 schema 唯一来源）：漏加键的失败形态是
# 设置页"看似保存成功、值被静默丢弃"，最难排查——派生后加配置项只改 config.DEFAULTS 一处
_LLM_KEYS = tuple(config.DEFAULTS["llm"].keys())

def _cfg_int(b: dict, old: dict, key: str, lo: int, hi: int) -> int:
    """配置数字字段钳制（备份计划用）：越界截断，非数字 400。"""
    try:
        return max(lo, min(hi, int(b.get(key, old[key]))))
    except (TypeError, ValueError):
        raise HTTPException(400, f"{key} 必须是数字")

@app.put("/api/config")
def put_config(body: dict):
    _admin_or_403()   # AI Key/机器人/备份计划是应用级配置，管理员统一管理（MULTIUSER-PLAN §3.3）
    cfg = config.load()
    for k, v in (body.get("llm") or {}).items():
        if k not in _LLM_KEYS:   # 白名单外的键不落盘（防任意键注入配置文件）
            continue
        if k in ("api_key", "vl_api_key") and v == "***":
            continue
        cfg["llm"][k] = v
    if "theme" in body:
        cfg["theme"] = body["theme"]
    if isinstance(body.get("backup"), dict):
        b, old = body["backup"], cfg["backup"]
        old.pop("keep", None)  # 清掉旧版字段
        old["enabled"] = bool(b.get("enabled", old["enabled"]))
        old["hour"] = _cfg_int(b, old, "hour", 0, 23)
        old["keep_months"] = _cfg_int(b, old, "keep_months", 0, 480)
        old["keep_years"] = _cfg_int(b, old, "keep_years", 0, 100)
    if isinstance(body.get("bot"), dict):
        b = body["bot"]
        old = cfg.setdefault("bot", {})
        if b.get("mode") in ("off", "direct"):   # 网关模式(B通道)已下线
            old["mode"] = b["mode"]
        if isinstance(b.get("allowed_users"), list):
            old["allowed_users"] = [str(x).strip() for x in b["allowed_users"] if str(x).strip()]
    config.save(cfg)
    _bot_apply()   # 热切换（6.2）：direct 启停轮询线程 / off 全停
    return {"ok": True}

# ---------- AI ----------

class AiParseIn(BaseModel):
    text: str

@app.post("/api/ai/parse")
def ai_parse(ledger: int, body: AiParseIn):
    _ledger_access(ledger)
    return ai.parse_booking(body.text, ledger, date.today().strftime("%Y-%m-%d"))

async def _save_receipt(file: UploadFile) -> tuple[Path, str]:
    """上传小票落盘（/api/ai/receipt 用）：魔数校验 + 裁剪压缩 + 时间戳随机名。
    process_receipt 原地覆写：裁掉桌面留白 + 缩至 1080px + JPEG 85（典型 3MB→200KB）。"""
    data = await _read_upload(file)
    if not bot_core.is_image(data):
        raise HTTPException(400, "小票仅支持 JPG / PNG / WEBP 图片")
    d = config.receipts_dir()
    d.mkdir(parents=True, exist_ok=True)
    fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}.jpg"
    p = d / fname
    p.write_bytes(data)
    await asyncio.to_thread(bot_core.process_receipt, p)   # PIL 压缩 CPU 密集，挪线程池（失败保留原图）
    return p, f"receipts/{fname}"

@app.post("/api/ai/receipt")
async def ai_receipt(ledger: int, file: UploadFile = File(...)):
    _ledger_access(ledger)
    path, rp = await _save_receipt(file)
    result = await asyncio.to_thread(ai.parse_receipt, str(path))   # 视觉 OCR 秒级网络调用，挪线程池
    if result.get("error"):
        path.unlink(missing_ok=True)   # 识别失败：前端只显错误不保留图片，删文件防孤儿
        return result
    result["receipt_path"] = rp
    return result

@app.delete("/api/receipts/discard")
def discard_receipt(path: str):
    """App 取消记账/删空卡片/关闭弹窗时清未入库小票（前端 fire-and-forget 调用）。
    只删无流水引用的文件——已入库或别笔在用的一律不动，误调无副作用。"""
    name = Path(path or "").name
    if not name or ".." in path:
        raise HTTPException(400, "无效路径")
    row = conn().execute("SELECT 1 FROM transactions WHERE receipt_path IN (?, ?) LIMIT 1",
                         (name, f"receipts/{name}")).fetchone()
    if row:
        return {"ok": True, "deleted": False}
    p = config.receipts_dir() / name
    if not p.is_file():
        return {"ok": True, "deleted": False}
    try:
        p.unlink()
    except OSError:
        return {"ok": True, "deleted": False}
    log.info(f"App 取消清理小票: {name}")
    return {"ok": True, "deleted": True}

def sweep_orphan_receipts(grace_hours: int = 24) -> int:
    """小票孤儿文件兜底清理：无任何流水引用且超过宽限期的图片删除。
    覆盖 App 取消时断网/关页面等收不到显式 discard 的场景（顺带清历史遗留）；
    宽限期 24h 远大于在途草稿存续（机器人 30min TTL、App 卡片随页面存亡），
    只扫图片后缀、只扫一层目录。"""
    rdir = config.receipts_dir()
    if not rdir.is_dir():
        return 0
    used = {Path(r["receipt_path"]).name for r in conn().execute(
        "SELECT receipt_path FROM transactions WHERE receipt_path IS NOT NULL AND receipt_path!=''")}
    cutoff = time.time() - grace_hours * 3600
    n = 0
    for p in rdir.iterdir():
        if (p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")
                and p.name not in used and p.stat().st_mtime < cutoff):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    if n:
        log.info(f"兜底清理 {n} 个无引用小票文件")
    return n

def _sse(gen) -> StreamingResponse:
    """AI 事件流统一响应（X-Accel-Buffering 关网关缓冲，前端逐块收到）。"""
    return StreamingResponse(ai.sse_stream(gen), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

@app.get("/api/ai/analyze")
def ai_analyze(ledger: int, month: str):
    _ledger_access(ledger)
    return _sse(ai.monthly_analysis(ledger, month))

@app.get("/api/ai/chat")
def ai_chat(ledger: int, q: str):
    _ledger_access(ledger)
    return _sse(ai.chat(q, ledger))

# ---------- 静态资源（小票图） ----------

@app.get("/receipts/{name}")
def get_receipt(name: str):
    _safe_name(name)
    p = config.receipts_dir() / name
    if not p.exists():
        raise HTTPException(404)
    # 三层禁缓存：浏览器/WebView + fnOS 网关(nginx) + 代理——裁剪覆写后任何端必须取新图
    return FileResponse(p, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "X-Accel-Expires": "0",       # nginx proxy_cache 不缓存此响应
        "Pragma": "no-cache",
    })

@app.post("/api/receipts/crop")
def crop_receipt(path: str = Query(...), body: dict = None):
    """手动裁剪小票（viewReceipt → ✂️）：Pillow 原地裁剪 + JPEG 85 覆写。"""
    name = Path(path or "").name
    if not name:
        raise HTTPException(400, "无效路径")   # Path("..").name == "" 已拦截穿越
    p = config.receipts_dir() / name
    if not p.is_file():
        raise HTTPException(404, "小票不存在")
    body = body or {}
    x, y = max(0, int(body.get("x") or 0)), max(0, int(body.get("y") or 0))
    w, h = int(body.get("w") or 0), int(body.get("h") or 0)
    if w < 10 or h < 10:
        raise HTTPException(400, "裁剪区域太小")
    try:
        from PIL import Image
        img = Image.open(p)
        w, h = min(w, img.size[0] - x), min(h, img.size[1] - y)
        if w < 10 or h < 10:
            raise HTTPException(400, "裁剪区域超出图片范围")
        img.crop((x, y, x + w, y + h)).save(p, "JPEG", quality=85)
        return {"ok": True, "size": [w, h]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"裁剪失败: {e}")

class BotTestIn(BaseModel):
    text: str = ""

def _ct_eq(a: str, b: str) -> bool:
    """常量时间字符串比较（Basic Auth 用）：非 ASCII 输入判失败而非抛异常。"""
    try:
        return secrets.compare_digest(a, b)
    except (TypeError, UnicodeError):
        return False

@app.post("/api/bot/test")
def bot_test(body: BotTestIn):
    """设置页「测试」按钮：按直连绑定的账本+归属走完整机器人链路（bypass_auth）。
    admin/系统上下文取首条绑定；普通用户取自己有权账本的首条（免得测试账落进他人账本）。
    B 通道（网关/MCP/TCP sidecar）已下线——bot 仅剩 A 通道 iLink 直连。"""
    entries = _mine_only(bot_ilink.ilink_entries(config.load()))
    if not entries:
        return {"reply": "尚无直连绑定：请先在上方选账本和用户生成二维码、扫码绑定后再测试"}
    e = entries[0]
    try:
        reply = bot_core.handle(body.text, "_panel", bypass_auth=True,
                                ledger_id=int(e.get("ledger") or 0),
                                attribution=str(e.get("label") or ""))
    except Exception as ex:   # 链路内意外异常（如 LLM 返回脏 patch）给 400 可读信息，而非 500
        raise HTTPException(400, f"机器人链路异常: {type(ex).__name__}: {ex}"[:200])
    return {"reply": reply}

@app.get("/api/rev/wait")
def rev_wait(since: int = -1, lsince: int = -1, hold: int = 25):
    """长轮询(方案B,替代被飞牛网关掐杀的 SSE 无限流):挂住直到 rev(数据代数)或
    lrev(账本列表代数,多用户共享/移除实时生效)偏离 since/lsince,或 hold 秒超时再返回;
    双缺省(首轮立基线/轻量探测)立即返回当前值。每请求时长有限(<60s 网关掐杀线,
    hold 收敛 1..30s),缓冲网关下照常送达——与 AI 聊天流可用同理。前端循环调用,变更后
    1s 内刷新。lsince 缺省 -1=不参与判定(旧客户端兼容:仍只看 rev)。"""
    def _changed() -> bool:
        if since >= 0 and _REV["n"] != since:
            return True
        if lsince >= 0 and _LREV["n"] != lsince:
            return True
        return since < 0 and lsince < 0
    with _REV_COND:
        _REV_COND.wait_for(_changed, timeout=max(1, min(hold, 30)))
        return {"rev": _REV["n"], "lrev": _LREV["n"]}

# ---------- 通道 A：iLink 直连（DESIGN 10.3） ----------

def _ilink_on_confirmed(bot, creds: dict) -> None:
    """扫码成功(十章):凭证落 chaimi.conf,扫码人自动进白名单(身份即授权);
    实例转正交 manager.adopt。平台规则:一个微信号对同一 bot 应用只有一条
    绑定,扫新码会顶替旧绑定——同 user 旧凭证一律删除,apply 差量停旧实例
    (防僵尸轮询)。"""
    ledger = bot._ledger or 1
    label = bot._label or ""
    user = creds.get("ilink_user_id") or ""
    cfg = config.load()
    bot_cfg = cfg.setdefault("bot", {})
    entries = [e for e in bot_ilink.ilink_entries(cfg)
               if not user or e.get("user") != user]   # 同微信仅留最新绑定
    entries.append({"account_id": creds.get("ilink_bot_id") or "",
                    "token": creds.get("bot_token") or "",
                    "base_url": creds.get("baseurl") or "",
                    "user": user, "ledger": ledger, "label": label})
    bot_cfg["ilink"] = entries
    allowed = bot_cfg.setdefault("allowed_users", [])
    if user and user not in allowed:
        allowed.append(user)
    config.save(cfg)
    # 出口闸(与 qrcode 入口闸成对):扫码悬在半路时模式被切走,只存凭证不启动实例,
    # 切回 direct 后 _bot_apply 按凭证自动拉起,免重扫
    if bot_cfg.get("mode") == "direct":
        _ilink.adopt(bot)
        _ilink.apply(entries)

@app.post("/api/bot/ilink/qrcode")
def bot_ilink_qrcode(body: dict):
    """按账本发起扫码(DESIGN 10.6):{ledger, label}——扫哪个账本的码记哪本。
    权限=目标账本 ACL（A 通道按账本收口，成员可给自己的账本发起绑定）；
    模式总开关/白名单仍是应用级配置（admin，MULTIUSER-PLAN §3.3）。"""
    try:
        ledger = int(body.get("ledger") or 1)
    except (TypeError, ValueError):
        raise HTTPException(400, "ledger 必须是数字")
    if _uid() is not None and not _isadmin():
        _ledger_access(ledger)
    # 入口闸:直连未开启不发票,网关/关闭模式下扫码无意义(回调还有出口闸兜底)
    if _bot_cfg().get("mode") != "direct":
        raise HTTPException(400, "微信扫码未开启：请先把模式切到「微信扫码」再生成二维码")
    label = str(body.get("label") or "")[:20]
    try:
        return _ilink.begin_login(ledger, label, "", _ilink_on_confirmed)
    except Exception as e:
        raise HTTPException(502, f"获取二维码失败：{e}")

@app.get("/api/bot/ilink/status")
def bot_ilink_status():
    """直连聚合状态:待扫码态 + 各成员实例状态(多人记账)。
    非管理员仅见自己有权访问的账本的绑定（与 qrcode/unbind 的按账本收口同口径）。"""
    return {"mode": _bot_cfg().get("mode", "off"),
            "pending": _ilink.pending(), "bots": _mine_only(_ilink.states())}

@app.post("/api/bot/ilink/unbind")
def bot_ilink_unbind(body: dict):
    """解绑成员:停实例 + 删凭证(白名单保守不动,孤儿 id 无害)。
    权限与 qrcode 同口径：admin 全局管，普通用户只能解绑自己账本的绑定。"""
    aid = str(body.get("account_id") or "")
    if not aid:
        raise HTTPException(400, "account_id 必填")
    cfg = config.load()
    entries = bot_ilink.ilink_entries(cfg)
    entry = next((e for e in entries if str(e.get("account_id")) == aid), None)
    if entry and _uid() is not None and not _isadmin():
        _ledger_access(int(entry.get("ledger") or 0))
    cfg.setdefault("bot", {})["ilink"] = [e for e in entries
                                          if str(e.get("account_id")) != aid]
    config.save(cfg)
    _bot_apply()
    return {"ok": True}

@app.get("/api/bot/ilink/qrimg")
def bot_ilink_qrimg():
    """二维码图片：qrcode_img_content 是「待编码的跳转 URL」
    （https://liteapp.weixin.qq.com/q/...?qrcode=...&bot_type=3）——
    服务端用 segno 把 URL 生成 PNG（同源回图，无混合内容/防盗链问题）。"""
    img = _ilink.qr_img()
    if not img:
        raise HTTPException(404, "尚未发起扫码登录")
    if not img.startswith(("http://", "https://")):
        raise HTTPException(502, f"无法识别的二维码内容: {img[:60]}")
    try:
        import segno
    except ImportError:
        raise HTTPException(503, "缺 segno 依赖（requirements.txt 升级后重装应用）")
    buf = io.BytesIO()
    segno.make(img, error="m").save(buf, kind="png", scale=8, border=2)
    return Response(buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-cache"})

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--uds")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18710)
    args = ap.parse_args()
    if args.uds:
        uvicorn.run(app, uds=args.uds, log_level="info")
    else:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
