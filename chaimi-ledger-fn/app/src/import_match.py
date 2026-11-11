"""账本快照 + 退款匹配链（统一 90 天回看窗）+ 跨源/手动配对。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import applog
import db as dbm   # refund_status 单源判定
import platform_templates as pt   # 跨源配对的平台渠道/显示名单源（PLATFORMS 派生）
from import_common import _is_placeholder_name, _is_new_row

_log = applog.get("import")   # 退款匹配决策日志：日志包里可直接看出每笔退款链到谁/置信度

class _LedgerSnapshot:
    """账本流水快照：预览期一次加载，精确去重/退款候选池/疑似重复提示/跨源配对四段共享同一份数据
    （消除重复全表扫描与跨源配对的逐行 N+1 查询）。行序固定 id 升序——等价旧 SQL 默认扫描序，
    多候选歧义时取哪条由"碰巧"变为确定（取最早一条）。"""

    _FIELDS = ("id", "type", "amount", "date", "name", "merchant", "order_no", "source",
               "row_hash", "tags")   # tags 供退款候选池解析括号退款额（bracket_refund）

    def __init__(self, conn: sqlite3.Connection, ledger_id: int):
        self.rows = [dict(r) for r in conn.execute(
            f"SELECT {', '.join(self._FIELDS)} FROM transactions WHERE ledger_id=? ORDER BY id",
            (ledger_id,))]
        # 跨源配对索引：(类型, 金额) → 行列表（id 升序）
        self.by_type_amount: dict = {}
        for r in self.rows:
            self.by_type_amount.setdefault((r["type"], r["amount"]), []).append(r)
        # 精确去重索引：(source, row_hash) → id；同键多行取后写者（同旧 IN 查询的覆盖序）。
        # 史前 NULL-source 行的 row_hash 与现行模板哈希算法不同、天然不可比：其重导
        # 判重由 dup_hints 软提示（同日同额）兜底，属设计行为（db.dup_hints 注释）
        self.by_hash = {(r["source"], r["row_hash"]): r["id"]
                        for r in self.rows if r["row_hash"]}

    def candidates(self, p: dict, sources, d_from: str, d_to: str) -> list:
        """跨源配对候选：(类型, 金额) 索引命中后滤来源与当日±1 窗口（等价旧 BETWEEN，id 升序）。"""
        return [r for r in self.by_type_amount.get((p["type"], p["amount"]), ())
                if r["source"] in sources and d_from <= (r["date"] or "")[:10] <= d_to]

def _fuzzy_ratio(bases: list, name: str) -> float:
    """模糊兜底相似度（京东改写标题容错，7.7 ② v1.2）：最长公共前缀 / 较短串长
    （各取前 60 字）；公共前缀 <2 字记 0——「京斯坦…」对 2 字泛化名「京东」的 1 字
    前缀比值恰踩 0.5 线，单字证据不算（与 _MIN_NAME_LEN 同口径）。"""
    name = (name or "")[:60]
    best = 0.0
    for b in bases:
        b = b[:60]
        i = 0
        while i < min(len(b), len(name)) and b[i] == name[i]:
            i += 1
        if i >= _MIN_NAME_LEN:
            best = max(best, i / max(min(len(b), len(name)), 1))
    return best

# 退款匹配统一回看窗（天）：_match_refund / rescan 入口一次收窄（日期 ≤ 退款日 且窗内），
# 窗外不猜——未命中的退款进待确认队列人工指定原消费。高置信（免确认入库）三门槛：
# 候选唯一 + 窗口内 + 前缀≥2 字；任一不满足降 low，宁低勿错。
REFUND_WINDOW_DAYS = 90
_MIN_NAME_LEN = 2

def _day_window(date_str: str, days: int) -> tuple[str, str]:
    """目标日的 ±days 天日期窗（跨源/手动配对用）；解析失败给全域（等效不限窗）。"""
    try:
        d = datetime.strptime((date_str or "")[:10], "%Y-%m-%d")
        return ((d - timedelta(days=days)).strftime("%Y-%m-%d"),
                (d + timedelta(days=days)).strftime("%Y-%m-%d"))
    except ValueError:
        return "0000-00-00", "9999-12-31"

# P2 跨源配对：平台显示名/渠道集合单源在 platform_templates.PLATFORMS（派生视图）
_PLATFORM_DISPLAY = pt.NAME_MATCH_PLATFORMS
_PAYMENT_CHANNELS = pt.PAYMENT_CHANNELS
# 手动记账来源：UI 记一笔（app）/ 机器人确认入库（bot；manual 为历史/测试写入口径）。
# 手动行无 row_hash，与平台账单的重复（先手记、后导微信）由 _link_payment 末层配对兜住
_MANUAL_SOURCES = ("app", "bot", "manual")


def _sole(cands: list):
    """唯一候选才返回（空/多候选=歧义返回 None）——退款匹配与跨源配对各层的共同安全
    门槛：多笔等额=证据不足以定行，不自动链/跳，留待确认队列人工裁决。"""
    return cands[0] if len(cands) == 1 else None

def _link_payment(conn: sqlite3.Connection, ledger_id: int, previews: list,
                  snap: _LedgerSnapshot | None = None) -> None:
    """P2 跨源配对（双向）：同一笔消费在平台账单与支付渠道账单各出现一次 → 后导入的标 pay_dupe 跳过。
    只认身份证据：正向（导平台 → 查渠道）① 订单号 ② 商户/名称含平台名；反向（导渠道 → 查平台）
    渠道行自带平台名 + 同窗同额唯一候选。曾有 ③ 层「同日同额唯一候选即跳」与反向纯金额层——
    无任何文本证据，同日两笔无关同额消费（手记午餐 ¥35 + 账单星巴克 ¥35）会被默认吞行，
    与 dup_hints「同日同额只提示不拦截」的全局原则矛盾，已删——该场景由 db.dup_hints 的
    「疑似重复」软提示接管（预览可见、一键跳过），不再静默吞。
    末层（任意导入来源 → 查手动记账）：同日±1 + 金额 + 类型 + 唯一候选 → 标「手动记账」跳过——
    先手记后导账单的双计由此兜住（手动行名称与账单名常对不上，金额+日期是仅有可用证据，
    唯一候选 + 1:1 消耗两道防线防误吞，是有意的权衡）；命中行预览默认跳过、点行可恢复
    （=用户确认），增强只补单号不碰手填名称。duplicate 行也过（旧数据重导时增强仍需执行）。
    匹配在账本快照上进行：①② 取首个命中 = id 升序最早一条（旧 SQL 无 ORDER BY 的默认扫描序，现为保证）。"""
    def mark(p, r, via):
        # 末层 via="手动记账" 的理由与跨源层区分：预览 skip 文案直读，混用会误导排查
        p.update(pay_dupe=True, pay_dupe_of=r["id"], pay_dupe_via=via,
                 skip="与手动记账重复" if via == "手动记账" else "跨源重复")

    snap = snap if snap is not None else _LedgerSnapshot(conn, ledger_id)   # 懒加载：build_previews 传共享快照
    used_manual: set = set()   # 已被配对消耗的手动行 id（1:1 防一笔手记吸收多笔同额导入）
    used_cross: set = set()    # 已被跨源配对消耗的库行 id（同款 1:1：一笔渠道流水只吸收
                               # 一笔平台流水，多笔同日同额真实消费不再被整批吞掉）
    for p in previews:
        if p.get("skip") or p.get("pay_dupe") or p.get("type") not in ("expense", "income"):
            continue
        src = p.get("source") or p.get("platform") or ""
        if not (p.get("date") or "")[:10]:
            continue
        d_from, d_to = _day_window(p["date"], 1)

        if src in _PLATFORM_DISPLAY:
            # 正向：导平台账单，查库中支付渠道流水（订单号 → 商户名，身份证据才跳）；
            # 候选剔除已消耗行、命中即消耗（1:1，与末层手动配对同款防线）
            display = _PLATFORM_DISPLAY[src]
            base = [c for c in snap.candidates(p, _PAYMENT_CHANNELS, d_from, d_to)
                    if c["id"] not in used_cross]
            if p.get("order_no"):
                r = next((c for c in base if c["order_no"] == p["order_no"]), None)
                if r:
                    mark(p, r, "订单号"); used_cross.add(r["id"]); continue
            r = next((c for c in base if display in (c["merchant"] or "")
                      or display in (c["name"] or "")), None)
            if r:
                mark(p, r, "商户名"); used_cross.add(r["id"]); continue

        elif src in _PAYMENT_CHANNELS and any(
                d in (p.get("merchant") or "") or d in (p.get("name") or "")
                for d in _PLATFORM_DISPLAY.values()):
            # 反向（导支付渠道 → 查平台）：仅身份证据——渠道行自身带平台名（微信账单的
            # 「美团平台」）且同窗同额唯一候选才跳，与正向②同类证据。曾有纯金额版
            # （同日同额唯一即跳）：库中一笔无关美团 ¥35 会吞掉手记午餐 ¥35，已收窄。
            cands = [c for c in snap.candidates(p, tuple(_PLATFORM_DISPLAY), d_from, d_to)
                     if c["id"] not in used_cross]
            r = _sole(cands)
            if r:
                mark(p, r, "商户名（反向）"); used_cross.add(r["id"])

        if not p.get("pay_dupe"):
            # 末层：查手动记账（app/bot）。手动行时间金额是人填的，同额碰撞概率高于渠道
            # 账单——两条防线防误吞：① 唯一候选门槛（3 个停车场 3 笔 5 元的库内歧义 →
            # 不跳，留 dup_hint 人工裁决）；② 1:1 消耗（一笔手动行最多吸收一笔导入行：
            # 手记 1 笔 + 账单 3 笔同额时只跳第一笔，余下正常入库）。来源为 app 的行也走
            # 这里：导出回环里手动行的重导靠这层跳过（手动行无 row_hash，精确去重够不着）。
            cands = [c for c in snap.candidates(p, _MANUAL_SOURCES, d_from, d_to)
                     if c["id"] not in used_manual]
            r = _sole(cands)
            if r:
                mark(p, r, "手动记账")
                used_manual.add(r["id"])

_REFUND_PREFIXES = ("退款-", "退款·", "【退款】", "退款:")

def _refund_name_candidates(name: str) -> list:
    """退款行名称候选：原名 + 剥退款前缀的变体，去重保序、滤占位名（主匹配与 rescan 共用单源）。"""
    out = [name] + [name[len(pref):] for pref in _REFUND_PREFIXES
                    if name.startswith(pref) and len(name) > len(pref)]
    return [c for c in dict.fromkeys(c for c in out if c) if not _is_placeholder_name(c)]

def _pool_entry(pid, name, merchant, amount, order_no, date, source,
                bracket_refund=None) -> dict:
    """退款候选池条目（库行与批内预览共用形状；负 id = -(预览序号+1)）。
    bracket_refund：原消费行括号标注的退款额（京东新导出，②'' 层匹配用）。
    _d 为日期到天的预解析（条目在池的整个匹配期不变，此前 _in_window 每条退款
    都对全池重新 strptime 一遍，千条退款×万级池就是千万次解析）；坏日期为 None。"""
    entry = {"id": pid, "name": name or "", "merchant": merchant or "",
             "amount": amount, "order_no": order_no or "", "date": date, "source": source}
    try:
        entry["_d"] = datetime.strptime((date or "")[:10], "%Y-%m-%d")
    except ValueError:
        entry["_d"] = None
    if bracket_refund is not None:
        entry["bracket_refund"] = bracket_refund
    return entry

def _bracket_of(row) -> int | None:
    """从库行 tags（JSON 文本）解析括号退款额；无/坏 JSON 返回 None。"""
    try:
        v = (json.loads(row["tags"] or "{}") or {}).get("bracket_refund")
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None

def _refund_pool(conn: sqlite3.Connection, ledger_id: int, previews: list,
                 snap: _LedgerSnapshot | None = None) -> list:
    """退款候选池：库中支出 + 本批支出预览（退款与原消费常在同一账单里；负 id=-(预览序号+1)）。
    日期倒序固定遍历顺序——旧版无排序，多候选时取哪条取决于数据库返回序（等于随机）。"""
    snap = snap if snap is not None else _LedgerSnapshot(conn, ledger_id)   # 懒加载：build_previews 传共享快照
    out = [_pool_entry(r["id"], r["name"], r["merchant"], r["amount"],
                       r["order_no"], r["date"], r["source"], _bracket_of(r))
           for r in snap.rows if r["type"] == "expense"]
    for i, e in enumerate(previews):
        if e.get("type") == "expense" and _is_new_row(e):
            out.append(_pool_entry(-(i + 1), e.get("name"), e.get("merchant"),
                                   e["amount"], e.get("order_no"), e["date"], e.get("source"),
                                   e.get("bracket_refund")))
    out.sort(key=lambda e: e["date"] or "", reverse=True)
    return out

def _match_order(pool: list, p: dict):
    """① 订单号精确 → high（pool 已收窗，隔季旧单不参与）；同单号多行（平台退款行
    带原单号/商家单号拆单共用）时优先等额那行；等额也歧义则让位后续层（②/③）裁决。
    同单号唯一时不校验金额——同订单部分退款（100 退 35）是合法场景。"""
    if not p.get("order_no"):
        return None
    hits = [e for e in pool if e["order_no"] and e["order_no"] == p["order_no"]]
    if not hits:
        return None
    r = _sole([e for e in hits if e["amount"] == p["amount"]])
    if r:
        return r, False
    r = _sole(hits)
    if r:
        return r, False
    return None   # 同单号多行且无等额：单号证据不足以定行，交给金额证据

def _in_window(pool: list, date: str, days: int) -> list:
    """池中落在退款日 [0, days] 天窗内的候选（0=当日；日期晚于退款日或解析失败 → 排除）。
    各匹配层共用：_match_refund / rescan 入口一次收窄，层内不再重复过滤。
    日期按天比较（_pool_entry 的 _d 预解析，口径同 [:10]）：坏日期条目（_d=None，
    norm_date 收紧前入库的行）排除——但 10 位历史日期行在入池时已按天解析，不会
    被静默排除出匹配（旧版只认 19 位，这些行的退款永远配不上原消费）。"""
    try:
        ref = datetime.strptime((date or "")[:10], "%Y-%m-%d")
    except ValueError:
        return []
    return [e for e in pool
            if e["_d"] is not None and 0 <= (ref - e["_d"]).days <= days]

def _pick_recent(hits: list):
    """命中挑选（「宁低勿错」统一收口）：唯一 → (候选, 高置信)；
    多命中 → (池序首个=最近, 低置信) 人工裁决；空 → None。"""
    if not hits:
        return None
    return hits[0], len(hits) > 1

def _strong_bases(bases: list) -> list:
    """有效名称候选（≥2 字）：单字前缀的等额碰撞过常见，不作高置信证据（主匹配与 rescan 共用）。"""
    return [b for b in bases if len(b) >= _MIN_NAME_LEN]

def _name_hits(pool: list, p: dict, bases: list) -> list:
    """② 的命中集：等额 + 名称前缀（前缀≥2 字）；pool 为已收窗候选。
    主匹配据此判 high/low（_pick_recent），rescan 取池序首个（最近）——单一口径。"""
    strong = _strong_bases(bases)
    if not strong:
        return []
    return [e for e in pool if e["amount"] == p["amount"]
            and any(e["name"].startswith(b) for b in strong)]

def _match_name(pool: list, p: dict, bases: list, fuzzy: bool = True):
    """② 金额+名称：等额+前缀唯一（前缀≥2 字）→ high；多命中取最近降 low（优先于
    模糊兜底）；无等额命中 → 模糊最长公共前缀比 ≥0.5（京东改写标题容错，前缀≥2 字）
    → low，同额候选优先于高相似度（真机案例：9.9 退款与同店 12.9/9.9 两笔模糊打平，
    误链时间更晚的 12.9）。high 三门槛缺一降级——宁低勿错。
    fuzzy=False 关闭模糊层（rescan 补关联口径：模糊命中不参与，宁缺勿滥）。"""
    pick = _pick_recent(_name_hits(pool, p, bases))
    if pick:
        return pick                      # 唯一命中 high / 多命中取最近降 low
    if not fuzzy:
        return None
    best, best_key = None, None
    for e in pool:                       # pool 已收窗：模糊只在窗内找相似
        if e["amount"] < p["amount"]:
            continue
        r = _fuzzy_ratio(bases, e["name"])
        if r < 0.5:
            continue
        key = (e["amount"] == p["amount"], r)   # 同额 > 相似度 > 池序（倒序=最近）
        if best is None or key > best_key:
            best, best_key = e, key
    return (best, True) if best else None

def _match_wechat(pool: list, p: dict, flow: dict):
    """②' 微信（pool 已收窗）：商户+金额唯一 → high（多命中取最近降 low）；红包/转账
    无名称信息 → 最近等额 → low。同额多候选时优先交易对方相同的那笔（退款行带交易
    对方：退给谁=原转账给谁，真机案例：35 转账退款被链到最近一笔别人的 35 转账）。"""
    rows = [e for e in pool if e["amount"] == p["amount"]
            and e["source"] not in _MANUAL_SOURCES]   # 手动行（app/bot/manual）不进微信专属层
    if flow.get("wechat_type") in ("微信红包", "转账"):
        if not rows:
            return None
        same = [e for e in rows if p.get("merchant") and e["merchant"] == p["merchant"]]
        return (same or rows)[0], True
    return _pick_recent([e for e in rows if e["merchant"] and e["merchant"] == p["merchant"]])

def _match_bracket(pool: list, p: dict):
    """②'' 括号退款额（京东新导出）：退款额 == 原消费行括号标注的退款额
    （(已退款26.72)；(已全额退款)=全额）——子件部分退款单号/名称都对不上时的唯一线索。
    金额单证据：唯一命中也只给 low（进待确认）；同额歧义放弃。"""
    hits = [e for e in pool if e.get("bracket_refund") == p["amount"]]
    r = _sole(hits)
    return (r, True) if r else None

def _match_heuristic(pool: list, p: dict):
    """③ 启发式：金额 ≥ 退款额 + 唯一候选 → low；多候选但恰有一笔同额 → 优先同额（low，
    仍进待确认）。同额也歧义（多笔等额）则放弃——宁低勿错。
    第二个 _sole 非冗余：首行让位仅因「≥ 的候选多笔」，等额子集仍可能恰好唯一
    （如 100/35 两笔 ≥ 35 时取唯一的 35），删掉会把这些可裁决行误抛给人工。"""
    r = _sole([e for e in pool if e["amount"] >= p["amount"]])
    if r:
        return r, True
    r = _sole([e for e in pool if e["amount"] == p["amount"]])
    return (r, True) if r else None

def _match_refund(pool_all: list, p: dict, flow: dict, *, fuzzy: bool = True,
                  heuristic: bool = True):
    """单条退款行的匹配链：候选一次收窄到统一 90 天窗（含日期 ≤ 退款日），再走
    ①订单号 → ②名称 → ②'微信专属 → ②''括号 → ③启发式。返回 (候选, 低置信) 或 None。
    fuzzy/heuristic 供 rescan 补关联收窄（两层证据太弱，命中也仅低置信进待确认）：
    关闭时名称层不做模糊前缀、不做「金额≥退款额」启发式；占位名/过短名回落
    等额最近（原 rescan 内联实现的口径，收口到这条链防两处漂移）。"""
    pool = _in_window(pool_all, p["date"], REFUND_WINDOW_DAYS)
    bases = _refund_name_candidates(p["name"])
    m = (_match_order(pool, p)
         or _match_name(pool, p, bases, fuzzy=fuzzy)
         or (_match_wechat(pool, p, flow) if flow.get("platform") == "wechat" else None)
         or _match_bracket(pool, p))
    if m:
        return m
    if heuristic:
        return _match_heuristic(pool, p)
    if not _strong_bases(bases):
        eq = [e for e in pool if e["amount"] == p["amount"]]
        return (eq[0], True) if eq else None
    return None

def _match_refunds(conn: sqlite3.Connection, ledger_id: int, previews: list[dict],
                   flows: list, snap: _LedgerSnapshot | None = None) -> None:
    """退款分层匹配（组装）：逐条走 _match_refund；低置信/未命中 → need_confirm 人工裁决。"""
    pool_all = _refund_pool(conn, ledger_id, previews, snap)
    for p, f in zip(previews, flows):
        if "skip" in p or not f.get("is_refund"):
            continue
        cand = _match_refund(pool_all, p, f)
        if cand:
            e, low_conf = cand
            p["refund_for"], p["refund_status"] = e["id"], dbm.refund_status(e["amount"], p["amount"])
            p["refund_confidence"] = "low" if low_conf else "high"
            _log.info(f"退款匹配 [{p.get('platform')}] {p.get('name')} {p['amount']}分 → "
                      f"#{e['id']} {e['name']} {e['amount']}分（{p['refund_status']}/{p['refund_confidence']}）")
            if low_conf:
                p["need_confirm"] = True
        else:
            # 未匹配到原消费的退款（如被退回的红包）：进待确认队列，由用户核对
            p["need_confirm"] = True
            p["refund_confidence"] = "unmatched"
            _log.info(f"退款匹配 [{p.get('platform')}] {p.get('name')} {p['amount']}分 → 无候选（待确认队列）")

