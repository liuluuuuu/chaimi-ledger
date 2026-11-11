"""分类瀑布数据与工具：关键词词典（含例外/车牌/加油序）、映射查询、自愈（IMPORT_REFACTOR_PLAN §三）。

自愈（fix_candidates/_purge_bad_learned_mappings）自 1.0.1 起不再随导入解析自动执行——
改为 /api/repair/preview 只读列出 + /api/repair/apply 用户确认后应用（真机事故：
预览即改库把用户手动整理的分类静默翻回词典结果）。
1.0.2 起手动改分类不再自动回写学习映射（改一笔只对当笔负责，泛化成全局规则是
把单笔意图偷换成规则）——此后库里的 learned 映射均属历史遗留，修复区的
映射冲突提示/重置只对旧数据有意义；要立规则去映射页手动加。"""
from __future__ import annotations

import re
import sqlite3

import applog
import db as dbm
from import_common import _chunks, norm_account_name

_log = applog.get("import")

# ---------- 分类解析（三段式 + transfer 子类；二级分类已取消） ----------

# 一级分类关键词兜底（微信账单无分类列，靠商品/商户名识别；顺序=优先级；
# 只收高精度词——能跨类误伤的泛化词（如「店」「平台」）一律不收）。
# 2026-11 按高频词扩充：每类补通用业态词 + 头部连锁品牌。扩词纪律（踩过/排查过的坑）：
#   · 子串撞车不收或改长词：面包(面包车)、料理(料理机)、豆浆(豆浆机)、笔记本(纸本)、猫眼(门镜)、
#     公园(楼盘名)、喷漆/加气(建材)、物美(物美价廉)、宝宝(配偶昵称常见)、健康(健康鞋)
#   · 常见人名/泛义的品牌不收：李宁、荣耀(王者荣耀)、小米(粮食，改收「小米之家」)、苹果(水果)
#   · 前类会先中：同句含多类词时排前的类赢（餐饮在日用前、美容在爱车前→「汽车美容」走例外表）
TOP_KEYWORD_LEXICON = [
    ("工资薪酬", ["工资", "薪资", "奖金", "报酬", "薪酬", "劳务", "年终奖", "加班费", "绩效"]),
    # 红包/礼金合并进「人情往来」（小额通用红包切「红包礼金」，见 dbm.is_small_generic_packet）
    ("人情往来", ["压岁", "红包", "礼金", "随礼", "人情往来",
                  "份子", "人情", "花圈", "奠仪", "伴手礼"]),
    ("美容美发", ["发艺", "理发", "美发", "美容", "美甲", "护肤",
                  "发廊", "剪发", "烫发", "染发", "发型", "美睫", "美妆", "化妆品",
                  "剪头发", "洗剪吹", "烫头", "染头", "口红", "美瞳"]),
    ("医疗健康", ["药房", "药店", "诊所", "医院", "牙科", "体检", "医药", "挂号",
                  "卫生院", "卫生室", "门诊", "中医", "口腔", "眼科", "理疗", "疫苗", "保健品", "医保", "药业",
                  # 口语购药/常备药（「买药15」这类）：买药≠山药子串（买山药中间隔「山」）
                  "买药", "感冒药", "退烧药", "消炎药", "止疼药", "眼药水", "创可贴",
                  "看病", "看牙", "住院", "手术"]),
    ("加油充电", ["加油", "加油站", "充电桩", "中石化", "中国石化", "电子钱包", "石油",
                  "中海油", "壳牌", "加气站", "充电费"]),
    ("爱车养车", ["停车", "ETC", "etc", "洗车", "捷停车", "速停车", "云停", "JSPAY",
                  "4S店", "维保", "保养", "汽修", "汽车维修", "汽车养护", "轮胎",
                  "高速费", "过路费", "通行费", "车险", "年检", "违章", "补胎", "钣金", "机油", "车位"]),
    ("交通出行", ["滴滴", "打车", "快车", "顺风车", "出租车", "网约车", "12306", "中铁网络", "铁路",
                  "高铁", "客运站", "汽车客运", "地铁", "公交", "机票", "航空", "机场", "单车",
                  "哈啰", "青桔", "曹操出行", "花小猪", "嘀嗒", "代驾", "车票", "火车", "动车", "大巴",
                  "轮渡", "船票", "乘车"]),
    # 物业费原在充值缴费，2026-10 拆独立一级后移入。只收带"费"的显式词——裸「物业」
    # 真机误伤：物业公司在小区里代收停车费，账单商品名只有"XX物业公司"没有"停车"，
    # ¥3~25 的停车费全被荐改住房物业（2026-10-23 真机反馈）；公司名里的
    # 物业管理/物业服务同理不收，停车与物业费从文本上无法区分，留给待确认
    ("住房物业", ["房贷", "房租", "租房", "物业费", "物业管理费", "租金", "装修", "中介费", "房产"]),
    ("充值缴费", ["话费", "宽带", "水费", "电费", "燃气", "有线电视", "阿里云", "卡充值",
                  "采暖", "取暖", "暖气", "流量", "中国移动", "中国联通", "中国电信", "天然气"]),
    # 餐饮 vs 日用 口径（用户定，2026-11）：上馆子/现成吃食归餐饮美食；买菜、买肉、逛农贸市场
    # 等买食材归日用百货。餐饮排在日用之前——「烤肉店」先中餐饮「烤肉」，不被日用「肉店」吞掉
    ("餐饮美食", ["食堂", "餐厅", "餐饮", "吃饭", "早餐", "早饭", "午饭", "午餐", "晚饭", "晚餐", "烧烤", "小吃", "奶茶", "咖啡", "早点", "外卖", "面馆", "火锅", "饭馆", "快餐", "小吃部",
                  "饮品", "蛋糕", "烘焙", "甜品", "夜宵",
                  "餐馆", "夜市", "零食", "熟食", "汤圆", "卤", "绝味", "烤肉",
                  # 就餐场景词：整句带场景时先于日用组的裸食材词命中（「下馆子吃鱼」→餐饮不落日用）
                  "下馆子", "上馆子", "聚餐",
                  # 日常口语补充（「宵夜」与「夜宵」两说都收；料理/豆浆是吃食，机器走例外表进数码）
                  "宵夜", "早茶", "下午茶", "豆浆", "料理",
                  # 苹果/小米带吃喝后缀的明确词（裸词的数码歧义由 AI 层按金额分辨）
                  "苹果汁", "苹果醋", "小米粥",
                  # 含食材词的现成吃食（日用组收了裸食材词后防撞，餐饮在前先命中）
                  "牛肉面", "羊肉汤", "鸡蛋灌饼", "茶叶蛋", "臭豆腐", "土豆粉", "水果茶", "大排档",
                  "饭店", "酒楼", "酒家", "食府", "菜馆", "小馆", "私房菜", "农家乐", "自助餐", "西餐", "中餐",
                  "米线", "拉面", "麻辣烫", "冒菜", "串串", "烤串", "炸鸡", "鸡排", "汉堡", "披萨", "寿司",
                  "饺子", "馄饨", "包子", "煎饼", "粥铺", "烤鱼", "酸菜鱼", "小龙虾", "牛排",
                  "饮料", "酸奶", "冰淇淋", "雪糕", "面包店", "烧仙草", "饿了么",
                  "肯德基", "麦当劳", "必胜客", "德克士", "华莱士", "塔斯汀", "星巴克", "瑞幸", "库迪",
                  "蜜雪冰城", "喜茶", "奈雪", "茶百道", "古茗", "沪上阿姨", "霸王茶姬",
                  "海底捞", "西贝", "老乡鸡", "真功夫", "杨国福", "周黑鸭", "煌上煌", "紫燕", "鲍师傅", "好利来"]),
    ("母婴亲子", ["奶粉", "尿裤", "尿不湿", "婴儿", "母婴", "玩具", "孕妇", "孕婴", "儿童", "辅食", "奶瓶", "童车"]),
    ("日用百货", ["超市", "百货", "连锁", "便利", "生鲜", "五金", "家具", "家居", "建材", "纸巾",
                  "卫生巾", "护垫", "湿巾", "纸", "茶行", "名创优品",
                  "农贸市场", "市集", "菜市场", "买菜", "蔬菜", "肉店",
                  "菜场", "菜摊", "果蔬", "粮油", "水产", "冻品", "干货", "调料", "肉铺", "肉摊",
                  # 口语裸食材词（AI 记一笔说「鱼15 买白菜25」这类）：同买菜口径归日用。
                  # 只收无撞车的——苹果/小米被撞车坑钉着不收（数码歧义），面包不收（面包车），
                  # 含这些词的现成吃食（牛肉面/茶叶蛋/水果茶…）在餐饮组前置于此防误伤
                  "白菜", "青菜", "菠菜", "生菜", "土豆", "番茄", "西红柿", "黄瓜", "茄子", "豆角",
                  "萝卜", "大葱", "生姜", "大蒜", "韭菜", "蘑菇", "豆腐", "鸡蛋", "鸭蛋",
                  "山药", "红薯", "南瓜", "冬瓜", "丝瓜", "莲藕", "玉米",
                  "猪肉", "牛肉", "羊肉", "排骨", "五花肉", "鱼", "虾", "螃蟹", "大闸蟹", "海鲜",
                  "水果", "香蕉", "橘子", "橙子", "葡萄", "西瓜", "草莓", "芒果", "荔枝", "柚子",
                  "樱桃", "火龙果", "哈密瓜", "大米", "面粉", "食用油", "花生油", "酱油",
                  "日化", "洗衣液", "洗发水", "沐浴露", "牙膏", "牙刷", "洗洁精", "垃圾袋",
                  "厨具", "餐具", "锅具", "收纳", "毛巾", "床品", "灯具", "杂货", "小商品", "文具",
                  "肥皂", "雨伞", "雨衣",
                  "罗森", "美宜佳", "永辉", "沃尔玛", "家乐福", "大润发", "华润万家", "盒马", "山姆",
                  "麦德龙", "朴朴", "钱大妈", "无印良品"]),   # 纸=纸类日用（「纸尿裤」母婴在前先中）；茶行=茶叶店；买菜/肉/农贸=买食材
    ("服饰装扮", ["男装", "女装", "童装", "服装", "服饰", "羽绒服", "运动鞋", "皮鞋", "鞋店",
                  "衣服", "裤子", "外套", "衬衫", "毛衣", "卫衣", "连衣裙", "裙子", "内衣", "袜子", "帽子", "围巾",
                  "穿搭", "鞋服", "鞋业", "鞋城", "男鞋", "女鞋", "童鞋", "健康鞋",
                  "箱包", "皮具", "首饰", "饰品", "珠宝",
                  "优衣库", "海澜之家", "森马", "太平鸟", "安踏", "特步", "鸿星尔克", "耐克", "阿迪达斯",
                  "斯凯奇", "波司登", "周大福", "周生生", "老凤祥"]),
    ("文化休闲", ["影院", "电影", "游戏", "图书", "KTV", "健身", "景区", "门票", "旅游",
                  "影城", "淘票票", "演出", "演唱会", "话剧", "剧场", "展览", "博物馆", "游乐", "乐园",
                  "网吧", "网咖", "台球", "棋牌", "桌游", "剧本杀", "密室", "游泳", "瑜伽", "球馆",
                  "书店", "旅行社", "索道", "游船",
                  "视频会员", "唱歌", "打球", "羽毛球", "钓鱼"]),
    ("教育培训", ["培训", "网课", "教育", "学费", "课程",
                  "驾校", "辅导", "补习", "考试", "学校", "幼儿园", "托管", "早教", "夏令营", "研学", "教材"]),
    ("数码电器", ["数码", "电器", "手机", "电脑", "家电", "耳机", "充电宝",
                  "华为", "小米之家", "OPPO", "oppo", "vivo", "VIVO", "联想", "戴尔", "苏宁", "国美",
                  "平板", "相机", "打印机", "路由器", "显示器", "键盘", "鼠标", "音箱", "音响", "数据线", "充电器",
                  "电视机", "冰箱", "洗衣机", "空调", "热水器", "微波炉", "电饭煲", "扫地机"]),
    # 收入侧口语组（2026-11-10 日常用语扩充）：放在词表末尾——「房贷利息」先被住房组的
    # 「房贷」命中归住房，剩下的「存款利息/理财/分红」才落到这里
    ("兼职副业", ["兼职", "外快"]),
    ("理财收益", ["理财", "利息", "分红", "余额宝"]),
    ("报销", ["报销"]),
]

