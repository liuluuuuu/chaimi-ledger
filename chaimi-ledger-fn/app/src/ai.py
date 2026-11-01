"""AI 记账（DESIGN 六章）：自然语言记账 / 小票图片 / 月度分析 / 对话查账 + 导入分类批量兜底。

隐私哲学：账本明细不出网——只发用户一句话/一张图/聚合统计；分类兜底只发
「一级分类+子类清单+备注/商户/金额」。
"""
from __future__ import annotations

import base64
import datetime
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator

from openai import OpenAI

import applog
import config
import db as dbm

_log = applog.get("ai")   # 批处理决策日志：每次兜底的行数/批数/耗时直接可读（日志包排查用）

# 记账规则版本：随 ai.py 变更升级，由 server 启动日志打印（applog.setup 之后），
# 用于核对线上跑的是不是新代码——模块 import 早于 setup，此处打印只会进 stderr 不落盘
RULES_VERSION = "1.1.11"


_NO_KEY_HINT = ("未配置 LLM Key：请到「设置」页填写 API Key。"
                "配置后可用：一句话智能记账（自动分类、礼金人名识别、段时时间）、"
                "小票拍照记账、月度分析、对话查账；未配置时记账仅本地规则兜底，日期为今天。")

# 上游 LLM 报错 → 用户可读中文（"Error code: 400 - {...Arrearage...}" 这类原文用户看不懂；
# 按子串匹配，命中给定向提示，未命中原样截断附在通用提示后便于排查）
_ERR_ZH = (
    ("arrearage", "LLM 服务账户欠费（阿里云百炼）：请到百炼控制台「费用中心」结清欠费后重试，"
                  "或在设置页更换服务商 Key（智谱 / DeepSeek / 本地模型）"),
    ("invalid_api_key", "API Key 无效或已过期：请到设置页检查 Key 是否填对"),
    ("incorrect api key", "API Key 无效或已过期：请到设置页检查 Key 是否填对"),
    ("model_not_exist", "所选模型不存在或无权限使用：请到设置页重新选择模型"),
    ("rate limit", "请求太频繁或额度用尽：请稍后再试"),
    ("timeout", "请求模型服务超时：请检查网络后重试"),
    ("timed out", "请求模型服务超时：请检查网络后重试"),
    ("connection", "无法连接模型服务：请检查网络或本地模型服务是否在线"),
)


def _zh_error(e) -> str:
    raw = str(e)
    low = raw.lower()
    for key, zh in _ERR_ZH:
        if key in low:
            return zh
    return "AI 服务异常：" + raw[:200]


def _client() -> OpenAI | None:
    sec = config.load().get("llm", {})
    if not sec.get("api_key"):
        return None
    return OpenAI(base_url=sec["base_url"], api_key=sec["api_key"])


def _vl_client() -> OpenAI | None:
    """视觉（小票 OCR）客户端：vl_base_url/vl_api_key 配了就用独立的，否则共用文本的。"""
    sec = config.load().get("llm", {})
    key = sec.get("vl_api_key") or sec.get("api_key")
    if not key:
        return None
    return OpenAI(base_url=sec.get("vl_base_url") or sec["base_url"], api_key=key)


def _host(url: str) -> str:
    """base_url → 域名(剥协议/路径/端口,Ollama 等本地地址带端口)。空值给 ? 供错误信息占位"""
    return (url or "").split("//")[-1].split("/")[0].split(":")[0] or "?"


# ---------- 模型自检（启动后台）：经 /models 核对配置模型是否仍存在，不在按候选序切换 ----------

# 各预设服务商（settings.js LLM_PRESETS）的候选模型序，前者优先；列表外的 id 不匹配自然跳过。
# 服务商下线旧模型（如百炼 2026-10-10 下线一代 qwen-vl-*）时按此自动换新
_MODEL_RANKS = {
    "dashscope.aliyuncs.com": {
        "txt": ("qwen-plus", "qwen3.7-plus", "qwen3.8-plus", "qwen3.8-flash", "qwen-turbo"),
        "vl": ("qwen3-vl-plus", "qwen3-vl-max", "qwen3-vl-flash", "qwen-vl-max", "qwen-vl-plus"),
    },
    "open.bigmodel.cn": {
        "txt": ("glm-4-air", "glm-4-flash", "glm-4-plus"),
        "vl": ("glm-4v-flash", "glm-4v-plus"),
    },
    "api.deepseek.com": {
        "txt": ("deepseek-chat", "deepseek-reasoner"),
        # 2026-08 DeepSeek 上线视觉（官方指南：deepseek-flash 收图；旧名 v4-flash-vision-exp
        # 已弃用但仍承接请求），与图示的 deepseek-chat/V3-0324 文本映射并存
        "vl": ("deepseek-flash", "deepseek-v4-flash-vision-exp"),
    },
}

# 预设发布时视觉默认为空的服务商域名：这些域上存量配置的空 vl_model 是「当时没有」
# 而非「用户关闭」，服务商上线视觉后 ensure_models 据此回填
_VL_BACKFILL_HOSTS = {"api.deepseek.com"}


def _list_models(base_url: str, api_key: str) -> list[str] | None:
    """OpenAI 兼容 GET /models。失败（不支持/网络/Key）返回 None=无法核对，调用方保持原配置。"""
    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=5, max_retries=0)
        return [m.id for m in client.models.list()]
    except Exception:
        return None


def ensure_models() -> list[str]:
    """核对配置的文本/视觉模型在服务商可用列表里是否仍存在；不在则按候选序切到
    第一个存在的并落盘。返回日志行（无变更为空）。未知服务商不猜、无法核对不动。
    视觉额外一条：vl_model 为空且服务商在 _VL_BACKFILL_HOSTS（曾以「无视觉」发布的
    预设域）时，空=当时没有而非用户关闭，按候选序回填。"""
    cfg = config.load()
    llm = cfg["llm"]
    notes: list[str] = []

    def pick(kind: str, model: str, base_url: str, key: str, backfill: bool = False) -> str | None:
        host = _host(base_url)
        ranks = _MODEL_RANKS.get(host)
        if not ranks or not key or (not model and not (backfill and kind == "vl"
                                                       and host in _VL_BACKFILL_HOSTS)):
            return None
        ids = _list_models(base_url, key)
        if model and (ids is None or model in ids):
            return None
        return next((c for c in ranks[kind] if ids and c in ids), None)

    new_txt = pick("txt", llm.get("model") or "", llm["base_url"], llm.get("api_key") or "")
    if new_txt:
        notes.append(f"[llm] 文本模型 {llm['model']} 已不在可用列表，自动切换为 {new_txt}")
        llm["model"] = new_txt
    vl_url = llm.get("vl_base_url") or llm["base_url"]
    vl_empty = not llm.get("vl_model")
    new_vl = pick("vl", llm.get("vl_model") or "", vl_url,
                  llm.get("vl_api_key") or llm.get("api_key") or "", backfill=vl_empty)
    if new_vl:
        notes.append((f"[llm] 视觉模型为空，{_host(vl_url)} 已上线视觉，自动填入 {new_vl}" if vl_empty
                      else f"[llm] 视觉模型 {llm['vl_model']} 已不在可用列表，自动切换为 {new_vl}"))
        llm["vl_model"] = new_vl
    if notes:
        config.save(cfg)
    return notes


# 模型分组（设置页「模型列表」按钮）：名字含 vl/4v/vision 视为视觉；非对话类（向量/语音/绘图等）滤掉
_VL_PAT = re.compile(r"vl|4v|vision", re.I)
# 名字不带 vl/vision 字样但确为视觉的已知模型（deepseek-flash：DeepSeek 官方图像理解指南口径）
_VL_KNOWN = {"deepseek-flash"}
_NOISE_PAT = re.compile(r"embedding|embed|rerank|tts|asr|audio|speech|voice|paraformer|sambert"
                        r"|wanx|wan2|t2v|i2v|cogview|cogvideo|generation", re.I)


