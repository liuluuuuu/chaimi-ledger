"""SQLite schema、迁移、种子数据与查询。

金额一律 INTEGER 存分（DESIGN.md 第四章）。统计口径：净支出 = expense −
归因到原消费日期(o.date)的已匹配退款（8.5.1）。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timedelta, timezone

import config

def _j(v):
    return json.dumps(v, ensure_ascii=False) if v else None

def jloads(s, default=None):
    """json.loads 容错（tags/ai_meta 等库内 JSON 列读取共用）：空/坏 JSON 返回 default。"""
    if not s:
        return default
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledgers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  icon TEXT, currency TEXT DEFAULT 'CNY', sort INTEGER DEFAULT 0,
  budget INTEGER DEFAULT 0, owner_uid INTEGER,
  created TEXT DEFAULT (datetime('now')));

CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY, ledger_id INTEGER NOT NULL,
  type TEXT NOT NULL,
  name TEXT NOT NULL, icon TEXT, color TEXT,
  parent_id INTEGER,
  sort INTEGER DEFAULT 0,
  FOREIGN KEY (ledger_id) REFERENCES ledgers(id));

CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY, ledger_id INTEGER NOT NULL,
  name TEXT NOT NULL, type TEXT,
  sort INTEGER DEFAULT 0,
  FOREIGN KEY (ledger_id) REFERENCES ledgers(id));

CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY, ledger_id INTEGER NOT NULL,
  type TEXT NOT NULL,
  amount INTEGER NOT NULL,
  batch_id INTEGER,
  category_id INTEGER, account_id INTEGER, to_account_id INTEGER,
  date TEXT NOT NULL, note TEXT,
  name TEXT, merchant TEXT,          -- 交易名称/商户(导入流水带,退款匹配与展示用)
  tags TEXT,
  receipt_path TEXT, source TEXT,
  ai_meta TEXT,
  order_no TEXT,
  refund_for INTEGER,
  refund_status TEXT DEFAULT 'none',
  refund_confidence TEXT,
  need_confirm INTEGER DEFAULT 0,
  manual_cat INTEGER DEFAULT 0,   -- 用户手选过分类（编辑/待确认/预览纠正/机器人改）；数据修复永不触碰
  row_hash TEXT,
  created TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (ledger_id) REFERENCES ledgers(id));

CREATE TABLE IF NOT EXISTS import_batches (
  id INTEGER PRIMARY KEY, ledger_id INTEGER,
  source TEXT, filename TEXT, count INTEGER, imported_at TEXT,
  date_from TEXT, date_to TEXT);

CREATE TABLE IF NOT EXISTS attribution_groups (
  id INTEGER PRIMARY KEY, ledger_id INTEGER NOT NULL, name TEXT NOT NULL,
  avatar TEXT NOT NULL DEFAULT 'f',
  sort INTEGER DEFAULT 0,
  UNIQUE(ledger_id, name));
CREATE TABLE IF NOT EXISTS attributions (
  id INTEGER PRIMARY KEY, ledger_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  group_id INTEGER REFERENCES attribution_groups(id) ON DELETE SET NULL,
  UNIQUE(ledger_id, name)
);
CREATE TABLE IF NOT EXISTS category_mappings (
  id INTEGER PRIMARY KEY,
  ledger_id INTEGER NOT NULL,
  flow_type TEXT NOT NULL,
  source TEXT NOT NULL,
  category_id INTEGER NOT NULL,
  learned INTEGER DEFAULT 0,
  batch_id INTEGER,
  FOREIGN KEY (ledger_id) REFERENCES ledgers(id),
  FOREIGN KEY (category_id) REFERENCES categories(id));

CREATE UNIQUE INDEX IF NOT EXISTS idx_map_ledger
  ON category_mappings(ledger_id, flow_type, source);

CREATE INDEX IF NOT EXISTS idx_tx_hash   ON transactions(ledger_id, source, row_hash);
CREATE INDEX IF NOT EXISTS idx_tx_date   ON transactions(ledger_id, date);
CREATE INDEX IF NOT EXISTS idx_tx_order  ON transactions(ledger_id, order_no);
CREATE INDEX IF NOT EXISTS idx_tx_batch  ON transactions(batch_id);
CREATE INDEX IF NOT EXISTS idx_tx_refund ON transactions(refund_for);
CREATE INDEX IF NOT EXISTS idx_tx_receipt ON transactions(receipt_path);
-- 退款重扫的日期窗取数（import_apply._rescan_unmatched_refunds）：窗内支出直取，不再全账本扫描
CREATE INDEX IF NOT EXISTS idx_tx_ledger_type_date ON transactions(ledger_id, type, date);

CREATE TABLE IF NOT EXISTS app_meta (
  key TEXT PRIMARY KEY, value TEXT);

-- 机器人消息幂等表（2026-11）：长轮询是 at-least-once（响应丢失后服务端会重发），
-- 重发、跨重启重放、旧进程未退净的双轮询都靠这里拦——同一键只有一个插入成功，
-- 失败方放弃处理，同一笔只会记一次。行只作占位，7 天滚动清理（bot_ilink._seen_once）。
CREATE TABLE IF NOT EXISTS bot_seen (
  account_id TEXT NOT NULL,        -- 机器人绑定账号（ilink_bot_id；CLI/测试为空串）
  user_id TEXT NOT NULL,           -- 发消息的微信用户
  msg_id TEXT NOT NULL,            -- 平台 message_id（无 id 平台为掺秒兜底指纹）
  seen_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (account_id, user_id, msg_id));

-- 多用户（MULTIUSER-PLAN）：账本归属 + 共享名单 + 打开过应用的飞牛用户档案。
-- uid = 网关注入的 X-Trim-Userid（fnOS 用户 uid，唯一键；username 可改名仅显示）。
CREATE TABLE IF NOT EXISTS ledger_members (
  ledger_id INTEGER NOT NULL, uid INTEGER NOT NULL,
  -- 用户可见时间戳一律本地时（与 known_users/backup 同口径；created 两列刻意保 UTC，见 bot_core 撤销窗口注释）
  added_at TEXT DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (ledger_id, uid));
CREATE TABLE IF NOT EXISTS known_users (
  uid INTEGER PRIMARY KEY,
  username TEXT NOT NULL DEFAULT '',
  isadmin INTEGER DEFAULT 0,
  first_seen TEXT DEFAULT (datetime('now','localtime')),
  last_seen TEXT DEFAULT (datetime('now','localtime')));
"""

# 支出一级（15 分类体系：转账红包/投资理财迁 transfer，微信交易不留；
# 2026-09 依据三渠道实测账单补「交通出行」（滴滴/12306 等原被挤入爱车养车）、
# 取消二级分类并把「加油充电」拆为独立一级（高频支出，与停车保养分开统计）；
# 2026-10 补「住房物业」——房贷/房租/物业费原挤在充值缴费，分开统计更清晰
# （老库由 init_db 一次性迁移补种））
EXPENSE_CATEGORIES = [
    "餐饮美食", "日用百货", "数码电器", "服饰装扮",
    "美容美发", "文化休闲", "医疗健康", "教育培训",
    "爱车养车", "加油充电", "交通出行", "充值缴费", "住房物业",
    "母婴亲子", "亲友代付", "人情往来", "红包礼金", "其他",
]

INCOME_CATEGORIES = [
    "工资薪酬", "兼职副业", "理财收益", "红包礼金",
    # 2026-09：收入侧新增「人情往来」，与支出侧对齐——收入红包/礼金统一归此类。
    # 无「退款返还」：退款不计收入，统一走不计收支组「退款」冲减原消费
    "人情往来", "报销", "投资本金", "其他收入",
]

# 收支两侧的兜底一级分类（单源）：规则层认不出时挂这里 + 待确认（import_classify.fallback），
# AI 兜底答这里视同「AI 也拿不准」、不算命中仍留待确认（ai.resolve_subcategories_batch）
FALLBACK_TOP = {"expense": "其他", "income": "其他收入"}

# 不计收支组一级分类（前端分组展示名「退款和不计收支」）：
# 「退款」=冲减类（关联原消费后抵扣那笔支出，归因原消费日/分类；未关联前暂不计）；
# 「不计收支」=位移类（内部转账/理财/还款，统计完全消失，子类明细存 tags.transfer_sub）。
# 落库仍是 type='transfer'，分类只作管理与映射目标
TRANSFER_CATEGORIES = ["退款", "不计收支"]

