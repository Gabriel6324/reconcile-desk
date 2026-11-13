# 对齐 · Reconcile Desk

导入两份表格，明确主键和比较规则，逐行核对并查看来源，导出可追溯的 XLSX。面向单人、本机运行的资料核对工作台。

技术栈：React / TypeScript / FastAPI / PostgreSQL / SQLAlchemy / Alembic / Decimal / Docker Compose / Nginx。

v0.2 新增可保存的字段模板、列重排复用与缺失字段提示，完善手机字段编辑和删除确认，并补齐完整 Docker Compose 交付与保留数据重建验收。

![核对结果与原始资料追溯](evidence/v0.2/results-desktop.png)

## 启动

Windows 用户从 [v0.2.0 Release](https://github.com/Gabriel6324/reconcile-desk/releases/tag/v0.2.0) 下载 `ReconcileDesk-v0.2.0-Windows-x64.zip`，完整解压后双击包内的 `启动.cmd`，等待浏览器自动打开；用完双击 `停止.cmd`。运行包包含 Python、PostgreSQL、VC 运行库和构建后的网页，无需安装开发环境或 Docker。面向 Windows 10 / 11 x64，资料保存在包内 `data` 目录，搬移时先停止再移动整个文件夹。请保留启动窗口。

从 v0.1 升级：先停止旧版并备份整个 `data` 文件夹，再把它复制到已解压的新版根目录。首次启动自动迁移数据库；旧版运行包可留作备份。不要同时启动使用同一份数据的新旧版本。尚未在干净 Windows 虚拟机或其他电脑独立验收；真实模型接口仍待接入，手工映射和模板流程可离线使用。

### 从源码运行

```bash
git clone https://github.com/Gabriel6324/reconcile-desk.git
cd reconcile-desk
```

安装并启动 Docker Desktop（Windows 使用 Linux containers）。在项目目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/init-env.ps1
docker compose up -d --build
```

macOS / Linux 先执行 `sh scripts/init-env.sh`，然后使用同一条 Compose 命令。

打开 <http://127.0.0.1:8080>。数据库仅在容器网络中使用，网页仅绑定本机。首次启动会自动迁移数据库；健康检查通过后网页才启动。

停止：`docker compose stop`。更新代码后：`docker compose up -d --build`。数据保存在 `reconcile_data` 命名卷；保留该卷即可保留历史。不要使用 `docker compose down -v`，它会删除数据库卷。

## 演示流程

1. 新建核对，选择项目 `samples/left-orders.csv` 和 `samples/right-payments.csv`。
2. 主键选择「订单编号」↔「业务单号」，保持精确匹配。
3. 比较「金额」↔「实付金额」，类型选择十进制数；可另加客户与状态的文本比较。
4. 确认字段后执行，查看差异、重复主键、空主键和无效金额。点击结果查看原始行。
5. 导出 XLSX，然后刷新页面，从历史记录打开同一结果。
6. 在结果页输入名称并“保存为模板”；下次导入同类资料，在“确认字段”页选择并应用。列顺序变化时按完整表头名称对应；缺失列会明确提示，当前草稿保留。应用后须重新勾选人工确认。

此样例仅比较金额时的预期：一致 2 组、差异 2 组、仅左 1 组、仅右 1 组、重复主键 1 组、无效值 1 组、空主键 1 组，左右合计 16 行。`9007199254740993.01` 与 `.02` 相差 `0.01`，计算不经浮点数。

页面内置的示例文件另有明确标注，所有展示结果均由后端实际核对生成。

模板保存在本机数据库中，最多 50 个。模板仅保存字段名称及比较规则，不保存原始行值；删除模板需确认，历史核对结果保持不变。

## 输入规则

- CSV：UTF-8（可带 BOM）、逗号分隔、首行表头。XLSX：单工作表、首行表头。
- 每文件最多 10 MiB、10,000 数据行、50 列；每单元格最多 4,000 字符。解析后的文本与压缩包展开体积另有限制。
- 表头不能重复或为空；公式、多表、外部引用、合并数据单元格、非法控制字符等返回明确错误。
- 主键必须来自文本值；重复和空主键单独列出，避免错误配对。
- 文本默认逐字比较。可显式选择仅裁剪 ASCII 空白。金额选择 Decimal；无效或缺失数值保留为待核查，绝不自动当零。
- 导出包含全部结果和来源；页面筛选不改变导出范围。文本按字面写入，不作为公式或自动超链接执行。

## 可选模型配置

未配置模型时，手动映射与核对流程可以独立使用。若有 OpenAI 兼容接口，在本机 `.env` 设置 `LLM_BASE_URL`（以 `/v1` 结尾）、`LLM_MODEL` 和 `LLM_API_KEY`，重建 API 服务。

模型请求由用户主动触发，提供字段建议，须明确应用并复核。原始资料中的指令只作为数据。核对结果由确定性代码计算。模型密钥只留在后端运行环境，勿写入前端或提交 `.env`。

当前用户暂无接口，真实模型响应、计费和最终容器内模型连通性待验收。模拟接口检查不代表真实模型验收。

## 验证边界

实际运行证据保存在 `evidence/verification.json`，历史 v0.1 证据保留在 `evidence/portable`。新版在原有核心、模拟模型、PostgreSQL 集成和接收边界测试基础上，增加模板复用、缺失表头、并发同名、容量回滚、删除保护与旧库迁移检查。

2026-11-13 [对应提交的 CI 验收](https://github.com/Gabriel6324/reconcile-desk/actions/runs/43417463630)通过 134 项后端测试、前端构建及完整 Docker 流程。最终 Windows ZIP 通过 9 项基础浏览器检查和 6 项模板流程检查；旧版数据库升级与正常重启后，历史及导出保持一致，模板可继续用于新核对。详见 `evidence/v0.2`。

Docker 验收在 GitHub Actions 的隔离环境构建真实镜像，经过 Nginx 执行浏览器流程，并保留数据卷重建 PostgreSQL、API 和网页容器。它比较历史、原始资料与导出文件，随后用保存的模板执行新的核对。原计划中其余故障注入场景逐项记录在 `evidence/acceptance-status.json`，完整原始故障矩阵仍有待执行项。

本地开发使用 Python 3.12、Node.js 22.12+ 和 PostgreSQL 17。安装 `backend/requirements.txt`，设置 `DATABASE_URL`，在 backend 下执行 `python -m uvicorn api.main:app --host 127.0.0.1 --port 8000`。在 frontend 下执行 `npm ci` 与 `npm run dev`。

在 backend 下执行 `python -m pytest tests/core tests/api/test_model.py -q` 可检查核心与模拟模型接口。完整测试先设置 `TEST_DATABASE_URL`，数据库名必须为 `reconcile_test`，再执行 `python -m pytest tests -q`。集成测试会重建这个专用测试库的 public schema。

验收结果与对应 CI 提交记录以 `evidence/verification.json` 为准。`python scripts/verify-evidence.py` 检查交付代码是否仍与验收指纹一致，修改后需要重新验证。[GitHub Actions](https://github.com/Gabriel6324/reconcile-desk/actions)分别执行后端测试、前端构建和完整 Docker 交付流程。模拟模型接口测试仍不代表真实模型验收。

浏览器复测脚本为 `scripts/browser-smoke.cjs`，需要安装 Playwright，并设置 `BROWSER_EXECUTABLE` 指向可用的 Chromium / Edge。服务启动后执行 `node scripts/browser-smoke.cjs`，脚本会上传标注样例、生成新记录并核验实际下载。可用 `PLAYWRIGHT_MODULE` 指向已有的 Playwright 模块目录。

模板流程复测使用 `node scripts/browser-templates.cjs`；它会创建明确标记的验收资料与模板，并检查列重排、再次确认、缺失字段、删除确认及手机布局。容器保留数据重建前后分别执行 `python scripts/check-persistence.py capture --require-template` 和 `verify --require-template`；只应在专用验收工作区运行这些会写入示例数据的脚本。

项目用于有明确边界的本地核对展示；当前版本不提供多人权限、模糊匹配、财务记账或聚合对账。

`Start-Local.ps1` 是原开发工作区专用入口，依赖同一工作区的 `work/runtime`。下载仓库的用户请使用上面的运行包或源码运行步骤。
