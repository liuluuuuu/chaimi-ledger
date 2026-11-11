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
import sqlite3
from contextlib import asynccontextmanager
from collections import OrderedDict
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

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response, StreamingResponse)
from pydantic import BaseModel, field_validator

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

@app.exception_handler(OverflowError)
async def _overflow_400(_, exc):
    """超大整数绑参的最后兜底（前端口径的 400，而非裸 500）：具体入口已有 _id64_ok/
    _budget_cap 的精确拦截（404/422 带原因），这里只兜请求体里其余整型字段
    （ResolveIn.refund_for、组 id、成员 uid 等）漏网的超大值。"""
    return JSONResponse(status_code=400, content={"detail": "数值超出范围"})

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

def _is_member() -> bool:
    """登录的普通用户（非管理员）。bot 端点族的权限前提单点：admin 全局管、
    未登录（多用户关闭）不设限、成员限自己账本（各端点在此之上做账本级判定）。"""
    return _uid() is not None and not _isadmin()

def _uid_header_ok(raw: str) -> bool:
    """uid 头合法性（trim_identity 用，提成纯函数便于钉子测试）：
    isascii 前置——上标数字（"²"）isdigit 为 True 但 int() 抛 ValueError → 500；
    阿拉伯-印度数字 int() 可解析出非预期 uid。身份只认 ASCII 十进制正整数
    （isascii+isdigit 成立后 int() 不可能抛，无需 try）。"""
    return bool(raw) and raw.isascii() and raw.isdigit() and int(raw) > 0

@app.middleware("http")
async def trim_identity(request: Request, call_next):
    """网关身份解析：X-Trim-Userid/Username/Isadmin → 请求级 ContextVar（官方文档
    developer.fnnas.com 统一网关章；应用只监听飞牛网关 UDS + 本地开发入口，头不可被
    外部伪造）。无头=系统上下文；uid 非正整数一律 401（防脏值落库），不静默当系统上下文。"""
    raw = (request.headers.get("x-trim-userid") or "").strip()
    if not raw:
        return await call_next(request)
    if not _uid_header_ok(raw):
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
_REV_WAIT_SEATS = threading.Semaphore(32)   # 长轮询并发座位：挂起期独占线程池令牌（默认 40），
                                   # 不设限时约 32+ 个页面/设备即可占满线程池，全站 API（含 /healthz）
                                   # 排队饥饿——满座时立即返回当前代数，客户端 300ms 后重试即可

def _bump_rev() -> None:
    with _REV_COND:
        _REV["n"] += 1
        _REV_COND.notify_all()

def _bump_lrev() -> None:
    """账本列表代数 +1（create/update/delete_ledger、共享/移除成员、恢复备份）。"""
    with _REV_COND:
        _LREV["n"] += 1
        _REV_COND.notify_all()

def _bot_apply() -> None:
    """机器人热切换（DESIGN 十章）：direct=按凭证列表差量启停，其余全停。"""
    cfg = config.load()
    if (cfg.get("bot") or {}).get("mode") == "direct":
        _ilink.apply(bot_ilink.ilink_entries(cfg))
    else:
        _ilink.stop_all()

def _edit_bot_entries(hit, rewrite) -> None:
    """按命中条件改写 conf 的机器人绑定列表，改完 _bot_apply 差量对账实例
    （组改名同步/删账本清理共用骨架）。无命中不动 conf（也避免 edit() 空写盘）。"""
    if not any(hit(e) for e in bot_ilink.ilink_entries(config.load())):
        return
    with config.edit() as cfg:   # 读-改-写持锁（与扫码/设置页等并发写方互斥）
        entries = bot_ilink.ilink_entries(cfg)
        cfg.setdefault("bot", {})["ilink"] = rewrite(entries)
    _bot_apply()

def _sync_bot_labels(ledger: int, old: str, new: str) -> None:
    """组改名同步机器人绑定 label：bot 记账归属=绑定 label（bot_core 以 attribution=label
    写 tags），改名不同步会让该成员经机器人记的账一直挂到旧组名下（归属统计在两个名字下
    分裂）。conf 改完 _bot_apply 差量重建（label 变化也会重建，见 bot_ilink.apply），
    运行实例即时生效，无需重启或重扫。"""
    hit = lambda e: bot_core.entry_ledger(e) == int(ledger) and e.get("label") == old
    _edit_bot_entries(hit, lambda es: [{**e, "label": new} if hit(e) else e for e in es])

def _purge_bot_entries(lid: int) -> None:
    """删账本连带清理机器人绑定：conf 条目与运行实例一并撤——否则实例继续为一个已不
    存在的账本轮询/记账（INSERT 因外键失败），白名单孤儿 id 无害保留。"""
    _edit_bot_entries(lambda e: bot_core.entry_ledger(e) == int(lid),
                      lambda es: [e for e in es if bot_core.entry_ledger(e) != int(lid)])

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
    # 版本号与上传上限注入:版本供前端 cv 信标(日志一眼可见各客户端跑的哪个版本)与
    # ?v= 缓存换血;上限供前端上传预检(core.js MAX_UPLOAD_B 单源派生,不再手抄同值)
    text = text.replace("<head>",
                        f'<head><script>window.CHAIMI_VER="{v}";'
                        f'window.CHAIMI_MAX_MB={MAX_UPLOAD // 1048576}</script>' + base, 1)
    # 静态引用一律带 ?v=版本:版本一变 URL 即变,任何缓存层(浏览器/fnOS 网关)必取新文件。
    # 单靠 no-cache 不够——网关可能按扩展名缓存 .js,升级后出现"新 HTML 引用旧 JS"
    # (曾出现新 HTML 引用旧 JS 导致按钮无反应),HTML 里的引用链必须随版本整体换血
    text = re.sub(r'((?:src|href)="static/[^"?]+)(?=")', rf'\1?v={v}', text)
    # no-store:HTML 本体不允许任何层缓存——网关若缓存了旧 HTML,里面的旧 JS 引用会一直
    # 命中旧缓存,版本角标/?v= 都救不回来;JS/CSS 靠 ?v= 换 URL,不依赖此头
    return HTMLResponse(text, headers={"Cache-Control": "no-store"})

def _date_where(frm: str, to: str, col: str = "t.date") -> tuple[str, list]:
    """日期区间过滤（流水列表/导出 CSV·xlsx/小票 zip 共用）：frm 含端 >=；
    to 一律排他上界（裸次日/周期首日，前端经 rangeDto 单源转换）<。
    片段构造委托 db.opt_bounds 单源（db 侧统计同口径），边界语义不再两处漂移。"""
    return dbm.opt_bounds(frm, to, col)

def _safe_name(name: str) -> str:
    """图片/小票文件名校验（app_image/get_receipt 共用）：拒绝路径穿越与分隔符。"""
    if not name or ".." in name or "/" in name or "\\" in name:
        raise HTTPException(400)
    return name

def _id64_ok(v) -> bool:
    """id 在 SQLite int64 正数范围内：超界的绑参会 OverflowError 500，各入口
    （账本行/引用校验/ids 过滤/批量取行）统一先按此拦下。"""
    return 0 < v < 2 ** 62

def _ledger_row(ledger_id: int):
    """取账本行（owner_uid）；不存在 404（_ledger_access/_owner_or_403 共用）。"""
    if not _id64_ok(ledger_id):
        raise HTTPException(404, "账本不存在")
    row = dbm.connect().execute("SELECT owner_uid FROM ledgers WHERE id=?", (ledger_id,)).fetchone()
    if not row:
        raise HTTPException(404, "账本不存在")
    return row

def _ledger_access(ledger_id: int) -> None:
    """账本访问判定（MULTIUSER-PLAN §2.3）：系统上下文放行（机器人/本地开发），
    但同样要求账本存在——此前系统上下文跳过存在性检查，对已删账本写入会走到
    FK 插入变 500，import_confirm 等端点为此各自补丁，现收口在此一处；
    飞牛用户须为 owner 或共享成员。不存在 404，无权 403。
    所有带 ledger 参数/可反查 ledger_id 的端点都经此咽喉点。"""
    uid = _uid()
    if uid is None:
        _ledger_row(ledger_id)
        return
    row = _ledger_row(ledger_id)
    if row["owner_uid"] == uid or dbm.connect().execute(
            "SELECT 1 FROM ledger_members WHERE ledger_id=? AND uid=?", (ledger_id, uid)).fetchone():
        return
    raise HTTPException(403, "无权访问该账本（可请所有者共享给你）")

def _my_ledger_ids(uid: int) -> set:
    """uid 可访问的账本 id 集（owner+共享成员，与 _ledger_access 同口径）。"""
    return {r["id"] for r in dbm.connect().execute(
        "SELECT id FROM ledgers WHERE owner_uid=? OR id IN "
        "(SELECT ledger_id FROM ledger_members WHERE uid=?)", (uid, uid))}

def _mine_only(entries: list) -> list:
    """非管理员视角过滤：机器人绑定只留自己有权账本的（与 qrcode/unbind 的按账本
    收口同口径）；admin/系统上下文全量。status 成员表绑定选取用。"""
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
    if _ledger_row(ledger_id)["owner_uid"] != uid:
        raise HTTPException(403, "仅账本所有者可操作")

def _row_of(table: str, rid: int):
    """按资源 id 取整行并过 ACL（id 类端点的越权防护：流水/分类/账户/映射/
    归属等 id 全库自增，不反查就等于跳过隔离）。行不存在/超界 404——取行与鉴权
    一次查询完成，调用方不再为兜并发删除窗口补第二个 SELECT。"""
    if not _id64_ok(rid):
        raise HTTPException(404)   # 超大 id 绑参 OverflowError 500，先按不存在拦下
    row = dbm.connect().execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
    if not row:
        raise HTTPException(404)
    _ledger_access(row["ledger_id"])
    return row

def _row_ledger(table: str, rid: int) -> int:
    return _row_of(table, rid)["ledger_id"]

# ---------- 页面 / 健康 ----------

_MEM_MAX_AGE = 365 * 24 * 3600   # 设备记忆 Cookie 有效期一年，每次给出主界面顺延

def _cookie_id(raw) -> int:
    """Cookie 里的 id：只认 18 位以内的 ASCII 十进制正整数（一定落在 SQLite int64 内），
    其余按 0（没有）。Cookie 是客户端能改的值，与 _uid_header_ok 一样从严。"""
    return int(raw) if raw and raw.isascii() and raw.isdigit() and len(raw) <= 18 else 0

def _device_memory(cookies) -> tuple[int, int] | None:
    """设备记忆问库验证。前端把「上次用的账本、这个账本上次用的用户」记在 Cookie 里
    （chaimi_ledger=账本id，chaimi_user_{账本id}=用户id，读写见 shared-lite.js）。
    有效 = 账本当前用户还能访问（_ledger_access 同口径）且用户还在这个账本下，与 home
    自动进入、app 的 restoreCurUser 判的是同一件事。有效返回 (账本id, 用户id)，否则 None。"""
    lid = _cookie_id(cookies.get("chaimi_ledger"))
    gid = _cookie_id(cookies.get(f"chaimi_user_{lid}")) if lid else 0
    if not gid:
        return None
    try:
        _ledger_access(lid)
    except HTTPException:
        return None
    if not dbm.connect().execute("SELECT 1 FROM attribution_groups WHERE id=? AND ledger_id=?",
                                 (gid, lid)).fetchone():
        return None
    return lid, gid

def _app_page(mem: tuple[int, int] | None) -> HTMLResponse:
    """主界面。设备记忆有效时把这两个 Cookie 由服务端重发一遍：一来续期；二来页面脚本
    写的 Cookie 在 Safari/iOS 上最多活 7 天，服务端下发的不受这个限制——home 选完人
    紧接着就请求 /app，刚写的记忆在这里换成长期的。Path 必须与前端一致（shared-lite.js
    的 _ckPath），否则浏览器会存成两份同名 Cookie。"""
    resp = _page("app.html")
    if mem:
        lid, gid = mem
        for k, v in (("chaimi_ledger", lid), (f"chaimi_user_{lid}", gid)):
            resp.set_cookie(k, str(v), max_age=_MEM_MAX_AGE, path=PREFIX.rstrip("/") or "/",
                            samesite="lax")
    return resp

@app.get("/", include_in_schema=False)
def index(request: Request):
    """应用入口。飞牛用户按设备记忆分流：记着有效用户的设备直接给主界面；没记忆或记忆
    失效（换设备、清了浏览器数据、无痕窗口、用户或账本已不在）的 302 到 home 选人/建
    用户。没带 Cookie 的设备不进 _device_memory、不问库——无从判断，直接去 home。
    记忆以前存在 localStorage，服务端看不见，只能一律先 302 到 home 让页面脚本判定，
    老用户每次打开都多过一个页面、启动画面中间闪一下；更早的版本看到有账本就给主界面，
    记忆失效时又会先渲染再被弹回。现在判定在服务端问库完成，两种情况都不再发生。
    系统上下文（机器人/本地开发）照常直出应用。
    分流前必须 _touch_fn_user：admin 认领无主账本与家人「打开过一次应用」的共享选人
    前提都不能丢档；认领会改账本归属，也得排在记忆验证之前。"""
    u = _user()
    if u:
        _touch_fn_user(u)
    # 没带 Cookie（新设备/清过数据/无痕窗口）无从判断：不问库，直接 302 home；
    # 带了才问库验证（_device_memory），有效直出主界面并由服务端续期
    mem = _device_memory(request.cookies) if request.cookies.get("chaimi_ledger") else None
    if u and not mem:
        return RedirectResponse(f"{PREFIX}/static/home.html", status_code=302,
                                headers={"Cache-Control": "no-store"})
    return _app_page(mem)

