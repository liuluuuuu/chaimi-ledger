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
from datetime import datetime

import db as dbm
import platform_templates as pt
from import_common import (_is_new_row, _is_placeholder_name, is_meaningless_name,
                           norm_account_name, normalize)
from import_classify import _top_by_keyword   # 门面再导出：ai/tests 经 ib._top_by_keyword
                                               # 访问（误删即断链，教训见 02bc9a9）
from import_classify import ClassifyCtx, classify_transfer, classify_flow
from import_match import _LedgerSnapshot, _match_refunds, _link_payment
from import_apply import apply_import, undo_batch, _rescan_unmatched_refunds

# ---------- 基础工具 ----------

#   京东金额列括号（全/半角都认；半角正则会因全角行 float() 抛错）：
#   ① 含「退款」关键词 → 拆为原消费 + 退款两条（拆分在 expand 分支）
#   ② 其他括号内容（已退积分/红包/入账/补偿等）→ 单条 + need_confirm
#   ③ 无括号 → 原样
_REFUND_PAREN_RE = re.compile(r"[（(]([^（）()]*退款[^（）()]*)[)）]")
_PAREN_RE = re.compile(r"[（(]([^（）()]*)[)）]")

def _amount_str(s) -> str:
    """金额单元格清洗（to_cents / _try_cents 共用）：去币符/千分位。"""
    return normalize(s).replace("¥", "").replace("￥", "").replace(",", "")

def to_cents(s) -> int:
    return round(float(_amount_str(s) or "0") * 100)

def _try_cents(s) -> int | None:
    """金额→分（严格校验场景）；解析失败或为 0 返回 None。
    先取绝对值再校验：银行流水的负号是方向冗余（方向由收/支列决定），旧版在此
    整行跳过负号金额、后置的 abs() 形同虚设。"""
    try:
        v = round(abs(float(_amount_str(s) or "0")) * 100)
        return v or None
    except (ValueError, TypeError):
        return None

def parse_dt(s: str) -> str:
    s = normalize(s).replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return s

def _row_hash(fields: list[str]) -> str:
    return hashlib.md5("|".join(normalize(f) for f in fields).encode("utf-8")).hexdigest()

# IN (...) 占位符分块上限：SQLite 单语句绑定变量有编译期上限（3.32+ 为 32766，旧版 999），
# 行数熔断允许 5 万行，超过上限的合法导入会在「too many SQL variables」上整个失败——分块兜底
# 解析期行数熔断上限（防高压缩比 xlsx / 超长 CSV 的解析期内存放大）；
# server.import_parse 的 len(flows) 校验引用同一常量作权威上限
MAX_PARSE_ROWS = 50_000

def _capped(rows):
    """行迭代熔断：超限抛 ValueError（import_parse 统一转 400）。
    余量 200 行给各平台表头前的导出信息块。"""
    for i, row in enumerate(rows):
        if i > MAX_PARSE_ROWS + 200:
            raise ValueError("行数超过上限 5 万行")
        yield row

# ---------- 原语层（引擎与测试共用；每个原语在抽取时即有调用方，不预建） ----------

# 缺省收/支枚举（_ttype 旧口径）；模板可传自己的 flow_map 覆盖（京东仅支出计收支）
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

def _ttype(shouzhi: str, is_refund: bool) -> str:
    """收/支列 → 流水类型（支付宝/微信旧口径，保留供测试与兼容引用）。"""
    return decide_type(shouzhi, None, is_refund)

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
    """金额列括号三态（京东）：
    ("split", 原额分, 退款分)   括号含「退款」：括号内最后一个数字为退款额，无数字则全额；
    ("confirm", 原额分)         其他括号（已退积分/红包等）→ 单条 + need_confirm；
    ("plain", 金额分)           无括号原样。"""
    m = _REFUND_PAREN_RE.search(raw)
    if m:
        inner = m.group(1)
        origin = _REFUND_PAREN_RE.sub("", raw)
        nums = re.findall(r"\d+(?:\.\d+)?", inner)
        refund = nums[-1] if nums else origin
        return ("split", to_cents(origin), to_cents(refund))
    m2 = _PAREN_RE.search(raw)
    if m2:
        origin = _PAREN_RE.sub("", raw).strip()
        return ("confirm", to_cents(origin) if origin else 0)
    return ("plain", to_cents(raw))

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
    """chaimi.conf 用户模板（向导写入；字段最小校验，坏行跳过）。"""
    try:
        import config
        return [t for t in (config.load().get("import_templates") or [])
                if isinstance(t, dict) and t.get("headers") and t.get("col")]
    except Exception:
        return []

