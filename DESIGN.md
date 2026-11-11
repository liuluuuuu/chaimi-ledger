# chaimi-ledger 设计文档

> 飞牛 NAS（fnOS）原生家庭记账应用。多账本分账 + 账单导入 + 统计看板 + AI 记账 + 微信机器人，数据全在本机 SQLite。
>
> 状态：**v1.0.9（2026-11-08）**。本文描述当前实现。

***

## 一、项目概述

- **形态**：飞牛原生应用（fpk 包），网关以 iframe 挂载 `/app/chaimi-ledger`
- **存储**：SQLite 单文件（`chaimi.db`），无需任何配置，备份就是拷文件
- **后端**：FastAPI（Python 3.12，`pip --target` 隔离装依赖，不动系统环境）
- **前端**：单 HTML + 原生 JS + Chart.js 4（按需加载，无构建链）
- **用户模型**：飞牛多用户（v1.0.4）。登录身份取自飞牛网关注入的请求头，各自账本互不可见、可共享（单账本多人记账互见）。「用户」（归属分组）是账本内的记账人维度，不是登录账号。应用级配置只有 admin 能改；账本级数据按 ACL（owner / 共享成员）控制，分类/映射/账户/用户/预算成员都可写，账本名/币种/删除/共享管理仅 owner
- **AI**：OpenAI 兼容协议，设置页填 Key 即用（预设百炼/智谱/DeepSeek 均含文本+视觉，也支持本地模型；没有视觉模型时图片记账自动禁用）
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
| import_apply.py | 入库五阶段（增强/批次/插入+批内回填/归属名单播种/rescan）与整批撤销 |
| import_common.py | 导入链路的叶子工具（normalize/去重谓词/占位名判定） |
| ai.py | AI 记账：自然语言/小票/月报/对话 + edit_round 编辑回合 |
| backup.py | SQLite backup API 备份（db 副本 + 配置副本 + 小票包）+ 月备/年备分层自动备份 |
| bot_core.py | 机器人大脑：白名单、指令路由、草稿会话、回复格式化（通道无关） |
| bot_ilink.py | 通道 A：iLink 客户端（QR 登录、长轮询、收发、媒体下载解密） |

前端（app/static/）：

- app.html 主界面 + home.html 登录/选用户页
- core / ledger / stats / settings / ai / import 六个 .js 按页拆分逻辑，catmgr.js 是分类管理共享模块（设置页与 home 建用户页抽屉复用，自带上下文适配层/样式/新建弹窗），theme.js（明暗主题三态，ESM）与 theme-boot.js（首帧防闪，两页共用）
- 业务 JS 以 defer 并行加载

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

**多端实时刷新（长轮询）**

前端调用 `GET /api/rev/wait?since=N&hold=20`，服务端最多挂住 hold 秒（钳制在 1~30 秒，缺省 25），数据版本 rev 一偏离就立即返回。

- 前端固定传 hold=20（core.js 里的 `HOLD`）。超时返回本身就充当心跳；客户端判断连接已断的超时由它推导为 hold+10 秒（正常请求一定在 hold 秒内完成，超过这个时间说明连接已经死了）
- 前端拿到返回后 300ms 发下一条长轮询；出错则 2 秒退避重连
- 页面回前台（focus/visibilitychange/pageshow）时掐掉挂起的请求，先做一次轻量探测：rev 与基线相同就不刷，偏离才刷
- 探测失败时不刷新页面：一次网络抖动就整页重载，会把打开的弹窗都丢掉。等长轮询自己重连后，按基线比对补拉就行
- 任意一端记账或改账本级设置，其他端 1 秒内刷新

写库侧统一走 `_bump_rev`：交易增/改/删、待确认转正、导入应用/撤销、bot 写库、账本级设置（分类/映射/账户/归属分组/预算）的写入，都经过它。前端被唤醒后统一重拉流水/统计与全局下拉的数据源（记一笔分类/编辑弹窗账户/顶栏用户），停在设置页时各表同步重画。预算与账本名/币种的改动另推 lrev，让各端换掉账本列表缓存（预算展示读该缓存）。

长轮询请求带 `?cv=版本信标`：build 时把版本烘焙进 `static/version.txt`，页面注入 `window.CHAIMI_VER` 与 `window.CHAIMI_MAX_MB`（上传上限），前端 MAX_UPLOAD_B 从注入值派生、不再手抄。这样日志里能分辨各客户端跑的前端版本。

**SSE 仅用于 AI 流式**（对话/月报）：ai 内部产出结构化事件流（生成器产 dict，HTTP 边界由 `sse_stream` 统一序列化），bot 通道直接消费同一个生成器，不二次解析文本。

***

## 三、目录结构

```
chaimi-ledger/
├── README.md / LICENSE
├── build.bat                    # 读 manifest 版本 → 烘焙 version.txt → py_compile 自检
│                                #   → 单元测试全绿 → 清理开发数据(app/data) → fnpack 打包
├── build.sh                     # 同流程的 fnOS/Linux 版（产物另投放下载目录）
└── chaimi-ledger-fn/
    ├── manifest                 # 飞牛应用清单（appname/version/install_dep_apps=python312…）
    ├── config/                  # resource（api-scope）/ privilege（run-as: package，无 root）
    ├── cmd/                     # 生命周期脚本：main(start/stop/status)、install_init(装依赖)、
    │                            #   install/uninstall/upgrade/config 的 init+callback
    ├── wizard/                  # 卸载向导
    └── app/
        ├── server/              # config（桌面注册 iframe）/ images（图标、牛牛头像，禁缓存路由）
        ├── src/                 # 后端源码（见模块清单）
        └── static/              # 前端（app.html/home.html/*.js/base.css/views.css/Chart.js）
```

设计文档（DESIGN.md）、更新日志（CHANGELOG.md）等放在仓库外工作区的 `docs/` 目录（与 `chaimi-ledger/` 同级）。

***

## 四、数据库设计（SQLite）

11 张表：`ledgers`（账本，v1.0.4 起 `owner_uid` 标记归属飞牛用户）、`categories`（分类，expense/income 平行两套，支持二级 parent_id）、`accounts`（账户）、`transactions`（流水）、`import_batches`（导入批次）、`category_mappings`（类型映射）、`attribution_groups`（归属分组=用户）、`attributions`（归属名单）、`ledger_members`（账本共享名单，v1.0.4）、`known_users`（打开过应用的飞牛用户档案，v1.0.4）、`app_meta`（迁移打标键值）。完整 DDL 见 `db.py`，要点如下。

**不变式与口径**

