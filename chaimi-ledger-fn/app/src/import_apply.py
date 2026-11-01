"""入库/撤销：批次事务（增强回填、批内退款回填、rescan）与整批撤销。"""
from __future__ import annotations

import sqlite3

import applog
import db as dbm

from import_common import _chunks, _is_placeholder_name, _is_new_row
from import_match import (_LedgerSnapshot, REFUND_WINDOW_DAYS, _match_order,
                          _strong_bases, _refund_name_candidates,
                          _name_hits, _in_window)

_log = applog.get("import")


def _ok_date(p: dict) -> bool:
    """confirm 提交行日期放行（防篡改/损坏；口径与 parse 期的 db.is_valid_date 一致）。"""
    return dbm.is_valid_date(str(p.get("date") or ""))

# ---------- 入库 ----------

def _backfill_pay_dupes(conn: sqlite3.Connection, previews: list[dict]) -> None:
    """态A 增强：跨源重复（pay_dupe）行虽跳过入库，但用平台账单的富信息回填已有流水——
    名称仅当已有值是占位/泛化（或等于商户名）时替换为订单标题；订单号仅当缺失时补充。"""
    for p in previews:
        if not p.get("pay_dupe") or not p.get("pay_dupe_of"):
            continue
        old = conn.execute("SELECT name, merchant, order_no FROM transactions WHERE id=?",
                           (p["pay_dupe_of"],)).fetchone()
        if not old:
            continue
        sets, args = [], []
        old_name = (old["name"] or "").strip()
        if p.get("name") and (not old_name or _is_placeholder_name(old_name)
                              or old_name == (old["merchant"] or "").strip()):
            sets.append("name=?"); args.append(p["name"][:40])
        if p.get("order_no") and not old["order_no"]:
            sets.append("order_no=?"); args.append(p["order_no"])
        if sets:
            conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id=?",
                         args + [p["pay_dupe_of"]])