# 默认全局映射（source → 一级，按 flow_type 分流；DESIGN 8.2，实测 source 补充见 8.1）
DEFAULT_MAPPINGS = [
    # 支出侧
    ("expense", "食品酒饮", "餐饮美食"), ("expense", "餐饮美食", "餐饮美食"),
    ("expense", "家居家装", "日用百货"), ("expense", "日用百货", "日用百货"),
    ("expense", "鞋服箱包", "日用百货"), ("expense", "清洁纸品", "日用百货"),
    ("expense", "其他网购", "日用百货"), ("expense", "网购", "日用百货"),
    ("expense", "手机通讯", "数码电器"), ("expense", "数码电器", "数码电器"),
    ("expense", "电脑办公", "数码电器"),
    ("expense", "服饰内衣", "服饰装扮"), ("expense", "服饰装扮", "服饰装扮"),
    ("expense", "钟表眼镜", "服饰装扮"),
    ("expense", "美妆个护", "美容美发"), ("expense", "美容美发", "美容美发"),
    ("expense", "图书文娱", "文化休闲"), ("expense", "运动户外", "文化休闲"),
    ("expense", "文体玩具", "文化休闲"), ("expense", "文化休闲", "文化休闲"),
    ("expense", "医疗保健", "医疗健康"), ("expense", "医疗健康", "医疗健康"),
    ("expense", "教育培训", "教育培训"),
    ("expense", "汽车用品", "爱车养车"), ("expense", "爱车养车", "爱车养车"),
    ("expense", "交通出行", "交通出行"),
    ("expense", "充值缴费", "充值缴费"),
    # 支付账单原生有「住房物业」分类（房贷/房租走这里），同名直连
    ("expense", "住房物业", "住房物业"),
    ("expense", "母婴用品", "母婴亲子"), ("expense", "母婴亲子", "母婴亲子"),
    ("expense", "亲友代付", "亲友代付"), ("expense", "亲属卡交易", "亲友代付"),
    ("expense", "美妆", "美容美发"), ("expense", "娱乐", "文化休闲"),
    # 支出侧一级自映射：让每个一级分类都在映射页可见/可选
    # （人情往来/红包礼金实际归类仍由导入时的关键词词典+小额红包规则优先判定）
    ("expense", "加油充电", "加油充电"),
    ("expense", "人情往来", "人情往来"), ("expense", "红包礼金", "红包礼金"),
    ("expense", "其他", "其他"),
    ("income", "转账红包", "红包礼金"), ("income", "微信红包", "红包礼金"),
    ("income", "投资理财", "理财收益"), ("income", "小金库", "理财收益"),
    ("income", "工资", "工资薪酬"), ("income", "报销", "报销"),
    # 收入侧一级自映射：补齐映射页缺失的一级分类（红包礼金收入侧仅同名直连，自动归类仍走人情往来）
    ("income", "兼职副业", "兼职副业"),
    ("income", "投资本金", "投资本金"), ("income", "其他收入", "其他收入"),
    ("income", "红包礼金", "红包礼金"),
]

# 导入平台 source → 中文名（attribution_rows 的 _PLAT SQL 与 server 归属下钻反解共用单源；
# 新增平台改这里一处即可，两边不会漂移）
PLATFORM_CN = {"alipay": "支付宝", "wechat": "微信", "jd": "京东", "meituan": "美团"}

# transfer 子类（7.6，不计收支；存 tags.transfer_sub）
DEFAULT_TRANSFER_SUB = "账户转账"   # 无映射时的兜底子类（import_classify 两处；前端 core.js TX_SUB_DEFAULT 单点，import/ai 展示共用）
TRANSFER_SUBCLASS = {
    "转账": "账户转账", "转账红包": "账户转账", "微信红包": "账户转账",
    "微信红包（单发）": "账户转账",
    "小金库": "理财申购", "投资理财": "理财申购", "转入零钱通": "理财申购",
    "余额宝": "理财申购", "余额宝-自动转入": "理财申购",
    "转入零钱通-来自零钱": "理财赎回", "赎回": "理财赎回",
    "信用卡还款": "信用卡还款", "信用借还": "信用卡还款",
    "余额互通": "账户充值", "充值": "账户充值", "退款": "退款",
}

# 微信「不计收支」transfer 类型集合（platform_templates 微信模板 transfer_types 的单源，
# 此处派生、彼处引用——此前两处字面各维护一份，靠单测断言同步，db 演进即两处手改）：
# 全集里排除个人间转账/红包族（按方向计收支）与退款（退款链单独处理）。
_WX_TRANSFER_EXCLUDE = {"转账", "转账红包", "微信红包", "微信红包（单发）", "退款"}
WX_TRANSFER_TYPES = [k for k in TRANSFER_SUBCLASS if k not in _WX_TRANSFER_EXCLUDE]

DEFAULT_ACCOUNTS = ["现金", "支付宝", "微信支付"]

_local = threading.local()

_CONN_GEN = 0

def connect() -> sqlite3.Connection:
    """线程级连接：WAL + busy_timeout（FastAPI 多线程并发写必需）。
    恢复备份经 backup API 原地写回同一文件；各线程缓存的连接仍以代数标记作废（防御性），
    reset_connections() 后各线程下次取连接自动重开。"""
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "gen", -1) != _CONN_GEN:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        config.db_path().parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.db_path(), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        # WAL 下的标准安全档（崩溃只丢最后事务不损库）：NAS 盘上每次「记一笔」少一次 fsync；
        # 默认 2MB 页缓存对全账本扫描的统计端点反复换入换出，提到 16MB
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-16000")
        _local.conn = conn
        _local.gen = _CONN_GEN
    return conn

def reset_connections() -> None:
    """作废全部线程的缓存连接（恢复备份替换 db 文件后调用）。"""
    global _CONN_GEN
    _CONN_GEN += 1

def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """老库升级补列：CREATE TABLE IF NOT EXISTS 不改已存在的表，
    启动时按 PRAGMA table_info 查缺再 ALTER，幂等（新库/已升级库无感）。"""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(ddl)

# source 取值域——导入侧（本元组）：账单原始归属是真实数据，删组/删归属的清理永不触碰；
#   平台 id（alipay/wechat/jd/meituan/export 模板）或 custom（向导/用户模板），导出回环行
#   会被「来源」列覆写回原始 source；cashbook 为历史库兼容（无现行写入方，仅防老行漏判）。
#   用户侧：app（App 手动/AI 记一笔默认）/ bot（机器人确认）/ manual（历史/测试写入口径）
IMPORT_SOURCES = ("alipay", "wechat", "jd", "meituan", "custom", "cashbook", "export")

def init_db() -> None:
    """建表 + 列级补齐 + 空库播种。"""
    conn = connect()
    with conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "attributions", "group_id",
                       "ALTER TABLE attributions ADD COLUMN group_id INTEGER "
                       "REFERENCES attribution_groups(id) ON DELETE SET NULL")
        _ensure_column(conn, "attribution_groups", "avatar",
                       "ALTER TABLE attribution_groups ADD COLUMN avatar TEXT NOT NULL DEFAULT 'f'")
        _ensure_column(conn, "ledgers", "budget",
                       "ALTER TABLE ledgers ADD COLUMN budget INTEGER DEFAULT 0")
        _ensure_column(conn, "transactions", "manual_cat",
                       "ALTER TABLE transactions ADD COLUMN manual_cat INTEGER DEFAULT 0")
        # owner_uid 已进 SCHEMA 建表列，此条只对多用户化之前的老库升级（新库 no-op）
        _ensure_column(conn, "ledgers", "owner_uid",
                       "ALTER TABLE ledgers ADD COLUMN owner_uid INTEGER")
        if conn.execute("SELECT COUNT(*) FROM ledgers").fetchone()[0] == 0:
            seed()
        _migrate(conn)

def ledger_exists(conn: sqlite3.Connection, ledger_id: int) -> bool:
    """账本行存在性（server 账本访问门与 bot 扫码回调共用单点）。"""
    return conn.execute("SELECT 1 FROM ledgers WHERE id=?", (ledger_id,)).fetchone() is not None

def top_cat_id(conn: sqlite3.Connection, ledger_id: int, name: str,
               flow_type: str = "") -> int | None:
    """按名字取一级分类 id；flow_type 非空时按类型消歧。
    同名收支分类（如「人情往来」两侧都有）必须带类型查，否则 fetchone() 取到哪条不确定。"""
    sql = ("SELECT id FROM categories WHERE ledger_id=? AND parent_id IS NULL AND name=?"
           + (" AND type=?" if flow_type else "") + " LIMIT 1")
    args = (ledger_id, name) + ((flow_type,) if flow_type else ())
    r = conn.execute(sql, args).fetchone()
    return r["id"] if r else None

def top_cat_rows(conn: sqlite3.Connection, ledger_id: int) -> list:
    """一级分类行集（id/name/type）——match_top_cat 的标准输入，调用方一次取行逐条匹配
    （bot 明细路由、AI 条目分类 id 注记、AI 兜底分类共用，SQL 单源）。"""
    return conn.execute("SELECT id, name, type FROM categories WHERE ledger_id=? AND parent_id IS NULL",
                        (ledger_id,)).fetchall()

def account_rows(conn: sqlite3.Connection, ledger_id: int) -> list:
    """账户行集（id/name，按 sort 排）——AI 账户名解析与账户统计工具共用，SQL 单源。"""
    return conn.execute("SELECT id, name FROM accounts WHERE ledger_id=? ORDER BY sort, id",
                        (ledger_id,)).fetchall()

def match_top_cat(rows, name: str, tx_type=None):
    """一级分类两段式匹配（先[按 type]精确名，再双向包含，简称可命中）：
    rows 为 top_cat_rows 一次取的行集（全库唯一匹配实现：AI 条目 id 注记 ai._cat_ids、
    bot 入库与「XX明细」路由共用；App 前端直接用 API 下发的 id，不另实现）。
    空名直接不中——空串在双向包含段恒真，守在唯一实现里，调用方不必各自拦。
    注意与 exact 系刻意区分——top_cat_id / ClassifyCtx.top 只做精确匹配：导入的
    「同名直连」须严格同名，包含匹配会把相近的账单来源分类误吞。第二段同样按 tx_type
    过滤：包含匹配跨收支侧会把收入行挂到支出分类（收入「其他」命中支出侧「其他」，
    实测复现）；tx_type=None 才放宽为全行集。"""
    if not name:
        return None
    r = next((x for x in rows if x["name"] == name
              and (tx_type is None or x["type"] == tx_type)), None)
    return r or next((x for x in rows if (tx_type is None or x["type"] == tx_type)
                      and (name in x["name"] or x["name"] in name)), None)

