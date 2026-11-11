"""入库/撤销：批次事务（增强回填、批内退款回填、rescan）与整批撤销。"""
from __future__ import annotations

import sqlite3

import applog
import db as dbm

from import_common import _chunks, _is_placeholder_name, _is_new_row
from import_match import _bracket_of, _match_refund, _pool_entry

_log = applog.get("import")


def _ok_date(p: dict) -> bool:
    """confirm 提交行日期放行（防篡改/损坏；口径与 parse 期的 db.is_valid_date 一致）。"""
    return dbm.is_valid_date(str(p.get("date") or ""))

# ---------- 入库 ----------

def _ref_ok_sets(conn: sqlite3.Connection, ledger_id: int, previews: list) -> tuple[set, set, set]:
    """confirm 提交行的引用白名单（与 server._check_tx_refs 同口径）：预览体来自客户端
    （confirm 可被直调改体），category_id/account_id/to_account_id 必须属于本账本、
    正数 refund_for 必须指向本账本的支出行——放行会把别账本的分类/账户名 JOIN 进本账本
    列表，或落一条绕过 _link_refund 校验的悬空退款链。
    按批内实际出现的 id 分块 IN 一次查齐：5 万行也只查三组，不逐行打点。"""
    news = [p for p in previews if _is_new_row(p)]

    def wanted(keys) -> set:
        out = set()
        for p in news:
            for k in keys:
                v = p.get(k)
                if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                    out.add(v)
        return out

    def valid(table: str, ids: set, extra: str = "") -> set:
        ok = set()
        for part in _chunks(sorted(ids)):
            marks = ",".join("?" * len(part))
            ok.update(r[0] for r in conn.execute(
                f"SELECT id FROM {table} WHERE ledger_id=? AND id IN ({marks}){extra}",
                (ledger_id, *part)))
        return ok

    return (valid("categories", wanted(("category_id",))),
            valid("accounts", wanted(("account_id", "to_account_id"))),
            valid("transactions", wanted(("refund_for",)), " AND type='expense'"))

def _refs_ok(p: dict, cats_ok: set, accs_ok: set, refund_ok: set) -> bool:
    """行级引用判定（_ref_ok_sets 的消费端，_insert_rows 逐行调用）。
    类型闸在前：直调体可给 category_id="x"/account_id=[] 等非整数——引用集合是 int 集，
    unhashable 值（list/dict）做 `in` 直接 TypeError → 整单 500；标量错型（"x"）虽可 hash
    但不是合法 id，同样按行跳过（truthy 错型在此拦截，None/0=未指定走后面）。"""
    for k, ok in (("category_id", cats_ok), ("account_id", accs_ok), ("to_account_id", accs_ok)):
        v = p.get(k)
        if not v:
            continue
        if not isinstance(v, int) or isinstance(v, bool) or v not in ok:
            return False
    rf = p.get("refund_for")
    if isinstance(rf, int) and not isinstance(rf, bool) and rf > 0 and rf not in refund_ok:
        return False
    if rf is not None and not isinstance(rf, (int, float)):
        return False
    return True

def _backfill_pay_dupes(conn: sqlite3.Connection, ledger_id: int, previews: list[dict]) -> None:
    """态A 增强：跨源重复（pay_dupe）行虽跳过入库，但用平台账单的富信息回填已有流水——
    名称仅当已有值是占位/泛化（或等于商户名）时替换为订单标题；订单号仅当缺失时补充。
    pay_dupe_of 来自客户端预览体（confirm 可被直调改体），读写一律限本账本：否则任一
    账本成员可凭可枚举的自增 id 改写他人账本流水的名称/订单号（退款匹配键）。"""
    for p in previews:
        pid = p.get("pay_dupe_of")
        if not p.get("pay_dupe") or not isinstance(pid, int) or isinstance(pid, bool) \
                or not 0 < pid < 2 ** 62:
            continue   # 坏形态/超界 id 按无此行跳过（直调体可达；int64 外的绑参会 500）
        old = conn.execute("SELECT name, merchant, order_no FROM transactions WHERE id=? AND ledger_id=?",
                           (pid, ledger_id)).fetchone()
        if not old:
            continue
        sets, args = [], []
        old_name = (old["name"] or "").strip()
        new_name = p.get("name")
        if isinstance(new_name, str) and new_name and (not old_name or _is_placeholder_name(old_name)
                                                       or old_name == (old["merchant"] or "").strip()):
            # 文本上限走 db._text_clip 单源（本函数是 UPDATE 已有流水，不属导入 INSERT 的原文保真例外）
            sets.append("name=?"); args.append(dbm._text_clip("name", new_name))
        new_order = p.get("order_no")
        if isinstance(new_order, str) and new_order and not old["order_no"]:
            sets.append("order_no=?"); args.append(new_order)
        if sets:
            conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id=? AND ledger_id=?",
                         args + [pid, ledger_id])