- 金额一律 INTEGER 存分（浮点累计有误差），前端/导入层换算，展示时 ÷100
- 账户余额不做增量维护：`余额 = initial_balance + Σincome − Σexpense + Σtransfer收 − Σtransfer付`，查询时实时算（撤销导入/删行/退款都不需要同步余额）
- 退款不变式：
  - `refund_for` 非空的行一定是 transfer，只用来扣减原消费，不计收支；原消费行一定是 expense
  - 原消费日 ≤ 退款日
  - 状态列始终等于 `db.refund_status(原消费额, 退款额)`
- 退款相关的四列只能经 db 层这几个函数写入：
  - `link_refund`：建立关联，校验自指、跨账本、非支出、日期先后、下游指向
  - `clear_refund`：解除关联，用 back_to_queue 区分用户主动取消和孤儿退款回到待确认
  - `orphan_origin_refunds`：原消费被删除时级联处理
  - `refund_link_gates`：导入落库前校验，伪造的数据整行跳过并重算状态
  - `refresh_pair_status` + `assert_date_order`：编辑后重算状态、检查日期先后
- 读取时的退款扣减统一用 `_REFUND_JOIN`，里面再判断一次 `r.type='transfer'`，双重保险
- 映射表 v6 架构：target 为 category_id 外键（悬空映射在结构上不可能），全局层取消、默认映射按账本种入（`seed_default_mappings`，数据源 `db.DEFAULT_MAPPINGS`）；分类改名/删除由外键联动。学习映射从 1.0.2 起不再产生，库里已有的 learned 行属于历史遗留，在映射页能看到、在修复区能重置，整批撤销时不会连带清理
- 单账本单币种：币种是账本属性（`ledgers.currency`），导入校验不一致整批拒绝

**索引与连接**

```sql
idx_tx_hash(ledger_id, source, row_hash)   -- 去重批量比对
idx_tx_date(ledger_id, date)               -- 统计一律区间比较(date>=? AND date<?)，不用 LIKE
idx_tx_order / idx_tx_batch / idx_tx_refund -- 退款匹配 / 整批撤销 / 反向扫退款
PRAGMA journal_mode=WAL; busy_timeout=5000; foreign_keys=ON;
```

**迁移**：`app_meta` 键值表打标的一次性迁移（`db._migrate`，启动按序执行未跑过的迁移并打标；恢复旧版备份会自动重跑补齐），只增不删列。对已发布表加列走 `_ensure_column`（PRAGMA table_info 查缺 → 幂等 ALTER），老库升级与新库建表共用同一路径。

**多用户与账本 ACL（v1.0.4，方案详见 MULTIUSER-PLAN.md）**

单库不拆文件，隔离靠账本级授权：

- 飞牛统一网关注入 `X-Trim-Userid/Username/Isadmin` 请求头（应用只监听网关 UDS + 本地开发入口，头不可被外部伪造），中间件解析为请求级 ContextVar
- 访问判定集中在 `_ledger_access`（owner 或 `ledger_members` 成员放行；系统上下文=机器人/本地开发全放行），所有按资源 id 操作的端点先经 `_row_ledger` 反查所属账本再判定
- 无主账本的处理：admin 打开应用时认领全部 `owner_uid IS NULL` 的账本。认领是否完成每次问库（`_admin_claim_done` 查还有没有无主账本，进程不缓存这个状态，所以恢复多用户之前的旧备份后会自动重新认领）
- `known_users` 由 `/api/whoami` 落档（fnOS 没有用户列表 API，这是共享选人器的数据源）
- 应用级操作（配置写/备份恢复/扫码总开关）走 `_admin_or_403`；账本级设置（分类/映射/账户/归属分组/预算）owner 与共享成员均可写，账本名/币种/删除/共享管理仅 owner（预算为独立端点 `PUT /api/ledgers/{id}/budget`）
- 小票图 `GET /receipts/{name}` 与手动裁剪 `POST /api/receipts/crop` 走 `_receipt_ledger_gate`（v1.0.6 起）：库内路径统一 `receipts/文件名` 单一形态（历史行启动迁移归一），按该形态索引精确查、未命中回落 basename 全扫兜启动后手改库的异常数据：反查到引用流水的账本后，用与 `_ledger_access` 同一套规则判权；未被流水引用的在途图（App 先预览后确认、机器人草稿期）放行：文件名是随机的，孤儿文件 24 小时后自动清理，这样已经够了

***

## 五、API 概览（前缀 /app/chaimi-ledger）

```
页面     GET /                    飞牛用户按设备记忆分流：Cookie 记着有效用户直出 app.html 并续期，否则 302 → /static/home.html；系统上下文直出 app.html
         GET /app                app.html（home 选定/自动进入的直入目标，不做分流）
         GET /static/home.html   登录/选用户页（自带 base）
身份     GET /api/whoami（当前飞牛用户，顺带落档 known_users/admin 认领）
         GET /api/users（打开过应用的飞牛用户=共享选人器）
账本     GET/POST/PUT/DELETE /api/ledgers[/{id}]（GET ?all=1 仅 admin 全量；PUT 改名/币种/图标仅 owner；
         PUT /{id}/category-source 对尚无流水的新账本整体替换分类来源，是 home 建用户页「分类来自」的后端）
         PUT /api/ledgers/{id}/budget（预算=账本级共享设置，owner 与共享成员都可改）
共享     GET/PUT/DELETE /api/ledgers/{id}/members（仅 owner；只授权不动账本数据，不自动建记账用户）
流水     GET /api/transactions?ledger=&from=&to=&type=&category=&page=…
         POST/PUT/DELETE /api/transactions[/{id}]   （PUT 支持 refund_for 关联/取消；
         App 写入时 source 只接受 app，导入和机器人有各自的写入口；不限制的话，任意字符串入库后会在列表里原样显示）
待确认   GET /api/transactions?need_confirm=1（列表徽标/筛选共用流水接口）
         POST /api/pending/{tx_id}/resolve   （改分类/账户/退款关联 + 转正）
分类/账户/映射  常规 CRUD（/api/categories /api/accounts /api/mappings，均带 ledger=；共享成员可写，改动经 rev 长轮询同步各端）
统计     /api/stats/{calendar|summary|category|trend|monthly|yearly|attribution|renqing}
         另有 range 系列（?from=&to= 自定义区间：summary/category/attribution）；month/year
         参数统一校验非法 400（格式校验只有一套：月 db.month_range、年 db.year_range）
导入     POST /api/import/parse（预览不入库）  POST /api/import/confirm（单事务入库+归属名单播种；
         预览数据由客户端提交，不能全信，所以按 id 引用的列（分类/账户/退款原消费/跨源回填目标）一律限定在本账本）
         GET /api/import/batches   DELETE /api/import/batches/{id}（整批撤销）
导出     GET /api/export?format=json|csv      导出 xlsx 含流水/归属统计 sheet
备份     POST /api/backup  GET /api/backups  POST /api/restore
用户     GET/POST/PUT/DELETE /api/attribution-groups[/{id}]（=用户，含 avatar）
         GET /api/attributions   PUT /api/attributions/{aid}（仅设组；增删已下线）
AI       GET /api/ai/chat（SSE 对话：记账出草稿/查账回统计）  POST /api/ai/receipt（小票）
         GET /api/ai/analyze（SSE 月报）  POST /api/ai/parse（保留接口）
         记账条目一律带 category_id/account_id 下发（后端 ai._cat_ids 统一解析，前端不另匹配）
实时     GET /api/rev/wait（长轮询，见第二章；rev=数据代数 + lrev=账本列表代数，
         共享/移除/建删账本与账本级设置改动实时同步各端，lsince 缺省兼容旧客户端）
机器人   /api/bot/ilink/*（扫码/状态/解绑，A 通道面板用；B 通道已下线；联调走 bot_core CLI）
资源     GET /images/{name}（图标/头像，禁缓存，替换同名 PNG 即生效）
```