_PLATE_RE = re.compile(
    "[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领]"
    "[A-HJ-NP-Z][A-HJ-NP-Z0-9]{4,6}")

def _renqing_split(text: str, amount: int | None) -> str:
    """人情类关键词命中后的两级归类：
    小额通用红包 → 「红包礼金」；其余（大额或带明确人情含义）→ 「人情往来」。"""
    return "红包礼金" if dbm.is_small_generic_packet(text, amount) else "人情往来"

# 拼手气/群红包等确定字样的判定收口在 dbm.renqing_need_confirm（与词表常量同居 db.py）

# 加油词表单列：优先于车牌正则（带车牌的加油记录归加油充电而非爱车养车）
_FUEL_WORDS = dict(TOP_KEYWORD_LEXICON)["加油充电"]

# 产品级例外（优先级最高）：子串撞车的产品名直接定向，防词典误伤。
# 汽车美容/洗车：美容美发排在爱车养车之前，「XX汽车美容」「洗车美容」会被「美容」抢走
# 鱼跃：医疗器械品牌，日用组收了裸词「鱼」（口语买鱼）后须定向回医疗
# 豆浆机/料理机：餐饮组收了「豆浆」「料理」（口语吃食）后，小家电定向回数码
# 钓鱼：文化组排在日用组之后，「鱼」会先抢——垂钓是休闲，例外表定向（钓鱼竿/渔具同享）
_KW_EXCEPTIONS = [("航空杯", "日用百货"), ("汽车美容", "爱车养车"), ("洗车", "爱车养车"),
                  ("鱼跃", "医疗健康"), ("豆浆机", "数码电器"), ("料理机", "数码电器"),
                  ("钓鱼", "文化休闲")]