@app.get("/app", include_in_schema=False)
def app_page(request: Request):
    """主应用直入页：home 选定/自动进入的目标（enter → "app"）。不做入口分流，一定给
    主界面——home 刚写的记忆万一没被带到服务端（比如网关不转发 Cookie），走 / 会被
    当成没记忆弹回 home，与 home 的自动进入互为循环。"""
    return _app_page(_device_memory(request.cookies))

# 登录/用户选择页即静态文件 /static/home.html（自带 <base>，经 / 入口 302 或前端
# location.replace 到达）——曾有的 /home 别名路由（_page 版）无任何引用方，网关化后移除。

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

def _name_not_blank(v: str) -> str:
    """名称去空格非空（LedgerIn/CategoryIn/AccountIn 的 field_validator 共用，pydantic v2
    复用式同 _zero_id_none）：与归属组的 _group_body 同口径——此前 API 直调可建出空名
    账本/分类/账户，下拉与列表出现空白项。ValueError 由 pydantic 统一转 422。"""
    v = (v or "").strip()
    if not v:
        raise ValueError("名称不能为空")
    return v

def _budget_cap(v):
    """预算上限校验（预算的三个写入口共用——LedgerIn/LedgerUpdIn/BudgetIn，
    pydantic v2 复用式同 _name_not_blank）：预算存 INTEGER 列，int64 外的绑参会
    OverflowError 500（与金额上限同款钳制）。"""
    if v is not None and not 0 <= v <= 2 ** 63 - 1:
        raise ValueError("预算超出范围")
    return v

class LedgerIn(BaseModel):
    name: str
    icon: str = ""
    currency: str = "CNY"
    budget: Optional[int] = None
    _nb = field_validator("name")(_name_not_blank)
    _budget_range = field_validator("budget")(_budget_cap)

@app.get("/api/ledgers")
def list_ledgers(all: bool = False):
    """账本列表：飞牛用户=我拥有+共享给我（附 mine 标记，false=共享来的）；
    ?all=1 仅 admin（设置页机器人绑定的账本下拉需全库账本名）；系统上下文返回全部。
    过滤口径复用 _my_ledger_ids（与 _ledger_access 同源，不再各写一份 SQL）。"""
    uid = _uid()
    rows = [dict(r) for r in dbm.connect().execute("SELECT * FROM ledgers ORDER BY sort, id")]
    if uid is None or (all and _isadmin()):
        return rows
    mine = _my_ledger_ids(uid)
    return [{**r, "mine": r["owner_uid"] == uid} for r in rows if r["id"] in mine]

@app.post("/api/ledgers")
def create_ledger(body: LedgerIn):
    with dbm.connect() as c:
        lid = dbm.create_ledger(c, body.name, body.icon, body.currency, _uid(), body.budget)
    _bump_lrev()   # 各端账本下拉即时出现新账本
    return {"id": lid}

@app.put("/api/ledgers/{lid}/category-source")
def set_ledger_category_source(lid: int, body: dict):
    """对刚建的账本应用分类来源（home 建用户页「分类来自」选中即应用）：
    body.from=源账本 id → 分类+来源映射整体替换为源账本复制（含 learned）；
    from=null → 恢复内置默认。仅限尚无流水的账本（删分类会砸流水引用）；
    源账本须可见（403/404 同口径）。"""
    src = body.get("from")
    if src is not None and (isinstance(src, bool) or not isinstance(src, int) or src == lid):
        raise HTTPException(400, "from 须为其他账本 id 或 null")
    _owner_or_403(lid)
    if src is not None:
        _ledger_access(src)
    with dbm.connect() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM transactions WHERE ledger_id=?", (lid,)).fetchone()["n"]
        if n:
            raise HTTPException(400, "仅限尚无流水的账本调整分类来源")
        dbm.replace_ledger_categories(c, lid, src)
    _bump_rev()   # 分类/映射已变：各端分类下拉与映射表刷新

class LedgerUpdIn(BaseModel):
    """PUT /api/ledgers/{id}：部分更新——缺省字段保持原值（budget 本就是 COALESCE 语义，
    1.0.11 起 name/icon/currency 同口径）。此前全量替换语义逼得前端改币种前先读回
    name/icon 再回显，漏带一处就被清空（lost-update 由接口缺陷逼出来的绕路）。"""
    name: Optional[str] = None
    icon: Optional[str] = None
    currency: Optional[str] = None
    budget: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _nb_opt(cls, v):
        return _name_not_blank(v) if v is not None else v

    _budget_range = field_validator("budget")(_budget_cap)

@app.put("/api/ledgers/{lid}")
def update_ledger(lid: int, body: LedgerUpdIn):
    _owner_or_403(lid)
    with dbm.connect() as c:
        c.execute("UPDATE ledgers SET name=COALESCE(?, name), icon=COALESCE(?, icon), "
                  "currency=COALESCE(?, currency), budget=COALESCE(?, budget) WHERE id=?",
                  (body.name, body.icon, body.currency, body.budget, lid))
    _bump_lrev()   # 名字/币种变了，各端下拉标签同步
    _bump_rev()    # 预算变了，统计与设置页预算行跨端同步
    return {"ok": True}

class BudgetIn(BaseModel):
    """独立预算端点的写体（预算的第三个写入口，与 LedgerIn/LedgerUpdIn 同挂 _budget_cap——
    此前漏挂：直调传超 int64 预算 500、传负数静默落库，AI 查账与设置页的折算随之错乱）。"""
    budget: Optional[int] = None
    _budget_range = field_validator("budget")(_budget_cap)

@app.put("/api/ledgers/{lid}/budget")
def update_ledger_budget(lid: int, body: BudgetIn):
    """预算是账本级共享设置：owner 与共享成员都可改（2026-10-29 修订；
    名称/币种/图标仍 owner-only，走 update_ledger）。"""
    _ledger_access(lid)
    with dbm.connect() as c:
        c.execute("UPDATE ledgers SET budget=COALESCE(?, budget) WHERE id=?",
                  (body.budget, lid))
    _bump_rev()    # 预算行/统计跨端刷新
    _bump_lrev()   # 各端 ledgersCache 随账本列表代数换血——预算展示读该缓存，只 bump rev 对端数字不更新
    return {"ok": True}

@app.delete("/api/ledgers/{lid}")
def delete_ledger(lid: int):
    _owner_or_403(lid)
    with dbm.connect() as c:
        # import_batches/ledger_members 一并清（审计发现的历史残余：漏清会留孤儿行）
        for t in ("transactions", "import_batches", "category_mappings", "categories",
                  "accounts", "attributions", "attribution_groups", "ledger_members"):
            c.execute(f"DELETE FROM {t} WHERE ledger_id=?", (lid,))
        c.execute("DELETE FROM ledgers WHERE id=?", (lid,))
    _purge_bot_entries(lid)   # 机器人绑定凭证/实例连带撤销（否则为已删账本空轮询）
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
# 加载各落一次档，多标签/高频刷新即是写放大）；admin 认领是否还有活干直接问库，不缓存
_TOUCH_THROTTLE_S = 300
_touch_state: dict = {}          # uid -> (username, isadmin, 上次写库 time.time())
_ORPHAN_LEDGER_SQL = "SELECT 1 FROM ledgers WHERE owner_uid IS NULL LIMIT 1"   # 认领预检与事务内权威检查共用

def _admin_claim_done() -> bool:
    """无主账本是否已认领完毕——真相只在库里，进程不缓存：恢复多用户化之前的旧备份
    后天然重开认领，不存在需要复位的进程状态。查询失败按未完成处理，下一轮重试。"""
    try:
        return not dbm.connect().execute(_ORPHAN_LEDGER_SQL).fetchone()
    except Exception as e:
        log.warning(f"认领预检查询失败(按未完成处理，下轮重试): {type(e).__name__}: {e}")
        return False

def _touch_fn_user(u: dict) -> None:
    """打开应用落档（whoami 与入口 / 共用）：known_users UPSERT（共享选人器数据源，
    fnOS 无用户列表 API）；admin 打开时把升级前的无主账本归属自己（MULTIUSER-PLAN
    §3.5 定案：旧数据固定给 admin）。认领是否还有活干每次问库（_admin_claim_done），
    恢复旧备份后自动重开。落档失败（如 DB 被长写事务锁住）静默放行：入口判定不因此
    500，下次打开再落。"""
    ts = time.time()
    prev = _touch_state.get(u["uid"])
    unchanged = (prev and prev[0] == u["username"] and prev[1] == bool(u["isadmin"])
                 and ts - prev[2] < _TOUCH_THROTTLE_S)
    if unchanged and (not u["isadmin"] or _admin_claim_done()):
        return
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with dbm.connect() as c:
            if not unchanged:
                c.execute(
                    "INSERT INTO known_users(uid, username, isadmin, first_seen, last_seen) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(uid) DO UPDATE SET username=excluded.username, "
                    "isadmin=excluded.isadmin, last_seen=excluded.last_seen",
                    (u["uid"], u["username"], 1 if u["isadmin"] else 0, now, now))
            # 仍有无主账本才发 UPDATE（常态 0 行零写放大）；完成与否不打标——下次问库
            if u["isadmin"] and c.execute(_ORPHAN_LEDGER_SQL).fetchone():
                n = c.execute("UPDATE ledgers SET owner_uid=? WHERE owner_uid IS NULL",
                              (u["uid"],)).rowcount
                if n:
                    log.info(f"多用户迁移：{n} 个无主账本归属 admin「{u['username']}」(uid={u['uid']})")
        _touch_state[u["uid"]] = (u["username"], bool(u["isadmin"]), ts)
    except Exception as e:
        # 静默放行口径不变（docstring：入口不因 DB 忙 500），但零日志会掩盖持续故障——留痕
        log.warning(f"用户落档失败(忽略): uid={u.get('uid')} {type(e).__name__}: {e}")

@app.get("/api/users")
def list_users():
    """打开过应用的飞牛用户（共享选人器数据源；家庭内网应用，用户名互通无隐私边界）。"""
    return [dict(r) for r in dbm.connect().execute(
        "SELECT uid, username, isadmin, last_seen FROM known_users ORDER BY last_seen DESC")]

