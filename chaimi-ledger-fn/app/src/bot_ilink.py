"""通道 A：iLink Bot API 客户端（DESIGN 10.3）。

协议来源：wechatbot.dev/zh/protocol 与 corespeed-io/wechatbot docs/protocol.md（已逐字核对，
所有业务端点带 /ilink/bot 前缀）：
  登录  POST /ilink/bot/get_bot_qrcode        body {local_token_list:[旧token...]}（同身份续登，
        已绑定的 bot 服务端直接回 binded_redirect 复用本地凭证）
        GET  /ilink/bot/get_qrcode_status?qrcode=...   头 iLink-App-ClientVersion: 1
        status: wait/scaned/confirmed/expired/scaned_but_redirect(切 redirect_host 节点)/
                binded_redirect(复用本地)/need_verifycode/verify_code_blocked
        confirmed → bot_token / baseurl / ilink_bot_id（baseurl 必须用返回值）
  收    POST /ilink/bot/getupdates   挂起 ~35s；请求体 {get_updates_buf, base_info}，游标原样回传
  发    POST /ilink/bot/sendmessage  msg.from/to_user_id + item_list[].text_item.text(type 1=TEXT)
        + 回传 context_token；message_type/state 2=BOT/FINISH
  在线  POST /ilink/bot/msg/notifystart|notifystop   轮询起停时上报（non-fatal）
  认证  AuthorizationType: ilink_bot_token + Authorization: Bearer <bot_token>
        + 每请求随机 X-WECHAT-UIN；body 统一带 base_info.channel_version=2.0.0
  过期  ret/errcode = -14 → 会话过期，停轮询等重新扫码（无自动续期）

msgs 元素字段协议文档未完全枚举——_extract_msg() 按「文档 + SDK 示例」多路径容错抽取，
真机实测若有出入只改这一个函数（设计稿风险⑤的适配层落点）。
"""
from __future__ import annotations

import base64
import applog

_log = applog.get("bot-ilink")
import config
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import bot_core
import db as dbm

DEFAULT_BASE = os.environ.get("CHAIMI_ILINK_BASE", "https://ilinkai.weixin.qq.com")
CDN_BASE = os.environ.get("CHAIMI_ILINK_CDN", "https://novac2c.cdn.weixin.qq.com/c2c")
API_PREFIX = "/ilink/bot"
CHANNEL_VERSION = "2.0.0"
POLL_TIMEOUT = 45              # 长轮询挂起 ~35s，客户端超时留余量
QR_WAIT_TIMEOUT = 180          # 二维码有效期（秒）
QR_POLL_INTERVAL = 2
MAX_MEDIA_BYTES = 20 * 1024 * 1024   # 媒体下载体积上限 20MB（read 多读 1 字节判超限，内存有界）；
                                     # 与 server.MAX_UPLOAD（导入/小票上传）语义无关，各自独立调整
_BACKOFF_MAX = 60
_STOP_WAIT = 3                 # stop() 等在途协议动作（上线通知/逐条处理消息）收尾的上限（秒）


def _api(base: str, path: str) -> str:
    return f"{base.rstrip('/')}{API_PREFIX}/{path.lstrip('/')}"


def _uin_header() -> str:
    """X-WECHAT-UIN：随机 uint32 → 十进制字符串 → base64（每请求重新生成）。"""
    n = int.from_bytes(os.urandom(4), "little")
    return base64.b64encode(str(n).encode()).decode()


def _headers(token: str, extra: dict | None = None) -> dict:
    h = {"Content-Type": "application/json",
         "AuthorizationType": "ilink_bot_token",
         "X-WECHAT-UIN": _uin_header()}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if extra:
        h.update(extra)
    return h


def _request(method: str, url: str, token: str, payload: dict | None = None,
             timeout: int = POLL_TIMEOUT, extra_headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=_headers(token, extra_headers))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    if not body.strip():
        return {}
    r = json.loads(body)
    # 网关错误页可能返回 JSON 数组/字符串，下游一律 r.get(...)：非 dict 统一按空响应处理
    return r if isinstance(r, dict) else {}


def _expired(r: dict) -> bool:
    return r.get("errcode") == -14 or r.get("ret") == -14


def _pick(r: dict, data: dict, key: str, default=None):
    """响应字段取值（顶层 / data 嵌套两种包裹形态，顶层优先）：此前同一条兼容
    规则以 or 链 / default 参两种写法散在 status、ret、buf、msgs 等 7 处，语义
    还不一致（ret=0 是真值不是缺失）。此处按「值非 None 即用」统一，两处都缺给 default。"""
    v = r.get(key)
    if v is None:
        v = data.get(key)
    return default if v is None else v


def _wx_host_ok(url: str) -> bool:
    """微信域白名单（媒体下载首跳与 3xx 逐跳共用）；畸形 URL 一律不放行。"""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "weixin.qq.com" or host.endswith(".weixin.qq.com")


