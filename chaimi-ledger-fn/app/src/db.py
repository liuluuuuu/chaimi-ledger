"""SQLite schema、迁移、种子数据与查询。

金额一律 INTEGER 存分（DESIGN.md 第四章）。统计口径：净支出 = expense −
归因到原消费日期(o.date)的已匹配退款（8.5.1）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime

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
  budget INTEGER DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS app_meta (
  key TEXT PRIMARY KEY, value TEXT);

-- 多用户（MULTIUSER-PLAN）：账本归属 + 共享名单 + 打开过应用的飞牛用户档案。
-- uid = 网关注入的 X-Trim-Userid（fnOS 用户 uid，唯一键；username 可改名仅显示）。
CREATE TABLE IF NOT EXISTS ledger_members (
  ledger_id INTEGER NOT NULL, uid INTEGER NOT NULL,
  added_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (ledger_id, uid));
CREATE TABLE IF NOT EXISTS known_users (
  uid INTEGER PRIMARY KEY,
  username TEXT NOT NULL DEFAULT '',
  isadmin INTEGER DEFAULT 0,
  first_seen TEXT DEFAULT (datetime('now')),
  last_seen TEXT DEFAULT (datetime('now')));
"""

# 支出一级（15 分类体系：转账红包/投资理财迁 transfer，微信交易不留；
# 2026-09 依据三渠道实测账单补「交通出行」——支付宝原生有该分类，微信商户消费
# 里滴滴/12306/客运等公共出行原被挤入爱车养车；
# 2026-09 取消二级分类，并把「加油充电」从爱车养车拆为独立一级——实测加油/充电
# 是高频独立支出（湖北石油/中石化电子钱包等），与停车保养分开统计更清晰）
# 2026-09 取消二级分类后改为纯列表（原为 分类→子分类 字典，子类恒空）
# 2026-10 补「住房物业」——房贷/房租/物业费原挤在充值缴费，房贷是多数家庭
# 最大单笔固定支出，与水电燃气分开统计更清晰（老库由 init_db 一次性迁移补种）
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

# transfer 子类（7.6，不计收支；存 tags.transfer_sub）
TRANSFER_SUBCLASS = {
    "转账": "账户转账", "转账红包": "账户转账", "微信红包": "账户转账",
    "微信红包（单发）": "账户转账",
    "小金库": "理财申购", "投资理财": "理财申购", "转入零钱通": "理财申购",
    "余额宝": "理财申购", "余额宝-自动转入": "理财申购",
    "转入零钱通-来自零钱": "理财赎回", "赎回": "理财赎回",
    "信用卡还款": "信用卡还款", "信用借还": "信用卡还款",
    "余额互通": "账户充值", "充值": "账户充值", "退款": "退款",
}

DEFAULT_ACCOUNTS = ["现金", "支付宝", "微信支付"]

_local = threading.local()

_CONN_GEN = 0

def connect() -> sqlite3.Connection:
    """线程级连接：WAL + busy_timeout（FastAPI 多线程并发写必需）。
    恢复备份会替换磁盘上的 db 文件，旧线程缓存的连接仍握旧文件句柄（读到陈旧数据）——
    以代数标记作废，reset_connections() 后各线程下次取连接自动重开新文件。"""
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

# 导入来源（账单里的原始归属是真实数据，删组/删归属的清理永不触碰）；其余 manual/ai_* 为用户侧
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
        _ensure_column(conn, "ledgers", "owner_uid",
                       "ALTER TABLE ledgers ADD COLUMN owner_uid INTEGER")
        conn.execute("UPDATE attribution_groups SET avatar='f' WHERE avatar=''")
        if conn.execute("SELECT COUNT(*) FROM ledgers").fetchone()[0] == 0:
            seed()
        _migrate(conn)

