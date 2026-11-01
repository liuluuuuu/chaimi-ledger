# chaimi-ledger 设计文档

> 飞牛 NAS（fnOS）原生家庭记账应用。多账本分账 + 账单导入 + 统计看板 + AI 记账 + 微信机器人，数据全在本机 SQLite。
>
> 状态：**v1.0.5（2026-11-01）**。本文描述当前实现。

***

## 一、项目概述

- **形态**：飞牛原生应用（fpk 包），网关以 iframe 挂载 `/app/chaimi-ledger`
- **存储**：SQLite 单文件（`chaimi.db`），零配置，备份 = 拷文件
- **后端**：FastAPI（Python 3.12，`--target` 隔离装依赖，不污染系统）
- **前端**：单 HTML + 原生 JS + Chart.js 4（按需加载，无构建链）
- **用户模型**：飞牛多用户（v1.0.4）——登录身份取自飞牛网关注入的请求头，各自账本互不可见、可共享（单账本多人记账互见）；「用户」（归属分组）是账本内的记账人维度而非登录账号。应用级配置 admin 收口；账本级数据按 ACL（owner/共享成员），分类/映射/账户/用户/预算成员均可写，账本名/币种/删除/共享管理仅 owner
- **AI**：OpenAI 兼容协议，设置页填 Key 即用（预设百炼/智谱/DeepSeek 均含文本+视觉，亦支持本地模型；视觉模型缺位时图片记账自动禁用）
- **微信机器人**：同一份记账大脑，直连微信（iLink 长轮询，A 通道；B 通道网关/MCP 已于 v1.0.4 下线）

### 模块清单（app/src/）

| 文件 | 职责 |
|---|---|
| server.py | FastAPI 入口：记账 CRUD / 统计 / 导入 / 配置 / 备份 / AI / bot 路由 |
| config.py | JSON 配置（chaimi.conf）+ TRIM_* 环境变量 + llm（含视觉 vl_*）/backup/bot/web_auth 段 |
| db.py | SQLite schema、迁移、公共查询、默认映射种子（DEFAULT_MAPPINGS） |
| import_bill.py | 导入门面：统一路由 + 模板引擎与原语（对外入口 `parse_bill/build_previews`） |
| platform_templates.py | 内置平台模板声明数据（支付宝/微信/京东/导出/美团，随应用发版） |
| import_classify.py | 分类瀑布：词典/映射/同名/兜底 + 人情二切与复核 + 自愈（1.0.1 起确认制：/api/repair 预览+应用，不随解析自动改库） |
| import_match.py | 账本快照、退款匹配链、跨源/手动配对 |
| import_apply.py | 入库四阶段（增强/批次/插入+learn+批内回填/rescan）与整批撤销 |
| import_common.py | 导入链路叶子工具（normalize/去重谓词/占位名判定） |
| ai.py | AI 记账：自然语言/小票/月报/对话 + edit_round 编辑回合 |
| backup.py | SQLite 文件备份 + 配置副本 |
| bot_core.py | 机器人大脑：白名单、指令路由、草稿会话、回复格式化（通道无关） |
| bot_ilink.py | 通道 A：iLink 客户端（QR 登录、长轮询、收发、媒体下载解密） |

前端（app/static/）：app.html 主界面 + home.html 登录/选用户页 + core/ledger/stats/settings/ai/import .js 分页逻辑。

***

## 二、整体架构

```
飞牛桌面 ──iframe──▶ chaimi-server(FastAPI)
                        │
   ┌─────────┬──────────┼──────────┬─────────────┐
   ▼         ▼          ▼          ▼             ▼
 记账CRUD   统计图表   账单导入     备份        AI 记账 ──▶ LLM / 视觉 API
 (db.py)  (Chart.js)(import_*)(backup.py)      │
   └─────────┴──────────┴──────────┴──────┬───────┘
                                          ▼
                                    SQLite(chaimi.db)
微信 ──iLink──▶ bot_ilink(长轮询线程) ──▶ bot_core.handle() ──▶ db / ai.chat
```