def seed_ledger_defaults(conn: sqlite3.Connection, ledger_id: int, accounts: bool = True) -> None:
    """给账本播种默认分类（存在即跳过，只增不删，不动用户自建）+ 默认账户。
    新建账本（seed / server.create_ledger）与分类体系整体替换共用；
    替换场景传 accounts=False 不动账户。"""
    for kind, table in (("expense", EXPENSE_CATEGORIES), ("income", INCOME_CATEGORIES),
                        ("transfer", TRANSFER_CATEGORIES)):
        for sort, parent in enumerate(table):
            row = conn.execute(
                "SELECT id FROM categories WHERE ledger_id=? AND type=? AND name=? AND parent_id IS NULL",
                (ledger_id, kind, parent)).fetchone()
            if row:
                continue
            conn.execute(
                "INSERT INTO categories(ledger_id, type, name, parent_id, sort) VALUES(?,?,?,?,?)",
                (ledger_id, kind, parent, None, sort))
    if accounts:
        for i, name in enumerate(DEFAULT_ACCOUNTS):
            conn.execute(
                "INSERT INTO accounts(ledger_id, name, type, sort) VALUES(?,?,?,?)",
                (ledger_id, name, "e-wallet" if name != "现金" else "cash", i))

def top_cat_by_id(conn: sqlite3.Connection, ledger_id: int, cid: int) -> int | None:
    """按 id 取属于该账本的一级分类 id；不属于/非一级/不存在返回 None（server 映射校验用）。"""
    r = conn.execute(
        "SELECT id FROM categories WHERE id=? AND parent_id IS NULL AND ledger_id=?",
        (cid, ledger_id)).fetchone()
    return r["id"] if r else None

def seed_default_mappings(conn: sqlite3.Connection, ledger_id: int) -> None:
    """给账本种默认映射（v6 起映射为账本级 + 外键，新账本必须显式种）。
    目标按 DEFAULT_MAPPINGS 里的分类名 + flow_type 解析到本账本的一级分类 id。"""
    for flow, source, target in DEFAULT_MAPPINGS:
        cid = top_cat_id(conn, ledger_id, target, flow)
        if cid:
            conn.execute(
                "INSERT OR IGNORE INTO category_mappings(ledger_id, flow_type, source, category_id) "
                "VALUES(?,?,?,?)", (ledger_id, flow, source, cid))

def copy_ledger_categories(c: sqlite3.Connection, src_id: int, dst_id: int) -> None:
    """账本间复制分类体系：categories（含二级，parent_id 重指新账本）+ category_mappings
    （含 learned 学习标记；category_id 指向新账本的对应分类）。目标账本应为空分类的
    新建账本（replace_ledger_categories 的调用方），不做冲突合并。"""
    id_map: dict[int, int] = {}
    # 先一级（parent_id IS NULL，按 sort 保序），再二级（parent 指向已复制的映射）
    for parent in (None, "not-null"):
        rows = c.execute(
            "SELECT * FROM categories WHERE ledger_id=? AND "
            + ("parent_id IS NULL" if parent is None else "parent_id IS NOT NULL")
            + " ORDER BY sort, id", (src_id,)).fetchall()
        for r in rows:
            new_id = c.execute(
                "INSERT INTO categories(ledger_id, type, name, icon, color, parent_id, sort) "
                "VALUES(?,?,?,?,?,?,?)",
                (dst_id, r["type"], r["name"], r["icon"], r["color"],
                 id_map[r["parent_id"]] if r["parent_id"] else None, r["sort"])).lastrowid
            id_map[r["id"]] = new_id
    for m in c.execute(
            "SELECT flow_type, source, category_id, learned FROM category_mappings "
            "WHERE ledger_id=? ORDER BY id", (src_id,)).fetchall():
        if m["category_id"] in id_map:   # 挂在已删分类上的悬挂映射不搬（建外键会炸）
            c.execute(
                "INSERT INTO category_mappings(ledger_id, flow_type, source, category_id, learned) "
                "VALUES(?,?,?,?,?)",
                (dst_id, m["flow_type"], m["source"], id_map[m["category_id"]], m["learned"]))

def replace_ledger_categories(c: sqlite3.Connection, lid: int, src_id: int | None) -> None:
    """把 lid 的分类+来源映射整体替换：src_id 有值=复制 src（含二级/learned），
    None=恢复内置默认（分类+默认映射；账户不动——建账已种，重种会重复）。
    home 建用户页「分类来自」的选中即应用。调用方须保证 lid 尚无流水（server 层
    守卫），映射先删（外键无级联）。"""
    c.execute("DELETE FROM category_mappings WHERE ledger_id=?", (lid,))
    c.execute("DELETE FROM categories WHERE ledger_id=?", (lid,))
    if src_id is None:
        seed_ledger_defaults(c, lid, accounts=False)
        seed_default_mappings(c, lid)
    else:
        copy_ledger_categories(c, src_id, lid)

def create_ledger(c: sqlite3.Connection, name: str, icon: str = "", currency: str = "CNY",
                  owner_uid: int | None = None, budget: int | None = None) -> int:
    """建账本 + 播种默认分类/账户/映射（首建 seed 与 POST /api/ledgers 单源，
    存在即跳过、只增不删；v6 起映射为账本级，新账本必须显式种）。budget 随建账
    一并落。分类来源的「复用」在 home 建用户页经 category-source 端点事后整体替换。
    返回新账本 id。"""
    cur = c.execute(
        "INSERT INTO ledgers(name, icon, currency, owner_uid, budget) VALUES(?,?,?,?,?)",
        (name, icon, currency, owner_uid, budget or 0))
    ledger_id = cur.lastrowid
    seed_ledger_defaults(c, ledger_id)
    seed_default_mappings(c, ledger_id)
    return ledger_id

def seed() -> None:
    """首次初始化：默认账本「日常」+ 默认分类/账户/映射（空库时由 init_db 调用）。"""
    conn = connect()
    with conn:
        create_ledger(conn, "日常", "🏠")

# ---------- 一次性库迁移（app_meta 键值打标） ----------

# 2026-10 老库补「住房物业」一级分类 + 同名默认映射（v1.0.4 前的库没有）。
# 不直接复用 seed_ledger_defaults 全量补种：那会把用户刻意删掉的默认分类在
# 每次重启时复活。打标后只跑一遍——用户随后删掉该分类/映射也尊重其选择。
def _m_housing_2026_10(conn: sqlite3.Connection) -> None:
    name = "住房物业"
    for r in conn.execute("SELECT id FROM ledgers").fetchall():
        lid = r["id"]
        cid = top_cat_id(conn, lid, name, "expense")
        if not cid:
            cid = conn.execute(
                "INSERT INTO categories(ledger_id, type, name, parent_id, sort) VALUES(?,?,?,?,?)",
                (lid, "expense", name, None, EXPENSE_CATEGORIES.index(name))).lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO category_mappings(ledger_id, flow_type, source, category_id) "
            "VALUES(?,?,?,?)", (lid, "expense", name, cid))