def _insert_rows(conn: sqlite3.Connection, ledger_id: int, previews: list[dict],
                 batch_id: int) -> None:
    """逐行入库（跳过 duplicate/skip）。批内退款回填内聚于此（负序号 → 行id 映射）。
    批内退款关联的负数 id 先记 pending、整批插完后统一回填（退款行常排在原消费之前
    ——微信账单按时间倒序）。回填按**预览序号**查映射，不能按已插列表位置索引：
    前方存在被跳过的行（手动配对/无效行/跨源重复）时两者错位，旧版会把退款链到
    错误的行（真机错链的根因）。
    1.0.2 起不再回写学习映射——手动改分类（user_changed）只对当笔负责，
    不把它泛化成该源分类的全局规则；要立规则去映射页手动加。"""
    inserted: dict[int, int] = {}
    pending_refund: list[tuple[int, int]] = []  # (退款预览序号, 目标预览序号)
    for pos, p in enumerate(previews):
        if not _is_new_row(p):
            continue                          # skip 含跨源重复/手动配对/无效行
        if not _ok_date(p):
            # 坏日期行不入库——原样入库会静默掉出所有区间统计、还会让 import_batches 边界脏
            _log.warning(f"确认导入跳过坏日期行：{p.get('date')!r} {str(p.get('name',''))[:20]}")
            continue
        rf = p.get("refund_for")
        if rf is not None and rf < 0:
            pending_refund.append((pos, -rf - 1))
            rf = None
        cur = conn.execute(
            """INSERT INTO transactions(ledger_id, type, amount, batch_id, category_id,
               account_id, to_account_id, date, note, name, merchant, tags, receipt_path,
               source, ai_meta, order_no, refund_for, refund_status, refund_confidence,
               need_confirm, manual_cat, row_hash)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ledger_id, p["type"], p["amount"], batch_id, p.get("category_id"),
             p.get("account_id"), p.get("to_account_id"), p["date"], p.get("note", ""),
             p.get("name", ""), p.get("merchant", ""), dbm._j(p.get("tags")), None,
             p.get("source"), dbm._j(p.get("ai_meta")),
             p.get("order_no", ""), rf, p.get("refund_status", "none"),
             p.get("refund_confidence"),  # NULL=非退款 / high / low / unmatched
             1 if p.get("need_confirm") else 0,
             1 if p.get("user_changed") else 0,   # 预览里手动改过分类 → 修复不触碰
             p["row_hash"]))
        inserted[pos] = cur.lastrowid
    # 回填批内退款关联（顺序无关）：预览序号 → 真实行 id。
    # 目标不在本批（被跳过/重复行）则保持 NULL，交给反向补关联（7.7 匹配时机②）
    for row_pos, tgt_pos in pending_refund:
        if tgt_pos in inserted:
            conn.execute("UPDATE transactions SET refund_for=? WHERE id=?",
                         (inserted[tgt_pos], inserted[row_pos]))

def apply_import(conn: sqlite3.Connection, ledger_id: int, previews: list[dict],
                 filename: str, source: str) -> int:
    """确认导入（7.8 ⑤~⑦），单事务四阶段：增强回填 → 建批次 → 逐行入库+批内回填 → rescan。"""
    # 批次边界只按合法日期算：坏日期行（_insert_rows 会跳过）不参与 min/max，
    # 否则 None/dict 等篡改值在这里 TypeError → 整个 confirm 500
    dates = [p["date"] for p in previews if _is_new_row(p) and _ok_date(p)]
    with conn:
        _backfill_pay_dupes(conn, previews)
        cur = conn.execute(
            "INSERT INTO import_batches(ledger_id, source, filename, count, imported_at, date_from, date_to) "
            "VALUES(?,?,?,?,datetime('now'),?,?)",
            (ledger_id, source, filename, len(dates), min(dates) if dates else "", max(dates) if dates else ""))
        batch_id = cur.lastrowid
        _insert_rows(conn, ledger_id, previews, batch_id)
        # 反向补关联：本批新 expense 可能是此前未匹配退款行的原消费（7.7 匹配时机②）
        _rescan_unmatched_refunds(conn, ledger_id)
    return batch_id

def _rescan_unmatched_refunds(conn: sqlite3.Connection, ledger_id: int) -> None:
    """反向补关联（7.7 匹配时机②）：本批新 expense 可能是此前未匹配退款行的原消费。
    口径与主匹配同源（_refund_name_candidates / _name_hits / REFUND_WINDOW_DAYS）：订单号精确；
    名称前缀须等额+窗内（旧版无金额约束，30 元退款曾静默链上 300 元同名消费，名称层
    无命中即放弃，不退占位分支）；占位名/过短名走等额+统一 90 天窗。
    候选池=快照支出、日期倒序——多候选取最近（旧 SQL 分支无 ORDER BY，取哪条随机）。
    关联一律写低置信并保持 need_confirm——由待确认队列人工转正（resolve → _link_refund），不静默 UPDATE。
    只重扫仍在待确认队列（need_confirm=1）的行：用户已裁决（need_confirm=0、取消关联）的
    退款行不再被静默重链——那是用户意图，直接影响 net 统计。"""
    refunds = conn.execute(
        "SELECT id, name, amount, order_no, date FROM transactions "
        "WHERE ledger_id=? AND type='transfer' AND refund_for IS NULL "
        "AND COALESCE(need_confirm,0)=1 "
        # json_extract 而非 LIKE（模式串会硬编码 _j 序列化的「": "」空格，序列化器一改即静默匹配 0 行）；
        # 前提 tags 必为 NULL 或合法 JSON（db._j 落库保证，与 server 的 json_set/json_remove 同前提）
        "AND json_extract(tags,'$.transfer_sub')='退款'", (ledger_id,)).fetchall()
    if not refunds:
        return
    pool = sorted((r for r in _LedgerSnapshot(conn, ledger_id).rows if r["type"] == "expense"),
                  key=lambda e: e["date"] or "", reverse=True)   # 与 _refund_pool 同序
    for r in refunds:
        p = {"name": r["name"] or "", "amount": r["amount"], "order_no": r["order_no"] or "",
             "date": r["date"], "merchant": ""}
        near = _in_window(pool, p["date"], REFUND_WINDOW_DAYS)
        bases = _refund_name_candidates(p["name"])
        hit = None
        if p["order_no"]:
            m = _match_order(near, p)               # 同主匹配口径：等额优先/唯一/歧义让位
            hit = m[0] if m else None
        if hit is None:
            hits = _name_hits(near, p, bases)
            if hits:
                hit = hits[0]                       # 名称层无命中即放弃，不退占位分支
            elif not _strong_bases(bases):
                eq = [e for e in near if e["amount"] == p["amount"]]   # 占位名/过短名：等额最近
                hit = eq[0] if eq else None
        if hit:
            status = dbm.refund_status(hit["amount"], p["amount"])
            conn.execute("UPDATE transactions SET refund_for=?, refund_status=?, refund_confidence='low' "
                         "WHERE id=?", (hit["id"], status, r["id"]))

def undo_batch(conn: sqlite3.Connection, batch_id: int) -> int:
    """整批撤销（7.8 ⑦）：删行 + 级联清理退款关联/learned 映射 + 删批次。"""
    with conn:
        n = conn.execute("SELECT COUNT(*) FROM transactions WHERE batch_id=?", (batch_id,)).fetchone()[0]
        ids = [r[0] for r in conn.execute("SELECT id FROM transactions WHERE batch_id=?", (batch_id,))]
        if ids:
            for part in _chunks(ids):
                q = ",".join("?" * len(part))
                # 字段清理走 UNLINK_REFUND_SET 单源；undo 额外置 need_confirm=1（撤销的退款行回待确认）
                conn.execute(f"UPDATE transactions SET {dbm.UNLINK_REFUND_SET}, need_confirm=1 "
                             f"WHERE refund_for IN ({q})", part)
            conn.execute("DELETE FROM transactions WHERE batch_id=?", (batch_id,))
        # legacy 兼容（≤1.0.1 旧库遗留的 learned 行）：1.0.2 起导入不再回写学习映射，
        # 新库此句删 0 行，仅为撤销旧批次时清掉当年随批写入的学习映射
        conn.execute("DELETE FROM category_mappings WHERE learned=1 AND batch_id=?", (batch_id,))
        conn.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))
    return n

