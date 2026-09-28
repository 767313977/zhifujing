"""会员鉴权：密码哈希、会话令牌、限流、以及两个 FastAPI 依赖。

设计见设计文档 §8.69。三个刻意的取舍：

1. **零第三方依赖**。密码哈希用标准库 `hashlib.scrypt`、令牌用 `secrets`、
   摘要用 `hashlib.sha256` —— 于是云端部署不必装新包（`requirements.txt` 一行不改）。
   自己实现签名（JWT 那一套）远比用随机令牌 + 一张会话表危险，所以不用 JWT。
2. **会话存服务端**。令牌原文只出现在 cookie 里，库里存它的 sha256 —— 库被看到
   （备份、快照、误提交）也不能直接拿去冒充登录。顺带换来「能踢人」：
   删一行就登出了，JWT 签出去是撤不回来的。
3. **登录态用 cookie，不是 Authorization 头**。cookie 的 `SameSite=Lax` 能挡住
   跨站发起的状态变更请求，于是不必再单独做一套 CSRF token。
"""

import hashlib
import hmac
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db import get_db, session_scope
from app.models import AppUser, UserSession

logger = logging.getLogger(__name__)

# cookie 名与有效期。30 天**滑动**：每次带有效 cookie 访问就往后推，
# 一直用就一直不用重登，闲置满 30 天才失效。
COOKIE_NAME = "fupan_session"
SESSION_DAYS = 30
# 滑动续期的写库节流：expires_at 只剩不到这个天数时才推。
# 不节流的话**每个请求都要写一次库**（一次页面加载十几个请求），
# 而 WAL 下的写是要拿写锁的 —— 用「最多每天推一次」换掉那堆无谓的写。
SESSION_TOUCH_DAYS = 1

# scrypt 参数。n 是 CPU/内存开销的主旋钮（n=2^15 约 32MB、单次约 50~100ms，
# 对「一天登录几次」的站足够，也不至于把 2 核小机器打满）。
# ⚠️ 参数会**写进哈希串**，所以以后想调大，老密码仍然能验（见 verify_password）。
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

MIN_PASSWORD_LENGTH = 8


# ---------------------------------------------------------------- 密码