**网关三件套**（SSE 流式与相对路径必需）：

- 响应头 `X-Accel-Buffering: no`
- `<base href="/app/chaimi-ledger/">` 注入
- SSE 流内 15s 心跳注释行保活

**多端实时刷新（长轮询）**：`GET /api/rev/wait?since=N&hold=25` 挂住最多 25s（服务端钳制 1~30s），数据版本 rev 偏离立即返回；前端返回后 300ms 续下一条、出错 2s 退避、单请求 35s 强制 abort 防半开连接；页面回前台（focus/visibilitychange/pageshow）掐掉挂起请求并主动刷一次。任意端记账或改账本级设置，其他端 1s 内刷新。写库侧统一 `_bump_rev`：交易增/改/删、待确认转正、导入应用/撤销、bot 写库、账本级设置（分类/映射/账户/归属分组/预算）写入全部同源；前端唤醒后统一重拉流水/统计与全局下拉数据源（记一笔分类/编辑弹窗账户/顶栏用户），停在设置页时各表同步重画。预算与账本名/币种改动另推 lrev 换血各端账本缓存（预算展示读该缓存）。请求带 `?cv=版本信标`（build 时烘焙 `static/version.txt`，页面注入 `window.CHAIMI_VER`），日志可分辨各客户端前端版本。

**SSE 仅用于 AI 流式**（对话/月报）：ai 内部为结构化事件流（生成器产 dict，HTTP 边界 `sse_stream` 统一序列化），bot 通道直接消费同一生成器，不二次解析文本。

***

## 三、目录结构

```
chaimi-ledger/
├── DESIGN.md / README.md / LICENSE
├── build.bat                    # 读 manifest 版本 → 烘焙 version.txt → py_compile 自检
│                                #   → 单元测试全绿 → 清理开发数据(app/data) → fnpack 打包
└── chaimi-ledger-fn/
    ├── manifest                 # 飞牛应用清单（appname/version/install_dep_apps=python312…）
    ├── config/                  # resource（api-scope）/ privilege（run-as: root）
    ├── cmd/                     # 生命周期脚本：main(start/stop/status)、install_init(装依赖)、
    │                            #   install/uninstall/upgrade/config 的 init+callback
    ├── wizard/                  # 卸载向导
    └── app/
        ├── server/              # config（桌面注册 iframe）/ images（图标、牛牛头像，禁缓存路由）
        ├── src/                 # 后端源码（见模块清单）
        └── static/              # 前端（app.html/home.html/*.js/base.css/views.css/Chart.js）
```

***

## 四、数据库设计（SQLite）

10 张表：`ledgers`（账本，v1.0.4 起 `owner_uid` 标记归属飞牛用户）、`categories`（分类，expense/income 平行两套，支持二级 parent_id）、`accounts`（账户）、`transactions`（流水）、`import_batches`（导入批次）、`category_mappings`（类型映射）、`attribution_groups`（归属分组=用户）、`attributions`（归属名单）、`ledger_members`（账本共享名单，v1.0.4）、`known_users`（打开过应用的飞牛用户档案，v1.0.4）。完整 DDL 见 `db.py`，要点：

**不变式与口径**

- **金额一律 INTEGER 存分**（浮点累计会漂移），前端/导入层换算，展示时 ÷100
- **账户余额不增量维护**：`余额 = initial_balance + Σincome − Σexpense + Σtransfer收 − Σtransfer付`，查询时实时算（撤销导入/删行/退款不需要同步余额）
- **退款不变式**：凡 `refund_for` 非空的行必为 transfer（只进原消费扣减、不计收支）；原消费行恒为 expense
- **映射表 v6 架构**：target 为 category_id 外键（悬空映射结构上不可能），全局层取消、默认映射按账本种入（`seed_default_mappings`，数据源 `db.DEFAULT_MAPPINGS`）；分类改名/删除由外键联动；learned=1 的学习映射在整批撤销时级联清理
- **单账本单币种**：币种是账本属性（`ledgers.currency`），导入校验不一致整批拒绝