# 2026-11 用户可见时间戳统一本地时：imported_at（导入批次列表）/added_at（共享时间）
# 历史值由 UTC DEFAULT/写入点产生，按 UTC→本地一次性搬正；此后写入点已全部改本地时。
# 恢复旧版备份换回无标记 db 时重跑本迁移——转的是备份里的存量 UTC 值，结果仍正确。
def _m_localtime_2026_11(conn: sqlite3.Connection) -> None:
    for table, col in (("import_batches", "imported_at"), ("ledger_members", "added_at")):
        # rowid 显式别名：INTEGER PRIMARY KEY 表上裸 SELECT rowid 的列名会解析成 id
        for r in conn.execute(f"SELECT rowid AS rid, {col} AS v FROM {table} WHERE {col} IS NOT NULL").fetchall():
            try:
                local = datetime.strptime(r["v"], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue   # 非 19 位标准格式的历史脏值：不动
            if local != r["v"]:
                conn.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?", (local, r["rid"]))

def _m_hang_toacct_2026_11(conn: sqlite3.Connection) -> None:
    """一次性清悬挂收款账户引用：1.0.7 前删账户只清付款列，旧版导出转账行的
    to_account_id 指向已删账户后，编辑弹窗（无收款字段）永远 400 无法修正。
    幂等：只清指向不存在账户的引用；指向存在账户的一律不动。"""
    conn.execute("UPDATE transactions SET to_account_id=NULL "
                 "WHERE to_account_id IS NOT NULL AND to_account_id NOT IN (SELECT id FROM accounts)")

def _m_avatar_f_2026_11(conn: sqlite3.Connection) -> None:
    """归属组 avatar 空串清为 'f'：写入口（server._check_avatar）只认 m/f，空串仅
    存在于更早版本的库。此前这条修补在 init_db 里每次启动都跑，现改一次性打标
    （恢复旧版备份换回无标记 db 时重跑，幂等 UPDATE 结果仍正确）。"""
    conn.execute("UPDATE attribution_groups SET avatar='f' WHERE avatar=''")

def _m_receipt_prefix_2026_11(conn: sqlite3.Connection) -> None:
    """一次性归一小票路径为 receipts/文件名 单一形态：写入口此前兼容裸名入库，
    读取侧（discard/裁剪/账本闸）被迫处处按两形态查询。裸名/绝对路径等历史
    形态在此统一补前缀（basename 保持不变），读取侧即可按索引单形态精确查。
    恢复旧版备份重跑仍正确：已带前缀的行 NOT LIKE 不命中。"""
    rows = conn.execute(
        "SELECT rowid AS rid, receipt_path FROM transactions "
        "WHERE receipt_path IS NOT NULL AND receipt_path!='' AND receipt_path NOT LIKE 'receipts/%'"
    ).fetchall()
    for r in rows:
        name = str(r["receipt_path"]).replace("\\", "/").rsplit("/", 1)[-1]
        conn.execute("UPDATE transactions SET receipt_path=? WHERE rowid=?",
                     ("receipts/" + name, r["rid"]))

_MIGRATIONS = [
    ("m_housing_2026_10", _m_housing_2026_10),
    ("m_localtime_2026_11", _m_localtime_2026_11),
    ("m_hang_toacct_2026_11", _m_hang_toacct_2026_11),
    ("m_avatar_f_2026_11", _m_avatar_f_2026_11),
    ("m_receipt_prefix_2026_11", _m_receipt_prefix_2026_11),
]

def _migrate(conn: sqlite3.Connection) -> None:
    """启动时按 app_meta 标记执行未跑过的一次性迁移（顺序注册，逐个打标）。
    恢复旧版备份会换回无标记的 db 文件，下次启动自动重跑补齐。"""
    done = {r["key"] for r in conn.execute("SELECT key FROM app_meta")}
    for key, fn in _MIGRATIONS:
        if key in done:
            continue
        fn(conn)
        conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES(?, datetime('now'))", (key,))

# ---------- 通用查询 ----------

def month_range(month: str) -> tuple[str, str]:
    """'2026-09' → ('2026-09-01', '2026-10-01')。月份格式的唯一裁判：非法（非
    YYYY-MM / 月份越界 / None）抛带人话的 ValueError——HTTP 端点翻译成 400，
    AI 工具层透传给用户，不会再出现裸 int() 解析错误。"""
    try:
        y, m = month.split("-")
        y, m = int(y), int(m)
    except (AttributeError, ValueError):
        raise ValueError(f"month 必须形如 2026-09（收到 {str(month)[:20]!r}）")
    if not 1 <= m <= 12:
        raise ValueError(f"月份越界：{str(month)[:20]!r}")
    if not 1000 <= y <= 9999:
        # 两位年份会被 :04d 补成 0026 生成字典序不可预期的空区间——与 year_range 拒两位年份同口径
        raise ValueError(f"month 必须形如 2026-09（年份须 4 位，收到 {str(month)[:20]!r}）")
    iso = f"{y:04d}-{m:02d}"
    return iso + "-01", _next_month(iso)

def _next_month(month: str) -> str:
    """'YYYY-MM' → 'YYYY-MM+1-01'，跨年自动 +1。"""
    y, m = map(int, month.split("-"))
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{ny:04d}-{nm:02d}-01"

def year_range(year: str) -> tuple[str, str]:
    """'2026' → ('2026-01-01', '2027-01-01')。年份格式的唯一裁判（server 月度趋势与
    AI 查账 range=year 共用单源）：非 4 位数字（"26"/"20x6"）抛带人话的 ValueError——
    两位年份生成的 "26-01-01" 区间在 date 字典序比较下结果不可预期。"""
    if not re.fullmatch(r"\d{4}", str(year or "")):
        raise ValueError(f"year 必须是 4 位数字年份（如 2026，收到 {str(year)[:20]!r}）")
    return f"{year}-01-01", f"{int(year) + 1}-01-01"

# ---------- 统计（8.5.1 口径：退款扣减归因到原消费日期 o.date） ----------

# 退款扣减 join 单源：此前 _refund_sum 系不校验 r.type、account_spend_map 校验——
# 同一条脏行（伪造 expense+refund_for，落库闸收口前的历史数据或漏网路径）会一边双计
# 一边不计。写侧收口后这里是纵深防御；改退款口径（如认 income 原消费）只动这一处。
_REFUND_JOIN = ("FROM transactions r JOIN transactions o ON r.refund_for=o.id "
                "AND r.type='transfer' AND o.type='expense'")

def _refund_sum(conn: sqlite3.Connection, ledger_id: int, dfrom: str, dto: str,
                category_id: int | None = None) -> int:
    """区间内已匹配退款合计（归因到原消费日，DESIGN 8.5.1 口径）。
    category_id 非空时只统计该分类（分类统计同口径）。"""
    sql = (f"SELECT COALESCE(SUM(r.amount),0) {_REFUND_JOIN} "
           "WHERE o.ledger_id=? AND o.date>=? AND o.date<?")
    args: list = [ledger_id, dfrom, dto]
    if category_id is not None:
        sql += " AND o.category_id=?"
        args.append(category_id)
    return conn.execute(sql, args).fetchone()[0]

def refund_status(origin_cents: int, refund_cents: int) -> str:
    """退款状态单源判定（手动关联/导出直连/导入匹配/确认入库四路共用）：
    退款额 ≥ 原消费额 = full——相等即全额退清；多退（订单号唯一命中不校验金额时允许的
    异常形态）旧实现手动/导入两路已分叉（full vs partial），统一按 full；不足额 = partial。"""
    return "full" if refund_cents >= origin_cents else "partial"

def stats_summary(conn: sqlite3.Connection, ledger_id: int, month: str) -> dict:
    dfrom, dto = month_range(month)
    return stats_summary_range(conn, ledger_id, dfrom, dto)

def stats_summary_range(conn: sqlite3.Connection, ledger_id: int, dfrom: str, dto: str) -> dict:
    r = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN type='expense' THEN amount END),0) AS gross,
               COALESCE(SUM(CASE WHEN type='income' THEN amount END),0) AS income
           FROM transactions WHERE ledger_id=? AND date>=? AND date<?""",
        (ledger_id, dfrom, dto)).fetchone()
    refund = _refund_sum(conn, ledger_id, dfrom, dto)
    return {"gross_expense": r["gross"], "refund_deduction": refund,
            "net_expense": r["gross"] - refund, "income": r["income"]}

def stats_calendar(conn: sqlite3.Connection, ledger_id: int, month: str = "",
                   dfrom: str = "", dto: str = "") -> list[dict]:
    """按日聚合：支出/收入/退款扣减（退款归因原消费日，按日查一次）。
    dfrom/dto 缺省按 month 整月（统计页日历）；AI 对话传问句区间（如「上周每天」
    跨月时整月口径会漏天），此时 month 不参与。dfrom 含端、dto 排他上界。"""
    if not dfrom or not dto:   # 只传一端会让 date>=? AND date<? 恒空，整体回落整月
        dfrom, dto = month_range(month)
    refunds: dict[str, int] = {}
    for r in conn.execute(
            f"SELECT substr(o.date,1,10) AS day, SUM(r.amount) AS amt {_REFUND_JOIN} "
            "WHERE o.ledger_id=? AND o.date>=? AND o.date<? "
            "GROUP BY day", (ledger_id, dfrom, dto)):
        refunds[r["day"]] = r["amt"]
    rows = conn.execute(
        """SELECT substr(date,1,10) AS day,
              SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) AS expense,
              SUM(CASE WHEN type='income' THEN amount ELSE 0 END) AS income
              FROM transactions WHERE ledger_id=? AND date>=? AND date<? GROUP BY day""",
        (ledger_id, dfrom, dto)).fetchall()
    return [{"day": r["day"], "expense": r["expense"], "income": r["income"],
             "refund": refunds.get(r["day"], 0),
             "net": r["expense"] - refunds.get(r["day"], 0)} for r in rows]

def stats_category(conn: sqlite3.Connection, ledger_id: int, month: str, flow_type: str = "expense") -> list[dict]:
    dfrom, dto = month_range(month)
    return stats_category_range(conn, ledger_id, dfrom, dto, flow_type)

# 「未分类」桶名单源：stats 饼图的无分类行与流水列表 category=-1 过滤（server）、前端
# showCategoryDetail 的下钻特判（ledger.js）共用同一名字，三处不漂移
UNCATEGORIZED_NAME = "未分类"

def stats_category_range(conn: sqlite3.Connection, ledger_id: int, dfrom: str, dto: str,
                         flow_type: str = "expense") -> list[dict]:
    """分类聚合（饼图数据源）。expense 按净额。
    LEFT JOIN：无分类（前端「未分类」选项）/分类被删的行归「未分类」桶——与
    summary/calendar 的口径对齐；此前 INNER JOIN 会让这些行从饼图静默消失，
    汇总条支出 > 饼图加总对不上账。退款按 o.category_id 一条 GROUP BY 取回
    （旧版逐分类 _refund_sum 的 N+1 在大库上拖慢统计页/AI 查账/导出三处高频路径）。"""
    rows = conn.execute(
        """SELECT c.id, c.name, COALESCE(c2.name,'') AS parent,
              SUM(t.amount) AS gross
              FROM transactions t LEFT JOIN categories c ON t.category_id=c.id
              LEFT JOIN categories c2 ON c.parent_id=c2.id
              WHERE t.ledger_id=:lid AND t.type=:ft AND t.date>=:dfrom AND t.date<:dto
              GROUP BY c.id""",
        {"lid": ledger_id, "ft": flow_type, "dfrom": dfrom, "dto": dto}).fetchall()
    refund_by_cat: dict = {}
    if flow_type == "expense":
        for r in conn.execute(
                f"SELECT o.category_id AS cid, COALESCE(SUM(r.amount),0) AS amt {_REFUND_JOIN} "
                "WHERE o.ledger_id=? AND o.date>=? AND o.date<? "
                "GROUP BY o.category_id", (ledger_id, dfrom, dto)):
            refund_by_cat[r["cid"]] = r["amt"]
    out = []
    for r in rows:
        refund = refund_by_cat.get(r["id"], 0)
        out.append({"id": r["id"], "name": r["name"] or UNCATEGORIZED_NAME,
                    "parent": r["parent"],
                    "gross": r["gross"], "net": r["gross"] - refund})
    # 一级汇总视图（饼图用）：二级挂到其一级
    top = {}
    for r in out:
        key = r["parent"] or r["name"]
        top.setdefault(key, {"name": key, "gross": 0, "net": 0})
        top[key]["gross"] += r["gross"]
        top[key]["net"] += r["net"]
    return sorted(top.values(), key=lambda x: -x["net"])

def _stats_by_period(conn: sqlite3.Connection, ledger_id: int, span: int,
                     dfrom: str = "", dto: str = "") -> list[dict]:
    """按月(span=7)/按年(span=4)聚合：毛额/收入一条 GROUP BY，退款一条 GROUP BY
    （归因原消费日、按同一分段键分组；月中起止的段裁剪由 WHERE 天然完成——
    此前逐段 _refund_sum 的 N+1，12 个月趋势就是 1+12 次查询）。"""
    where = " AND t.date>=? AND t.date<?" if dfrom else ""
    rows = conn.execute(
        f"""SELECT substr(t.date,1,{span}) AS p,
              SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END) AS gross,
              SUM(CASE WHEN t.type='income' THEN t.amount ELSE 0 END) AS income
              FROM transactions t WHERE t.ledger_id=?{where} GROUP BY p ORDER BY p""",
        (ledger_id, *([dfrom, dto] if dfrom else ()))).fetchall()
    refunds = {r["p"]: r["amt"] for r in conn.execute(
        f"SELECT substr(o.date,1,{span}) AS p, SUM(r.amount) AS amt {_REFUND_JOIN} "
        f"WHERE o.ledger_id=?{' AND o.date>=? AND o.date<?' if dfrom else ''} GROUP BY p",
        (ledger_id, *([dfrom, dto] if dfrom else ())))}
    out = []
    for r in rows:
        if not re.fullmatch(r"\d{4}-\d{2}" if span == 7 else r"\d{4}", r["p"] or ""):
            continue   # 遗留坏日期行（norm_date 收紧前入库）不参与分段，免一行拖垮整个接口
        refund = refunds.get(r["p"], 0)
        out.append({("month" if span == 7 else "year"): r["p"], "gross": r["gross"],
                    "income": r["income"], "net": r["gross"] - refund})
    return out

def stats_trend(conn: sqlite3.Connection, ledger_id: int, dfrom: str, dto: str) -> list[dict]:
    return _stats_by_period(conn, ledger_id, 7, dfrom, dto)

def stats_yearly(conn: sqlite3.Connection, ledger_id: int) -> list[dict]:
    """全部数据按年聚合：{year, gross, income, net}。"""
    return _stats_by_period(conn, ledger_id, 4)

def jx(col: str, path: str = "$.attribution") -> str:
    """tags 安全取值 SQL 片段（单点）：库中若混入非法 JSON 文本（手改库/归一前的历史
    脏行），裸 json_extract 抛 malformed JSON → 流水列表/归属统计/导入 rescan 全 500，
    一个脏行拖垮全页面。json_valid 前置守卫把脏行当 NULL（与 Python 侧 jloads 容错同口径）；
    tags 为 NULL 时 json_valid 返回 NULL 同样短路，行为与裸 json_extract 一致。"""
    return f"CASE WHEN json_valid({col}) THEN json_extract({col},'{path}') END"

def attribution_rows(conn: sqlite3.Connection, ledger_id: int, dfrom: str, dto: str) -> list[dict]:
    """归属汇总：先按统计键、再按归属分组两级聚合（统计页 /api/stats/attribution、
    导出 xlsx 归属 sheet、AI 对话查账 kind=attribution 三方共用，唯一实现防口径漂移）。
    有组的归属并成一行（person=组名，members=成员明细），无组按统计键单列；
    统计键 = 归属名[·平台]——app 手动/AI 记账保持纯归属名（同一人手动与平台账单分列），
    导入平台账单带平台后缀。dfrom 含端、dto 排他上界（与其他 stats_* 一致）。"""
    _PLAT = "CASE t.source " + " ".join(
        f"WHEN '{s}' THEN '{cn}'" for s, cn in PLATFORM_CN.items()) + " ELSE '' END"
    _attr = jx("t.tags")
    _person = (f"{_attr} || "
               f"CASE WHEN {_PLAT}='' THEN '' ELSE '·' || {_PLAT} END")
    sql = (f"SELECT COALESCE(g.id, g2.id) AS gid, COALESCE(g.name, g2.name) AS group_name, "
           f"{_person} AS person, COUNT(*) AS cnt, "
           "SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END) AS expense, "
           "SUM(CASE WHEN t.type='income'  THEN t.amount ELSE 0 END) AS income "
           "FROM transactions t "
           # 分组成员=归属名（非统计键）：剥平台后缀后查组，新导任一平台账单自动继承分组；
           "LEFT JOIN attributions a ON a.ledger_id=t.ledger_id "
           f"AND a.name={_attr} "
           "LEFT JOIN attribution_groups g ON g.id=a.group_id "
           "LEFT JOIN attribution_groups g2 ON g2.ledger_id=t.ledger_id "
           f"AND g2.name={_attr} "
           "WHERE t.ledger_id=:lid AND t.type IN ('expense','income') "
           "AND t.date>=:dfrom AND t.date<:dto "
           f"AND {_attr} IS NOT NULL "
           f"AND TRIM({_attr})!='' "
           "GROUP BY g.id, person")
    groups: dict[int, dict] = {}
    plain: list[dict] = []
    for r in conn.execute(sql, {"lid": ledger_id, "dfrom": dfrom, "dto": dto}):
        row = {"person": r["person"], "cnt": r["cnt"], "expense": r["expense"], "income": r["income"]}
        if r["gid"] is None:
            row["group"] = False
            plain.append(row)
            continue
        g = groups.setdefault(r["gid"], {"person": r["group_name"], "group": True,
                                         "cnt": 0, "expense": 0, "income": 0, "members": []})
        g["members"].append(row)
        g["cnt"] += row["cnt"]
        g["expense"] += row["expense"]
        g["income"] += row["income"]
    for g in groups.values():
        g["members"].sort(key=lambda m: (m["expense"], m["income"]), reverse=True)
    out = list(groups.values()) + plain
    out.sort(key=lambda r: (r["expense"], r["income"]), reverse=True)
    return out

# 人情往来取数口径：一级分类为「人情往来」（收支两侧同名分类都算）；
# 名称取 COALESCE(name, note)，与流水列表的人情明细过滤（server.list_transactions）共用
RENQING_NAME_SQL = "COALESCE(NULLIF(TRIM(t.name),''), NULLIF(TRIM(t.note),''), '（未命名）')"
RENQING_CAT_SQL = ("t.category_id IN (SELECT id FROM categories "
                   "WHERE ledger_id=? AND name='人情往来' AND parent_id IS NULL)")

def opt_bounds(dfrom: str, dto: str, col: str = "date") -> tuple[str, list]:
    """可选区间过滤片段：dfrom 含端、dto 排他上界（「含当天」由调用方经 incl_to_excl
    或前端 rangeDto 单点转换，db 层只认排他），空串不限。
    db 侧统计与 server._date_where 共用，边界语义单源防漂移。"""
    frag, args = "", []
    if dfrom:
        frag += f" AND {col}>=?"
        args.append(dfrom)
    if dto:
        frag += f" AND {col}<?"
        args.append(dto)
    return frag, args

def incl_to_excl(day: str) -> str:
    """含端末日（YYYY-MM-DD）→ 排他上界（次日）：db 区间约定「date < dto」的换算单点，
    ai 工具区间（LLM 给 [from,to] 闭区间）等调用方不再各自手写 +1 天。"""
    return (datetime.strptime(day[:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

def excl_to_incl(dto: str, default: str | None = None) -> str:
    """排他上界 → 含端末日（-1 天，显示用）；空/非法返回 default（None 则原样返回）。
    bot 列表标题（给用户看的「~到哪天」）与 ai 预算月数折算共用。"""
    fallback = dto if default is None else default
    try:
        return (datetime.strptime((dto or "")[:10], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return fallback

def range_months(dfrom: str, dto: str) -> int:
    """区间覆盖的自然月数（首尾各算一个月；dfrom 含端、dto 排他上界）：预算折算
    「区间预算=月预算×月数」的月数单源——AI 查账 _budget_tool 与设置页账户/预算行
    共用，此前前后端各实现一遍、靠注释互相同步。"""
    end = datetime.strptime((dto or "")[:10], "%Y-%m-%d") - timedelta(days=1)   # 含端末日
    return (end.year * 12 + end.month) - (int(dfrom[:4]) * 12 + int(dfrom[5:7])) + 1

def stats_renqing(conn: sqlite3.Connection, ledger_id: int,
                  dfrom: str = "", dto: str = "") -> list[dict]:
    """人情往来汇总（原实现自 server.py 迁入：统计页 /api/stats/renqing 与 AI 查账共用，
    单条 GROUP BY 直接聚合）。given=我给出（支出）、received=我收到（收入），
    balance=given-received>0 即对方尚未还礼。统计页不传区间=全量；AI 按问句时间
    传区间（dfrom 含端、dto 排他上界，空串不限）。"""
    frag, bounds = opt_bounds(dfrom, dto, "t.date")
    rows = conn.execute(
        f"SELECT {RENQING_NAME_SQL} AS person, "
        "SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END) AS given, "
        "SUM(CASE WHEN t.type='income'  THEN t.amount ELSE 0 END) AS received, "
        "MAX(t.date) AS last "
        "FROM transactions t "
        f"WHERE t.ledger_id=? AND {RENQING_CAT_SQL}{frag} "
        f"GROUP BY {RENQING_NAME_SQL} "
        "ORDER BY given - received DESC", (ledger_id, ledger_id, *bounds)).fetchall()
    return [{**dict(r), "balance": r["given"] - r["received"]} for r in rows]

def account_spend_map(conn: sqlite3.Connection, ledger_id: int,
                      dfrom: str = "", dto: str = "") -> dict:
    """每账户 (毛支出, 退款) 金额（分）：expense 记在付款账户 account_id 上、按自身日期；
    退款冲减记在**原消费的付款账户**、按原消费日期——与 summary/calendar/category/trend 的
    「退款归因原消费」同口径（此前记在退款落账账户/退款日期：跨账户退款会让原付款账户虚高、
    退款落账账户出现负支出）。原消费无账户时回落退款行自身账户，仍无则归 None 键。
    server 账户列表的 spend 列与 AI kind=account 共用此口径。
    dfrom 含端、dto 排他上界，空串不限。"""
    efrag, ebounds = opt_bounds(dfrom, dto, "date")
    rfrag, rbounds = opt_bounds(dfrom, dto, "o.date")
    out: dict = {}
    for a, amt in conn.execute(
            "SELECT account_id, amount FROM transactions "
            f"WHERE ledger_id=? AND type='expense'{efrag}",
            (ledger_id, *ebounds)):
        g, r = out.get(a, (0, 0))
        out[a] = (g + amt, r)
    for a, amt in conn.execute(
            f"SELECT COALESCE(o.account_id, r.account_id), r.amount {_REFUND_JOIN} "
            f"WHERE o.ledger_id=?{rfrag}",
            (ledger_id, *rbounds)):
        g, rr = out.get(a, (0, 0))
        out[a] = (g, rr + amt)
    return out

def account_income_map(conn: sqlite3.Connection, ledger_id: int,
                       dfrom: str = "", dto: str = "") -> dict:
    """每账户收入金额（分）：type='income' 记 account_id；NULL 归入 None 键（未指定）。
    与 account_spend_map 的支出侧对应，AI kind=account 问「每账户收入」时用。
    dfrom 含端、dto 排他上界，空串不限。"""
    frag, bounds = opt_bounds(dfrom, dto)
    return {a: amt for a, amt in conn.execute(
        "SELECT account_id, SUM(amount) FROM transactions "
        f"WHERE ledger_id=? AND type='income'{frag} GROUP BY account_id",
        (ledger_id, *bounds))}

def tx_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("tags", "ai_meta"):
        d[k] = jloads(d.get(k), default=d.get(k))   # 坏 JSON 原样保留
    return d

# 会话侧写入的文本列上限（入库 insert_tx 与改单 patch_tx 共用单源）：此前入库截 40/80、
# 改单另截 60/60，同一字段两道上限，改单后长名与新记的同类流水折行形态不一。
# 导入路径（import_apply 自有 INSERT）不截——账单原文保真，去重 hash 依赖原文。
TX_TEXT_MAX = {"name": 40, "merchant": 60, "note": 80}

def _text_clip(col: str, v) -> str | None:
    """文本列按 TX_TEXT_MAX 截断（None 保持 None，不入库为空串）。"""
    return None if v is None else str(v)[:TX_TEXT_MAX[col]]

# 流水查询公共件（bot 明细列表与 ai 对话附件共用一条口径）：列清单取两者并集
TX_COLS = ("t.id, t.type, t.amount, t.date, t.name, t.merchant, t.note, t.tags, t.order_no, "
           "t.source, t.need_confirm, t.receipt_path, t.refund_confidence, t.ai_meta, "
           "c.name AS category_name, a.name AS account_name")
TX_FROM = (" FROM transactions t LEFT JOIN categories c ON t.category_id=c.id "
           "LEFT JOIN categories c2 ON c.parent_id=c2.id "
           "LEFT JOIN accounts a ON t.account_id=a.id")

def _tx_filter(ledger_id: int, flow: str, dfrom: str, dto: str,
               category: str) -> tuple[str, list]:
    """流水查询过滤条件（fetch_tx_rows/count_tx_rows 单源共用，防两处 WHERE 口径漂移：
    列表与总数不一致正是 count 与 fetch 各写一份 WHERE 的经典漂移 bug）。"""
    sql = " WHERE t.ledger_id=? AND t.type=? AND t.date>=? AND t.date<?"
    args: list = [ledger_id, flow, dfrom, dto]
    if category:
        sql += " AND (c.name=? OR c2.name=?)"
        args += [category, category]
    return sql, args

def fetch_tx_rows(conn: sqlite3.Connection, ledger_id: int, *, flow: str = "expense",
                  dfrom: str = "2000-01-01", dto: str = "2100-01-01", category: str = "",
                  limit: int | None = None, over_count: bool = False) -> list:
    """按收支侧/日期区间/分类取流水，时间倒序（bot 查询与 ai 附件共用）。
    category 命中一级或其二级（精确名）；over_count=True 附 COUNT(*) OVER() AS cnt
    （LIMIT 前总数，首屏缩略+总数一次取回）；limit=None 取全部。
    JOIN categories 为 LEFT：无分类的行也进列表（category_name 为 NULL，渲染层回落
    类型名）——此前 INNER 会让 bot「对应明细」/AI 附件静默漏掉这些行，与统计页
    「未分类」桶口径不一致。"""
    sql, args = _tx_filter(ledger_id, flow, dfrom, dto, category)
    sql = (f"SELECT {TX_COLS}" + (", COUNT(*) OVER() AS cnt" if over_count else "")
           + TX_FROM + sql + " ORDER BY t.date DESC, t.id DESC")
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return conn.execute(sql, args).fetchall()

def count_tx_rows(conn: sqlite3.Connection, ledger_id: int, *, flow: str = "expense",
                  dfrom: str = "2000-01-01", dto: str = "2100-01-01", category: str = "") -> int:
    """与 fetch_tx_rows 同 WHERE 的总笔数（不含 ORDER/LIMIT）。JOIN 复用 TX_FROM 单源——
    此前内联了一份 INNER JOIN，fetch 改 LEFT 后两处漂移（列表 2 行、总数 1 行）。"""
    sql, args = _tx_filter(ledger_id, flow, dfrom, dto, category)
    sql = f"SELECT COUNT(*) n{TX_FROM}" + sql
    return conn.execute(sql, args).fetchone()["n"]

# 人情往来 vs 红包礼金 的拆分口径（is_small_generic_packet / renqing_need_confirm 共用）：
#   ≤200 元 + 只像通用红包（红包/拼手气/恭喜发财）+ 无强人情词 → 「红包礼金」（小额红包）
#   其余（大额，或带随礼/份子/结婚等明确人情含义）→ 「人情往来」

# 通用红包字样：小额（如群红包/拼手气）大概率不是人情往来
RENQING_GENERIC_PACKET = ("红包", "拼手气", "恭喜发财")
RENQING_SMALL_PACKET_CENTS = 20000          # ≤200 元视为小额红包
RENQING_STRONG_HINTS = ("随礼", "礼金", "份子", "人情", "结婚", "婚", "白事", "满月", "乔迁", "压岁", "寿")
# 明确的群红包/拼手气字样：确定不是人情，红包礼金直接入库不再人工核实
RENQING_PACKET_CERTAIN = ("拼手气", "群红包", "恭喜发财")

def is_small_generic_packet(text: str, amount: int | None) -> bool:
    """小额通用红包判定：≤200 元 + 只像普通红包字样 + 无强人情词。
    归「红包礼金」而非「人情往来」；收支两侧同一口径。"""
    return (amount is not None and amount <= RENQING_SMALL_PACKET_CENTS
            and any(p in (text or "") for p in RENQING_GENERIC_PACKET)
            and not any(h in (text or "") for h in RENQING_STRONG_HINTS))

def renqing_need_confirm(text: str) -> bool:
    """红包礼金是否待确认的统一口径（词典/复核/AI 三处共用）：
    带拼手气/群红包/恭喜发财字样直接入库，其余小额红包仍待核实。"""
    return not any(w in (text or "") for w in RENQING_PACKET_CERTAIN)

# 典型场景：手动/AI 记了「今天早餐15」，月底微信账单导入又来一笔同日同额 15——
# row_hash 精确去重只防「同一行」重导入，防不了跨来源的真实重复消费。
# 口径：(日期[到天], 类型, 金额[分]) 三元组相同 = 疑似；只提示不拦截——
# 同日同额完全可能是真实消费（两杯同价咖啡），由用户在确认环节决定去留。

def dup_hints(conn: sqlite3.Connection, ledger_id: int, items: list, rows: list | None = None) -> int:
    """给 items（dict 列表，含 type/amount[分]/date）就地注记 dup_hint 字段，返回命中数。
    kind=db：与库中已有流水（含待确认）同日同额同类型，hint 带对方 {id,name,date,amount,source}；
    kind=batch：与本批更早一笔相同（如一句话解析出两笔一样），hint 带 {no=第几笔}。
    date 参与 key 前截到天（草稿/导入行带的 HH:MM 不参与）。
    rows：导入预览流程传入的账本快照行（免重复查库）；缺省自行加载。"""
    dbmap: dict = {}
    if rows is None:
        rows = conn.execute("SELECT id, type, amount, date, name, source FROM transactions "
                            "WHERE ledger_id=?", (ledger_id,)).fetchall()
    for r in rows:
        dbmap.setdefault(((r["date"] or "")[:10], r["type"], r["amount"]), []).append(
            {"id": r["id"], "name": r["name"] or "", "date": r["date"],
             "amount": r["amount"], "source": r["source"] or ""})
    seen: dict = {}
    for i, it in enumerate(items):
        key = ((it.get("date") or "")[:10], it.get("type") or "expense", it.get("amount") or 0)
        hit = dbmap.get(key)
        if hit:
            it["dup_hint"] = {"kind": "db", "of": hit[0]}
        elif key in seen:
            it["dup_hint"] = {"kind": "batch", "no": seen[key] + 1}
        else:
            seen[key] = i
    return sum(1 for it in items if "dup_hint" in it)

# 宽松日期写法（2026/11/1、2026.11.01、2026年11月1日 20点30、全角数字）：LLM/OCR 常见。
# 非 ISO 串入库会破坏统计 date>=? AND date<? 的字典序区间比较，流水从统计里消失
_LOOSE_DATE = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*[日号]?"
                         r"(?:[T\s]*(\d{1,2})\s*[:时点]\s*(?:(\d{1,2})(?:\s*[:分]\s*(\d{1,2}))?)?)?")
_ISO_DT = re.compile(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?")
_ISO_FMT = {10: "%Y-%m-%d", 16: "%Y-%m-%d %H:%M", 19: "%Y-%m-%d %H:%M:%S"}

def iso_date(s: str) -> str:
    """日期格式归一（不补位）：宽松写法 → YYYY-MM-DD[ HH:MM[:SS]]（「8点」补 :00）；
    ISO 输入原样；认不出的原样返回——放不放行由 norm_date 把关。
    match 只锚定开头，必须再验「覆盖到串尾」：否则「2026-10-011」的尾 1 被静默吞掉、
    「2026-10-01晚上」的尾文本无声丢弃，错日期以合法形态入库（AI/OCR 常见）。"""
    s = (s or "").strip()
    s2 = unicodedata.normalize("NFKC", s)   # NFKC：全角数字/符号转半角
    m = _LOOSE_DATE.match(s2)
    if not m or m.end() != len(s2):
        return s
    y, mo, d, hh, mi, ss = m.groups()
    out = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    if hh is not None:
        out += f" {int(hh):02d}:{int(mi or 0):02d}" + (f":{int(ss):02d}" if ss else "")
    return out

def is_valid_date(s: str) -> bool:
    """ISO（10/16/19 位）且日历/时刻合法——norm_date 的放行口径（ai 草稿/小票同用）。"""
    if not _ISO_DT.fullmatch(s or ""):
        return False
    try:
        datetime.strptime(s, _ISO_FMT[len(s)])
        return True
    except ValueError:
        return False

def iso_date_ok(s: str) -> str | None:
    """归一 + 放行一步完成（放行口径与 norm_date 一致）：宽松写法 → ISO；
    认不出/日历非法返回 None。「不拒用户、按场景兜底」的入口（AI 草稿兜今天、
    小票兜空、bot 编辑跳字段）共用，替代各处 iso_date+is_valid_date 两行式。"""
    s = iso_date(s)
    return s if is_valid_date(s) else None

def norm_date(s: str, *, old: str = "") -> str:
    """日期统一 19 位（所有写库路径的出口）：iso_date 归一后严格校验，认不出或日历非法
    （2026-13-45）抛 ValueError——坏日期入库会让年报/趋势分段 int() 崩、区间统计漏行。
    16 位补秒；10 位沿用旧记录时刻，无旧记录用当前时刻。"""
    s = iso_date(s)
    if not is_valid_date(s):
        raise ValueError(f"日期无法识别：{s[:20]!r}")
    if len(s) == 16:
        return s + ":00"
    if len(s) == 10:
        t = (old or "")[11:19]
        if re.fullmatch(r"\d{2}:\d{2}", t):
            t += ":00"
        elif not re.fullmatch(r"\d{2}:\d{2}:\d{2}", t):
            t = datetime.now().strftime("%H:%M:%S")
        return f"{s} {t}"
    return s

# 流水值域单源：所有写库路径（App TxIn / 编辑 / 批量 / 待确认 resolve / 导入 confirm /
# bot）共用——坏值落库会让导出 type_map[type] KeyError、统计 amount 求和失真且无 UI 可修
TX_TYPES = frozenset(("expense", "income", "transfer"))

def check_tx_type(type_: str) -> None:
    """type 值域校验（单字段入口，TxIn 校验器/patch_tx 只验单字段时用）：不合法抛
    ValueError，HTTP 层沿用 _tx_date 同款 400 映射。"""
    if type_ not in TX_TYPES:
        raise ValueError(f"流水类型必须是 expense/income/transfer（收到 {type_!r}）")

def check_tx_amount(amount: int) -> None:
    """amount 值域校验（单字段入口，同上）：以分整数存储，负值冲抵统计、零额无
    统计意义（导入侧零额行本就按 skip 处理），一律拒绝。"""
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ValueError(f"金额必须是正整数分值（收到 {amount!r}）")
    if amount > 2 ** 63 - 1:
        # SQLite INTEGER 是 int64：超出范围的绑参直接 OverflowError 500（App 写入/导入 confirm 共用本闸）
        raise ValueError(f"金额超出上限（收到 {amount!r}）")

def check_tx_fields(type_: str, amount: int) -> None:
    """流水 type/amount 值域校验（写边界统一入口，双字段整验）：不合法抛 ValueError，
    HTTP 层沿用 _tx_date 同款 400 映射。"""
    check_tx_type(type_)
    check_tx_amount(amount)

def insert_tx(c: sqlite3.Connection, ledger_id: int, *, type: str, amount: int,
              category_id: int | None = None, account_id: int | None = None,
              to_account_id: int | None = None, date: str = "",
              name: str | None = None, merchant: str | None = None, note: str = "",
              tags: dict | None = None, receipt_path: str | None = None,
              source: str = "app", ai_meta: dict | None = None,
              order_no: str | None = None, need_confirm: bool = False,
              manual_cat: bool = False) -> int:
    """单笔流水插入（与原 server._insert_tx 同构）：row_hash 掺入当前时刻，
    手工/机器人来源永不撞导入去重索引。返回新行 id。
    （微信机器人='bot'、导入=平台名，dup_hint 与流水标签可区分三方）。
    manual_cat：分类是否用户手选（App 记一笔带分类置位；bot/AI 自动路径不置），
    置位的行不参与数据修复的词典纠正。"""
    check_tx_fields(type, amount)   # 值域单源：bot/AI 等不经 HTTP 校验的直写路径同样拦
    date = norm_date(date)
    row_hash = hashlib.md5(
        f"manual|{date}|{amount}|{note}|{datetime.now().isoformat()}".encode()).hexdigest()
    cur = c.execute(
        """INSERT INTO transactions(ledger_id, type, amount, category_id, account_id, to_account_id,
           date, name, merchant, note, tags, receipt_path, source, ai_meta, order_no, need_confirm,
           manual_cat, row_hash)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ledger_id, type, amount, category_id, account_id, to_account_id,
         date, _text_clip("name", name), _text_clip("merchant", merchant),
         _text_clip("note", note), _j(tags or {}), receipt_path, source, _j(ai_meta or {}),
         order_no, 1 if need_confirm else 0, 1 if manual_cat else 0, row_hash))
    return cur.lastrowid

