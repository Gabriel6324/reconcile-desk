import type { Operation } from './types';

export class ApiError extends Error {
  code: string;
  constructor(message: string, code = 'REQUEST_FAILED') { super(message); this.name = 'ApiError'; this.code = code; }
}

async function checked(response: Response): Promise<Response> {
  if (!response.ok) {
    let error: { message?: string; detail?: unknown; code?: string } = {};
    try { error = await response.json(); } catch { /* Non-JSON proxy errors remain explicit. */ }
    throw new ApiError(error.message || (typeof error.detail === 'string' ? error.detail : `请求未完成（HTTP ${response.status}）`), error.code || `HTTP_${response.status}`);
  }
  return response;
}

export async function api<T>(path: string, options: { method?: string; body?: unknown; file?: File; idempotent?: boolean } = {}): Promise<T> {
  const headers: Record<string, string> = { 'X-Reconcile-Client': 'web' };
  if (options.idempotent) headers['Idempotency-Key'] = crypto.randomUUID();
  let body: BodyInit | undefined;
  if (options.file) { const form = new FormData(); form.append('file', options.file); body = form; }
  else if (options.body !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(options.body); }
  try {
    const response = await checked(await fetch(path.startsWith('/api/') ? path : `/api${path}`, { method: options.method || 'GET', headers, body, credentials: 'same-origin' }));
    return response.json() as Promise<T>;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError('暂时无法连接工作区。请检查服务状态；已保存的任务可在恢复连接后查看。', 'NETWORK_ERROR');
  }
}

export async function waitOperation(initial: Operation): Promise<Operation> {
  let current = initial;
  const deadline = Date.now() + 180_000;
  while (current.state === 'running' || current.state === 'stopping') {
    if (Date.now() > deadline) throw new ApiError('此操作仍在处理中。可以稍后从任务记录恢复查看；本次等待没有取消服务端任务。', 'POLL_TIMEOUT');
    await new Promise(resolve => setTimeout(resolve, 700));
    current = await api<Operation>(`/operations/${encodeURIComponent(current.operation_id)}`);
  }
  if (current.state !== 'completed') throw new ApiError(current.error_message || (current.state === 'interrupted' ? '操作已中断，请检查状态后重新发起。' : '操作未完成，请检查输入或服务状态。'), current.error_code || current.state);
  return current;
}

export function resourceId(operation: Operation): string {
  const id = operation.result_resource?.id || operation.resource_id;
  if (!id) throw new ApiError('服务返回了完成状态，但缺少结果标识。请从任务记录核查。', 'MISSING_RESULT');
  return id;
}

export async function download(path: string, name: string): Promise<void> {
  const response = await checked(await fetch(path.startsWith('/api/') ? path : `/api${path}`, { credentials: 'same-origin' }));
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement('a'); anchor.href = objectUrl; anchor.download = name; document.body.appendChild(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 20_000);
}

export function displayCell(cell?: { raw?: unknown; value?: unknown; tag?: string; type?: string }): string {
  if (!cell || cell.tag === 'null' || cell.type === 'null') return '∅';
  const value = cell.raw !== undefined ? cell.raw : cell.value;
  return value === null || value === undefined ? '∅' : value === '' ? '（空字符串）' : String(value);
}