**索引与连接**

```sql
idx_tx_hash(ledger_id, source, row_hash)   -- 去重批量比对
idx_tx_date(ledger_id, date)               -- 统计一律区间比较(date>=? AND date<?)，不用 LIKE
idx_tx_order / idx_tx_batch / idx_tx_refund -- 退款匹配 / 整批撤销 / 反向扫退款
PRAGMA journal_mode=WAL; busy_timeout=5000; foreign_keys=ON;
```

**迁移**：`PRAGMA user_version` 记录版本，启动按序执行（只增不删列）；对已发布表加列走 `_ensure_column`（PRAGMA table_info 查缺 → 幂等 ALTER），老库升级/新库建表共用同一路径。

**多用户与账本 ACL（v1.0.4，方案详见 MULTIUSER-PLAN.md）**：单库不拆文件，隔离靠账本级授权——飞牛统一网关注入 `X-Trim-Userid/Username/Isadmin` 请求头（应用只监听网关 UDS + 本地开发入口，头不可被外部伪造），中间件解析为请求级 ContextVar；访问判定收敛在 `_ledger_access`（owner 或 `ledger_members` 成员放行，系统上下文=机器人/本地开发全放行），所有按资源 id 操作的端点先经 `_row_ledger` 反查所属账本再判定。无头请求/无主账本的兜底：升级后首个 admin 打开应用认领全部 `owner_uid IS NULL` 的账本；`known_users` 由 `/api/whoami` 落档（fnOS 无用户列表 API，共享选人器数据源）。应用级操作（配置写/备份恢复/扫码总开关）走 `_admin_or_403`；账本级设置（分类/映射/账户/归属分组/预算）owner 与共享成员均可写，账本名/币种/删除/共享管理仅 owner（预算为独立端点 `PUT /api/ledgers/{id}/budget`）。

***

## 五、API 概览（前缀 /app/chaimi-ledger）

```
页面     GET /                    app.html      GET /home   登录/选用户页
身份     GET /api/whoami（当前飞牛用户，顺带落档 known_users/admin 认领）
         GET /api/users（打开过应用的飞牛用户=共享选人器）
账本     GET/POST/PUT/DELETE /api/ledgers[/{id}]（GET ?all=1 仅 admin 全量；PUT 改名/币种/图标仅 owner）
         PUT /api/ledgers/{id}/budget（预算=账本级共享设置，owner 与共享成员都可改）
共享     GET/PUT/DELETE /api/ledgers/{id}/members（仅 owner；只授权不动账本数据，不自动建记账用户）
流水     GET /api/transactions?ledger=&from=&to=&type=&category=&page=…
         POST/PUT/DELETE /api/transactions[/{id}]   （PUT 支持 refund_for 关联/取消）
待确认   GET /api/pending?ledger=&kind=     POST /api/pending/{tx_id}/resolve
分类/账户/映射  常规 CRUD（/api/categories /api/accounts /api/mappings，均带 ledger=；共享成员可写，改动经 rev 长轮询同步各端）
统计     /api/stats/{calendar|summary|category|trend|monthly|attribution}
导入     POST /api/import/parse（预览不入库）  POST /api/import/confirm（入库+learn 回写）
         GET /api/import/batches   DELETE /api/import/batches/{id}（整批撤销）
导出     GET /api/export?format=json|csv      导出 xlsx 含流水/归属统计 sheet
备份     POST /api/backup  GET /api/backups  POST /api/restore
用户     GET/POST/PUT/DELETE /api/attribution-groups[/{id}]（=用户，含 avatar）
         GET /api/attributions   PUT /api/attributions/{aid}（仅设组；增删已下线）
AI       GET /api/ai/chat（SSE 对话：记账出草稿/查账回统计）  POST /api/ai/receipt（小票）
         GET /api/ai/analyze（SSE 月报）  POST /api/ai/parse（保留接口）
实时     GET /api/rev/wait（长轮询，见第二章；rev=数据代数 + lrev=账本列表代数，
         共享/移除/建删账本与账本级设置改动实时同步各端，lsince 缺省兼容旧客户端）
机器人   POST /api/bot/test（设置页测试，按首条直连绑定走链路）
         /api/bot/ilink/*（扫码/状态/解绑，A 通道面板用；B 通道已下线）
资源     GET /images/{name}（图标/头像，禁缓存——替换同名 PNG 即生效）
```