流水分页响应统一 `{items, total, page, page_size}`。

***

## 六、AI 记账（ai.py）

| 功能 | 输入 → 输出 | 说明 |
|---|---|---|
| 自然语言记账 | 「早餐15元」「昨天打车32 微信付」→ 草稿卡 | 解析金额/类型/分类/账户/日期/备注；App 确认才入库，机器人可开直接入库（见 10.2） |
| 小票识别 | 图片 → 草稿卡 | 视觉模型 OCR+结构化（App 上传/微信发图共用） |
| 月度分析 | 月度聚合 → 自然语言洞察（SSE） | 只发聚合统计，不发逐条流水 |
| 对话查账 | 自然语言 → function call 查库 → 回答 | query_stats 出总额+流水明细附件 |
| 编辑回合 | 「第二笔是30」「分类改成购物」→ 结构化修改 | 草稿与流水列表共用（见机器人章） |

**要点**：

- 账户识别：提示词注入可用账户清单，`_resolve_account` 精确→包含匹配（「微信」→账户「微信支付」）
- 分类 id 由后端统一解析：`ai._cat_ids` 调用 `db.match_top_cat`，给每个条目填上 category_id
  - 匹配一级分类分两步：先在同收支方向里找名字完全相同的，再找双向包含的，所以简称「餐饮」能命中「餐饮美食」
  - 空名不参与匹配。空字符串在「包含」这一步永远成立，这个判断只写在 `db.match_top_cat` 里
  - LLM 记账、编辑回合（`_booking_items`）、离线兜底、小票（`parse_receipt` 带 ledger）这四路下发的条目都带 id，App 前端直接用 id 建卡，不再自己按名称匹配；分类和账户的行查询也只有一份（`db.top_cat_rows` / `db.account_rows`）
  - 机器人入库时仍按名称重新解析，不沿用草稿里的 id，这样草稿期间分类被删改也不会出外键错误
- 离线兜底（无 Key/上游故障时 `fix_local` 正则拆分）只在完全没有产出时触发，任何非错误事件（包括查账的 stats）都算产出。否则「这个月加油300的是哪天」这类查账句在统计已推送、结尾断流时会被拆成假账；兜底条目同带 category_id 与同日同额重复提示（`dup_hints`）
- 整句启发式（礼金人名集中在 `_gift_fix`、时段补时刻 `_time_fix`）只对单笔的句子生效，多笔的句子套用的话，会把第一个时刻或人名套到每一笔上；口说金额字符串「1.5万」「1万5」按倍数折算（`strip_amount_str`，与 fix_local 共用同一份实现）
- 统计区间脏值容错：模型给出「上周」「上个月」这类非法端点/月份时回落今天/本月（格式校验同一套：`db.iso_date_ok` / `db.month_range`），不抛成整轮英文报错
- 名称必填/备注可空：名称=简短品项（早餐），备注=商家门店（肯德基）
- 导入批处理要选快对话模型：批分类要求秒级返回，推理/思考型模型（reasoner/thinking/r1 类）会先长思考再作答，10~20 行的批也会用满 40s 时限。设置页模型选 qwen-plus / deepseek-chat / glm-4-air 这类；AI 批日志带「模型 @ 服务商」便于定位
- 分类兜底批量调用：单请求 ≤50 行、收/支/名称全部批次同池 6 路并发；`response_format=json_object`，temperature=0。总时限 40s（`ai._IMPORT_AI_BUDGET`，飞牛网关约 60s 掐断请求，40s 留出解析与传输余量）：单批超时=时限剩余、批处理不重试，到点还没处理的批记下原因后跳过。这样预览总能返回，AI 慢也拖不垮整个导入
- AI 兜底状态可见：未配置 Key / 调用失败（欠费、Key 无效、超时、无法连接…中文原因）/ 模型返回非 JSON，一律回传导入页（`ai` 字段）并写日志，不再静默。模型答兜底分类（其他/其他收入，只在 `db.FALLBACK_TOP` 定义）说明 AI 也拿不准，不算命中，这行保持待确认。规则层也是这样：落到「其他」的行必须待确认
- 礼金规则只有一份：≤200 元的红包/转账归红包礼金（阈值只在 `db.RENQING_SMALL_PACKET_CENTS` 定义一处），词典/映射/AI 三处同一套规则；提示词层 `ai._GIFT_RULE` 只管 name/note 填法
- 隐私：每次只发「一句话/一张图 + 分类账户清单 + 50 条少样本 + 聚合统计」，从不发全量账单；数据存储全本地

**配置**（chaimi.conf，设置页向导填写）：

```json
"llm": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus",
        "vl_model": "qwen3-vl-plus", "api_key": "",
        "vl_base_url": "", "vl_api_key": ""}
```

单 `llm` 段：`vl_model` 视觉模型（图片记账），`vl_base_url`/`vl_api_key` 留空共用文本端点、可单独覆盖。

***

## 七、账单导入（import_* 模块族 + platform_templates.py）

**模块结构**：`import_bill.py`（门面：统一入口 + 解析引擎与原语）+ `import_classify.py`（分类瀑布）+ `import_match.py`（快照/退款链/跨源与手动配对）+ `import_apply.py`（入库/撤销）+ `import_common.py`（叶子工具）+ `platform_templates.py`（五平台声明数据）。