class _MediaRedirectHandler(urllib.request.HTTPRedirectHandler):
    """媒体下载重定向闸：3xx 每一跳的目标域名仍须在白名单内——
    白名单域上的开放重定向可 30x 跳内网（169.254.169.254 等），仅首跳校验挡不住。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _wx_host_ok(newurl):
            raise urllib.error.URLError(f"重定向目标不在微信白名单: {newurl[:80]}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# 凭证类字段子串：扫码/轮询/入站消息原文入日志前递归掩掉（bot_token/context_token/aeskey 等）
_REDACT_HINTS = ("token", "authorization", "aeskey", "aes_key", "filekey", "encrypt", "password", "secret")

def _redact(o):
    """日志脱敏：递归复制并掩掉键名含凭证线索的字段（值→***）。
    扫码 confirmed 响应（bot_token）、getupdates 响应（context_token）、
    媒体字段（aeskey/下载参数）都可能出现在联调原文日志里，落盘即泄漏。"""
    if isinstance(o, dict):
        return {k: ("***" if any(h in str(k).lower() for h in _REDACT_HINTS) else _redact(v))
                for k, v in o.items()}
    if isinstance(o, list):
        return [_redact(x) for x in o]
    return o


# ---------- 媒体(小票图片):检测 + 下载 + AES-128-ECB 解密 ----------
# 真机结构已校准(2026-10-08 日志):图片消息 item_list[].image_item 内嵌
#   aeskey: 32位hex(密钥形态③) + media.encrypt_query_param: 下载参数(注意无 d,
#   协议文档写的是 encrypted_query_param——真机字段少了 d,线上参数名仍用文档拼写)。
# 检测仍用广谱线索递归展平,新增字段形态自动兼容。

_IMG_KEY_HINTS = ("aeskey", "aes_key", "filekey", "cdnurl", "cdn_url", "file_url",
                  "download_url", "encrypt", "cdnmedia", "imageurl", "img_url")


def _extract_image(m) -> dict | None:
    """递归展平消息树,收集键名含媒体线索的叶子字段 {字段名: 字符串值}。
    命中线索但值是 dict/list 时继续下钻(真机的 image_item.media.encrypt_query_param 即此形态)。"""
    found: dict = {}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                lk = str(k).lower()
                if any(h in lk for h in _IMG_KEY_HINTS):
                    if isinstance(v, (str, int, float)) and str(v):
                        found[str(k)] = str(v)
                    else:
                        walk(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(m)
    return found or None


def _decode_aes_key(s: str) -> bytes | None:
    """密钥三种编码形态(协议 docs/protocol.md):① base64(16字节)
    ② base64(hex字符串) ③ 32位hex 原串。逐一试解,16 字节才算数。"""
    s = (s or "").strip()
    try:
        b = base64.b64decode(s, validate=True)
        if len(b) == 16:
            return b
        h = b.decode("ascii", "ignore").strip()
        if len(h) == 32:
            try:
                return bytes.fromhex(h)
            except ValueError:
                pass
    except Exception:
        pass
    if len(s) == 32:
        try:
            return bytes.fromhex(s)
        except ValueError:
            pass
    return None


def _aes_key(info: dict) -> bytes | None:
    for k, v in info.items():
        if "aes" in str(k).lower():
            b = _decode_aes_key(str(v))
            if b:
                return b
    return None


def _aes_decrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB + PKCS7 解密(pycryptodome);解不开时原样返回,由上层按明文兜底。"""
    try:
        from Crypto.Cipher import AES
        plain = AES.new(key, AES.MODE_ECB).decrypt(data)
        pad = plain[-1] if plain else 0
        if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
            plain = plain[:-pad]
        return plain
    except (ImportError, ValueError):
        # ImportError=缺依赖；ValueError=密文长度非 16 字节倍数等形态异常(字段带 aeskey 线索
        # 但媒体字节实为明文/截断)——两种都按「解不开」原样返回,由上层 is_image 魔数闸兜底。
        # 此前只捕 ImportError,ValueError 会沿 _fetch_media 逃逸,同批后续消息全部不处理不回复
        _log.info("媒体 AES 解密不可用(缺依赖或密文形态异常),按明文兜底")
        return data


