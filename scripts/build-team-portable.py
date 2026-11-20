"""Build a clean offline Windows distribution from locally provisioned runtimes.

Run --runtime-only while application compilation is in progress, then --finish.
Never package an installation with data/ or backups/. No user credentials read.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

PROJECT=Path(__file__).resolve().parents[1]
WORKSPACE=PROJECT.parents[1]


def ignored(directory,names):
    return [name for name in names if name in {'__pycache__','.pytest_cache','node_modules','.git','tests'}
            or name.endswith(('.pyc','.tsbuildinfo','.log')) or name.startswith('.env')]


def runtime(bundle,python_base,runtime_root):
    target=bundle/'runtime/python';target.mkdir(parents=True,exist_ok=True)
    for file in python_base.iterdir():
        if file.is_file() and (file.suffix in {'.exe','.dll'} or file.name=='LICENSE.txt'):
            shutil.copy2(file,target/file.name)
    shutil.copytree(python_base/'DLLs',target/'DLLs',dirs_exist_ok=True,ignore=ignored)
    shutil.copytree(python_base/'Lib',target/'Lib',dirs_exist_ok=True,
                   ignore=lambda d,n:ignored(d,n)+(['site-packages'] if 'site-packages' in n else []))
    shutil.copytree(runtime_root/'venv/Lib/site-packages',target/'Lib/site-packages',dirs_exist_ok=True,
                   ignore=lambda d,n:ignored(d,n)+[x for x in n if x=='pip' or x.startswith('pip-')])
    shutil.copytree(runtime_root/'postgresql/pgsql',bundle/'runtime/postgresql/pgsql',dirs_exist_ok=True,ignore=ignored)
    vc=runtime_root/'vc-runtime-x64'
    for name in ['msvcp140.dll','vcruntime140.dll','vcruntime140_1.dll']:
        shutil.copy2(vc/name,bundle/'runtime/postgresql/pgsql/bin'/name)
    shutil.copytree(vc,bundle/'runtime/vc-runtime-notices',dirs_exist_ok=True,
                   ignore=lambda d,n:[x for x in n if x.endswith('.dll')])
    with zipfile.ZipFile(runtime_root/'postgresql-17.11-windows.zip') as archive:
        for name in ['pgsql/server_license.txt','pgsql/commandlinetools_3rd_party_licenses.txt']:
            (bundle/'runtime/postgresql/pgsql'/Path(name).name).write_bytes(archive.read(name))


def application(bundle):
    if not (PROJECT/'frontend/dist/index.html').is_file() or not (PROJECT/'backend/team/local.py').is_file():
        raise RuntimeError('Build frontend and local team app before finishing package')
    # Rebuilding a clean distribution must not retain obsolete hashed JS assets
    # or removed backend modules. main() rejects installations containing data.
    for relative in ['app/backend','app/frontend/dist']:
        target=(bundle/relative).resolve()
        if not target.is_relative_to(bundle.resolve()) or target==bundle.resolve():
            raise RuntimeError('Application output escaped the clean bundle')
        if target.exists():shutil.rmtree(target)
    shutil.copytree(PROJECT/'backend',bundle/'app/backend',dirs_exist_ok=True,ignore=ignored)
    shutil.copytree(PROJECT/'frontend/dist',bundle/'app/frontend/dist',dirs_exist_ok=True,ignore=ignored)
    (bundle/'app/scripts').mkdir(parents=True,exist_ok=True)
    shutil.copy2(PROJECT/'scripts/team_portable_launcher.py',bundle/'app/scripts/team_portable_launcher.py')
    sample_dir=PROJECT/'frontend/public/collection-samples'
    if sample_dir.exists():shutil.copytree(sample_dir,bundle/'samples',dirs_exist_ok=True)
    licenses=bundle/'runtime/frontend-licenses';licenses.mkdir(exist_ok=True)
    for package in ['react','react-dom','scheduler']:
        source=PROJECT/'frontend/node_modules'/package/'LICENSE'
        if source.exists():shutil.copy2(source,licenses/(package+'.txt'))
    for name,action in [('启动','start %*'),('停止','stop'),('备份','backup'),('恢复备份','restore %*')]:
        script='@echo off\nchcp 65001 >nul\ncd /d "%~dp0"\n"%~dp0runtime\\python\\python.exe" -I -X utf8 "%~dp0app\\scripts\\team_portable_launcher.py" '+action+'\nif errorlevel 1 pause\n'
        if name in ('备份','恢复备份'):script+='if not errorlevel 1 pause\n'
        (bundle/(name+'.cmd')).write_text(script,encoding='utf-8',newline='\r\n')
    (bundle/'使用说明.txt').write_text('''对齐 · Reconcile Desk 0.3 Windows 本机运行包

完整解压后，双击“启动.cmd”，首次会建立本包自己的数据库并自动打开浏览器。
首次进入请创建管理员账号和密码。系统不会内置演示账号或固定密码。
运行期间请保留启动窗口。双击“停止.cmd”正常停止；关闭浏览器不会停止数据库。
再次启动将继续使用本包 data 里的原文件、规则、业务周期、差异案件及审计记录。

主要流程：上传订单/收款资料 → 确认字段和规则 → 保存可复用配置 → 创建业务周期 →
选择资料并声明完整性 → 确认配置 → 后台核对 → 启用当前结果 → 差异处理与另一账号复核。
多角色不允许同一账号批准自己提交的处置；可在成员管理中建立独立复核账号。
samples 中是合成示例资料，请勿当作真实业务凭据。

本包是独立的新安装目录，不会自动接管已发布 v0.2 的数据库。
请勿把 v0.2 的 data 直接覆盖到本包，也不要覆盖正在运行的任何安装。
移动本包前先停止，再移动整个文件夹。中文目录会临时使用一个空闲盘符，正常停止会释放。
重复点击启动会打开已运行的同一工作区；本机端口被占用时自动使用另一个空闲端口。

备份：先启动工作区，待就绪后双击“备份.cmd”。backups 中生成一致性 PostgreSQL 备份和 SHA-256 清单。
备份包含账号、原文件和业务记录，请保管好，不要公开分享 data、backups 或日志。
恢复：双击“恢复备份.cmd”，输入 .dump 文件完整路径；同名 .json 校验清单须保留。
恢复会新建相邻的 recovered 目录，原目录和备份保持不变，并校验恢复后的源文件及报告哈希。
旧登录会话会失效，恢复副本的任务暂时冻结。先停止原工作区，再启动新副本并按提示输入 RESUME，
才会恢复任务执行；随后使用原账号重新登录。正常迁移本机也可停止后完整复制整个运行目录。
管理员密码遗忘时可以使用本包 Python 运行 app/backend/team/cli.py 的本地恢复命令，
但须在私密环境配置 DATABASE_URL；请勿把凭据发到聊天或发布到 GitHub。

运行条件：Windows 10/11 x64，可写目录。本包内含 Python、PostgreSQL、VC 运行库及网页，无需另装开发环境。
只监听本机回环地址；这份便携包不用于直接对公网或局域网发布服务。
真实模型接口尚未接入，手工确认映射和确定性核对可离线工作。
资料完整性是人工声明；当前周期日期不自动筛选源文件行。
查看详细功能与验收范围：开发源码中的 docs/team-development.md 及 evidence/verification.json。
''',encoding='utf-8-sig')
    (bundle/'第三方组件说明.txt').write_text('''包含 Python 3.12、PostgreSQL 17.11、React 19 及 app/backend/requirements.txt 中的依赖。
Python 许可见 runtime/python/LICENSE.txt，各包许可见 *.dist-info；React 许可见 runtime/frontend-licenses。
PostgreSQL 二进制来自官方链接的 EDB Windows binaries，其许可文件位于 runtime/postgresql/pgsql。
微软 VC 运行库许可和来源记录位于 runtime/vc-runtime-notices。
''',encoding='utf-8-sig')


def package(bundle):
    files=[p for p in bundle.rglob('*') if p.is_file() and p.name!='package-manifest.json'
           and not any(part in {'__pycache__','.pytest_cache','.git'} for part in p.relative_to(bundle).parts)
           and not p.name.endswith(('.pyc','.log')) and not p.name.startswith('.env')]
    hashes={p.relative_to(bundle).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}
    manifest={'version':'0.3.0','package':'Windows x64 local team workflow','sha256':hashes}
    manifest_path=bundle/'package-manifest.json'
    manifest_path.write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    archive=bundle.with_name(bundle.name+'.zip')
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as output:
        for p in files+[manifest_path]:output.write(p,bundle.name+'/'+p.relative_to(bundle).as_posix())
    with zipfile.ZipFile(archive) as output:
        assert output.testzip() is None
        assert not any('/data/' in name or '/backups/' in name or '/.env' in name for name in output.namelist())
    checksum=hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.sha256').write_text(checksum+'  '+archive.name+'\n',encoding='ascii')
    print(json.dumps({'archive':str(archive),'bytes':archive.stat().st_size,'files':len(files)+1,'sha256':checksum,'zip_crc':'PASS'}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-only',action='store_true')
    parser.add_argument('--finish',action='store_true')
    parser.add_argument('--output',type=Path,default=WORKSPACE/'outputs/ReconcileDesk-v0.3.0-Windows-x64')
    parser.add_argument('--runtime-root',type=Path,default=WORKSPACE/'work/runtime')
    parser.add_argument('--python-base',type=Path,default=Path(sys.base_prefix))
    args=parser.parse_args();bundle=args.output.resolve()
    if not bundle.is_relative_to(WORKSPACE.resolve()):raise RuntimeError('Output must stay inside the workspace')
    if (bundle/'data').exists() or (bundle/'backups').exists():raise RuntimeError('Refusing to package a used installation')
    bundle.mkdir(parents=True,exist_ok=True)
    if not args.finish:runtime(bundle,args.python_base,args.runtime_root)
    if args.runtime_only:
        print('Clean runtime prepared: '+str(bundle));return
    application(bundle);package(bundle)


if __name__=='__main__':main()