def list_models_grouped() -> dict:
    """经 /models 拉取可用模型并按 文本/视觉 分组：{"text":[...], "vision":[...], "error":"..."}。
    视觉配了独立 API 时两家列表合并去重；无法核对的一侧在 error 里说明。"""
    llm = config.load()["llm"]
    txt_ids = _list_models(llm["base_url"], llm.get("api_key") or "") if llm.get("api_key") else None
    vl_key = llm.get("vl_api_key") or llm.get("api_key") or ""
    vl_url = llm.get("vl_base_url")
    vl_ids = (_list_models(vl_url, vl_key) if vl_key else None) if vl_url else txt_ids
    text: list[str] = []
    vision: list[str] = []
    for mid in sorted({m for lst in (txt_ids, vl_ids) if lst for m in lst}):
        if not _NOISE_PAT.search(mid):
            (vision if (_VL_PAT.search(mid) or mid in _VL_KNOWN) else text).append(mid)
    if not llm.get("api_key") and not vl_key:
        return {"text": [], "vision": [], "error": "未配置 API Key"}
    errs = []
    if not llm.get("api_key"):   # 只配了视觉 Key：文本侧根本没发请求，是「未配置」而非「获取失败」
        errs.append("文本 API 未配置 Key")
    elif txt_ids is None:
        errs.append("文本 API 列表获取失败")
    if vl_url and vl_ids is None:   # 仅独立视觉 API 才单列其失败;共用时归并到文本 API
        errs.append("视觉独立 API 列表获取失败")
    fail = any(e.endswith("获取失败") for e in errs)
    return {"text": text, "vision": vision,
            "error": "；".join(errs) + ("（Key 无效或服务商不支持 /models）" if fail else "")}


