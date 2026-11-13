// Real browser acceptance of saved mapping templates against a running workspace.
// This creates labelled sample imports, two runs and one retained sample template.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const crypto = require('node:crypto');

const project = path.resolve(__dirname, '..');
const evidence = process.env.EVIDENCE_DIR || path.join(project, 'evidence');
const baseUrl = (process.env.BASE_URL || 'http://127.0.0.1:8080').replace(/\/$/, '');
const sampleDir = process.env.SAMPLE_DIR || path.join(project, 'samples');
const suffix = crypto.randomUUID().slice(0, 8);
const retainedName = `验收_订单金额_${suffix}`;
const disposableName = `验收_删除确认_${suffix}`;
fs.mkdirSync(evidence, { recursive: true });

// These controlled fixtures contain four plain CSV columns, without quoted cells.
function reorderedSample(filename, order) {
  const lines = fs.readFileSync(path.join(sampleDir, filename), 'utf8').replace(/^\uFEFF/, '').trimEnd().split(/\r?\n/);
  return Buffer.from(lines.map(line => {
    const cells = line.split(',');
    assert.equal(cells.length, 4, 'Sample shape changed; update the acceptance fixture');
    assert(!line.includes('"'), 'Use a CSV parser if the controlled fixture gains quoted cells');
    return order.map(index => cells[index]).join(',');
  }).join('\n') + '\n');
}

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
  const page = await context.newPage();
  const errors = [];
  const checks = [];
  page.on('pageerror', error => errors.push(error.message));
  const mark = message => { checks.push(message); console.log(message); };
  const headers = { 'X-Reconcile-Client': 'web', Origin: baseUrl };

  async function api(route, options = {}) {
    const response = await context.request.fetch(baseUrl + '/api' + route, {
      ...options, headers: { ...headers, ...options.headers }, timeout: 30000,
    });
    assert(response.ok(), `${options.method || 'GET'} ${route}: HTTP ${response.status()} ${(await response.text()).slice(0, 500)}`);
    return response.json();
  }
  async function waitOperation(initial) {
    let op = initial;
    const deadline = Date.now() + 60000;
    while (op.state === 'running' || op.state === 'stopping') {
      assert(Date.now() < deadline, `Operation timed out: ${op.operation_id}`);
      await new Promise(resolve => setTimeout(resolve, 150));
      op = await api('/operations/' + op.operation_id);
    }
    assert.equal(op.state, 'completed', JSON.stringify(op));
    return op.resource_id || op.result_resource?.id;
  }
  async function uploadApi(filename, buffer) {
    return waitOperation(await api('/imports', { method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() },
      multipart: { file: { name: filename, mimeType: 'text/csv', buffer } } }));
  }
  function waitPost(route) {
    return page.waitForResponse(response => response.url() === baseUrl + '/api' + route && response.request().method() === 'POST');
  }
  async function saveTemplate(name) {
    await page.getByLabel('模板名称', { exact: true }).fill(name);
    const responsePromise = waitPost('/templates');
    await page.getByRole('button', { name: '保存为模板', exact: true }).click();
    const response = await responsePromise;
    assert(response.ok(), `Saving template failed: ${await response.text()}`);
    const template = await response.json();
    await page.getByRole('status').filter({ hasText: `模板“${name}”已保存` }).waitFor();
    return template;
  }
  async function uploadUiPair(leftFile, rightFile) {
    await page.getByRole('button', { name: '新建核对', exact: true }).first().click();
    await page.getByLabel('上传左表', { exact: true }).setInputFiles(leftFile);
    await page.getByText(leftFile.name, { exact: true }).first().waitFor();
    await page.getByLabel('上传右表', { exact: true }).setInputFiles(rightFile);
    await page.getByRole('button', { name: '下一步：确认字段', exact: true }).click();
    await page.getByLabel('已保存的字段模板', { exact: true }).waitFor();
  }
  async function draftValues() {
    const fields = ['左表主键', '右表主键', '第1组左表字段', '第1组右表字段', '第1组比较类型'];
    return Promise.all(fields.map(label => page.getByLabel(label, { exact: true }).inputValue()));
  }
  try {
    // Seed only the existing baseline behavior through HTTP; every template mutation
    // and the reordered-source run below uses the actual browser controls.
    const leftId = await uploadApi(`验收_原始左表_${suffix}.csv`, fs.readFileSync(path.join(sampleDir, 'left-orders.csv')));
    const rightId = await uploadApi(`验收_原始右表_${suffix}.csv`, fs.readFileSync(path.join(sampleDir, 'right-payments.csv')));
    const definition = { key: { left_column_id: 'c1', right_column_id: 'c1', normalization: 'exact' },
      comparisons: [{ left_column_id: 'c3', right_column_id: 'c3', type: 'decimal', normalization: 'exact' }] };
    const mapping = await api('/mappings', { method: 'POST', data: { left_import_id: leftId, right_import_id: rightId, definition } });
    await api(`/mappings/${mapping.mapping_id}/confirm`, { method: 'POST', data: { expected_revision: mapping.current_revision } });
    const originalRunId = await waitOperation(await api('/runs', { method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() },
      data: { mapping_id: mapping.mapping_id, expected_revision: mapping.current_revision } }));
    const originalRun = await api('/runs/' + originalRunId);
    assert.equal(originalRun.summary.total_groups, 9);

    await page.goto(baseUrl);
    await page.getByRole('button', { name: '核对历史', exact: true }).click();
    await page.locator('.history-table tbody tr').filter({ hasText: originalRunId.slice(0, 8) })
      .getByRole('button', { name: '查看', exact: true }).click();
    await page.getByRole('heading', { name: '核对结果', exact: true }).waitFor();
    const retained = await saveTemplate(retainedName);
    const disposable = await saveTemplate(disposableName);
    await page.reload();
    const persisted = await api('/templates?limit=100');
    assert(persisted.items.some(item => item.id === retained.id && item.name === retainedName));
    mark('A confirmed historical result saves named templates, which survive page refresh');

    const leftReordered = { name: `验收_重排列左表_${suffix}.csv`, mimeType: 'text/csv', buffer: reorderedSample('left-orders.csv', [2, 0, 3, 1]) };
    const rightReordered = { name: `验收_重排列右表_${suffix}.csv`, mimeType: 'text/csv', buffer: reorderedSample('right-payments.csv', [3, 2, 1, 0]) };
    await uploadUiPair(leftReordered, rightReordered);
    // A pre-existing review tick must never authorize newly applied template fields.
    await page.getByLabel('左表主键', { exact: true }).selectOption('c2');
    await page.getByLabel('右表主键', { exact: true }).selectOption('c4');
    await page.getByLabel('第1组左表字段', { exact: true }).selectOption('c4');
    await page.getByLabel('第1组右表字段', { exact: true }).selectOption('c3');
    await page.getByRole('checkbox').check();
    await page.getByLabel('已保存的字段模板', { exact: true }).selectOption(retained.id);
    await page.getByRole('button', { name: '应用模板', exact: true }).click();
    await page.getByRole('status').filter({ hasText: `已应用模板“${retainedName}”` }).waitFor();
    assert.deepEqual(await draftValues(), ['c2', 'c4', 'c1', 'c2', 'decimal']);
    assert.equal(await page.getByRole('checkbox').isChecked(), false);
    assert.equal(await page.getByRole('button', { name: '确认并核对', exact: true }).isDisabled(), true);
    mark('Templates match exact header names after column reordering and require a fresh human confirmation');
    await page.screenshot({ path: path.join(evidence, 'templates-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 2), false);
    await page.screenshot({ path: path.join(evidence, 'templates-mobile.png'), fullPage: true });
    mark('Template controls fit a 390px mobile viewport without page-level horizontal overflow');
    await page.setViewportSize({ width: 1440, height: 1000 });

    await page.getByRole('checkbox').check();
    const runResponse = waitPost('/runs');
    await page.getByRole('button', { name: '确认并核对', exact: true }).click();
    const response = await runResponse;
    assert(response.ok(), `Reordered run failed: ${await response.text()}`);
    const reorderedRunId = await waitOperation(await response.json());
    await page.getByRole('heading', { name: '核对结果', exact: true }).waitFor();
    await page.locator('.results-table tbody tr').nth(8).waitFor();
    assert.equal(await page.locator('.results-table tbody tr').count(), 9);
    const reorderedRun = await api('/runs/' + reorderedRunId);
    assert.deepEqual(reorderedRun.summary.counts, originalRun.summary.counts);
    assert.notEqual(reorderedRun.mapping_id, originalRun.mapping_id);
    const differences = await api(`/runs/${reorderedRunId}/results?status=changed`);
    const large = differences.items.find(item => item.key_text === 'ORD-008');
    assert.equal(large.comparisons[0].difference, '0.01');
    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: '导出全部结果', exact: true }).click();
    const downloaded = await downloadPromise;
    const exportPath = path.join(evidence, 'template-reordered-export.xlsx');
    await downloaded.saveAs(exportPath);
    const exportHash = crypto.createHash('sha256').update(fs.readFileSync(exportPath)).digest('hex');
    mark('Reusing the template produces the same nine semantic groups, exact 0.01 difference, and a downloadable XLSX');

    // Missing-column failures must preserve the user's existing editable draft.
    const missing = { name: `验收_缺失金额列_${suffix}.csv`, mimeType: 'text/csv', buffer: Buffer.from('订单编号,客户\nORD-001,林溪\n') };
    await uploadUiPair(missing, rightReordered);
    await page.getByLabel('左表主键', { exact: true }).selectOption('c1');
    await page.getByLabel('右表主键', { exact: true }).selectOption('c4');
    await page.getByLabel('第1组左表字段', { exact: true }).selectOption('c2');
    await page.getByLabel('第1组右表字段', { exact: true }).selectOption('c3');
    await page.getByRole('checkbox').check();
    const beforeFailure = await draftValues();
    await page.getByLabel('已保存的字段模板', { exact: true }).selectOption(retained.id);
    const previewResponse = waitPost(`/templates/${retained.id}/preview`);
    await page.getByRole('button', { name: '应用模板', exact: true }).click();
    const missingResponse = await previewResponse;
    assert.equal(missingResponse.status(), 422);
    assert.equal((await missingResponse.json()).code, 'TEMPLATE_COLUMNS_MISSING');
    const alert = page.getByRole('alert');
    await alert.waitFor();
    assert((await alert.innerText()).includes('金额'), 'Missing-column feedback must name the missing header');
    assert.deepEqual(await draftValues(), beforeFailure);
    assert.equal(await page.getByRole('checkbox').isChecked(), true);
    mark('Missing headers produce an actionable message while preserving all current draft selections and review state');
    await page.getByLabel('关闭错误提示', { exact: true }).click();

    await page.getByLabel('已保存的字段模板', { exact: true }).selectOption(disposable.id);
    await page.getByRole('button', { name: '删除模板', exact: true }).click();
    const deleteGroup = page.getByRole('group', { name: '确认删除字段模板', exact: true });
    await deleteGroup.waitFor();
    assert.equal(await page.getByRole('button', { name: '取消删除', exact: true }).evaluate(el => el === document.activeElement), true);
    await page.keyboard.press('Escape');
    await deleteGroup.waitFor({ state: 'hidden' });
    assert((await api('/templates?limit=100')).items.some(item => item.id === disposable.id));
    await page.getByRole('button', { name: '删除模板', exact: true }).click();
    await page.getByRole('button', { name: '确认删除模板', exact: true }).click();
    await page.getByRole('status').filter({ hasText: `模板“${disposableName}”已删除` }).waitFor();
    const remaining = await api('/templates?limit=100');
    assert(!remaining.items.some(item => item.id === disposable.id));
    assert(remaining.items.some(item => item.id === retained.id));
    assert.deepEqual(await draftValues(), beforeFailure);
    assert.deepEqual(await api('/runs/' + originalRunId), originalRun);
    assert.deepEqual(await api('/runs/' + reorderedRunId), reorderedRun);
    mark('Template deletion supports keyboard cancellation and explicit confirmation without changing drafts or historical results');
    assert.deepEqual(errors, [], 'Unexpected browser exceptions');
    fs.writeFileSync(path.join(evidence, 'template-checks.json'), JSON.stringify({
      status: 'PASS', checked_at: new Date().toISOString(), origin: baseUrl,
      runtime: process.env.RUNTIME_DESCRIPTION || 'Running HTTP workspace; see runtime-specific acceptance evidence',
      checks, retained_template_id: retained.id, retained_template_name: retained.name,
      original_run_id: originalRunId, reordered_run_id: reorderedRunId,
      reordered_left_import_id: reorderedRun.left_import_id, reordered_right_import_id: reorderedRun.right_import_id,
      export_sha256: exportHash, page_errors: errors,
    }, null, 2));
  } catch (error) {
    await page.screenshot({ path: path.join(evidence, 'template-failure.png'), fullPage: true }).catch(() => {});
    console.error((await page.locator('body').innerText().catch(() => '')).slice(0, 4500));
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