回归保障双保险：解析级黄金快照（文件→RawFlow）+ 预览级黄金快照（RawFlow→FlowPreview，带状态账本），解析、分类、匹配逻辑有任何改动，输出要么逐字段不变，要么明确地重新生成快照。

**统一模板引擎**

支付宝/微信/京东（2026-10 新导出）/本应用导出/美团全部为内置模板（声明数据随应用发版）；用户模板（`chaimi.conf`，向导写入，用于未知新格式）与内置模板走同一条执行路径 `parse_template`。

- 路由按表头集合匹配：用户模板优先（复制的模板改列名后即接管该格式），内置互斥保序；平台调整导出格式时随应用更新内置模板
- xlsx 统一转 CSV 行后参与匹配；GBK/UTF-8 双解码
- 模板 schema = v1 向导字段 + 引擎扩展：`refund_rules` 退款规则含方向守卫、`transfer_types` 不计收支集合、`name_fallback` 名称回退链、`expand` 京东括号取原消费+括号退款额、`two_pass` 导出批内直连、`hash_fields` 去重字段集等；未知字段显式报错

**支持来源**：支付宝（CSV/GBK）、微信（xlsx，CSV 亦可）、京东（2026-10 新导出 CSV：退款独立成行）、本应用导出（CSV/xlsx）、美团（CSV）、任意自定义格式（向导模板）。

**流程（预览制）**：

```
上传 → parse_bill 解析(不入库) → 预览页（分类依据悬浮 / duplicate 灰显 / 跨源与手动重复置顶可恢复
     / 疑似重复提示 / 全重复时提示先撤销原批次）
     → 用户确认 → apply_import 五阶段（增强回填→批次→插入+批内退款回填→归属名单播种→rescan，
       单事务原子提交；用户预览中手改分类打 manual_cat 标，不回写映射表）
     → 低置信项照常入库打标，事后在「待确认」队列处理；支持整批撤销
```

**类型分流**（模板驱动）：退款规则命中 → transfer 并触发退款匹配（微信「已全额退款」支出行有方向守卫防误判）；类型列命中 transfer 集合 → transfer 子类（资金搬运不计收支）；其余按收/支枚举，未命中一律 transfer。

**分类瀑布**（支出/收入行五级，先中先赢）：

1. 关键词词典：产品例外 → 加油词 → 车牌正则 → 15 类词典序；人情类按小额二切红包礼金/人情往来
2. 映射表：默认+学习；映射到退款/不计收支组则整行转 transfer
3. 同名直连：来源分类名与账本分类同名
4. AI 兜底（单次生效不学习）
5. 全没命中：落「其他」+ 待确认

只学用户纠正，自动结果一律不回写映射表。转账行走子类映射（TRANSFER_SUBCLASS，未知子类待确认）。

**柴米账单直入**（v1.0.8）：本应用导出的文件不走瀑布，因为文件里的分类就是用户定过的最终结果，按原名直取（`classify_export`），不进待确认、不调 AI（端点对整份柴米账单关 AI）。账本缺失的分类自动新建同名分类（`ClassifyCtx.ensure`，与导入建银行卡账户同一处理），源头未分类保持未分类；自建的不计收支组分类按原名还原，已知子类名（旧版二级分类形态）仍走「不计收支 + transfer_sub」。直接入库的行打 manual_cat：文件里的分类可能包含导出前的手动调整，而 manual_cat 标记本身不在文件里，所以导入时补上，之后数据修复不会改动这些分类。

**退款匹配**（分层链，共五层，同额优先贯穿全线，宁少匹配也不错匹配；所有层统一 90 天回看窗，窗外不猜、进待确认）：

1. 订单号：同单多行优先等额；唯一=同订单部分退款直连
2. 金额+名称：等额+前缀唯一=high；模糊按同额>相似度>新旧排序，且公共前缀 ≥2 字；多候选取最近降 low
3. 微信专属：商户+等额；红包/转账等额取最近，多候选优先交易对方相同
4. 括号退款额：京东子件部分退款，退款额=原消费括号标注额，跨批经 tags 持久化
5. 启发式：金额 ≥ 退款额 + 窗内唯一；多候选恰有一笔等额则链等额

出口：high 免确认自动扣减（归因原消费日期）/ low 待确认队列 / unmatched 队列。补救路径：原消费后导入时 rescan 反向补关联；批内退款对负序号回填；每笔决策写日志（`chaimi.import`）。

**去重四道防线**（共用一次账本快照，含 (类型,金额) 索引与 hash 索引）：

1. row_hash 精确去重：单号进 hash，同文件重导全跳
2. 跨源配对：平台↔渠道三层证据，同日±1 同额须唯一
3. 手动记账配对：同日±1+等额+同类型，唯一候选+1:1 消耗，防先手记后导账单双计；增强只补单号不碰手填名称
4. 疑似重复软提示：三元组只提醒不拦截

同日多笔同额（多次停车等同额真实消费）由单号进 hash、唯一性门槛、1:1 消耗三重保护全部正常入库。

**上传安全**：20MB 上限、扩展名白名单、xlsx 文件头校验（前两字节须为 PK）、5 万行熔断、临时文件即用即删。

**导出回环**：导出 13 列（支付账户、订单号、流水ID、退款关联、币种、归属等），再导入无待确认无重复，退款关联/归属无损还原（两代列名由列别名兼容）。前提是账本数据本身没问题：库里已有的 ¥0 流水导回时按设计标为无效，日期颠倒的退款关联会被日期先后校验拦下、转待确认，见 CHANGELOG 1.0.8 排查记录。

v1.0.8 起去掉恒空的「小票」列与卡片上不显示的「收款账户」列；导入模板仍认这两列，旧导出文件重导照常还原转账收款端。回环判重三层：row_hash →（来源,订单号,日,金额,类型）五元组 → 流水ID 身份（本账本有此 id 且来源/日/金额/类型一致）。row_hash 含名称/分类/备注且冻结在入库时，入库后改过这几列的无订单号行只能靠流水ID 认回；跨账本合并时 id 不在目标账本快照内，自然不命中。零额行解析期一律标无效（skip_zero_reason 定制理由），与确认入库的 check_tx_fields 用同一套判定。

> 模板字段规范与各平台声明以代码为准：`platform_templates.py`、`import_bill.py`（引擎+原语）、`db.DEFAULT_MAPPINGS`。

***

## 八、分类体系与统计口径

