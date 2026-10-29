"""微信机器人大脑（DESIGN 十章）：通道无关，文本进 / 文本出。

A 通道（bot_ilink iLink 直连）与设置页测试按钮都只调 handle()；
CLI：python bot_core.py 直接交互联调（bypass_auth），不依赖任何外部通道。
会话三态（per user，内存，TTL 30 分钟）：draft 草稿 / query 最近查询列表 /
del_pending 两步删除确认——指令用词互不冲突，可与草稿态共存。
"""
from __future__ import annotations

import datetime
import json
import re
import threading
from pathlib import Path

import ai
import applog
import config
import db as dbm

TTL_SECONDS = 30 * 60     # 三态会话 TTL；会话期内任何消息续期

_on_change = None         # 数据变更钩子(server 注册:唤醒长轮询,前端刷新)

def _bump() -> None:
    """bot 侧任何写库后调用;server 挂回调唤醒长轮询,App 前端实时刷新。"""
    if _on_change:
        try:
            _on_change()
        except Exception as e:
            applog.get("bot-core").warning(f"on_change 回调异常: {e}")
LIST_MAX = 10
ALL_MAX = 30
RANK_DOTS = 6             # L1 分类排行圆点条最大点数

_RE_OK = re.compile(r"^(y|yes|ok|确认|入账)$", re.I)
_RE_DROP = re.compile(r"^(n|no|丢弃|取消|不要)$")
_RE_DRAFT = re.compile(r"^(草稿|看看)$")
_RE_DEL_OK = re.compile(r"^确认删除$")
_RE_DEL_NO = re.compile(r"^取消删除$")
_RE_PAGE = re.compile(r"^(更多|对应明细|明细|上一页|上页)$")
_RE_CAT_PAGE = re.compile(r"^(.{1,12})明细$")   # 「XX明细」：XX=分类名（可简称），见 _cat_page
_RE_UNDO = re.compile(r"^(撤销|撤回|undo)$", re.I)
_RE_HELP = re.compile(r"^帮助$")
_RE_MONTH = re.compile(r"^(?:月报|分析)\s*(\d{4}-\d{2})?$")

_sessions: dict[str, dict] = {}
_lock = threading.Lock()
_user_locks: dict[str, threading.Lock] = {}

def _ulock(user: str) -> threading.Lock:
    """per-user 串行锁：同一 user 的并发消息（A 轮询线程 × 设置页测试线程）不交错
    读写会话状态——否则两个线程都能通过「有草稿」检查并各自确认入库（重复记账）。
    锁表注册由 _lock 保护（短临界区）；锁本身覆盖整个 handle（LLM 慢调用也无妨，
    同一人的消息本就应按序处理，不同用户互不影响）。"""
    with _lock:
        lk = _user_locks.get(user)
        if lk is None:
            lk = _user_locks[user] = threading.Lock()
        return lk

def _now() -> float:
    return datetime.datetime.now().timestamp()

def _session(user: str) -> dict:
    with _lock:
        s = _sessions.setdefault(user, {})
    now = _now()
    # 三态各自独立过期(删除态的 ts 键是 del_ts 不是 del_pending_ts);
    # 过期静默清理(无法主动通知,等下一条消息自然走新流程)
    for key, ts in (("draft", "draft_ts"), ("query", "query_ts"), ("del_pending", "del_ts")):
        if s.get(key) and now - s.get(ts, 0) > TTL_SECONDS:
            if key == "draft":
                _purge_receipts(s[key].get("items"))   # 超期草稿未入库,小票文件一并清理
            s[key] = None
    return s

def _today() -> str:
    return datetime.date.today().strftime("%Y-%m-%d")

def _cfg() -> dict:
    return config.load().get("bot") or {}

def _ledger(ledger_id: int | None) -> int | None:
    """解析目标账本：仅认显式指定（A 通道各 bot 绑定 / 测试按钮首条绑定）；
    未指定或账本已被删返回 None——调用方回复重绑提示。不做任何默认兜底：
    多用户同一库后"第一个账本/全局默认"都会落到他人账本（B 通道网关模式已下线）。"""
    if not ledger_id:
        return None
    row = dbm.connect().execute("SELECT id FROM ledgers WHERE id=?", (ledger_id,)).fetchone()
    return ledger_id if row else None

_BOT_NO_LEDGER = ("机器人未绑定有效账本（账本可能已被删除）："
                  "请到 App 设置→微信机器人重新扫码绑定。")