def _scrypt(password: bytes, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    """所有 scrypt 调用都走这里 —— 免得将来只有一处带了 `maxmem`。

    ⚠️ `maxmem` **必须显式给**：OpenSSL 的默认上限是 32 MiB，而 n=2^15 / r=8 需要
    128*n*r = 正好 32 MiB，会顶到上限并抛
    `ValueError: [digital envelope routines] memory limit exceeded`（本地跑迁移脚本时
    第一次就撞上）。留一倍余量，将来把 n 调大也不会先在这里炸。
    """
    return hashlib.scrypt(
        password,
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=dklen,
        maxmem=128 * n * r * 2,
    )


def hash_password(password: str) -> str:
    """把明文密码变成 `scrypt$n$r$p$盐$摘要` 一整串。

    盐与参数**都放在串里**（而不是另开两列）：将来调参数时，老值仍能被正确验签，
    不会出现「改了参数所有人都登不进」这种只能靠回滚解决的事故。
    """
    salt = secrets.token_bytes(16)
    digest = _scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """验密码。**必须用 hmac.compare_digest**（等时比较）。

    用 `==` 比较的话，比较会在第一个不同的字节处提前返回，理论上能被逐字节
    试出摘要。这里的耗时差极小、实战价值有限，但换成等时比较是零成本的。
    """
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = _scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(digest_hex) // 2,
        )
    except (ValueError, TypeError):
        # 串被改坏 / 是旧格式：当验不过处理，不要抛出去（那会变成 500）
        logger.warning("密码哈希串无法解析，按验证失败处理")
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def validate_password(password: str) -> None:
    """注册与改密码时的强度检查。抛 HTTPException(400)。

    只卡长度，不强制「大小写+数字+符号」那套组合规则 —— 那种规则的实际效果是
    逼出 `Abc123!` 这类键盘走位，反而更好猜。
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"密码至少 {MIN_PASSWORD_LENGTH} 位",
        )


# ---------------------------------------------------------------- 会话


def _digest(token: str) -> str:
    """令牌摘要。**不做加盐/慢哈希**：令牌本身就是 256 位随机数，
    没有字典可爆破，sha256 一次足够，还便宜。
    """
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: AppUser, user_agent: str | None = None) -> str:
    """建一个会话，返回**原文令牌**（只有调用方会把它放进 cookie）。"""
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    db.add(
        UserSession(
            token_hash=_digest(token),
            user_id=user.id,
            created_at=now,
            expires_at=now + timedelta(days=SESSION_DAYS),
            user_agent=(user_agent or "")[:200] or None,
        )
    )
    user.last_login_at = now
    # 顺手清掉这个人已经过期的会话：不清理的话表会随着「登录过多少次」一直长，
    # 而清理时机几乎不花钱（登录本来就是低频操作）
    db.execute(
        delete(UserSession).where(
            UserSession.user_id == user.id, UserSession.expires_at < now
        )
    )
    return token


def resolve_session(db: Session, token: str | None) -> AppUser | None:
    """把 cookie 里的令牌换成用户。无效/过期/已停用一律返回 None。"""
    if not token:
        return None
    row = db.get(UserSession, _digest(token))
    if row is None:
        return None
    now = datetime.now()
    if row.expires_at < now:
        db.delete(row)
        db.commit()
        return None
    user = db.get(AppUser, row.user_id)
    if user is None or user.disabled_at is not None:
        return None
    # 滑动续期（节流：只在剩得不多时才写库，见 SESSION_TOUCH_DAYS 的说明）
    if row.expires_at - now < timedelta(days=SESSION_DAYS - SESSION_TOUCH_DAYS):
        row.expires_at = now + timedelta(days=SESSION_DAYS)
        db.commit()
    return user


def delete_session(db: Session, token: str | None) -> None:
    """退出登录：删掉这一行。"""
    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == _digest(token)))
        db.commit()


def delete_user_sessions(db: Session, user_id: int, keep: str | None = None) -> int:
    """踢掉某个用户的全部会话（可保留当前这一个）。返回删掉的行数。

    改密码、停用账号、管理员重置密码都要用：**改了密码却让旧会话继续有效**，
    等于「怀疑密码泄露」时什么都没做。
    """
    statement = delete(UserSession).where(UserSession.user_id == user_id)
    if keep:
        statement = statement.where(UserSession.token_hash != _digest(keep))
    result = db.execute(statement)
    db.commit()
    return result.rowcount or 0


# ---------------------------------------------------------------- 依赖


def client_ip(request: Request) -> str:
    """取客户端 IP。**优先 X-Forwarded-For**：线上是 nginx 反代，
    直连地址永远是 127.0.0.1，按它限流等于全站共用一个桶。
    ⚠️ 这个头是客户端可伪造的 —— 限流只是「提高爆破成本」，不承担安全边界。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def current_user(request: Request, db: Session = Depends(get_db)) -> AppUser:
    """**业务接口的当前用户**。由 app 级依赖 `require_login` 保证已登录。"""
    user = getattr(request.state, "user", None)
    if user is None:
        # 正常走不到这里：require_login 已经把未登录的挡在 401。
        # 留着是为了「哪天漏挂依赖」时不至于变成「无用户却放行」——
        # 那种失败方式（以为有门其实没有）比报错危险得多。
        raise HTTPException(status_code=401, detail="未登录")
    return user


def require_admin(user: AppUser = Depends(current_user)) -> AppUser:
    """管理员专用（`/api/admin/*`）。能触发采集的接口全在这一层后面。"""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def require_login(request: Request) -> None:
    """**全站登录关卡**（app 级依赖，见 main.py）。

    为什么用「app 级依赖」而不是逐个路由挂依赖：现在有 20 多个 `/api/*` 路由，
    一个一个挂**漏掉一个就是一个洞**，将来新加路由也容易忘。这里是白名单制、
    **默认拒绝** —— 不在名单里的一律要求登录。

    ⚠️ 将来要加「不需要登录」的接口，**必须来 PUBLIC_PATHS 加白名单**。
    """
    path = request.url.path
    if not path.startswith("/api/"):
        # 前端产物 / SPA 回退 / /assets：登录页本身也得打得开
        return
    if path in PUBLIC_PATHS:
        return
    with session_scope() as db:
        user = resolve_session(db, request.cookies.get(COOKIE_NAME))
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录")
        # 已经加载好的 ORM 对象在 session 关闭后仍可读标量字段（expire_on_commit=False），
        # 所以这里可以安全地把它挂到 request 上给下游用
        request.state.user = user