**分类**：支出 18 个一级（餐饮美食/日用百货/数码电器/…/住房物业；2026-09 起取消二级细分，2026-10 补了住房物业，房贷、房租、物业费原来都挤在充值缴费里）。收入独立 8 个一级（工资薪酬/兼职副业/理财收益/红包礼金/人情往来/报销/投资本金/其他收入；没有「退款返还」，退款走 transfer 冲减）。收支两套平行，映射表按 flow_type 分开，工资、兼职不会再混进「投资理财」。种子数据见 db.py；历史库新增默认分类走 app_meta 打标的一次性迁移（db._migrate）。

**统计隔离规则**：

| type | 支出 | 收入 |
|---|---|---|
| expense | 计入 | — |
| income | — | 计入 |
| transfer（资金搬运） | 不计 | 不计 |
| transfer（退款，refund_for 非空） | 扣减 | 不计 |

```
净支出 = Σ(expense) − Σ(退款行.amount WHERE refund_for 非空)
```

退款扣减归因到原消费日期（`o.date`），不是退款发生日。跨月退款（9 月买、10 月退）会算回 9 月：9 月饼图能扣掉，10 月也不会出现负的净支出。日历/趋势/饼图/汇总全部同口径。归属统计维持毛支出口径（不扣退款），与分类净口径并存。

***

## 九、流水归属与用户（记账人）

**模型**：`attributions`（归属名单，由账单导入自动产生，不可手动增删）+ `attribution_groups`（分组，即「用户」，含头像）；一归属至多一组，删组成员回未分组（FK ON DELETE SET NULL）。

**核心决策：分组成员是归属名，不是统计键**

统计键是查询时拼接的「归属名·平台」（如 大牛·支付宝）；分组取的是归属名原子值，配置一次即覆盖该成员全部平台账单 + 手动记账，新平台账单导入自动继承分组。例：大牛 + 小牛 → 「老公」，统计页看「老公这个月的收支」，组行可展开成员明细。

**显示解析层**（四端统一：流水页/编辑弹窗/导出/统计）：有组显组名、无组显原始归属（`attr_display` 解析列，名单查组 + 同名组双路径）。两个关键语义：

1. 编辑未变更不重写：改其他字段、归属没动时保留原始归属（大牛 不被「老公」覆盖），平台构成在组内仍可追溯
2. 分组改名同步改写：手动账存的是组名，改组名时同步 UPDATE 相关流水，否则会「掉组」

**用户 = 分组的业务别名**（表/API 不变）。登录页 home.html 三态：无用户 → 强制设置；记忆失效/换设备 → 用户卡选择；记忆有效的设备在 / 入口就直出主界面，不经过本页。设备记忆（上次账本 + 各账本上次用户）按账本存 Cookie（`chaimi_ledger` / `chaimi_user_{账本id}`，读写见 shared-lite.js），记账默认归属当前用户（可改）。头像两只牛（m/f，默认 f），`/images` 路由禁缓存。

桌面布局与主应用一致：左品牌区 = 窗口灰（`--win`，app 侧栏同底色），右表单 = 描边圆角卡片（同 .main）。两列都是纵向 flex，子项不收缩，高度不够时改为滚动（::before/::after 弹性占位保持垂直居中，超高时占位归零贴顶可滚），按钮/输入框不压缩。

建用户流程「分类来自」是 select+按钮 的 flex 行，select 必须带 `min-width:0`（flex 子项默认最小宽=最长 option 文本，不放开窄屏会把按钮挤出屏，v1.0.9 修复；全站 flex+select 行同一处理，比如 app.html 的共享弹窗。这个弹窗在 `.st-sec` 作用域外，`.row` 类不带 flex，行内须自备 `display:flex`）。

手机物理返回键走 popstate 守卫（core.js navTopModal 的单屏简化版 homeTopLayer）：新建分类弹窗 > 编辑抽屉 > 「再按一次退出应用」提示；首帧垫一层 history 帧、popstate 关顶层浮层并补一帧，浮层经 ✕/遮罩关闭不动历史（v1.0.9；此前抽屉开着按返回会直接退出应用）。

**入口分流（v1.0.13 定稿：设备记忆存 Cookie）**

- 设备记忆 = 上次账本 + 该账本上次用户，存 Cookie（`chaimi_ledger`、`chaimi_user_{账本id}`）。曾存 localStorage，服务端看不见，入口只能一律先 302 到 home 让页面脚本判定；Cookie 随每次请求自动上报，判定得以收进服务端。老版本（≤1.0.12）的 localStorage 记忆不做迁移：升级后首开停在选人页，点一次用户即写入 Cookie，旧键留在浏览器里不再读写
- GET / 分两段：没带 Cookie 的设备（新设备/清了数据/无痕窗口）不问库，直接 302 到 home；带了才问库验证（`_ledger_access` 同口径 + 用户组仍在该账本下），有效直出 app.html，并由服务端重发两个 Cookie 续期——页面脚本写的 Cookie 在 Safari/iOS 最多活 7 天，服务端下发的不受此限，验证不过（用户或账本已删、无权）同样 302 home。302 前 `_touch_fn_user` 保认领/落档
- GET /app 不做分流，一定给主界面：home 选定/自动进入的落点（enter → "app"）。不走 / ——那会再判一次，万一 Cookie 没被网关带到，就与 home 的自动进入互为循环
- home 的自动进入只服务直达本页的场景（书签直开、升级首开搬完记忆、app 弹回带 `?ledger=`）；「切换用户」（app 顶栏 gbUser）带 `?pick=1` 跳过自动进入。常规冷启动不再经过 home，页内账本列表→用户列表两步串行即可（v1.0.9 时代为省一个来回做的请求预取已随入口改版移除）
- 判断期间不是白屏等：gate-hold 期间盖一层启动画面（主题色底 + 静止图标，规则在 base.css，app/home 两页共用同一份），判定完成经 gateLift（theme-boot.js）渐隐揭层（gate-fade 过渡态，260ms 后移除两个类；「减少动态效果」偏好下过渡关闭直接揭）。home 的放行点是 render()（boot 出口全经此），自动进入的用户全程只见启动画面、初始「正在加载…」不闪现，停下的用户判断完一次性看到完整页面；4s 到点仍无结果则亮出加载/失败态
- app.html 的 gate-hold 保留作刷新/直入的保底：无条件加类（同一层启动画面），唯一可信的判断是 restoreCurUser 查库，绑定成功就放行，没有有效用户就跳 home。4s 到点仍无结果时分两种：有记忆放行（接口挂不误伤回访用户）、没记忆改投 home 自行恢复

***

## 十、微信机器人

### 10.1 传输与大脑分离

