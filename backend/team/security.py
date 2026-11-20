"""Server-side identity and authorization; callers own transaction boundaries."""
import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select

from api.common import Problem
from api.db import now
from .models import Membership, OrganizationRole, Session, User, Workspace


ROLE_PERMISSIONS = {
    'deployment_admin': frozenset({'users.manage', 'deployment.manage', 'backups.manage'}),
    'workspace_manager': frozenset({
        'members.manage', 'rules.publish', 'period.manage', 'cases.assign', 'data.read',
        'data.export', 'period.reopen', 'audit.read', 'jobs.read', 'results.read',
        'period.create', 'period.activate', 'jobs.manage',
    }),
    'operator': frozenset({
        'data.import', 'data.read', 'rules.draft', 'runs.create', 'cases.work', 'data.export',
        'period.inputs', 'jobs.read', 'jobs.cancel_own', 'cases.claim', 'results.read',
        'period.create', 'period.activate',
    }),
    'reviewer': frozenset({
        'data.read', 'cases.review', 'period.close', 'data.export', 'period.reopen',
        'audit.read', 'jobs.read', 'results.read',
    }),
    'viewer': frozenset({'results.read'}),
}
WORKSPACE_ROLES = frozenset(ROLE_PERMISSIONS) - {'deployment_admin'}
ABSOLUTE_TIMEOUT = timedelta(hours=12)
IDLE_TIMEOUT = timedelta(minutes=30)
PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)
_LOGIN = re.compile(r'[a-z0-9][a-z0-9._@+-]{2,127}\Z', re.ASCII)


def normalize_login(value):
    """Deliberately small account identifier alphabet; display names are separate."""
    if not isinstance(value, str):
        raise Problem('INVALID_LOGIN', '账号格式无效。')
    login = value.strip().lower()
    if not _LOGIN.fullmatch(login):
        raise Problem('INVALID_LOGIN', '账号须为 3 至 128 位英文、数字或 . _ @ + -。')
    return login