def patch_tx(c: sqlite3.Connection, tid: int, fields: dict) -> bool:
    """字段级部分更新（微信 bot 编辑回合用）：只改给出的列，其余保持原值。
    可改列：amount(分)/category_id(连带 manual_cat=1)/account_id/date/name/merchant/note
    （文本列按 TX_TEXT_MAX 截断，与 bot 入库同口径）。空 fields 视为成功（不查行存在）。
    退款感知：改日期先过日期序不变式（破坏抛 ValueError，调用方已有「修改未生效」提示）；
    改金额/日期后重算退款对双方的 refund_status（与 App 编辑接口同源 refresh_pair_status）。"""
    sets, args = [], []
    if fields.get("amount") is not None:
        amt = int(fields["amount"])
        check_tx_amount(amt)   # 值域单源：bot 编辑路径的 0/负金额在此拦下，不让坏值冲抵统计
        sets.append("amount=?")
        args.append(amt)
    if "category_id" in fields:
        sets += ["category_id=?", "manual_cat=1"]   # 用户改分类 → 数据修复不再触碰
        args.append(fields["category_id"])
    if "account_id" in fields:
        sets.append("account_id=?")
        args.append(fields["account_id"])
    if fields.get("date"):   # 传旧值：只改日期时保留原时刻（与 App 编辑接口同口径）
        old = c.execute("SELECT date FROM transactions WHERE id=?", (tid,)).fetchone()
        new_date = norm_date(str(fields["date"]), old=old["date"] if old else "")
        assert_date_order(c, tid, new_date)
        sets.append("date=?")
        args.append(new_date)
    for col in ("name", "merchant", "note"):
        if fields.get(col) is not None:
            sets.append(f"{col}=?")
            args.append(_text_clip(col, fields[col]))
    if not sets:
        return True
    args.append(tid)
    cur = c.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id=?", args)
    if fields.get("amount") is not None or fields.get("date"):
        refresh_pair_status(c, tid)
    return cur.rowcount > 0