```
微信 ──iLink──▶ bot_ilink(长轮询线程) ──▶ bot_core.handle(text, user, image?)
                                                  │
                          reply：对话式草稿(内存会话) ──「确认」──▶ _commit ──▶ db
                          auto ：识别结果 ─────────── 直接入库 ──▶ _commit ──▶ db
                                                  │      （坏金额/入库异常 → 留草稿等「确认」）
                                        统一回复文本(A 通道自行送出)
```

bot_core 通道无关（新通道可插；B 网关通道已下线，见 10.4）。A 直连是当前唯一传输层，共用大脑保证行为/话术/数据一致。iLink bot 身份单会话，`bot.mode` off↔direct 热切换（不重启）。企微通道与图片外发链路已移除（微信端图文渲染效果不佳），只发文本；小票识别收图完整保留。

### 10.2 bot_core（指令路由：1 删除确认 → 2~8 确定性规则 → 其余进 AI）

入口 `handle()` 先过白名单（不在名单返回 ID 提示绑定，扫码自动加入；陌生 id 不注册 per-user 锁），授权用户全程持 per-user 锁串行，再进下列路由：

1. 删除确认（PENDING_DELETE 态，最先判）：只认「确认删除/取消删除」；收到任何其他消息都当场取消挂起。以前草稿指令、翻页、撤销、月报、帮助、小票都会提前 return，把挂起留着，30 分钟内迟到的「确认删除」会删掉早就忘了的旧清单。无挂起时「确认删除」明确提示重新发起
2. 草稿指令（DRAFT_PENDING 态）：确认/丢弃/草稿重发/查看（重复对照）
3. 翻页：更多/上一页（续看上次查询）
4. 撤销：单笔（删最近一笔 source='bot' 流水，限 24h；bot 编辑过的手工/导入流水 source 不变不会被误删）/ 批量「撤销N笔」（撤销5笔/撤回二笔/撤销两笔/undo 2，「笔」可省，中文数字一/两~九十九）。批量两步确认：先列将删的最近 N 笔（超 10 行折叠），「确认删除」执行；规则与单笔完全一致（source='bot'、24h、按归属过滤不误删家人），`_undo_rows` 是同一份实现。挂起态统一为待删 id 列表（单笔 1 元素），确认间隙被并发删掉的跳过并如实计数
5. 月报：精确匹配 `月报[ YYYY-MM]` → monthly_analysis 文本
6. 帮助：指令清单（随确认模式切换文案）
7. 图片：小票 → 草稿 / 并入当前草稿 / 直接入库（随模式）
8. 其余一切 → 状态分流：有草稿进编辑回合；否则 `ai.chat`（模型自带记账/查账路由）

**对话式草稿与记账确认模式**（`bot.confirm_mode`，设置页开关）

默认 reply=回复确认入库：识别结果进草稿卡（不落库，全程对话修订），「确认」才批量入库（need_confirm=0, source='bot'）；内存态 TTL 30 分钟，超时/重启即失，丢弃不留残余。

可切 auto=识别直接入库：一句话多笔、小票识别后直接入库，回复列出本笔明细（整批多笔时行格式与草稿卡相同；坏笔单列）并附「撤销N笔」整批撤指引；错账靠撤销补救。

两种模式共用 `_commit` 单一入库口：

- 金额无效的笔（0/没识别出）一律不硬入库、留草稿等修正后「确认」（全坏则整单转草稿）
- 入库异常（事务回滚）整单留草稿重试
- 遗留待确认草稿与新记账并入同批处理（不互相顶掉）

疑似重复（同日同额）在直接入库模式下没有确认前的人工检查，入库后如实附注请核对。会话状态 per-user。

**编辑回合**（`ai.edit_round`，草稿与流水列表共用）：输出结构化意图：

- `target=draft,action=revise`（改草稿，复用 `_booking_items` 规范化；模型漏回的键按原草稿补齐：只回 `{"amount":50}` 时，日期/名称/账户不会被重置，缺金额的条目也不再被整笔滤掉；显式给出的键含置空以模型为准）
- `target=tx,update`（改已入库流水，patch 字段解析后走 `db.patch_tx` 字段级部分更新，与 `PUT /api/transactions` 走同一套更新，文本列截断统一用 `db.TX_TEXT_MAX`）
- `target=tx,delete`（两步确认）
- `target=new`（新记账替换会话）
- `target=none`（与编辑无关，走 ai.chat，草稿保留）

**查询渲染**：明细行「▪ 日期 名称 符号金额」三要素，首屏最多 10 笔（LIST_MAX），「更多」列全部上限 30（防刷屏）；「XX明细」分类下钻本地路由（精确→包含两段匹配到一级），`matched=false` 不编造明细；修改回执明示改前→改后。

**无分类流水的可见口径（v1.0.8 起统一 LEFT JOIN）**：机器人明细列表 / AI 对话附件 / 分类下钻取数走 `db.fetch_tx_rows`（TX_FROM 为 LEFT JOIN categories），无分类/分类被删的行照常出现在这些列表里（分类名回落类型名），与统计聚合（汇总/日历/分类饼图归「未分类」桶）口径一致。该行为由 `tests/test_db.py` 的 join/filters 用例锁定。

### 10.3 通道 A：bot_ilink.py（iLink 直连）

- **QR 登录**：设置页发起 → 渲染二维码 → 手机微信扫 → 轮询状态至 logged_in，凭证 `{account_id, token, base_url}` 落 chaimi.conf。扫码会话凭证（qrcode 与待编码的绑定 URL）不写进日志，只记长度，因为有效期内拿到日志的人就能抢先绑定
- **长轮询线程**：daemon 线程循环（getupdates 挂起 ~35s），单 in-flight 守卫；消息去重一道落库（`bot_seen` 幂等表，同键只有一个插入成功，7 天滚动清理）——重投、跨重启重放、双进程同收都拦下，记账恒一笔（消息身份按 message_id，无则回落 client_id、再无才用掺秒指纹；message_type 显式非 1 的是机器人回声，不处理）；每条收发的消息都留 INFO 日志（无平台 id 时连原始字段名记下，真机形态可从日志直读）；回复 POST 必须回传 `context_token`。空体/非对象应答（网关错误页形态；合法超时也要先挂满 hold 才回）固定等 1s 再发，也不标记为在线。以前把它当成功处理、立刻重发，会变成空转的死循环
- **停机**：stop 不等长轮询返回（请求返回后停止检查即退出，不处理/回复任何消息），只等持 `_busy` 锁的在途协议动作（上线通知/逐条处理消息）收尾、至多 3s；apply/stop_all 并发停止多个实例，所以恢复备份、换绑的耗时和绑定数量无关
- **收图**（小票识别）：广谱检测媒体线索 → CDN 下载（三种 URL 形态）→ AES-128-ECB + PKCS7 解密（密钥三形态兼容，依赖 pycryptodome）→ 落盘 receipts → parse_receipt → 草稿卡
- **凭证生命周期**：过期收 401 → 线程停、面板亮「凭证过期请重新扫码」（重扫必须人扫，无自动续期）
- **网络**：长轮询是出站连接，无需公网穿透，全程无回调端口