def top_cat_id(conn: sqlite3.Connection, ledger_id: int, name: str,
               flow_type: str = "") -> int | None:
    """按名字取一级分类 id；flow_type 非空时按类型消歧。
    同名收支分类（如「人情往来」两侧都有）必须带类型查，否则 fetchone() 取到哪条不确定。"""
    sql = ("SELECT id FROM categories WHERE ledger_id=? AND parent_id IS NULL AND name=?"
           + (" AND type=?" if flow_type else "") + " LIMIT 1")
    args = (ledger_id, name) + ((flow_type,) if flow_type else ())
    r = conn.execute(sql, args).fetchone()
    return r["id"] if r else None

def seed_ledger_defaults(conn: sqlite3.Connection, ledger_id: int, accounts: bool = True) -> None:
    """给账本播种默认分类（存在即跳过，只增不删，不动用户自建）+ 默认账户。
    新建账本（seed / server.create_ledger）与历史迁移（v2/v4/v8/v9）共用；
    迁移场景传 accounts=False 不动账户。"""
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

def create_ledger(c: sqlite3.Connection, name: str, icon: str = "", currency: str = "CNY",
                  owner_uid: int | None = None) -> int:
    """建账本 + 播种默认分类/账户/映射（首建 seed 与 POST /api/ledgers 单源，
    存在即跳过、只增不删；v6 起映射为账本级，新账本必须显式种）。返回新账本 id。"""
    cur = c.execute(
        "INSERT INTO ledgers(name, icon, currency, owner_uid) VALUES(?,?,?,?)",
        (name, icon, currency, owner_uid))
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