def _chat_json(client: OpenAI, prompt: str, system: str = "") -> dict | None:
    """返回 LLM 响应解析后的 dict；失败（网络/Key/超时/LLM 返回非 JSON）统一返回 None，
    由上层兜底（parse_booking 提示"AI 解析失败"、_batch_llm 丢批）。"""
    try:
        resp = client.chat.completions.create(
            model=config.load()["llm"]["model"],
            messages=([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content or "{}")
    except Exception:
        return None


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def sse_stream(events: Iterator[dict]) -> Iterator[str]:
    """结构化事件 → SSE 文本（HTTP 边界统一序列化；bot 通道直接消费事件流，不再二次解析）。"""
    for ev in events:
        yield _sse(ev)
    yield "data: [DONE]\n\n"


# 批并发数：批调用纯网络等待（GIL 释放），6 路把大账单兜底耗时除以~6；再高会撞限流
_BATCH_CONCURRENCY = 6


def _batch_llm(client: OpenAI, pending: list, row_fn, prompt_fn) -> Iterator[tuple]:
    """批处理脚手架（分类兜底/名称兜底共用）：50 行/批 → 拼 rows JSON（no=批内序号）
    → _chat_json 线程池并发跑批 → 按原批序回放，逐行产出 (行, 模型返回值)。
    prompt 提交前拼好（worker 只做网络调用，无共享写）；单批失败丢该批不阻断。"""
    chunks = [pending[i:i + 50] for i in range(0, len(pending), 50)]
    _log.info(f"AI 批：{len(pending)} 行 / {len(chunks)} 批 × {_BATCH_CONCURRENCY} 并发")
    prompts = [prompt_fn([{"no": j, **row_fn(p)} for j, p in enumerate(chunk)])
               for chunk in chunks]
    with ThreadPoolExecutor(max_workers=_BATCH_CONCURRENCY) as pool:
        for chunk, out in zip(chunks, pool.map(lambda pr: _chat_json(client, pr), prompts)):
            if not out:
                continue
            for j, p in enumerate(chunk):
                yield p, out.get(str(j))


# ---------- 自然语言记账 ----------

# 就倾向选 transfer → 变成不计收支，与导入侧口径不一致（见 platform_templates.WX_TRANSFER_TYPES）
_TYPE_RULE = (
    "type 判断规则（重要）：转给别人、给别人发红包算 expense（如「给大宝转了100」「给小李发红包50」）；"
    "别人转给我、收到转账/红包算 income（如「小明给我转了50」「收到小王转账」）；"
    "只有自有账户之间搬钱才用 transfer（还信用卡、买理财/余额宝、给零钱充值、零钱提现），"
    "这类没有真实收支发生。"
)

_AMOUNT_DATE_RULE = (
    "金额与日期规则：金额词形不限「花了/去了/买了/随了/付了/消费了」等，句中任何紧跟数字的消费金额都填进 amount，"
    "数字后可省略「块/元」（「买了52零食」amount=52、name=零食）；「万」要换算（「随了1万5礼金」amount=15000）。"
    "日期：相对词（昨天/前天/上周五）按今天换算；「10月3号」这类缺年份的默认今年，"
    "若换算出的日期在今天之后则取去年（记的都是已发生的事）；带「上个月/去年」的按之换算。"
    "句首的日期/时间适用于句中所有笔（「昨天买早餐5块 午饭15」两笔都是昨天）。"
    "带时段词时 date 输出 YYYY-MM-DD HH:MM，映射：早上→08:00、上午→09:00、中午/午饭/午餐→12:00、"
    "下午→14:00、晚上→18:00（「10月3号中午吃了15」date=当年10月3日 12:00）；无时段词只给日期。"
)

# 账户选择规则（BOOKING_SYSTEM / CHAT_TOOLS / 对话 system 三处共用，单源防口径漂移）
_ACCOUNT_RULE = "用户说微信/支付宝/刷卡等支付方式时选对应账户，解析不到留空"

# 礼金/份子钱规则（记账解析与对话工具共用）：name 必须是人名，note 是喜事对象和事由
_GIFT_RULE = (
    "礼金/份子钱场景（无论收出）name 填对方的人名或称呼，note 填喜事对象和事由，"
    "人名+结婚/满月/白事+金额即是礼金场景，句中没出现「人情/份子钱」字样也一样；"
    "不要在 name 里填礼金/份子钱这类泛化词，也不要把整句塞进 note："
    "「10月3号小明儿子结婚去了5000」→ expense、5000、name=小明、note=儿子结婚；"
    "「收到小李结婚礼金600」→ income、600、name=小李、note=结婚礼金。"
)

BOOKING_SYSTEM = (
    "你是记账解析器。把用户的一句话解析成 JSON。一句话里可能有多笔（「早上过早5块 中午吃饭20块」是两笔），"
    "必须逐笔拆分；只有一笔也放进 items 数组。只输出 JSON。输出格式："
    '{"items":[{...}]}，每个 item 字段：'
    'type(expense/income/transfer)、amount(数字,元)、category_name(分类名)、'
    'account(账户名,从可用账户清单里选;' + _ACCOUNT_RULE + ')、'
    'date(YYYY-MM-DD,相对词如昨天要换算)、'
    'name(简短品项名,必填:早餐/午餐/打车/加油/工资这类词)、'
    'note(补充信息,可空:商家/门店/地点/事由)。'
    "例：「早上在肯德基吃了5块钱的早餐」→ name=早餐、note=肯德基；"
    "「早上吃了5块钱的早饭」→ name=早餐、note=空；"
    "「10月发工资8500」→ type=income、name=工资。"
    + _GIFT_RULE
    + _AMOUNT_DATE_RULE + _TYPE_RULE
)


def _booking_context(ledger_id: int) -> tuple[str, str, str]:
    """记账上下文：一级分类清单 + 可用账户清单 + 最近 50 条备注→分类少样本（parse_booking 与 chat 共用）。"""
    conn = dbm.connect()
    samples = conn.execute(
        "SELECT note, c.name FROM transactions t LEFT JOIN categories c ON t.category_id=c.id "
        "WHERE t.ledger_id=? AND t.note != '' ORDER BY t.id DESC LIMIT 50", (ledger_id,)).fetchall()
    few = chr(10).join(f"{s['note']}→{s['name']}" for s in samples if s["name"])
    cats = conn.execute(
        "SELECT name FROM categories WHERE ledger_id=? AND parent_id IS NULL", (ledger_id,)).fetchall()
    accs = conn.execute(
        "SELECT name FROM accounts WHERE ledger_id=? ORDER BY sort, id", (ledger_id,)).fetchall()
    return ("、".join(c["name"] for c in cats),
            "、".join(a["name"] for a in accs), few)


def _member_roster(conn, ledger_id: int) -> str:
    """成员（用户）清单，与统计页归属口径一致：用户=attribution_groups（顶栏记账人，
    手动/AI 记账默认归属其名，不经 attributions 行——只查 attributions 会漏掉纯手动账本
    的全部用户）；未归入用户的原始归属（平台导入产生）单列。用户在前，去重保序。"""
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM attribution_groups WHERE ledger_id=? ORDER BY sort, id", (ledger_id,))]
    names += [r["name"] for r in conn.execute(
        "SELECT a.name FROM attributions a WHERE a.ledger_id=? AND a.group_id IS NULL "
        "ORDER BY a.id", (ledger_id,))]
    return "、".join(dict.fromkeys(names))   # 去重保序


def _resolve_account(ledger_id: int, name) -> int | None:
    """LLM 给的账户名 → 账户 id：先精确，再包含匹配（说「微信」→ 账户「微信支付」）。
    解析不到返回 None（前端草稿卡回落未指定/默认现金）。"""
    if not name or not str(name).strip():
        return None
    name = str(name).strip()
    rows = dbm.connect().execute(
        "SELECT id, name FROM accounts WHERE ledger_id=? ORDER BY sort, id", (ledger_id,)).fetchall()
    for r in rows:
        if r["name"] == name:
            return r["id"]
    for r in rows:
        if name in r["name"] or r["name"] in name:
            return r["id"]
    return None


#   收侧(收了/收到/到账/进账/报销/退款)一律收入;
#   发侧(发了/发工资)为收入,但「发红包」是给别人发 → 支出
_CUE_IN = ("收了", "收到", "到账", "进账", "报销", "退款")
_CUE_FA = ("发了", "发工资")


def _income_fix(text: str, items: list):
    """收支方向收口：
    单笔 + 原句含收入动词（发了/发工资/收到/收了/到账/进账）→ income;
    「发红包」例外（给别人发=支出，与 _TYPE_RULE 同口径）。多笔句不收口，防误伤。"""
    if len(items) != 1:
        return
    it = items[0]
    if (it.get("type") != "expense"
            or not any(c in text for c in _CUE_IN + _CUE_FA)
            or ("红包" in text and "收到" not in text and "收了" not in text)):
        return
    it["type"] = "income"


# 喜事短语：可选辈分 + 场合词；人名取其前紧邻的 1-4 个汉字（去掉号/日/月/年等日期残字）
_OCCASION_RE = re.compile(
    r"(?:儿子|女儿|孙子|孙女|外孙(?:子|女)?)?(?:结婚|大婚|办?婚礼|出嫁|嫁女|千金|满月|百日宴?|丧事|白事|办?葬礼|乔迁|升学宴?|生日)")
_DATE_TAIL = "0123456789号日月年点时分周晚"
# 「参加了同学婚礼宴席AA制200」「给儿子买了结婚对戒8000」这类正常消费会被旁路误伤；
# 真实礼金句（无论收出）实测都自带指示词，旁路并无必要。
_GIFT_HINTS = ("人情", "礼金", "份子", "随了", "上了", "去了", "封了", "包了", "给了", "送了", "贺了")

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "两": 2}
_PERIOD_HM = [("早上", "08:00"), ("早晨", "08:00"), ("清晨", "07:00"), ("上午", "09:00"),
              ("中午", "12:00"), ("午饭", "12:00"), ("午餐", "12:00"), ("午后", "13:00"),
              ("下午", "14:00"), ("傍晚", "17:00"), ("晚上", "18:00"), ("晚饭", "18:00"),
              ("晚餐", "18:00"), ("夜宵", "22:00"), ("半夜", "23:00"), ("深夜", "23:00")]
_TIME_RE = re.compile(r"(?:(\d{1,2})[点:：时]|([一二三四五六七八九十两]{1,3})[点时])(?:(\d{1,2})分?)?")


def _hour_minute(text: str) -> str | None:
    """句中显式时刻（8点/8:30/下午三点半）→ HH:MM；无显式时刻再看时段词。"""
    m = _TIME_RE.search(text)
    pm = any(w in text for w in ("下午", "傍晚", "晚上", "晚饭", "晚餐", "夜宵", "半夜", "深夜"))
    if m:
        if m.group(1):
            h = int(m.group(1))
        else:
            cn = m.group(2)
            h = _CN_NUM.get(cn[0], 0) if len(cn) == 1 else (
                10 + _CN_NUM.get(cn[-1], 0) if cn[0] == "十" else _CN_NUM.get(cn[0], 0) * 10 + _CN_NUM.get(cn[-1], 0))
        if 1 <= h <= 12 and pm and h < 12:
            h += 12
        mm = int(m.group(3)) if m.group(3) else (30 if text[m.end():m.end() + 1] == "半" else 0)
        return f"{h:02d}:{mm:02d}" if 0 <= h <= 23 and mm < 60 else None
    for w, hm in _PERIOD_HM:
        if w in text:
            return hm
    return None


def _gift_fix(text: str, it: dict):
    """礼金场景字段收口：name 必须是对方人名/称呼，note 是喜事短语。命中即以规则为准（LLM 极易给泛化名）。
    门槛：句中有礼金指示词（去了/上了/随了/礼金…）。「参加婚礼宴席AA制」「给儿子买结婚对戒」
    这类正常消费没有指示词，不进礼金分支。"""
    m = _OCCASION_RE.search(text)
    if not m or not any(h in text for h in _GIFT_HINTS):
        return
    person = re.search(r"[\u4e00-\u9fa5]{1,4}$", text[:m.start()])
    name = person.group(0).lstrip(_DATE_TAIL) if person else ""
    name = re.sub(r"^(收到|收了|给了|回了|恭喜|恭贺|给|送)", "", name)
    if not name or name == "我" or name == m.group(0):
        return
    it["name"] = name
    it["note"] = m.group(0)


def _time_fix(text: str, it: dict):
    """10 位日期 + 句中时段/时刻 → 补 HH:MM（LLM 已带时刻则不动）。"""
    d = str(it.get("date") or "")
    if len(d) == 10:
        hm = _hour_minute(text)
        if hm:
            it["date"] = f"{d} {hm}"


def _kw_fallback_category(it: dict) -> None:
    """条目缺分类名时的关键词兜底（离线正则兜底 / LLM 漏给分类共用）：
    复用导入侧同一部 TOP_KEYWORD_LEXICON——早餐→餐饮美食、打车→交通出行、
    红包按小额两级归类，导入与 AI 记一笔口径天然一致。"""
    if it.get("category_name"):
        return
    import import_bill as ib
    kw = ib._top_by_keyword(f"{it.get('name','')} {it.get('note','')}", it.get("amount"))
    if kw:
        it["category_name"] = kw

def _booking_items(raw_items, ledger_id: int | None = None, text: str = "") -> list:
    """条目草稿规范化：只保留有金额的 dict，元换算成分（与前端 amount 单位一致）；
    account 账户名解析成 account_id，解析不到留 None；传入原句做礼金人名/时段时刻的规则化收口。"""
    items = [it for it in (raw_items or []) if isinstance(it, dict) and it.get("amount") is not None]
    for it in items:   # 元 → 分（带单位/货币符号的字符串先清洗："5000元" → 5000）
        try:
            amt = it["amount"]
            if isinstance(amt, str):
                amt = "".join(ch for ch in amt if ch.isdigit() or ch in ".-")
            it["amount"] = round(float(amt) * 100)
        except (TypeError, ValueError):
            it["amount"] = 0
        it["account_id"] = _resolve_account(ledger_id, it.get("account")) if ledger_id else None
        # 未提账户 → 默认「现金」（建账种子账户，被删/改名则不硬填）。
        # 提了账户但解析不到的**不**硬填：「用银行卡付」没有对应账户时填现金反而是错，
        # 留给用户在草稿/编辑里补——微信 bot 直连无编辑界面，靠确认后的待确认/编辑弹窗兜底。
        if ledger_id and it["account_id"] is None and not str(it.get("account") or "").strip():
            it["account_id"] = _resolve_account(ledger_id, "现金")
        # 礼金收口仅限单笔句：_gift_fix 按整句判定，多笔句（「小明儿子结婚随了500 午饭15」）
        # 会把其他笔也改写成人名/事由（与 _income_fix 的多笔守卫同口径）
        if len(items) == 1:
            _gift_fix(text, it)
        # 日期：宽松写法归一 ISO（_time_fix 按 10 位判定，卡片日期框只认 ISO）；认不出/缺省
        # 按今天——与 App 卡片、bot 确认入库的兜底同口径，同日重复提示（dup_hints）才对得上
        it["date"] = dbm.iso_date_ok(str(it.get("date") or "")) or datetime.date.today().isoformat()
        _time_fix(text, it)
        _kw_fallback_category(it)
    if items:
        _income_fix(text, items)
    if items and ledger_id:
        dbm.dup_hints(dbm.connect(), ledger_id, items)
    return items


def parse_booking(text: str, ledger_id: int, today: str) -> dict:
    """自然语言 → 结构化流水预览（6.3），支持一句话多笔；注入该账本最近 50 条备注→分类做少样本。"""
    client = _client()
    if not client:
        return {"error": _NO_KEY_HINT}
    cat_list, acc_list, few = _booking_context(ledger_id)
    nl = chr(10)
    prompt = ("今天是 " + today + "。可用分类：" + cat_list + "。可用账户：" + acc_list + "。" + nl
              + "用户历史记账习惯（备注→分类）：" + nl + few + nl + nl + "用户输入：" + text)
    out = _chat_json(client, prompt, BOOKING_SYSTEM)
    # 兼容两种返回：{"items":[...]}（标准）/ 单个对象（LLM 未按格式输出时的兜底）
    raw = out.get("items") if isinstance(out, dict) else None
    if not isinstance(raw, list) or not raw:
        raw = [out] if isinstance(out, dict) and "amount" in out else []
    items = _booking_items(raw, ledger_id, text)
    return {"items": items, "text": text} if items else {"error": "AI 解析失败，请稍后重试"}


def resolve_subcategories_batch(previews: list, ledger_id: int | None = None) -> int:
    """AI 兜底分类：对 classified_via=raw 且待确认的行，从一级分类清单里选（50 行/批）。"""
    t0 = datetime.datetime.now()
    pending = [p for p in previews if not p.get("skip") and not p.get("duplicate")
               and p["type"] != "transfer" and p.get("classified_via") == "raw"
               and p.get("need_confirm")]
    if not pending:
        return 0
    client = _client()
    if not client:
        return 0
    tops = {}
    if ledger_id:
        conn = dbm.connect()
        for c in conn.execute(
                "SELECT id, name, type FROM categories WHERE ledger_id=? AND parent_id IS NULL", (ledger_id,)):
            tops[(c["type"], c["name"])] = c["id"]
    n = 0
    by_type = {}
    for p in pending:
        by_type.setdefault(p["type"], []).append(p)
    nl = chr(10)
    for ftype, group in by_type.items():
        names = [t for (t2, t) in tops if t2 == ftype]
        type_cn = "支出" if ftype == "expense" else "收入"
        for p, item in _batch_llm(
                client, group,
                lambda p: {"name": (p.get("name") or "")[:60], "merchant": (p.get("merchant") or "")[:20],
                           "note": (p.get("note") or "")[:30], "amount": p["amount"] / 100},
                lambda rows: ("以下是记账软件的一级分类清单：" + nl + "、".join(names) + nl
                              + "请逐笔仔细阅读 name/merchant/note（含商户名、车牌号等线索），"
                              "为每笔" + type_cn + "流水选择最合适的一级分类（必须从上面列表选，无合适选其他）。"
                              "不要把非餐饮消费归入餐饮美食。" + nl
                              + "流水：" + json.dumps(rows, ensure_ascii=False) + nl
                              + '返回 JSON：{"0": "一级分类", ...}，'
                              + "键必须严格等于各行 no，不要遗漏或新增。")):
            parent = str(item if isinstance(item, str) else (item or {}).get("parent", "")).strip()
            if not (parent and (not names or parent in names)):
                continue   # 无效答案整行跳过；行已是待确认态（pending 过滤保证），无需再标
            text = f"{p.get('name','')} {p.get('merchant','')} {p.get('note','')}"
            # 重切「红包礼金」（＜200 的红包/转账红包不应建议人情往来）
            if parent == "人情往来":
                split = "红包礼金" if dbm.is_small_generic_packet(text, p.get("amount")) else "人情往来"
                if split != parent and tops.get((ftype, split)):
                    parent = split
            p["parent_category"] = parent
            cid = tops.get((ftype, parent))
            if cid:
                p["category_id"] = cid
            p["category_name"] = parent
            p["classified_via"] = "ai"
            # 与导入侧统一口径（renqing_need_confirm 收口）：人情往来直接入库，
            # 红包礼金带拼手气/群红包字样直接入库，其余小额红包仍待确认
            p["need_confirm"] = (parent == "红包礼金" and dbm.renqing_need_confirm(text))
            n += 1
    _log.info(f"AI 兜底·分类：{len(pending)} 行命中 {n}，耗时 {(datetime.datetime.now()-t0).total_seconds():.1f}s")
    return n


def resolve_names_batch(previews: list) -> int:
    """AI 名称兜底：微信红包/转账行的「商品」列写作 "/"、「微信红包」这类泛化值，
    规则层回填「交易对方」后仍无名称时，让 LLM 从交易类型/对方/备注里提取名称（50 行/批）。"""
    import import_bill as ib
    t0 = datetime.datetime.now()
    pending = [p for p in previews if not p.get("skip") and not p.get("duplicate")
               and p.get("platform") == "wechat" and ib.is_meaningless_name(p.get("name"))]
    if not pending:
        return 0
    client = _client()
    if not client:
        return 0
    nl = chr(10)
    n = 0
    for p, item in _batch_llm(
            client, pending,
            lambda p: {"交易类型": (p.get("map_source") or "")[:20],
                       "交易对方": (p.get("merchant") or "")[:30], "备注": (p.get("note") or "")[:60],
                       "金额": p["amount"] / 100,
                       "收/支": "支出" if p["type"] == "expense" else ("收入" if p["type"] == "income" else "不计收支")},
            lambda rows: ("以下是微信账单里的红包/转账流水，「商品」列为空或 \"/\"、「微信红包」等泛化值，名称缺失。" + nl
                          + "请根据「交易对方」「备注」「交易类型」推断这笔往来对应的名称，规则：" + nl
                          + "1) 取对方的人名/昵称（如“张三”“老王”），去掉“转账”“红包”“微信红包”等类型词；" + nl
                          + "2) 群红包可提取群名；3) 备注里写了事由/对象时以备注为准；" + nl
                          + "4) 只有泛化词、确实判断不出对象时返回空字符串，不要编造。" + nl
                          + "流水：" + json.dumps(rows, ensure_ascii=False) + nl
                          + '返回 JSON：{"0": "名称", ...}，键必须严格等于各行 no，判断不出的填 ""。')):
        nm = str(item or "").strip().strip('"“”')
        if not nm or ib.is_meaningless_name(nm):
            continue
        p["name"] = nm[:40]
        n += 1
    _log.info(f"AI 兜底·名称：{len(pending)} 行命中 {n}，耗时 {(datetime.datetime.now()-t0).total_seconds():.1f}s")
    return n


def _stream(client: OpenAI, messages: list, model: str) -> Iterator[str]:
    resp = client.chat.completions.create(model=model, messages=messages, stream=True)
    for chunk in resp:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            yield delta


def monthly_analysis(ledger_id: int, month: str) -> Iterator[dict]:
    """月度洞察事件流（结构化 dict，sse_stream 序列化 / bot 直消费）：只发聚合统计（非逐条流水）。"""
    client = _client()
    if not client:
        yield {"error": _NO_KEY_HINT}
        return
    conn = dbm.connect()
    summary = dbm.stats_summary(conn, ledger_id, month)
    cats = dbm.stats_category(conn, ledger_id, month)[:12]
    arows = dbm.attribution_rows(conn, ledger_id, *dbm.month_range(month))
    mem_txt = "；".join(f"{r['person']} {r['expense']/100:.2f}" for r in arows[:8] if r["expense"])
    stat_text = (
        f"{month} 汇总：毛支出 {summary['gross_expense']/100:.2f} 元，退款 {summary['refund_deduction']/100:.2f} 元，"
        f"净支出 {summary['net_expense']/100:.2f} 元，收入 {summary['income']/100:.2f} 元。\n"
        "分类净支出（Top12）：" + "；".join(f"{c['name']} {c['net']/100:.2f}" for c in cats)
        + (f"\n成员支出（Top8）：{mem_txt}" if mem_txt else ""))
    model = config.load()["llm"]["model"]
    try:
        for piece in _stream(client, [
            {"role": "system", "content": "你是个人财务分析师。基于给定统计数据输出简洁的月度分析：总体概况、"
                                          "Top 分类点评、成员支出点评（给了成员数据时）、异常点、下月节流建议。"
                                          "用中文，条理清晰，不超过 300 字。"},
            {"role": "user", "content": stat_text}], model):
            yield {"text": piece}
    except Exception as e:
        yield {"error": _zh_error(e)}


CHAT_TOOLS = [{
    "type": "function",
    "function": {
        "name": "query_stats",
        "description": "查询账本统计数据。支持：汇总(summary)、分类明细(category)、日历按日(calendar)、"
                       "成员归属(attribution，每人支出/收入+人数+人均+每人差额，问『几个人/每位成员/每人/人均/"
                       "平均/AA/谁花得最多/谁垫付』时用，旅行账单算 AA 必用)、按月趋势(trend，每月毛支出/收入/"
                       "净支出序列，问『趋势/每月花多少/最近几个月对比』)、人情往来(renqing，每人给出/收到/"
                       "未还差额，问『人情/随礼/谁没还礼』)、账户(account，每账户支出，问『每个账户/某账户"
                       "花了多少』，type=income 时为每账户收入）、预算(budget，月预算×月数折算+剩余+是否超支，问『预算/还剩多少/超支没』)。"
                       "range=month 按月（默认）、year 按年、all 账本全部历史、custom 自定义日期区间"
                       "（from/to 为起止日，含当天，用于『上周/本周/某几天/最近N天』等区间问题）。"
                       "用户问'总共/一共/全部/累计'时用 all，问'某年'时用 year。",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["summary", "category", "calendar", "attribution",
                                                    "trend", "renqing", "account", "budget"]},
                "range": {"type": "string", "enum": ["month", "year", "all", "custom"], "description": "统计范围，默认 month"},
                "month": {"type": "string", "description": "YYYY-MM，range=month 时生效，默认当月"},
                "year": {"type": "string", "description": "YYYY，range=year 时生效，默认当年"},
                "from": {"type": "string", "description": "YYYY-MM-DD，range=custom 时生效，区间起始日（含）"},
                "to": {"type": "string", "description": "YYYY-MM-DD，range=custom 时生效，区间结束日（含）"},
                "type": {"type": "string", "enum": ["expense", "income"], "description": "支出/收入侧，默认 expense；问收入/赚了/收了多少钱时必须用 income"},
                "category": {"type": "string", "description": "用户问到的分类名（如『餐饮』），有则必填：用于定位该分类的数据"},
            },
            "required": ["kind"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "create_transactions",
        "description": "用户描述已经发生的消费/收入/转账（记账意图）时调用：把这句话整理成待确认的记账条目草稿。"
                       "只产生草稿，确认由用户在前端卡片上完成，不要声称已经记好账，也不要再输出记账结果文字。",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "一句话里的多笔必须逐笔拆分；只有一笔也要放进数组",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["expense", "income", "transfer"]},
                            "amount": {"type": "number", "description": "金额，单位元"},
                            "category_name": {"type": "string", "description": "分类名，从可用分类里选"},
                            "account": {"type": "string", "description": "账户名，从可用账户清单里选（" + _ACCOUNT_RULE + "）"},
                            "date": {"type": "string", "description": "YYYY-MM-DD，相对词如昨天要换算"},
                            "name": {"type": "string", "description": "简短品项名，必填，如早餐/打车/加油（「在肯德基吃早餐」name=早餐）"},
                            "note": {"type": "string", "description": "补充信息可空：商家/门店/地点（「在肯德基吃早餐」note=肯德基）"},
                        },
                        "required": ["amount", "name"],
                    },
                },
            },
            "required": ["items"],
        },
    },
}]