# 不需要登录的接口。**只有这三个**：
#   - login / register 是登录本身
#   - health 是部署脚本用来验「服务起来了没有」的（它不含任何业务数据）
PUBLIC_PATHS = frozenset({"/api/auth/login", "/api/auth/register", "/api/health"})


# ---------------------------------------------------------------- 限流


class RateLimiter:
    """按 key 计失败次数的内存限流器（当前用于登录/注册防爆破）。

    ⚠️ 两个已知边界，都是**故意的**：
    - 进程内存：重启即清零，多 worker 各算各的。服务必须单进程跑（调度器同因），
      所以这里够用；真要上量再换 Redis —— 接口行为不变，只是换存储。
    - 按 IP 计数挡不住「换 IP」的分布式爆破。它的定位是提高成本，不是安全边界
      （真正的边界是邀请码：没有码连账号都建不出来）。
    """

    def __init__(self, limit: int, window_sec: int, lock_sec: int) -> None:
        self.limit = limit
        self.window = window_sec
        self.lock_sec = lock_sec
        self._hits: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> float | None:
        """被锁的话返回还要等多少秒，否则 None。"""
        now = time.monotonic()
        with self._lock:
            until = self._locked.get(key)
            if until is None:
                return None
            if until <= now:
                del self._locked[key]
                return None
            return until - now

    def fail(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            hits.append(now)
            self._hits[key] = hits
            if len(hits) >= self.limit:
                self._locked[key] = now + self.lock_sec
            # 字典的键是 IP，本身没有过期机制：被扫站的换着 IP 打就会一直加。
            # 超过一定规模就顺手把过期的清掉（一条简单兜底，不做 LRU）。
            if len(self._hits) > _MAX_RATE_KEYS:
                stale = [
                    k
                    for k, ts in self._hits.items()
                    if not ts or now - ts[-1] >= self.window
                ]
                for k in stale:
                    self._hits.pop(k, None)
                    self._locked.pop(k, None)

    def reset(self, key: str) -> None:
        """成功一次就清零：不然正常用户偶尔打错几次会累积到被锁。"""
        with self._lock:
            self._hits.pop(key, None)
            self._locked.pop(key, None)


# 10 分钟内失败 5 次 → 锁 10 分钟
login_limiter = RateLimiter(limit=5, window_sec=600, lock_sec=600)

# 限流器里允许积压的 key 数（见 RateLimiter.fail 里的清理）
_MAX_RATE_KEYS = 1000


def guard_rate(key: str) -> None:
    """被锁就抛 429（带剩余秒数）。"""
    wait = login_limiter.retry_after(key)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail=f"尝试过于频繁，请 {int(wait) + 1} 秒后再试",
            headers={"Retry-After": str(int(wait) + 1)},
        )


class Cooldown:
    """同一把 key 在 N 秒内只放行一次。

    给「手动触发一次、而且花 iFinD 配额」的入口用（如 `/api/watchlist/sync`）：
    日常采集本来就会做这件事，手动那一下只是补救，没必要让人连点 ——
    每次点都在花真金白银的调用次数。

    键是用户 id，数量有界（会员数），所以不做清理。
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def remaining(self, key: str) -> float | None:
        """还要等多少秒；现在可以放行时返回 None。"""
        now = time.monotonic()
        with self._lock:
            last = self._last.get(key)
            if last is None:
                return None
            left = self.seconds - (now - last)
            return left if left > 0 else None

    def touch(self, key: str) -> None:
        """记下「刚放行过一次」。"""
        with self._lock:
            self._last[key] = time.monotonic()


__all__ = [
    "COOKIE_NAME",
    "SESSION_DAYS",
    "MIN_PASSWORD_LENGTH",
    "Cooldown",
    "client_ip",
    "create_session",
    "current_user",
    "delete_session",
    "delete_user_sessions",
    "guard_rate",
    "hash_password",
    "login_limiter",
    "require_admin",
    "require_login",
    "resolve_session",
    "validate_password",
    "verify_password",
]