def _match_top_cat(rows: list, name: str, tx_type=None):
    """一级分类两段式匹配：先（按 type）精确名，再双向包含。
    _resolve_category（取 id）与 _cat_page（取显示名）共用；tx_type=None 时只按名匹配。"""
    r = next((x for x in rows if x["name"] == name
              and (tx_type is None or x["type"] == tx_type)), None)
    return r or next((x for x in rows if name in x["name"] or x["name"] in name), None)

def _resolve_category(ledger: int, tx_type: str, name) -> int | None:
    """一级分类名 → id：先按 type 精确匹配（与前端 aiItems 同口径），再放宽包含匹配。
    解析不到返回 None（category_id 留空，App 端编辑时可补）。"""
    if not name or not str(name).strip():
        return None
    hit = _match_top_cat(dbm.connect().execute(
        "SELECT id, name, type FROM categories WHERE ledger_id=? AND parent_id IS NULL",
        (ledger,)).fetchall(), str(name).strip(), tx_type)
    return hit["id"] if hit else None

# ---------- 渲染 ----------

def _rowget(r, k, default=""):
    """sqlite3.Row / dict 双兼容取值。"""
    try:
        return r[k] if k in r.keys() else default
    except AttributeError:
        return r.get(k, default)

_TYPE_SIGN = {"expense": "−", "income": "+", "transfer": ""}
_TYPE_CN = {"expense": "支出", "income": "收入", "transfer": "转账"}