### 10.4 通道 B：已下线（v1.0.4）

网关/MCP 通道（OpenClaw 等 REST/MCP 对接、TCP sidecar 127.0.0.1:18710、api_token、chaimi_mcp.py、技能包）整体移除：网关客户端只连一个 MCP，且其微信 bot 通道仅扫码人私有（无法区分记账人），绑定方案在该形态下不成立。机器人仅保留 A 通道直连；`_ledger` 仅认显式账本，不做任何默认账本（多用户同库防串账）。

### 10.5 配置与热切换（chaimi.conf `bot` 段）

```json
"bot": {
  "mode": "off",                  // off | direct
  "confirm_mode": "reply",        // reply（回复确认入库，默认）| auto（识别直接入库）；坏值读侧归一 reply
  "allowed_users": [],            // 微信ID白名单（扫码自动加入）
  "ilink": [ {"account_id":"","token":"","base_url":"","user":"","ledger":"","label":""} ]
}
```

保存设置时 `_bot_apply` 差量生效（off↔direct 启停轮询线程，不重启）；`confirm_mode` 每条消息现读配置，切换即生效、无需重启。`PUT /api/config` 只收 reply/auto 两个枚举值（仅 admin，与 mode/白名单同一权限）。链路联调走 `python bot_core.py` CLI（bypass_auth，直连大脑不经过通道）；设置页联调期的「测试对话」模块已随正式版移除。

### 10.6 多人多账本绑定（一账本一用户一机器人）

绑定模型：（账本, 用户）→ 一个 bot 实例，凭证以 user 为唯一键（同微信重扫顶替、旧实例差量停机防僵尸轮询）。成员用自己的微信扫某账本的码，微信出现专属 bot 联系人，消息记入该账本，确认响应后自动加入白名单，扫码本身就是授权。绑定谁的码，记账自动写 `tags.attribution=该用户`。例：大牛扫「日常」的码 → 消息进日常；小牛另扫「日常」→ 也进日常（家庭共账）；大牛再扫「旅行」→ 顶替旧绑定转进旅行。二维码一次一扫。

### 10.7 数据量与隐私

- 长轮询 ~1KB/次 × 3000 次/天 ≈ 月 <100MB；记账一次 LLM ≈ 2500 in + 400 out ≈ 0.3 分，家庭使用（~10 笔/天）月成本不到 1 元
- 隐私出网口径与 AI 页一致（一句话/一张图 + 清单 + 少样本 + 聚合），全程出站直连

***

## 十一、构建与部署

```
build.bat：读 manifest 版本 → 烘焙 static/version.txt → py_compile 自检 → 单元测试全绿
          → 清理 app/data 开发残留 → src/chaimi.conf（本地开发配置回落）打包期间移开
          → fnpack 打包 → chaimi-ledger.<版本>.fpk（打包后 conf 原样恢复，杜绝误分发）
NAS 安装：install_init 只体检（python3/pip 缺失即拒装），依赖统一由 cmd/main 的
          ensure_site 安装 ─▶ pip --target ${VAR_BASE}/site-packages（VAR_BASE=PKGVAR 优先，
          PKGHOME/PKGTMP//tmp 回落；镜像首源中科大，失败依次换下一个源）；python3 取应用中心运行时 bin
cmd/main：PYTHONPATH=${SITE} 直跑；启动时按 requirements md5 哨兵判定变更才重装依赖；
          进程判定 = PID 存活 + 核对身份（/proc cmdline 含本应用入口）。断电重启后残留 pid 被
          复用时，只验存活会让 status 误报运行中致应用起不来、stop 更会把信号发给无关进程。
          残留进程清理一道，在 uninstall_init：停应用依赖 pid 文件，升级中断丢 pid 文件后
          旧版服务进程会带着旧逻辑继续轮询微信（一条消息两份回复记两笔，旧代码不认新版
          的 bot_seen 幂等表，只能清进程断根）；卸载/升级都先跑 uninstall_init，按 cmdline
          含 chaimi-ledger/src/server.py（路径尾巴，任何安装根都命中）清残留进程，连带清
          pid 文件/启动锁/网关 socket，命中记日志。server 以运行时符号链接 + exec -a 启动，
          进程列表显示 chaimi-ledger 而非 python3（命令行保留 server.py 路径，身份判定不受
          影响）。应用不自建子进程、后台线程全为 daemon、
          不注册定时任务——除 server 本体外无任何可独立存活的东西
build.sh：fnOS/Linux 对应版，流程相同（含 app/data 与 __pycache__ 残留复查，脏树拒绝打包）
```

**备份**：SQLite 开 WAL 后直接拷 .db 会得到不一致快照，必须用 `sqlite3.Connection.backup()`（backup.py 实现）。备份 = db 副本 + chaimi.conf 副本 + 小票 zip 包。

自动备份分层：月末（12-31 除外）生成月备 `chaimi-ledger-YYYYMM.db`；12-31 优先生成年备（年备保留关闭时月备照常）；保留数按 keep_months/keep_years 清理，只清周期文件不动手动备份；失败下一分钟自动重试。

恢复走 backup API 原地写回同一 db 文件（旧连接立即可见，避免临时文件替换的 inode/句柄问题），前置 `integrity_check` 校验，conf 原子写，直连轮询按备份后的配置差量重建。小票图片存 `${TRIM_PKGVAR}/receipts/`，库内只存相对路径。

***

## 十二、关键设计决策