流水分页响应统一 `{items, total, page, page_size}`。

***

## 六、AI 记账（ai.py）

| 功能 | 输入 → 输出 | 说明 |
|---|---|---|
| 自然语言记账 | 「早餐15元」「昨天打车32 微信付」→ 草稿卡 | 解析金额/类型/分类/账户/日期/备注，确认才入库 |
| 小票识别 | 图片 → 草稿卡 | 视觉模型 OCR+结构化（App 上传/微信发图共用） |
| 月度分析 | 月度聚合 → 自然语言洞察（SSE） | 只发聚合统计，不发逐条流水 |
| 对话查账 | 自然语言 → function call 查库 → 回答 | query_stats 出总额+流水明细附件 |
| 编辑回合 | 「第二笔是30」「分类改成购物」→ 结构化修改 | 草稿与流水列表共用（见机器人章） |

**要点**：

- 账户识别：提示词注入可用账户清单，`_resolve_account` 精确→包含匹配（「微信」→账户「微信支付」）
- 名称必填/备注可空：名称=简短品项（早餐），备注=商家门店（肯德基）
- 分类兜底批量调用：按一级分组、单请求 ≤50 行、整批未命中 >200 熔断；`response_format=json_object`，temperature=0
- 礼金规则单源（`_GIFT_RULE`）：＜200 的红包/转账归红包礼金、不进人情往来，词典/映射/AI 三处同口径
- **隐私哲学**：每词只发「一句话/一张图 + 分类账户清单 + 50 条少样本 + 聚合统计」，从不发全量账单；数据存储全本地

**配置**（chaimi.conf，设置页向导填写）：

```json
"llm": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus",
        "vl_model": "qwen3-vl-plus", "api_key": "",
        "vl_base_url": "", "vl_api_key": ""}
```

单 `llm` 段：`vl_model` 视觉模型（图片记账），`vl_base_url`/`vl_api_key` 留空共用文本端点、可单独覆盖。

***

## 七、账单导入（import_* 模块族 + platform_templates.py）

**模块结构**：`import_bill.py`（门面：统一入口 + 解析引擎与原语）+ `import_classify.py`（分类瀑布）+ `import_match.py`（快照/退款链/跨源与手动配对）+ `import_apply.py`（入库/撤销）+ `import_common.py`（叶子工具）+ `platform_templates.py`（五平台声明数据）。回归保障双保险：**解析级黄金快照**（文件→RawFlow）+ **预览级黄金快照**（RawFlow→FlowPreview，带状态账本），任何解析/分类/匹配变更须逐字段等价或显式再冻结。

**统一模板引擎**：支付宝/微信/京东（2026-10 新导出）/本应用导出/美团全部为**内置模板**（声明数据随应用发版），与用户模板（`chaimi.conf`，向导写入，用于未知新格式）走同一条执行路径 `parse_template`。路由按**表头集合匹配**：用户模板优先 → 内置互斥保序；平台调整导出格式时随应用更新内置模板。xlsx 统一转 CSV 行后参与匹配；GBK/UTF-8 双解码。模板 schema = v1 向导字段 + 引擎扩展（`refund_rules` 退款规则含方向守卫、`transfer_types` 不计收支集合、`name_fallback` 名称回退链、`expand` 京东括号取原消费+括号退款额、`two_pass` 导出批内直连、`hash_fields` 去重字段集等），未知字段显式报错。

