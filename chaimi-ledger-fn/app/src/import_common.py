"""导入公共原语（各子模块共享的叶子工具，无内部依赖）。"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import re
import unicodedata

import platform_templates as pt

_WAN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万\s*(\d)?(?![\d.])")

def strip_amount_str(v: str) -> str:
    """会话侧金额字符串清洗（AI 记一笔/对话改单共用，yuan_to_cents 的前置步骤）：
    保留数字与小数点/负号，剥掉币符（¥30）、单位（5000元）、空白等一切杂字符。
    「万」按倍数折算、孤数字尾按千（1.5万=15000、1万5=15000，与 fix_local 的
    _local_amount 同口径）——此前一律剥字，"1.5万" 成 1.5 元，差一万倍且无任何提示。
    只用于自由文本里的口说金额——导入账单单元格走 yuan_to_cents 自带的 NFKC+币符
    清洗（账单列不会出现「万」写法，不混用）。"""
    m = _WAN_RE.search(v)
    if m:
        return str(Decimal(m.group(1)) * 10000 + Decimal(m.group(2) or 0) * 1000)
    return "".join(ch for ch in v if ch.isdigit() or ch in ".-")

def yuan_to_cents(v) -> int | None:
    """金额（元）→ 分的统一换算（全应用单点收口：导入解析 / AI 草稿 / 票据 OCR / 对话改单共用）。
    NFKC 全角归一（１２．３４／－）+ 去币符/千分位后走 Decimal（十进制精确）；
    失败或非有限值（""／"N/A"／"nan"／"1e999"→inf）返回 None，0 是合法零额
    （None 与 0 语义分离，与 parse_cents 一致）。此前五处各自防御、口径不一：
    漏 OverflowError 或漏 nan 都曾把脏值穿成 500 / 非法 JSON——统一到这里一次防全。
    Decimal 而非 float 的取整：1.005*100 在二进制浮点是 100.4999…，裸 round 得 100 分
    与前端 toCents（toPrecision(12)）的 101 分答案分叉——十进制 + HALF_UP 两端一致。"""
    try:
        if isinstance(v, bool):
            return None   # bool 是 int 子类：True→100 分无意义，按解析不了处理
        if isinstance(v, int):
            return v * 100
        s = amount_clean(v).strip().replace(" ", "")
        d = Decimal(s) * 100
        return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if d.is_finite() else None
    except (ValueError, TypeError, ArithmeticError):
        # InvalidOperation/ArithmeticError=空串/非数字/nan/inf（Decimal 不做 float 溢出，无 OverflowError）
        return None

def cents_loose(v) -> int | None:
    """口说金额（元）→ 分的会话侧入口：字符串先过 strip_amount_str（"¥30"/"5000元"/
    "1.5万"），再走 yuan_to_cents 单点换算（None=空/解析不了/nan/inf）。AI 记账条目
    与 bot 对话改单共用——此前两处各抄一份两行式，靠注释互相同步。"""
    if isinstance(v, str):
        v = strip_amount_str(v)
    return yuan_to_cents(v)

def amount_clean(s) -> str:
    """金额字符串清洗（yuan_to_cents 与 import_bill._amount_str 共用一份）：
    NFKC 全角归一（１２．３４／－／，）+ 去币符/千分位。"""
    s = unicodedata.normalize("NFKC", str(s if s is not None else ""))
    for ch in ("¥", "￥", ","):
        s = s.replace(ch, "")
    return s

def normalize(field) -> str:
    """trim（含京东/美团单元格尾部 \\t）。"""
    return str(field or "").strip().replace("\t", "")

_IN_CHUNK = 500

def _chunks(seq, size: int = _IN_CHUNK):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]

def _is_placeholder_name(name: str) -> bool:
    """占位名判定：微信账单「商品」列无内容时常写作 "/"（红包/转账行普遍如此），这类占位名
    不参与名称匹配：否则两条同样写作 "/" 的行会因「金额相等 + 前缀相等」被判为高置信，
    把退款关联到无关的红包/转账行。"""
    return not name.strip("/-—\\ \u3000")

def is_meaningless_name(name: str) -> bool:
    """名称是否无意义：空 / 占位符（"/"、"-"）/ 微信红包·转账这类泛化值。
    用于「对方优先 + AI 兜底」的名称回填判定。"""
    n = normalize(name)
    return _is_placeholder_name(n) or n in pt.WX_GENERIC_NAMES


def _is_new_row(p: dict) -> bool:
    """预览行是否入库（server 计数 / 退款候选池 / apply 两处共用）：非精确重复且未被跳过
    （skip 含跨源/手动配对/无效行）。"""
    return not p.get("duplicate") and not p.get("skip")


# 「中国」前缀归一白名单：官方名称带「中国」的银行（中国农业银行≡农业银行…）。
# 「中国银行」剥后只剩「银行」不在名单，保持原样——它本身就是行名
_BANK_AFTER_ZHONGGUO = ("农业银行", "建设银行", "工商银行", "交通银行", "邮政储蓄",
                        "光大银行", "民生银行", "中信银行", "招商银行")

def norm_account_name(name: str) -> str:
    """账户名归一（自动导入的同一账户多写法合并判定共用）：
    全角括号→半角、去空白、「中国」前缀按白名单归一、借记卡≡储蓄卡。
    例：中国农业银行储蓄卡（3878） ≡ 农业银行储蓄卡(3878) ≡ 农业银行借记卡(3878)。"""
    s = normalize(name).replace("（", "(").replace("）", ")")
    s = s.replace(" ", "").replace("\u3000", "")
    if s.startswith("中国") and any(s[2:].startswith(b) for b in _BANK_AFTER_ZHONGGUO):
        s = s[2:]
    return s.replace("借记卡", "储蓄卡")