def _date_show(it: dict) -> str:
    """时间显示遵循 AI 记一笔规则：带时段/时刻(早上→08:00 等)的流水显 HH:MM；
    没提时间的只显日期——入库时 norm_date 给纯日期补的当前时刻不属于用户语义,不展示。
    判定：ai_meta.time_source=='none'（确认入库时打的标）或日期本身只有 10 位。"""
    d = (_rowget(it, "date") or "").strip()
    if len(d) <= 10:
        return d[:10]
    meta = _rowget(it, "ai_meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta) if meta else {}
        except (json.JSONDecodeError, ValueError):
            meta = {}
    if isinstance(meta, dict) and meta.get("time_source") == "none":
        return d[:10]
    return d[:16]

def _tx_name(r) -> str:
    """流水主名：name 缺省回退 note，再缺省「（未命名）」，截 40 字（明细行/两行卡共用）。"""
    return (_rowget(r, "name") or _rowget(r, "note") or "（未命名）")[:40]

def _fmt_item(no: int, it: dict) -> str:
    """流水行文本化——对齐 App 流水卡片（ai.js txRow）的信息结构：
    主行 = 序号. 名称 符号金额(−支出/+收入/转账无符号,小票🧾随金额)；
    副行 = 日期 · 备注 · 商户 · 分类 · 账户。副行另起一行且不缩进——
    行首空格会被微信按 Markdown 续行合并,导致"金额后没有换行"。
    dup_hint（db.dup_hints 注记）行尾附 ⚠️ 疑似重复提示。"""
    t = _rowget(it, "type") or "expense"
    head = _tx_name(it)
    note = _rowget(it, "note") or ""
    merchant = _rowget(it, "merchant") or ""
    sub = [_date_show(it),
           note if note and note != head else "",
           merchant if merchant and merchant != note else "",
           _rowget(it, "category_name") or _TYPE_CN.get(t, ""),
           _rowget(it, "account") or _rowget(it, "account_name") or ""]
    amt = f"{_TYPE_SIGN.get(t, '')}¥{_rowget(it, 'amount', 0) / 100:.2f}"
    if _rowget(it, "receipt_path"):
        amt += " 🧾"
    line1 = (f"{no}. " if no else "") + f"{head} {amt}"
    dh = _rowget(it, "dup_hint")
    if isinstance(dh, dict):
        if dh.get("kind") == "db":
            line1 += f" ⚠️同日同额已记「{(dh.get('of') or {}).get('name', '')}」"
        elif dh.get("kind") == "batch":
            line1 += f" ⚠️与第{dh.get('no')}笔重复"
    sub_line = " · ".join(x for x in sub if x)
    return line1 + (f"\n{sub_line}" if sub_line else "")

def _rows_block(rows: list, start: int) -> str:
    """流水行批量渲染：start=起始序号(草稿从 1 计,翻页按页偏移)。"""
    return "\n".join(_fmt_item(start + i, r) for i, r in enumerate(rows))

def _cat_rank(cats: list) -> str:
    """L1 分类排行圆点条：「分类 ●点条 金额」前 8 行，净额降序（stats 附件已排好）。
    点数=|净额|/最大净额×RANK_DOTS 四舍五入,最少 1 点；金额分转元 1 位小数；
    分类名不足 4 字补全角空格。"""
    valid = [c for c in (cats or []) if c.get("net")]
    if not valid:
        return ""
    mx = max(abs(c["net"]) for c in valid) or 1

    def row(c):
        name = c["name"] + "　" * max(0, 4 - len(c["name"]))
        return f"{name} {'●' * max(1, round(abs(c['net']) / mx * RANK_DOTS))} {c['net'] / 100:.1f}"

    return "\n".join(row(c) for c in valid[:8])

def _dup_ids(items: list) -> list[int]:
    """草稿条目里对库重复（kind=db）命中的流水 id，按条目顺序——_dup_view 取行用。"""
    return [it["dup_hint"]["of"]["id"] for it in items
            if isinstance(it.get("dup_hint"), dict)
            and it["dup_hint"].get("kind") == "db"
            and (it["dup_hint"].get("of") or {}).get("id")]

def _dup_view(s: dict, ledger: int) -> str:
    """草稿态「查看」：把草稿里标 ⚠️ 各笔（kind=db）命中的库中流水列成
    两行卡对照——看完再决定「确认」全记，或删掉草稿里的重复笔。"""
    ids = _dup_ids(s["draft"]["items"])
    if not ids:
        return "草稿里没有对库的 ⚠️ 重复笔（批内重复直接说「第N笔删了」）。"
    lines = [f"{no}. {_fmt_item(0, row)}" for no, tid in enumerate(ids, 1)
             if (row := _tx_row(tid, ledger))]
    # 仍指「第N笔删了」防误删其他笔
    drop = "或「丢弃」去掉草稿里那笔" if len(s["draft"]["items"]) == 1 else "或说「第N笔删了」去掉草稿里那笔"
    return "已记的疑似重复流水：\n" + "\n".join(lines) + f"\n对照后「确认」全记 · {drop}"

def _draft_reply(s: dict, title: str = "") -> str:
    """草稿卡文本版——标题/话术与 App AI 页卡片（ai.js aiBookingCard）一字不差：
    「识别到 N 笔，请确认」；仅尾部动作行是微信交互形态（App 为按钮）。
    尾注改「查看」——先看已记的那笔再决定删/确认。"""
    items = s["draft"]["items"]
    n = len(items)
    dup = any(isinstance(it.get("dup_hint"), dict) for it in items)
    return (f"{title or f'识别到 {n} 笔，请确认'}：\n" + _rows_block(items, 1) +
            ("\n⚠️ 标 ⚠️ 的笔疑似与已记流水重复，回复「查看」显示重复流水" if dup else "") +
            f"\n回复「确认」记入 {n} 笔 · 「丢弃」取消 · 有误直接说（如「金额改为30」）")

def _help() -> str:
    return ("🤖 柴米记账助手\n"
            "· 记账：直接说，如「午饭25 打车13」，小票直接拍照发来\n"
            "· 草稿确认中：确认 / 丢弃，改字段直接说\n"
            "· 查账：如「上周餐饮多少钱」「本月花了多少」；「餐饮明细」看分类流水，「对应明细」看区间流水\n"
            "· 改流水：查询后说「第2笔改成86」「打车那笔删了」\n"
            "· 撤销：删最近一笔机器人记账 · 月报：月报 / 月报 2026-09 · 帮助")

# ---------- SSE 收集（ai.chat / monthly_analysis 结构化事件流 → dict） ----------

def _collect(events) -> dict:
    """ai 事件流（结构化 dict，见 ai.sse_stream 注释）→ 汇总：文本拼接，
    booking/stats/error 单值（后者覆盖前者）。"""
    text, booking, stats, error = [], None, None, None
    for d in events:
        if "error" in d:
            error = d["error"]
        elif "booking" in d:
            booking = d["booking"]
        elif "stats" in d:
            stats = d["stats"]
        elif "text" in d:
            text.append(d["text"])
    return {"text": "".join(text), "booking": booking, "stats": stats, "error": error}

# ---------- 主入口 ----------

def handle(text: str, user: str, image_path: str | None = None,
           bypass_auth: bool = False, ledger_id: int | None = None,
           attribution: str = "") -> str:
    """消息统一入口：返回回复文本。A 通道在轮询线程调用后 POST 送出(带该 bot 绑定的
    ledger_id 与用户名——绑定谁,记的账自动带谁的归属)。bypass_auth 供 CLI / 设置页测试按钮使用。
    并发口径：白名单校验先行(陌生 id 不注册 per-user 锁,防锁表被撑大——能发消息的
    微信用户 id 不可枚举)，授权用户全程持 per-user 锁串行（见 _ulock）。"""
    cfg = _cfg()
    if not bypass_auth and user not in (cfg.get("allowed_users") or []):
        return (f"未绑定：你的 ID 是「{user}」，请到 App 设置→微信机器人→白名单添加后重试。")
    with _ulock(user):
        return _handle(text, user, image_path, ledger_id, attribution, cfg)

def _handle(text: str, user: str, image_path: str | None,
            ledger_id: int | None, attribution: str, cfg: dict) -> str:
    text = (text or "").strip()
    ledger = _ledger(ledger_id)
    if ledger is None:
        return _BOT_NO_LEDGER
    s = _session(user)

    # 1) 草稿指令（DRAFT_PENDING）
    if s.get("draft"):
        if _RE_OK.match(text):
            return _confirm(s, ledger, attribution)
        if _RE_DROP.match(text):
            _purge_receipts(s["draft"].get("items"))
            s["draft"] = None
            return "已丢弃 🗑️"
        if _RE_DRAFT.match(text):
            return _draft_reply(s)
        if text == "查看":
            return _dup_view(s, ledger)
    # 2) 删除确认（PENDING_DELETE；任何其他消息在后续分支里被取消）
    if s.get("del_pending"):
        if _RE_DEL_OK.match(text):
            return _do_delete(s, ledger)
        if _RE_DEL_NO.match(text):
            s["del_pending"] = None
            return "已取消删除"
    if s.get("query") and _RE_PAGE.match(text):
        return _page(s, ledger, text)
    #      直接问「餐饮明细」会被系统提示的「没提时间默认当月」带偏——本地路由与「更多」同思路不过模型）
    m = _RE_CAT_PAGE.match(text)
    if m and s.get("query"):
        return _cat_page(s, ledger, m.group(1).strip())
    # 4) 撤销
    if _RE_UNDO.match(text):
        return _undo(ledger, attribution)
    # 5) 月报（精确匹配；模糊句落入编辑/对话由模型处理）
    m = _RE_MONTH.match(text)
    if m:
        return _monthly(ledger, m.group(1) or datetime.date.today().strftime("%Y-%m"))
    # 6) 帮助
    if _RE_HELP.match(text):
        return _help()
    # 7) 图片：小票 → 开草稿 / 追加到当前草稿
    if image_path:
        return _receipt(s, ledger, image_path)
    # 8) 其余一切：有草稿进编辑回合，无草稿走 ai.chat（记账/查账模型自路由）
    s["del_pending"] = None   # 走到这说明不是删除确认指令 → 取消挂起的删除
    if s.get("draft"):
        return _edit_flow(s, ledger, text)
    return _chat_flow(s, ledger, text)

# ---------- 记账链路 ----------

def _confirm(s: dict, ledger: int, attribution: str = "") -> str:
    """「确认」：批量入库（need_confirm=0 正式流水，source='bot'）;
    attribution=绑定的账本用户,写入 tags.attribution(流水页按归属筛选可用)。
    入库异常时草稿放回会话（DB 事务已回滚,输入与小票引用不丢,孤儿清理链路管不到）,
    用户重试「确认」即可,不必重新口述/重拍小票。"""
    draft = s["draft"]
    s["draft"] = None
    c = dbm.connect()
    ids = []
    try:
        with c:
            for it in draft["items"]:
                ttype = it.get("type") if it.get("type") in ("expense", "income", "transfer") else "expense"
                d = it.get("date") or _today()
                ids.append(dbm.insert_tx(
                    c, ledger, type=ttype, amount=int(it.get("amount") or 0),
                    category_id=_resolve_category(ledger, ttype, it.get("category_name")),
                    account_id=it.get("account_id"),
                    date=d,
                    name=(it.get("name") or "")[:40], note=(it.get("note") or "")[:80],
                    receipt_path=it.get("receipt_path"),
                    tags=({"attribution": attribution} if attribution else None),
                    source="bot",
                    # time_source:AI 记一笔时间规则——date 带 HH:MM(时段/时刻映射)记 "ai",
                    # 纯日期记 "none"(显示层据此只显日期,norm_date 补的当前时刻不展示)
                    ai_meta={"via": "bot", "chat_text": (draft.get("text") or "")[:200],
                             "time_source": "ai" if len(d) > 10 else "none"}))
    except Exception as e:
        s["draft"] = draft
        applog.get("bot-core").warning(f"草稿入库失败(草稿已保留): {type(e).__name__}: {e}")
        return "⚠️ 入库失败，草稿已保留，请稍后重发「确认」。"
    _bump()
    return f"✅ 已记入 {len(ids)} 笔 · 回复「撤销」可删最近一笔"

def _receipt(s: dict, ledger: int, image_path: str) -> str:
    """小票图片 → parse_receipt → 开草稿或追加（连拍多张场景）。"""
    out = ai.parse_receipt(image_path)
    if out.get("error"):
        unlink_receipt(image_path)   # 识别失败的图片不留草稿,文件直接清理(防孤儿)
        return f"⚠️ {out['error']}"
    it = {"type": out.get("type") or "expense",
          "amount": round(float(out.get("amount") or 0) * 100),
          "category_name": out.get("category_name") or "", "account": "", "account_id": None,
          "date": out.get("date") or _today(),
          "name": out.get("name") or "小票", "note": out.get("note") or "",
          # 入库约定与 App 一致:存 "receipts/文件名"(viewReceipt 按相对 URL 取图)。
          # parse_receipt 不回传该字段,由本函数从落盘路径补齐(server /api/ai/receipt 同款)
          "receipt_path": "receipts/" + Path(image_path).name}
    title = ""
    if not s.get("draft"):
        s["draft"] = {"items": [], "text": "小票"}
        title = "小票识别结果，请确认"   # 首张:App 同款标题;追加时用统一草稿卡
    s["draft"]["items"].append(it)
    s["draft_ts"] = _now()
    return _draft_reply(s, title)

def _undo(ledger: int, attribution: str = "") -> str:
    """撤销：删最近一笔 source='bot' 的流水（24h 内，限本人）。
    多人共用账本按归属过滤——A 通道只删绑定人记的（tags.attribution=归属名），
    B 网关/CLI（无归属）只删同样无归属的，不误删家人刚记的流水。"""
    if attribution:
        cond, args = " AND json_extract(tags,'$.attribution')=?", (attribution,)
    else:
        cond, args = " AND COALESCE(json_extract(tags,'$.attribution'),'')=''", ()
    row = dbm.connect().execute(
        f"SELECT id, name, amount FROM transactions WHERE ledger_id=? AND source='bot' "
        f"AND created>=datetime('now','localtime','-1 day'){cond} ORDER BY id DESC LIMIT 1",
        (ledger, *args)).fetchone()
    if not row:
        return "最近 24 小时没有你经机器人记的流水。"
    _remove_tx(row["id"])
    _bump()
    return f"🗑️ 已撤销：{row['name'] or '（未命名）'} ¥{row['amount'] / 100:.2f}"

def _remove_tx(tid: int) -> None:
    """删行 + 解除退款关联 + 清小票文件（撤销/两步删除共用）。"""
    with dbm.connect() as c:
        old_path = dbm.delete_tx(c, tid)
    unlink_receipt(old_path)

def is_image(data: bytes) -> bool:
    """图片内容魔数校验（JPG/PNG/WEBP）：App 上传 / 网关 bot_receipt / 微信直连媒体下载
    三路共用——只放行图片字节，任意内容（脚本/HTML 等）不落盘。"""
    return (data[:3] == b"\xff\xd8\xff"
            or data[:8] == b"\x89PNG\r\n\x1a\n"
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))