def _row_reject(p: dict, cats_ok: set, accs_ok: set, refund_ok: set) -> str | None:
    """逐行四类闸门（确认导入按行跳过、其余照常入库）：返回拒绝理由（进日志），
    None=放行。预览体来自客户端（confirm 可被直调改体），值域与 App 写入口同闸
    单源校验——坏 type/负金额落库会让导出与统计永久失真；坏日期行原样入库会
    静默掉出所有区间统计、还会让 import_batches 边界脏；row_hash 是 INSERT 必填列；
    跨账本/悬空 category_id/account_id/refund_for 不落库。"""
    if not _ok_date(p):
        return f"坏日期 {p.get('date')!r}"
    try:
        dbm.check_tx_fields(p["type"], p["amount"])
    except (ValueError, KeyError, TypeError):
        return f"坏值 type={p.get('type')!r} amount={p.get('amount')!r}"
    if not p.get("row_hash"):
        return "缺 row_hash"
    # source 不做白名单：confirm 接受 app/manual/bot 等用户侧值是被测试锁定的契约
    # （跨源配对手动行的增强回填、NULL-source 史前行的导出回环都依赖），且影响范围
    # 仅限调用者自己的账本（解析路径恒产平台 id，伪造仅直调可达），不构成越权
    if not _refs_ok(p, cats_ok, accs_ok, refund_ok):
        return (f"引用非法 cat={p.get('category_id')!r} "
                f"acc={p.get('account_id')!r}/{p.get('to_account_id')!r} "
                f"refund_for={p.get('refund_for')!r}")
    return None

def _backfill_batch_refunds(conn: sqlite3.Connection, inserted: dict[int, int],
                            pending_refund: list[tuple[int, int]],
                            gate_confs: dict[int, str | None]) -> None:
    """批内退款回填（顺序无关）：预览序号 → 真实行 id。走 db.link_refund 单点
    （type 强转/日期序/下游指向/status 重算一体）——此前手写 UPDATE 只补两列，绕过
    单点声明。目标不在本批（被跨源/手动配对层标 skip、或重复行）或 link 被拒
    （如同批日期序被篡改）时打回待确认并清退款态，等人工/rescan 关联。"""
    for row_pos, tgt_pos in pending_refund:
        if row_pos not in inserted:
            continue
        rid = inserted[row_pos]
        if tgt_pos in inserted:
            try:
                dbm.link_refund(conn, rid, inserted[tgt_pos],
                                conn.execute("SELECT amount FROM transactions WHERE id=?",
                                             (rid,)).fetchone()["amount"],
                                gate_confs.get(row_pos))
                continue
            except ValueError as ex:
                _log.warning(f"批内退款回填被拒(行 {rid}): {ex}")
        dbm.clear_refund(conn, rid, back_to_queue=True)
        _log.warning(f"批内退款的目标行被跳过(序号{tgt_pos}),退款行 {rid} "
                     "已转待确认等待人工/rescan 关联")