# 通用模板缺省 hash 五元组（旧 parse_generic 口径：原始日期格 + 派生 名称/金额/单号/商户）
_DEFAULT_HASH_FIELDS = ["$date", "$name", "$amount", "$order", "$merchant"]

def _eval_refund_rules(rules: list, cellval, flow_val: str, type_col: str):
    """退款规则求值（规则间 OR）。返回 (is_refund, 待剥后缀)：
    后缀规则命中在类型列上时返回命中后缀（微信 -退款 → source_category 取基础类型）。"""
    is_refund, strip = False, None
    for rule in rules:
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

def parse_template(rows: list, tpl: dict) -> list[RawFlow]:
    """统一模板引擎：解释声明（列映射/收支枚举/退款规则/transfer 集合/名称回退/
    括号拆行/两遍直连/hash 字段集），产出 RawFlow。前言块靠表头集合定位天然跳过。"""
    unknown = set(tpl) - _TPL_ALLOWED_KEYS
    if unknown:
        raise ValueError(f"模板含未知字段：{', '.join(sorted(unknown))}")
    h = _tpl_header_hit(rows, tpl)
    if h < 0:
        raise ValueError(f"表头与模板「{tpl.get('name') or tpl.get('id')}」不符")
    header = [normalize(c) for c in rows[h]]
    idx: dict[str, int] = {}
    for i, c in enumerate(header):
        idx.setdefault(c, i)          # 重复列名取首现（与 .index 口径一致）
    col = tpl.get("col") or {}
    flow_map = tpl.get("flow_map") or {}
    transfer_types = tpl.get("transfer_types") or ()
    note_fallback = bool(tpl.get("note_fallback_last"))
    note_names = ([col["note"]] if isinstance(col.get("note"), str)
                  else list(col.get("note") or []))

    def cellval(r, name: str):
        """按列名取原始单元格；备注列缺失且声明 fallback 时借末列（hash 同步此口径）。"""
        i = idx.get(name, -1)
        if i < 0 and note_fallback and name in note_names:
            i = len(header) - 1
        return r[i] if 0 <= i < len(r) else ""

    def getter(r):
        """标准字段取值：列名别名取首个非空值（导出两代列名兼容）。"""
        def g(key):
            names = col.get(key)
            if not names:
                return ""
            for nm in ([names] if isinstance(names, str) else names):
                v = cellval(r, nm)
                if v not in (None, ""):
                    return v
            return ""
        return g

    # 类型列解析名（refund_type 编译与后缀剥离的参照）
    type_names = col.get("type") or ""
    type_cols = [type_names] if isinstance(type_names, str) else list(type_names)
    type_col = next((nm for nm in type_cols if nm in idx), type_cols[0] if type_cols else "")
    rules = list(tpl.get("refund_rules") or [])
    if tpl.get("refund_type") and type_col:
        rules.insert(0, {"col": type_col, "op": "contains", "values": [tpl["refund_type"]]})

    owner = str(tpl.get("owner") or "").strip()
    if not owner and tpl.get("owner_re"):
        preamble = "\n".join(" ".join(str(c) for c in r) for r in rows[:h])
        m = re.search(tpl["owner_re"], preamble)
        if m:
            owner = m.group(1).strip()
    attribution_col = tpl.get("attribution_col") or ""
    filt = tpl.get("row_filter")
    hash_spec = tpl.get("hash_fields") or _DEFAULT_HASH_FIELDS
    amount_names = set(([col["amount"]] if isinstance(col.get("amount"), str)
                        else list(col.get("amount") or [])))

    out: list[RawFlow] = []
    data_rows = rows[h + 1:]
    skip_n = int(tpl.get("skip_first_n") or 0)
    if skip_n > 0:
        data_rows = data_rows[skip_n:]   # 表头后的前 N 行数据跳过（列说明/注释行等）
    for r in data_rows:
        if not r or not normalize(r[0]):
            continue
        if filt and normalize(cellval(r, filt["col"])) in filt["skip_in"]:
            continue
        g = getter(r)
        flow_val = normalize(g("flow"))
        is_refund, strip = _eval_refund_rules(rules, lambda n: cellval(r, n), flow_val, type_col)
        type_val = normalize(g("type"))
        base_type = type_val[:len(type_val) - len(strip)] if strip else type_val

        # 金额三态（expand）/ 宽松-严格两模式
        if tpl.get("expand"):
            raw_amount = str(g("amount") or "").strip().replace("\t", "")
            sr = split_bracket_amount(raw_amount) if raw_amount else ("plain", 0)
            kind, a = sr[0], sr[1]
            b = sr[2] if kind == "split" else 0
            reason = tpl.get("skip_zero_reason") or ""
            if reason and kind == "plain" and a == 0:
                out.append(RawFlow(skip=reason))
                continue
        elif tpl.get("amount_mode") == "lenient":
            cents = to_cents(g("amount"))
            reason = tpl.get("skip_zero_reason") or ""
            if reason and cents == 0:
                out.append(RawFlow(skip=reason))
                continue
            kind = "plain"
        else:   # strict：向导/用户模板口径——先严格校验（负号金额取绝对值），失败标 skip
            cents = _try_cents(g("amount"))
            if cents is None:
                out.append(RawFlow(skip=f"金额无法解析：{normalize(g('amount'))[:20]}"))
                continue
            kind = "plain"

        # 名称：回退链（微信）或直取；后缀清理（美团「订单详情」）
        if tpl.get("name_fallback"):
            nf = tpl["name_fallback"]
            name = resolve_name([cellval(r, c) for c in nf["cols"]], nf)
        else:
            name = normalize(g("name"))
        for suf in tpl.get("name_strip") or []:
            name = name.replace(suf, "").strip()

        # 备注：占位清理（美团 "/"）后与商户拼接
        note_val = normalize(cellval(r, note_names[0])) if note_names else ""
        for s in tpl.get("note_strip") or []:
            if not note_val.strip(s):
                note_val = ""
        note = _join_note(normalize(g("merchant")), note_val)

        # 支付/收款账户：别名解析 + 分隔符拆分（导出 " & "）
        pay = normalize(g("pay"))
        to = normalize(g("to_account_name"))
        sep = tpl.get("pay_split") or ""
        if sep and not to and sep in pay:
            pay, to = [x.strip() for x in pay.split(sep, 1)]

        attr = normalize(cellval(r, attribution_col)) if attribution_col else owner
        common = dict(
            platform=tpl.get("id") or "custom", date=parse_dt(g("date")),
            merchant=normalize(g("merchant")), name=name, note=note,
            attribution=attr, pay_account=pay, order_no=normalize(g("order")),
        )
        if "to_account_name" in col or sep:
            common["to_account_name"] = to
        if tpl.get("category_split"):
            cats = base_type.split()
            common["source_category"] = cats[0] if cats else ""
            common["extra_categories"] = cats[1:]
        else:
            common["source_category"] = base_type
        if tpl.get("derive_base_as"):
            common[tpl["derive_base_as"]] = base_type   # 微信 wechat_type（退款匹配消费）

        def hash_list(amt_cents):
            fields = []
            for e in hash_spec:
                if e == "$name":
                    fields.append(name)
                elif e == "$amount":
                    fields.append(str(amt_cents))
                elif e == "$date":
                    fields.append(g("date"))            # 原始单元格（旧通用模板口径）
                elif e == "$order":
                    fields.append(normalize(g("order")))
                elif e == "$merchant":
                    fields.append(normalize(g("merchant")))
                elif e in amount_names:
                    fields.append(str(amt_cents))       # 金额列 → 整数分字符串
                else:
                    fields.append(cellval(r, e))        # 普通列：原始单元格
            return fields

        if kind == "split":
            # 金额列含「退款」括号（京东）：退款额已独立成行——仅取括号外原消费（强制
            # expense 供退款行关联，含全额退的不计收支行），括号额存 bracket_refund
            # （(已全额退款)=全额）作②'' 层关联线索
            out.append(RawFlow(**common, amount=a, type="expense",
                               is_refund=False, hash_fields=hash_list(a),
                               bracket_refund=b))
            continue
        need_confirm = False
        if kind == "confirm":
            cents, is_refund, need_confirm = a, False, True
        elif tpl.get("expand"):
            cents = a
        ttype = decide_type(flow_val, base_type, is_refund, transfer_types, flow_map)
        flow = RawFlow(**common, amount=cents, type=ttype, is_refund=is_refund,
                       hash_fields=hash_list(cents))
        if need_confirm:
            flow["need_confirm"] = True
        # col 非标准键原样入 RawFlow（导出来源/流水ID/退款关联等）
        for k in col:
            if k not in _STANDARD_COL_KEYS and k not in flow:
                flow[k] = normalize(g(k))
        out.append(flow)

    if tpl.get("two_pass"):
        link_batch_refunds(out)
    return out

