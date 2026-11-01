"""内置平台模板（解析声明数据，随应用发版；import_bill.parse_template 解释执行）。

字段规范（TEMPLATE_MIGRATION_PLAN.md §三；v1 字段供向导/用户模板沿用）：
  headers          表头列名集合（嗅探匹配用；只列跨导出变体的不变列，可选列不进）
  col              标准字段→列名，值可为别名数组（取首个非空值）；除标准键
                   （date/type/name/flow/pay/amount/order/merchant/note/to_account_name）
                   外的键原样拷入 RawFlow（如导出的 origin_source/export_id）
  flow_map         收/支列枚举→expense/income；未命中与缺失一律 transfer
  refund_type      v1：类型列含此值 → is_refund（引擎编译为 contains 规则）
  refund_rules     退款规则数组 {col, op(in/contains/suffix), values, contains_none?, guard?}，规则间 OR
  row_filter       {col, skip_in}：某列值命中黑名单 → 整行丢弃
  transfer_types   类型列命中集合 → transfer（不计收支），优先于 flow_map
  name_fallback    {cols, generic_set}：名称列无意义时依次回退，全无意义留空给 AI
  derive_base_as   将剥退款后缀的基础类型另存为该 RawFlow 键（微信 wechat_type，供退款匹配）
  hash_fields      去重字段集：列名（原始单元格）或派生键 $date/$name/$amount/$order/$merchant；
                   等于 amount 列名的条目规范化为整数分字符串；缺省 = 通用模板五元组
  expand           京东金额括号：仅取原消费，括号内退款额存 bracket_refund（②'' 层线索）
  category_split   分类列按分隔符拆多分类（首段为 source_category，余段 extra_categories）
  two_pass         导出回环：按 col.export_id/export_ref 批内直连退款对（负序号协议）
  pay_split        支付账户按分隔符拆出收款账户（导出 " & "）
  attribution_col  归属直取列（导出 流水归属）
  skip_zero_reason 0 元行标 skip 并以此为由（支付宝）
  note_strip       备注占位清理（美团 "/"）
  amount_mode      lenient=to_cents 宽松（内置平台）；strict=先严格校验、失败标 skip、取绝对值
                   （缺省 strict，向导/用户模板沿用旧 parse_generic 口径）
  note_fallback_last  备注列缺失借末列（hash 同步）
  owner / owner_re / skip_first_n / name_strip   v1 字段，语义不变
"""
from __future__ import annotations

# 「本应用导出」列名单源：server._export_table 表头与 export 模板的 headers/col/hash_fields/
# attribution_col 全部引用同一份（导出文件须被自家模板命中，tests 有契约断言），改列名只动这里
EXPORT_COLUMNS = {
    "date": "交易时间", "name": "交易名称", "amount": "交易金额", "flow": "流水类型",
    "type": "支出类型/收入类型", "pay": "支付账户", "pay_legacy": "支付方式/收款方式",
    "to_account": "收款账户", "attribution": "流水归属", "note": "备注", "order": "订单号",
    "origin_source": "来源", "export_id": "流水ID", "export_ref": "退款关联",
}

# 微信「商品」列的泛化值：不参与名称匹配（红包/转账行普遍如此），回退到「交易对方」
WX_GENERIC_NAMES = frozenset({
    "微信红包", "微信红包（单发）", "微信红包（群红包）", "群红包", "红包",
    "微信转账", "转账", "微信红包-退款", "转账-退款",
})

# 微信 transfer 集合：账户间搬运不计收支（信用卡还款/理财申赎/充值等）。
# 字面列表，须与 db.TRANSFER_SUBCLASS 派生集保持一致（单测断言同步，防 db.py 演进失同步；
# 个人间转账/红包族与退款按方向计收支或单独处理，故排除）。
WX_TRANSFER_TYPES = [
    "小金库", "投资理财", "转入零钱通", "余额宝", "余额宝-自动转入",
    "转入零钱通-来自零钱", "赎回", "信用卡还款", "信用借还", "余额互通", "充值",
]