# 解除退款关联的统一字段清理（delete_tx / server._link_refund 取消分支 / undo_batch 共用）：
# 三字段必须一起清，漏一列即半清理脏态；将来退款态加列只改这里一处
UNLINK_REFUND_SET = "refund_for=NULL, refund_status='none', refund_confidence=NULL"

# ---------- 退款状态转移单点（写侧根因收口） ----------
# 退款不变式：refund_for 非空 ⇒ 本行 type='transfer'、目标为本账本 type='expense' 行、
# o.date ≤ r.date、refund_status=refund_status(o.amount, r.amount)。
# 此前四列的写路径有三套并行实现（server._link_refund / 导入 rescan 手写 UPDATE /
# 导入 INSERT 直信客户端），每版审查都在其中一处再补一个口子。现在四列只允许由
# 本节函数写；调用方（server/import_apply/bot）只做入口翻译，不再手写 UPDATE。

def link_refund(c: sqlite3.Connection, tid: int, origin_id: int, amount: int,
                confidence: str | None) -> None:
    """建立退款关联（手动编辑/队列确认/导入 rescan 共用；取消关联走 clear_refund）。
    全部不变式校验在此单点，非法抛 ValueError（HTTP 层转 400，bot 层显示提示）：
      自指 / 跨账本 / 目标非 expense / 换链日期序 / 本行已被其他退款指向。
    最后这条（下游指向检查）此前缺失：已作为原消费的行可再被链成退款行，强写
    type='transfer' 让指向它的退款在 _refund_sum 里扣减静默消失（一笔钱两种视图）。"""
    row = c.execute("SELECT ledger_id, date, refund_for FROM transactions WHERE id=?", (tid,)).fetchone()
    if not row:
        raise ValueError("流水不存在")
    o = c.execute("SELECT ledger_id, type, amount, date FROM transactions WHERE id=?",
                  (origin_id,)).fetchone()
    if origin_id == tid:
        # 自指会让本行 type 强转 transfer、从支出统计里静默消失（o 即自身，其余校验全过）
        raise ValueError("退款不能关联自身")
    if not o or o["ledger_id"] != row["ledger_id"] or o["type"] != "expense":
        raise ValueError("退款只能关联本账本的支出流水")
    if c.execute("SELECT 1 FROM transactions WHERE refund_for=? LIMIT 1", (tid,)).fetchone():
        raise ValueError("本行已是其他退款的原消费，不能同时作为退款关联；请先取消那条退款行的关联")
    if origin_id != (row["refund_for"] or 0) and (o["date"] or "")[:10] > (row["date"] or "")[:10]:
        raise ValueError("原始订单日期晚于退款日期，不能关联")
    c.execute("UPDATE transactions SET refund_for=?, refund_status=?, refund_confidence=?, "
              "type='transfer' WHERE id=?",
              (origin_id, refund_status(o["amount"], amount), confidence, tid))