def parse_generic(text: str, tpl: dict) -> list[RawFlow]:
    """通用模板入口（向导试解析 / 兼容引用）：文本 + 模板 → 统一引擎。"""
    rows = list(_capped(csv.reader(io.StringIO(text))))
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
    rows = list(_capped(csv.reader(io.StringIO(text))))
    for tpl in [*user_templates(), *pt.PLATFORM_TEMPLATES]:
        if _tpl_header_hit(rows, tpl) >= 0:
            return parse_template(rows, tpl)
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
    rows = list(_capped(csv.reader(io.StringIO(text))))
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

# 平台钱包关键词（含花呗/零钱通等信用与理财钱包，按用户规则归平台账户）
_WALLET_KEYWORDS = {
    "alipay": ("余额宝", "账户余额", "花呗", "支付宝"),
    "wechat": ("零钱", "微信"),
    "jd": ("余额", "白条", "京东"),
    "meituan": ("余额", "美团支付", "美团"),
}
_PLATFORM_ACC = {"alipay": "支付宝", "wechat": "微信支付", "jd": "京东", "meituan": "美团支付"}

def _find_norm_account(conn: sqlite3.Connection, ledger_id: int, name: str) -> int | None:
    """归一同名查账户（同一张卡的历史变体写法兜底）：命中返回 id，未命中 None。"""
    key = norm_account_name(name)
    for r in conn.execute("SELECT id, name FROM accounts WHERE ledger_id=?", (ledger_id,)):
        if norm_account_name(r["name"]) == key:
            return r["id"]
    return None