def _y(v: int) -> float:
    """分值金额 → 元（保留两位）。"""
    return round(v / 100, 2)


def _attach(label: str, dfrom: str, dto: str, ft: str, cats: list, **extra) -> dict:
    """查账图表附件（前端 aiStatsCard 的契约：环形图 + 下钻键）；cats 喂分值，前端 /100。
    extra 携带维度标记（attribution/account=True 时前端换成成员/账户占比并禁用分类下钻）。"""
    return {"label": label, "from": dfrom, "to": dto, "flow": ft,
            "categories": cats, **extra}


def _yuan(obj):
    """把工具结果里的分值金额统一转成元，避免模型把分当元。"""
    if isinstance(obj, dict):
        return {k: _yuan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_yuan(x) for x in obj]
    if isinstance(obj, int):
        return _y(obj)
    return obj


def _settlement_plan(names: list, diffs: list) -> list:
    """AA 最少转账结算方案（经典贪心：最大应收↔最大应付配对），全程分值整数运算。
    前置条件：diffs 收支守恒（_attribution_tool 已把人均取整残差归一到支出最大者）。
    模型自行心算「谁给谁多少」易错（真机踩过：欠 273.75 给出 335 的方案），
    结算方案一律以此结果为准。"""
    creds = sorted(((n, d) for n, d in zip(names, diffs) if d > 0), key=lambda x: -x[1])
    debts = sorted(((n, -d) for n, d in zip(names, diffs) if d < 0), key=lambda x: -x[1])
    plan: list = []
    i = j = 0
    while i < len(creds) and j < len(debts):
        (nc, vc) = creds[i]
        (nd, vd) = debts[j]
        t = min(vc, vd)
        if t > 0:
            plan.append({"from": nd, "to": nc, "amount": t})
        creds[i] = (nc, vc - t)
        debts[j] = (nd, vd - t)
        if creds[i][1] <= 0:
            i += 1
        if debts[j][1] <= 0:
            j += 1
    return plan