def clear_refund(c: sqlite3.Connection, tid: int, *, back_to_queue: bool) -> None:
    """解链单点：清退款三列。back_to_queue=True 同时置 need_confirm=1 回待确认队列
    （原消费被删/整批撤销/批内目标行被跳过——退款失去扣减对象，须人工重链或 rescan
    重扫，漏置即成无提示无自愈的孤儿退款）；False=用户主动取消关联（意图明确，
    rescan 不得再扫，need_confirm 由调用方语义决定）。"""
    suffix = ", need_confirm=1" if back_to_queue else ""
    c.execute(f"UPDATE transactions SET {UNLINK_REFUND_SET}{suffix} WHERE id=?", (tid,))

def orphan_origin_refunds(c: sqlite3.Connection, origin_ids: int | list) -> None:
    """原消费消失（删行/撤批）后，指向它的全部退款行统一解链+回待确认（孤儿自愈单点）。
    origin_ids 可为单 id 或 id 列表（整批撤销按 chunk 批量解链，避免逐行 N 次 UPDATE）。"""
    if isinstance(origin_ids, int):
        c.execute(f"UPDATE transactions SET {UNLINK_REFUND_SET}, need_confirm=1 WHERE refund_for=?",
                  (origin_ids,))
        return
    ids = [i for i in origin_ids if i is not None]
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        c.execute(f"UPDATE transactions SET {UNLINK_REFUND_SET}, need_confirm=1 "
                  f"WHERE refund_for IN ({','.join('?' * len(part))})", part)