def _extract_msg(m) -> dict | None:
    """入站消息 → {user_id, text, context_token, msg_id}；非消息/无来源返回 None。
    多路径容错：协议字段(from_user_id/client_id/item_list) + SDK 字段(user_id/text)。
    message_type（1=用户 / 2=机器人，见 _send_item）：getupdates 会把机器人自己发出的
    回复也回投回来，显式非 1 的不当用户消息——否则回声会当新指令进 bot_core。"""
    if not isinstance(m, dict):
        return None

    def g(*keys, default=None):
        for k in keys:
            v = m.get(k)
            if v not in (None, ""):
                return v
        return default

    mt = m.get("message_type")
    if mt is not None and str(mt) != "1":   # 1=用户：数字/数字串两种形态都放行
        return None
    user = g("from_user_id", "from_userid", "user_id", "ilink_user_id")
    if not user:
        return None
    text = ""
    items = m.get("item_list") or m.get("items") or []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                ti = it.get("text_item") or {}
                if isinstance(ti, dict) and ti.get("text"):
                    text += str(ti["text"])
    if not text:
        text = g("text", "content", default="") or ""
    # 消息身份取 message_id：长轮询 at-least-once，响应丢失后服务端重投同一条（message_id
    # 不变，cli-in-wechat 同协议实现同口径）。旧代码只认 client_id/msg_id/msgid 三个键名，
    # 真机字段 message_id 从没被读到，落空后兜底指纹掺接收秒，跨秒重投被判成新消息：
    # 一笔记两次、回复两次
    mid = g("message_id", "client_id", "msg_id", "msgid")
    # 无 id 平台的兜底指纹掺接收秒：纯内容前缀会把用户连发的两条相同短消息（如两条
    # 「确认」）误吞一条；掺秒后同秒重投仍去重，跨秒的合法重发不误吞
    return {"user_id": str(user), "text": str(text).strip(),
            "context_token": g("context_token", "contextToken"),
            "msg_id": str(mid) if mid is not None else
            f"#{int(time.time())}:{json.dumps(m, ensure_ascii=False, default=str)[:120]}"}