def _attribution_tool(conn, ledger_id: int, dfrom: str, dto: str,
                      label: str, ft: str) -> tuple[str, dict | None]:
    """成员归属统计（人均/AA 场景）。金额显式走 _y 转元而不走 _yuan 递归——
    它见整数就 /100，cnt/人数这类计数字段会被误当分值。人均与差额在分值上取整再转元，避免浮点漂移。"""
    rows = dbm.attribution_rows(conn, ledger_id, dfrom, dto)
    side = "income" if ft == "income" else "expense"
    members = [{"name": r["person"], "cnt": r["cnt"],
                "expense": _y(r["expense"]), "income": _y(r["income"]),
                **({"group_members": [m["person"] for m in r["members"]]} if r["group"] else {})}
               for r in rows]
    vals = [r[side] for r in rows]
    total = sum(vals)
    n = len(rows)
    avg = round(total / n) if n else 0
    diffs = [v - avg for v in vals]
    if n and sum(diffs):
        # 人均取整残差（<n/2 分）记到支出最大者头上，diffs 恰好守恒——结算方案收支才能闭合
        diffs[max(range(n), key=lambda i: vals[i])] -= sum(diffs)
    for m, d in zip(members, diffs):
        m["diff"] = _y(d)          # 该成员-人均：正=多垫付（应收），负=少付（应补）
    result = {"label": label, "type": ft, "member_count": n, "total": _y(total),
              "average": _y(avg),
              "note": "金额单位元；average=总额÷有流水的成员数；diff=该成员-人均，"
                      "正数多垫付应收、负数少付应补；settlement=算好的最少转账结算方案"
                      "（谁给谁多少，收支已守恒），问 AA/结算时直接引用、不要自行计算；"
                      "区间内没流水的成员不在 members 里，算 AA 时人数按对话上下文的成员清单为准",
              "members": members,
              "settlement": [{"from": p["from"], "to": p["to"], "amount": _y(p["amount"])}
                             for p in _settlement_plan([m["name"] for m in members], diffs)]}
    attach = _attach(label, dfrom, dto, ft,
                     [{"name": r["person"], "net": v} for r, v in zip(rows, vals) if v > 0],
                     attribution=True)
    return json.dumps(result, ensure_ascii=False), attach