def process_receipt(path) -> bool:
    """小票压缩（三路上传共用，原地覆写）：缩至 1080px → JPEG 85。
    不做自动裁剪——App 路径在预览卡片中手动裁剪（viewReceipt → ✂️），微信/网关只压缩。"""
    try:
        from PIL import Image
        img = Image.open(path)
        if img.size[0] > 1080:
            img = img.resize((1080, int(img.size[1] * 1080 / img.size[0])), Image.LANCZOS)
        img.convert("RGB").save(path, "JPEG", quality=85)
        return True
    except Exception:
        return False

def unlink_receipt(receipt_path: str | None) -> None:
    """删小票文件（bot/server 共用的唯一入口）：按文件名拼 receipts 目录，
    None/空串/失败一律静默——清理永不阻断主流程。"""
    if not receipt_path:
        return
    try:
        (config.receipts_dir() / Path(receipt_path).name).unlink(missing_ok=True)
    except OSError:
        pass

def _purge_receipts(items) -> None:
    """草稿整体废弃（丢弃/超期/编辑删光）时清小票文件：条目未入库，
    receipt_path 只活在草稿里，不清就是孤儿文件（DB 删行链路管不到它们）。"""
    for it in items or []:
        if isinstance(it, dict) and it.get("receipt_path"):
            unlink_receipt(it["receipt_path"])