def _top_by_keyword(text: str, amount: int | None = None) -> str | None:
    # 优先级：产品例外 > 加油充电 > 车牌正则 > 词典顺序
    for word, cat in _KW_EXCEPTIONS:
        if word in text:
            return cat
    if any(w in text for w in _FUEL_WORDS):
        return "加油充电"
    if _PLATE_RE.search(text):
        return "爱车养车"
    for cat, words in TOP_KEYWORD_LEXICON:
        if any(w in text for w in words):
            # 人情类词表统一按金额/字样分到「红包礼金」或「人情往来」
            return _renqing_split(text, amount) if cat == "人情往来" else cat
    return None

def _resolve_by_mapping(conn: sqlite3.Connection, ledger_id: int, flow_type: str, source: str):
    """映射解析（v6 外键版）：source → 分类行 {id, name, type}，未命中返回 None。
    词典优先后无需区分 learned（学习映射不再需要内联防误判），外键保证目标必存在。
    带出 type：目标是「退款/不计收支」组时整行转不计收支。
    同源可同时命中本收支侧映射与另一侧指向转账类的映射（OR 条件）：本侧精确优先，
    再按 id 定序——此前裸 fetchone 取存储序，哪条生效取决于建映射先后、不可复现。"""
    if not source:
        return None
    r = conn.execute(
        "SELECT m.category_id, c.name AS name, c.type AS type FROM category_mappings m "
        "JOIN categories c ON m.category_id=c.id "
        "WHERE m.ledger_id=? AND (m.flow_type=? OR c.type='transfer') AND m.source=? "
        "ORDER BY m.flow_type<>?, m.id",
        (ledger_id, flow_type, source, flow_type)).fetchone()
    return {"id": r["category_id"], "name": r["name"], "type": r["type"]} if r else None