def _budget_tool(conn, ledger_id: int, rng: str, month: str,
                 dfrom: str, dto: str, label: str) -> dict:
    """预算进度：ledgers.budget 为月预算（分），区间预算=月预算×月数（与设置页折算一致），
    range=all 不折算只报净支出。percent 用 float——经 _yuan 时 int 会被误当分值除 100。"""
    budget = conn.execute("SELECT budget FROM ledgers WHERE id=?",
                          (ledger_id,)).fetchone()["budget"]
    spend = (dbm.stats_summary(conn, ledger_id, month) if rng == "month"
             else dbm.stats_summary_range(conn, ledger_id, dfrom, dto))["net_expense"]
    months = {"month": 1, "year": 12, "all": 0}.get(rng)
    if months is None:   # custom：按区间覆盖的自然月数
        end = datetime.date.fromisoformat(dto) - datetime.timedelta(days=1)   # dto 排他上界
        months = (end.year * 12 + end.month) - (int(dfrom[:4]) * 12 + int(dfrom[5:7])) + 1
    out = {"label": label, "spend": spend, "budget_monthly": budget}
    if not budget:
        out["note"] = "未设置月预算（设置页可配置）；spend 为该区间净支出"
        return out
    if months:
        total = budget * months   # budget/months 均非零（上方已挡），total 恒正
        out.update(budget_total=total, remaining=total - spend,
                   percent=float(round(spend * 100 / total)),
                   note="budget_monthly=月预算，budget_total=月预算×月数；remaining<0 即超支")
    else:
        out["note"] = "「全部」范围不按预算折算（与设置页一致），仅报区间净支出"
    return out


def _run_tool(ledger_id: int, args: dict) -> tuple[str, dict | None]:
    """执行 query_stats。返回 (给模型看的 JSON 结果, 给前端渲染的图表附件或 None)。"""
    conn = dbm.connect()
    def ret(data):
        return json.dumps(_yuan(data), ensure_ascii=False)
    kind = args["kind"]
    # 人情缺省全量（与统计页 /api/stats/renqing 同口径：人名的往来是历史概念）——
    # 模型忘传 range=all 时按当月取会漏旧账，缺省在后端兜住
    rng = args.get("range") or ("all" if kind == "renqing" else "month")
    month = args.get("month") or datetime.date.today().strftime("%Y-%m")
    label = ""
    if rng == "all":
        dfrom, dto, label = "2000-01-01", "2100-01-01", "全部"
    elif rng == "year":
        year = args.get("year") or datetime.date.today().strftime("%Y")
        dfrom, dto, label = f"{year}-01-01", f"{int(year)+1}-01-01", f"{year}年"
    elif rng == "custom":
        cf, ct = (args.get("from") or "")[:10], (args.get("to") or "")[:10]
        today = datetime.date.today().strftime("%Y-%m-%d")
        if not cf:
            cf = ct or today
        if not ct:
            ct = cf
        # [from, to] 闭区间 → 后端统计按 date < 排他上界
        dfrom = cf
        dto = (datetime.date.fromisoformat(ct) + datetime.timedelta(days=1)).isoformat()
        label = f"{cf} ~ {ct}" if cf != ct else cf
    else:
        dfrom, dto = dbm.month_range(month)
        label = month
    ft = args.get("type") or "expense"   # 支出/收入侧（问收入必须传 income）
    if kind == "attribution":
        return _attribution_tool(conn, ledger_id, dfrom, dto, label, ft)
    if kind == "trend":
        # 按月趋势序列（每月毛支出/收入/净支出），文字回答即可，无图表附件
        return ret(dbm.stats_trend(conn, ledger_id, dfrom, dto)), None
    if kind == "renqing":
        # 人情按问句区间取数（range=all 即全量）；结果包一层 note 说明 balance 方向——
        # 方向解释只在这里存在一份，模型照念即可（真机踩过：没 note 时模型猜反差额含义）
        rows = dbm.stats_renqing(conn, ledger_id, dfrom, dto)
        return ret({"note": "given=我随出（支出）、received=我收到（收入）、"
                            "balance=given-received：正数=我多给、对方尚未还礼；"
                            "负数=我收得多、我尚未回礼（我欠对方），按此解释引用，不要自行推断方向",
                    "persons": rows}), None
    if kind == "account":
        names = {r["id"]: r["name"] for r in conn.execute(
            "SELECT id, name FROM accounts WHERE ledger_id=? ORDER BY sort, id", (ledger_id,))}
        if ft == "income":
            vals = dbm.account_income_map(conn, ledger_id, dfrom, dto)
        else:   # 支出侧=毛支出-退款转账（与统计页账户列表同口径）
            vals = {a: g - rf
                    for a, (g, rf) in dbm.account_spend_map(conn, ledger_id, dfrom, dto).items()}
        key = "income" if ft == "income" else "spend"
        accs = [{"name": names.get(a, "未指定"), key: v} for a, v in vals.items()]
        accs += [{"name": nm, key: 0} for aid, nm in names.items() if aid not in vals]   # 区间零支出的账户也列出
        accs.sort(key=lambda x: -x[key])
        attach = _attach(label, dfrom, dto, ft,
                         [{"name": a["name"], "net": a[key]} for a in accs if a[key] > 0],
                         account=True)
        return ret(accs), attach
    if kind == "budget":
        return ret(_budget_tool(conn, ledger_id, rng, month, dfrom, dto, label)), None
    # 附件图表数据：任何查账调用都附——该区间的一级分类净额环形图（stats_category_range 已按净额降序）
    cats = (dbm.stats_category(conn, ledger_id, month, ft) if rng == "month"
            else dbm.stats_category_range(conn, ledger_id, dfrom, dto, ft))
    if kind == "summary":
        result = (ret(dbm.stats_summary(conn, ledger_id, month)) if rng == "month"
                  else ret(dbm.stats_summary_range(conn, ledger_id, dfrom, dto)))
    elif kind == "category":
        result = ret(cats)   # 结果与附件同源，直接复用，不再二次查询
    else:
        # 日历按问句区间取数（rng=month 时 dfrom/dto 即整月，与原口径等价）；
        # 跨月自定义区间（如 11 月初问「上周」）整月口径会漏另一月的天
        result = ret(dbm.stats_calendar(conn, ledger_id, month, dfrom, dto))
    attach = _attach(label, dfrom, dto, ft,
                     [{"name": c["name"], "net": c["net"]} for c in cats])
    hint = (args.get("category") or "").strip()
    if hint:
        # 命中判定与取数同一口径（评估 R-06）：先把简称归一化到规范分类名（「餐饮」→「餐饮美食」），
        # SQL 侧按归一化名精确匹配——旧版 matched 用模糊判定、取数用精确匹配，
        # 简称命中时 matched=true 却查不出明细（items 恒空、前端显示矛盾）。
        hit = next((c["name"] for c in cats if hint in c["name"] or c["name"] in hint), None)
        attach["category"] = hit or hint
        attach["matched"] = hit is not None
        if hit:
            # 命中：流水卡片数据（txRow 所需列）+ 总笔数（COUNT OVER 一次取回；≤5 直接全显，>5 出缩略+点击看全部）
            rows = dbm.fetch_tx_rows(conn, ledger_id, flow=ft, dfrom=dfrom, dto=dto,
                                     category=hit, limit=5, over_count=True)
            attach["items"] = [{k: v for k, v in dbm.tx_to_dict(r).items() if k != "cnt"} for r in rows]
            attach["total"] = rows[0]["cnt"] if rows else 0
    return result, attach