def _ensure_account(conn: sqlite3.Connection, ledger_id: int, name: str, atype: str = "bank") -> int:
    """取或建账户：精确名 → 归一同名（同一张卡在微信/支付宝账单里的不同写法不再
    各建一份）→ 按归一名建户。建户在导入解析期即时生效、独立提交；撤销导入不删。"""
    row = conn.execute("SELECT id FROM accounts WHERE ledger_id=? AND name=?",
                       (ledger_id, name)).fetchone()
    if row:
        return row["id"]
    aid = _find_norm_account(conn, ledger_id, name)
    if aid:
        return aid
    with conn:
        sort = conn.execute("SELECT COALESCE(MAX(sort),0)+1 FROM accounts WHERE ledger_id=?",
                            (ledger_id,)).fetchone()[0]
        cur = conn.execute("INSERT INTO accounts(ledger_id, name, type, sort) VALUES(?,?,?,?)",
                           (ledger_id, norm_account_name(name), atype, sort))
    return cur.lastrowid

def _match_account(conn: sqlite3.Connection, ledger_id: int, platform: str, pay: str) -> int | None:
    """收/付款方式 → 账户 id（未匹配返回 None）：
    ① 银行卡段优先（& 组合取左起第一个银行卡段），按原文自动建户（如 招商银行储蓄卡(7529)）；
    ② 平台钱包/空值/" 归平台账户（花呗→支付宝、零钱通→微信支付），平台账户缺失自动补建（京东）；
    ③ 其余（本应用导出等）按现有账户名子串或归一同名匹配。"""
    parts = [p.strip() for p in (pay or "").split("&") if p.strip() and p.strip() != "/"]
    bank = next((p for p in parts if _BANK_RE.search(p)), None)
    if bank:
        return _ensure_account(conn, ledger_id, bank, "bank")
    if platform in _PLATFORM_ACC and (
            not parts or any(any(k in p for k in _WALLET_KEYWORDS[platform]) for p in parts)):
        return _ensure_account(conn, ledger_id, _PLATFORM_ACC[platform], "e-wallet")
    accs = {r["name"]: r["id"] for r in conn.execute(
        "SELECT name, id FROM accounts WHERE ledger_id=?", (ledger_id,))}
    if parts:
        p = parts[0]
        for name, aid in accs.items():
            if name in p or p in name:
                return aid
        return _find_norm_account(conn, ledger_id, p)   # 归一同名兜底历史旧写法账户
    return None