**支持来源**：支付宝（CSV/GBK）、微信（xlsx，CSV 亦可）、京东（2026-10 新导出 CSV：退款独立成行）、本应用导出（CSV/xlsx）、美团（CSV）、任意自定义格式（向导模板）。

**流程（预览制）**：

```
上传 → parse_bill 解析(不入库) → 预览页（分类依据悬浮 / duplicate 灰显 / 跨源与手动重复置顶可恢复
     / 疑似重复提示 / 全重复时提示先撤销原批次）
     → 用户确认（改分类才回写学习映射）→ apply_import 四阶段（增强回填→批次→插入+learn+批内退款回填→rescan）
     → 低置信项照常入库打标，事后在「待确认」队列处理；支持整批撤销
```

**类型分流**（模板驱动）：退款规则命中→transfer 并触发退款匹配（微信「已全额退款」支出行有方向守卫防误判）；类型列命中 transfer 集合→transfer 子类（资金搬运不计收支）；其余按收/支枚举，未命中一律 transfer。

**分类瀑布**（支出/收入行五级，先中先赢）：① 关键词词典（产品例外→加油词→车牌正则→15 类词典序；人情类按小额二切红包礼金/人情往来）→ ② 映射表（默认+学习；映射到退款/不计收支组则整行转 transfer）→ ②' 同名直连（导出格式）→ ③ AI 兜底（单次生效不学习）→ ④ 硬兜底"其他"+待确认。**只学用户纠正**，自动结果一律不回写映射表。转账行走子类映射（TRANSFER_SUBCLASS，未知子类待确认）。

**退款匹配**（分层链，**同额优先贯穿全线**，宁低勿错；**所有层统一 90 天回看窗，窗外不猜进待确认**）：① 订单号（同单多行优先等额；唯一=同订单部分退款直连）→ ② 金额+名称（等额+前缀唯一=high；模糊按同额>相似度>新旧且公共前缀≥2 字；多候选取最近降 low）→ ②' 微信专属（商户+等额；红包/转账等额取最近，多候选优先交易对方相同）→ ②'' 括号退款额（京东子件部分退款：退款额=原消费括号标注额，跨批经 tags 持久化）→ ③ 启发式（≥额+窗内唯一，多候选恰有一笔等额则链等额）。出口：high 免确认自动扣减（归因原消费日期）/ low 待确认队列 / unmatched 队列。兜底：原消费后导入时 rescan 反向补关联；批内退款对负序号回填；每笔决策写日志（`chaimi.import`）。

**去重四道防线**（共用一次账本快照，含 (类型,金额) 索引与 hash 索引）：row_hash 精确去重（单号进 hash，同文件重导全跳）→ 跨源配对（平台↔渠道三层证据，同日±1 同额须唯一）→ **手动记账配对**（同日±1+等额+同类型，唯一候选+1:1 消耗，防先手记后导账单双计；增强只补单号不碰手填名称）→ 疑似重复软提示（三元组只提醒不拦截）。同日多笔同额（多次停车等同额真实消费）由单号进 hash、唯一性门槛、1:1 消耗三重保护全部正常入库。

**上传安全**：20MB 上限、扩展名白名单、xlsx 魔数校验、5 万行熔断、临时文件即用即删。

**导出回环**：导出 14 列（转账双账户、订单号、流水ID、退款关联），再导入零待确认零重复，退款关联/双账户/归属无损还原（两代列名由列别名兼容）。

> 模板字段规范与各平台声明以代码为准：`platform_templates.py`、`import_bill.py`（引擎+原语）、`db.DEFAULT_MAPPINGS`。

***

## 八、分类体系与统计口径