def refund_link_gates(conn: sqlite3.Connection, ledger_id: int, p: dict):
    """导入落库闸（_insert_rows INSERT 前调用，仅对 refund_for 非空的正链行）：校验
    退款链合法性并重算状态。返回 (refund_for, refund_status, refund_confidence) 落库
    三元组；退款链非法返回 None → 调用方整行跳过（与引用非法行同风格）。此前三列直信
    客户端 confirm 体：伪造 full+high 可免确认静默入库。status 一律按 DB 重算；
    行 type 闸（带链必须 transfer）由调用方统一把守——它对批内负序号行同样要拦，
    在调用方一处检查即覆盖正/负两类（此前闸内闸外各一份）。"""
    rf = p.get("refund_for")
    o = conn.execute("SELECT amount, date FROM transactions WHERE id=? AND ledger_id=? AND type='expense'",
                     (rf, ledger_id)).fetchone()
    if not o:
        return None
    if (o["date"] or "")[:10] > (p.get("date") or "")[:10]:
        # 日期序与 link_refund 同口径（原消费 ≤ 退款日）：此前只校验 type/归属，
        # 直调体可落"当时能入库、取消后重链被拒"的口径分叉行
        return None
    return rf, refund_status(o["amount"], p["amount"]), p.get("refund_confidence")

def refresh_pair_status(c: sqlite3.Connection, tid: int) -> None:
    """编辑金额后重算退款对双方的 refund_status（此前 status 停留旧值：改退款行或原消费
    的金额，full/partial 标签不再正确）。只重算标签，不产生/取消关联，与 link/clear 互补。"""
    row = c.execute("SELECT id, refund_for, amount FROM transactions WHERE id=?", (tid,)).fetchone()
    if not row:
        return
    if row["refund_for"]:
        o = c.execute("SELECT amount, type FROM transactions WHERE id=?", (row["refund_for"],)).fetchone()
        if o and o["type"] == "expense":
            c.execute("UPDATE transactions SET refund_status=? WHERE id=?",
                      (refund_status(o["amount"], row["amount"]), tid))
    for r in c.execute("SELECT id, amount FROM transactions WHERE refund_for=? AND type='transfer'",
                       (tid,)).fetchall():
        c.execute("UPDATE transactions SET refund_status=? WHERE id=?",
                  (refund_status(row["amount"], r["amount"]), r["id"]))

def assert_date_order(c: sqlite3.Connection, tid: int, new_date: str) -> None:
    """编辑日期前校验退款对「原消费日 ≤ 退款日」不变式（此前只在换链时校验，编辑日期
    可静默破坏，导出回环/再匹配依赖日期序）。破坏抛 ValueError（HTTP 400 / bot 未生效）。"""
    row = c.execute("SELECT refund_for FROM transactions WHERE id=?", (tid,)).fetchone()
    if not row:
        return
    if row["refund_for"]:
        o = c.execute("SELECT date FROM transactions WHERE id=?", (row["refund_for"],)).fetchone()
        if o and (o["date"] or "")[:10] > new_date[:10]:
            raise ValueError("退款日期不能早于原消费日期；请一并调整，或先取消关联再改")
    for r in c.execute("SELECT date FROM transactions WHERE refund_for=?", (tid,)).fetchall():
        if new_date[:10] > (r["date"] or "")[:10]:
            raise ValueError("原消费日期不能晚于关联退款的日期；请一并调整，或先取消下游关联再改")

def delete_tx(c: sqlite3.Connection, tid: int) -> str | None:
    """删行 + 解除其他行对本行的退款关联（回待确认）；返回旧小票路径（调用方负责删文件）。"""
    row = c.execute("SELECT receipt_path FROM transactions WHERE id=?", (tid,)).fetchone()
    # 单笔删/批量删/bot 删除三条入口都走这里；级联收敛到 orphan_origin_refunds 单点
    orphan_origin_refunds(c, tid)
    c.execute("DELETE FROM transactions WHERE id=?", (tid,))
    return row["receipt_path"] if row else None