def _merge_receipts(old_items, new_items) -> None:
    """编辑回合整组替换草稿后，把旧条目的 receipt_path 迁到新条目，脱离草稿的图片清理。
    编辑模型上下文不带 receipt_path（_draft_ctx 隐私口径），不迁则确认入库即丢小票关联。
    等长按位次迁（in-place 改字段是主流场景，模型输出保序）；不等长仅同位次
    (type,金额,日期) 未变的条目才迁——增删条目后位次错位，宁可清理文件也不误挂到别的笔。"""
    old = [it for it in (old_items or []) if isinstance(it, dict)]
    same_len = len(old) == len(new_items)
    carried = set()
    for i, it in enumerate(new_items):
        if i >= len(old) or it.get("receipt_path"):
            continue
        o = old[i]
        if not same_len:
            if ((o.get("type"), o.get("amount"), (o.get("date") or "")[:10])
                    != (it.get("type"), it.get("amount"), (it.get("date") or "")[:10])):
                continue
        if o.get("receipt_path"):
            it["receipt_path"] = o["receipt_path"]
            carried.add(i)
    _purge_receipts([o for i, o in enumerate(old) if i not in carried])

# ---------- 对话 / 编辑链路 ----------

def _chat_flow(s: dict, ledger: int, text: str) -> str:
    """无草稿态：ai.chat 全权处理（记账→booking 草稿 / 查账→统计回答+列表+条形图）。
    兜底在 ai.chat 后端核心:LLM 不可用时补发正则拆分的 booking,
    bot 自动同源获得;连兜底都拆不出才透传错误。传输层依旧零解析。"""
    out = _collect(ai.chat(text, ledger))
    if out["error"] and not out["text"] and not out["booking"]:
        return f"⚠️ {out['error']}"   # 兜底也没拆出东西才透传错误(兜底在 ai.chat 后端核心)
    reply = out["text"].strip()
    st = out["stats"]
    if st:
        s["query"] = {"dfrom": st.get("from") or "", "dto": st.get("to") or "",
                      "flow": st.get("flow") or "expense",
                      "category": st.get("category") if st.get("matched") else None}
        s["query_ts"] = _now()
        if st.get("matched") and st.get("items"):
            # 命中分类：原始词换规范名（「餐饮」→「餐饮美食」），紧凑明细首屏与「XX明细」同形态
            s["query"]["category"] = _match_cat_name(ledger, st.get("category") or "") \
                or s["query"]["category"]
            lst = _list_view(s, ledger, LIST_MAX)
            if lst:
                reply += ("\n" if reply else "") + lst
        else:
            reply += ("\n" if reply else "") + _cat_rank(st.get("categories") or [])
            if reply and "回复" not in reply:
                reply += "\n（回复「对应明细」看这笔区间的流水列表）"
    if out["booking"]:
        items = out["booking"].get("items") or []
        if items:
            btext = out["booking"].get("text") or ""
            s["draft"] = {"items": items, "text": btext or text}
            s["draft_ts"] = _now()
            # 兜底 booking 的 text 是提示标题(≠原句),沿用为草稿标题;LLM booking 的 text=原句不重复显示
            reply = _draft_reply(s, btext if btext and btext != text else "")
    return reply.strip() or "（没有返回内容）"