def _insert_rows(conn: sqlite3.Connection, ledger_id: int, previews: list[dict],
                 batch_id: int) -> dict[int, int]:
    """逐行入库（跳过 duplicate/skip），返回 {预览序号: 行id}（apply_import 用其实数
    回填 import_batches.count）。批内退款关联的负数 id 先记 pending、整批插完后
    统一回填（退款行常排在原消费之前——微信账单按时间倒序；回填在
    _backfill_batch_refunds）。回填按**预览序号**查映射，不能按已插列表位置索引：
    前方存在被跳过的行（手动配对/无效行/跨源重复）时两者错位，旧版会把退款链到
    错误的行（真机错链的根因）。
    1.0.2 起不再回写学习映射——手动改分类（user_changed）只对当笔负责，
    不把它泛化成该源分类的全局规则；要立规则去映射页手动加。"""
    inserted: dict[int, int] = {}
    pending_refund: list[tuple[int, int]] = []  # (退款预览序号, 目标预览序号)
    gate_confs: dict[int, str | None] = {}      # 批内退款行的预览置信（回填 link_refund 用）
    cats_ok, accs_ok, refund_ok = _ref_ok_sets(conn, ledger_id, previews)
    for pos, p in enumerate(previews):
        if not _is_new_row(p):
            continue                          # skip 含跨源重复/手动配对/无效行
        reason = _row_reject(p, cats_ok, accs_ok, refund_ok)
        if reason:
            # 四类按行跳过发生在预统计（apply_import 的 dates）之后：此前 count=预统计数
            # 与实际 INSERT 数不符（批次列表/撤销页笔数失真），由 apply_import 以返回值回填
            _log.warning(f"确认导入跳过（{reason}）：{str(p.get('name',''))[:20]}")
            continue
        rf = p.get("refund_for")
        is_pending = rf is not None and rf < 0
        if is_pending:
            pending_refund.append((pos, -rf - 1))
            rf = None
        # 退款落库闸（db.refund_link_gates 单点）：伪造体（type='expense'+refund_for 的
        # 双计行 / 伪造 full+high 免确认）在此拦下；status 一律按 DB 重算不信客户端。
        # 批内负序号行同样是退款行：type 闸对齐正链（伪造 expense+负序号此前绕过校验，
        # 回填后成"expense 带 refund_for"脏行并永久占用下游指向检查）
        if (rf is not None or is_pending) and p.get("type") != "transfer":
            _log.warning(f"确认导入跳过（退款链非法 type={p.get('type')!r} "
                         f"refund_for={p.get('refund_for')!r}）：{str(p.get('name',''))[:20]}")
            continue
        gated = dbm.refund_link_gates(conn, ledger_id, {**p, "refund_for": rf}) if rf is not None else None
        if rf is not None and gated is None:
            _log.warning(f"确认导入跳过（退款链非法 refund_for={rf!r}）："
                         f"{str(p.get('name',''))[:20]}")
            continue
        gate_status, gate_conf = "none", None
        if rf is not None:
            gate_status, gate_conf = gated[1], gated[2]
        elif is_pending:
            # 批内行：目标尚未落库，先按无链入库；confidence 沿预览生产者的值
            # （导出直连=high），回填 link_refund 时同一值写入
            gate_conf = p.get("refund_confidence")
            gate_confs[pos] = gate_conf
        # 文本列收口：直调体的 name/merchant/note/order_no/source 可给 dict/list——
        # 绑定非文本到 TEXT 列会 datatype mismatch 500（解析路径恒产字符串，仅伪造体可达）
        def _txt(v) -> str:
            return v if isinstance(v, str) else ""
        order_no = _txt(p.get("order_no"))
        cur = conn.execute(
            """INSERT INTO transactions(ledger_id, type, amount, batch_id, category_id,
               account_id, to_account_id, date, note, name, merchant, tags, receipt_path,
               source, ai_meta, order_no, refund_for, refund_status, refund_confidence,
               need_confirm, manual_cat, row_hash)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ledger_id, p["type"], p["amount"], batch_id, p.get("category_id"),
             p.get("account_id"), p.get("to_account_id"), p["date"], _txt(p.get("note")),
             _txt(p.get("name")), _txt(p.get("merchant")), dbm._j(p.get("tags")), None,
             _txt(p.get("source")), dbm._j(p.get("ai_meta")),
             order_no, rf, gate_status,
             gate_conf,   # NULL=非退款 / high / low / unmatched（来源仅取预览值，状态列已重算）
             1 if p.get("need_confirm") else 0,
             1 if p.get("user_changed") else 0,   # 预览里手动改过分类 → 修复不触碰
             p["row_hash"]))
        inserted[pos] = cur.lastrowid
    _backfill_batch_refunds(conn, inserted, pending_refund, gate_confs)
    return inserted

def _seed_attribution_roster(conn: sqlite3.Connection, ledger_id: int,
                             previews: list[dict]) -> None:
    """导入的归属名进归属名单（设置页「原始归属」表的数据源）：与组同名的跳过——
    同名路径已归组，名单不保留孤行（与 server._attr_group_absorb 同口径）。
    在 apply_import 事务内执行：入库与名单同一提交，无中途崩溃留半截的窗口。"""
    group_names = {r["name"] for r in conn.execute(
        "SELECT name FROM attribution_groups WHERE ledger_id=?", (ledger_id,))}
    for p in previews:
        # 直调体的 tags 可能不是 dict、attribution 可能不是 str（_insert_rows 的 _txt 同款类型闸）
        tags = p.get("tags")
        attr = tags.get("attribution") if isinstance(tags, dict) else None
        attr = attr.strip() if isinstance(attr, str) else ""
        if attr and attr not in group_names:
            conn.execute("INSERT OR IGNORE INTO attributions(ledger_id, name) VALUES(?,?)",
                         (ledger_id, attr))


def apply_import(conn: sqlite3.Connection, ledger_id: int, previews: list[dict],
                 filename: str, source: str) -> int:
    """确认导入（7.8 ⑤~⑦），单事务四阶段：增强回填 → 建批次 → 逐行入库+批内回填 →
    归属名单播种 + rescan。"""
    # 批次边界只按合法日期算：坏日期行（_insert_rows 会跳过）不参与 min/max，
    # 否则 None/dict 等篡改值在这里 TypeError → 整个 confirm 500
    dates = [p["date"] for p in previews if _is_new_row(p) and _ok_date(p)]
    with conn:
        _backfill_pay_dupes(conn, ledger_id, previews)
        cur = conn.execute(
            "INSERT INTO import_batches(ledger_id, source, filename, count, imported_at, date_from, date_to) "
            # 本地时：前端批次列表直接展示该列，此前 UTC 写入比真实时间早 8 小时（中国时区）
            "VALUES(?,?,?,?,datetime('now','localtime'),?,?)",
            (ledger_id, source, filename, len(dates), min(dates) if dates else "", max(dates) if dates else ""))
        batch_id = cur.lastrowid
        inserted = _insert_rows(conn, ledger_id, previews, batch_id)
        # 批次笔数以实际入库数回填：_insert_rows 在预统计之后还可能按行跳过
        # （坏值/缺 hash/引用非法），预统计会让批次列表/撤销页笔数虚高
        if len(inserted) != len(dates):
            conn.execute("UPDATE import_batches SET count=? WHERE id=?", (len(inserted), batch_id))
        _seed_attribution_roster(conn, ledger_id, previews)
        # 反向补关联：本批新 expense 可能是此前未匹配退款行的原消费（7.7 匹配时机②）
        _rescan_unmatched_refunds(conn, ledger_id)
    return batch_id

def _rescan_unmatched_refunds(conn: sqlite3.Connection, ledger_id: int) -> None:
    """反向补关联（7.7 匹配时机②）：本批新 expense 可能是此前未匹配退款行的原消费。
    匹配链复用 _match_refund 单源（fuzzy/heuristic 关闭）：订单号精确；名称前缀须
    等额+窗内（旧版无金额约束，30 元退款曾静默链上 300 元同名消费，名称层无命中即
    放弃，不退模糊/占位分支）；占位名/过短名走等额最近+统一 90 天窗。
    候选池=快照支出、日期倒序——多候选取最近（旧 SQL 分支无 ORDER BY，取哪条随机）。
    关联一律写低置信并保持 need_confirm——由待确认队列人工转正（resolve → _link_refund），不静默 UPDATE。
    只重扫仍在待确认队列（need_confirm=1）的行：用户已裁决（need_confirm=0、取消关联）的
    退款行不再被静默重链——那是用户意图，直接影响 net 统计。"""
    refunds = conn.execute(
        "SELECT id, name, amount, order_no, date FROM transactions "
        "WHERE ledger_id=? AND type='transfer' AND refund_for IS NULL "
        "AND COALESCE(need_confirm,0)=1 "
        # json_extract 而非 LIKE（模式串会硬编码 _j 序列化的「": "」空格，序列化器一改即静默匹配 0 行）；
        # db.jx 安全片段：脏 tags 行不再让整轮 rescan 500（与列表/统计同口径防御）
        f"AND {dbm.jx('tags', '$.transfer_sub')}='退款'", (ledger_id,)).fetchall()
    if not refunds:
        return
    # 候选池按待确认退款的日期窗从库直取（此前 = 全账本快照加载 + 排序，O(全账本) 随库
    # 增长，且本函数在写事务内：大库 + 深待确认队列时会把并发写顶到锁超时）。
    # 窗口 = [最早退款日 − 90 天，最晚退款日 +1 天) 的支出行，是各退款 90 天回看窗的
    # 超集（_match_refund 的 _in_window 仍逐条精校，超集不影响匹配结果）；
    # 排序 date DESC + id ASC 与旧快照池完全同序（多候选取最近语义不变）。
    # 配合 idx_tx_ledger_type_date 索引，成本从 O(全账本) 降为 O(窗内行)。
    # 边界只用合法日期参与算窗（脏日期退款本就无匹配可能，_in_window 必拒——
    # 不能让它把 date() 算成 NULL 毒化整个窗口、拖死同批合法退款）。
    dated = [r for r in refunds if dbm.is_valid_date((r["date"] or "")[:10])]
    if not dated:
        return
    dmin = min(r["date"][:10] for r in dated)
    dmax = max(r["date"][:10] for r in dated)
    pool = []
    for r in conn.execute(
            "SELECT id, name, merchant, amount, order_no, date, source, tags "
            "FROM transactions WHERE ledger_id=? AND type='expense' "
            "AND date >= date(?, '-90 day') AND date < date(?, '+1 day') "
            "ORDER BY date DESC, id ASC", (ledger_id, dmin, dmax)):
        # 池条目走 _pool_entry 契约形状（name/merchant/order_no 收口非空、bracket_refund
        # 从 tags 解析）——此前裸行直塞：NULL name 让 _name_hits 崩（confirm 500）、
        # 括号退款层在 rescan 里静默失效
        pool.append(_pool_entry(r["id"], r["name"], r["merchant"], r["amount"],
                                r["order_no"], r["date"], r["source"], _bracket_of(r)))
    for r in refunds:
        p = {"name": r["name"] or "", "amount": r["amount"], "order_no": r["order_no"] or "",
             "date": r["date"], "merchant": ""}
        m = _match_refund(pool, p, {}, fuzzy=False, heuristic=False)
        if not m:
            continue
        e = m[0]
        try:
            # 关联写入口单点（校验+状态重算+type 强转一体）：confidence='low'、
            # need_confirm 保持 1 由队列人工转正。极端脏态（该行已被其他退款指向）
            # 时 link_refund 拒绝——跳过该行，不中断整轮 rescan
            dbm.link_refund(conn, r["id"], e["id"], r["amount"], "low")
        except ValueError as ex:
            _log.warning(f"rescan 关联被拒(行 {r['id']}): {ex}")

def undo_batch(conn: sqlite3.Connection, batch_id: int) -> int:
    """整批撤销（7.8 ⑦）：删行 + 级联清理退款关联 + 删批次。
    （≤1.0.1 随批写入的学习映射清理已随 1.0.6 后移除：写入窗口只有 2026-10-18~20
    两天，残留 learned 行在映射页可见、修复区可重置，撤销不再连带清理。）"""
    with conn:
        n = conn.execute("SELECT COUNT(*) FROM transactions WHERE batch_id=?", (batch_id,)).fetchone()[0]
        ids = [r[0] for r in conn.execute("SELECT id FROM transactions WHERE batch_id=?", (batch_id,))]
        if ids:
            for part in _chunks(ids):
                # 撤销的行可能是某退款行的原消费：解链+回待确认（orphan 单点批量版，
                # 与 delete_tx 同款不变式）
                dbm.orphan_origin_refunds(conn, part)
            conn.execute("DELETE FROM transactions WHERE batch_id=?", (batch_id,))
        conn.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))
    return n