def validate_password(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 128:
        raise Problem('INVALID_PASSWORD', '密码须为 12 至 128 个字符。')
    try:
        encoded = value.encode('utf-8')
    except UnicodeEncodeError:
        raise Problem('INVALID_PASSWORD', '密码包含无效字符。') from None
    if len(encoded) > 512 or '\x00' in value:
        raise Problem('INVALID_PASSWORD', '密码包含无效字符或超过长度限制。')
    return value


def hash_password(value):
    return PASSWORD_HASHER.hash(validate_password(value))


def verify_password(encoded_hash, value):
    if not isinstance(encoded_hash, str) or not encoded_hash.isascii():
        return False
    try:
        validate_password(value)
        return PASSWORD_HASHER.verify(encoded_hash, value)
    except (Problem, InvalidHashError, VerificationError, TypeError):
        return False


def needs_password_rehash(encoded_hash):
    if not isinstance(encoded_hash, str) or not encoded_hash.isascii():
        return True
    try:
        return PASSWORD_HASHER.check_needs_rehash(encoded_hash)
    except (InvalidHashError, TypeError):
        return True


def new_token():
    return secrets.token_urlsafe(32)


def token_hash(raw):
    if not isinstance(raw, str) or not 32 <= len(raw) <= 256 or not raw.isascii():
        raise Problem('UNAUTHENTICATED', '请重新登录。', 401)
    return hashlib.sha256(raw.encode('ascii')).hexdigest()


def workspace_permissions(roles):
    """Only explicit workspace roles count; an organization grant is no bypass."""
    if not isinstance(roles, (list, tuple, set, frozenset)):
        return frozenset()
    if not roles or any(not isinstance(role, str) or role not in WORKSPACE_ROLES for role in roles):
        return frozenset()
    return frozenset().union(*(ROLE_PERMISSIONS[role] for role in roles))


def issue_session(db, user):
    if not user.active:
        raise Problem('UNAUTHENTICATED', '请重新登录。', 401)
    raw_token, stamp = new_token(), now()
    raw_csrf = csrf_for_session(raw_token)
    session = Session(token_hash=token_hash(raw_token), user_id=user.id,
                      csrf_hash=token_hash(raw_csrf), created_at=stamp, last_seen_at=stamp,
                      expires_at=stamp + ABSOLUTE_TIMEOUT, session_version=user.session_version)
    db.add(session)
    return session, raw_token, raw_csrf


def csrf_for_session(raw_token):
    token_hash(raw_token)  # Validate token format before deriving a browser-visible token.
    return hmac.new(raw_token.encode('ascii'), b'reconcile-csrf-v1', hashlib.sha256).hexdigest()


def _select(db, statement, lock=False, no_key_update=False):
    if lock:
        statement = statement.with_for_update(key_share=no_key_update)
    return db.scalar(statement.execution_options(populate_existing=True))


def authenticate_session(db, raw_token, csrf=None, mutation=False):
    """Mutation authentication locks user before session and retains locks to commit.

    Browser tokens are never stored in plaintext. Callers must commit successful
    authentication to retain the idle-time refresh; failures must not be committed.
    """
    hashed = token_hash(raw_token)
    record = _select(db, select(Session).where(Session.token_hash == hashed))
    if record is None:
        raise Problem('UNAUTHENTICATED', '请重新登录。', 401)
    user = _select(db, select(User).where(User.id == record.user_id), lock=mutation, no_key_update=True)
    # Lock the session after the identity, then re-read it to notice logout/reset.
    record = _select(db, select(Session).where(Session.token_hash == hashed), lock=True)
    stamp = now()
    if (record is None or user is None or not user.active
            or record.user_id != user.id or record.session_version != user.session_version
            or stamp >= record.expires_at or stamp - record.last_seen_at >= IDLE_TIMEOUT):
        raise Problem('UNAUTHENTICATED', '请重新登录。', 401)
    if mutation:
        try:
            supplied_hash = token_hash(csrf)
        except Problem:
            raise Problem('CSRF_REJECTED', '请求验证失败，请刷新后重试。', 403) from None
        if not hmac.compare_digest(record.csrf_hash, supplied_hash):
            raise Problem('CSRF_REJECTED', '请求验证失败，请刷新后重试。', 403)
    record.last_seen_at = stamp
    return user, record


def authorize_workspace(db, user_id, workspace_id, permission, lock=False):
    user = _select(db, select(User).where(User.id == user_id), lock=lock, no_key_update=True)
    if user is None or not user.active:
        raise Problem('UNAUTHENTICATED', '请重新登录。', 401)
    workspace = _select(db, select(Workspace).where(Workspace.id == workspace_id))
    if workspace is None:
        raise Problem('NOT_FOUND', '记录不存在。', 404)
    # Read/lock organization grants before membership, without allowing a bypass.
    _select(db, select(OrganizationRole).where(OrganizationRole.org_id == workspace.org_id,
            OrganizationRole.user_id == user_id,
            OrganizationRole.role == 'deployment_admin'), lock=lock)
    membership = _select(db, select(Membership).where(
        Membership.workspace_id == workspace_id, Membership.user_id == user_id), lock=lock)
    if membership is None or not membership.active:
        raise Problem('NOT_FOUND', '记录不存在。', 404)
    if permission not in workspace_permissions(membership.roles):
        raise Problem('FORBIDDEN', '当前账号没有执行此操作的权限。', 403)
    return membership


def require_org_admin(db, user_id, org_id, lock=False):
    user = _select(db, select(User).where(User.id == user_id), lock=lock, no_key_update=True)
    if user is None or not user.active:
        raise Problem('UNAUTHENTICATED', '请重新登录。', 401)
    grant = _select(db, select(OrganizationRole).where(OrganizationRole.org_id == org_id,
            OrganizationRole.user_id == user_id,
            OrganizationRole.role == 'deployment_admin'), lock=lock)
    if grant is None:
        raise Problem('FORBIDDEN', '需要组织管理员权限。', 403)
    return grant