def _edit_flow(s: dict, ledger: int, text: str) -> str:
    """草稿态：先过编辑回合（revise/tx/new/none），new/none 落回 ai.chat。"""
    out = ai.edit_round(text, _draft_ctx(s), _query_ctx(s, ledger), ledger)
    tgt = out.get("target")
    if tgt == "draft":
        items = out.get("items") or []
        if not items:
            _purge_receipts(s["draft"].get("items"))
            s["draft"] = None
            return "草稿条目已全部删除。重新说一句即可再记。"
        _merge_receipts(s["draft"].get("items"), items)
        s["draft"] = {"items": items, "text": (s["draft"].get("text") or "") + ";" + text}
        s["draft_ts"] = _now()
        return _draft_reply(s)   # App 等价:卡片原地重渲染 → 重发同款草稿卡
    if tgt == "tx":
        if out.get("action") == "delete":
            return _ask_delete(s, ledger, out["id"])
        if out.get("action") == "update":
            return _apply_patch(ledger, out["id"], out.get("patch") or {})
    return _chat_flow(s, ledger, text)   # new / none：草稿保留，正常对话

def _draft_ctx(s: dict) -> list | None:
    """给编辑模型的草稿上下文（金额转元，字段与记账解析器一致）。"""
    if not s.get("draft"):
        return None
    return [{"no": i + 1, "type": it.get("type"), "amount": (it.get("amount") or 0) / 100,
             "category_name": it.get("category_name") or "", "account": it.get("account") or "",
             "date": it.get("date") or "", "name": it.get("name") or "", "note": it.get("note") or ""}
            for i, it in enumerate(s["draft"]["items"])]