def mapping_reset_candidates(conn: sqlite3.Connection, ledger_id: int) -> list[dict]:
    """将被 _purge_bad_learned_mappings 删除的「与默认冲突的学习映射」只读预览：
    同源在 DEFAULT_MAPPINGS 有默认、但学习目标与默认不同 → 应用时删学习行、补种默认。
    1.0.2 起手动改分类不再回写 learned，候选全部是历史遗留（旧版自动回写/手动改泛化）。
    ②（学习行=默认 → learned=0）与 ③（补种缺失默认）是无感归一化，不列入。
    ledger_id 参数：只扫当前账本——此前全局遍历 SELECT id FROM ledgers，repair 端点是
    账本级 ACL，会把成员无权访问的其他账本映射重置/泄露（跨账本越权）。"""
    out = []
    for flow, source, target in dbm.DEFAULT_MAPPINGS:
        cid = dbm.top_cat_id(conn, ledger_id, target, flow)
        if not cid:
            continue
        row = conn.execute(
            "SELECT m.category_id, c.name AS cat_name FROM category_mappings m "
            "JOIN categories c ON m.category_id=c.id "
            "WHERE m.ledger_id=? AND m.flow_type=? AND m.source=? AND m.learned=1",
            (ledger_id, flow, source)).fetchone()
        if row and row["category_id"] != cid:
            out.append({"ledger_id": ledger_id, "flow_type": flow, "source": source,
                        "learned": row["cat_name"], "default": target})
    return out

def _purge_bad_learned_mappings(conn: sqlite3.Connection, ledger_id: int) -> int:
    """修复历史 AI 误判写入的脏学习映射（v6 单行模型；仅 /api/repair/apply 确认后触发）：
    ① 删与默认冲突的学习行——冲突口径同 mapping_reset_candidates（单一来源），
       按 UNIQUE(ledger_id, flow_type, source) 三元组定位删除；
    ② 学习行=默认 → 还原 learned=0（它就是默认）；③ 补种缺失默认（①删过的在此补回，
       复用建账同款 seed_default_mappings）。默认目标分类被用户删掉的源不参与②③。
    只处理当前账本（与 mapping_reset_candidates 同口径，见其越权注记）。"""
    removed = 0
    with conn:
        for m in mapping_reset_candidates(conn, ledger_id):
            conn.execute("DELETE FROM category_mappings "
                         "WHERE ledger_id=? AND flow_type=? AND source=?",
                         (m["ledger_id"], m["flow_type"], m["source"]))
            removed += 1
        dbm.seed_default_mappings(conn, ledger_id)        # ③ 补种缺失默认
        for flow, source, target in dbm.DEFAULT_MAPPINGS:
            if dbm.top_cat_id(conn, ledger_id, target, flow):
                conn.execute(                              # ② 默认目标在 → 学习行还原 learned=0
                    "UPDATE category_mappings SET learned=0 "
                    "WHERE ledger_id=? AND flow_type=? AND source=? AND learned=1",
                    (ledger_id, flow, source))
    if removed:
        _log.info(f"数据修复：重置 {removed} 条与内置默认冲突的学习映射（用户确认触发）")
    return removed