class ILinkBot:
    """A 通道管理器（仿 AutoBackup 生命周期）：start/stop + 登录流程 + 轮询线程。

    状态机：off → pending_scan → scaned → logged_in ⇄ error →(可) expired
    expired 后线程退出，设置页亮「凭证过期」，重新扫码即恢复。
    """

    def __init__(self, ledger: int | None = None, label: str = "", manager=None):
        self._ledger = ledger        # 绑定的账本(多人记账:扫哪个账本的码记哪本)
        self._label = label          # 成员备注
        self._manager = manager      # 所属注册表（start 时自收编，见 start 注释；测试直建为 None）
        self._lock = threading.Lock()
        self._busy = threading.Lock()   # 轮询线程做协议动作时持有：stop() 只等它，不等长轮询
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state = {"state": "off", "detail": ""}
        self._last_qr_img = ""    # 最近一次二维码图片内容(URL/相对路径/base64,供 qrimg 代理)
        self._token = ""          # bot_token
        self._local_token = ""    # 扫码时上送的本地旧 token（binded_redirect 复用）
        self._base = DEFAULT_BASE
        self._bot_id = ""         # ilink_bot_id（sendmessage 的 from_user_id）
        self._ctx: dict[str, str] = {}                    # user_id → 最新 context_token
        self._on_confirmed = None                          # creds → None（server 侧存配置并 start）

    # ---------- 状态 ----------

    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def qr_img(self) -> str:
        """最近一次二维码图片内容（URL/相对路径/base64 三态），供 server 代理端点取图。"""
        return self._last_qr_img

    def _set_state(self, s: str, detail: str = "") -> None:
        detail = str(detail)[:200]
        with self._lock:
            prev = self._state
            self._state = {"state": s, "detail": detail}
        # 状态未变的例行刷新（长轮询每 ~18s 一跳 logged_in）降 DEBUG（applog INFO 级即静默）；
        # 状态迁移（error→logged_in 恢复、凭证过期等）仍 INFO。在线状态查询走 /api/bot/ilink/status，不靠日志。
        if prev.get("state") == s and prev.get("detail") == detail:
            _log.debug(f"{s} {detail}"[:220])
        else:
            _log.info(f"{s} {detail}"[:220])

    # ---------- 登录 ----------

    def begin_login(self, existing_token: str, on_confirmed) -> dict:
        """发起扫码：取二维码，后台线程轮询状态直到 confirmed/expired。
        existing_token 随 local_token_list 上送(同身份续登);多人记账每次绑定都是
        新扫码(新 bot 身份),一般传空。on_confirmed(bot, creds) 携带实例自身回调
        (server 侧:存 chaimi.conf → manager.adopt(bot))。"""
        self._on_confirmed = on_confirmed
        self._local_token = existing_token or ""
        # 真机实测口径：POST /ilink/bot/get_bot_qrcode?bot_type=3（bot_type 是查询参数），
        # local_token_list 走 body；缺 bot_type 服务端回 {"ret":1,"err_msg":"missing bot_type"}
        r = _request("POST", _api(DEFAULT_BASE, "get_bot_qrcode?bot_type=3"),
                     self._local_token,
                     {"local_token_list": [self._local_token] if self._local_token else [],
                      "base_info": {"channel_version": CHANNEL_VERSION}})
        qr = r.get("qrcode") or ""
        if not qr:
            raise RuntimeError(f"获取二维码失败: {json.dumps(r, ensure_ascii=False)[:160]}")
        img = r.get("qrcode_img_content") or ""
        self._last_qr_img = img
        # 只记长度：qrcode 是扫码会话凭证、img 是待编码的绑定跳转 URL（二维码即绑定凭证），
        # 原样入日志后，有效期内拿到日志（排障外发/日志包下载）的人可抢先完成绑定
        _log.info(f"二维码已获取（qrcode {len(qr)} 字符，img {len(img)} 字符）")
        self._set_state("pending_scan", "等待扫码")
        t = threading.Thread(target=self._login_waiter, args=(qr,), daemon=True,
                             name="ilink-login")
        t.start()
        return {"qrcode": qr, "img": img, "state": self.state()["state"]}

    def _login_stage(self, st: str, r: dict, data: dict, host: str) -> tuple[str, bool]:
        """扫码过程态（scaned 族）展示与节点切换。返回 (host, done)：done=True 为终态
        （verify_code_blocked），调用方结束等待；host 可能被 redirect_host 更新。"""
        if st == "scaned":
            self._set_state("scaned", "已扫码，请在手机上确认")
        elif st == "scaned_but_redirect":
            rh = str(_pick(r, data, "redirect_host") or "")
            if rh:
                host = rh
                self._set_state("scaned", "已扫码，切换节点确认中")
        elif st == "need_verifycode":
            self._set_state("scaned", "需在手机上输入配对码验证")
        elif st == "verify_code_blocked":
            self._set_state("expired", "配对码错误次数过多，请重新获取二维码")
            return host, True
        return host, False

    def _login_confirmed(self, d: dict) -> None:
        """confirmed 终态：凭证装配 + 通道自启 + on_confirmed 回调（半路取消也交回调落凭证，
        切回 direct 后按凭证自动拉起，免重扫）。"""
        creds = {"bot_token": d.get("bot_token") or "",
                 "baseurl": d.get("baseurl") or DEFAULT_BASE,
                 "ilink_bot_id": d.get("ilink_bot_id") or "",
                 "ilink_user_id": str(d.get("ilink_user_id") or "")}   # 扫码人=成员
        if not creds["bot_token"]:
            self._set_state("error", "confirmed 但缺 bot_token")
            return
        if self._stop.is_set():
            # 扫码半路被取消（模式切走→stop_all / 新扫码顶替 / 停机）不自启轮询
            # （server 出口闸拒 adopt 后即无人收编）
            _log.info("扫码确认时登录已被取消，不自启轮询")
        else:
            self._ctx.clear()
            self.start(creds)
        if self._on_confirmed:
            try:
                self._on_confirmed(self, creds)
            except Exception as e:   # 回调失败不影响通道自启
                _log.warning(f"on_confirmed 回调异常: {e}")

    def _login_waiter(self, qr: str) -> None:
        host = DEFAULT_BASE
        deadline = time.time() + QR_WAIT_TIMEOUT
        last = ""
        logged_raw = False      # 首次响应原文入日志(真机联调定位响应形态)
        err_logged = False
        while time.time() < deadline and not self._stop.is_set():
            url = _api(host, f"get_qrcode_status?qrcode={qr}")
            try:
                r = _request("GET", url, self._local_token, timeout=10,
                             extra_headers={"iLink-App-ClientVersion": "1"})
            except Exception as e:   # HTTPError（带状态码与响应体）与其余网络/解析异常：首错记日志 + 同款退避
                if not err_logged:
                    if isinstance(e, urllib.error.HTTPError):
                        try:
                            body = e.read().decode()[:200]
                        except Exception:
                            body = ""
                        _log.warning(f"get_qrcode_status HTTP {e.code}: {body}")
                    else:
                        _log.warning(f"get_qrcode_status 异常: {type(e).__name__}: {e}")
                    err_logged = True
                time.sleep(QR_POLL_INTERVAL)
                continue
            if not logged_raw:
                _log.info(f"qrcode_status 首次响应: {json.dumps(_redact(r), ensure_ascii=False)[:400]}")
                logged_raw = True
            # 状态字段兼容顶层与 data 嵌套两种形态(微信 API 常见包裹，取值收口 _pick)
            data = r.get("data") if isinstance(r.get("data"), dict) else {}
            st = str(_pick(r, data, "status") or "")
            if st != last:
                host, done = self._login_stage(st, r, data, host)
                if done:
                    return
                last = st
            if st == "confirmed":
                self._login_confirmed(data or r)
                return
            if st == "binded_redirect":
                # 扫的 bot 已绑定本客户端：直接复用本地 token（无需重新确认）
                if self._stop.is_set():
                    return
                if self._local_token:
                    self._ctx.clear()
                    self.start({"bot_token": self._local_token,
                                "baseurl": str(_pick(r, data, "baseurl") or DEFAULT_BASE),
                                "ilink_bot_id": str(_pick(r, data, "ilink_bot_id") or "")})
                    return
                self._set_state("expired", "binded_redirect 但本地无凭证，请重新扫码")
                return
            if st == "expired":
                self._set_state("expired", "二维码已过期，请重新获取")
                return
            time.sleep(QR_POLL_INTERVAL)
        if not self._stop.is_set():
            self._set_state("expired", "扫码超时，请重试")

    # ---------- 轮询主循环 ----------

    def start(self, creds: dict) -> bool:
        """用已有凭证启动轮询线程（热切换/重启恢复共用）。creds 一律平台 confirmed
        响应字段名（bot_token/baseurl/ilink_bot_id）——conf 条目（token/base_url/
        account_id）由 apply 翻译后再进本接口，两套键名不在此处互认。无 token 返回 False。"""
        token = (creds or {}).get("bot_token") or ""
        if not token:
            self._set_state("off", "无凭证，请扫码登录")
            return False
        # 同实例重启（换凭证）：先停旧轮询；仍挂在长轮询里的旧线程由 _poll_loop 的
        # 线程身份闸兜底退出（_dead 见下），不会与新轮询并存处理消息
        self.stop()
        self._token = token
        self._base = ((creds.get("baseurl") or DEFAULT_BASE)
                      .rstrip("/")) or DEFAULT_BASE
        self._bot_id = str(creds.get("ilink_bot_id") or "")
        # 事件对象 __init__ 只建一次：这里只 clear 不替换——替换会让仍挂在长轮询里的
        # 旧循环从此读到未 set 的新事件，stop 的停止信号对它永久失效
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="ilink-poll")
        self._thread.start()
        self._set_state("logged_in", f"base={self._base} bot={self._bot_id[:12]}")
        if self._manager is not None:
            # start 即收编（单一收口）：任何自启路径——扫码 confirmed（_stop 检查与 start
            # 之间存在毫秒级竞态窗口）、binded_redirect 复用本地 token、未来的调用点——
            # 只要轮询起来了就登记进注册表，状态页可见、stop_all/apply 可停。旧版只在
            # server 回调里按模式条件 adopt，竞态下产生脱离管理的僵尸轮询实例（mode=off
            # 仍继续收消息写库，直到进程重启）。
            self._manager.adopt(self)
        return True

    def stop(self) -> None:
        """停轮询：不等长轮询返回（可挂 ~35s；请求返回后 _poll_loop 的停止检查即退出，
        不处理/回复任何消息），只等持 _busy 的在途协议动作收尾，至多 _STOP_WAIT 秒。"""
        if self._thread is None:
            return
        self._stop.set()
        self._thread = None
        if self._busy.acquire(timeout=_STOP_WAIT):
            self._busy.release()
        self._notify("notifystop")
        self._set_state("off")

    def _notify(self, path: str) -> None:
        """在线状态上报（notifystart/stop）：协议标明 non-fatal，失败只记日志。"""
        try:
            _request("POST", _api(self._base, f"msg/{path}"), self._token,
                     {"base_info": {"channel_version": CHANNEL_VERSION}}, timeout=5)
        except Exception as e:
            _log.warning(f"{path} 失败(忽略): {e}")

    def _poll_loop(self) -> None:
        me = threading.current_thread()
        buf = ""
        backoff = 1

        def _backoff() -> None:
            nonlocal backoff
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

        def _dead() -> bool:
            """本循环已被告知退出：停止事件已 set，或线程已被 stop()/重启替换/置空。"""
            return self._stop.is_set() or self._thread is not me

        with self._busy:   # 与 stop() 的下线通知不交错：已停则不再上报上线
            if _dead():
                return
            self._notify("notifystart")
        logged_raw = False
        while not _dead():
            try:
                r = _request("POST", _api(self._base, "getupdates"), self._token,
                             {"get_updates_buf": buf,
                              "base_info": {"channel_version": CHANNEL_VERSION}})
                if _dead():
                    # stop() 不等长轮询返回：请求返回后先查停止态，
                    # 已停（或本线程已被顶替）则不处理/回复任何消息，直接退出
                    return
                if not logged_raw:
                    _log.info(f"getupdates 首次响应: {json.dumps(_redact(r), ensure_ascii=False)[:400]}")
                    logged_raw = True
                if _expired(r):
                    self._set_state("expired", "会话过期，请重新扫码")
                    return
                if not r:
                    # 空体/非对象应答（_request 归一为 {}）：合法的长轮询超时也要先挂满 hold 才回，
                    # 立即返回的必是网关错误页类异常——此前按成功处理、零间隔重发成热循环
                    # （请求风暴 + CPU 空转）。固定 1s 再发；不置 logged_in，异常不被粉饰成在线
                    time.sleep(1)
                    continue
                data = r.get("data") if isinstance(r.get("data"), dict) else {}
                ret = _pick(r, data, "ret")
                if ret not in (0, None):
                    self._set_state("error", f"getupdates ret={ret}")
                    _backoff()
                    continue
                backoff = 1
                buf = _pick(r, data, "get_updates_buf") or buf
                self._set_state("logged_in")
                for m in (_pick(r, data, "msgs") or []):
                    with self._busy:   # 可能写库，stop() 会等它做完；锁内再查停止态
                        if _dead():
                            return
                        self._dispatch(m)
            except Exception as e:
                if _dead():
                    return
                self._set_state("error", f"{type(e).__name__}: {e}")
                _backoff()

    def _dispatch(self, m) -> None:
        """单条入站消息：库内幂等去重（_seen_once）→ bot_core（白名单也在里面）→
        回传 context_token 发回复。每条消息与回复都留 INFO 日志——此前收发全程静默，
        重复记账排查时日志包里什么证据都没有。"""
        msg = _extract_msg(m)
        if not msg:
            return
        if not self._seen_once(msg["user_id"], msg["msg_id"]):
            return
        # 兜底指纹(#开头)=消息没带任何平台 id：把原始键名记下来，真机形态一目了然
        fb = (f" 无平台id(键:{','.join(sorted(str(k) for k in m))[:120]}"
              if msg["msg_id"].startswith("#") else "")
        _log.info(f"收到消息 bot={self._bot_id[:12]} user={msg['user_id'][:16]} "
                  f"id={msg['msg_id'][:40]}{fb} text={msg['text'][:30]!r}")
        if msg["context_token"]:
            self._ctx[msg["user_id"]] = msg["context_token"]
        if not msg["text"]:
            # 媒体消息(小票图片):下载解密落盘 → bot_core 小票链路
            img = _extract_image(m)
            if img is None:
                _log.info(f"非文本且无媒体线索(原文留档): "
                          f"{json.dumps(_redact(m), ensure_ascii=False, default=str)[:400]}")
                return
            path = self._fetch_media(img, m)
            if path:
                self._respond(msg, "", image_path=path)
            else:
                self._reply_raw(msg, "⚠️ 未收到有效图片（仅支持 JPG/PNG 小票照片），请重发图片")
            return
        self._respond(msg, msg["text"])

    def _seen_once(self, user: str, msg_id: str) -> bool:
        """去重唯一一道：bot_seen 库内幂等占位（表口径见 db.py）——长轮询 at-least-once
        的重投、跨重启重放、旧进程未退净的双轮询，同键只有一个插入成功。True=首次可
        处理；表读写异常不拦消息（可用性优先，接受该窗口内重投重复的小风险）。"""
        try:
            with dbm.connect() as c:
                cur = c.execute(
                    "INSERT OR IGNORE INTO bot_seen(account_id, user_id, msg_id) VALUES(?,?,?)",
                    (self._bot_id, user, msg_id))
                if cur.rowcount == 0:
                    _log.info(f"跳过重复消息(库中已处理) bot={self._bot_id[:12]} "
                              f"user={user[:16]} id={msg_id[:40]}")
                    return False
                c.execute("DELETE FROM bot_seen WHERE seen_at < datetime('now','-7 days')")
            return True
        except Exception as e:
            _log.warning(f"消息幂等表读写失败(不拦消息): {type(e).__name__}: {e}")
            return True

    def _respond(self, msg: dict, text: str, image_path: str | None = None) -> None:
        """handle + 回发（文本/图片消息共用的收尾）：异常兜底后仍尽力回复。
        图片仅用于收小票。"""
        try:
            reply = bot_core.handle(text, msg["user_id"], image_path=image_path,
                                    ledger_id=self._ledger, attribution=self._label)
        except Exception as e:
            kind = "图片" if image_path else "消息"
            _log.warning(f"handle 异常({kind}): {e}")
            reply = f"⚠️ {kind}处理出错，请稍后再试。"
        if reply:
            _log.info(f"回复消息 user={msg['user_id'][:16]} {reply[:60]!r}")
            self._reply_raw(msg, reply)

    def _reply_raw(self, msg: dict, text: str) -> None:
        """不经 bot 大脑的直接回发（媒体拒收提示等运维消息）。"""
        try:
            self.send_text(msg["user_id"], text, msg["context_token"])
        except Exception as e:
            _log.warning(f"发送失败({msg['user_id']}): {e}")

    def _media_url(self, info: dict) -> str:
        """从展平的图片线索字段拼下载地址:完整 URL 限微信 CDN 域（入站消息字段对本服务
        是不可信输入,放行任意 http(s) 会被借来盲打内网 SSRF——只放行 *.weixin.qq.com）;
        encrypt(_ed)_query_param / filekey 拼到 CDN download 端点（参数值 URL 编码,防 & 夹带）。
        真机口径:字段名 encrypt_query_param(无 d),线上参数名用文档拼写 encrypted_query_param。"""
        for v in info.values():
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                if _wx_host_ok(v):
                    return v
                _log.warning(f"媒体 URL 域名不在微信白名单,已拒收: {v[:120]}")
        for hint, param in (("encrypt", "encrypted_query_param"), ("filekey", "filekey")):
            for k, v in info.items():
                if hint in str(k).lower() and isinstance(v, str) and v:
                    return f"{CDN_BASE}/download?{param}={urllib.parse.quote(v, safe='')}"
        return ""

    def _fetch_media(self, info: dict, raw) -> str | None:
        """CDN 下载 + AES 解密 → receipts 目录;返回文件路径。字段结构真机校准用日志齐全。"""
        url = self._media_url(info)
        if not url:
            _log.warning(f"图片消息缺下载地址,字段: "
                  f"{json.dumps(_redact(info), ensure_ascii=False, default=str)[:300]} 原文: "
                  f"{json.dumps(_redact(raw), ensure_ascii=False, default=str)[:400]}")
            return None
        _log.info(f"媒体下载: {url.split('?', 1)[0]}")   # query 含下载凭证，不入日志（与 _redact 同口径）
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MicroMessengerClient"})
            # 最多读 20MB+1 字节：多出的 1 字节用于超限判定；下载发生在白名单校验之前，
            # 任何能给机器人发消息的人都可诱导下载，无上限会整读任意大文件进内存
            with urllib.request.build_opener(_MediaRedirectHandler).open(req, timeout=30) as r:
                data = r.read(MAX_MEDIA_BYTES + 1)
        except Exception as e:
            _log.warning(f"CDN 下载失败: {type(e).__name__}: {e}")
            return None
        if len(data) > MAX_MEDIA_BYTES:
            _log.warning(f"媒体超过 {MAX_MEDIA_BYTES // (1024 * 1024)}MB 上限,已拒收: {len(data)}B")
            return None
        key = _aes_key(info)
        if key:
            data = _aes_decrypt(data, key)
        if not bot_core.is_image(data):   # 微信直连媒体同款魔数闸：非图片字节（视频/文件等）不落盘
            _log.info(f"媒体内容非图片,已拒收: {len(data)}B aes={'y' if key else 'n'}")
            return None
        # 落盘段同罩异常：磁盘满/目录只读时 write_bytes 抛 OSError 会沿 _poll_loop 的
        # 外层 except 走 backoff——该条消息静默丢失且不回复（下载失败有「未收到有效图片」
        # 回复，写盘失败反而没有）。失败按拒收口径返回 None，用户可收到提示后重发。
        try:
            d = config.receipts_dir()
            d.mkdir(parents=True, exist_ok=True)
            fname = f"ilink_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}.jpg"
            p = d / fname
            p.write_bytes(data)
            orig_size = len(data)
            bot_core.process_receipt(p)   # 裁剪+压缩（失败保留原图，不阻断）
        except OSError as e:
            _log.warning(f"媒体落盘失败(拒收): {type(e).__name__}: {e}")
            return None
        _log.info(f"媒体已落盘: {fname} {orig_size}B→{p.stat().st_size}B aes={'y' if key else 'n'} "
              f"字段={list(info.keys())[:8]}")
        return str(p)

    def _send_item(self, user_id: str, item: dict, context_token: str | None) -> dict:
        """sendmessage 公共信封（文本消息当前唯一形态）。
        context_token 优先用本条入站带的，其次该用户最新缓存（协议要求每条回传）。"""
        payload = {"msg": {
            "from_user_id": self._bot_id,
            "to_user_id": user_id,
            "client_id": uuid.uuid4().hex,
            "message_type": 2,           # 文档示例值 2 = BOT
            "message_state": 2,          # 文档示例值 2 = FINISH
            "context_token": context_token or self._ctx.get(user_id) or "",
            "item_list": [item],
        }, "base_info": {"channel_version": CHANNEL_VERSION}}
        return _request("POST", _api(self._base, "sendmessage"), self._token, payload)

    def send_text(self, user_id: str, text: str, context_token: str | None = None) -> dict:
        """发文本（type 1 = TEXT，协议明确）。"""
        return self._send_item(user_id, {"type": 1, "text_item": {"text": text}},
                               context_token)