**分类**：支出 18 一级（餐饮美食/日用百货/数码电器/…/住房物业，2026-09 起取消二级细分，2026-10 补住房物业——房贷/房租/物业费原挤在充值缴费）；收入**独立** 8 一级（工资薪酬/兼职副业/理财收益/红包礼金/人情往来/报销/投资本金/其他收入，无退款返还——退款走 transfer 冲减），收支两套平行，映射表按 flow_type 分流——工资/兼职不再混进「投资理财」。种子数据见 db.py；历史库新增默认分类走 app_meta 打标的一次性迁移（db._migrate）。

**统计隔离规则**：

| type | 计入支出 | 计入收入 |
|---|---|---|
| expense | ✅ | — |
| income | — | ✅ |
| transfer（资金搬运） | ❌ | ❌ |
| transfer（退款，refund_for 非空） | **扣减** | ❌ |

```
净支出 = Σ(expense) − Σ(退款行.amount WHERE refund_for 非空)
```

**退款扣减归因到原消费日期（`o.date`）**，而非退款发生日——跨月退款（9月买10月退）自然回溯，9 月饼图扣得掉、10 月不会出现负净支出。日历/趋势/饼图/汇总全部同口径。归属统计维持毛支出口径（不扣退款），与分类净口径并存。

***

## 九、流水归属与用户（记账人）

**模型**：`attributions`（归属名单，由账单导入自动产生，不可手动增删）+ `attribution_groups`（分组，即「用户」，含头像）；一归属至多一组，删组成员回未分组（FK ON DELETE SET NULL）。

**核心决策——分组成员 = 归属名，非统计键**：统计键是查询时拼接的「归属名·平台」（如 大牛·支付宝）；分组取归属名原子值，配置一次即覆盖该成员全部平台账单 + 手动记账，新平台账单导入自动继承分组。例：大牛 + 小牛 → 「老公」，统计页看「老公这个月的收支」，组行可展开成员明细。

**显示解析层**（四端统一：流水页/编辑弹窗/导出/统计）：有组显组名、无组显原始归属（`attr_display` 解析列，名单查组 + 同名组双路径）。两个关键语义：

1. **编辑未变更不重写**：改其他字段、归属没动时保留原始归属（大牛 不被「老公」覆盖），平台构成在组内仍可追溯
2. **分组改名同步改写**：手动账存的是组名，改组名时同步 UPDATE 相关流水，否则会「掉组」

**用户 = 分组的业务别名**（表/API 不变）：登录页 home.html 三态——无用户→强制设置；缓存失效/换设备→用户卡选择；缓存有效→直进记账页。当前用户按账本存 localStorage（记账默认归属，可改）。头像两只牛（m/f，默认 f），`/images` 路由禁缓存。

***

## 十、微信机器人

### 10.1 传输与大脑分离

```
微信 ──iLink──▶ bot_ilink(长轮询线程) ──▶ bot_core.handle(text, user, image?)
                                                  │
                                        对话式草稿(内存会话) ──「确认」──▶ db
                                                  │
                                        统一回复文本(A 通道自行送出)
```

传输与大脑分离：bot_core 通道无关（新通道可插；B 网关通道已下线，见 10.4），A 直连是当前唯一传输层，共用大脑保证行为/话术/数据一致。iLink bot 身份单会话，`bot.mode` off↔direct 热切换（不重启）。企微通道与图片外发链路已移除（微信端图文渲染效果不佳），只发文本；小票识别收图完整保留。

### 10.2 bot_core（指令路由，1~7 确定性规则 + 8 兜底）

1. **白名单**：不在名单返回 ID 提示绑定（扫码自动加入）
2. **草稿指令**（DRAFT_PENDING 态）：确认/丢弃/草稿重发
3. **删除确认**（PENDING_DELETE 态）：确认删除/取消删除
4. **翻页**：更多/上一页（续看上次查询）
5. **撤销**：删最近一笔 source='bot' 流水（限 24h；bot 编辑过的手工/导入流水 source 不变，不会被误删）
6. **月报**：精确匹配 `月报[ YYYY-MM]` → monthly_analysis 文本
7. **帮助**：指令清单
8. **其余一切** → 状态分流：有草稿进编辑回合；否则 `ai.chat`（模型自带记账/查账路由）