PLATFORM_TEMPLATES = [
    {   # 京东（2026-10 新导出）：退款独立成行（「退款-」前缀 + 退款成功状态 + 不计收支），
        # 括号仅标在原消费上——expand=True：括号行只取原消费金额（强制 expense 供退款行
        # 关联），括号内退款额存 bracket_refund（②'' 层的关联线索），不拆退款行。
        "id": "jd", "name": "京东",
        "headers": ["交易时间", "商户名称", "交易说明", "金额", "收/支",
                    "交易状态", "交易分类", "交易订单号", "收/付款方式", "商家订单号"],
        "col": {"date": "交易时间", "merchant": "商户名称", "name": "交易说明",
                "amount": "金额", "flow": "收/支", "type": "交易分类",
                "order": "交易订单号", "pay": "收/付款方式", "note": "备注"},
        "flow_map": {"支出": "expense"},          # 仅支出计收支，其余（含收入侧退款）transfer
        "refund_rules": [
            {"col": "交易状态", "op": "in", "values": [
                "退款成功", "已退款", "退款完成", "全额退款", "部分退款",
                "退款已到账", "退款到账", "成功退款", "退已成功"]},
            {"col": "交易状态", "op": "contains", "values": ["退款"],
             "contains_none": ["处理中", "退款中"]},
        ],
        "hash_fields": ["交易时间", "交易分类", "商户名称", "交易说明",
                        "金额", "交易订单号", "备注"],
        "note_fallback_last": True,
        "expand": True,
        "category_split": " ",
        "amount_mode": "lenient",
        "skip_zero_reason": "0 元行（先享后付等）",
        "owner_re": r"京东账号名\s*[:：]\s*([^\s，,]+)",
    },
    {
        "id": "alipay", "name": "支付宝",
        "headers": ["交易时间", "交易分类", "交易对方", "商品说明", "收/支",
                    "金额", "收/付款方式", "交易状态", "交易订单号", "商家订单号"],
        "col": {"date": "交易时间", "type": "交易分类", "merchant": "交易对方",
                "name": "商品说明", "flow": "收/支", "amount": "金额",
                "pay": "收/付款方式", "order": "交易订单号", "note": "备注"},
        "flow_map": {"支出": "expense", "收入": "income"},
        # 不过滤「交易关闭」：多为已全额退款的原消费（退款行独立存在），保留才能闭环
        "refund_rules": [{"col": "交易分类", "op": "in", "values": ["退款"]}],
        "hash_fields": ["交易时间", "交易分类", "交易对方", "商品说明",
                        "金额", "商家订单号", "备注"],
        "note_fallback_last": True,
        "skip_zero_reason": "0 元退款行",
        "amount_mode": "lenient",
        "owner_re": r"姓名\s*[:：]\s*([^\s，,]+)",
    },
    {
        "id": "export", "name": "本应用导出",
        # 两代导出格式的列交集（旧代用「支付方式/收款方式」合并列，由 col 别名兼容）；
        # 列名引用上方 EXPORT_COLUMNS 单源
        "headers": [EXPORT_COLUMNS["date"], EXPORT_COLUMNS["name"],
                    EXPORT_COLUMNS["amount"], EXPORT_COLUMNS["flow"]],
        "col": {"date": EXPORT_COLUMNS["date"], "name": EXPORT_COLUMNS["name"],
                "amount": EXPORT_COLUMNS["amount"], "flow": EXPORT_COLUMNS["flow"],
                "type": EXPORT_COLUMNS["type"],
                "pay": [EXPORT_COLUMNS["pay"], EXPORT_COLUMNS["pay_legacy"]],
                "to_account_name": EXPORT_COLUMNS["to_account"], "order": EXPORT_COLUMNS["order"],
                "note": EXPORT_COLUMNS["note"],
                "origin_source": EXPORT_COLUMNS["origin_source"],
                "export_id": EXPORT_COLUMNS["export_id"], "export_ref": EXPORT_COLUMNS["export_ref"]},
        "flow_map": {"收入": "income", "支出": "expense"},
        "hash_fields": [EXPORT_COLUMNS["date"], EXPORT_COLUMNS["name"], EXPORT_COLUMNS["amount"],
                        EXPORT_COLUMNS["type"], EXPORT_COLUMNS["note"]],
        "two_pass": True,
        "pay_split": " & ",
        "attribution_col": EXPORT_COLUMNS["attribution"],
        "amount_mode": "lenient",
    },
    {
        "id": "wechat", "name": "微信",
        "headers": ["交易时间", "交易类型", "交易对方", "商品", "收/支",
                    "金额(元)", "支付方式", "当前状态", "交易单号", "商户单号"],
        "col": {"date": "交易时间", "type": "交易类型", "merchant": "交易对方",
                "name": "商品", "flow": "收/支", "amount": "金额(元)",
                "pay": "支付方式", "order": "交易单号", "note": "备注"},
        "flow_map": {"支出": "expense", "收入": "income"},
        # 方向守卫：退款一定表现为钱回到我账户（收入）——「已全额退款」的支出行是
        # 被退的原消费，不能误判为退款（误判会让它既不计收支又无法被退款行匹配）
        "refund_rules": [
            {"col": "交易类型", "op": "suffix", "values": ["-退款"]},
            {"col": "当前状态", "op": "in", "values": ["已全额退款", "对方已退还"],
             "guard": {"flow_not": "支出"}},
        ],
        "row_filter": {"col": "当前状态", "skip_in": ["交易关闭", "已删除"]},
        "transfer_types": WX_TRANSFER_TYPES,
        "name_fallback": {"cols": ["商品", "交易对方"], "generic_set": WX_GENERIC_NAMES},
        "derive_base_as": "wechat_type",
        # 备注列不进 hash：有无备注列的两次导出仍判同一行
        "hash_fields": ["交易时间", "交易类型", "交易对方", "商品", "金额(元)", "交易单号"],
        "amount_mode": "lenient",
        "owner_re": r"微信昵称\s*[:：]\s*([^\s，,]+)",
    },
    {
        "id": "meituan", "name": "美团",
        "headers": ["交易创建时间", "交易成功时间", "交易类型", "订单标题", "收/支",
                    "支付方式", "订单金额", "实付金额", "交易单号", "商家单号", "备注"],
        "col": {"date": "交易成功时间", "type": "交易类型", "name": "订单标题",
                "flow": "收/支", "pay": "支付方式", "amount": "实付金额",
                "order": "交易单号", "merchant": "", "note": "备注"},
        "flow_map": {"支出": "expense", "收入": "income"},
        "refund_type": "退款",
        "name_strip": ["订单详情"],
        "note_strip": ["/"],
        "owner_re": r"美团用户名：\[(.+?)\]",
    },
]
