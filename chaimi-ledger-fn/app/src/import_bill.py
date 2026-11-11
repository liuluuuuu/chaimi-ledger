"""账单导入：统一模板引擎 + 三段式分类 + 退款匹配 + 去重（DESIGN 七/八章）。

链路：parse_bill(文件) → FlowPreview[]（不写库）→ apply_import(确认)。
金额全程整数分。

解析层三层结构（TEMPLATE_MIGRATION_PLAN.md）：
  路由层  parse_bill：xlsx→行/CSV 解码 → 依次试模板（用户 → 内置）→ 表头集合命中即解析
  引擎层  parse_template：解释模板声明，逻辑全部委托原语
  原语层  decide_type / match_refund（规则求值在引擎内联）/ split_bracket_amount /
          resolve_name / link_batch_refunds 等（本文件）
  数据层  platform_templates.py（内置五平台，随应用发版）+ chaimi.conf import_templates（用户）
内置模板与用户模板同一条执行路径；复制内置模板改列名存为自定义即接管路由（覆盖机制）。
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3

import config
import db as dbm
import platform_templates as pt
from import_common import (_is_new_row, _is_placeholder_name, is_meaningless_name,
                           amount_clean, norm_account_name, normalize, yuan_to_cents)
from import_classify import _top_by_keyword
from import_classify import ClassifyCtx, classify_transfer, classify_flow
from import_match import _LedgerSnapshot, _match_refunds, _link_payment
from import_apply import apply_import, undo_batch, _rescan_unmatched_refunds
# 本文件是导入子系统的稳定门面：server/ai/tests 统一经 ib.xxx 访问上面再导出的
# 各子模块函数（归属：import_common=名称/金额原语，import_classify=词典，
# import_apply=入库/撤销/重扫）。改动名字前先 grep ib.调用方，勿静默改名。

# ---------- 基础工具 ----------

#   京东金额列括号（全/半角都认；半角正则会因全角行 float() 抛错）：
#   ① 含「退款」关键词 → 原消费一条入列 + 退款额存 bracket_refund 作退款匹配线索
#      （不拆退款行，见 split_bracket_amount 与京东模板注释）
#   ② 其他括号内容（已退积分/红包/入账/补偿等）→ 单条 + need_confirm
#   ③ 无括号 → 原样
_REFUND_PAREN_RE = re.compile(r"[（(]([^（）()]*退款[^（）()]*)[)）]")
_PAREN_RE = re.compile(r"[（(]([^（）()]*)[)）]")

def _amount_str(s) -> str:
    """金额单元格清洗（parse_cents 用）：清洗本体在 import_common.amount_clean 单源
    （NFKC 全角归一 + 去币符/千分位），此处只叠加单元格 trim。"""
    return amount_clean(normalize(s))

def parse_cents(s) -> int | None:
    """金额→分，保符号；解析失败返回 None。0 是合法值，与「解析不了」语义分离——
    零额行走各模板的 skip_zero 语义、坏值行走统一的跳行提示，互不冒充。
    负号是否冗余由调用方决定（lenient/expand 取 abs，导出回环保号）。
    换算本体在 yuan_to_cents 单点收口（`or "0"` 保持空单元格→零额行的旧口径）。"""
    return yuan_to_cents(s or "0")

def parse_dt(s: str) -> str:
    """日期归一：db.iso_date 宽松写法（2026年1月1日 / 2026.1.1 / 全角数字）→ ISO，
    再经 db.norm_date 补全为 19 位（纯日期按零点补全——账单无时刻的约定，与
    手工路径的「沿用旧时刻/当前时刻」不同）。认不出的原样返回——合法性由调用方
    （parse_template）以 db.is_valid_date 把关：非 ISO 串入库会破坏 date>=?
    AND date<? 的字典序区间比较。"""
    s = dbm.iso_date(normalize(s))
    try:
        return dbm.norm_date(s, old="1970-01-01 00:00:00")   # old 仅为给纯日期提供零点时刻基准
    except ValueError:
        return s

def _row_hash(fields: list[str]) -> str:
    return hashlib.md5("|".join(normalize(f) for f in fields).encode("utf-8")).hexdigest()

# 解析期行数熔断上限（防高压缩比 xlsx / 超长 CSV 的解析期内存放大）；
# server.import_parse 的 len(flows) 校验引用同一常量作权威上限。
# （IN (...) 的分块上限是另一码事：SQLite 单语句绑定变量有编译期上限，见 import_common._chunks）
MAX_PARSE_ROWS = 50_000

def _capped(rows):
    """行迭代熔断：超限抛 ValueError（import_parse 统一转 400）。
    余量 200 行给各平台表头前的导出信息块。"""
    for i, row in enumerate(rows):
        if i > MAX_PARSE_ROWS + 200:
            raise ValueError(f"行数超过上限 {MAX_PARSE_ROWS // 10000} 万行")
        yield row

def _csv_rows(text: str) -> list:
    """CSV 文本 → 行列表（带熔断）：parse_generic / parse_bill / sniff_template 共用。"""
    return list(_capped(csv.reader(io.StringIO(text))))

# ---------- 原语层（引擎与测试共用；每个原语在抽取时即有调用方，不预建） ----------

# 缺省收/支枚举；模板可传自己的 flow_map 覆盖（京东仅支出计收支）
DEFAULT_FLOW_MAP = {"支出": "expense", "收入": "income"}

# col 的标准键：引擎按各自语义装配；其余键（导出的 origin_source/export_id/export_ref 等）
# 原样拷入 RawFlow
_STANDARD_COL_KEYS = {"date", "type", "name", "flow", "pay", "amount",
                      "order", "merchant", "note", "to_account_name"}

def decide_type(flow_val: str, type_val: str, is_refund: bool,
                transfer_types=(), flow_map: dict | None = None) -> str:
    """类型决策：退款恒 transfer → 类型列命中 transfer 集合 transfer（不计收支）
    → 收/支枚举（未命中/缺失一律 transfer）。"""
    if is_refund:
        return "transfer"
    if type_val and transfer_types and type_val in transfer_types:
        return "transfer"
    return (DEFAULT_FLOW_MAP if flow_map is None else flow_map).get(flow_val, "transfer")

def resolve_name(values, cfg: dict) -> str:
    """名称回退链：依次取列值，首个有意义者胜出；全无意义返回 ""（AI 兜底）。
    微信用法：商品列无意义时用交易对方顶替，对方也是泛化值则留空。"""
    generic = cfg.get("generic_set") or frozenset()
    for v in values:
        n = normalize(v)
        if not _is_placeholder_name(n) and n not in generic:
            return n
    return ""

def _join_note(*parts) -> str:
    """备注拼接：商户/对方 + 备注列，非空部分空格连接。"""
    return " ".join(p for p in (normalize(x) for x in parts) if p)

def split_bracket_amount(raw: str):
    """金额列括号四态（京东）：
    ("split", 原额分, 退款分)   括号含「退款」：括号内最后一个数字为退款额，无数字则全额；
    ("confirm", 原额分)         其他括号（已退积分/红包等）→ 单条 + need_confirm；
    ("plain", 金额分)           无括号原样；
    ("skip", 原因)              剥掉括号后金额本体仍解析不了（"N/A"/"abc"）→ 跳行，
                                与 strict 模式同口径——一格脏值只损失本行，不再炸整张账单。
    负号一律取绝对值（收/支列才是方向权威，lenient 同口径）；全角数字/逗号经 _amount_str
    的 NFKC 归一后取数。"""
    m = _REFUND_PAREN_RE.search(raw)
    if m:
        inner = m.group(1)
        origin = _REFUND_PAREN_RE.sub("", raw)
        # 退款括号之外还可能并存其他括号（「100(已退积分5.00)(已退款100)」＝现金退款+积分退回
        # 各一个括号,两类都是真实导出形态）：原额取数前剥掉剩余括号（同一份括号定义）
        origin = _PAREN_RE.sub("", origin).strip()
        oc = parse_cents(origin or "0")
        if oc is None:
            return ("skip", f"金额无法解析：{origin[:20]}")
        # 退款额取括号内最后一个数字（NFKC 后千分位已被 _amount_str 去掉，全/半角逗号通吃；
        # 正则提取的纯数字串恒可解析，parse_cents 不会返回 None）
        nums = re.findall(r"\d+(?:\.\d+)?", _amount_str(inner))
        refund = parse_cents(nums[-1]) if nums else oc
        return ("split", abs(oc), abs(refund))
    m2 = _PAREN_RE.search(raw)
    if m2:
        origin = _PAREN_RE.sub("", raw).strip()
        oc = parse_cents(origin) if origin else 0
        if oc is None:
            return ("skip", f"金额无法解析：{origin[:20]}")
        return ("confirm", abs(oc))
    pc = parse_cents(raw)
    if pc is None:
        return ("skip", f"金额无法解析：{normalize(raw)[:20]}")
    return ("plain", abs(pc))

def link_batch_refunds(flows: list) -> None:
    """导出回环第二遍：按流水ID 批内直连退款对（apply_import 的负序号协议：-(目标序号+1)）。
    引用不在本批（如原消费是更早批次）时保持无关联，交由通用退款匹配/rescan。"""
    by_id = {f.get("export_id"): i for i, f in enumerate(flows) if f.get("export_id")}
    for f in flows:
        ref = f.get("export_ref")
        if ref and ref in by_id:
            f["export_refund_idx"] = -(by_id[ref] + 1)

class RawFlow(dict):
    """平台无关的原始流水。"""

# ---------- 模板引擎 ----------

# 表头搜索窗口（行）：微信导出前言块实测可达 16+ 行（真机 2026-10-13 复现曾漏判），50 留足余量
_HEADER_WINDOW = 50

def _tpl_header_hit(rows: list, tpl: dict) -> int:
    """模板表头定位：某行单元格集合 ⊇ 模板列名（窗口内找）。返回行号或 -1。"""
    want = set(tpl["headers"])
    for i, r in enumerate(rows[:_HEADER_WINDOW]):
        if want <= {normalize(c) for c in r if normalize(c)}:
            return i
    return -1

def user_templates() -> list:
    """chaimi.conf 用户模板（向导写入；字段最小校验，坏行跳过）。
    config.load() 读失败时内部已告警并回落默认值，此处不再兜底——静默吞会把
    「模板读取出错」降级成「没有用户模板」，解析路由悄悄跳过用户模板无从察觉。"""
    return [t for t in (config.load().get("import_templates") or [])
            if isinstance(t, dict) and t.get("headers") and t.get("col")]

# 通用模板缺省 hash 五元组（旧 parse_generic 口径：原始日期格 + 派生 名称/金额/单号/商户）
_DEFAULT_HASH_FIELDS = ["$date", "$name", "$amount", "$order", "$merchant"]

def _eval_refund_rules(rules: list, cellval, flow_val: str, type_col: str):
    """退款规则求值（规则间 OR）。返回 (is_refund, 待剥后缀)：
    后缀规则命中在类型列上时返回命中后缀（微信 -退款 → source_category 取基础类型）。
    规则结构容错：save_import_template 已校验，但手改 conf/旧版落盘的缺键规则在此
    按 get() 跳过——此前直接下标 KeyError 让整张账单导不进（报错还不定位）。"""
    is_refund, strip = False, None
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("col") or not isinstance(rule.get("values"), list):
            continue
        v = normalize(cellval(rule["col"]))
        if not v:
            continue
        hit, suffix = False, None
        op = rule.get("op")
        if op == "in":
            hit = v in rule["values"]
        elif op == "contains":
            hit = (any(s in v for s in rule["values"])
                   and not any(s in v for s in rule.get("contains_none") or ()))
        elif op == "suffix":
            for s in rule["values"]:
                if v.endswith(s):
                    hit, suffix = True, s
                    break
        guard = rule.get("guard") or {}
        if hit and guard.get("flow_not") == flow_val:
            hit = False   # 方向守卫：如「已全额退款」的支出行是被退的原消费，不是退款
        if hit:
            is_refund = True
            if suffix and rule["col"] == type_col:
                strip = suffix
    return is_refund, strip

# 模板字段白名单（引擎只认这些键；未知字段显式报错而非静默忽略——防用户模板写错键名失效）
_TPL_ALLOWED_KEYS = {
    "id", "name", "headers", "col", "flow_map", "refund_type", "name_strip",
    "owner", "owner_re", "skip_first_n",                       # v1（向导）
    "refund_rules", "row_filter", "transfer_types", "name_fallback", "derive_base_as",
    "hash_fields", "expand", "category_split", "two_pass", "pay_split",
    "attribution_col", "skip_zero_reason", "note_strip", "amount_mode", "note_fallback_last",
}

def _amount_mode(g, tpl: dict):
    """金额三模式（parse_template 每行调用）→ (kind, cents, bracket_refund, need_confirm, skip_zero)。
    kind=None 表示行级跳过（跳过理由放在返回的 cents 位）；expand 全模式 0 元行统一标
    skip_zero——0 元原消费 confirm 时也会被 check_tx_fields（amount<=0）静默丢弃，
    预览计数与实际入库数对不上。
      expand   京东金额括号四态（split=原额+退款额 / confirm=其他括号待确认 / plain / skip）
      lenient  宽松取绝对值——负号是方向冗余（方向由收/支列决定），坏值（全角/N/A）跳行
      strict   严格校验失败或零额标 skip（向导/用户模板缺省口径）"""
    if tpl.get("expand"):
        raw_amount = normalize(g("amount"))   # 单元格 trim 单源（与 lenient/strict 的错误消息同口径）
        sr = split_bracket_amount(raw_amount) if raw_amount else ("plain", 0)
        if sr[0] == "skip":
            return None, sr[1], 0, False, False
        kind, cents = sr[0], sr[1]
        b = sr[2] if kind == "split" else 0
        return kind, cents, b, kind == "confirm", cents == 0
    if tpl.get("amount_mode") == "lenient":
        cents = parse_cents(g("amount"))
        if cents is None:
            return None, f"金额无法解析：{normalize(g('amount'))[:20]}", 0, False, False
        return "plain", abs(cents), 0, False, cents == 0
    cents = parse_cents(g("amount"))
    # 负号同 lenient 口径视为方向冗余（旧 _try_cents 内置 abs）；零额沿用跳行
    if cents is None or cents == 0:
        return None, f"金额无法解析：{normalize(g('amount'))[:20]}", 0, False, False
    return "plain", abs(cents), 0, False, False

class _TplCtx:
    """parse_template 的每文件解析上下文（原闭包变量的显式载体）：行级解析
    _parse_one_row / hash 取值共用，成员在 _tpl_ctx 一次性装配。"""
    __slots__ = ("tpl", "col", "idx", "header", "note_names", "note_fallback", "rules",
                 "type_col", "attribution_col", "owner", "filt", "flow_map",
                 "transfer_types", "amount_names", "hash_spec")

def _tpl_ctx(rows: list, tpl: dict, h: int) -> _TplCtx:
    """模板声明 → 行级解析上下文（表头定位/列索引/退款规则编译/owner 提取/hash 装配）。"""
    ctx = _TplCtx()
    ctx.tpl = tpl
    ctx.col = col = tpl.get("col") or {}
    ctx.header = header = [normalize(c) for c in rows[h]]
    idx: dict[str, int] = {}
    for i, c in enumerate(header):
        idx.setdefault(c, i)          # 重复列名取首现（与 .index 口径一致）
    ctx.idx = idx
    # 缺省 None → decide_type 落 DEFAULT_FLOW_MAP（支出/收入）：模板没写 flow_map 时
    # （sniff 猜测模板/直连 API），收/支列的常规枚举不该整单判 transfer
    ctx.flow_map = tpl.get("flow_map") or None
    ctx.transfer_types = tpl.get("transfer_types") or ()
    ctx.note_fallback = bool(tpl.get("note_fallback_last"))
    ctx.note_names = ([col["note"]] if isinstance(col.get("note"), str)
                      else list(col.get("note") or []))
    # 类型列解析名（refund_type 编译与后缀剥离的参照）
    type_names = col.get("type") or ""
    type_cols = [type_names] if isinstance(type_names, str) else list(type_names)
    ctx.type_col = next((nm for nm in type_cols if nm in idx), type_cols[0] if type_cols else "")
    ctx.rules = list(tpl.get("refund_rules") or [])
    if tpl.get("refund_type") and ctx.type_col:
        ctx.rules.insert(0, {"col": ctx.type_col, "op": "contains", "values": [tpl["refund_type"]]})
    ctx.owner = str(tpl.get("owner") or "").strip()
    if not ctx.owner and tpl.get("owner_re"):
        preamble = "\n".join(" ".join(str(c) for c in r) for r in rows[:h])
        m = re.search(tpl["owner_re"], preamble)
        if m:
            ctx.owner = m.group(1).strip()
    ctx.attribution_col = tpl.get("attribution_col") or ""
    ctx.filt = tpl.get("row_filter")
    if ctx.filt is not None and not (
            isinstance(ctx.filt, dict) and isinstance(ctx.filt.get("col"), str)
            and isinstance(ctx.filt.get("skip_in"), list)):
        # 手改 conf/旧版落盘的坏形态：入口即报并带原因（此前行级下标 KeyError 让整张
        # 账单 400 且不定位；静默跳过又会让本该过滤的行入库，与 _eval_refund_rules 的
        # 规则容错取舍不同——规则失效错一行、过滤器失效错一批）
        raise ValueError("模板 row_filter 形态不合法（须为 {col: 列名, skip_in: [值]}，请检查自定义模板）")
    ctx.hash_spec = tpl.get("hash_fields") or _DEFAULT_HASH_FIELDS
    ctx.amount_names = set(([col["amount"]] if isinstance(col.get("amount"), str)
                            else list(col.get("amount") or [])))
    return ctx

def _cellval(ctx: _TplCtx, r, name: str):
    """按列名取原始单元格；备注列缺失且声明 fallback 时借末列（hash 同步此口径）。"""
    i = ctx.idx.get(name, -1)
    if i < 0 and ctx.note_fallback and name in ctx.note_names:
        i = len(ctx.header) - 1
    return r[i] if 0 <= i < len(r) else ""

def _cellget(ctx: _TplCtx, r):
    """标准字段取值器：列名别名取首个非空值（导出两代列名兼容）。判空按归一后
    口径（空白/纯 tab 单元格视为缺失、落到下一别名）——所有消费方本就先 normalize
    再用，别名链不再另抄一份「归一后判空」循环。"""
    def g(key):
        names = ctx.col.get(key)
        if not names:
            return ""
        for nm in ([names] if isinstance(names, str) else names):
            v = _cellval(ctx, r, nm)
            if normalize(v):
                return v
        return ""
    return g

def _hash_fields(ctx: _TplCtx, r, g, name: str, amt_cents: int) -> list:
    """hash 字段集取值（$date=原始单元格保留旧通用模板口径；金额列 → 整数分字符串；
    普通列取原始单元格）。模块级按行传参，不再每行重建闭包。"""
    fields = []
    for e in ctx.hash_spec:
        if e == "$name":
            fields.append(name)
        elif e == "$amount":
            fields.append(str(amt_cents))
        elif e == "$date":
            fields.append(g("date"))
        elif e == "$order":
            fields.append(normalize(g("order")))
        elif e == "$merchant":
            fields.append(normalize(g("merchant")))
        elif e in ctx.amount_names:
            fields.append(str(amt_cents))
        else:
            fields.append(_cellval(ctx, r, e))
    return fields

def _parse_one_row(r, ctx: _TplCtx) -> RawFlow | None:
    """数据行 → RawFlow（skip 行以 RawFlow(skip=…) 表达）；静默跳过（空行/黑名单行）
    返回 None。逻辑与拆分前逐字等价（tests/test_parsers 等钉子锁定）。"""
    tpl = ctx.tpl
    # 空行判定看整行而非首列：自定义模板（向导）列序来自用户文件，首列不一定是
    # 交易时间——首列空白但日期/金额俱全的行此前被静默吞掉（不进 skip 统计）
    if not r or not any(normalize(c) for c in r):
        return None
    if ctx.filt and normalize(_cellval(ctx, r, ctx.filt["col"])) in ctx.filt["skip_in"]:
        return None
    g = _cellget(ctx, r)
    flow_val = normalize(g("flow"))
    is_refund, strip = _eval_refund_rules(ctx.rules, lambda n: _cellval(ctx, r, n), flow_val, ctx.type_col)
    type_val = normalize(g("type"))
    base_type = type_val[:len(type_val) - len(strip)] if strip else type_val

    # 日期归一 + 严校验：归一后仍非法（db.is_valid_date 为假）的行与「金额无法解析」
    # 同款标 skip——坏日期入库会破坏区间统计的字典序比较
    date_val = parse_dt(g("date"))
    if not dbm.is_valid_date(date_val):
        return RawFlow(skip=f"日期无法解析：{normalize(g('date'))[:20]}")

    # 金额三模式（kind=None=跳行，理由在 cents 位；confirm 按待确认普通行处理不继承退款命中）
    kind, cents, b, need_confirm, skip_zero = _amount_mode(g, tpl)
    if kind is None:
        return RawFlow(skip=cents)
    if kind == "confirm":
        is_refund = False
    if skip_zero:   # 零额行入库会被 dbm.check_tx_fields 拒收：解析期即标无效，预览不计「新」
        return RawFlow(skip=tpl.get("skip_zero_reason") or "0 元行")

    # 名称：回退链（微信）或直取；后缀清理（美团「订单详情」）
    if tpl.get("name_fallback"):
        nf = tpl["name_fallback"]
        name = resolve_name([_cellval(ctx, r, c) for c in nf["cols"]], nf)
    else:
        name = normalize(g("name"))
    for suf in tpl.get("name_strip") or []:
        name = name.replace(suf, "").strip()

    # 备注：别名链取首个非空（getter 已按归一后判空，此处一行），占位清理（美团 "/"）后与商户拼接
    note_val = normalize(g("note"))
    for s in tpl.get("note_strip") or []:
        if not note_val.strip(s):
            note_val = ""
    merchant = normalize(g("merchant"))
    note = _join_note(merchant, note_val)

    # 支付/收款账户：别名解析 + 分隔符拆分（导出 " & "）
    pay = normalize(g("pay"))
    to = normalize(g("to_account_name"))
    sep = tpl.get("pay_split") or ""
    if sep and not to and sep in pay:
        pay, to = [x.strip() for x in pay.split(sep, 1)]

    attr = normalize(_cellval(ctx, r, ctx.attribution_col)) if ctx.attribution_col else ctx.owner
    common = dict(
        platform=tpl.get("id") or "custom", date=date_val,
        merchant=merchant, name=name, note=note,
        attribution=attr, pay_account=pay, order_no=normalize(g("order")),
    )
    if "to_account_name" in ctx.col or sep:
        common["to_account_name"] = to
    if tpl.get("category_split"):
        cats = base_type.split()
        common["source_category"] = cats[0] if cats else ""
        common["extra_categories"] = cats[1:]
    else:
        common["source_category"] = base_type
    if tpl.get("derive_base_as"):
        common[tpl["derive_base_as"]] = base_type   # 微信 wechat_type（退款匹配消费）

    if kind == "split":
        # 金额列含「退款」括号（京东）：退款额已独立成行——仅取括号外原消费（强制
        # expense 供退款行关联，含全额退的不计收支行），括号额存 bracket_refund
        # （(已全额退款)=全额）作②'' 层关联线索
        return RawFlow(**common, amount=cents, type="expense",
                       is_refund=False, hash_fields=_hash_fields(ctx, r, g, name, cents),
                       bracket_refund=b)
    ttype = decide_type(flow_val, base_type, is_refund, ctx.transfer_types, ctx.flow_map)
    flow = RawFlow(**common, amount=cents, type=ttype, is_refund=is_refund,
                   hash_fields=_hash_fields(ctx, r, g, name, cents))
    if need_confirm:
        flow["need_confirm"] = True
    # col 非标准键原样入 RawFlow（导出来源/流水ID/退款关联等）
    for k in ctx.col:
        if k not in _STANDARD_COL_KEYS and k not in flow:
            flow[k] = normalize(g(k))
    return flow

def parse_template(rows: list, tpl: dict, h: int | None = None) -> list[RawFlow]:
    """统一模板引擎：解释声明（列映射/收支枚举/退款规则/transfer 集合/名称回退/
    括号拆行/两遍直连/hash 字段集），产出 RawFlow。前言块靠表头集合定位天然跳过。
    h 可传入已定位的表头行号（parse_bill 命中即解析，免同文件扫两遍）；
    声明装配（_tpl_ctx）与行级解析（_parse_one_row）分治，本函数只剩编排。"""
    unknown = set(tpl) - _TPL_ALLOWED_KEYS
    if unknown:
        raise ValueError(f"模板含未知字段：{', '.join(sorted(unknown))}")
    h = _tpl_header_hit(rows, tpl) if h is None else h
    if h < 0:
        raise ValueError(f"表头与模板「{tpl.get('name') or tpl.get('id')}」不符")
    ctx = _tpl_ctx(rows, tpl, h)
    out: list[RawFlow] = []
    data_rows = rows[h + 1:]
    skip_n = int(tpl.get("skip_first_n") or 0)
    if skip_n > 0:
        data_rows = data_rows[skip_n:]   # 表头后的前 N 行数据跳过（列说明/注释行等）
    for r in data_rows:
        flow = _parse_one_row(r, ctx)
        if flow is not None:
            out.append(flow)
    if tpl.get("two_pass"):
        link_batch_refunds(out)
    return out

def parse_generic(text: str, tpl: dict) -> list[RawFlow]:
    """通用模板入口（向导试解析 / 兼容引用）：文本 + 模板 → 统一引擎。"""
    rows = _csv_rows(text)
    return parse_template(rows, tpl)

def _decode_csv(data: bytes) -> str:
    """CSV 解码：utf-8-sig → GBK；都失败按替换字符少的降级（表情坏字节等）。"""
    for enc in ("utf-8-sig", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return min(((data.decode(e, errors="replace"), e) for e in ("utf-8-sig", "gbk")),
               key=lambda p: p[0].count("\ufffd"))[0]

def _xlsx_to_csv(data: bytes) -> str:
    """xlsx 首 sheet → CSV 文本（引擎统一按行处理；表头集合匹配替代旧子串嗅探）。"""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    for row in _capped(wb.active.iter_rows(values_only=True)):
        w.writerow(["" if v is None else v for v in row])
    return buf.getvalue()

def text_from_upload(data: bytes, filename: str) -> str:
    """上传字节 → CSV 文本：xlsx 首表转换 / 其余按 CSV 解码（server 上传口与 parse_bill 共用单源）。"""
    return (_xlsx_to_csv(data) if (filename or "").lower().endswith(".xlsx")
            else _decode_csv(data))

def parse_bill(data: bytes, filename: str) -> list[RawFlow]:
    """统一路由：xlsx→行 / CSV 解码后，用户模板优先、内置模板次之，表头集合命中即解析。
    覆盖机制：用户复制的模板排在前面，改列名后即接管该格式的路由。"""
    text = text_from_upload(data, filename)
    rows = _csv_rows(text)
    for tpl in [*user_templates(), *pt.PLATFORM_TEMPLATES]:
        h = _tpl_header_hit(rows, tpl)
        if h >= 0:
            return parse_template(rows, tpl, h)   # 命中行号直传，引擎内不再重扫表头窗口
    raise ValueError("无法识别账单平台（支持：支付宝/微信/京东/本应用导出/自定义模板）")

# ---------- 向导嗅探（纯函数，输入输出与解析引擎解耦） ----------

# 列名→标准字段的猜测词表（精确优先于包含；键序=字段内优先级）
_COL_GUESS = [
    ("date", ("交易成功时间", "交易时间", "付款时间", "创建时间", "时间", "日期")),
    ("amount", ("实付金额", "实收金额", "交易金额", "金额", "发生额", "金额(元)")),
    ("flow", ("收/支", "收支", "借贷方向", "借贷", "方向")),
    ("order", ("交易单号", "订单号", "流水号", "商户单号", "单号")),
    ("pay", ("支付方式", "付款方式", "支付渠道", "结算方式")),
    ("type", ("交易类型", "业务类型", "交易状态", "类型", "状态")),
    ("name", ("订单标题", "商品说明", "商品", "交易摘要", "摘要", "描述", "品名", "名称")),
    ("merchant", ("交易对方", "商户名称", "商户", "对方")),
    ("note", ("备注", "说明", "附言")),
]

def sniff_template(text: str) -> dict:
    """向导嗅探（纯函数）：定位表头行 + 提取样例 + 预填列映射猜测。
    表头启发：窗口内首个"≥4 个非空单元格 且 含 时间/日期/金额/单号 字样"的行；
    全不中再退到首个 ≥4 列的行。"""
    rows = _csv_rows(text)
    h, fallback = -1, -1
    for i, r in enumerate(rows[:_HEADER_WINDOW]):
        cells = [normalize(c) for c in r if normalize(c)]
        if len(cells) < 4:
            continue
        if any(re.search(r"时间|日期|金额|单号", c) for c in cells):
            h = i
            break
        if fallback < 0:
            fallback = i            # 无关键字但 ≥4 列的行：全不中时的兜底表头
    if h < 0:
        h = fallback
    if h < 0:
        return {"headers": [], "header_row": -1, "samples": [], "guess": {}}
    headers = [normalize(c) for c in rows[h]]
    guess = {}
    for field, keys in _COL_GUESS:
        for k in keys:
            hit = next((c for c in headers if c == k), None) \
                  or next((c for c in headers if k in c), None)
            if hit and hit not in guess.values():
                guess[field] = hit
                break
    samples = []
    for r in rows[h + 1:]:
        if any(normalize(c) for c in r):
            samples.append([str(c)[:24] for c in r[:len(headers)]])
        if len(samples) >= 5:
            break
    return {"headers": headers, "header_row": h, "samples": samples, "guess": guess}

_BANK_RE = re.compile(r"银行|储蓄卡|信用卡|借记卡|贷记卡")

# 平台钱包匹配/建户视图：事实单源在 platform_templates.PLATFORMS（新增平台只改那里）
_WALLET_KEYWORDS = {k: v["keywords"] for k, v in pt.PLATFORMS.items()}
_PLATFORM_ACC = {k: v["account"] for k, v in pt.PLATFORMS.items()}

class _AccountIndex:
    """账户表内存索引（build_previews 逐行匹配用）：一次解析可上万行，逐行
    「查账户表 + 反查账户名」是 N+1（此前每行 2-4 次 SQL，预览全程持
    _PREVIEW_LOCK 串行）。建户即时同步进索引，同一解析内后续行可见（与此前
    每行直查库的可见性一致）。"""
    __slots__ = ("conn", "lid", "by_name", "by_norm", "names")

    def __init__(self, conn: sqlite3.Connection, ledger_id: int):
        self.conn, self.lid = conn, ledger_id
        self.by_name: dict[str, int] = {}   # 精确名 → id（插入序 = id 序）
        self.by_norm: dict[str, int] = {}   # norm_account_name → id（首个命中）
        self.names: dict[int, str] = {}     # id → 名（预览的 account_name 反查）
        for r in conn.execute("SELECT id, name FROM accounts WHERE ledger_id=? ORDER BY id",
                              (ledger_id,)):
            self._add(r["id"], r["name"])

    def _add(self, aid: int, name: str) -> None:
        self.by_name[name] = aid
        self.by_norm.setdefault(norm_account_name(name), aid)
        self.names[aid] = name

    def ensure(self, name: str, atype: str = "bank") -> int:
        """取或建账户：精确名 → 归一同名（同一张卡在微信/支付宝账单里的不同写法不再
        各建一份）→ 按归一名建户。建户在导入解析期即时生效、独立提交；撤销导入不删。"""
        aid = self.by_name.get(name) or self.by_norm.get(norm_account_name(name))
        if aid:
            return aid
        with self.conn:
            sort = self.conn.execute(
                "SELECT COALESCE(MAX(sort),0)+1 FROM accounts WHERE ledger_id=?",
                (self.lid,)).fetchone()[0]
            cur = self.conn.execute(
                "INSERT INTO accounts(ledger_id, name, type, sort) VALUES(?,?,?,?)",
                (self.lid, norm_account_name(name), atype, sort))
        self._add(cur.lastrowid, norm_account_name(name))
        return cur.lastrowid

    def match(self, platform: str, pay: str) -> int | None:
        """收/付款方式 → 账户 id（未匹配返回 None）：
        ① 银行卡段优先（& 组合取左起第一个银行卡段），按原文自动建户（如 招商银行储蓄卡(7529)）；
        ② 平台钱包/空值/" 归平台账户（花呗→支付宝、零钱通→微信支付），平台账户缺失自动补建（京东）；
        ③ 其余（本应用导出等）按现有账户名子串或归一同名匹配。"""
        parts = [p.strip() for p in (pay or "").split("&") if p.strip() and p.strip() != "/"]
        bank = next((p for p in parts if _BANK_RE.search(p)), None)
        if bank:
            return self.ensure(bank, "bank")
        if platform in _PLATFORM_ACC and (
                not parts or any(any(k in p for k in _WALLET_KEYWORDS[platform]) for p in parts)):
            return self.ensure(_PLATFORM_ACC[platform], "e-wallet")
        # 子串匹配按 id 序（by_name 插入序）：多账户名互为子串时（微信支付/微信支付零钱）
        # 命中须确定，不排序会随存储顺序漂移——同库同输入应产出同一导入结果
        if parts:
            p = parts[0]
            for name, aid in self.by_name.items():
                if name in p or p in name:
                    return aid
            return self.by_norm.get(norm_account_name(p))   # 归一同名兜底历史旧写法账户
        return None

    def name_of(self, acc_id: int | None) -> str:
        """id → 账户名（预览行 account_name 用）；id 空返回空串。"""
        return self.names.get(acc_id or 0, "")

def _ensure_account(conn: sqlite3.Connection, ledger_id: int, name: str, atype: str = "bank") -> int:
    """一次性建/取账户（索引类 _AccountIndex 的单发便捷入口）。"""
    return _AccountIndex(conn, ledger_id).ensure(name, atype)

def _match_account(conn: sqlite3.Connection, ledger_id: int, platform: str, pay: str) -> int | None:
    """单发账户匹配（索引类 _AccountIndex 的便捷入口，测试/偶发调用用；
    批量路径走 build_previews 的常驻索引）。"""
    return _AccountIndex(conn, ledger_id).match(platform, pay)

def build_previews(conn: sqlite3.Connection, ledger_id: int, flows: list[RawFlow]) -> list[dict]:
    """RawFlow → FlowPreview（含分类解析/去重/退款匹配预填，7.5/7.8）。
    1.0.1 起**纯只读**：旧版在这里随解析静默执行 _purge_bad_learned_mappings +
    auto_fix_transactions 改写已有数据——用户手动整理的分类会在下一次（哪怕只是
    预览、未确认导入）被翻回词典结果。自愈移至 /api/repair/*（预览+确认应用）。"""
    # 账本快照：去重/退款匹配/跨源配对/疑似重复提示共用（一次加载，见 _LedgerSnapshot）
    snap = _LedgerSnapshot(conn, ledger_id)
    existing = snap.by_hash
    # 身份索引 (来源, 订单号|流水ID, 日期到天, 金额, 类型) → id，row_hash 未命中时判重：导出文件的
    # row_hash 与平台原始行字段集不同，且含名称/分类/备注、冻结在入库时（入库后改过即失配）。
    # 订单号是平台交易唯一键；流水ID 即库行 id（快照只含本账本，其余四项防别库导出撞号）。
    # 含 type：个别平台退款行复用原消费单号，同额同日时不含 type 会把退款行误判重丢失。
    # 两者皆无的行不按名称/金额冒险（同名同额的真实重复消费存在），仍走疑似重复软提示。
    by_order, by_id = {}, {}
    for r in snap.rows:
        fact = ((r["date"] or "")[:10], r["amount"], r["type"])
        by_id[(r["source"], str(r["id"]), *fact)] = r["id"]
        if r["order_no"]:
            by_order[(r["source"], r["order_no"], *fact)] = r["id"]
    # 分类瀑布上下文（一级分类索引 + transfer 专用分类；见 import_classify）
    ctx = ClassifyCtx(conn, ledger_id)
    # 账户索引（逐行匹配/建户/反查共用，见 _AccountIndex）
    accs = _AccountIndex(conn, ledger_id)

    previews = []
    for f in flows:
        if "skip" in f or "hash_fields" not in f:
            previews.append({"skip": True, "reason": f.get("skip", "无效行"),
                             "name": f.get("name", ""), "amount": 0})
            continue
        h = _row_hash(f["hash_fields"])
        # 查重键与入库 source 同口径（导出回环「来源」列覆写先于查重），否则带来源列的导出行重导不判重
        src = f.get("origin_source") or f["platform"]
        p = {"type": f["type"], "amount": f["amount"], "date": f["date"],
             "name": f["name"], "merchant": f.get("merchant", ""), "note": f.get("note", ""),
             "platform": f["platform"], "source": src,
             "order_no": f.get("order_no", ""), "row_hash": h,
             "category_id": None, "category_name": "", "parent_category": "",
             "classified_via": "raw", "ai_confidence": None,
             "need_confirm": f.get("need_confirm", False), "refund_for": None, "refund_status": "none",
             "refund_confidence": None,  # high/low/unmatched，仅 is_refund=True 才有意义
             "duplicate": False, "duplicate_of": None,
             "to_account_id": None, "tags": {}, "ai_meta": {}}
        fact = ((f["date"] or "")[:10], f["amount"], f["type"])
        dup = (existing.get((src, h)) or by_order.get((src, f.get("order_no"), *fact))
               or by_id.get((src, f.get("export_id"), *fact)))
        if dup:
            p["duplicate"], p["duplicate_of"] = True, dup
            previews.append(p)
            continue

        acc_id = accs.match(f.get("platform", ""), f.get("pay_account", ""))
        p["account_id"] = acc_id
        p["account_name"] = accs.name_of(acc_id)

        if f["type"] == "transfer":
            # 导出回环：收款账户列按名取户（精确 → 归一同名 → 建户），转账对方不丢
            if f.get("to_account_name"):
                p["to_account_id"] = accs.ensure(f["to_account_name"], "bank")
            classify_transfer(f, ctx, p,
                              None if f.get("export_refund_idx") is None
                              else flows[-f["export_refund_idx"] - 1]["amount"])
        else:
            classify_flow(f, ctx, p)
        if f.get("bracket_refund") is not None:
            p["bracket_refund"] = f["bracket_refund"]
            p["tags"]["bracket_refund"] = f["bracket_refund"]   # 持久化，跨批括号匹配用
        if f.get("extra_categories"):
            p["tags"]["extra_categories"] = f["extra_categories"]
        if f.get("attribution"):
            p["tags"]["attribution"] = f["attribution"]
        previews.append(p)

    # 退款匹配预填（7.7）
    _match_refunds(conn, ledger_id, previews, flows, snap)
    # P2 跨源配对：平台支付行 vs 库中支付渠道流水 → pay_dupe（默认跳过，可恢复）
    _link_payment(conn, ledger_id, previews, snap)
    # 疑似重复提示：同额同日（hash 不同）→ dup_hint 由前端标「疑似重复」；
    # 精确 duplicate（hash 相同）仍自动跳过，两者互不影响
    dbm.dup_hints(conn, ledger_id, [p for p in previews if not p.get("skip")
                                    and not p.get("duplicate") and "amount" in p], rows=snap.rows)
    return previews