# 修复不触碰人情二分类（人情往来/红包礼金）——词典的人情切分是「金额+字样」的
# 启发式（灰色地带，非确定性证据）：真机两轮反馈都是它把用户手动归类翻掉
# （如 爸爸给人情/便利店小票 被建议改人情往来）。两侧都不参与：要改请手动改，
# 手动改完即带 manual_cat 保护。
_REPAIR_RENQING = ("人情往来", "红包礼金")

def fix_candidates(conn: sqlite3.Connection, ledger_id: int) -> list[dict]:
    """auto_fix_transactions 的只读预览（不写库）：行文本（名称+商户+备注）命中
    关键词词典、且当前一级分类与词典矛盾（如停车费被误分为餐饮美食）→ 列为候选。
    词典是类型盲的（如收入行名称也能命中「数码电器」），目标分类按**行类型**解析
    id——该类型下不存在的分类不可修也不列出（否则应用时永远跳过、预览里赖着不走）。
    manual_cat=1（用户手选过分类）的行永不列出——手动整理是用户意图，不 second-guess；
    当前分类是**用户自建**（不在默认分类名单）的行同理：自动路径只会落默认分类，
    进自建分类必是批量改/单笔改/用户配的映射目标，全是刻意安置（真机 1.0.4 事故：
    早期批量改漏打 manual_cat，自建「房贷」几十笔被建议翻内置「住房物业」）；
    人情二分类两侧均不参与（见 _REPAIR_RENQING）。"""
    _default_names = (set(dbm.EXPENSE_CATEGORIES) | set(dbm.INCOME_CATEGORIES)
                      | set(dbm.TRANSFER_CATEGORIES))
    cat_ids = {(c["name"], c["type"]): c["id"] for c in conn.execute(
        "SELECT id, name, type FROM categories "
        "WHERE ledger_id=? AND parent_id IS NULL", (ledger_id,))}
    rows = conn.execute(
        "SELECT t.id, t.type, t.amount, t.date, t.name, t.merchant, t.note, "
        "COALESCE(p.name, c.name) AS top_name "
        "FROM transactions t JOIN categories c ON t.category_id=c.id "
        "LEFT JOIN categories p ON c.parent_id=p.id "
        "WHERE t.ledger_id=? AND t.type IN ('expense','income') AND t.refund_for IS NULL "
        "AND COALESCE(t.manual_cat,0)=0",
        (ledger_id,)).fetchall()
    out = []
    for r in rows:
        if r["top_name"] in _REPAIR_RENQING or r["top_name"] not in _default_names:
            continue
        kw = _top_by_keyword(f"{r['name'] or ''} {r['merchant'] or ''} {r['note'] or ''}", r["amount"])
        cid = cat_ids.get((kw, r["type"])) if kw not in _REPAIR_RENQING else None
        if cid and kw != r["top_name"]:
            out.append({"id": r["id"], "type": r["type"], "amount": r["amount"],
                        "date": r["date"], "name": (r["name"] or "")[:40],
                        "current": r["top_name"], "target": kw, "target_id": cid})
    return out

def auto_fix_transactions(conn: sqlite3.Connection, ledger_id: int) -> int:
    """修复历史误分类流水（仅 /api/repair/apply 用户确认后触发）：
    候选集见 fix_candidates（目标 id 已按类型解析），按目标分组纠正。
    仅按确定性关键词修，不碰转账/退款行/手动分类行。"""
    by_target: dict = {}
    for f in fix_candidates(conn, ledger_id):
        by_target.setdefault(f["target_id"], []).append(f["id"])
    n = 0
    with conn:
        for cid, ids in by_target.items():
            for part in _chunks(ids):
                q = ",".join("?" * len(part))
                cur = conn.execute(
                    f"UPDATE transactions SET category_id=? WHERE id IN ({q})", (cid, *part))
                n += cur.rowcount
    if n:
        _log.info(f"数据修复：{n} 笔历史流水按关键词词典纠正分类（用户确认触发）")
    return n

