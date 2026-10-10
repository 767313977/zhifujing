"""注册 / 登录 / 退出 / 改密码。

设计见设计文档 §8.69。四条与安全直接相关的约定：

- **一次性邀请码靠「比较并换」保证**（见 `register` 里的 UPDATE ... WHERE used_by IS NULL）：
  只在 Python 侧判一下「用过了没」是挡不住并发两个人的 —— 而「一个码只能注册一个账号」
  正是这套东西的核心不变式。
- **登录失败不区分「没这个人」与「密码不对」**。区分开来等于免费送出一个
  「哪些用户名有效」的探测接口。
- **注册与登录都按 IP 限流**（防爆破，见 services/auth.RateLimiter）。
- cookie 的 `Secure` 标志**按请求的实际协议决定**，不写成配置项：配置项会跟着 `.env`
  一起被打包推到云端（`pack_deploy.py` 的 INCLUDE 里有 `.env`），本地要关、云端要开，
  一个字段同时满足两边就会出错一边（`SCHEDULER_ENABLED` 就为此在 install.sh 里另加了一道
  覆盖）。按协议判断则两边自动都对。
  ⚠️ 依赖 nginx 转发 `X-Forwarded-Proto`（见 deploy/setup_nginx.sh），
  否则云端也会被判成 http、cookie 少一个 Secure。
- **明文 http 下拒绝登录 / 注册 / 改密码**（2026-10-10 加，见
  `_require_secure_transport`）：因为上面那条按协议给 `Secure`，走 http 时 cookie
  不带 Secure、浏览器照存 —— 于是 `http://<公网 IP>:8080` 那条**明文**路其实能正常登录，
  密码明文过网（`deploy/setup_nginx.sh` 里那句「8080 登录不了」是错的：不是代码比注释
  严，是**代码比注释松**）。现在明文 + 公网直接 403，本机与内网放行。
"""

import ipaddress
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, InviteCode
from app.schemas import LoginIn, MeOut, PasswordIn, RegisterIn
from app.services import auth

router = APIRouter(prefix="/api/auth", tags=["登录"])

USERNAME_MAX = 32

#: 明文 http 下**放行**的主机名：本机调试用。
#: 内网地址（192.168.x.x / 10.x / 172.16-31.x）也放行，判据见 `_is_local_or_private`。
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_local_or_private(host: str) -> bool:
    """本机或内网地址？

    ⚠️ 只有 IP 判得了私有段，**域名一律按「非内网」处理** —— 一个域名解析到哪儿
    这里看不到，不该靠猜。
    """
    if host in _LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _require_secure_transport(request: Request) -> None:
    """别让密码走明文公网。

    为什么要有这道闸：cookie 的 `Secure` 是按**请求协议**给的（见模块开头），所以走
    http 时它不带 `Secure`、浏览器照存 —— 结果是 `http://<公网 IP>:8080` 那条**明文**路
    能正常登录，密码明文过网。`deploy/setup_nginx.sh` 里写着「8080 登录不了」，实际
    登得了（2026-10-10 修：让行为与那句注释对齐）。

    为什么只拦「公网 + 明文」：本地开发就是 `http://127.0.0.1`，内网是自家的网，
    这两处没必要逼人上证书；而往公网发明文密码是纯亏。
    ⚠️ 影响：公网 8080 兜底入口从此**只能看页面 / 查 nginx 报错，不能登录** —— 那本来
    就是它的用途。域名 / 证书出问题时请走 SSH 修，别用明文登录。
    """
    if request.url.scheme == "https":
        return
    if _is_local_or_private((request.url.hostname or "").lower()):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "当前是明文 http，登录会把密码明文发出去。请改用 https 打开本站"
            "（本机调试用 http://127.0.0.1 可以）"
        ),
    )


