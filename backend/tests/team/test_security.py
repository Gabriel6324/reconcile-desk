"""Identity boundary regressions independent of any running service/database."""
import hashlib

import pytest

from api.common import Problem
from team.security import (
    ROLE_PERMISSIONS, WORKSPACE_ROLES, hash_password, needs_password_rehash,
    new_token, normalize_login, token_hash, validate_password, verify_password,
    workspace_permissions,
)


def test_password_hash_salted_argon2id_and_exact_password():
    password = ' correct horse battery '  # Password whitespace is significant.
    first, second = hash_password(password), hash_password(password)
    assert first.startswith('$argon2id$') and second.startswith('$argon2id$')
    assert first != second
    assert verify_password(first, password)
    assert not verify_password(first, password.strip())
    assert not verify_password(first, 'incorrect password')
    assert not needs_password_rehash(first)


@pytest.mark.parametrize('password', ['short', 'x'*129, None, 'x'*12+'\x00', '\ud800'*12])
def test_password_input_bounds(password):
    with pytest.raises(Problem) as exc:
        validate_password(password)
    assert exc.value.code == 'INVALID_PASSWORD'


def test_unicode_password_preserved():
    password = '安全核对工作台密码验证测试'
    encoded = hash_password(password)
    assert verify_password(encoded, password)


@pytest.mark.parametrize('encoded', ['invalid', '$argon2id$invalid', None])
def test_corrupted_hash_denies_login(encoded):
    assert verify_password(encoded, 'valid long password') is False


def test_login_normalization_is_unambiguous():
    assert normalize_login('  Member.Name+1@Example.COM ') == 'member.name+1@example.com'


@pytest.mark.parametrize('login', ['', 'ab', 'a b', '管理员', 'admin\nuser', '_admin', 'x'*129, None])
def test_invalid_login_rejected(login):
    with pytest.raises(Problem):
        normalize_login(login)


def test_high_entropy_tokens_stored_only_as_digest():
    tokens = [new_token() for _ in range(100)]
    assert len(set(tokens)) == 100
    assert all(len(token) == 43 for token in tokens)
    assert token_hash(tokens[0]) == hashlib.sha256(tokens[0].encode('ascii')).hexdigest()
    assert len(token_hash(tokens[0])) == 64


@pytest.mark.parametrize('token', [None, '', 'x'*31, 'x'*257, '中'*43])
def test_invalid_session_token_denied(token):
    with pytest.raises(Problem) as exc:
        token_hash(token)
    assert exc.value.status == 401


def test_viewer_cannot_read_raw_data_or_export():
    assert workspace_permissions(['viewer']) == {'results.read'}
    assert 'data.read' not in workspace_permissions(['viewer'])
    assert 'data.export' not in workspace_permissions(['viewer'])


def test_admin_grant_cannot_be_injected_as_workspace_role():
    assert workspace_permissions(['deployment_admin']) == set()
    assert workspace_permissions(['operator', 'deployment_admin']) == set()
    assert 'deployment_admin' not in WORKSPACE_ROLES


def test_workspace_roles_additive_but_not_escalating():
    permissions = workspace_permissions(['operator', 'reviewer', 'operator'])
    assert 'cases.work' in permissions and 'cases.review' in permissions
    assert 'users.manage' not in permissions
    assert 'members.manage' not in permissions
    for role in ('operator', 'workspace_manager', 'reviewer'):
        assert 'results.read' in ROLE_PERMISSIONS[role]


@pytest.mark.parametrize('roles', [[], ['owner'], ['viewer', 'unknown'], 'operator', [None]])
def test_unknown_or_malformed_role_denies_all(roles):
    assert workspace_permissions(roles) == set()