def account_merge_groups(conn: sqlite3.Connection, ledger_id: int) -> list[dict]:
    """同账本内归一后同名的账户组（自动导入的历史变体，只读预览）：
    [{key: 归一名, accounts: [{id, name, tx_count}]}]，键排序保证预览顺序稳定。"""
    groups: dict = {}
    for r in conn.execute(
            "SELECT a.id, a.name, (SELECT COUNT(*) FROM transactions t "
            "WHERE t.account_id=a.id) AS n FROM accounts a "
            "WHERE a.ledger_id=? ORDER BY a.id", (ledger_id,)):
        groups.setdefault(norm_account_name(r["name"]), []).append(
            {"id": r["id"], "name": r["name"], "tx_count": r["n"]})
    return [{"key": k, "accounts": v} for k, v in sorted(groups.items()) if len(v) > 1]

def merge_duplicate_accounts(conn: sqlite3.Connection, ledger_id: int) -> int:
    """合并归一同名的重复账户（/api/repair/apply 用户确认后触发）：
    幸存者优先取名字已是归一形的成员，否则取流水最多的（并列取小 id）；
    幸存者改名为归一形，其余成员的 account_id/to_account_id 改指幸存者后删除。
    返回合并掉（删除）的账户数。"""
    merged = 0
    with conn:
        for g in account_merge_groups(conn, ledger_id):
            key, members = g["key"], g["accounts"]
            survivor = next((m for m in members if m["name"] == key), None) \
                or max(members, key=lambda m: (m["tx_count"], -m["id"]))
            sid = survivor["id"]
            others = [m["id"] for m in members if m["id"] != sid]
            if not others:
                continue
            if survivor["name"] != key:
                conn.execute("UPDATE accounts SET name=? WHERE id=?", (key, sid))
            for oid in others:
                conn.execute("UPDATE transactions SET account_id=? WHERE account_id=?", (sid, oid))
                conn.execute("UPDATE transactions SET to_account_id=? WHERE to_account_id=?", (sid, oid))
                conn.execute("DELETE FROM accounts WHERE id=?", (oid,))
            merged += len(others)
            names = "、".join(f"「{m['name']}」" for m in members)
            _log.info(f"账户合并（用户确认触发）：{names} → 「{key}」")
    return merged

def repair_preview(conn: sqlite3.Connection, ledger_id: int) -> dict:
    """数据修复只读预览：词典分类冲突流水 + 将被重置的学习映射 + 可合并的重复账户。
    映射重置候选只取当前账本（端点是账本级 ACL，全局口径会泄露他人账本信息）。"""
    return {"tx_fixes": fix_candidates(conn, ledger_id),
            "mapping_resets": mapping_reset_candidates(conn, ledger_id),
            "account_merges": account_merge_groups(conn, ledger_id)}

def repair_apply(conn: sqlite3.Connection, ledger_id: int) -> dict:
    """数据修复应用（/api/repair/apply，用户确认后触发）：合并重复账户 → 清脏映射 →
    纠正流水分类。映射清理只处理当前账本（历史全局口径是跨账本越权写，已收口）。"""
    n_acc = merge_duplicate_accounts(conn, ledger_id)
    n_map = _purge_bad_learned_mappings(conn, ledger_id)
    n_tx = auto_fix_transactions(conn, ledger_id)
    return {"tx_fixed": n_tx, "mappings_reset": n_map, "accounts_merged": n_acc}


# ---------- 分类瀑布（IMPORT_REFACTOR_PLAN §三；自 build_previews 显式化，行为逐字段等价） ----------

# transfer 子类的已知名字（TRANSFER_SUBCLASS 源键 + 子类名）：柴米账单直入时这些名字走
# 「不计收支 + transfer_sub」规范表示，不当自建分类新建（classify_transfer）
_TRANSFER_SUB_NAMES = frozenset(dbm.TRANSFER_SUBCLASS) | frozenset(dbm.TRANSFER_SUBCLASS.values())

