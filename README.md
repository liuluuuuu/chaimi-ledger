# 柴米记账（chaimi-ledger）

跑在飞牛 NAS（fnOS）上的家庭记账应用。一家人各有各的账本，也能一起记一本账；支付宝、微信、京东的账单可以导入自动分类，平时用 AI 记账，或者在微信里说一句话记账。

> 数据不出家门：账目全部存在 NAS 本机的 SQLite 单文件里，备份就是拷文件。

## 交流群

QQ 群：[108508834](https://qm.qq.com/q/3LSusMpHnO)。使用问题、功能建议、Bug 反馈、新版本尝鲜，都可以在群里聊。

## 功能

- 飞牛多用户：登录不同飞牛用户，各自一套账本，互相看不到；账本可以共享给其他飞牛用户，一本账多人记，账目互相可见，归属自动区分
- 多账本：日常、旅行、装修各记各的，成员和预算一起管
- 账单导入：支付宝、微信、京东、美团一键导入，自动分类、匹配退款、去重
- 统计：日历收支热力、分类饼图、趋势曲线、月度对比、人情往来
- AI 记账：一句话记一笔、小票拍照识别、对话查账（汇总、分类、日历、趋势、成员人均与 AA 结算、人情往来、账户、预算）、月度分析。填个 Key 就能用，支持百炼、智谱、DeepSeek 和本地 OpenAI 兼容模型
- 微信机器人：在微信里对话记账、查账，支持多人多账本（iLink Bot 扫码直连，一人一绑定）。记账确认有两种模式：默认回「确认」入库，也可以切成识别后直接入库，记错了回「撤销」或「撤销N笔」
- 明暗主题：浅色、深色、自动三态。自动模式跟随飞牛主题（桌面端实时生效，安卓/iOS 回到应用时生效，经官方 JS SDK）；没有飞牛环境时跟随系统设置。选择会被记住，刷新不闪色
- 多端同步：长轮询，任意端记账，其他端 1 秒内刷新
- 隐私：单文件 SQLite 存储，无云端上报；AI 识别只在你配置了 Key 之后调用对应服务

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.12 · FastAPI · uvicorn |
| 前端 | 单 HTML + 原生 JS + Chart.js 4（按需加载，无构建链） |
| 存储 | SQLite 单文件（`chaimi.db`） |
| 部署 | 飞牛 fnOS 原生应用（fpk 包），网关 `/app/chaimi-ledger` 反代 |

## 界面

| AI 记一笔 · 收支概览 | 日历收支热力 |
|:---:|:---:|
| ![AI 记一笔](screenshots/01-ai-home.png) | ![日历收支热力](screenshots/02-calendar-light.png) |
| 统计看板 | 流水与筛选 |
| ![统计看板](screenshots/03-stats-light.png) | ![流水与筛选](screenshots/04-list-light.png) |
| 账单导入预览 | 深色模式 |
| ![账单导入预览](screenshots/05-import-preview.png) | ![深色模式](screenshots/06-stats-dark.png) |

## 部署

### 部署前提（重要）

这个应用按家庭内网自托管设计：网页端没有应用层的登录鉴权，访问控制完全依赖飞牛 fnOS 网关和你的局域网。微信机器人是 NAS 主动往外连接（出站长轮询），不需要开放任何入站端口。

- x86_64 和 ARM（aarch64）的 NAS 都能装（`platform=all`，C 扩展依赖都有 aarch64 预编译 wheel）
- 适合：家庭 NAS、内网服务器、Tailscale/WireGuard 这类私有网络
- **不要把网关反代直接暴露到公网**，那样任何人都能读写全部账目
- 依赖版本已锁定（见 `app/src/requirements.txt`）；要升级依赖，先跑完测试再提交

### 可选：开启网页端 Basic Auth（v1.0.1 起）

想在局域网边界之外多加一道门，可以编辑配置文件 `chaimi.conf` 开启 Basic Auth。飞牛上这个文件在所选存储卷的 `@appconf/chaimi-ledger/` 下（安装时自选存储卷，不固定在系统分区）；本地开发在 `app/src/`。文件里已预置这个配置块（默认关闭），把 `enabled` 改成 `true`，填上用户名密码：

```json
"web_auth": {
  "enabled": true,
  "username": "你的用户名",
  "password": "你的密码"
}
```

保存即生效，不用重启（每个请求都会现读配置）。开启后浏览器第一次访问会弹出系统登录框，输一次就会记住；`/healthz` 探活不受影响。用户名或密码留空时一律拒绝（fail-closed），配置好才会放行。微信直连是出站轮询，不受影响。

### 飞牛 NAS（推荐）

1. 在飞牛应用中心安装本应用的 fpk 包（或从源码自行打包，见下）
2. 打开应用，设置一个用户名，就可以开始记账
3. 需要机器人或 AI 时，在「设置」页按向导配置

多用户（v1.0.4 起）：应用入口对全部飞牛用户可见，登录不同飞牛用户各自一套账本，互相看不到。在「设置 → 账本设置 → 共享」可以把账本共享给其他飞牛用户（对方需要先打开过一次应用），共享后同一本账多人记账、互相可见，记账时自选记账用户。**从旧版本升级后，请先用 NAS 管理员账号打开一次应用**，原有账本会自动归属到管理员名下，再按需共享给家人。AI Key、机器人、备份/恢复这些应用级设置由管理员统一管理。

### 进程与运维（v1.0.12 起）

- 应用只有一个服务进程，在任务管理器/`ps` 里显示为 **`chaimi-ledger`**（用户列同样是 `chaimi-ledger`，不再是没法区分的 `python3`），命令行里带完整 `src/server.py` 路径。
- 启停走应用中心即可；命令行也可以：`appcenter-cli start|stop chaimi-ledger`，或 `/var/apps/chaimi-ledger/cmd/main start|stop|status`。
- SSH 手动结束进程：`pkill -x chaimi-ledger`（精确按进程名，只杀本应用；别用 `pkill -f chaimi-ledger`，会误伤命令行里含该路径的其他进程）。默认 TERM 为优雅关闭；`kill -9` 也不会损坏数据（SQLite WAL），残留的 pid 文件/网关 socket 下次启动自动清理。

### 本地开发

```bash
cd chaimi-ledger-fn/app/src
pip install -r requirements.txt
python server.py --host 127.0.0.1 --port 18710
```

浏览器打开 <http://127.0.0.1:18710>。数据库会自动创建在 `app/data/chaimi.db`。

## 构建 fpk

```bat
build.bat        :: Windows（cmd）
./build.sh       :: fnOS / Linux
```

脚本会做语法自检、跑全量单元测试（失败就拒绝打包）、清理开发数据（`app/data/`）、调用 fnpack 打包，并按 manifest 里的版本号命名产物。`fnpack.exe` 是飞牛的专有打包工具，从飞牛开发者渠道获取后放在仓库根目录。

## 目录结构

```
chaimi-ledger/
├── README.md / LICENSE
├── build.bat / build.sh     # fpk 打包脚本（Windows / fnOS·Linux）
├── screenshots/             # 界面截图（README 演示，兼应用商店上架材料）
├── tests/                   # 单元测试与发版脚本
└── chaimi-ledger-fn/
    ├── manifest             # 飞牛应用清单
    ├── cmd/                 # 飞牛生命周期脚本（安装/升级/启停回调）
    ├── config/              # 权限与资源声明
    ├── wizard/              # 卸载向导
    └── app/
        ├── src/             # 后端：server / db / ai / import_bill / bot_* 等
        ├── static/          # 前端：app.html / home.html / *.js / Chart.js
        └── server/          # 应用图标等静态资源
```

## 开源引用

本项目直接打包分发的前端库（许可声明保留在文件内）：

| 组件 | 版本 | 许可证 | 说明 |
|---|---|---|---|
| [Chart.js](https://www.chartjs.org) | 4.4.3 | MIT | 统计图表，`app/static/chart.umd.min.js` |

运行时通过 pip 安装的依赖（见 `app/src/requirements.txt`）：

| 库 | 许可证 | 用途 |
|---|---|---|
| fastapi | MIT | Web 框架 |
| uvicorn | BSD-3-Clause | ASGI 服务器 |
| openai | Apache-2.0 | LLM / 视觉模型客户端（OpenAI 兼容协议） |
| openpyxl | MIT | 微信 xlsx 账单解析 |
| python-multipart | Apache-2.0 | 文件上传 |
| Pillow | MIT-CMU (HPND) | 小票图片裁剪压缩 |
| segno | BSD-3-Clause | 机器人扫码二维码 |
| pycryptodome | BSD-2-Clause / 公有领域双许可 | 凭证加密存储 |

## 许可证

本项目以 [MIT License](LICENSE) 开源，© 2026 liulu。