def _query_ctx(s: dict, ledger: int) -> list | None:
    """给编辑模型的流水列表上下文：只带最近一次查询已展示的行
    （首屏≤LIST_MAX 笔；「更多」显示过全部后=全部行）。"""
    q = s.get("query")
    if not q:
        return None
    rows = _fetch_rows(ledger, q, ALL_MAX) if q.get("more") is False else _fetch_rows(ledger, q, LIST_MAX)
    return [{"no": r["id"], "type": r["type"], "amount": r["amount"] / 100,
             "category_name": r["category_name"] or "", "account": r["account_name"] or "",
             "date": (r["date"] or "")[:10], "name": r["name"] or "",
             "note": r["note"] or "", "merchant": r["merchant"] or ""}
            for r in rows]

# ---------- 流水查询 / 明细列表 / 编辑 ----------

def _q(q: dict) -> dict:
    """会话缓存的查询条件 → dbm.fetch_tx_rows/count_tx_rows 的关键字参数。"""
    return {"flow": q.get("flow") or "expense", "dfrom": q.get("dfrom") or "2000-01-01",
            "dto": q.get("dto") or "2100-01-01", "category": q.get("category") or ""}

def _fetch_rows(ledger: int, q: dict, limit: int | None = None) -> list:
    """按缓存查询条件取流水（dbm.fetch_tx_rows 公共口径），时间倒序。limit=None 取全部。"""
    return dbm.fetch_tx_rows(dbm.connect(), ledger, **_q(q), limit=limit)

def _q_title(q: dict) -> str:
    """缓存查询标题：分类（有则）· 区间（无界=全部流水）。"""
    rng = f"{q.get('dfrom')}~{q.get('dto')}" if q.get("dfrom") and q.get("dto") else "全部流水"
    return f"{q['category']} · {rng}" if q.get("category") else rng

def _rows_compact(rows: list) -> str:
    """明细行紧凑版：「日期 名称 符号金额」——只三要素，无序号无副行；
    （不用 ●：分类排行圆点已占用，两类列表形态要能一眼区分）。"""
    def row(r):
        t = _rowget(r, "type") or "expense"
        return (f"▪ {_date_show(r)[:10]} {_tx_name(r)} "
                f"{_TYPE_SIGN.get(t, '')}¥{_rowget(r, 'amount', 0) / 100:.2f}")
    return "\n".join(map(row, rows))

def _list_view(s: dict, ledger: int, limit: int) -> str | None:
    """明细视图（首屏 LIST_MAX / 「更多」展开 ALL_MAX 共用）：紧凑行时间倒序取 limit 笔。
    more 标记三态：首屏 True=还有更多 / 展开后 False=已显示完（「更多」按此分流）。
    无流水返回 None 由调用者组织文案。"""
    q = s["query"]
    rows = _fetch_rows(ledger, q, limit)
    s["query_ts"] = _now()
    if not rows:
        return None
    total = dbm.count_tx_rows(dbm.connect(), ledger, **_q(q))   # 与 _fetch_rows 同 WHERE
    if limit > LIST_MAX:   # 展开视图：标题带总数，more 复位；超 ALL_MAX 只提示去 App
        q["more"] = False
        head, tail = f"【{_q_title(q)} · 共{total}笔】", \
            (f"\n（仅显示最近{limit}笔，其余请到 App 查看）" if total > limit else "")
    else:
        q["more"] = total > limit
        head = f"【{_q_title(q)}】"
        tail = f"\n（共{total}笔，回复「更多」显示全部）" if q["more"] else ""
    return head + "\n" + _rows_compact(rows) + tail

def _page(s: dict, ledger: int, text: str) -> str:
    """列表指令分发：「更多」按 q["more"] 三态（True=展开全部 / None=先出首屏 /
    False=已显示完）；「明细/对应明细/上一页」回首屏（最多 LIST_MAX 笔）。"""
    q = s["query"]
    if text == "更多":
        if q.get("more") is False:
            return "已全部显示，没有更多了。"
        if q.get("more"):
            return _list_view(s, ledger, ALL_MAX)
    return _list_view(s, ledger, LIST_MAX) or "这段区间没有流水。"

def _match_cat_name(ledger: int, name: str) -> str | None:
    """一级分类名 → 规范名（_cat_page 与命中分类明细共用，简称可命中）。"""
    hit = _match_top_cat(dbm.connect().execute(
        "SELECT name FROM categories WHERE ledger_id=? AND parent_id IS NULL",
        (ledger,)).fetchall(), name)
    return hit["name"] if hit else None