class ClassifyCtx:
    """分类瀑布的账本上下文（每次导入构建一次）：一级分类索引 + transfer 专用分类。"""

    def __init__(self, conn: sqlite3.Connection, ledger_id: int):
        self.conn, self.ledger_id = conn, ledger_id
        cats = list(conn.execute(
            "SELECT c.id, c.name, c.type, c.parent_id, p.name AS pname FROM categories c "
            "LEFT JOIN categories p ON c.parent_id=p.id WHERE c.ledger_id=?", (ledger_id,)))
        self.cat_top = {(c["name"], c["type"]): c for c in cats if not c["pname"]}
        # transfer 专用分类直接取自已加载的一级索引，不再各查一次库
        self.tcat = {n: (self.cat_top.get((n, "transfer")) or {"id": None})["id"]
                     for n in ("退款", "不计收支")}

    def top(self, name: str, ftype: str):
        """按 (名字, 类型) 精确查一级分类——同名收支分类（人情往来）互不覆盖。
        刻意只做精确匹配（区别于 db.match_top_cat 的两段式包含匹配）：导入的「同名直连」
        须严格同名才落，包含匹配会把相近的账单来源分类名误吞。dict 预载索引也是刻意——
        导入逐行走这里，SQL 精确版（db.top_cat_id）每行一查会是 N 次库往返。"""
        return self.cat_top.get((name, ftype))

    def fallback(self, ftype: str):
        """兜底一级分类：支出→「其他」，收入→「其他收入」。"""
        return self.top(dbm.FALLBACK_TOP[ftype], ftype)

    def ensure(self, name: str, ftype: str):
        """取或建一级分类（柴米账单直入用）：账单里的分类在本账本不存在时新建同名分类——
        与导入建银行卡账户 import_bill._ensure_account 同口径：解析期即时生效、独立提交，
        撤销导入不删。新建行同步进索引，同批多行同名只建一次。"""
        hit = self.top(name, ftype)
        if hit:
            return hit
        with self.conn:
            sort = self.conn.execute(
                "SELECT COALESCE(MAX(sort),0)+1 FROM categories "
                "WHERE ledger_id=? AND type=? AND parent_id IS NULL",
                (self.ledger_id, ftype)).fetchone()[0]
            cid = self.conn.execute(
                "INSERT INTO categories(ledger_id, type, name, parent_id, sort) VALUES(?,?,?,?,?)",
                (self.ledger_id, ftype, name, None, sort)).lastrowid
        _log.info(f"柴米账单导入：新建分类「{name}」({ftype})")
        row = self.cat_top[(name, ftype)] = {"id": cid, "name": name, "type": ftype}
        return row


def classify_transfer(f: dict, ctx: ClassifyCtx, p: dict, export_tgt_amount=None) -> None:
    """转账行：子类映射（TRANSFER_SUBCLASS；未知子类 → 待确认）+ 导出回环退款对直连
    （负序号走 apply_import 回填，高置信免确认）。就 p 原地更新。"""
    sub = dbm.TRANSFER_SUBCLASS.get(f.get("source_category", ""), dbm.DEFAULT_TRANSFER_SUB)
    if f.get("is_refund") or f.get("export_refund_idx") is not None:
        sub = "退款"
    p["tags"]["transfer_sub"] = sub
    p["category_name"] = sub
    p["classified_via"] = "transfer"
    p["category_id"] = ctx.tcat["退款" if sub == "退款" else "不计收支"]
    raw_cat = (f.get("source_category") or "").strip()
    if (f.get("platform") == "export" and raw_cat and sub != "退款"
            and raw_cat not in dbm.TRANSFER_CATEGORIES):
        # 柴米账单直入：账本里有同名的不计收支组分类就直接挂，自建的（如「借给朋友」）缺失则新建；
        # 已知子类名（账户转账/信用卡还款…，旧版二级分类的导出形态）无同名分类时仍走
        # 「不计收支 + transfer_sub」规范表示，不为子类名另建分类
        top = ctx.top(raw_cat, "transfer") or (
            None if raw_cat in _TRANSFER_SUB_NAMES else ctx.ensure(raw_cat, "transfer"))
        if top:
            p["category_id"], p["category_name"] = top["id"], raw_cat
    if (not f.get("is_refund") and f.get("export_refund_idx") is None
            and f.get("source_category") not in dbm.TRANSFER_SUBCLASS
            and f.get("platform") != "export"):   # 导出回环的转账源端已确认，不重标
        p["need_confirm"] = True
    if f.get("export_refund_idx") is not None:
        p["refund_for"] = f["export_refund_idx"]
        p["refund_confidence"] = "high"
        p["refund_status"] = dbm.refund_status(export_tgt_amount, f["amount"])
        p["need_confirm"] = False