def _date_anchor() -> str:
    """预计算的相对日期锚点：模型不必自己做日期换算，直接取用（周一为一周之始）。"""
    t = datetime.date.today()
    mon = t - datetime.timedelta(days=t.weekday())          # 本周一
    lmon, lsun = mon - datetime.timedelta(days=7), mon - datetime.timedelta(days=1)
    first = t.replace(day=1)
    last_month_end = first - datetime.timedelta(days=1)
    last_month_first = last_month_end.replace(day=1)
    def d(x): return x.strftime("%Y-%m-%d")
    return (f"今天是 {d(t)}（星期{'一二三四五六日'[t.weekday()]}）。"
            f"本周：{d(mon)}~{d(mon + datetime.timedelta(days=6))}；"
            f"上周：{d(lmon)}~{d(lsun)}；"
            f"本月：{t.strftime('%Y-%m')}；上月：{last_month_end.strftime('%Y-%m')}"
            f"（{d(last_month_first)}~{d(last_month_end)}）；今年：{t.strftime('%Y')}。"
            "问周/区间/最近N天时用 range=custom（from/to 用上面的日期，含当天），不要自己算日期。")


def _chat_llm(query: str, ledger_id: int) -> Iterator[dict]:
    """对话式记账/查账（function calling），产出结构化事件（text/stats/booking/error）。
    模型自行判断：记账 → create_transactions 出草稿卡片；
    查账 → query_stats 后流式回答。统计范围不预设（scope=auto），时间词由模型按日期锚点解析。"""
    client = _client()
    if not client:
        yield {"error": _NO_KEY_HINT}
        return
    model = config.load()["llm"]["model"]
    cat_list, acc_list, few = _booking_context(ledger_id)
    attr_list = _member_roster(dbm.connect(), ledger_id)
    nl = chr(10)
    system = (
        "你是记账助手，先判断用户这句话是记账还是查账：" + nl +
        "1) 记账（在描述已发生的消费/收入/转账，如「今天早餐5块」）→ 调用 create_transactions 整理成条目草稿；"
        "不要自己写记账结果文字，也不要声称已经记好账（确认由用户在前端卡片上完成）。" + nl +
        "2) 查账（在问花销/收入/统计）→ 必须先调用 query_stats 拿到数据，再按问题的时间范围用自然语言回答，"
        "金额单位元；禁止不查就回答数字。" + nl +
        "查账时间范围不预设，按用户问题解析：整月/某月/上月用 range=month，某年/去年用 range=year，"
        "问「总共/一共/全部/累计」用 range=all，周/区间/最近N天按下方日期锚点用 range=custom；"
        "问题没提时间则默认当月。" + nl +
        "成员（归属，用户说的「用户/家里人/成员们/谁」即指账本成员）：" + (attr_list or "（暂无）") +
        "。成员人数以此清单为准，问「几个用户/几个成员/都有谁」时直接按清单回答人数和名单。" + nl +
        "问「几个人/每位成员/每人/人均/平均/AA/谁花得最多/谁垫付了」→ query_stats kind=attribution："
        "结果自带总额、人数、人均、每人差额，直接引用结果里的数字，不要自己算；"
        "成员列表竖向对齐：成员名右补空格到最长名的显示宽度（中文名按 2 个半角空格宽折算）"
        "再接「：支出 X 元」，使冒号和金额上下对齐。输出纪律（严格遵守，宁可少给不可多给，"
        "只回答用户问到的内容）：问「每人/各成员支出」时只报对齐的每人支出和总额，"
        "人均、差额一律不得出现；人均只在问「人均/平均」时给；"
        "差额只在问「AA/结算/谁垫付/差额」时给，且只报带正负号的数字"
        "（正负号已表意，不缀「多付/少付」等解释文字）；"
        "区间内没流水的成员不在结果里，算 AA 时结合上面的成员清单补足人数；"
        "用户明确问 AA/怎么结算/谁该给谁多少钱时，转账方案直接引用结果的 settlement 字段"
        "（后端已算好且收支守恒），严禁自行计算或改动谁给谁多少。" + nl +
        "其他维度路由：问「趋势/每月花多少/最近几个月」→ kind=trend；「人情往来/随礼/谁没还礼」"
        "→ kind=renqing（人情问句没提时间一律 range=all 全量，与统计页一致——人名的往来是历史概念，"
        "默认当月会漏旧账；提到本月/今年等才按区间；balance 方向按结果 note 解释引用）；"
        "「每个账户/某账户花了多少」→ kind=account；"
        "「预算/还剩多少/超支了吗」→ kind=budget——同样只回答问到的内容，列表类竖向对齐。" + nl +
        "同一回合只允许调用一次 create_transactions，且记账时不要调用 query_stats。" + nl +
        _AMOUNT_DATE_RULE + nl +
        _GIFT_RULE + nl +
        _TYPE_RULE + nl +
        _date_anchor() + nl +
        "可用分类：" + cat_list + "。可用账户：" + acc_list + "（" + _ACCOUNT_RULE + "）。" + nl +
        "用户历史记账习惯（备注→分类）：" + nl + few)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": query},
    ]
    booking = None
    try:
        for _round in range(3):
            resp = client.chat.completions.create(model=model, messages=messages,
                                                  tools=CHAT_TOOLS, stream=False)
            msg = resp.choices[0].message
            if msg.tool_calls:
                messages.append({"role": "assistant", "content": "", "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {
                        "name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls]})
                for tc in msg.tool_calls:
                    if tc.function.name == "create_transactions":
                        # 记账意图：只产出草稿，本回合到此为止（前端渲染卡片等用户确认）
                        booking = json.loads(tc.function.arguments or "{}")
                        continue
                    result, attach = _run_tool(ledger_id, json.loads(tc.function.arguments or "{}"))
                    if attach is not None:
                        # 图表附件推给前端（环形图/明细缩略），不进模型上下文
                        yield {"stats": attach}
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                if booking is not None:
                    break
                continue
            # 最终回答 → 流式转发
            for piece in _stream(client, messages, model):
                yield {"text": piece}
            break
    except Exception as e:
        yield {"error": _zh_error(e)}
    if booking is not None:
        items = _booking_items(booking.get("items") if isinstance(booking, dict) else None, ledger_id, query)
        if items:
            yield {"booking": {"items": items, "text": query}}


def parse_receipt(path: str) -> dict:
    """小票照片 → 结构化预览（视觉模型 OCR）。
    返回 {type, amount(元), date, category_name, name(商家/门店), note(品项摘要)} 或 {error}；
    amount 识别失败给 0；name/note 缺失给空串。"""
    client = _vl_client()
    if not client:
        return {"error": "未配置 API Key（文本或视觉任一即可，设置页填写）"}
    cfg = config.load()["llm"]
    model = cfg.get("vl_model") or ""
    if not model:
        return {"error": "当前平台未配置视觉模型，图片记账不可用（设置页填写视觉模型）"}
    try:
        b64 = base64.b64encode(Path(path).read_bytes()).decode()
        resp = client.chat.completions.create(
            model=model, temperature=0,
            messages=[{"role": "user", "content": [
                {"type": "text", "text":
                 "这是消费/收款小票或发票（增值税电子发票/纸质发票）照片。提取信息，只返回 JSON，不要多余文字："
                 '{"type":"expense 或 income","amount":金额数字(单位元,价税合计),'
                 '"date":"YYYY-MM-DD HH:MM(票据上的日期和时间;有时刻就带上,没有只给日期;都没有给空)",'
                 '"category_name":"最贴近的一级分类(餐饮美食/日用百货/数码电器/服饰装扮/交通出行/'
                 '医疗健康/充值缴费/住房物业/教育培训/工资薪酬等)",'
                 '"name":"商家/门店名/销售方名称(如 星巴克/沃尔玛/中国移动),识别不出给空串",'
                 '"note":"品项摘要或发票内容(如 拿铁咖啡/苹果2个/住宿服务),不含商家名,识别不出给空串"} '
                 '金额识别不出时给 0。'},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}])
        txt = (resp.choices[0].message.content or "{}").strip()
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            s, e = txt.find("{"), txt.rfind("}")
            data = json.loads(txt[s:e + 1]) if s >= 0 and e > s else {}
        try:
            amount = round(float(str(data.get("amount") or 0).replace(",", "")), 2)
        except ValueError:
            amount = 0.0
        # 日期归一 ISO；认不出（「11月1日」缺年等）按「识别不出给空」口径给空，由上层兜底
        d = dbm.iso_date_ok(str(data.get("date") or ""))
        return {"type": "income" if "income" in str(data.get("type") or "") else "expense",
                "amount": amount, "date": d[:16] if d else "",
                "category_name": str(data.get("category_name") or ""),
                "name": str(data.get("name") or ""),
                "note": str(data.get("note") or "")}
    except Exception as e:
        # 带上 模型名@服务商域名:404 model_not_found 这类配置错位(视觉模型名与
        # base_url 服务商不匹配)用户可自行对照设置页排查
        return {"error": f"小票识别失败（视觉模型 {model} @ {_host(cfg.get('vl_base_url') or cfg.get('base_url'))}）：{_zh_error(e)}"}


EDIT_ROUND_SYSTEM = (
    "你是记账编辑器。根据用户这句话判断意图，只输出 JSON：" + chr(10) +
    '1) 修改当前记账草稿（未入库）：{"target":"draft","action":"revise","items":[...]} —— '
    "输出修改后的**完整**草稿数组，每项字段与记账解析器一致："
    "type(expense/income/transfer)、amount(数字,元)、category_name、account、"
    "date(YYYY-MM-DD 或 YYYY-MM-DD HH:MM)、name、note；没被提到的项原样保留，可增删条目；"
    + chr(10) +
    '2) 修改账本里已有流水：{"target":"tx","action":"update","id":编号,"patch":{...}} —— '
    "patch 只含被改字段：amount(元)/category_name/account/date/name/note/merchant；"
    + chr(10) +
    '3) 删除已有流水：{"target":"tx","action":"delete","id":编号}；' + chr(10) +
    '4) 全新的记账（不是修改现有的）：{"target":"new"}；' + chr(10) +
    '5) 与草稿和流水都无关（查账、闲聊）：{"target":"none"}。' + chr(10) +
    "「第N笔/第二笔」指上下文清单里的编号；「52」即 52 元；「删掉第2笔」是 delete 不是 revise。"
)


def edit_round(text: str, draft_items: list | None, tx_rows: list | None,
               ledger_id: int) -> dict:
    """编辑回合：判断这句话是改草稿 / 改已有流水 / 全新记账 / 无关。
    输入只带草稿与已展示过的流水行（隐私口径与记账解析一致，不发全量账单）。
    返回 {target: draft|tx|new|none}，draft 附 items（已过 _booking_items 规范化），
    tx 附 {action: update|delete, id, patch?}；无 Key / 解析失败统一回落 none（上层走正常对话）。"""
    client = _client()
    if not client:
        return {"target": "none"}
    ctx = []
    if draft_items:
        ctx.append("当前记账草稿（未入库，编号即序号）：" + json.dumps(draft_items, ensure_ascii=False))
    if tx_rows:
        ctx.append("最近查询到的账本流水（id 即编号，可直接引用）：" + json.dumps(tx_rows, ensure_ascii=False))
    if not ctx:
        return {"target": "none"}
    out = _chat_json(client, chr(10).join(ctx) + chr(10) * 2 + "用户说：" + text, EDIT_ROUND_SYSTEM)
    if not isinstance(out, dict):
        return {"target": "none"}
    tgt = out.get("target")
    if tgt == "draft":
        items = _booking_items(out.get("items"), ledger_id, text)
        return {"target": "draft", "items": items}   # 空列表=全部删光，上层提示丢弃
    if tgt == "tx":
        try:
            tid = int(out.get("id"))
        except (TypeError, ValueError):
            return {"target": "none"}
        if out.get("action") == "delete":
            return {"target": "tx", "action": "delete", "id": tid}
        if out.get("action") == "update" and isinstance(out.get("patch"), dict):
            return {"target": "tx", "action": "update", "id": tid, "patch": out["patch"]}
    if tgt in ("new", "none"):
        return {"target": tgt}
    return {"target": "none"}


def chat(query: str, ledger_id: int) -> Iterator[dict]:
    """_chat_llm 的兜底包装（结构化事件流；HTTP 边界用 sse_stream 序列化，
    bot 通道直接消费事件不再解析 SSE 文本）：
    流中出现错误且无任何产出（无 Key/网络故障/解析失败）→ fix_local 正则拆分
    补发 booking 事件（标题第一行 = 失败原因）。
    解析责任全在后端:LLM 可用走 LLM,不可用走正则,传输/前端层零感知。"""
    err = text_seen = booked = False
    err_msg = ""
    for ev in _chat_llm(query, ledger_id):
        if "error" in ev:
            err = True
            err_msg = ev["error"]
        elif "booking" in ev:
            booked = True
        elif "text" in ev:
            text_seen = True
        yield ev
    if err and not booked and not text_seen:
        items = fix_local(query)
        if items:
            # 两行标题（失败原因 + 本地识别说明；微信端两行，App 折叠为空格）。原因沿用
            # 错误事件的中文提示——欠费时一律写「未配置 Key」会误导用户去查 Key
            title = f"{err_msg}\n本地识别到 {len(items)} 笔，日期为今天，请核对"
            yield {"booking": {"items": items, "text": title}}


def fix_local(q: str) -> list:
    """离线兜底拆分：
    只在 AI 请求失败时应急，不参与成功结果的渲染。先剥句首日期（避免「10月3号」的
    「月 3」被拆成假消费），再按「名称+数字+单位」逐笔拆出（块/元/毛/角/分，
    「4块5」=4.5 元），日期今天；收入动词(发了工资/收到转账)切 income,
    「发红包」仍为支出;裸动词前缀时品名取金额后的尾词(「发了5000工资」→ 工资)。"""
    if not q:
        return []
    q2 = re.sub(r"^\s*(?:\d{2,4}\s*年|(?:去|前|今)年?)?\s*\d{1,2}\s*月\s*\d{1,2}\s*[号日]\s*", "", q)
    q2 = re.sub(r"^\s*(今[天日]|昨[天日]|前天|大前天)\s*", "", q2)
    out = []
    today = datetime.date.today().strftime("%Y-%m-%d")
    for m in re.finditer(r"([^\d¥，。,\s]{1,10}?)(?:¥)?(\d+(?:\.\d+)?)(块|元|毛|角|分|块钱|元钱)?", q2):
        unit = m.group(3) or "块"
        yuan = float(m.group(2))
        tail = q2[m.end():m.end() + 1]
        # 「4块5」「4元5」＝4.5 元：块/元后紧跟的孤数字按角补
        if not re.search(r"毛|角|分", unit) and re.fullmatch(r"\d", tail):
            yuan += int(tail) / 10
        mult = 10 if re.search(r"毛|角", unit) else (1 if "分" in unit else 100)
        amount = round(yuan * mult)
        name = m.group(1).strip().rstrip("的")
        if not (name and amount > 0 and not re.fullmatch(r"[月号日年点]", name)):
            continue
        ttype = "expense"
        cue = next((c for c in _CUE_IN + _CUE_FA if c in name), None)
        if name in _CUE_IN or name in _CUE_FA:
            # 裸动词前缀(「发了5000工资」):金额后的尾词才是品名,取到分隔符/数字为止
            tail_txt = re.split(r"[，。,、\s\d]", q2[m.end():m.end() + 10])[0].strip()
            if 0 < len(tail_txt) <= 8:
                name = tail_txt
        if cue:
            ttype = "income"
            if cue in _CUE_FA and "红包" in name:
                ttype = "expense"          # 发红包=给别人发,支出(与 _TYPE_RULE 同口径)
            rest = name[len(cue):] if name.startswith(cue) else ""
            if len(rest) >= 2:             # 「发工资8500」→工资；cue 非前缀（「老婆收到转账」）保留原词
                name = rest
        item = {"type": ttype, "amount": amount, "name": name, "date": today,
                "category_name": "", "account": "", "account_id": None, "note": ""}
        _kw_fallback_category(item)   # 离线兜底也分类（无 Key 时卡片不再全靠手动选）
        out.append(item)
    return out