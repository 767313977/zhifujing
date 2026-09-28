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
"""

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
    ip = auth.client_ip(request)
    auth.guard_rate(ip)
    username = _normalize_username(payload.username)
    auth.validate_password(payload.password)

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
            InviteCode.code == payload.invite_code.strip(),
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
    ip = auth.client_ip(request)
    auth.guard_rate(ip)

    user = db.scalar(select(AppUser).where(AppUser.username == payload.username.strip()))
    # 没这个用户也照走一次哈希校验（空串必然验不过），免得「有没有这个人」
    # 从响应快慢上漏出去
    stored = user.password_hash if user is not None else ""
    if user is None or not auth.verify_password(payload.password, stored):
        auth.login_limiter.fail(ip)
        raise HTTPException(status_code=401, detail="用户名或密码不对")
    if user.disabled_at is not None:
        raise HTTPException(status_code=403, detail="这个账号已被停用")

    token = auth.create_session(db, user, request.headers.get("user-agent"))
    db.commit()
    auth.login_limiter.reset(ip)
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
    if not auth.verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="旧密码不对")
    auth.validate_password(payload.new_password)

    row = db.get(AppUser, user.id)
    if row is None:  # 会话有效但账号没了（理论上不会发生）
        raise HTTPException(status_code=401, detail="请重新登录")
    row.password_hash = auth.hash_password(payload.new_password)
    db.commit()

    auth.delete_user_sessions(db, user.id, keep=request.cookies.get(auth.COOKIE_NAME))
    return MeOut.model_validate(row)