def classify_export(f: dict, ctx: ClassifyCtx, p: dict) -> None:
    """柴米账单（本应用导出）收支行：文件里的分类即用户定过的最终结果——按原名直取，
    不走词典/映射/人情复核，不进待确认，也不交 AI 兜底（classified_via 非 raw，
    ai.resolve_subcategories_batch 天然跳过；server 另对整份柴米账单关 AI）。
    本账本没有该分类则新建同名分类（ClassifyCtx.ensure）；原分类为空（源头即未分类）
    保持未分类。就 p 原地更新。"""
    raw_cat = (f.get("source_category") or "").strip()
    top = ctx.ensure(raw_cat, f["type"]) if raw_cat else None
    p["category_id"] = top["id"] if top else None
    p["parent_category"] = p["category_name"] = raw_cat
    p["classified_via"] = "export"
    p["need_confirm"] = False
    # 文件分类=用户定过的最终结果（可能承载导出前的手动调整，manual_cat 不随文件携带）：
    # 打 user_changed（落库转 manual_cat）——否则「数据修复」可按词典把它翻掉，违背
    # 「按原分类原样入库、不走词典」的本路径声明（跨账本合并/编辑后重导场景可达）
    p["user_changed"] = True
    if raw_cat:
        p["map_source"] = raw_cat


def classify_flow(f: dict, ctx: ClassifyCtx, p: dict) -> None:
    """支出/收入行五级链：①词典（人情二切）→ ②映射（含映射到退款/不计收支组 → 整行转
    transfer）→ ②' 同名直连 → ③AI 兜底（上层 ai 模块在预览后补）→ ④硬兜底+待确认；
    尾部人情复核。柴米账单不走瀑布，按原分类直入（classify_export）。就 p 原地更新。"""
    if f.get("platform") == "export":
        classify_export(f, ctx, p)
        return
    # 来源就叫「人情往来」（自定义模板复制的导出格式等）时尊重原分类，跳过词典的人情重切
    text = f"{f.get('name','')} {f.get('merchant','')} {f.get('note','')}"
    raw_cat = (f.get("source_category") or "").strip()
    src_is_renqing = raw_cat == "人情往来" and bool(ctx.top(raw_cat, f["type"]))
    kw = None if src_is_renqing else _top_by_keyword(text, f["amount"])
    kw_top = ctx.top(kw, f["type"]) if kw else None
    if kw_top:
        top = kw_top
        p["classified_via"] = "keyword"
        if top["name"] == "红包礼金":
            p["need_confirm"] = dbm.renqing_need_confirm(text)
    else:
        # ② 映射表（v6 外键直取分类行，结构上无悬空）
        target = _resolve_by_mapping(ctx.conn, ctx.ledger_id, f["type"], raw_cat)
        direct = ctx.top(raw_cat, f["type"]) if raw_cat else None
        if target and target["type"] == "transfer":
            # 用户显式指定 → 高置信不进待确认；top 设好后走公共尾行（人情复核天然跳过）
            sub = "退款" if target["name"] == "退款" else dbm.TRANSFER_SUBCLASS.get(raw_cat, dbm.DEFAULT_TRANSFER_SUB)
            p["type"] = "transfer"
            p["tags"]["transfer_sub"] = sub
            p["need_confirm"] = False
            p["classified_via"] = "mapping"
            # 预览名取分类本体（落库 category_id 指向它）：此前用子类名「账户转账」展示，
            # 映射到自建转账分类（借给朋友等）时预览与入库分类对不上；子类只进 tags
            top = {"id": target["id"], "name": target["name"]}
        elif target:
            top = target
            p["classified_via"] = "mapping"
        # ②' 同名直连：来源分类名与账本现有分类同名 → 直接归类
        elif direct:
            top = direct
            p["classified_via"] = "direct-name"
        else:
            top = ctx.fallback(f["type"])
            p["classified_via"] = "raw"
            p["need_confirm"] = True
    p["category_id"] = top["id"] if top else None
    p["parent_category"] = top["name"] if top else (
        raw_cat or dbm.FALLBACK_TOP[f["type"]])
    # 记录来源分类名：AI 补名称的上下文（「交易类型」字段）；1.0.2 起不再作学习键
    if raw_cat:
        p["map_source"] = raw_cat
    p["category_name"] = p["parent_category"]
    _renqing_review(f, ctx, p, text, raw_cat)


def _renqing_review(f: dict, ctx: ClassifyCtx, p: dict, text: str, raw_cat: str) -> None:
    """人情类复核：词典分支已按金额切分过，这里只复核映射/同名直连的结果；
    切分结果是「人情往来」直接入库，是「红包礼金」按统一口径决定是否待确认。"""
    if (p["classified_via"] != "keyword" and raw_cat != "人情往来"
            and p["parent_category"] in ("人情往来", "红包礼金")):
        alt = ctx.top(_renqing_split(f"{raw_cat} {text}", f["amount"]), f["type"])
        if alt:
            p["category_id"] = alt["id"]
            p["parent_category"] = p["category_name"] = alt["name"]
        p["need_confirm"] = (p["parent_category"] == "红包礼金"
                             and dbm.renqing_need_confirm(text))