def _cat_page(s: dict, ledger: int, cat: str) -> str:
    """「XX明细」：沿用上一条统计的区间/收支侧切到 XX 分类，明细形态同区间列表。
    分类名经 _match_cat_name 对齐（简称可命中）。"""
    q = s["query"]
    name = _match_cat_name(ledger, cat)
    if not name:
        return f"没有叫「{cat}」的分类，试试完整分类名。"
    old = q.get("category")
    q["category"] = name
    r = _list_view(s, ledger, LIST_MAX)
    if r is None:
        q["category"] = old   # 该区间没这分类的流水：还原，别污染后续查询
        fw = "收入" if q.get("flow") == "income" else "支出"
        return f"「{name}」在这段区间没有{fw}流水。"
    return r

def _tx_row(tid: int, ledger: int):
    # 单笔取行用 LEFT JOIN（外连接）：无分类的行也取得到（列表查询的 INNER JOIN 口径会排除它们）
    return dbm.connect().execute(
        f"SELECT {dbm.TX_COLS} FROM transactions t LEFT JOIN categories c ON t.category_id=c.id "
        f"LEFT JOIN accounts a ON t.account_id=a.id WHERE t.id=? AND t.ledger_id=?",
        (tid, ledger)).fetchone()

_TX_NOT_FOUND = "没找到这笔流水（可能已删除或列表过期），请重新查询后再试。"

def _ask_delete(s: dict, ledger: int, tid: int) -> str:
    row = _tx_row(tid, ledger)
    if not row:
        return _TX_NOT_FOUND
    s["del_pending"] = tid
    s["del_ts"] = _now()
    return (f"⚠️ 确认删除：{_fmt_item(0, row)} ?\n"
            "回复「确认删除」执行 · 「取消删除」放弃")

def _do_delete(s: dict, ledger: int) -> str:
    tid = s["del_pending"]
    s["del_pending"] = None
    row = _tx_row(tid, ledger)
    _remove_tx(tid)
    _bump()
    return "🗑️ 已删除：" + (_fmt_item(0, row) if row else f"#{tid}")

def _apply_patch(ledger: int, tid: int, patch: dict) -> str:
    row = _tx_row(tid, ledger)
    if not row:
        return _TX_NOT_FOUND
    fields = {}
    if patch.get("amount") is not None:
        try:
            fields["amount"] = round(float(str(patch["amount"]).replace(",", "")) * 100)
        except (TypeError, ValueError):
            pass
    if patch.get("category_name"):
        cid = _resolve_category(ledger, row["type"], patch["category_name"])
        if cid:
            fields["category_id"] = cid
    if patch.get("account"):
        aid = ai._resolve_account(ledger, str(patch["account"]))
        if aid:
            fields["account_id"] = aid
    if patch.get("date"):
        fields["date"] = str(patch["date"])[:16]
    for k in ("name", "note", "merchant"):
        if patch.get(k) is not None:
            fields[k] = str(patch[k])[:60]
    if not fields:
        return "没有识别到要修改的字段，请说得更明确些（如「第2笔改成86.5」）。"
    c = dbm.connect()
    with c:
        dbm.patch_tx(c, tid, fields)
    new = _tx_row(tid, ledger)
    _bump()
    return "✏️ 已改：" + _fmt_item(0, row) + "\n     → " + _fmt_item(0, new)

# ---------- 月报 ----------

def _monthly(ledger: int, month: str) -> str:
    out = _collect(ai.monthly_analysis(ledger, month))
    if out["error"] and not out["text"]:
        return f"⚠️ {out['error']}"
    return f"📊 {month} 月报：\n{out['text']}".strip()

# ---------- CLI 冒烟（python bot_core.py；不依赖任何通道） ----------

if __name__ == "__main__":
    applog.setup()   # CLI 独立进程也走统一日志
    dbm.init_db()
    # _ledger 只认显式指定（无默认兜底）：CLI 取库中首个账本充任绑定，恢复免通道联调
    row = dbm.connect().execute("SELECT id FROM ledgers ORDER BY sort, id LIMIT 1").fetchone()
    lg = row["id"] if row else None
    print("柴米微信机器人大脑 CLI —— 直接输入对话；img <路径> 发小票；q 退出"
          + ("" if lg else "\n（库中无账本，先建账再联调）"))
    while True:
        try:
            line = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line.lower() == "q":
            break
        img = None
        if line.lower().startswith("img "):
            img, line = line[4:].strip(), ""
        try:
            print("bot> " + handle(line, user="cli", image_path=img, bypass_auth=True,
                                  ledger_id=lg))
        except Exception as e:   # CLI 联调不吞栈，直接暴露问题
            import traceback
            traceback.print_exc()
            print(f"bot> ❌ {e}")