_MIGRATIONS = [
    ("m_housing_2026_10", _m_housing_2026_10),
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
    """'2026-09' → ('2026-09-01', '2026-10-01')."""
    y, m = map(int, month.split("-"))
    return f"{y:04d}-{m:02d}-01", _next_month(month)

def _next_month(month: str) -> str:
    """'YYYY-MM' → 'YYYY-MM+1-01'，跨年自动 +1。"""
    y, m = map(int, month.split("-"))
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{ny:04d}-{nm:02d}-01"

# ---------- 统计（8.5.1 口径：退款扣减归因到原消费日期 o.date） ----------

def _refund_sum(conn: sqlite3.Connection, ledger_id: int, dfrom: str, dto: str,
                category_id: int | None = None) -> int:
    """区间内已匹配退款合计（归因到原消费日，DESIGN 8.5.1 口径）。
    category_id 非空时只统计该分类（分类统计同口径）。"""
    sql = ("SELECT COALESCE(SUM(r.amount),0) FROM transactions r "
           "JOIN transactions o ON r.refund_for=o.id "
           "WHERE o.ledger_id=? AND o.type='expense' AND o.date>=? AND o.date<?")
    args: list = [ledger_id, dfrom, dto]
    if category_id is not None:
        sql += " AND o.category_id=?"
        args.append(category_id)
    return conn.execute(sql, args).fetchone()[0]

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

def stats_calendar(conn: sqlite3.Connection, ledger_id: int, month: str,
                   dfrom: str = "", dto: str = "") -> list[dict]:
    """按日聚合：支出/收入/退款扣减（退款归因原消费日，按日查一次）。
    dfrom/dto 缺省按 month 整月（统计页日历）；AI 对话传问句区间（如「上周每天」
    跨月时整月口径会漏天）。dfrom 含端、dto 排他上界。"""
    if not dfrom:
        dfrom, dto = month_range(month)
    refunds: dict[str, int] = {}
    for r in conn.execute(
            "SELECT substr(o.date,1,10) AS day, SUM(r.amount) AS amt "
            "FROM transactions r JOIN transactions o ON r.refund_for=o.id "
            "WHERE o.ledger_id=? AND o.type='expense' AND o.date>=? AND o.date<? "
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

def stats_category_range(conn: sqlite3.Connection, ledger_id: int, dfrom: str, dto: str,
                         flow_type: str = "expense") -> list[dict]:
    """分类聚合（饼图数据源）。expense 按净额。"""
    rows = conn.execute(
        """SELECT c.id, c.name, COALESCE(c2.name,'') AS parent,
              SUM(t.amount) AS gross
              FROM transactions t JOIN categories c ON t.category_id=c.id
              LEFT JOIN categories c2 ON c.parent_id=c2.id
              WHERE t.ledger_id=:lid AND t.type=:ft AND t.date>=:dfrom AND t.date<:dto
              GROUP BY c.id""",
        {"lid": ledger_id, "ft": flow_type, "dfrom": dfrom, "dto": dto}).fetchall()
    out = []
    for r in rows:
        refund = 0
        if flow_type == "expense":
            refund = _refund_sum(conn, ledger_id, dfrom, dto, r["id"])
        out.append({"id": r["id"], "name": r["name"], "parent": r["parent"],
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
    """按月(span=7)/按年(span=4)聚合：gross/income 单查询，net 需逐段查退款。
    月度可带区间，年度为全量数据。"""
    where = " AND t.date>=? AND t.date<?" if dfrom else ""
    rows = conn.execute(
        f"""SELECT substr(t.date,1,{span}) AS p,
              SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END) AS gross,
              SUM(CASE WHEN t.type='income' THEN t.amount ELSE 0 END) AS income
              FROM transactions t WHERE t.ledger_id=?{where} GROUP BY p ORDER BY p""",
        (ledger_id, *([dfrom, dto] if dfrom else ()))).fetchall()
    out = []
    for r in rows:
        if span == 7:
            seg_from, seg_to = r["p"] + "-01", _next_month(r["p"])
            key, k = r["p"], "month"
        else:
            seg_from, seg_to = f"{r['p']}-01-01", f"{int(r['p']) + 1}-01-01"
            key, k = r["p"], "year"
        refund = _refund_sum(conn, ledger_id, seg_from, seg_to)
        out.append({k: key, "gross": r["gross"], "income": r["income"],
                    "net": r["gross"] - refund})
    return out

def stats_trend(conn: sqlite3.Connection, ledger_id: int, dfrom: str, dto: str) -> list[dict]:
    return _stats_by_period(conn, ledger_id, 7, dfrom, dto)

def stats_yearly(conn: sqlite3.Connection, ledger_id: int) -> list[dict]:
    """全部数据按年聚合：{year, gross, income, net}。"""
    return _stats_by_period(conn, ledger_id, 4)

def attribution_rows(conn: sqlite3.Connection, ledger_id: int, dfrom: str, dto: str) -> list[dict]:
    """归属汇总：先按统计键、再按归属分组两级聚合（统计页 /api/stats/attribution、
    导出 xlsx 归属 sheet、AI 对话查账 kind=attribution 三方共用，唯一实现防口径漂移）。
    有组的归属并成一行（person=组名，members=成员明细），无组按统计键单列；
    统计键 = 归属名[·平台]——app 手动/AI 记账保持纯归属名（同一人手动与平台账单分列），
    导入平台账单带平台后缀。dfrom 含端、dto 排他上界（与其他 stats_* 一致）。"""
    _PLAT = ("CASE t.source WHEN 'alipay' THEN '支付宝' WHEN 'wechat' THEN '微信' "
             "WHEN 'jd' THEN '京东' WHEN 'meituan' THEN '美团' ELSE '' END")
    _person = (f"json_extract(t.tags,'$.attribution') || "
               f"CASE WHEN {_PLAT}='' THEN '' ELSE '·' || {_PLAT} END")
    sql = (f"SELECT COALESCE(g.id, g2.id) AS gid, COALESCE(g.name, g2.name) AS group_name, "
           f"{_person} AS person, COUNT(*) AS cnt, "
           "SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END) AS expense, "
           "SUM(CASE WHEN t.type='income'  THEN t.amount ELSE 0 END) AS income "
           "FROM transactions t "
           # 分组成员=归属名（非统计键）：剥平台后缀后查组，新导任一平台账单自动继承分组；
           "LEFT JOIN attributions a ON a.ledger_id=t.ledger_id "
           "AND a.name=json_extract(t.tags,'$.attribution') "
           "LEFT JOIN attribution_groups g ON g.id=a.group_id "
           "LEFT JOIN attribution_groups g2 ON g2.ledger_id=t.ledger_id "
           "AND g2.name=json_extract(t.tags,'$.attribution') "
           "WHERE t.ledger_id=:lid AND t.type IN ('expense','income') "
           "AND t.date>=:dfrom AND t.date<:dto "
           "AND json_extract(t.tags,'$.attribution') IS NOT NULL "
           "AND TRIM(json_extract(t.tags,'$.attribution'))!='' "
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

def _opt_bounds(dfrom: str, dto: str, col: str = "date") -> tuple[str, list]:
    """可选区间过滤片段：dfrom 含端、dto 排他上界，空串不限
    （stats_renqing / account_spend_map / account_income_map 共用，防边界语义漂移）。"""
    frag, args = "", []
    if dfrom:
        frag += f" AND {col}>=?"
        args.append(dfrom)
    if dto:
        frag += f" AND {col}<?"
        args.append(dto)
    return frag, args

def stats_renqing(conn: sqlite3.Connection, ledger_id: int,
                  dfrom: str = "", dto: str = "") -> list[dict]:
    """人情往来汇总（原实现自 server.py 迁入：统计页 /api/stats/renqing 与 AI 查账共用，
    单条 GROUP BY 直接聚合）。given=我给出（支出）、received=我收到（收入），
    balance=given-received>0 即对方尚未还礼。统计页不传区间=全量；AI 按问句时间
    传区间（dfrom 含端、dto 排他上界，空串不限）。"""
    frag, bounds = _opt_bounds(dfrom, dto, "t.date")
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
    """每账户 (毛支出, 退款) 金额（分）：expense 记在付款账户 account_id 上，
    退款转账（refund_for 非空的 transfer）记在钱退回的 account_id 上；
    account_id 为 NULL 的支出归入 None 键（未指定）。server 账户列表的 spend 列
    与 AI kind=account 共用此口径。dfrom 含端、dto 排他上界，空串不限。"""
    frag, bounds = _opt_bounds(dfrom, dto)
    out: dict = {}
    for ty, a, amt in conn.execute(
            "SELECT type, account_id, amount FROM transactions "
            "WHERE ledger_id=? AND (type='expense' OR "
            f"(type='transfer' AND refund_for IS NOT NULL AND account_id IS NOT NULL)){frag}",
            (ledger_id, *bounds)):
        g, r = out.get(a, (0, 0))
        out[a] = (g + amt, r) if ty == "expense" else (g, r + amt)
    return out

def account_income_map(conn: sqlite3.Connection, ledger_id: int,
                       dfrom: str = "", dto: str = "") -> dict:
    """每账户收入金额（分）：type='income' 记 account_id；NULL 归入 None 键（未指定）。
    与 account_spend_map 的支出侧对应，AI kind=account 问「每账户收入」时用。
    dfrom 含端、dto 排他上界，空串不限。"""
    frag, bounds = _opt_bounds(dfrom, dto)
    return {a: amt for a, amt in conn.execute(
        "SELECT account_id, SUM(amount) FROM transactions "
        f"WHERE ledger_id=? AND type='income'{frag} GROUP BY account_id",
        (ledger_id, *bounds))}

def tx_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["amount"] = d.get("amount", 0)
    for k in ("tags", "ai_meta"):
        d[k] = jloads(d.get(k), default=d.get(k))   # 坏 JSON 原样保留
    return d

# 流水查询公共件（bot 明细列表与 ai 对话附件共用一条口径）：列清单取两者并集
TX_COLS = ("t.id, t.type, t.amount, t.date, t.name, t.merchant, t.note, t.tags, t.order_no, "
           "t.source, t.need_confirm, t.receipt_path, t.refund_confidence, t.ai_meta, "
           "c.name AS category_name, a.name AS account_name")
TX_FROM = (" FROM transactions t JOIN categories c ON t.category_id=c.id "
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
    注意 JOIN categories 为 INNER：无分类的行不进列表（既有口径）。"""
    sql, args = _tx_filter(ledger_id, flow, dfrom, dto, category)
    sql = (f"SELECT {TX_COLS}" + (", COUNT(*) OVER() AS cnt" if over_count else "")
           + TX_FROM + sql + " ORDER BY t.date DESC, t.id DESC")
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return conn.execute(sql, args).fetchall()

def count_tx_rows(conn: sqlite3.Connection, ledger_id: int, *, flow: str = "expense",
                  dfrom: str = "2000-01-01", dto: str = "2100-01-01", category: str = "") -> int:
    """与 fetch_tx_rows 同 WHERE 的总笔数（不含 ORDER/LIMIT；JOIN 仅保留分类侧，计数不需账户）。"""
    sql, args = _tx_filter(ledger_id, flow, dfrom, dto, category)
    sql = ("SELECT COUNT(*) n FROM transactions t JOIN categories c ON t.category_id=c.id "
           "LEFT JOIN categories c2 ON c.parent_id=c2.id" + sql)
    return conn.execute(sql, args).fetchone()["n"]

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

def norm_date(s: str, *, old: str = "") -> str:
    """日期统一 19 位：datetime-local 给 16 位(YYYY-MM-DD HH:MM)补秒；
    10 位（旧调用方/导入）沿用旧记录时刻，无旧记录用当前时刻。"""
    s = (s or "").strip().replace("T", " ")
    if len(s) == 16:
        return s + ":00"
    if len(s) == 10:
        hm = (old or "")[11:19] or datetime.now().strftime("%H:%M:%S")
        return f"{s} {hm}"
    return s

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
    date = norm_date(date)
    row_hash = hashlib.md5(
        f"manual|{date}|{amount}|{note}|{datetime.now().isoformat()}".encode()).hexdigest()
    cur = c.execute(
        """INSERT INTO transactions(ledger_id, type, amount, category_id, account_id, to_account_id,
           date, name, merchant, note, tags, receipt_path, source, ai_meta, order_no, need_confirm,
           manual_cat, row_hash)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ledger_id, type, amount, category_id, account_id, to_account_id,
         date, name, merchant, note, _j(tags or {}), receipt_path, source, _j(ai_meta or {}),
         order_no, 1 if need_confirm else 0, 1 if manual_cat else 0, row_hash))
    return cur.lastrowid

def patch_tx(c: sqlite3.Connection, tid: int, fields: dict) -> bool:
    """字段级部分更新（微信 bot 编辑回合用）：只改给出的列，其余保持原值。
    返回是否命中行。可改列：amount(分)/category_id/account_id/date/name/merchant/note。"""
    sets, args = [], []
    if fields.get("amount") is not None:
        sets.append("amount=?")
        args.append(int(fields["amount"]))
    if "category_id" in fields:
        sets += ["category_id=?", "manual_cat=1"]   # 用户改分类 → 数据修复不再触碰
        args.append(fields["category_id"])
    if "account_id" in fields:
        sets.append("account_id=?")
        args.append(fields["account_id"])
    if fields.get("date"):
        sets.append("date=?")
        args.append(norm_date(str(fields["date"])))
    for col in ("name", "merchant", "note"):
        if fields.get(col) is not None:
            sets.append(f"{col}=?")
            args.append(str(fields[col])[:60])
    if not sets:
        return True
    args.append(tid)
    cur = c.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id=?", args)
    return cur.rowcount > 0

def delete_tx(c: sqlite3.Connection, tid: int) -> str | None:
    """删行 + 解除其他行对本行的退款关联；返回旧小票路径（调用方负责删文件）。"""
    row = c.execute("SELECT receipt_path FROM transactions WHERE id=?", (tid,)).fetchone()
    c.execute("UPDATE transactions SET refund_for=NULL, refund_status='none', refund_confidence=NULL "
              "WHERE refund_for=?", (tid,))
    c.execute("DELETE FROM transactions WHERE id=?", (tid,))
    return row["receipt_path"] if row else None