**对话式草稿**：不落库，全程对话修订，「确认」才批量入库（need_confirm=0, source='bot'）；内存态 TTL 30 分钟，超时/重启即失，丢弃零残留。会话状态 per-user。

**编辑回合**（`ai.edit_round`，草稿与流水列表共用）：输出结构化意图——
`target=draft,action=revise`（改草稿，复用 `_booking_items` 规范化）/ `target=tx,update`（改已入库流水，patch 字段解析后走 `_update_tx`，与 `PUT /api/transactions` 同一条路）/ `target=tx,delete`（两步确认）/ `target=new`（新记账替换会话）/ `target=none`（与编辑无关，走 ai.chat，草稿保留）。

**查询渲染**：明细行「▪ 日期 名称 符号金额」三要素，首屏最多 10 笔（LIST_MAX），「更多」列全部上限 30（防刷屏）；「XX明细」分类下钻本地路由（精确→包含对齐一级），`matched=false` 不编造明细；修改回执明示改前→改后。

**无分类流水的可见口径（既有行为，注意）**：机器人明细列表 / AI 对话附件 / 分类下钻取数走 `db.fetch_tx_rows`（INNER JOIN categories），**无分类的流水不出现在这些列表里**；但统计聚合（汇总/日历/分类饼图）按金额正常计入。典型场景：bot 兜底记账的条目分类解析不到时以 category_id=NULL 入库——查账总额里算得进、明细列表里看不到。处理路径：在 App 里给它补分类（待确认队列或编辑弹窗），补完即进列表。该行为由 `tests/test_db.py::test_inner_join_and_filters` 锁定。

### 10.3 通道 A：bot_ilink.py（iLink 直连）

- **QR 登录**：设置页发起 → 渲染二维码 → 手机微信扫 → 轮询状态至 logged_in，凭证 `{account_id, token, base_url}` 落 chaimi.conf
- **长轮询线程**：daemon 线程循环（getupdates 挂起 ~35s），单 in-flight 守卫；**按 msg_id 去重**（最近 200 条 LRU，重连/重投只处理一次）；回复 POST 必须回传 `context_token`
- **收图**（小票识别）：广谱检测媒体线索 → CDN 下载（三种 URL 形态）→ AES-128-ECB + PKCS7 解密（密钥三形态兼容，依赖 pycryptodome）→ 落盘 receipts → parse_receipt → 草稿卡
- **凭证生命周期**：过期收 401 → 线程停、面板亮「凭证过期请重新扫码」（重扫必须人扫，无自动续期）
- **网络**：长轮询是出站连接，**无需公网穿透**，全程无回调端口

### 10.4 通道 B：已下线（v1.0.4）

网关/MCP 通道（OpenClaw 等 REST/MCP 对接、TCP sidecar 127.0.0.1:18710、api_token、
chaimi_mcp.py、技能包）整体移除：网关客户端只连一个 MCP，且其微信 bot 通道仅扫码人
私有（无法区分记账人），绑定方案在该形态下不成立。机器人仅保留 A 通道直连；
`_ledger` 仅认显式账本，不做任何默认兜底（多用户同库防串账）。

### 10.5 配置与热切换（chaimi.conf `bot` 段）

```json
"bot": {
  "mode": "off",                  // off | direct
  "allowed_users": [],            // 微信ID白名单（扫码自动加入）
  "ilink": [ {"account_id":"","token":"","base_url":"","user":"","ledger":"","label":""} ]
}
```

保存设置时 `_bot_apply` 差量生效（off↔direct 启停轮询线程，不重启）。设置页测试按钮
按首条 ilink 绑定的账本+归属走完整链路。

