"""Explicit migration and local administrator provisioning. Never accepts CLI passwords."""
import argparse
import getpass
import os
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.orm import Session as DBSession
from api.db import make_engine, new_id
from .models import User, OrganizationRole, Membership, AuditEvent, Organization
from .security import hash_password, normalize_login

ORG_ID='00000000-0000-0000-0000-000000000001'
LEGACY_ID='00000000-0000-0000-0000-000000000002'


def migrate(engine):
    config=Config(str(Path(__file__).resolve().parents[1]/'alembic.ini'))
    config.set_main_option('script_location',str(Path(__file__).resolve().parents[1]/'alembic'))
    with engine.begin() as conn:
        # The legacy executor and all new workers use the same ownership lock.
        if not conn.scalar(text('SELECT pg_try_advisory_xact_lock(732914622)')):
            raise RuntimeError('Stop the existing executor before migrating')
        config.attributes['connection']=conn
        command.upgrade(config,'0009')


def bootstrap(engine, login, password, claim_legacy=False):
    normalized=normalize_login(login)
    encoded=hash_password(password)
    with DBSession(engine) as db,db.begin():
        if not db.get(Organization,ORG_ID,with_for_update=True):
            raise RuntimeError('Run the explicit team migration first')
        if db.scalar(select(OrganizationRole).where(OrganizationRole.org_id==ORG_ID)):
            raise RuntimeError('Administrator already exists; bootstrap is single use')
        user=User(id=new_id(),login=normalized,password_hash=encoded,active=True,session_version=1)
        db.add(user);db.flush()
        db.add(OrganizationRole(org_id=ORG_ID,user_id=user.id,role='deployment_admin'))
        if claim_legacy:
            db.add(Membership(workspace_id=LEGACY_ID,user_id=user.id,roles=['workspace_manager','operator','reviewer'],active=True,version=1))
        db.add(AuditEvent(id=new_id(),org_id=ORG_ID,workspace_id=LEGACY_ID if claim_legacy else None,
            actor_id=user.id,action='admin.bootstrap',resource_type='user',resource_id=user.id,
            request_id=new_id(),safe_detail={'legacy_claimed':claim_legacy}))
        return user.id


def change_account(engine, login, *, active=None, password=None):
    """Trusted deployment-console recovery; audit actor is explicitly system/local admin."""
    normalized=normalize_login(login)
    encoded=hash_password(password) if password is not None else None
    with DBSession(engine) as db,db.begin():
        db.scalar(select(Organization).where(Organization.id==ORG_ID).with_for_update(key_share=True))
        target=db.scalar(select(User).where(User.login==normalized).with_for_update(key_share=True))
        if target is None:
            raise RuntimeError('Account does not exist')
        if active is False:
            admin_ids=list(db.scalars(select(OrganizationRole.user_id).where(OrganizationRole.org_id==ORG_ID)))
            active_admins=list(db.scalars(select(User.id).where(User.id.in_(admin_ids),User.active.is_(True))))
            if target.id in active_admins and len(active_admins)==1:
                raise RuntimeError('Cannot disable the last active deployment administrator')
        if active is not None:
            target.active=active
        if encoded is not None:
            target.password_hash=encoded
        target.session_version+=1
        db.add(AuditEvent(id=new_id(),org_id=ORG_ID,workspace_id=None,actor_id=None,
            action='account.console_recovery',resource_type='user',resource_id=target.id,
            request_id=new_id(),safe_detail={'via':'local_deployment_console','active':target.active,'password_reset':encoded is not None}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('migrate')
    admin=sub.add_parser('bootstrap')
    admin.add_argument('--login',required=True)
    admin.add_argument('--claim-legacy',action='store_true')
    status=sub.add_parser('set-active')
    status.add_argument('--login',required=True)
    choices=status.add_mutually_exclusive_group(required=True)
    choices.add_argument('--enable',action='store_true')
    choices.add_argument('--disable',action='store_true')
    reset=sub.add_parser('reset-password')
    reset.add_argument('--login',required=True)
    args=parser.parse_args()
    engine=make_engine()
    try:
        if args.command=='migrate':
            migrate(engine)
            print('Team schema 0009 ready; no API or worker started.')
        elif args.command=='set-active':
            change_account(engine,args.login,active=args.enable)
            print('Account state changed; existing sessions revoked.')
        else:
            password=getpass.getpass('Administrator password (12-128 characters): ')
            if password!=getpass.getpass('Confirm password: '):
                raise RuntimeError('Password confirmation differs')
            if args.command=='bootstrap':
                bootstrap(engine,args.login,password,args.claim_legacy)
                print('Administrator created. No password or session token has been logged.')
            else:
                change_account(engine,args.login,password=password)
                print('Password changed; existing sessions revoked.')
    finally:
        engine.dispose()


if __name__=='__main__':
    main()