1. **SQLite 而非 PG**：年万条级数据量，无需配置、文件即库、备份=拷文件；安装只需 pip 依赖，卸载干净
2. **单页原生 JS**：交互复杂度中等，单 HTML + 原生 JS + Chart.js 足够，无前端构建链
3. **预览制导入**：解析不入库 → 用户确认 → 整批可撤销；低置信不阻断（打标事后处理），几百行账单不被几行待确认卡住
4. **退款统一口径**：原消费恒 expense、退款行恒 transfer、扣减算在原消费日期上。这样结果与导入顺序无关，统计口径也只有一套
5. **确认才入库（默认）**：AI 草稿（App/微信同语义）、对话式修订、删除两步确认。确认之前不影响统计，丢弃后不留痕迹。机器人可按家庭习惯切「识别直接入库」（v1.0.8，`bot.confirm_mode`）：省掉一步确认，记错了靠「撤销」「撤销N笔」补救，金额无效仍会拦下，疑似重复仍会提示；删除（含批量撤销）恒两步确认、不受该开关影响
6. **传输与大脑分离**：bot_core 通道无关，新通道可插；当前仅 A 通道直连，off↔direct 配置热切换（B 通道 v1.0.4 下线）
7. **导入上传安全**：大小/白名单/文件头/行数四重校验，防伪装与解压炸弹
8. **身份复用飞牛账号**：不自建账号体系，网关注入的请求头就是身份，账本级 ACL 隔离（v1.0.4 多用户）；「用户」（归属分组）是账本内的记账人维度
9. **界面布局仿飞牛官方应用**（v1.0.6 定三层几何，v1.0.8 重构承载方式）：

   几何：侧栏和窗口底色统一用窗口灰 `--win`（浅色 #F3F3F3，深色 #0C0C0D），菜单贴着窗口边缘，不做圆角。主内容区放进 1px `--border` 描边、`--radius-panel` 圆角的卡片，圆角 20px，不小于内层卡片的 18px，免得外圆角比内圆角还小。面板底色是 `--panel`（浅色白，深色 #1F2021）。深色的 `--card` 提亮到 #26272B，保持「窗口 → 面板 → 卡片」逐级变亮。手机端全屏，不套卡片。

   承载：所有 `.page.on` 在内部滚动（flex:1 + overflow），顶栏常驻，.main 本身不滚动。这样就不依赖 sticky：部分手机 WebView 里 sticky 会失效，搜索区会跟着内容滚走，卡片从缝里露出来。开发中用过的 sticky 加 `--gbar-h` 测高补偿（gbarFit / ResizeObserver）在发版前已全部删掉。

   - 顶栏在 .main（纵向 flex）里设为 sticky。流水页和 AI 页做法相同：顶栏 gbar（范围条、汇总、横线）布局不变，流水的筛选/搜索区是紧挨在它下面的固定行
   - 搜索：从上到下依次是范围选择器、汇总、横线、筛选按钮和搜索按钮。搜索按钮是「编辑」左边的 icon-btn，点了以后按钮隐藏，.tx-search 从按钮的位置向左展开：电脑上用 margin-left:auto 靠住操作按钮，宽 min(380px,42vw)；手机上（≤768px）盖住筛选按钮，占满整行。收起用同一个 ✕：有内容时清空，没内容时收起，靠 pointerdown 时的快照区分「本来就空」和「刚清空」；Esc 或点工具条外面也能收起；有关键词时按钮高亮
   - 上滑收起汇总：任意页面往上滑时，#gsum 的高度跟着当前页滚动容器的 scrollTop 1:1 收起，实现在 ledger.js 的 sumbarCollapseApply，在 document 上捕获 scroll 事件统一处理。各页的滚动容器：流水是 #txList，AI 是消息区或工作台，其余是 .page.on。#gsum 设 overflow:hidden 形成 BFC，子元素的 margin 不会漏出来。收到底后顶栏只剩范围条，滚回顶部再展开；切页时复位，进页时按保留的滚动位置对齐。翻页行是普通的 flex 行，卡片列表在 #txList 里滚动，设了 overscroll-behavior:contain
   - 页面列设 width:100%、max-width 1400，内边距用 clamp。width:100% 要显式写，因为在 flex 里交叉轴方向的 auto 外边距会让宽度退化成 fit-content。scrollbar-gutter:stable 防止切页时内容左右跳
   - 弹窗的 z-index 用一组分层变量，--z-confirm 永远在最上面，和 DOM 顺序无关。弹窗带 role=dialog，支持 Esc 关闭，焦点限制在弹窗内，键盘操作时显示 :focus-visible 焦点框
   - 流水卡是行式的：每天一张卡，行之间用 --line 细线分隔，悬停有底色，左右各留 12px。退款卡有两行：主行，加上原消费那一行。原消费名称最多显示 10 个字，悬停看全名；图标在整张卡里上下居中；小票、来源标签在金额下方，净额和退款标签跟金额列右对齐
   - 断点统一为 640 / 768 / 1020 / 1400

10. **主题三态跟随宿主（v1.0.6）**：浅色/深色/自动三态循环（默认自动、全端记忆），主题逻辑单独放在 `static/theme.js`（ESM，app/home 两页共用，全部由事件驱动，不用定时器）。自动模式优先跟随飞牛宿主，用的是官方 JS SDK `@trimjs/web-app`（内嵌 trim-sdk.js，manifest 声明 `micro_app=true`，需系统 ≥1.2.0401）：桌面 iframe 订阅 `$on('os/theme')` 实时事件，安卓/iOS App 载入读取 + 回前台/系统偏好变化时重读；没有 SDK 桥的环境（鸿蒙 ArkWeb、独立浏览器、旧系统）改用浏览器的 `prefers-color-scheme`。各页 `<body>` 起始内联脚本按上次解析明暗在首帧前应用（刷新/跳登录页不闪色）；明暗实际翻转才重建统计图表（dropCharts），配色集中定义在 themeColors

***

## 十三、版本里程碑

**1.0.9（2026-11-08）手机端界面小修**：入口一律先进选人页（GET / 对飞牛用户 302 到 home，新增 `/app` 直入路由），手机返回键逐层关闭弹层（app 页补上两处漏掉的浮层，home 页第一次加上返回键处理），共享弹窗与「分类来自」行排版修复。无数据迁移，详见 CHANGELOG.md。

**1.0.8（2026-11-08）微信机器人确认开关 + 批量撤销 + 全量代码审查**（1.0.6 后首个上架版，1.0.7 为未上架过渡版、内容并入本版）：机器人「记账确认」二选一（回复确认入库 / 识别直接入库，`bot.confirm_mode`）与「撤销N笔」批量撤销；全量代码审查修复 2 处安全问题（导入确认跨账本改写、流水来源存储型 XSS）与一批数据正确性/稳定性问题；分类 id 改由后端统一解析（ai._cat_ids）。测试 328 → 573。无数据迁移。详见 CHANGELOG.md。

**1.0.0（2026-10-18）首个正式版**（与 manifest / `CHANGELOG.md` 一致）：功能包括多账本记账、五平台账单导入（统一模板引擎 + 五级分类瀑布 + 四道去重防线 + 退款五层匹配链）、统计看板、AI 记账与查账、微信机器人双通道、分层备份；148 用例 + 双级黄金快照，通过才允许打包，多份真机账单端到端验证。定版之前的内部迭代不单独记录，演进明细见 git 历史；此后变更一律记 `CHANGELOG.md`。