def _account_name(conn: sqlite3.Connection, acc_id: int | None) -> str:
    """根据 account_id 反查账户名；id 空则返回空串。"""
    if not acc_id:
        return ""
    r = conn.execute("SELECT name FROM accounts WHERE id=?", (acc_id,)).fetchone()
    return r["name"] if r else ""

def build_previews(conn: sqlite3.Connection, ledger_id: int, flows: list[RawFlow]) -> list[dict]:
    """RawFlow → FlowPreview（含分类解析/去重/退款匹配预填，7.5/7.8）。
    1.0.1 起**纯只读**：旧版在这里随解析静默执行 _purge_bad_learned_mappings +
    auto_fix_transactions 改写已有数据——用户手动整理的分类会在下一次（哪怕只是
    预览、未确认导入）被翻回词典结果。自愈移至 /api/repair/*（预览+确认应用）。"""
    # 账本快照：去重/退款匹配/跨源配对/疑似重复提示共用（一次加载，见 _LedgerSnapshot）
    snap = _LedgerSnapshot(conn, ledger_id)
    existing = snap.by_hash
    # 分类瀑布上下文（一级分类索引 + transfer 专用分类；见 import_classify）
    ctx = ClassifyCtx(conn, ledger_id)

    previews = []
    for f in flows:
        if "skip" in f or "hash_fields" not in f:
            previews.append({"skip": True, "reason": f.get("skip", "无效行"),
                             "name": f.get("name", ""), "amount": 0})
            continue
        h = _row_hash(f["hash_fields"])
        p = {"type": f["type"], "amount": f["amount"], "date": f["date"],
             "name": f["name"], "merchant": f.get("merchant", ""), "note": f.get("note", ""),
             "platform": f["platform"], "source": f["platform"],
             "order_no": f.get("order_no", ""), "row_hash": h,
             "category_id": None, "category_name": "", "parent_category": "",
             "classified_via": "raw", "ai_confidence": None,
             "need_confirm": f.get("need_confirm", False), "refund_for": None, "refund_status": "none",
             "refund_confidence": None,  # high/low/unmatched，仅 is_refund=True 才有意义
             "duplicate": False, "duplicate_of": None,
             "to_account_id": None, "tags": {}, "ai_meta": {}}
        dup = existing.get((f["platform"], h))
        if dup:
            p["duplicate"], p["duplicate_of"] = True, dup
            previews.append(p)
            continue

        if f.get("origin_source"):
            p["source"] = f["origin_source"]

        acc_id = _match_account(conn, ledger_id, f.get("platform", ""), f.get("pay_account", ""))
        p["account_id"] = acc_id
        p["account_name"] = _account_name(conn, acc_id) if acc_id else ""

        if f["type"] == "transfer":
            # 导出回环：收款账户列按名取户（精确 → 归一同名 → 建户），转账对方不丢
            if f.get("to_account_name"):
                p["to_account_id"] = _ensure_account(conn, ledger_id, f["to_account_name"], "bank")
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
    # 精确去重）/批内重复 → dup_hint 由前端标「疑似重复」；精确 duplicate 仍自动跳过，互不影响
    dbm.dup_hints(conn, ledger_id, [p for p in previews if not p.get("skip")
                                    and not p.get("duplicate") and "amount" in p], rows=snap.rows)
    return previews