@app.get("/api/ledgers/{lid}/members")
def list_members(lid: int):
    _owner_or_403(lid)
    return [dict(r) for r in dbm.connect().execute(
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
    if not dbm.connect().execute("SELECT 1 FROM known_users WHERE uid=?", (uid,)).fetchone():
        raise HTTPException(400, "该用户还没打开过柴米记账，请对方先登录飞牛打开一次应用")
    with dbm.connect() as c:
        # added_at 显式给本地时（老库 SCHEMA 的 DEFAULT datetime('now') 是 UTC，
        # 显式值新旧库口径一致；与 imported_at 的 2026-11 时区迁移同批）
        c.execute("INSERT OR IGNORE INTO ledger_members(ledger_id, uid, added_at) VALUES(?,?,?)",
                  (lid, uid, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    _bump_lrev()   # 被共享方在线即见新账本（lrev 触发重拉账本下拉）
    return {"ok": True}

@app.delete("/api/ledgers/{lid}/members")
def del_member(lid: int, uid: int):
    _owner_or_403(lid)
    with dbm.connect() as c:
        c.execute("DELETE FROM ledger_members WHERE ledger_id=? AND uid=?", (lid, uid))
    _bump_lrev()   # 被移除方正看着该账本 → lrev 触发回落选择页，不再对着 403 空转
    return {"ok": True}

# ---------- 分类 / 账户 ----------

@app.get("/api/categories")
def list_categories(ledger: int):
    _ledger_access(ledger)
    rows = [dict(r) for r in dbm.connect().execute(
        "SELECT c.*, p.name AS parent_name FROM categories c "
        "LEFT JOIN categories p ON c.parent_id=p.id WHERE c.ledger_id=? ORDER BY c.sort, c.id", (ledger,))]
    return rows

class CategoryIn(BaseModel):
    name: str
    type: str = "expense"
    parent_id: Optional[int] = None
    icon: str = ""
    _nb = field_validator("name")(_name_not_blank)

    @field_validator("type")
    @classmethod
    def _type_domain(cls, v):
        """值域单源（db.TX_TYPES）：垃圾 type（"foo"）此前可建出统计静默忽略、
        下拉里的死分类。ValueError 由 pydantic 统一转 422。"""
        if v not in dbm.TX_TYPES:
            raise ValueError(f"type 必须是 {'/'.join(sorted(dbm.TX_TYPES))}")
        return v

@app.post("/api/categories")
def create_category(ledger: int, body: CategoryIn):
    _ledger_access(ledger)
    if body.parent_id is not None:
        # 父分类必须属于本账本的一级分类：跨账本 parent（id 可枚举）会让分类列表
        # LEFT JOIN 出他账本名（越权泄漏），分类来源复制 copy_ledger_categories
        # 的 id_map 直接 KeyError 500
        ok = dbm.connect().execute(
            "SELECT 1 FROM categories WHERE id=? AND ledger_id=? AND parent_id IS NULL",
            (body.parent_id, ledger)).fetchone()
        if not ok:
            raise HTTPException(400, "父分类不存在、不属于本账本或不是一级分类")
    with dbm.connect() as c:
        cur = c.execute(
            "INSERT INTO categories(ledger_id, type, name, parent_id, icon) VALUES(?,?,?,?,?)",
            (ledger, body.type, body.name, body.parent_id, body.icon))
    _bump_rev()   # 账本级设置跨端同步：共享账本任一方改动即唤醒对端刷新（账户/映射/归属分组同）
    return {"id": cur.lastrowid}

@app.put("/api/categories/{cid}")
def update_category(cid: int, body: CategoryIn):
    _row_ledger("categories", cid)
    with dbm.connect() as c:
        c.execute("UPDATE categories SET name=?, icon=? WHERE id=?", (body.name, body.icon, cid))
        # v6 起映射走 category_id 外键，改名自动联动，无需同步映射表
    _bump_rev()
    return {"ok": True}

@app.delete("/api/categories/{cid}")
def delete_category(cid: int):
    """删分类必须回炉关联流水（列表与统计对其统一按「未分类」口径可见，回炉让用户
    重新归类）——但留审计日志，静默大面积改数据必须可追溯。"""
    _row_ledger("categories", cid)
    with dbm.connect() as c:
        # 连子分类流水一并回炉：只回父分类会留下指向已删子分类的悬空 category_id
        # （无 FK；LEFT JOIN 口径下该行按「未分类」展示）；映射也按同集清理
        kids = [r[0] for r in c.execute("SELECT id FROM categories WHERE parent_id=?", (cid,))]
        ids = (cid, *kids)
        marks = ",".join("?" * len(ids))
        # 已关联退款的行（refund_for 非空）只清分类不打回待确认——它们是正确行；打回会把
        # 队列塞满（删「退款」分类时全库已匹配退款一起进队列，同 1.0.1 删账户事故形态）。
        # 其展示回落类型名「转账」，统计不受影响（transfer 不进分类聚合）
        n1 = c.execute(f"UPDATE transactions SET category_id=NULL, need_confirm=1 "
                       f"WHERE category_id IN ({marks}) AND refund_for IS NULL", ids).rowcount
        n2 = c.execute(f"UPDATE transactions SET category_id=NULL "
                       f"WHERE category_id IN ({marks}) AND refund_for IS NOT NULL", ids).rowcount
        c.execute(f"DELETE FROM category_mappings WHERE category_id IN ({marks})", ids)
        c.execute("DELETE FROM categories WHERE parent_id=?", (cid,))
        c.execute("DELETE FROM categories WHERE id=?", (cid,))
    n = n1 + n2
    if n:
        log.info(f"删除分类 #{cid}：{n} 笔流水置空分类（其中 {n1} 笔回炉待确认重新归类）")
    _bump_rev()
    return {"ok": True, "affected": n}

# 存储不动（tags.attribution 仍存原始名），两条归组路径：
#   ① 名单路径：归属名查 attributions.group_id（导入的 liulu·支付宝 → 组「老公」，导入自动映射）
#   ② 同名路径：归属名恰与组名相同（编辑弹窗选分组保存的组名）→ 直接归该组
# json_extract 全部走 db.jx 安全片段：库中脏 tags 不再让列表/导出 500（见 db.jx 注释）
_attr_jx = dbm.jx("t.tags")
_ATTR_RESOLVE = (
    "LEFT JOIN attributions _ax ON _ax.ledger_id=t.ledger_id "
    f"AND _ax.name={_attr_jx} "
    "LEFT JOIN attribution_groups _gx ON _gx.id=_ax.group_id "
    "LEFT JOIN attribution_groups _g2 ON _g2.ledger_id=t.ledger_id "
    f"AND _g2.name={_attr_jx} ")
_ATTR_EXPR = f"COALESCE(_gx.name, _g2.name, {_attr_jx})"
_ATTR_DISPLAY = (_ATTR_EXPR + " AS attr_display")

def _attr_group_absorb(c, ledger: int, name: str) -> None:
    """建组/改组时吸收同名归属行：其流水的归属名==组名，经同名路径仍归本组，名单不保留孤行。"""
    c.execute("DELETE FROM attributions WHERE ledger_id=? AND name=?", (ledger, name))

def _user_tx_attribution_where(ledger: int, name: str) -> tuple[str, tuple]:
    """按归属名命中「用户侧」流水的 WHERE 片段+参数（组改名/删组改写 tags 共用单源）：
    导入流水的 tags.attribution 是账单原始归属（真实数据），改组名/删组都不得触碰
    ——旧版改名 UPDATE 无此守卫，把与导入归属同名的组改名会把原始归属永久改写。"""
    ph = ",".join("?" * len(dbm.IMPORT_SOURCES))
    return (f"ledger_id=? AND {dbm.jx('tags')}=? "
            f"AND COALESCE(source,'manual') NOT IN ({ph})", (ledger, name, *dbm.IMPORT_SOURCES))

def _clear_user_attribution(c, ledger: int, name: str) -> None:
    """删组时清掉用户侧（手动/AI）流水存的该组名；导入流水的原始归属保留（账单真实数据）。"""
    frag, params = _user_tx_attribution_where(ledger, name)
    c.execute(f"UPDATE transactions SET tags=json_remove(tags,'$.attribution') WHERE {frag}",
              params)

def _check_attr_group_name(c, ledger: int, name: str, gid: int | None = None) -> None:
    """组名唯一（改名传 gid 排除自身）；与归属名同名的场景走吸收而非报错（见 _attr_group_absorb）。"""
    row = c.execute("SELECT id FROM attribution_groups WHERE ledger_id=? AND name=?",
                    (ledger, name)).fetchone()
    if row and row["id"] != gid:
        raise HTTPException(400, f"分组「{name}」已存在")

@app.get("/api/attribution-groups")
def list_attr_groups(ledger: int):
    _ledger_access(ledger)
    return [dict(r) for r in dbm.connect().execute(
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
    with dbm.connect() as c:
        _check_attr_group_name(c, ledger, name)
        _attr_group_absorb(c, ledger, name)   # 同名归属行吸收（其流水经同名路径仍归本组）
        try:
            # 查重与 INSERT 间的并发窗口由唯一索引兜底（延迟事务下检查 SELECT 不持写锁）
            cur = c.execute("INSERT INTO attribution_groups(ledger_id, name, avatar) VALUES(?,?,?)",
                            (ledger, name, avatar))
        except sqlite3.IntegrityError:
            raise HTTPException(400, f"分组「{name}」已存在")
    _bump_rev()
    return {"ok": True, "id": cur.lastrowid}

class AttrGroupUpdIn(BaseModel):
    """PUT /api/attribution-groups/{gid}：部分更新——name/avatar 各自缺省=保持原值
    （此前全量替换，改名须随带 avatar、换头像须随带 name，漏带即被重置；
    收拢时也不能把 name 设必填——换头像的前端只发 avatar，必填等于换方向造回耦合）。"""
    name: Optional[str] = None
    avatar: Optional[str] = None

@app.put("/api/attribution-groups/{gid}")
def rename_attr_group(gid: int, body: AttrGroupUpdIn):
    name = (body.name or "").strip()
    if body.name is not None and not name:
        raise HTTPException(400, "用户名不能为空")
    if body.avatar is not None:
        _check_avatar(body.avatar)
    with dbm.connect() as c:
        if not _id64_ok(gid):
            raise HTTPException(404, "分组不存在")
        g = c.execute("SELECT ledger_id, name FROM attribution_groups WHERE id=?", (gid,)).fetchone()
        if not g:
            raise HTTPException(404, "分组不存在")
        _ledger_access(g["ledger_id"])
        if name and name != g["name"]:
            _check_attr_group_name(c, g["ledger_id"], name, gid)
            # 手动账存的是组名（编辑弹窗选分组）：改名同步改写已存旧组名的用户侧流水，否则会「掉组」；
            # 导入流水的原始归属不动（_user_tx_attribution_where 单源守卫）
            frag, params = _user_tx_attribution_where(g["ledger_id"], g["name"])
            c.execute(f"UPDATE transactions SET tags=json_set(tags,'$.attribution',?) WHERE {frag}",
                      (name, *params))
            _attr_group_absorb(c, g["ledger_id"], name)
        c.execute("UPDATE attribution_groups SET name=COALESCE(?, name), avatar=COALESCE(?, avatar) WHERE id=?",
                  (name or None, body.avatar, gid))
    if name and name != g["name"]:
        _sync_bot_labels(g["ledger_id"], g["name"], name)   # 绑定 label 跟随组改名（见函数注释）
    _bump_rev()
    return {"ok": True}

@app.delete("/api/attribution-groups/{gid}")
def delete_attr_group(gid: int):
    # FK ON DELETE SET NULL：名单成员自动回未分组；导入流水的原始归属名不动；
    with dbm.connect() as c:
        if not _id64_ok(gid):
            raise HTTPException(404, "用户不存在")
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
    return [dict(r) for r in dbm.connect().execute(
        "SELECT a.*, g.name AS group_name FROM attributions a "
        "LEFT JOIN attribution_groups g ON a.group_id=g.id "
        "WHERE a.ledger_id=? ORDER BY a.id", (ledger,))]

class AttrGroupSetIn(BaseModel):
    group_id: int | None = None

@app.put("/api/attributions/{aid}")
def update_attribution(aid: int, body: AttrGroupSetIn):
    """归属设分组/移出组（group_id=null 移出）。只改名单表，历史流水不动，统计口径即时生效。"""
    with dbm.connect() as c:
        if not _id64_ok(aid):
            raise HTTPException(404, "归属不存在")
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
    """账户列表。带区间时附账本级汇总（设置页账户表+预算行用）：
    net_expense=含未选账户支出的区间净支出（退款按原消费日归因，与按账户拆分的
    spend 列因口径差异可能不相等）、months=区间覆盖的自然月数（预算折算，
    db.range_months 单源）。汇总放顶层而非平铺到行上——账户全删空时（流水都成
    「未指定账户」）汇总仍在，此前借道首行传输会静默丢。"""
    # frm：形参不能叫 from（Python 关键字），query 名用 alias 保持 ?from=；
    # to 为排他上界（裸次日/周期首日，前端 accScopeInfo 经 rangeDto 单源转换）
    _ledger_access(ledger)
    rows = [dict(r) for r in dbm.connect().execute(
        "SELECT * FROM accounts WHERE ledger_id=? ORDER BY sort, id", (ledger,))]
    # spend 口径（毛支出-退款转账）在 db.account_spend_map 唯一实现，与 AI kind=account 共用
    spend_map = dbm.account_spend_map(dbm.connect(), ledger, frm, to)
    # 全量流水笔数（不受日期范围影响）：删账户确认框提示"将置空 N 笔"用，
    # 口径与 delete_account 的两条 UPDATE 一致（付款列+收款列都算，按**笔**去重——
    # 此前 UNION 单列去重让 COUNT 恒为 1，删除确认框的笔数提示失真）
    cnt = {r[0]: r[1] for r in dbm.connect().execute(
        "SELECT aid, COUNT(DISTINCT tid) FROM ("
        "SELECT account_id AS aid, id AS tid FROM transactions WHERE ledger_id=? AND account_id IS NOT NULL "
        "UNION SELECT to_account_id AS aid, id AS tid FROM transactions WHERE ledger_id=? AND to_account_id IS NOT NULL"
        ") GROUP BY aid", (ledger, ledger))}
    for r in rows:
        g, rf = spend_map.get(r["id"], (0, 0))
        r["spend"] = g - rf
        r["tx_count"] = cnt.get(r["id"], 0)
    out: dict = {"items": rows}
    if frm or to:
        out["net_expense"] = dbm.stats_summary_range(dbm.connect(), ledger, *_span(frm, to))["net_expense"]
        out["months"] = dbm.range_months(*_span(frm, to))
    return out

class AccountIn(BaseModel):
    name: str
    type: str = "cash"
    _nb = field_validator("name")(_name_not_blank)

@app.post("/api/accounts")
def create_account(ledger: int, body: AccountIn):
    _ledger_access(ledger)
    with dbm.connect() as c:
        cur = c.execute("INSERT INTO accounts(ledger_id, name, type) VALUES(?,?,?)",
                        (ledger, body.name, body.type))
    _bump_rev()
    return {"id": cur.lastrowid}

@app.put("/api/accounts/{aid}")
def update_account(aid: int, body: AccountIn):
    _row_ledger("accounts", aid)
    with dbm.connect() as c:
        c.execute("UPDATE accounts SET name=?, type=? WHERE id=?",
                  (body.name, body.type, aid))
    _bump_rev()
    return {"ok": True}

@app.delete("/api/accounts/{aid}")
def delete_account(aid: int):
    """1.0.1 起不再把关联流水打回待确认——旧版 `need_confirm=1` 会把该账户全部
    历史流水瞬间塞满待确认队列（真机事故：删一个旧账户，次日发现待确认爆满）。
    账户置空即可：分类/统计不受影响，流水照常显示为「未指定账户」。"""
    name = _row_of("accounts", aid)["name"]
    c = dbm.connect()
    with c:
        # 收款账户列一并置空（与账户合并改指两列同口径）：旧版导出转账行带 to_account_id，
        # 只清付款列会让该行收款端悬空——编辑弹窗无此字段，_check_tx_refs 400 后无从修正
        n = c.execute("UPDATE transactions SET account_id=NULL WHERE account_id=?", (aid,)).rowcount
        n2 = c.execute("UPDATE transactions SET to_account_id=NULL WHERE to_account_id=?", (aid,)).rowcount
        c.execute("DELETE FROM accounts WHERE id=?", (aid,))
    log.info(f"删除账户 #{aid}「{name}」：{n} 笔付款列、{n2} 笔收款列置空（分类与确认状态不动）")
    _bump_rev()
    return {"ok": True, "affected": n}

# ---------- 流水 ----------

def _zero_id_none(v):
    """id 字段 0 → None（TxIn / ResolveIn 的 field_validator 共用，pydantic v2 复用式）：
    0 不是合法自增 id，写库即悬空引用（该行从带分类/账户名的列表里丢名字）。"""
    return v or None

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

    @field_validator("source")
    @classmethod
    def _source_domain(cls, v):
        """App 直写口只认 app（bot 路径走 insert_tx 不经 TxIn；PUT 不改 source）：白名单而非
        黑名单——此前只拦导入来源词，任意串（含 HTML）可入库再经流水列表回显；伪造导入
        来源还可逃过「导入侧数据不可改写」守卫、骗取归属平台后缀与导入去重键。ValueError → 422。"""
        if v != "app":
            raise ValueError("source 只能是 app（导入/机器人走各自写入口）")
        return v
    refund_for: Optional[int] = None

    @field_validator("type")
    @classmethod
    def _type_ok(cls, v):
        """type 值域校验单源在 db.check_tx_type（导入 confirm / 批量等同用）：
        坏 type 落库会让导出与统计永久 500，且无 UI 途径修复。
        ValueError 由 pydantic 统一转 422，无需在此重抛。"""
        dbm.check_tx_type(v)
        return v

    @field_validator("amount")
    @classmethod
    def _amount_ok(cls, v):
        """amount 值域校验单源在 db.check_tx_amount（同上）。"""
        dbm.check_tx_amount(v)
        return v

    # 0=未提供（与 _check_tx_refs 口径对齐）：防悬空引用落库——0 不是合法自增 id，
    # 写进 category_id/account_id 的悬空引用会让该行丢分类/账户名（同删分类悬空事故）。
    # refund_for 不归一：0=取消关联是合法语义。
    _zero_ids_none = field_validator("category_id", "account_id", "to_account_id")(_zero_id_none)

def _check_tx_refs(c, ledger: int, category_id=None, account_id=None, to_account_id=None) -> None:
    """流水引用的 分类/账户 必须属于本账本（写边界统一入口，create/update/批量/resolve 共用）。
    反查缺失即 400——放行会把别的账本（可能他人私有）的分类名 JOIN 进本账本列表，
    对端删分类还会连锁把本账本流水打成待确认。id 全库自增可枚举，必须校验。
    分类只验归属不验层级（历史库可能仍有二级分类行被流水引用，编辑不改分类时不拦）。"""
    for v in (category_id, account_id, to_account_id):
        if v is not None and not _id64_ok(v):
            raise HTTPException(400, "引用 id 超出范围")
    if category_id:
        if not c.execute("SELECT 1 FROM categories WHERE id=? AND ledger_id=?",
                         (category_id, ledger)).fetchone():
            raise HTTPException(400, "分类不存在或不属于本账本")
    for aid in (account_id, to_account_id):
        if aid:
            if not c.execute("SELECT 1 FROM accounts WHERE id=? AND ledger_id=?",
                             (aid, ledger)).fetchone():
                raise HTTPException(400, "账户不存在或不属于本账本")

def _check_receipt_path(c, ledger: int, receipt_path: str | None) -> str | None:
    """TxIn.receipt_path 写入闸（create/update 共用）：只认 receipts/文件名 单一形态
    （上传落盘恒为 receipts/时间戳_hex.jpg，见 _save_receipt；历史裸名行已由
    db 迁移归一），校验跨账本占用后返回入库用的规范路径。
    两个 falsy 值语义不同、不能 or 归一：None=保持原值，""=清空（入库 NULL，
    update 的 CASE WHEN 三分依赖此区分）。预览体/请求体来自客户端——伪造他人
    账本的小票路径挂到自己流水上，会让 _receipt_ledger_gate 的账本交集判定
    放行，借 GET/crop 读出甚至裁剪覆盖他人图片。"""
    if receipt_path is None or receipt_path == "":
        return receipt_path
    m = re.fullmatch(r"receipts/([A-Za-z0-9_-]{1,64}\.(?:jpg|jpeg|png|webp))", receipt_path)
    if not m:
        raise HTTPException(400, "小票路径不合法")
    lids = {r["ledger_id"] for r in c.execute(
        "SELECT ledger_id FROM transactions WHERE receipt_path=?", (receipt_path,))}
    if lids - {ledger}:
        raise HTTPException(400, "该小票文件不属于本账本")
    return receipt_path

_TX_HEAD = ("SELECT t.*, c.name AS category_name, a.name AS account_name, "
            "ro.id AS ro_id, ro.date AS ro_date, ro.name AS ro_name, ro.note AS ro_note, "
            "ro.amount AS ro_amount, ro.type AS ro_type, ro.category_id AS ro_category_id, "
            "ro.account_id AS ro_account_id, ro.order_no AS ro_order_no, ro.merchant AS ro_merchant, "
            "ro.source AS ro_source, ro.tags AS ro_tags, ro.need_confirm AS ro_need_confirm, "
            "rc.name AS ro_category_name, ra.name AS ro_account_name, "
            + _ATTR_DISPLAY + " "
            "FROM transactions t "
            "LEFT JOIN categories c ON t.category_id=c.id "
            "LEFT JOIN accounts a ON t.account_id=a.id "
            "LEFT JOIN transactions ro ON ro.id=t.refund_for "
            "LEFT JOIN categories rc ON ro.category_id=rc.id "
            "LEFT JOIN accounts ra ON ro.account_id=ra.id "
            + _ATTR_RESOLVE +
            "WHERE t.ledger_id=?")

def _tx_conditions(ledger: int, _from: str, to: str, type: str, category: int,
                   need_confirm: bool, q: str, renqing_name: str, attribution: str,
                   with_refunds: bool, ids: str) -> list[tuple[str, list]]:
    """list_transactions 的筛选收集：以 (SQL 片段, 参数表) 二元组返回，片段与参数
    同组维护（此前两列平行 append，category 分支一次追加 7 个参数对位 7 个 ?，
    加筛选要同步两处）。"""
    conds: list[tuple[str, list]] = []
    if ids:
        # 按 id 精确取行（编辑弹窗打开时拉服务端最新值，防前端缓存的陈旧/残缺对象回写）：
        # 显式 id 列表与其余筛选正交叠加（调用方只传 ids 时其余筛选不生效）
        try:
            idl = [int(x) for x in ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "ids 必须是数字 id 列表")
        if len(idl) > 1000:
            raise HTTPException(400, "ids 过多（上限 1000）")   # 无上限直拼 IN 会超 SQLite 变量上限 → 500
        idl = [x for x in idl if _id64_ok(x)]   # 超界 id 不可能命中，滤掉防绑参溢出
        if idl:
            conds.append((f"AND t.id IN ({','.join('?' * len(idl))})", idl))
        else:
            conds.append(("AND 1=0", []))   # 全部超界：语义是取空集，而非退回无 ids 约束的全列表
    if q.strip():
        # 关键词命中 name/note/订单号/交易对方；归属按显示口径匹配（组名优先，_ATTR_RESOLVE 已 JOIN）；
        # 金额单独走精确匹配——LIKE 无法表达小数点，输入可解析为数字时按「分」比对。
        # 退款对只看主行（t），原交易由 ro_* 提供
        kw = f"%{q.strip()}%"
        or_conds = ["t.name LIKE ?", "t.note LIKE ?", "t.order_no LIKE ?",
                    "t.merchant LIKE ?",
                    "c.name LIKE ?", f"{_ATTR_EXPR} LIKE ?"]
        or_args = [kw] * 6
        # 金额精确匹配走 yuan_to_cents 单点收口（nan/inf/超界/全角脏值一律 None）：
        # 此处此前是第五处各写一遍防御的换算点（历史 nan/inf/长数字漏捕曾 500）
        cents = ib.yuan_to_cents(q.strip())
        # 超出 SQLite INTEGER（int64）不可能与任何流水金额相等，不追加金额条件
        if cents is not None and abs(cents) <= 2 ** 63 - 1:
            or_conds.append("t.amount=?")
            or_args.append(cents)
        conds.append(("AND (" + " OR ".join(or_conds) + ")", or_args))
    if _from or to:
        # to 一律排他上界（裸日期对 10 位纯日期行和 19 位带时间行都正确排他；
        # 补 00:00:00 反而会把 10 位的边界日手工流水包含进来）——「含当天」由
        # 前端 rangeDto/expRangeParams 单源转次日表达，服务端不再双编码兼容
        conds.append(_date_where(_from, to))
    if type:
        if with_refunds and type == "expense":
            # 汇总条「支出」为净额(毛−退款)：明细须带退款行（关联后 type=transfer，
            # 前端渲染退款双卡片）才能与条上数字对账；未关联退款仍是收入行不计入
            conds.append(("AND (t.type=? OR t.refund_for IS NOT NULL)", [type]))
        else:
            conds.append(("AND t.type=?", [type]))
    if category and not _id64_ok(abs(category)):
        raise HTTPException(400, "category 超出范围")
    if category == -1:
        # 「未分类」桶下钻（stats 饼图 db.UNCATEGORIZED_NAME 行）：无分类行 +
        # 指向无分类原消费的退款对（与常规分类过滤的退款连带取同构）。
        # 与饼图 LEFT JOIN 口径对齐：category_id 指向已删分类的悬挂行同算未分类
        conds.append(("AND (t.category_id IS NULL OR t.category_id NOT IN "
                      "(SELECT id FROM categories WHERE ledger_id=?) OR t.refund_for IN "
                      "(SELECT id FROM transactions WHERE ledger_id=? AND category_id IS NULL))",
                      [ledger, ledger]))
    elif category:
        # 连带取「退款行指向本分类消费」的对（退款行 type=transfer 且 category_id 常不落本分类，不连带取会丢失退款双卡片）
        cat_sql = "SELECT id FROM categories WHERE ledger_id=? AND (id=? OR parent_id=?)"
        conds.append((f"AND (t.category_id IN ({cat_sql}) "
                      f"OR t.refund_for IN (SELECT id FROM transactions WHERE ledger_id=? AND category_id IN ({cat_sql})))",
                      [ledger, category, category, ledger, ledger, category, category]))
    if need_confirm:
        conds.append(("AND t.need_confirm=1", []))
    if renqing_name:
        # 人情明细：分类=人情往来 + 名称精确匹配（与 /api/stats/renqing 的分组键同一表达式）
        conds.append((f"AND {dbm.RENQING_CAT_SQL}", [ledger]))
        conds.append((f"AND {dbm.RENQING_NAME_SQL} = ?", [renqing_name]))
    if attribution:
        # 归属下钻（统计页蝴蝶图条形）：与 /api/stats/attribution 同口径。
        # 分组行传组名（_ATTR_EXPR 双路径：名单映射/直名建组）；未分组行传
        # 「归属名·平台」统计键，剥后缀反解回 source 一并过滤
        base, _, plat = attribution.rpartition("·")
        src = {cn: s for s, cn in dbm.PLATFORM_CN.items()}.get(plat)
        if not src:
            base = attribution
        conds.append((f"AND {_ATTR_EXPR}=?", [base]))
        if src:
            conds.append(("AND t.source=?", [src]))
    return conds

def _refund_origin_dict(r) -> dict | None:
    """退款对的原交易摘要（前端用来在退款行下渲染第二张卡片）。"""
    if r["ro_id"] is None:
        return None
    return {
        "id": r["ro_id"], "date": r["ro_date"] or "", "name": r["ro_name"] or "",
        "note": r["ro_note"] or "", "amount": r["ro_amount"] or 0,
        "type": r["ro_type"] or "expense", "category_id": r["ro_category_id"],
        "account_id": r["ro_account_id"], "order_no": r["ro_order_no"] or "",
        "merchant": r["ro_merchant"] or "", "source": r["ro_source"] or "manual",
        "tags": dbm.jloads(r["ro_tags"]) or {}, "need_confirm": bool(r["ro_need_confirm"]),
        "category_name": r["ro_category_name"] or "", "account_name": r["ro_account_name"] or "",
    }

@app.get("/api/transactions")
def list_transactions(ledger: int, _from: str = Query("", alias="from"), to: str = "", type: str = "",
                      category: int = 0, need_confirm: bool = False, q: str = "",
                      renqing_name: str = "", attribution: str = "",
                      with_refunds: bool = False,
                      ids: str = "", page: int = 1, page_size: int = 50):
    _ledger_access(ledger)
    # 分页钳制：页号 ≥1、≤10^7（OFFSET 乘积须留 int64 内）；页大小 ≤500（明细弹窗最大口径），无上限放行会一次拉全账本
    page = max(1, min(page, 10 ** 7))
    page_size = max(1, min(page_size, 500))
    conds = _tx_conditions(ledger, _from, to, type, category, need_confirm,
                           q, renqing_name, attribution, with_refunds, ids)
    where = _TX_HEAD + " " + " ".join(frag for frag, _ in conds)
    args = [ledger] + [a for _, params in conds for a in params]
    # 全量过滤集一次聚合（COUNT+三类合计单遍扫描，此前 COUNT 与 GROUP BY 各扫一遍）：
    # 明细弹窗表头「支出/收入/退款」直接消费，不受分页截断——超页（100/200/500 笔封顶）
    # 时前端对当前页求和会偏低。refund=结果集内退款行（transfer 且带 refund_for）合计，
    # 真实转账不计入。
    agg = dbm.connect().execute(
        f"SELECT COUNT(*),"
        f" COALESCE(SUM(CASE WHEN type='expense' THEN amount END),0),"
        f" COALESCE(SUM(CASE WHEN type='income' THEN amount END),0),"
        f" COALESCE(SUM(CASE WHEN type='transfer' AND refund_for IS NOT NULL THEN amount END),0)"
        f" FROM ({where})", args).fetchone()
    total = agg[0]
    sums = {"expense": agg[1], "income": agg[2], "refund": agg[3]}
    rows = dbm.connect().execute(f"{where} ORDER BY t.date DESC, t.id DESC LIMIT ? OFFSET ?",
                          args + [page_size, (page - 1) * page_size]).fetchall()
    items = []
    for r in rows:
        d = dbm.tx_to_dict(r)
        d["refund_origin"] = _refund_origin_dict(r) if d.get("refund_for") else None
        items.append(d)
    return {"items": items, "total": total, "page": page, "page_size": page_size, "sums": sums}

def _tx_date(raw: str, old: str = "") -> str:
    """TxIn 日期 → 入库口径（db.norm_date）；认不出/日历非法给 400 而非 500。"""
    try:
        return dbm.norm_date(raw, old=old)
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.post("/api/transactions")
def create_transaction(ledger: int, body: TxIn):
    _ledger_access(ledger)
    with dbm.connect() as c:
        _check_tx_refs(c, ledger, body.category_id, body.account_id, body.to_account_id)
        rp = _check_receipt_path(c, ledger, body.receipt_path)
        tid = dbm.insert_tx(
            c, ledger, type=body.type, amount=body.amount, category_id=body.category_id,
            account_id=body.account_id, to_account_id=body.to_account_id, date=_tx_date(body.date),
            name=body.name, merchant=body.merchant, note=body.note, tags=body.tags,
            receipt_path=rp or None,   # 新建无「保持原值」语义：""与 None 都是无小票（NULL）
            source=body.source, ai_meta=body.ai_meta,
            order_no=body.order_no, need_confirm=body.need_confirm,
            manual_cat=bool(body.category_id))   # App 记一笔带分类=用户手选
        if body.refund_for:
            _link_refund(c, tid, body.refund_for, body.amount, confidence=None)
        # 新 expense 可能正是某条待确认退款的原消费（自愈缺口：此前只在 transfer+order_no
        # 时扫，手工补记原消费后队列里的退款不会自动链上）；与导入 apply 后必扫同口径
        if body.type == "expense" or (body.type == "transfer" and body.order_no):
            ib._rescan_unmatched_refunds(c, ledger)
    _bump_rev()   # App 侧写库同推刷新:唤醒所有页面的长轮询(与 bot 写库同源)
    return {"id": tid}

@app.put("/api/transactions/{tid}")
def update_transaction(tid: int, body: TxIn):
    if not _id64_ok(tid):
        raise HTTPException(404, "流水不存在")   # 超大 id 绑参 OverflowError 500，先按不存在拦下
    with dbm.connect() as c:
        # name/merchant/order_no 允许单独更新：传 null 保持原值（COALESCE），不传不会被清空
        # receipt_path：None=保持原值；""=清空(NULL)；非空=覆盖。SQLite 无「参数是否为 NULL」的
        # 内省，原 COALESCE(NULLIF(?, ''), receipt_path) 会把 "" 与 None 同视（都保留原值），
        # 故用 CASE WHEN 显式三分。
        old = c.execute("SELECT ledger_id, receipt_path, date, category_id, refund_for FROM transactions WHERE id=?",
                        (tid,)).fetchone()
        if not old:
            raise HTTPException(404, "流水不存在")
        _ledger_access(old["ledger_id"])
        # 已关联退款的行必须保持不计收支：改成 expense/income 会让该行既计入毛支出、
        # 又被 _refund_sum（不校验 r.type）按退款扣减原消费——一笔钱扣两次。前端有同款
        # 拦截（saveEditTx/txItemCatChange），这里是 API 直调的服务端兜底。
        if old["refund_for"] and body.type != "transfer":
            raise HTTPException(400, "该流水已关联退款，须保持不计收支；请先在下方取消关联再改类型")
        # 反方向守卫：本行是原消费、他行退款正指向本行时，type 改成非 expense 会让
        # _refund_sum（只认 type='expense' 的原消费）静默不再扣减——退款行仍显示已关联，
        # 扣减却消失。须先取消下游退款关联再改类型（与 _link_refund 的不变式对齐）。
        if (body.type != "expense" and not old["refund_for"]
                and c.execute("SELECT 1 FROM transactions WHERE refund_for=? LIMIT 1", (tid,)).fetchone()):
            raise HTTPException(400, "有退款流水关联在本行上，改成该类型会让退款扣减失效；请先取消那条退款行的关联")
        _check_tx_refs(c, old["ledger_id"], body.category_id, body.account_id, body.to_account_id)
        rp = _check_receipt_path(c, old["ledger_id"], body.receipt_path)
        new_date = _tx_date(body.date, old=old["date"])
        # 日期序不变式（db.assert_date_order 单点）：改退款行日期早于原消费、或改原消费
        # 晚于下游退款，都会破坏「原消费日 ≤ 退款日」（导出回环/再匹配依赖）。破坏即 400。
        try:
            dbm.assert_date_order(c, tid, new_date)
        except ValueError as e:
            raise HTTPException(400, str(e))
        old_path = old["receipt_path"]
        # 文本列与 bot/导入同上限（db.TX_TEXT_MAX 写库单点）：COALESCE 前先截，None 保持原值
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
             new_date, dbm._text_clip("name", body.name), dbm._text_clip("merchant", body.merchant),
             body.order_no,
             dbm._text_clip("note", body.note), dbm._j(body.tags), rp, rp,
             1 if body.need_confirm else 0,
             body.category_id, old["category_id"], tid))
        # 金额/日期变了就重算退款对双方的 full/partial 标签（此前停留旧值）
        dbm.refresh_pair_status(c, tid)
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

def _link_refund(c, tid: int, refund_for: int, amount: int, *, confidence: str | None) -> None:
    """退款关联统一入口（HTTP 层薄封装）：0=取消关联；>0=建链。校验与写入单源在
    db.link_refund/clear_refund（含此前缺失的下游指向检查），此处只做翻译：
    ValueError → 400、行不存在 → 404。confidence 由入口决定（手动编辑=人工清空
    NULL，队列确认=人工已核对 high）。
    建链分支的行存在性由 db.link_refund 自查（三个调用方在调此前都已确认 tid 存在）；
    取消分支的 clear_refund 是盲 UPDATE，行不存在会静默成功，须在此先 404。"""
    if refund_for == 0:
        if not c.execute("SELECT 1 FROM transactions WHERE id=?", (tid,)).fetchone():
            raise HTTPException(404, "流水不存在")
        dbm.clear_refund(c, tid, back_to_queue=False)
        return
    try:
        dbm.link_refund(c, tid, refund_for, amount, confidence)
    except ValueError as e:
        raise HTTPException(400, str(e))

class TxBatchIn(BaseModel):
    """批量修改：ids=目标流水；三字段缺省=不改。
    attribution：""=清除归属，非空=写入 tags.attribution（与原 tags 其余键合并）"""
    ids: list[int]
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    attribution: Optional[str] = None
    # 0=未提供（与 TxIn._zero_ids_none 同口径），防悬空引用落库
    _zero_ids_none = field_validator("category_id", "account_id")(_zero_id_none)

@app.post("/api/transactions/batch_update")
def batch_update_transactions(ledger: int, body: TxBatchIn):
    _ledger_access(ledger)
    if not body.ids:
        raise HTTPException(400, "未选择流水")
    updated = 0
    with dbm.connect() as c:
        # 分类/账户一旦下发即作用于全部选中行，先验归属（缺省则跳过）
        _check_tx_refs(c, ledger, body.category_id, body.account_id)
        for tid in body.ids:
            row = _tx_row_in_ledger(c, ledger, tid, "ledger_id, tags")
            if not row:
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

def _tx_row_in_ledger(c, ledger: int, tid: int, cols: str = "ledger_id"):
    """批量端点的取行+核对归属（跨账本/已删 id 返回 None 按行跳过——全选后他端删
    数据的竞态不中断整批）。batch_update/batch_delete 两处同构循环共用。"""
    if not _id64_ok(tid):
        return None   # 超界 id（直调体可达）按无此行跳过
    row = c.execute(f"SELECT {cols} FROM transactions WHERE id=?", (tid,)).fetchone()
    return row if row and row["ledger_id"] == ledger else None

@app.post("/api/transactions/batch_delete")
def batch_delete_transactions(ledger: int, body: TxBatchDelIn):
    """批量删除：跳过规则与批量修改同款（跨账本/已删 id 直接跳过，全选后他端删数据的竞态）。
    逐笔走 delete_tx——删原消费自动解链其退款行；关联小票文件一并清理。"""
    _ledger_access(ledger)
    if not body.ids:
        raise HTTPException(400, "未选择流水")
    paths = []
    with dbm.connect() as c:
        for tid in body.ids:
            if not _tx_row_in_ledger(c, ledger, tid):
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
    with dbm.connect() as c:
        old_path = dbm.delete_tx(c, tid)
    bot_core.unlink_receipt(old_path)   # 无小票路径时 no-op
    _bump_rev()
    return {"ok": True}

# ---------- 待确认队列 ----------

class ResolveIn(BaseModel):
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    refund_for: Optional[int] = None

    _zero_ids_none = field_validator("category_id", "account_id")(_zero_id_none)

@app.post("/api/pending/{tid}/resolve")
def resolve_pending(tid: int, body: ResolveIn):
    """处理待确认：改分类 / 关联退款。改分类只对当笔负责（manual_cat 保护），
    不回写学习映射——1.0.2 前这里会把用户手选泛化成该源分类的全局规则。"""
    row = _row_of("transactions", tid)
    lid = row["ledger_id"]
    tx_amount = row["amount"]
    c = dbm.connect()
    with c:
        _check_tx_refs(c, lid, body.category_id, body.account_id)
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

def _ledger_month(ledger: int, month: str) -> None:
    """月度统计端点公共前置（summary/calendar/category 与 AI 月报四端点共用）：
    账本 ACL + month 格式校验（裁判在 db.month_range 单点实现，ValueError 统一翻译 400）。"""
    _ledger_access(ledger)
    try:
        dbm.month_range(month)
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.get("/api/stats/summary")
def stats_summary(ledger: int, month: str):
    _ledger_month(ledger, month)
    return dbm.stats_summary(dbm.connect(), ledger, month)

@app.get("/api/stats/calendar")
def stats_calendar(ledger: int, month: str):
    _ledger_month(ledger, month)
    return dbm.stats_calendar(dbm.connect(), ledger, month)

@app.get("/api/stats/category")
def stats_category(ledger: int, month: str, type: str = "expense"):
    _ledger_month(ledger, month)
    return dbm.stats_category(dbm.connect(), ledger, month, type)

@app.get("/api/stats/monthly")
def stats_monthly(ledger: int, year: str):
    _ledger_access(ledger)
    # 年份格式裁判单源在 db.year_range（AI 查账同口径）：非 4 位年份生成的 "26-01-01"
    # 区间在 date 字典序比较下结果不可预期
    try:
        dfrom, dto = dbm.year_range(year)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return dbm.stats_trend(dbm.connect(), ledger, dfrom, dto)

@app.get("/api/stats/yearly")
def stats_yearly(ledger: int):
    _ledger_access(ledger)
    return dbm.stats_yearly(dbm.connect(), ledger)

def _span(_from: str, to: str) -> tuple[str, str]:
    """统计区间（from 含端、to 排他上界——前端 rangeDto 单源转次日）：任一端传空即
    不限该端，与导出/流水列表口径一致。"""
    return _from or "0000-01-01", to or "9999-12-31"

@app.get("/api/stats/trend")
def stats_trend_range(ledger: int, _from: str = Query("", alias="from"), to: str = ""):
    """任意区间的按月趋势。to 为排他上界（口径见 _span）。"""
    _ledger_access(ledger)
    return dbm.stats_trend(dbm.connect(), ledger, *_span(_from, to))

@app.get("/api/stats/range")
def stats_range(ledger: int, _from: str = Query("", alias="from"), to: str = ""):
    _ledger_access(ledger)
    return dbm.stats_summary_range(dbm.connect(), ledger, *_span(_from, to))

@app.get("/api/stats/category-range")
def stats_category_range(ledger: int, _from: str = Query("", alias="from"), to: str = "",
                         type: str = "expense"):
    _ledger_access(ledger)
    return dbm.stats_category_range(dbm.connect(), ledger, *_span(_from, to), type)

def _attribution_rows(ledger: int, _from: str, to: str) -> list[dict]:
    """归属汇总：db.attribution_rows 唯一实现（统计页 /api/stats/attribution、导出 xlsx
    「归属统计」sheet、AI 对话查账 kind=attribution 共用）。to 为排他上界。"""
    return dbm.attribution_rows(dbm.connect(), ledger, *_span(_from, to))

@app.get("/api/stats/attribution")
def stats_attribution(ledger: int, _from: str = Query("", alias="from"), to: str = ""):
    _ledger_access(ledger)
    return _attribution_rows(ledger, _from, to)

@app.get("/api/stats/renqing")
def stats_renqing(ledger: int):
    """人情往来汇总：db.stats_renqing 唯一实现（统计页与 AI 查账 kind=renqing 共用）。
    given=我给出（支出），received=我收到（收入），balance=given-received>0 即对方尚未还礼。"""
    _ledger_access(ledger)
    return dbm.stats_renqing(dbm.connect(), ledger)

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

def _parse_counts(previews: list) -> dict:
    """导入预览四类互斥计数（total = new + duplicate + cross_dupe + skip）：duplicate 行
    也过 _link_payment（重导增强回填），可能同时带 pay_dupe——不剔除会让 skip 的差值
    双重扣减，重导账单时「无效」一栏出现负数（前端 impSummaryHtml 各项加总对不上 total）。"""
    dup = sum(1 for p in previews if p.get("duplicate"))
    xdup = sum(1 for p in previews if p.get("pay_dupe") and not p.get("duplicate"))
    new = sum(1 for p in previews if ib._is_new_row(p))
    return {"new": new, "duplicate": dup, "cross_dupe": xdup,
            "skip": len(previews) - new - dup - xdup}


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
    # build_previews 逐行查/写库，同样挪线程；dbm.connect() 必须在 worker 内取（线程本地连接
    # 禁止跨线程传）。持 _PREVIEW_LOCK 串行：预览期 _ensure_account 是 check-then-insert，
    # 并发导入同一账本会建出同名账户
    def _previews():
        with _PREVIEW_LOCK:
            return ib.build_previews(dbm.connect(), ledger, flows)
    previews = await asyncio.to_thread(_previews)
    _bump_rev()   # 预览期 _ensure_account/ClassifyCtx.ensure 可能已落库（新账户/分类）：推刷新让共享端下拉即时可见
    # 柴米账单（本应用导出模板命中）按原分类直入：分类/名称都是用户定过的结果，不做 AI 兜底
    # （分类瀑布同样绕开，见 import_classify.classify_export）
    chaimi = any(f.get("platform") == "export" for f in flows)
    ai_status = None
    if use_ai and not chaimi:
        # 同步 LLM 兜底挪线程池（async 里直接调卡死事件循环）；保序串行不 gather——
        # 分类兜底读 name 与名称兜底写 name 并行会交错，且各自内部已并发。两段共用一个
        # 截止时刻（ai.import_deadline），整体压在网关掐断线内；状态回给导入页说明 AI 做了什么
        deadline = ai.import_deadline()
        ai_status = await asyncio.to_thread(ai.resolve_subcategories_batch, previews, ledger, deadline)
        await asyncio.to_thread(ai.resolve_names_batch, previews, deadline)
    n_counts = _parse_counts(previews)
    return {"filename": file.filename, "total": len(previews), **n_counts,
            "need_confirm": sum(1 for p in previews if p.get("need_confirm") and not p.get("skip")),
            "chaimi": chaimi, "ai": ai_status, "previews": previews}

class ConfirmIn(BaseModel):
    previews: list[dict]
    filename: str
    source: str

@app.post("/api/import/confirm")
def import_confirm(ledger: int, body: ConfirmIn):
    """确认导入：单事务见 ib.apply_import（入库 + 归属名单播种原子提交）。
    账本存在性由 _ledger_access 统一把关（系统上下文同样 404，不再此处单独补查）。"""
    _ledger_access(ledger)
    batch_id = ib.apply_import(dbm.connect(), ledger, body.previews, body.filename, body.source)
    n = dbm.connect().execute("SELECT count FROM import_batches WHERE id=?", (batch_id,)).fetchone()[0]
    _bump_rev()
    return {"batch_id": batch_id, "imported": n}

@app.get("/api/import/batches")
def list_batches(ledger: int):
    _ledger_access(ledger)
    return [dict(r) for r in dbm.connect().execute(
        "SELECT * FROM import_batches WHERE ledger_id=? ORDER BY id DESC", (ledger,))]

@app.delete("/api/import/batches/{batch_id}")
def undo_import(batch_id: int):
    _row_ledger("import_batches", batch_id)
    n = ib.undo_batch(dbm.connect(), batch_id)
    _bump_rev()
    return {"deleted": n}

# ---------- 数据修复（原导入自愈的确认版，1.0.1） ----------
# 旧版在预览阶段静默改写已有数据（用户手动整理的分类会在下一次预览被翻回词典结果），
# 现改为只读预览 + 显式应用。

@app.get("/api/repair/preview")
def repair_preview(ledger: int):
    _ledger_access(ledger)
    return ic.repair_preview(dbm.connect(), ledger)

@app.post("/api/repair/apply")
def repair_apply(ledger: int):
    _ledger_access(ledger)
    r = ic.repair_apply(dbm.connect(), ledger)
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

def _tpl_str_list(body: dict, key: str) -> list | None:
    """模板可选字符串数组字段：缺省 None；非列表 400；其余逐项字符串化。
    类型先验后用（迭代非列表会裸 500），transfer_types/name_strip 等共用。"""
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, list):
        raise HTTPException(400, f"{key} 格式不合法")
    return [str(x) for x in v]

def _tpl_refund_rules(body: dict) -> list | None:
    """refund_rules 字段校验+规范化（save_import_template 拆出，三个可选结构字段同批）。"""
    rules = body.get("refund_rules")
    if rules is None:
        return None
    if (not isinstance(rules, list)
            or not all(isinstance(x, dict) and x.get("col") and x.get("op")
                       and isinstance(x.get("values"), list) for x in rules)):
        raise HTTPException(400, "refund_rules 格式不合法")
    return [{"col": str(x["col"]), "op": str(x["op"]),
             "values": [str(v) for v in x["values"]],
             **({"contains_none": [str(v) for v in x["contains_none"]]}
                if x.get("contains_none") else {}),
             **({"guard": {"flow_not": str(x["guard"]["flow_not"])}}
                if isinstance(x.get("guard"), dict) and x["guard"].get("flow_not") else {})}
            for x in rules]

def _tpl_name_fallback(body: dict) -> dict | None:
    """name_fallback 字段校验+规范化。"""
    nf = body.get("name_fallback")
    if nf is None:
        return None
    if (not isinstance(nf, dict) or not isinstance(nf.get("cols"), list)):
        raise HTTPException(400, "name_fallback 格式不合法")
    return {"cols": [str(c) for c in nf["cols"]],
            **({"generic_set": [str(s) for s in nf["generic_set"]]}
               if isinstance(nf.get("generic_set"), list) else {})}

def _tpl_row_filter(body: dict) -> dict | None:
    """row_filter 字段校验+规范化。"""
    rf = body.get("row_filter")
    if rf is None:
        return None
    if not isinstance(rf, dict) or not rf.get("col") or not isinstance(rf.get("skip_in"), list):
        raise HTTPException(400, "row_filter 格式不合法")
    return {"col": str(rf["col"]), "skip_in": [str(s) for s in rf["skip_in"]]}

@app.get("/api/import/templates")
def list_import_templates():
    return config.load().get("import_templates") or []

@app.post("/api/import/templates")
def save_import_template(body: dict):
    """新建/更新模板（按 name 覆盖）。最小校验 + 字段白名单（import_bill.user_templates 消费）。
    白名单覆盖引擎全 schema：复制内置模板改列名/状态词存为自定义即接管路由（覆盖机制）。"""
    _admin_or_403()   # 写应用级配置（chaimi.conf），与 put_config 同门
    name = str(body.get("name") or "").strip()
    headers, col = body.get("headers"), body.get("col")
    # 类型先验后用（col/flow_map/note_strip 等直接 .items()/迭代，传字符串/数字会裸 500）
    if (not name or not isinstance(headers, list) or not headers
            or not isinstance(col, dict) or not col):
        raise HTTPException(400, "模板需要名称、表头列与列映射")
    col = {k: v for k, v in col.items() if k in _TPL_FIELDS and v}
    # 列名值类型闸：值须为 str 或 str 数组（别名链）。此前 dict/int 等垃圾值能"保存成功"
    # 落进 conf，首次解析才在引擎里 TypeError——模板永不工作且无保存时反馈
    if not all(isinstance(v, str) or (isinstance(v, list) and v and all(isinstance(x, str) for x in v))
               for v in col.values()):
        raise HTTPException(400, "col 映射的值必须是列名或列名数组")
    if "date" not in col or "amount" not in col:
        raise HTTPException(400, "交易时间与金额两列必须映射")
    rules = _tpl_refund_rules(body)
    ttypes = _tpl_str_list(body, "transfer_types")
    name_strip = _tpl_str_list(body, "name_strip")
    nf = _tpl_name_fallback(body)
    rf = _tpl_row_filter(body)
    owner_re = str(body.get("owner_re") or "")
    if owner_re:
        try:
            re.compile(owner_re)
        except re.error:
            raise HTTPException(400, "owner_re 不是合法正则")
    try:
        skip_n = max(0, int(body.get("skip_first_n") or 0))
        # 负数此前放行 → 引擎 data_rows[skip_n:] 反向截尾丢末尾数据行（无提示）
    except (TypeError, ValueError):
        raise HTTPException(400, "skip_first_n 必须是数字")
    flow_map = body.get("flow_map")
    if flow_map is not None and not isinstance(flow_map, dict):
        raise HTTPException(400, "flow_map 必须是对象")
    # config.edit()：读-改-写持锁——与扫码落凭证/设置页保存/模板删除并发互斥
    # （裸 load→save 的窗口里，扫码回调落盘的凭证会被本端旧快照整体覆盖丢失）
    with config.edit() as cfg:
        tpls = [t for t in (cfg.get("import_templates") or []) if t.get("name") != name]
        t = {"id": "custom", "name": name, "headers": [str(h) for h in headers], "col": col,
             "flow_map": {str(k): v for k, v in (flow_map or {}).items()
                          if v in ("expense", "income", "transfer")},
             "refund_type": str(body.get("refund_type") or "").strip(),
             "name_strip": [s for s in (name_strip or []) if s],
             "owner": str(body.get("owner") or "").strip(),
             "skip_first_n": skip_n}
        # 可选键：进 body 才落（复制内置模板的默认值不被空值覆盖）
        for key, val in (("refund_rules", rules), ("transfer_types", ttypes),
                         ("name_fallback", nf), ("row_filter", rf)):
            if val is not None:
                t[key] = val
        if isinstance(body.get("hash_fields"), list):
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
    return {"ok": True}

@app.delete("/api/import/templates/{name}")
def delete_import_template(name: str):
    _admin_or_403()   # 写应用级配置（chaimi.conf），与 save_import_template 同门
    with config.edit() as cfg:   # 读-改-写持锁（与设置页保存/扫码落凭证并发互斥）
        cfg["import_templates"] = [t for t in (cfg.get("import_templates") or []) if t.get("name") != name]
    return {"ok": True}

def _csv_download_headers(ledger_id: int, ext: str, name: str | None) -> dict:
    """导出文件名：chaimi-ledger-{账本名}-{导出日期}.{ext}；中文走 RFC 5987 filename*，
    同时给 ASCII 回退（老浏览器丢弃中文名也不至于拿到空文件名）。
    name 由调用方传入（/api/export 一次查 ledgers 行三处共用，避免同请求 3 连查）。"""
    stem = f"chaimi-ledger-{name if name else ledger_id}-{time.strftime('%Y%m%d')}"
    quoted = quote(f"{stem}.{ext}")
    return {"Content-Disposition": f"attachment; filename=ledger-{time.strftime('%Y%m%d')}.{ext}; filename*=UTF-8''{quoted}"}

def _neut(v) -> str:
    """公式注入中和（OWASP 惯例）：账单/用户可控文本以 = + - @ 或 TAB/CR 开头时前缀单引号，
    防"导入恶意账单 → 导出 CSV/xlsx → Excel/WPS 打开"时单元格被当公式执行；
    代价是此类开头的正常文本（如 -5折）导出带前导 '，Excel 显示时还原为原文。"""
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s

def _export_table(rows, cur_code: str) -> list:
    """导出表（CSV/xlsx 共用）：13 列格式——流水卡片上可见的数据 + 导出回环管道列。
    金额保留分→元的换算由调用方格式化；交易名称=name（回落备注/分类，与卡片标题同口径）；
    流水归属=解析后名字（有组显组名）；账户=支付账户（收款账户为旧代列不再写出，
    导入侧模板仍认——已下载的旧导出文件重导照常还原转账两端）；
    流水ID/退款关联——导出回环：导入时按 ID 直连退款对，无需重新匹配。
    用户/账单可控的文本列一律 _neut 中和；日期/金额/ID 等本源列不涉及。
    cur_code 由调用方传入（/api/export 单查 ledgers 行共用）。"""
    type_map = {"expense": "支出", "income": "收入", "transfer": "不计收支"}
    C = pt.EXPORT_COLUMNS   # 列名与 export 模板单源（platform_templates），改列名只动一处
    return [[C["date"], C["name"], C["amount"], "币种", C["flow"], C["type"],
             C["pay"], C["attribution"], C["note"], C["order"],
             C["origin_source"], C["export_id"], C["export_ref"]]] + [
        [r["date"], _neut(r["name"] or r["note"] or r["category"] or ""), r["amount"] / 100, cur_code,
         type_map.get(r["type"], r["type"]), _neut(r["category"] or ""), _neut(r["account"] or ""),
         _neut(r["attr_display"] or ""), _neut(r["note"] or ""), _neut(r["order_no"] or ""), _neut(r["source"] or ""),
         r["id"], r["refund_for"] or ""] for r in rows]

@app.get("/api/receipts/export")
def export_receipts(ledger: int, _from: str = Query("", alias="from"), to: str = ""):
    """小票打包下载：按所选时间范围把流水带的小票打成 zip。
    文件名 = 日期_交易名.jpeg（重名加交易 id 前缀）；范围/账本内无小票返回 404 提示。"""
    _ledger_access(ledger)
    sql = ("SELECT t.id, t.date, t.name, t.receipt_path FROM transactions t "
           "WHERE t.ledger_id=? AND t.receipt_path IS NOT NULL AND t.receipt_path!=''")
    frag, params = _date_where(_from, to)
    sql += frag
    args: list = [ledger, *params]
    rows = dbm.connect().execute(sql + " ORDER BY t.date", args).fetchall()
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
            if name in used:   # 同日同名：加交易 id（全表唯一）防覆盖
                name = f"{(r['date'] or '')[:10]}_{r['id']}_{safe}{f.suffix}"
            used.add(name)
            z.write(f, name)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition":
                                      f'attachment; filename="receipts-{ledger}.zip"'})

def _export_meta_rows(rows, ledger: int, lg_name: str | None, lg_cur: str,
                      _from: str, to: str) -> list[list]:
    """导出文件头部说明（csv/xlsx 共用）：每条说明一行单格 + 空行
    （csv.writer/ws.append 对字符串会逐字符迭代，必须包成列表）。
    to 为排他上界（前端 expRangeParams 单源转次日）：范围说明给用户看的仍是
    含端末日，显示侧经 db.excl_to_incl 单点减回（空/非法原样保留）。"""
    if _from or to:
        range_txt = " 至 ".join(x for x in (_from, dbm.excl_to_incl(to, default="")) if x)
    else:
        range_txt = "全部"
    meta = [f"# 账本：{lg_name if lg_name else ledger}",
            f"# 币种：{lg_cur}",
            f"# 导出范围：{range_txt}",
            f"# 导出时间：{time.strftime('%Y-%m-%d %H:%M')}",
            f"# 流水笔数：{len(rows)}"]
    return [[m] for m in meta] + [[]]

def _export_csv_resp(table: list, meta_rows: list[list], ledger: int,
                     lg_name: str | None) -> StreamingResponse:
    """CSV 导出（金额文本格式自带两位小数；BOM 前缀防 Excel 乱码）。"""
    for row in table[1:]:
        row[2] = f"{row[2]:.2f}"
    buf = io.StringIO()
    csv.writer(buf).writerows(meta_rows + table)
    return StreamingResponse(iter([("\ufeff" + buf.getvalue()).encode("utf-8")]),
                             media_type="text/csv",
                             headers=_csv_download_headers(ledger, "csv", lg_name))

def _export_xlsx_resp(ledger: int, meta_rows: list[list], table: list,
                      _from: str, to: str, lg_name: str | None) -> StreamingResponse:
    """xlsx 导出：流水 sheet + 归属统计 sheet（上界与流水同一排他 to 原样下传）。"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "流水"
    for row in meta_rows + table:
        ws.append(row)
    ws.column_dimensions["A"].width = 12   # 日期列宽，避免 2026-09-01 显示成 ####
    ws2 = wb.create_sheet("归属统计")
    ws2.append(["流水归属", "笔数", "支出", "收入", "包含归属"])
    for r in _attribution_rows(ledger, _from, to):
        ws2.append([_neut(r["person"]), r["cnt"], r["expense"] / 100, r["income"] / 100,
                    _neut("、".join(m["person"] for m in r.get("members", [])))])
    buf = io.BytesIO()
    wb.save(buf)
    return StreamingResponse(iter([buf.getvalue()]),
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers=_csv_download_headers(ledger, "xlsx", name=lg_name))

@app.get("/api/export")
def export(ledger: int, _from: str = Query("", alias="from"), to: str = "", format: str = "json"):
    """流水导出（json=结构化回环导入用 / csv·xlsx=人读）。to 为排他上界。"""
    _ledger_access(ledger)
    sql = ("SELECT t.id, t.refund_for, t.date, t.type, t.amount, t.name, c.name AS category, "
           "a.name AS account, t.note, "
           "t.order_no, t.source, t.tags, " + _ATTR_DISPLAY + " "
           "FROM transactions t "
           "LEFT JOIN categories c ON t.category_id=c.id "
           "LEFT JOIN accounts a ON t.account_id=a.id "
           + _ATTR_RESOLVE + "WHERE t.ledger_id=?")
    frag, params = _date_where(_from, to)
    sql += frag
    args: list = [ledger, *params]
    rows = dbm.connect().execute(sql + " ORDER BY t.date", args).fetchall()
    if format in ("csv", "xlsx"):
        # ledgers 行一次取全（此前文件名/币种/meta 三处各查一遍）
        lg_row = dbm.connect().execute("SELECT name, currency FROM ledgers WHERE id=?", (ledger,)).fetchone()
        lg_name = lg_row["name"] if lg_row else None
        lg_cur = (lg_row["currency"] if lg_row else None) or "CNY"
        table = _export_table(rows, lg_cur)
        meta_rows = _export_meta_rows(rows, ledger, lg_name, lg_cur, _from, to)
        if format == "csv":
            return _export_csv_resp(table, meta_rows, ledger, lg_name)
        return _export_xlsx_resp(ledger, meta_rows, table, _from, to, lg_name)
    items = []
    for r in rows:
        # tx_to_dict：tags/ai_meta 解析为对象，与 /api/transactions 同构（此前是原始 JSON 字符串，
        # 外部脚本消费需二次 parse，两个接口结构不一致）
        d = dbm.tx_to_dict(r)
        d["amount"] = d["amount"] / 100
        items.append(d)
    return JSONResponse(items)

# ---------- 映射配置 ----------

class MappingIn(BaseModel):
    flow_type: str
    source: str
    category_id: int

    @field_validator("flow_type")
    @classmethod
    def _flow_domain(cls, v):
        """值域单源：映射只按 expense/income 分流（不计收支组经 catmgr 按支出侧落行）。
        垃圾 flow_type 此前落库后永不命中（import_classify 按 flow_type 过滤），
        成为映射页可见、解析端无视的死行。"""
        if v not in ("expense", "income"):
            raise ValueError("flow_type 必须是 expense 或 income")
        return v

@app.get("/api/mappings")
def list_mappings(ledger: int):
    _ledger_access(ledger)
    return [dict(r) for r in dbm.connect().execute(
        "SELECT m.id, m.flow_type, m.source, m.category_id, m.learned, c.name AS target "
        "FROM category_mappings m JOIN categories c ON m.category_id=c.id "
        "WHERE m.ledger_id=? ORDER BY m.source", (ledger,))]

@app.post("/api/mappings")
def create_mapping(ledger: int, body: MappingIn):
    """v6：目标必须是已存在的分类（category_id 外键），悬空映射从结构上不可能。"""
    _ledger_access(ledger)
    if not dbm.top_cat_by_id(dbm.connect(), ledger, body.category_id):
        raise HTTPException(400, "目标分类不存在或不属于当前账本")
    with dbm.connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO category_mappings(ledger_id, flow_type, source, category_id) VALUES(?,?,?,?)",
            (ledger, body.flow_type, body.source.strip(), body.category_id))
    _bump_rev()
    return {"ok": True}

@app.delete("/api/mappings/{mid}")
def delete_mapping(mid: int):
    _row_ledger("category_mappings", mid)
    with dbm.connect() as c:
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
    with dbm.connect() as c:
        c.execute("UPDATE category_mappings SET source=? WHERE id=?", (source[:60], mid))
    _bump_rev()
    return {"ok": True}

# ---------- 备份 ----------

@app.post("/api/backup")
def do_backup():
    _admin_or_403()   # 整库备份含所有用户的账本（MULTIUSER-PLAN §3.3）
    # 并发长写事务撞锁映射 409 可重试（与 _backup_call 同款映射；本端点无文件名参数、
    # 要透传 create_backup 返回的 dict，故不经 _backup_call 的参数闸）
    try:
        return backup.create_backup()
    except sqlite3.OperationalError as e:
        raise HTTPException(409, f"备份被并发写库打断，请重试：{e}")

@app.get("/api/backups")
def get_backups():
    _admin_or_403()
    return backup.list_backups()

def _backup_call(op, name) -> None:
    """备份操作（restore/delete）公共入口：参数先验 + 异常映射。
    非字符串 file（null/数字）旧版直达 backup._valid_name 的 startswith → AttributeError 500；
    OperationalError=恢复期间在途写库撞锁（restore 前已 stop_all，此处是残余兜底）→ 409 可重试。"""
    if not isinstance(name, str):
        raise HTTPException(400, "file 必须是备份文件名")
    try:
        op(name)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    except sqlite3.OperationalError as e:
        raise HTTPException(409, f"备份操作被并发写库打断，请重试：{e}")

@app.post("/api/restore")
def do_restore(body: dict):
    _admin_or_403()   # 恢复整库影响所有用户
    # 恢复耗时数秒且要替换库文件：先停全部 iLink 轮询（期间 bot 确认入库会撞
    # database is locked，且旧连接指向被替换的旧文件），恢复后 _bot_apply 按回滚的 conf 重建；
    # 自动备份线程同暂停（恢复中的库无锁保护，恰逢整点会产出半新半旧的组合备份）
    _ilink.stop_all()
    _auto_backup.paused = True
    try:
        _backup_call(backup.restore_backup, body.get("file", ""))
        dbm.init_db()
    finally:
        _auto_backup.paused = False
        # 成败都要重建（finally 单点）：stop_all 已把轮询全停，restore 失败（400/409）
        # 时若跳过 _bot_apply，微信机器人会静默停摆到下次配置变更/重启；
        # chaimi.conf 随备份回滚：直连轮询线程必须按新模式差量对齐，否则旧监听残留
        _bot_apply()
    # 恢复旧备份后无主账本认领自动重开：认领完成与否每次问库（_admin_claim_done），
    # 无进程状态需要复位
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
    lp = config.log_path()
    sources.append(("app/chaimi-ledger.log", lp))   # applog 轮转文件（本地开发也有）
    # 轮转出的历史段一并入包：只带当前段会正好漏掉轮转点之前出事的时段
    for i in (1, 2):
        rot = lp.with_name(f"{lp.name}.{i}")
        sources.append((f"app/{rot.name}", rot))
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
    """设置页「模型列表」：服务商 /models 按 文本/视觉 分组（可失败，error 带说明）。
    admin 门：出网探测消费服务端存的 Key，属应用级配置操作（与 PUT /api/config 同门）。"""
    _admin_or_403()
    return ai.list_models_grouped()

def _mask(d: dict, *keys: str) -> None:
    """凭证字段就地掩码（明文只存 chaimi.conf 与备份副本，出接口一律 ***）。"""
    for k in keys:
        if d.get(k):
            d[k] = "***"

@app.get("/api/config")
def get_config():
    """配置下发（设置页三连读共用）。敏感字段一律掩码——此前 ilink 的 user（微信 ID）/
    account_id 对任意登录用户明文可见，与「应用级配置管理员统一管理」口径不符
    （端点本身保持开放：非管理员设置页同样要读 llm/backup 表单回显，写入侧已 admin 门）。"""
    cfg = config.load()
    _mask(cfg["llm"], "api_key", "vl_api_key")
    # A 通道凭证掩码（config 读侧已把 bot.ilink 归一为列表）
    for e in (cfg.get("bot") or {}).get("ilink") or []:
        if isinstance(e, dict):
            _mask(e, "token", "user", "account_id")
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
    # config.edit()：读-改-写全程持锁——与扫码落凭证/解绑等并发写方互斥，防后写者旧快照覆盖
    with config.edit() as cfg:
        llm_in = body.get("llm")
        # 类型先验后用：list/str 的 .items() 会 AttributeError → 500（save_import_template 同款教训）
        if llm_in is not None and not isinstance(llm_in, dict):
            raise HTTPException(400, "llm 必须是对象")
        for k, v in (llm_in or {}).items():
            if k not in _LLM_KEYS:   # 白名单外的键不落盘（防任意键注入配置文件）
                continue
            if k in ("api_key", "vl_api_key") and v == "***":
                continue
            cfg["llm"][k] = v
        if "theme" in body:
            # 任意 JSON 透传曾可落 dict/None：前端主题逻辑拿到非字符串静默失效
            if body["theme"] not in ("light", "dark"):
                raise HTTPException(400, "theme 只支持 light / dark")
            cfg["theme"] = body["theme"]
        if isinstance(body.get("backup"), dict):
            b, old = body["backup"], cfg["backup"]
            old["enabled"] = bool(b.get("enabled", old["enabled"]))
            old["hour"] = _cfg_int(b, old, "hour", 0, 23)
            old["keep_months"] = _cfg_int(b, old, "keep_months", 0, 480)
            old["keep_years"] = _cfg_int(b, old, "keep_years", 0, 100)
        if isinstance(body.get("bot"), dict):
            b = body["bot"]
            old = cfg.setdefault("bot", {})
            if b.get("mode") in ("off", "direct"):   # 网关模式(B通道)已下线
                old["mode"] = b["mode"]
            if b.get("confirm_mode") in ("reply", "auto"):   # 记账确认模式（读侧坏值也归一 reply）
                old["confirm_mode"] = b["confirm_mode"]
            if isinstance(b.get("allowed_users"), list):
                old["allowed_users"] = [str(x).strip() for x in b["allowed_users"] if str(x).strip()]
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
    with _receipt_owners_lock:   # 在途归属（discard 校验用，见 _receipt_owners 注释）；跨线程统一加锁
        _receipt_owners[fname] = _uid()
        if len(_receipt_owners) > 4096:   # 上限防漂移：正常同时至多几张
            _receipt_owners.popitem(last=False)   # 淘汰最旧一条；clear() 会误伤他人分钟级在途窗口内的归属
    await asyncio.to_thread(bot_core.process_receipt, p)   # PIL 压缩 CPU 密集，挪线程池（失败保留原图）
    return p, f"receipts/{fname}"

# 在途小票归属：文件名 → 上传者 uid（_save_receipt 记录、discard_receipt 校验）。
# 此前 discard 无任何身份校验——文件名可枚举（时间戳+16bit hex），任意登录用户可删
# 他人的在途小票。归属只存进程内：在途窗口分钟级、重启后归属未知的文件仅管理员可
# 丢弃（残留由 24h sweep 兜底），无需持久化。OrderedDict：超上限时 popitem(last=False)
# 精确淘汰最旧一条（dict.clear() 会把所有用户在途窗口内的归属一并抹掉）。
# 写入在事件循环线程、读取/删除在线程池线程：跨线程无锁并发在极端时序下会 RuntimeError
# （popitem 与 pop 竞争）或归属误判，统一经 _receipt_owners_lock 串行化。
_receipt_owners: OrderedDict[str, int | None] = OrderedDict()
_receipt_owners_lock = threading.Lock()

def _receipt_owner_pop(name: str) -> None:
    with _receipt_owners_lock:
        _receipt_owners.pop(name, None)

@app.post("/api/ai/receipt")
async def ai_receipt(ledger: int, file: UploadFile = File(...)):
    _ledger_access(ledger)
    path, rp = await _save_receipt(file)
    result = await asyncio.to_thread(ai.parse_receipt, str(path), ledger)   # 视觉 OCR 秒级网络调用，挪线程池
    if result.get("error"):
        path.unlink(missing_ok=True)   # 识别失败：前端只显错误不保留图片，删文件防孤儿
        _receipt_owner_pop(path.name)   # 在途归属随文件一并清（防滞留到 4096 兜底清空）
        return result
    result["receipt_path"] = rp
    return result

@app.delete("/api/receipts/discard")
def discard_receipt(path: str):
    """App 取消记账/删空卡片/关闭弹窗时清未入库小票（前端 fire-and-forget 调用）。
    只删无流水引用的文件——已入库或别笔在用的一律不动，误调无副作用。
    归属校验：普通用户只能丢自己上传的在途图（_receipt_owners），归属未知
    （机器人草稿/重启前的旧文件）与管理员例外——防枚举文件名删他人在途小票。"""
    name = Path(path or "").name
    if not name or ".." in path:
        raise HTTPException(400, "无效路径")
    if not _receipt_owner_ok(name):
        raise HTTPException(403, "只能丢弃自己上传的小票")
    if _receipt_ref_lids(name):
        return {"ok": True, "deleted": False}
    p = config.receipts_dir() / name
    if not p.is_file():
        return {"ok": True, "deleted": False}
    try:
        p.unlink()
    except OSError:
        return {"ok": True, "deleted": False}
    _receipt_owner_pop(name)
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
    used = {Path(r["receipt_path"]).name for r in dbm.connect().execute(
        "SELECT receipt_path FROM transactions WHERE receipt_path IS NOT NULL AND receipt_path!=''")}
    cutoff = time.time() - grace_hours * 3600
    n = 0
    for p in rdir.iterdir():
        try:
            if (p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")
                    and p.name not in used and p.stat().st_mtime < cutoff):
                p.unlink()
                n += 1
        except OSError:
            # iterdir 与 stat/unlink 之间文件被并发删除（discard/另一线程清理）：
            # 只跳过该文件，不让 FileNotFoundError 中断整轮（中断则本轮剩余孤儿再等 24h）
            continue
    if n:
        log.info(f"兜底清理 {n} 个无引用小票文件")
    return n

def _sse(gen) -> StreamingResponse:
    """AI 事件流统一响应（X-Accel-Buffering 关网关缓冲，前端逐块收到）。"""
    return StreamingResponse(ai.sse_stream(gen), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

@app.get("/api/ai/analyze")
def ai_analyze(ledger: int, month: str):
    _ledger_month(ledger, month)   # 坏月份若漏进生成器，SSE 会在流中途炸连接（比 500 更难排查）
    return _sse(ai.monthly_analysis(ledger, month))

@app.get("/api/ai/chat")
def ai_chat(ledger: int, q: str):
    _ledger_access(ledger)
    return _sse(ai.chat(q, ledger))

# ---------- 静态资源（小票图） ----------

def _receipt_ref_lids(name: str) -> set:
    """引用该小票文件的流水所属账本集（discard/crop/_receipt_ledger_gate 共用一查询）：
    库内路径已统一 receipts/名 单一形态（写入口只认该形态，历史行由 db 迁移归一），
    走 idx_tx_receipt 索引精确查。"""
    return {r["ledger_id"] for r in dbm.connect().execute(
        "SELECT ledger_id FROM transactions WHERE receipt_path=?", (f"receipts/{name}",))}

def _receipt_owner_ok(name: str) -> bool:
    """在途小票归属判定（discard/crop 共用）：管理员/未登录（多用户关闭）放行；
    普通用户仅自己上传的（_receipt_owners）放行——归属未知（机器人草稿/重启后
    进程内表丢失的旧文件）不放行，仅管理员可操作。"""
    uid = _uid()
    if uid is None or _isadmin():
        return True
    with _receipt_owners_lock:
        return _receipt_owners.get(name) == uid

def _receipt_ledger_gate(name: str, lids: set | None = None) -> None:
    """小票文件级 ACL（get_receipt/crop_receipt 共用）：被流水引用即按所属账本
    收口（与 _ledger_access 同口径）。lids 可传入 _receipt_ref_lids 的结果
    （crop 已查过一遍时不重复查）。索引精确查未命中再回落 basename 全扫，
    兜底迁移后仍可能出现的异常数据（启动后手改库写的绝对路径等，
    test_multiuser 有防御用例锁定该口径）。
    未被引用的在途图不拦（App 先预览后确认、机器人草稿期）：随机文件名 +
    24h 兜底清理已足够。"""
    uid = _uid()
    if uid is None:
        return
    lids = _receipt_ref_lids(name) if lids is None else lids
    if not lids:
        lids = {r["ledger_id"] for r in dbm.connect().execute(
            "SELECT ledger_id, receipt_path FROM transactions WHERE receipt_path!=''")
            if Path(r["receipt_path"]).name == name}
    if lids and not lids & _my_ledger_ids(uid):
        raise HTTPException(403, "无权访问该小票")

@app.get("/receipts/{name}")
def get_receipt(name: str):
    _safe_name(name)
    _receipt_ledger_gate(name)
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
    body = body or {}
    def _coord(key: str) -> int:
        try:
            return int(body.get(key) or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, f"裁剪参数 {key} 必须是数字")
    x, y = max(0, _coord("x")), max(0, _coord("y"))
    w, h = _coord("w"), _coord("h")
    if w < 10 or h < 10:
        raise HTTPException(400, "裁剪区域太小")
    # 在途未引用小票走归属门（与 discard 同口径）：_receipt_ledger_gate 只按流水归属
    # 收口，未入库的在途图（文件名时间戳+hex 可枚举）此前任意登录用户可裁剪/破坏
    lids = _receipt_ref_lids(name)
    if not lids and not _receipt_owner_ok(name):
        raise HTTPException(403, "只能裁剪自己上传的小票")
    _receipt_ledger_gate(name, lids)
    p = config.receipts_dir() / name
    if not p.is_file():
        raise HTTPException(404, "小票不存在")
    try:
        from PIL import Image
        # with + load()：先读全像素再写同路径（读句柄与写不交织），退出即关句柄——
        # 此前不关闭，Windows 下滞留句柄会让紧随的 unlink（discard/sweep）PermissionError
        with Image.open(p) as img:
            img.load()
            w, h = min(w, img.size[0] - x), min(h, img.size[1] - y)
            if w < 10 or h < 10:
                raise HTTPException(400, "裁剪区域超出图片范围")
            img.crop((x, y, x + w, y + h)).save(p, "JPEG", quality=85)
        return {"ok": True, "size": [w, h]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"裁剪失败: {e}")

def _ct_eq(a: str, b: str) -> bool:
    """常量时间字符串比较（Basic Auth 用）：按 UTF-8 字节比较——str 版 compare_digest
    对非 ASCII 直接抛 TypeError（中文凭证即使正确也恒 False），字节版无此限制。"""
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

@app.get("/api/rev/wait")
def rev_wait(since: int = -1, lsince: int = -1, hold: int = 25):
    """长轮询(方案B,替代被飞牛网关掐杀的 SSE 无限流):挂住直到 rev(数据代数)或
    lrev(账本列表代数,多用户共享/移除实时生效)偏离 since/lsince,或 hold 秒超时再返回;
    双缺省(首轮立基线/轻量探测)立即返回当前值。每请求时长有限(<60s 网关掐杀线,
    hold 收敛 1..30s),缓冲网关下照常送达——与 AI 聊天流可用同理。前端循环调用,变更后
    1s 内刷新。lsince 缺省 -1=不参与判定(旧客户端兼容:仍只看 rev)。
    hold 契约：core.js 固定发 hold=20，超时返回本身即心跳；客户端半开检测=hold+10s。"""
    def _changed() -> bool:
        if since >= 0 and _REV["n"] != since:
            return True
        if lsince >= 0 and _LREV["n"] != lsince:
            return True
        return since < 0 and lsince < 0
    if not _REV_WAIT_SEATS.acquire(blocking=False):
        # 满座：立即返回当前代数（客户端轮询循环 300ms 后自来），不让线程池被挂起请求占满
        return {"rev": _REV["n"], "lrev": _LREV["n"]}
    try:
        with _REV_COND:
            _REV_COND.wait_for(_changed, timeout=max(1, min(hold, 30)))
            return {"rev": _REV["n"], "lrev": _LREV["n"]}
    finally:
        _REV_WAIT_SEATS.release()

# ---------- 通道 A：iLink 直连（DESIGN 10.3） ----------

def _ilink_on_confirmed(bot, creds: dict) -> None:
    """扫码成功(十章):凭证落 chaimi.conf,扫码人自动进白名单(身份即授权);
    实例转正交 manager.adopt。平台规则:一个微信号对同一 bot 应用只有一条
    绑定,扫新码会顶替旧绑定——同 user 旧凭证一律删除,apply 差量停旧实例
    (防僵尸轮询)。"""
    ledger = bot._ledger
    label = bot._label or ""
    user = creds.get("ilink_user_id") or ""
    if not dbm.ledger_exists(dbm.connect(), ledger):
        # 扫码悬空期间账本被删（_purge_bot_entries 漏网的窗口）：凭证不落盘；实例虽已由
        # _login_confirmed 拉起（回调在其后），在此停掉轮询——注册表里的残项由下一次
        # _bot_apply 差量清掉，不为已删账本留僵尸线程
        bot.stop()
        return
    # config.edit()：读-改-写持锁——扫码落凭证与设置页保存并发时,后写者旧快照会覆盖刚落的凭证
    with config.edit() as cfg:
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
    # 收编无条件：扫码确认瞬间模式被切走的竞态下，waiter 的 _stop 检查与 start() 之间
    # 有毫秒级窗口，实例可能已自启——不收编即成「状态页看不见、stop_all 停不掉」的
    # 僵尸轮询（mode=off 仍收消息写库）。收编后 _bot_apply 按当前模式对账（重读 conf，
    # entries 即刚落盘的那份）：direct 差量拉起，其余 stop_all 收编即停；凭证已落盘，
    # 切回 direct 免重扫（原出口闸语义保留）。
    _ilink.adopt(bot)
    _bot_apply()

@app.post("/api/bot/ilink/qrcode")
def bot_ilink_qrcode(body: dict):
    """按账本发起扫码(DESIGN 10.6):{ledger, label}——扫哪个账本的码记哪本。
    权限=目标账本 ACL（A 通道按账本收口，成员可给自己的账本发起绑定）；
    模式总开关/白名单仍是应用级配置（admin，MULTIUSER-PLAN §3.3）。"""
    # ledger 必显式：缺省兜底「账本1」在多用户同库下会把绑定静默落到他人账本
    # （bot_core._ledger 同一条不变式）；前端下拉无选中时不发请求
    if body.get("ledger") is None:
        raise HTTPException(400, "ledger 必填")
    try:
        ledger = int(body.get("ledger"))
    except (TypeError, ValueError):
        raise HTTPException(400, "ledger 必须是数字")
    if not _id64_ok(ledger):
        raise HTTPException(400, "ledger 超出范围")   # admin/系统上下文不走 _ledger_access，超界 id 只会在扫码回调 OverflowError
    if _is_member():
        _ledger_access(ledger)
    # 入口闸:直连未开启不发票,网关/关闭模式下扫码无意义(回调还有出口闸兜底)
    if bot_core._cfg().get("mode") != "direct":
        raise HTTPException(400, "微信扫码未开启：请先把模式切到「微信扫码」再生成二维码")
    label = str(body.get("label") or "")[:20]
    try:
        return _ilink.begin_login(ledger, label, "", _ilink_on_confirmed)
    except Exception as e:
        raise HTTPException(502, f"获取二维码失败：{e}")

@app.get("/api/bot/ilink/status")
def bot_ilink_status():
    """直连聚合状态:待扫码态 + 各成员实例状态(多人记账)。
    非管理员仅见自己有权访问的账本的绑定（与 qrcode/unbind 的按账本收口同口径）：
    bots 与 pending 同闸——待扫码态带账本号/归属组名，他人扫码状态不外泄。"""
    pend = _ilink.pending()
    uid = _uid()
    if (pend.get("state") not in (None, "", "off") and _is_member()
            and pend.get("ledger") not in _my_ledger_ids(uid)):
        pend = {"state": "off", "detail": ""}
    return {"mode": bot_core._cfg().get("mode", "off"),
            "pending": pend, "bots": _mine_only(_ilink.states())}

@app.post("/api/bot/ilink/unbind")
def bot_ilink_unbind(body: dict):
    """解绑成员:停实例 + 删凭证(白名单保守不动,孤儿 id 无害)。
    权限与 qrcode 同口径：admin 全局管，普通用户只能解绑自己账本的绑定。"""
    aid = str(body.get("account_id") or "")
    if not aid:
        raise HTTPException(400, "account_id 必填")
    with config.edit() as cfg:   # 读-改-写持锁（与扫码/设置页等并发写方互斥）
        entries = bot_ilink.ilink_entries(cfg)
        entry = next((e for e in entries if str(e.get("account_id")) == aid), None)
        if entry and _is_member():
            _ledger_access(int(entry.get("ledger") or 0))
        cfg.setdefault("bot", {})["ilink"] = [e for e in entries
                                              if str(e.get("account_id")) != aid]
    _bot_apply()
    return {"ok": True}

@app.get("/api/bot/ilink/qrimg")
def bot_ilink_qrimg():
    """二维码图片：qrcode_img_content 是「待编码的跳转 URL」
    （https://liteapp.weixin.qq.com/q/...?qrcode=...&bot_type=3）——
    服务端用 segno 把 URL 生成 PNG（同源回图，无混合内容/防盗链问题）。
    权限与 qrcode 发起端点同口径（admin 全局/成员限本账本）：二维码即绑定凭证，
    任何登录用户可取图=可扫走他人账本的绑定通道。"""
    if _is_member():
        p = _ilink.pending()
        led = p.get("ledger")
        if led is None:
            raise HTTPException(403, "当前无待确认的扫码，或无权查看")
        _ledger_access(int(led))
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