# ---------- 多实例管理(多人记账:一账本一用户一机器人,DESIGN 10.6) ----------

def ilink_entries(cfg: dict) -> list:
    """bot.ilink 凭证列表：config 读侧已把旧单 dict 归一为列表（config._norm_ilink），
    此处只滤无 token 条目——直构 cfg 的调用方（测试/手工构造）绕过读侧时同样
    不得成为"幽灵绑定"。"""
    return [e for e in (cfg.get("bot") or {}).get("ilink") or []
            if isinstance(e, dict) and e.get("token")]


def _stop_concurrently(bots: list) -> None:
    """并发 stop（apply/stop_all 共用）：在途等待与下线通知往返重叠，耗时不随绑定数累加。"""
    ts = [threading.Thread(target=b.stop, daemon=True, name="ilink-stop") for b in bots]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


class ILinkManager:
    """凭证注册表:account_id → 运行实例;apply 按配置差量启停;
    扫码登录走独立 pending 实例,start() 即自收编（见 ILinkBot.start），
    server 回调按当前模式对账（apply/stop_all）。"""

    def __init__(self):
        # RLock：bot.start()（apply 持锁内调用）会经 _manager.adopt(self) 重入本锁——
        # 同线程重入安全；跨调用方（begin_login/stop_all/states）的串行语义不变
        self._lock = threading.RLock()
        self._bots: dict[str, ILinkBot] = {}
        self._login: ILinkBot | None = None

    def begin_login(self, ledger: int, label: str, existing_token: str, on_confirmed) -> dict:
        # 注册表读写与持锁的 adopt/stop_all 同锁串行：无锁交错会丢失取消信号
        # （如 stop_all 清 _login 与此处赋值交错 → 已停用的旧扫码存活）
        with self._lock:
            if self._login is not None:      # 同一时刻只挂一张码
                self._login._stop.set()
            bot = ILinkBot(ledger=ledger, label=label, manager=self)
            self._login = bot
        # 取二维码是网络调用，放锁外（锁内只做注册表读写，不跨网络持锁）
        return bot.begin_login(existing_token, on_confirmed)

    def adopt(self, bot: ILinkBot) -> None:
        """实例收编（start 自注册 / server 回调共用）：登记进 _bots 使其可见可停。
        无 bot_id（服务端 confirmed 响应缺字段）也按占位键登记——apply 的差量对账
        （want 键集不含占位键）会把它停掉，不再产生状态页看不见、无人能停的实例。
        旧实例的 stop（等在途处理≤3s + 下线通知网络往返）放锁外：持锁 stop 会把 states/begin_login
        等所有读操作阻塞数秒（begin_login 自己声明「锁内不跨网络」，adopt 曾违反）。"""
        old = None
        with self._lock:
            if bot._bot_id:
                old = self._bots.get(bot._bot_id)
                self._bots[bot._bot_id] = bot
            else:
                self._bots[f"_anon_{id(bot)}"] = bot
            if self._login is bot:
                self._login = None
        if old is not None and old is not bot:
            old.stop()

    def apply(self, entries: list) -> None:
        """按配置列表差量启停:新增/换 token 的启动,消失的停止（锁外并发停，见 _stop_concurrently）。"""
        want = {str(e.get("account_id") or ""): e for e in entries if e.get("account_id")}
        to_stop: list[ILinkBot] = []
        with self._lock:
            for aid in list(self._bots):
                if aid not in want:
                    to_stop.append(self._bots.pop(aid))
            for aid, e in want.items():
                cur = self._bots.get(aid)
                # 差量键含 label/ledger（不止 token）：归属组改名同步 conf 后,运行实例若不重建
                # 仍持旧 label 记账——账会一直挂到旧组名下,且 label 变化后实例属性与 conf 永久漂移
                if (cur is not None and cur._token == e.get("token")
                        and str(cur._label) == str(e.get("label") or "")
                        and int(cur._ledger or 0) == bot_core.entry_ledger(e)):
                    continue
                if cur is not None:
                    to_stop.append(self._bots.pop(aid))
                bot = ILinkBot(ledger=bot_core.entry_ledger(e), label=str(e.get("label") or ""),
                               manager=self)
                # 注册表写入归 apply 自管（不依赖 start→adopt 的内部副作用，测试替身可替换 ILinkBot）
                if bot.start({"bot_token": e.get("token"),
                              "baseurl": e.get("base_url") or "",
                              "ilink_bot_id": aid}):
                    self._bots[aid] = bot
        _stop_concurrently(to_stop)

    def stop_all(self) -> None:
        with self._lock:
            bots = list(self._bots.values())
            self._bots.clear()
            login, self._login = self._login, None
        _stop_concurrently(bots)
        if login is not None:
            login._stop.set()

    def states(self) -> list:
        with self._lock:
            return [{"account_id": aid, "label": b._label, "ledger": b._ledger, **b.state()}
                    for aid, b in self._bots.items()]

    def pending(self) -> dict:
        # 锁内取快照：stop_all/begin_login 清 _login 与此处读之间无锁会竞态出
        # None.state()/None.qr_img()（状态接口偶发 500）
        with self._lock:
            login = self._login
        if not login:
            return {"state": "off", "detail": ""}
        s = login.state()
        s["ledger"], s["label"] = login._ledger, login._label
        return s

    def qr_img(self) -> str:
        with self._lock:
            login = self._login
        return login.qr_img() if login else ""