def _normalize_username(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise HTTPException(status_code=400, detail="用户名不能为空")
    if len(name) > USERNAME_MAX:
        raise HTTPException(status_code=400, detail=f"用户名最多 {USERNAME_MAX} 个字符")
    return name


def _set_cookie(response: Response, token: str, request: Request) -> None:
    """种会话 cookie。`Secure` 按实际协议（见模块说明）。"""
    response.set_cookie(
        key=auth.COOKIE_NAME,
        value=token,
        max_age=auth.SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


@router.post("/register", response_model=MeOut)
def register(
    payload: RegisterIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> MeOut:
    """用邀请码注册。成功即登录（不让刚注册的人再去登一次）。"""
    _require_secure_transport(request)
    ip = auth.client_ip(request)
    auth.guard_rate(ip)
    username = _normalize_username(payload.username)
    auth.validate_password(payload.password)

    # **先确认邀请码可用，再动用户名**（2026-10-10 修）。下面 `db.flush()` 才是用户名
    # 唯一性的判定点，而它原本排在「消耗邀请码」之前 —— 于是一个**手持无效邀请码**的人
    # 也能靠「用户名已被占用」这条错误把用户名一个个试出来。
    # 这里只是一次**预检**：真正的判据仍是后面那句原子的 UPDATE ... WHERE used_by IS NULL
    # （并发下靠它保证「一个码只注册一个账号」），所以那条不变式不受影响。
    code = payload.invite_code.strip()
    if (
        db.scalar(
            select(InviteCode.code).where(
                InviteCode.code == code,
                InviteCode.used_by.is_(None),
                InviteCode.disabled_at.is_(None),
            )
        )
        is None
    ):
        auth.login_limiter.fail(ip)
        raise HTTPException(status_code=400, detail="邀请码无效或已被使用")

    user = AppUser(
        username=username,
        password_hash=auth.hash_password(payload.password),
        is_admin=False,
    )
    db.add(user)
    try:
        # 用户名唯一性靠库上的唯一约束，不靠「先查一次」—— 后者在并发下会让两个人
        # 同时通过检查。这个 flush 就是唯一性的判定点，也是用户 id 的来源（下面要用）。
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        auth.login_limiter.fail(ip)
        raise HTTPException(status_code=400, detail="用户名已被占用") from exc

    # 「比较并换」：只有 `used_by` 还是空**并且**没被停用时才认，且这一步是原子的。
    # 先 SELECT 判一次再 UPDATE 的写法在两个人同时点注册时会让同一个码注册出两个账号。
    consumed = db.execute(
        update(InviteCode)
        .where(
            InviteCode.code == code,
            InviteCode.used_by.is_(None),
            InviteCode.disabled_at.is_(None),
        )
        .values(used_by=user.id, used_at=datetime.now())
    ).rowcount
    if not consumed:
        # 「无效」与「已用过」合并成一句：不告诉对方这个码到底存不存在。
        # rollback 会把上面那个刚插入的账号一起撤掉
        db.rollback()
        auth.login_limiter.fail(ip)
        raise HTTPException(status_code=400, detail="邀请码无效或已被使用")

    token = auth.create_session(db, user, request.headers.get("user-agent"))
    db.commit()
    db.refresh(user)
    auth.login_limiter.reset(ip)
    _set_cookie(response, token, request)
    return MeOut.model_validate(user)


@router.post("/login", response_model=MeOut)
def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> MeOut:
    _require_secure_transport(request)
    ip = auth.client_ip(request)
    # 用户名先归一化，限流与查库都用这一份
    username = payload.username.strip()
    # **两道限流并存**：按 IP（login_limiter）与按用户名（login_user_limiter），
    # 任一命中就拒绝。只有按 IP 的话，换个 IP 就能绕开、分布式爆破几乎不受限。
    auth.guard_rate(ip)
    auth.guard_user_rate(username)

    user = db.scalar(select(AppUser).where(AppUser.username == username))
    # 没这个用户也照走一次**同样开销**的哈希校验。⚠️ 这里必须给**假哈希**而不是空串
    # （`services.auth.DUMMY_PASSWORD_HASH`）：空串会卡在解析那步直接返回 False、
    # **scrypt 根本不跑**，于是「有没有这个人」就从响应快慢上漏出去了（2026-10-10 修）
    stored = user.password_hash if user is not None else auth.DUMMY_PASSWORD_HASH
    if user is None or not auth.verify_password(payload.password, stored):
        # 两条限流**都**记一笔 —— 用户名不存在也照记，否则按名计数本身就成了
        # 「哪些用户名有效」的探测口（见 services/auth.login_user_limiter）
        auth.login_limiter.fail(ip)
        auth.login_user_limiter.fail(username)
        raise HTTPException(status_code=401, detail="用户名或密码不对")
    if user.disabled_at is not None:
        raise HTTPException(status_code=403, detail="这个账号已被停用")

    token = auth.create_session(db, user, request.headers.get("user-agent"))
    db.commit()
    auth.login_limiter.reset(ip)
    auth.login_user_limiter.reset(username)
    _set_cookie(response, token, request)
    return MeOut.model_validate(user)


@router.post("/logout")
def logout(
    request: Request, response: Response, db: Session = Depends(get_db)
) -> dict:
    """删掉这个会话并清 cookie。重复调用无害。"""
    auth.delete_session(db, request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me", response_model=MeOut)
def me(user: AppUser = Depends(auth.current_user)) -> MeOut:
    """前端启动时问一次「我是谁」。未登录由 app 级依赖直接 401。"""
    return MeOut.model_validate(user)


@router.put("/password", response_model=MeOut)
def change_password(
    payload: PasswordIn,
    request: Request,
    user: AppUser = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> MeOut:
    """改密码，并把**其它**设备踢下线。

    ⚠️ 改密码还有效的旧会话等于「怀疑密码泄露却什么也没做」，所以除当前这一个
    之外全删。当前这个留着，免得刚改完就被踢出去、还得再登一次。
    """
    # 这个接口一次发**两个**密码，所以和登录一样要过这两道闸（2026-10-10 加）：
    # 明文公网不许发；旧密码试错也计入按 IP 的限流（否则可以用它当密码爆破口）。
    _require_secure_transport(request)
    ip = auth.client_ip(request)
    auth.guard_rate(ip)

    if not auth.verify_password(payload.old_password, user.password_hash):
        auth.login_limiter.fail(ip)
        raise HTTPException(status_code=400, detail="旧密码不对")
    auth.validate_password(payload.new_password)
    auth.login_limiter.reset(ip)

    row = db.get(AppUser, user.id)
    if row is None:  # 会话有效但账号没了（理论上不会发生）
        raise HTTPException(status_code=401, detail="请重新登录")
    row.password_hash = auth.hash_password(payload.new_password)
    db.commit()

    auth.delete_user_sessions(db, user.id, keep=request.cookies.get(auth.COOKIE_NAME))
    return MeOut.model_validate(row)