### 10.6 多人多账本绑定（一账本一用户一机器人）

绑定模型：**（账本, 用户）→ 一个 bot 实例**，凭证以 user 为唯一键（同微信重扫顶替、旧实例差量停机防僵尸轮询）。成员用自己的微信扫某账本的码，微信出现专属 bot 联系人，消息记入该账本，确认响应自动进白名单——身份即授权。绑定谁的码，记账自动写 `tags.attribution=该用户`。例：大牛扫「日常」的码→消息进日常；小牛另扫「日常」→也进日常（家庭共账）；大牛再扫「旅行」→顶替旧绑定转进旅行。二维码一次一扫。

### 10.7 数据量与隐私

- 长轮询 ~1KB/次 × 3000 次/天 ≈ 月 <100MB；记账一次 LLM ≈ 2500 in + 400 out ≈ 0.3 分，家庭使用（~10 笔/天）月成本不到 1 元
- 隐私出网口径与 AI 页一致（一句话/一张图 + 清单 + 少样本 + 聚合），全程出站直连
- 设置面板遗留待办：移除「测试对话」行（联调期保留）

***

## 十一、构建与部署

```
build.bat：读 manifest 版本 → 烘焙 static/version.txt → py_compile 自检 → 单元测试全绿
          → 清理 app/data 开发残留 → fnpack 打包 → chaimi-ledger.<版本>.fpk
NAS 安装：install_init ─▶ pip --target ${PKGVAR}/site-packages（清华/阿里源兜底）
cmd/main：系统 python3 + PYTHONPATH=site-packages 直跑；启动时依赖双保险补装
```

**备份哲学**：SQLite 开 WAL 后直接拷 .db 会得到不一致快照，必须用 `sqlite3.Connection.backup()` 或 `VACUUM INTO`（backup.py 实现）；备份 = db 副本 + chaimi.conf 副本，建议每日一次、保留 7 日；恢复 = 停写 → 覆盖 → `PRAGMA integrity_check` → 重启。小票图片存 `${TRIM_PKGVAR}/receipts/`，库内只存相对路径。

***

## 十二、关键设计决策

1. **SQLite 而非 PG**：年万条级数据量，零配置、文件即库、备份=拷文件；安装只需 pip 依赖，卸载干净
2. **单页原生 JS**：交互复杂度中等，单 HTML + 原生 JS + Chart.js 足够，无前端构建链
3. **预览制导入**：解析不入库 → 用户确认 → 整批可撤销；低置信不阻断（打标事后处理），几百行账单不被几行待确认卡住
4. **退款统一口径**：原消费恒 expense、退款行恒 transfer、扣减归因原消费日期——导入顺序无关，统计口径不分叉
5. **确认才入库**：AI 草稿（App/微信同语义）、对话式修订、删除两步确认——确认前统计零污染、丢弃零残留
6. **传输与大脑分离**：bot_core 通道无关，新通道可插；当前仅 A 通道直连，off↔direct 配置热切换（B 通道 v1.0.4 下线）
7. **导入上传安全**：大小/白名单/魔数/行数四重校验，防伪装与解压炸弹
8. **身份复用飞牛账号**：无自建账号体系——网关注入头即身份，账本级 ACL 隔离（v1.0.4 多用户）；「用户」（归属分组）是账本内的记账人维度

***

## 十三、版本里程碑

**1.0.0（2026-10-18）首个正式版**（与 manifest / `CHANGELOG.md` 一致）：功能全集——多账本记账、五平台账单导入（统一模板引擎 + 五级分类瀑布 + 四道去重防线 + 退款五层匹配链）、统计看板、AI 记账与查账、微信机器人双通道、分层备份；148 用例 + 双级黄金快照打包门禁，多份真机账单端到端验证。此前的内部迭代随定版收敛，演进明细见 git 历史；此后变更一律记 `CHANGELOG.md`。
