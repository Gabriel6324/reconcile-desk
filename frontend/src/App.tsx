import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { api, ApiError, displayCell, download, resourceId, waitOperation } from './api';
import Icon from './Icon';
import type { Capabilities, Comparison, Definition, ImportTable, Mapping, Operation, Page, ResultGroup, ResultStatus, Run, SourceRow, Suggestion } from './types';

const statusLabels: Record<ResultStatus, string> = { equal: '一致', changed: '存在差异', left_only: '仅左表', right_only: '仅右表', duplicate_key: '重复主键', invalid_value: '无效值', blank_key: '空主键' };
const stateLabels: Record<string, string> = { running: '处理中', stopping: '正在停止', completed: '已完成', failed: '失败', interrupted: '已中断' };
const emptyDefinition = (): Definition => ({ key: { left_column_id: '', right_column_id: '', normalization: 'exact' }, comparisons: [{ left_column_id: '', right_column_id: '', type: 'text', normalization: 'exact' }] });
type View = 'overview' | 'workspace' | 'history';
type Draft = { left?: string; right?: string; mapping?: string; definition?: Definition; step?: number };
type Pending = { operation_id: string; kind: 'import' | 'run' | 'export' | 'suggest'; side?: 'left' | 'right' };
const draftKey = 'reconcile-desk.draft.v1';
const pendingKey = 'reconcile-desk.pending.v1';
function readStored<T>(key: string): T | null { try { return JSON.parse(localStorage.getItem(key) || 'null') as T | null; } catch { return null; } }
function store(key: string, value: unknown) { try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, JSON.stringify(value)); } catch { /* Saved server resources remain recoverable through history. */ } }
function date(value?: string) { return value ? new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'; }
function Badge({ status }: { status: string }) { return <span className={`badge status-${status}`}>{statusLabels[status as ResultStatus] || stateLabels[status] || status}</span>; }
function Panel({ title, aside, children, className = '' }: { title: string; aside?: ReactNode; children: ReactNode; className?: string }) { return <section className={`panel ${className}`}><div className="panel-heading"><h2>{title}</h2>{aside}</div>{children}</section>; }

export default function App() {
  const [view, setView] = useState<View>('overview');
  const [step, setStep] = useState(1);
  const [left, setLeft] = useState<ImportTable | null>(null);
  const [right, setRight] = useState<ImportTable | null>(null);
  const [imports, setImports] = useState<ImportTable[]>([]);
  const [mapping, setMapping] = useState<Mapping | null>(null);
  const [definition, setDefinition] = useState<Definition>(emptyDefinition);
  const [suggestion, setSuggestion] = useState<Suggestion | null>(null);
  const [task, setTask] = useState('');
  const [confirmedReview, setConfirmedReview] = useState(false);
  const [run, setRun] = useState<Run | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [runCursor, setRunCursor] = useState<string | null>(null);
  const [operations, setOperations] = useState<Operation[]>([]);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [results, setResults] = useState<Page<ResultGroup>>({ items: [] });
  const [filter, setFilter] = useState('');
  const [query, setQuery] = useState('');
  const [pageNumber, setPageNumber] = useState(1);
  const [resultBusy, setResultBusy] = useState(false);
  const [selected, setSelected] = useState<{ group: ResultGroup; left: SourceRow[]; right: SourceRow[] } | null>(null);
  const [sourcePage, setSourcePage] = useState({ left: 0, right: 0 });
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [ready, setReady] = useState(false);
  const [connected, setConnected] = useState(false);
  const [pending, setPending] = useState<Pending | null>(() => readStored(pendingKey));
  const initialized = useRef(false);
  const resultRequest = useRef(0);
  const drawerClose = useRef<HTMLButtonElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const busyRef = useRef(false);
  useEffect(() => { window.scrollTo({ top: 0, behavior: 'instant' }); }, [view, step]);

  async function refreshLists() {
    const [a, b, c, d] = await Promise.all([api<Page<ImportTable>>('/imports?limit=100'), api<Page<Run>>('/runs?limit=20'), api<Page<Operation>>('/operations?limit=20'), api<Capabilities>('/capabilities')]);
    setImports(a.items); setRuns(b.items); setRunCursor(b.next_cursor || null); setOperations(c.items); setCapabilities(d); setConnected(true);
  }
  async function perform(label: string, action: () => Promise<void>) {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(label); setError(''); setNotice('');
    try { await action(); }
    catch (e) { setError(e instanceof Error ? e.message : '操作未完成，请重试。'); if (e instanceof ApiError && e.code === 'NETWORK_ERROR') setConnected(false); }
    finally { setBusy(''); busyRef.current = false; }
  }
  function setPendingOperation(value: Pending | null) { store(pendingKey, value); setPending(value); }
  async function track(initial: Operation, context: Omit<Pending, 'operation_id'>) {
    setPendingOperation({ operation_id: initial.operation_id, ...context });
    try {
      const completed = await waitOperation(initial);
      setPendingOperation(null);
      return completed;
    } catch (e) {
      if (e instanceof ApiError && e.code !== 'NETWORK_ERROR' && e.code !== 'POLL_TIMEOUT') setPendingOperation(null);
      throw e;
    }
  }
  async function loadImport(id: string) { return api<ImportTable>(`/imports/${encodeURIComponent(id)}`); }
  async function loadRun(id: string) {
    const next = await api<Run>(`/runs/${encodeURIComponent(id)}`);
    const [a, b, m] = await Promise.all([loadImport(next.left_import_id), loadImport(next.right_import_id), api<Mapping>(`/mappings/${next.mapping_id}`)]);
    setLeft(a); setRight(b); setMapping(m); setDefinition(m.definition); setRun(next); setFilter(''); setQuery(''); setStep(3); setView('workspace'); setConfirmedReview(false); setSuggestion(null);
  }
  async function restorePending(context: Pending) {
    const op = await api<Operation>(`/operations/${context.operation_id}`);
    const done = await track(op, context);
    const id = resourceId(done);
    if (context.kind === 'import') { const table = await loadImport(id); if (context.side === 'right') setRight(table); else setLeft(table); setMapping(null); setStep(1); setView('workspace'); }
    else if (context.kind === 'run') await loadRun(id);
    else if (context.kind === 'suggest') { setSuggestion(await api<Suggestion>(`/suggestions/${id}`)); setStep(2); setView('workspace'); }
    else setNotice('导出已准备好，打开对应历史核对即可下载。');
    await refreshLists();
  }
  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;
    void perform('正在连接工作区', async () => {
      await refreshLists();
      const draft = readStored<Draft>(draftKey);
      if (draft) {
        const [a, b, m] = await Promise.all([draft.left ? loadImport(draft.left) : null, draft.right ? loadImport(draft.right) : null, draft.mapping ? api<Mapping>(`/mappings/${draft.mapping}`) : null]);
        setLeft(a); setRight(b); setMapping(m); setDefinition(draft.definition || m?.definition || emptyDefinition()); setStep(Math.min(draft.step || 1, 2));
      }
      const remembered = readStored<Pending>(pendingKey);
      if (remembered) await restorePending(remembered);
    }).finally(() => setReady(true));
  }, []);
  useEffect(() => { if (ready) store(draftKey, { left: left?.id, right: right?.id, mapping: mapping?.mapping_id, definition, step }); }, [ready, left, right, mapping, definition, step]);
  useEffect(() => {
    if (!run || step !== 3) return;
    void fetchResults(run.id || run.run_id!, filter);
  }, [run?.id, filter, step]);
  useEffect(() => {
    if (!selected) return;
    previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    drawerClose.current?.focus();
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSelected(null);
      if (event.key === 'Tab') {
        const focusable = Array.from(document.querySelectorAll<HTMLElement>('.drawer button, .drawer a, .drawer [tabindex="0"]'));
        if (focusable.length && event.shiftKey && document.activeElement === focusable[0]) { event.preventDefault(); focusable[focusable.length - 1].focus(); }
        else if (focusable.length && !event.shiftKey && document.activeElement === focusable[focusable.length - 1]) { event.preventDefault(); focusable[0].focus(); }
      }
    };
    document.addEventListener('keydown', close);
    return () => { document.removeEventListener('keydown', close); previousFocus.current?.focus(); };
  }, [selected?.group.id]);

  function newTask() { setLeft(null); setRight(null); setMapping(null); setDefinition(emptyDefinition()); setSuggestion(null); setRun(null); setStep(1); setView('workspace'); setError(''); setNotice(''); setConfirmedReview(false); setTask(''); }
  function updateDefinition(next: Definition) { setDefinition(next); setConfirmedReview(false); }
  function invalidateSources() { setMapping(null); setRun(null); setSuggestion(null); setDefinition(emptyDefinition()); setConfirmedReview(false); }
  async function uploadFile(file: File, side: 'left' | 'right') {
    if (!/\.(csv|xlsx)$/i.test(file.name)) throw new ApiError('请选择 CSV 或 XLSX 文件。');
    if (file.size > 10 * 1024 * 1024) throw new ApiError('单个文件不能超过 10 MiB。');
    const op = await api<Operation>('/imports', { method: 'POST', file, idempotent: true });
    const done = await track(op, { kind: 'import', side });
    const table = await loadImport(resourceId(done));
    if (side === 'left') setLeft(table); else setRight(table);
    invalidateSources();
  }
  async function chooseImport(id: string, side: 'left' | 'right') {
    const table = id ? await loadImport(id) : null;
    if (side === 'left') setLeft(table); else setRight(table);
    invalidateSources();
  }
  async function useSamples() {
    const samples = await Promise.all(['left', 'right'].map(async side => {
      const response = await fetch(`/samples/${side}.csv`); if (!response.ok) throw new ApiError('示例文件暂时不可用。');
      return new File([await response.blob()], side === 'left' ? '示例_业务台账.csv' : '示例_收款记录.csv', { type: 'text/csv' });
    }));
    setView('workspace'); setStep(1);
    await uploadFile(samples[0], 'left'); await uploadFile(samples[1], 'right'); await refreshLists();
    setNotice('已导入明确标注的示例文件。请继续选择主键和核对字段，结果由实际计算生成。');
  }
  async function ensureMapping(): Promise<Mapping> {
    if (!left || !right) throw new ApiError('请先选择两份表格。');
    let saved = mapping;
    if (!saved) saved = await api<Mapping>('/mappings', { method: 'POST', body: { left_import_id: left.id, right_import_id: right.id, definition } });
    else if (JSON.stringify(saved.definition) !== JSON.stringify(definition)) saved = await api<Mapping>(`/mappings/${saved.mapping_id}`, { method: 'PUT', body: { expected_revision: saved.current_revision, definition } });
    setMapping(saved); setDefinition(saved.definition); return saved;
  }
  async function reconcile() {
    const saved = await ensureMapping();
    const confirmed = await api<Mapping>(`/mappings/${saved.mapping_id}/confirm`, { method: 'POST', body: { expected_revision: saved.current_revision } });
    setMapping(confirmed);
    const op = await api<Operation>('/runs', { method: 'POST', idempotent: true, body: { mapping_id: confirmed.mapping_id, expected_revision: confirmed.current_revision } });
    const done = await track(op, { kind: 'run' });
    await loadRun(resourceId(done)); await refreshLists();
  }
  async function requestSuggestion() {
    const saved = await ensureMapping();
    const op = await api<Operation>(`/mappings/${saved.mapping_id}/suggest`, { method: 'POST', idempotent: true, body: { expected_revision: saved.current_revision, task } });
    const done = await track(op, { kind: 'suggest' });
    setSuggestion(await api<Suggestion>(`/suggestions/${resourceId(done)}`)); await refreshLists();
  }
  async function applySuggestion() {
    if (!mapping || !suggestion) return;
    const saved = await api<Mapping>(`/mappings/${mapping.mapping_id}/apply-suggestion`, { method: 'POST', body: { expected_revision: mapping.current_revision, suggestion_id: suggestion.suggestion_id } });
    setMapping(saved); setDefinition(saved.definition); setConfirmedReview(false); setSuggestion(null); setNotice('建议已填入，请逐项检查后确认核对。');
  }
  async function fetchResults(id: string, status: string, cursor?: string) {
    const request = ++resultRequest.current; setResultBusy(true);
    try {
      const params = new URLSearchParams({ limit: '50' }); if (status) params.set('status', status); if (cursor) params.set('cursor', cursor);
      const page = await api<Page<ResultGroup>>(`/runs/${id}/results?${params}`);
      if (request === resultRequest.current) { setResults(page); setPageNumber(n => cursor ? n + 1 : 1); }
    } catch (e) { if (request === resultRequest.current) setError(e instanceof Error ? e.message : '读取结果失败'); }
    finally { if (request === resultRequest.current) setResultBusy(false); }
  }
  async function inspect(group: ResultGroup) {
    if (!left || !right) return;
    const [a, b] = await Promise.all([Promise.all(group.left_rows.slice(0, 10).map(id => api<SourceRow>(`/imports/${left.id}/rows/${id}`))), Promise.all(group.right_rows.slice(0, 10).map(id => api<SourceRow>(`/imports/${right.id}/rows/${id}`)))]);
    setSourcePage({ left: 0, right: 0 });
    setSelected({ group, left: a, right: b });
  }
  async function turnSourcePage(side: 'left' | 'right', offset: number) {
    const table = side === 'left' ? left : right;
    if (!selected || !table) return;
    const rows = await Promise.all(selected.group[`${side}_rows`].slice(offset, offset + 10).map(id => api<SourceRow>(`/imports/${table.id}/rows/${id}`)));
    setSelected(current => current ? { ...current, [side]: rows } : null);
    setSourcePage(current => ({ ...current, [side]: offset }));
  }
  async function exportRun() {
    if (!run) return;
    const id = run.id || run.run_id!;
    const op = await api<Operation>(`/runs/${id}/exports`, { method: 'POST', idempotent: true, body: {} });
    await track(op, { kind: 'export' });
    await download(`/runs/${id}/export`, `对齐_核对结果_${id.slice(0, 8)}.xlsx`); setNotice('导出已下载，包含全部结果与来源。界面筛选不会缩减导出范围。');
  }

  const comparisonsValid = definition.comparisons.length > 0 && definition.comparisons.every(x => x.left_column_id && x.right_column_id);
  const mappingValid = Boolean(definition.key.left_column_id && definition.key.right_column_id && comparisonsValid);
  const counts = run?.summary?.counts || {};
  const unresolved = run?.summary?.unresolved_rows || 0;
  const visibleResults = results.items.filter(x => !query || (x.key_text || '').toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const activeOperations = operations.filter(x => x.state === 'running' || x.state === 'stopping');
  const latestRun = runs.find(x => x.state === 'completed');

  function renderRuns(items: Run[]) {
    if (!items.length) return <div className="empty-state"><span className="empty-icon"><Icon name="history" size={27} /></span><h3>第一份核对，从这里开始</h3><p>完成核对后，结果与原始来源会保存在这里。</p><button className="button primary" onClick={newTask} disabled={!!busy}><Icon name="plus" />新建核对</button></div>;
    return <div className="table-scroll"><table className="history-table"><thead><tr><th>核对记录</th><th>状态</th><th>一致 / 差异</th><th>创建时间</th><th><span className="sr-only">操作</span></th></tr></thead><tbody>{items.map(item => <tr key={item.id}><td><span className="record-title"><Icon name="rows" /><span>核对 {item.id.slice(0, 8)}<small>两份表格 · 已保存的结果</small></span></span></td><td><Badge status={item.state} /></td><td>{item.summary ? `${item.summary.counts?.equal || 0} / ${item.summary.counts?.changed || 0}` : '—'}</td><td className="muted">{date(item.created_at)}</td><td><button className="text-button" disabled={!!busy || item.state !== 'completed'} onClick={() => void perform('正在打开核对结果', () => loadRun(item.id))}>查看<Icon name="chevron" size={15} /></button></td></tr>)}</tbody></table></div>;
  }
  function uploadCard(side: 'left' | 'right', table: ImportTable | null) {
    const label = side === 'left' ? '左表' : '右表';
    return <section className="source-card"><div className="source-title"><span className={`source-letter ${side}`}>{side === 'left' ? 'A' : 'B'}</span><div><h3>{label}</h3><p>{side === 'left' ? '作为核对的基准资料' : '需要与左表核对的资料'}</p></div>{table && <span className="badge status-equal">已导入</span>}</div>
      {table ? <div className="file-loaded"><Icon name="file" size={27} /><div><strong title={table.filename}>{table.filename}</strong><span>{table.row_count.toLocaleString()} 行 · {table.columns.length} 列</span></div><label className={`text-button file-replace ${busy ? 'disabled' : ''}`}>更换<input type="file" accept=".csv,.xlsx" disabled={!!busy} aria-label={`更换${label}`} onChange={e => { const file = e.target.files?.[0]; if (file) void perform(`正在导入${label}`, async () => { await uploadFile(file, side); await refreshLists(); }); e.target.value = ''; }} /></label></div>
        : <label className={`upload-zone ${busy ? 'disabled' : ''}`}><input type="file" accept=".csv,.xlsx" aria-label={`上传${label}`} disabled={!!busy} onChange={e => { const file = e.target.files?.[0]; if (file) void perform(`正在导入${label}`, async () => { await uploadFile(file, side); await refreshLists(); }); e.target.value = ''; }} /><span className="upload-icon"><Icon name="upload" size={24} /></span><strong>点击选择{label}文件</strong><span>CSV / XLSX · 单个文件不超过 10 MiB</span></label>}
      <div className="saved-import"><label htmlFor={`existing-${side}`}>或选择已导入的资料</label><select id={`existing-${side}`} value={table?.id || ''} disabled={!!busy} onChange={e => void perform('正在读取资料', () => chooseImport(e.target.value, side))}><option value="">选择资料…</option>{imports.filter(x => x.state === 'completed' || x.state === 'ready').map(x => <option key={x.id} value={x.id}>{x.filename} · {x.row_count} 行</option>)}</select></div>
      {table && <><div className="preview-label">数据预览 <span>前 {Math.min(5, table.rows.length)} 行</span></div><div className="table-scroll preview-table" tabIndex={0} aria-label={`${label}预览`}><table><thead><tr><th>原始行</th>{table.columns.map(c => <th key={c.id}>{c.name || '（空表头）'}<small>第 {c.ordinal || table.columns.indexOf(c) + 1} 列</small></th>)}</tr></thead><tbody>{table.rows.slice(0, 5).map(row => <tr key={row.id}><td className="row-number">{row.source?.row || row.ordinal + 1}</td>{table.columns.map(c => <td key={c.id} title={displayCell(row.cells[c.id])}>{displayCell(row.cells[c.id])}</td>)}</tr>)}</tbody></table></div>{!table.rows.length && <p className="inline-note">该表只有表头，没有数据行。</p>}</>}
    </section>;
  }
  function columnSelect(side: 'left' | 'right', value: string, label: string, onChange: (value: string) => void) {
    const columns = side === 'left' ? left?.columns : right?.columns;
    return <select aria-label={label} value={value} disabled={!!busy} onChange={e => onChange(e.target.value)}><option value="">选择{side === 'left' ? '左表' : '右表'}字段</option>{columns?.map((c, i) => <option value={c.id} key={c.id}>{c.name || '（空表头）'} · 第 {i + 1} 列</option>)}</select>;
  }
  function changeComparison(index: number, change: Partial<Comparison>) { updateDefinition({ ...definition, comparisons: definition.comparisons.map((x, i) => i === index ? { ...x, ...change } : x) }); }
  function ignored(side: 'left' | 'right') {
    const columnKey = side === 'left' ? 'left_column_id' : 'right_column_id';
    const used = new Set([definition.key[columnKey], ...definition.comparisons.map(x => x[columnKey])]);
    return (side === 'left' ? left : right)?.columns.filter(c => !used.has(c.id)).map(c => c.name || '（空表头）').join('、') || '无';
  }
  function columnName(side: 'left' | 'right', id: unknown) {
    return (side === 'left' ? left : right)?.columns.find(column => column.id === id)?.name || String(id || '未选择');
  }
  function comparisonDetail(comparison: Record<string, unknown>, index: number) {
    type Evidence = { raw?: unknown; tag?: string; source?: { row?: number }; normalized?: { tag?: string; value?: unknown } | null };
    const a = comparison.left as Evidence | null;
    const b = comparison.right as Evidence | null;
    return <div className="comparison-detail" key={index}>
      <div><span>核对字段</span><strong>{columnName('left', comparison.left_column_id)} ↔ {columnName('right', comparison.right_column_id)}</strong></div>
      <div><span>比较方式</span><strong>{comparison.type === 'decimal' ? '精确数值' : '文本'} · {comparison.normalization === 'trim' ? '去除首尾空白' : '保留原样'}</strong></div>
      <div><span>比较状态</span><strong>{comparison.status === 'unpaired' ? '无法直接配对' : statusLabels[comparison.status as ResultStatus] || '未比较'}</strong></div>
      <div><span>左表原值</span><strong>{a ? displayCell(a) : '无唯一对应值'}{a?.source?.row && <small className="value-location">原始第 {a.source.row} 行</small>}</strong></div>
      <div><span>右表原值</span><strong>{b ? displayCell(b) : '无唯一对应值'}{b?.source?.row && <small className="value-location">原始第 {b.source.row} 行</small>}</strong></div>
      {comparison.type === 'decimal' && <div><span>差额（右 − 左）</span><strong className="decimal-difference">{comparison.difference === null || comparison.difference === undefined ? '无法计算' : String(comparison.difference)}</strong></div>}
    </div>;
  }
  function issueDetail(issue: unknown, index: number) {
    if (!issue || typeof issue !== 'object') return <p key={index}>{String(issue)}</p>;
    const item = issue as { side?: string; row_id?: string; column_id?: string; code?: string; raw?: unknown; tag?: string; source?: { row?: number } };
    const explanations: Record<string, string> = { EXPECTED_TEXT: '需要文本类型', EXPECTED_DECIMAL: '此值无法作为十进制数值', INVALID_DECIMAL_SYNTAX: '数值格式不符合约定', DECIMAL_OUT_OF_RANGE: '数值位数或小数精度超出范围' };
    return <div className="issue-detail" key={index}><strong>{item.side === 'left' ? '左表' : '右表'} · 原始第 {item.source?.row || '?'} 行 · {columnName(item.side === 'left' ? 'left' : 'right', item.column_id)}</strong><p>{explanations[item.code || ''] || item.code}：{displayCell(item)}</p></div>;
  }

  return <div className="app-shell">
    <aside className="sidebar"><a className="brand" href="#" onClick={e => { e.preventDefault(); setView('overview'); }} aria-label="对齐工作区首页"><span className="brand-symbol"><i /><i /><i /></span><span>对齐<small>RECONCILE DESK</small></span></a><div className="workspace-label"><span className="workspace-avatar">个</span><div>个人工作区<small>本地资料 · 自主核对</small></div><span className="local-tag">LOCAL</span></div>
      <button className="button primary new-button" onClick={newTask} disabled={!!busy}><Icon name="plus" />新建核对</button>
      <nav aria-label="主导航"><button className={view === 'overview' ? 'active' : ''} onClick={() => setView('overview')}><Icon name="grid" />工作区概览</button><button className={view === 'workspace' ? 'active' : ''} onClick={() => setView('workspace')}><Icon name="rows" />当前核对{(left || right) && <span className="nav-dot" />}</button><button className={view === 'history' ? 'active' : ''} onClick={() => { setView('history'); void perform('正在刷新历史', refreshLists); }}><Icon name="history" />核对历史</button></nav>
      <div className="sidebar-note"><span className="note-mark">A <Icon name="link" size={16} /> B</span><strong>每一处差异，都有出处。</strong><p>从原始表格到核对结果，<br />保留完整的数据线索。</p></div><div className="sidebar-bottom"><span className={`connection-dot ${connected ? 'online' : ''}`} />{connected ? '工作区已连接' : '正在等待服务'}<span>v0.1</span></div>
    </aside>
    <div className="main-shell"><header className="topbar"><div className="breadcrumb">个人工作区 <Icon name="chevron" size={14} /><strong>{view === 'overview' ? '概览' : view === 'history' ? '核对历史' : '当前核对'}</strong></div><span className="private-label"><span className="connection-dot online" />本地工作区</span></header><main>
      {error && <div className="message error" role="alert"><Icon name="alert" /><div><strong>操作未完成</strong><p>{error}</p></div><button className="icon-button" aria-label="关闭错误提示" onClick={() => setError('')}><Icon name="close" /></button></div>}
      {notice && <div className="message success" role="status"><Icon name="check" /><p>{notice}</p><button className="icon-button" aria-label="关闭提示" onClick={() => setNotice('')}><Icon name="close" /></button></div>}
      {busy && <div className="busy-strip" role="status"><span className="spinner" />{busy}<span>请稍候</span></div>}
      {pending && !busy && <div className="message warning"><Icon name="history" /><p>有一个已提交的操作需要恢复查看。重新查询状态不会重复执行。</p><button className="button small" onClick={() => void perform('正在恢复操作', () => restorePending(pending))}>恢复查看</button></div>}
      {!connected && !busy && <div className="message warning"><Icon name="refresh" /><p>请确认本地服务已启动，然后重新连接。</p><button className="button small" onClick={() => void perform('正在连接工作区', refreshLists)}>重新连接</button></div>}

      {view === 'overview' && <><div className="page-heading"><div><span className="eyebrow">WORKSPACE OVERVIEW</span><h1>把差异，核对清楚。</h1><p>两份表格，一次逐行核对。让每个结果都可查、可解释。</p></div><button className="button secondary" onClick={() => void perform('正在导入示例数据', useSamples)} disabled={!!busy || !connected}><Icon name="file" />使用示例数据<Icon name="arrow" size={16} /></button></div>
        <div className="overview-stats"><div className="stat-card"><span>最近保存的核对</span><strong>{runs.length}{runCursor ? '+' : ''}<small>次</small></strong><p>工作区真实记录</p><Icon name="history" size={24} /></div><div className="stat-card"><span>可用资料</span><strong>{imports.filter(x => x.state === 'completed' || x.state === 'ready').length}<small>份</small></strong><p>支持重复引用与来源查看</p><Icon name="file" size={24} /></div><div className="stat-card accent"><span>字段匹配方式</span><strong className="text-stat">{capabilities?.model.configured ? 'AI 建议 + 人工确认' : '手动确认'}</strong><p>{capabilities?.model.configured ? '每次建议由你明确发起' : '模型未配置，核对功能可正常使用'}</p><Icon name="link" size={24} /></div></div>
        <section className="start-banner"><div><span className="eyebrow">START A RECONCILIATION</span><h2>从两份表格开始</h2><p>选定对应字段，定位缺项、数值差异和异常主键。</p><button className="button primary" onClick={newTask} disabled={!!busy}>开始新的核对<Icon name="arrow" size={17} /></button></div><div className="flow-illustration" aria-hidden="true"><div className="mini-sheet"><span>A <i>业务台账</i></span><b /><b /><b className="highlight" /><b /></div><span className="flow-connector"><Icon name="link" size={25} /></span><div className="mini-sheet"><span>B <i>收款记录</i></span><b /><b /><b className="highlight" /><b /></div></div></section>
        <Panel title="最近核对" aside={<button className="text-button" onClick={() => setView('history')}>查看全部<Icon name="chevron" size={15} /></button>}>{renderRuns(runs.slice(0, 5))}</Panel>
        <div className="principles"><div><span>01</span><strong>明确核对范围</strong><p>全部数据行参与，字段由你选择。</p></div><div><span>02</span><strong>保留原始值</strong><p>异常独立标记，差异逐项呈现。</p></div><div><span>03</span><strong>随时回到来源</strong><p>查看原始行，导出完整核对结果。</p></div></div>
      </>}

      {view === 'history' && <><div className="page-heading"><div><span className="eyebrow">RECONCILIATION HISTORY</span><h1>每一次核对，都留有记录。</h1><p>已完成的结果固定保存，后续修改字段不会改变历史结果。</p></div><button className="button secondary" disabled={!!busy} onClick={() => void perform('正在刷新历史', refreshLists)}><Icon name="refresh" />刷新</button></div><Panel title="核对记录" aside={<span className="muted">最近 {runs.length} 条</span>}>{renderRuns(runs)}{runCursor && <div className="pagination"><button className="button small" disabled={!!busy} onClick={() => void perform('正在读取更多记录', async () => { const next = await api<Page<Run>>(`/runs?limit=20&cursor=${encodeURIComponent(runCursor)}`); setRuns(x => [...x, ...next.items]); setRunCursor(next.next_cursor || null); })}>加载更多</button></div>}</Panel><Panel title="最近操作" aside={<span className="muted">刷新后可找回已提交的操作</span>}><div className="operation-list">{operations.length ? operations.map(op => <div key={op.operation_id}><span><strong>{({ import: '导入资料', run: '执行核对', reconcile: '执行核对', export: '准备导出', suggest: '字段建议', model: '字段建议', model_suggestion: '字段建议' } as Record<string, string>)[op.kind || ''] || '工作区操作'}</strong><small>{op.operation_id.slice(0, 8)} · {date(op.created_at)}</small>{op.error_message && <small className="error-text">{op.error_message}</small>}</span><Badge status={op.state} />{op.state === 'completed' && (op.kind === 'run' || op.kind === 'reconcile') && <button className="text-button" disabled={!!busy} onClick={() => void perform('正在读取结果', () => loadRun(resourceId(op)))}>查看结果</button>}</div>) : <p className="inline-note">还没有操作记录。</p>}</div></Panel></>}

      {view === 'workspace' && <><div className="page-heading workspace-heading"><div><span className="eyebrow">RECONCILIATION WORKSPACE</span><h1>{step === 3 ? '核对结果' : '新建核对'}</h1><p>{step === 1 ? '导入两份资料，先看看它们的原始内容。' : step === 2 ? '明确主键与比较字段，再开始核对。' : '结果已保存。点击任意一组，即可查看两侧原始行。'}</p></div>{step === 3 && run && <button className="button primary" disabled={!!busy} onClick={() => void perform('正在准备完整导出', exportRun)}><Icon name="download" />导出全部结果</button>}</div>
        <div className="stepper" aria-label="核对步骤">{['导入资料', '确认字段', '查看结果'].map((text, i) => <div className={step === i + 1 ? 'current' : step > i + 1 ? 'done' : ''} key={text}><span>{step > i + 1 ? <Icon name="check" size={15} /> : `0${i + 1}`}</span><strong>{text}</strong>{i < 2 && <i />}</div>)}</div>
        {step === 1 && <><div className="section-intro"><h2>选择两份资料</h2><button className="text-button" disabled={!!busy || !connected} onClick={() => void perform('正在导入示例数据', useSamples)}>使用示例数据<Icon name="arrow" size={15} /></button></div><div className="source-grid">{uploadCard('left', left)}{uploadCard('right', right)}</div><div className="format-note"><Icon name="alert" size={17} /><p>第一行作为表头；XLSX 仅支持单个工作表，不接受公式。所有数据行都会参与核对。</p></div><div className="step-footer"><span>{left && right ? '两份资料已就绪，可以选择对应字段。' : '导入完成后，可预览并确认资料内容。'}</span><button className="button primary" disabled={!!busy || !left || !right} onClick={() => { setStep(2); setConfirmedReview(false); }}>下一步：确认字段<Icon name="arrow" size={17} /></button></div></>}
        {step === 2 && left && right && <><div className="source-pair"><span><b>A</b>{left.filename}<small>{left.row_count} 行</small></span><Icon name="link" /><span><b>B</b>{right.filename}<small>{right.row_count} 行</small></span></div>
          <div className="mapping-layout"><div><Panel title="01  主键对应" aside={<span className="muted">用于确定哪两行属于同一条记录</span>}><div className="key-mapping">{columnSelect('left', definition.key.left_column_id, '左表主键', value => updateDefinition({ ...definition, key: { ...definition.key, left_column_id: value } }))}<Icon name="link" />{columnSelect('right', definition.key.right_column_id, '右表主键', value => updateDefinition({ ...definition, key: { ...definition.key, right_column_id: value } }))}</div><div className="key-options"><label>主键处理<select aria-label="主键空白处理" value={definition.key.normalization} disabled={!!busy} onChange={e => updateDefinition({ ...definition, key: { ...definition.key, normalization: e.target.value as 'exact' | 'trim' } })}><option value="exact">按原文精确匹配</option><option value="trim">去除首尾空格、制表符及换行</option></select></label><p>主键按文本比较；空主键与重复主键单独列出，不会自动配对。</p></div></Panel>
          <Panel title="02  核对字段" aside={<button className="text-button" disabled={!!busy || definition.comparisons.length >= 20} onClick={() => updateDefinition({ ...definition, comparisons: [...definition.comparisons, { left_column_id: '', right_column_id: '', type: 'text', normalization: 'exact' }] })}><Icon name="plus" size={16} />添加字段</button>}><div className="comparison-list"><div className="comparison-labels"><span>左表字段</span><span>右表字段</span><span>比较类型</span><span>空白处理</span><span /></div>{definition.comparisons.map((comparison, index) => <div className="comparison-row" key={index}>{columnSelect('left', comparison.left_column_id, `第${index + 1}组左表字段`, value => changeComparison(index, { left_column_id: value }))}{columnSelect('right', comparison.right_column_id, `第${index + 1}组右表字段`, value => changeComparison(index, { right_column_id: value }))}<select aria-label={`第${index + 1}组比较类型`} value={comparison.type} disabled={!!busy} onChange={e => changeComparison(index, { type: e.target.value as 'text' | 'decimal' })}><option value="text">文本</option><option value="decimal">精确数值</option></select><select aria-label={`第${index + 1}组空白处理`} value={comparison.normalization} disabled={!!busy} onChange={e => changeComparison(index, { normalization: e.target.value as 'exact' | 'trim' })}><option value="exact">保留原样</option><option value="trim">去除首尾空白</option></select><button className="icon-button" aria-label={`删除第${index + 1}组字段`} disabled={!!busy || definition.comparisons.length === 1} onClick={() => updateDefinition({ ...definition, comparisons: definition.comparisons.filter((_, i) => i !== index) })}><Icon name="trash" size={17} /></button></div>)}</div><p className="inline-note">精确数值按十进制比较；空值与格式异常会保留为待处理，不按 0 计算。</p></Panel>
          <Panel title="03  核对范围"><div className="scope-review"><p><strong>全部数据行</strong> · 左表 {left.row_count} 行 / 右表 {right.row_count} 行</p><p><span>左表不参与比较的字段</span>{ignored('left')}</p><p><span>右表不参与比较的字段</span>{ignored('right')}</p><label className="checkbox-label"><input type="checkbox" checked={confirmedReview} disabled={!mappingValid || !!busy} onChange={e => setConfirmedReview(e.target.checked)} /><span>我已检查主键、比较类型及忽略字段，确认按以上范围核对。</span></label></div></Panel></div>
          <aside className="assist-panel"><div className="assist-title"><span><Icon name="spark" /></span><h3>字段建议</h3><span className="assist-badge">辅助</span></div><p>描述字段含义，获取对应关系建议。最终选择始终由你确认。</p><label htmlFor="mapping-task">字段匹配说明</label><textarea id="mapping-task" maxLength={2000} rows={4} disabled={!!busy || !capabilities?.model.configured} value={task} onChange={e => setTask(e.target.value)} placeholder="例如：交易编号对应订单号，实付金额对应到账金额。" /><div className="model-availability"><span className={`connection-dot ${capabilities?.model.configured ? 'online' : ''}`} />{capabilities?.model.configured ? '模型接口已配置' : '模型未配置 · 请手动选择字段'}</div><button className="button secondary full-width" disabled={!!busy || !capabilities?.model.configured} onClick={() => void perform('正在请求字段建议', requestSuggestion)}><Icon name="spark" />获取一次建议</button><p className="assist-footnote">仅提供字段建议，不会执行“只核对某日期”等数据筛选。请求会向配置的模型接口发送字段名与单元格类型，不发送单元格内容。</p>{suggestion && <div className="suggestion-result"><strong>模型建议待确认</strong><p>基于版本 {suggestion.base_revision} 的映射</p>{suggestion.proposal.ambiguities?.map((text, i) => <p key={i}>{text}</p>)}<div className="proposal-fields"><p><span>主键</span>{columnName('left', suggestion.proposal.key?.left_column_id)} ↔ {columnName('right', suggestion.proposal.key?.right_column_id)}</p>{suggestion.proposal.comparisons?.map((pair, i) => <p key={i}><span>{pair.type === 'decimal' ? '精确数值' : '文本'}</span>{columnName('left', pair.left_column_id)} ↔ {columnName('right', pair.right_column_id)}</p>)}</div>{suggestion.base_revision !== mapping?.current_revision && <p className="error-text">该建议对应旧版本，无法直接应用。请重新获取建议。</p>}<button className="button primary small" disabled={!!busy || suggestion.base_revision !== mapping?.current_revision || JSON.stringify(definition) !== JSON.stringify(mapping?.definition)} onClick={() => void perform('正在应用建议', applySuggestion)}>填入建议并检查</button><p>如已手动修改，请先保存草稿，再重新获取建议。</p></div>}</aside></div>
          <div className="step-footer"><button className="text-button" disabled={!!busy} onClick={() => setStep(1)}>返回资料</button><div><button className="button secondary" disabled={!!busy} onClick={() => void perform('正在保存字段草稿', async () => { await ensureMapping(); setNotice('字段草稿已保存。'); })}>保存草稿</button><button className="button primary" disabled={!!busy || !mappingValid || !confirmedReview} onClick={() => void perform('正在执行核对', reconcile)}>确认并核对<Icon name="arrow" size={17} /></button></div></div>
        </>}
        {step === 3 && run && <><div className="result-banner"><span className="result-check"><Icon name={unresolved ? 'alert' : 'check'} size={23} /></span><div><h2>{run.summary?.decision === 'no_data' ? '没有可核对的数据行' : unresolved ? '核对已完成，部分记录需要处理' : '核对已完成'}</h2><p>{left?.filename} <span>↔</span> {right?.filename}</p></div><div className="result-meta">{date(run.created_at)}<small>记录 {run.id.slice(0, 8)}</small></div></div><div className="result-stats">{(['equal', 'changed', 'left_only', 'right_only'] as ResultStatus[]).map(status => <button key={status} onClick={() => setFilter(status)} className={filter === status ? 'selected' : ''}><span>{statusLabels[status]}</span><strong>{counts[status] || 0}<small>组</small></strong><i className={`dot-${status}`} /></button>)}<button onClick={() => setFilter('')}><span>待处理原始行</span><strong>{unresolved}<small>行</small></strong><i className="dot-unresolved" /></button></div>
          <Panel title="逐组核对结果" aside={<span className="muted">{run.summary?.total_groups ?? '—'} 组 · 全部原始行参与</span>}><div className="result-toolbar"><div className="status-filters"><button className={!filter ? 'active' : ''} onClick={() => setFilter('')}>全部</button>{(Object.keys(statusLabels) as ResultStatus[]).map(status => <button key={status} className={filter === status ? 'active' : ''} onClick={() => setFilter(status)}>{statusLabels[status]}<span>{counts[status] || 0}</span></button>)}</div><label className="search-field"><Icon name="search" size={17} /><input value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索当前页主键" aria-label="搜索当前页主键" /></label></div><div className="table-scroll"><table className="results-table" aria-busy={resultBusy}><thead><tr><th>主键</th><th>核对状态</th><th>左表原始行</th><th>右表原始行</th><th>比较详情</th><th /></tr></thead><tbody>{visibleResults.map(group => <tr key={group.id}><td className="key-cell">{group.key_text === '' ? '（空字符串）' : group.key_text ?? '∅'}</td><td><Badge status={group.status} /></td><td>{group.left_rows.length ? group.left_rows.slice(0, 5).join('、') + (group.left_rows.length > 5 ? ` … 共 ${group.left_rows.length} 行` : '') : <span className="muted">无对应行</span>}</td><td>{group.right_rows.length ? group.right_rows.slice(0, 5).join('、') + (group.right_rows.length > 5 ? ` … 共 ${group.right_rows.length} 行` : '') : <span className="muted">无对应行</span>}</td><td><span className="muted">{group.comparisons?.length || 0} 个字段{group.source_cell_issues?.length ? ` · ${group.source_cell_issue_count ?? group.source_cell_issues.length} 项异常` : ''}</span></td><td><button className="text-button" disabled={!!busy} onClick={() => void perform('正在读取原始来源', () => inspect(group))}>查看来源<Icon name="chevron" size={15} /></button></td></tr>)}</tbody></table></div>{!visibleResults.length && <div className="empty-result">{resultBusy ? '正在读取结果…' : query ? '当前页没有匹配该主键的记录。' : '该分类没有记录。'}</div>}<div className="pagination"><span>第 {pageNumber} 页 · 本页 {results.items.length} 组{query && ` · 搜索匹配 ${visibleResults.length} 组`}</span><div><button className="button small" disabled={resultBusy || pageNumber === 1} onClick={() => void fetchResults(run.id, filter)}>返回首页</button><button className="button small" disabled={resultBusy || !results.next_cursor} onClick={() => void fetchResults(run.id, filter, results.next_cursor!)}>下一页<Icon name="chevron" size={14} /></button></div></div></Panel><p className="format-note">“待处理原始行”包含重复主键、空主键、无效值以及单侧记录中的类型异常。它们不会计作已核对一致；界面筛选仅影响当前显示。</p><div className="step-footer"><button className="text-button" disabled={!!busy} onClick={() => { setStep(2); setConfirmedReview(false); setNotice('现在编辑的是当前字段草稿。已保存的历史核对结果保持不变。'); }}>调整字段，再次核对</button><span>结果与原始来源已保存至核对历史</span></div></>}
      </>}
      {activeOperations.length > 0 && !busy && !pending && <p className="inline-note">工作区有 {activeOperations.length} 个操作正在处理，可在核对历史中刷新状态。</p>}
      <footer className="page-footer"><span>对齐 · 让数据说得清楚</span><span>{latestRun ? `最近核对 ${date(latestRun.created_at)}` : '你的第一份核对，准备就绪'}</span></footer>
    </main></div>
    {selected && <div className="drawer-backdrop" onClick={e => { if (e.target === e.currentTarget) setSelected(null); }}><section className="drawer" role="dialog" aria-modal="true" aria-labelledby="source-detail-title"><header><div><span className="eyebrow">SOURCE DETAILS</span><h2 id="source-detail-title">追溯原始记录</h2></div><button ref={drawerClose} className="icon-button" onClick={() => setSelected(null)} aria-label="关闭来源详情"><Icon name="close" size={22} /></button></header><div className="detail-key"><span>主键</span><strong>{selected.group.key_text || '∅'}</strong><Badge status={selected.group.status} /></div><div className="drawer-body"><h3>字段比较</h3>{selected.group.comparisons?.length ? selected.group.comparisons.map(comparisonDetail) : <p className="muted">该组没有可直接比较的成对记录。</p>}{selected.group.source_cell_issues?.length ? <div className="issue-block"><h3>来源异常{selected.group.source_cell_issue_count ? `（共 ${selected.group.source_cell_issue_count} 项）` : ''}</h3>{selected.group.issues_truncated && <p className="issue-limit">仅显示前 20 项异常，完整信息请查看导出。</p>}{selected.group.source_cell_issues.map(issueDetail)}</div> : null}
          {(['left', 'right'] as const).map(side => { const table = side === 'left' ? left : right; return <section className="raw-source" key={side}><h3><span className={`source-letter ${side}`}>{side === 'left' ? 'A' : 'B'}</span>{side === 'left' ? '左表' : '右表'}原始记录</h3><p>{table?.filename}</p>{selected.group[`${side}_rows`].length > 10 && <div className="source-pagination"><span>共 {selected.group[`${side}_rows`].length} 行 · 当前 {sourcePage[side] + 1}–{Math.min(sourcePage[side] + 10, selected.group[`${side}_rows`].length)} 行</span><div><button className="button small" disabled={!!busy || sourcePage[side] === 0} onClick={() => void perform('正在读取来源', () => turnSourcePage(side, sourcePage[side] - 10))}>上一页</button><button className="button small" disabled={!!busy || sourcePage[side] + 10 >= selected.group[`${side}_rows`].length} onClick={() => void perform('正在读取来源', () => turnSourcePage(side, sourcePage[side] + 10))}>下一页</button></div></div>}{selected[side].length ? selected[side].map(row => <div className="raw-row" key={row.id}><div className="raw-row-heading">{row.source?.sheet ? `工作表 ${row.source.sheet} · ` : ''}原始第 {row.source?.row || row.ordinal + 1} 行<span>{row.id}</span></div><dl>{table?.columns.map(column => <div key={column.id}><dt>{column.name || '（空表头）'}<small>第 {column.ordinal || table.columns.indexOf(column) + 1} 列</small></dt><dd>{displayCell(row.cells[column.id])}</dd></div>)}</dl></div>) : <div className="no-source">这一侧没有对应记录。</div>}</section>; })}</div><footer><button className="button secondary" onClick={() => setSelected(null)}>关闭详情</button></footer></section></div>}
  </div>;
}
