# Local collection workflow and development scope

Status: `0.3.0 / LOCAL_COLLECTION_IMPLEMENTED / FINAL_ACCEPTANCE_IN_PROGRESS`.

The v0.3 local collection workflow is implemented from first-run account setup through import, rules, periods, independent case review, period closing and frozen reports. The backend regression passed 558 tests; the distinct-account browser and portable lifecycle/restore checks also passed. The public export records sanitized summaries only, with its own validation status in evidence/verification.json. This is a bounded local collection delivery, not completion of the entire production design. External production deployment, refund/settlement stages and a real business pilot remain separate work. The released v0.2 Windows package is unchanged. Existing data is not automatically migrated into the team service.

Implemented modules:

- `team.models`, `team.security`: PostgreSQL identity, Argon2id passwords, revocable opaque sessions, CSRF, additive workspace roles, organization administrator grants, append-only audit, idempotency receipts. Organization administration does not bypass workspace membership.
- `team.app`: authenticated session/workspace/member administration, authorized legacy source/export/history reads, scoped job status and cancellation. There is no legacy unauthenticated route mounted in this app. No migration or compute runner starts in its lifespan.
- `team.cli`: explicit migration, one-time administrator provisioning, local-console account recovery and session revocation. The last active organization administrator cannot be disabled through this tool.
- `team.jobs`: transaction-scoped admission/claim/renew/cancel/finalization/recovery primitives, bounded PostgreSQL queue, a single active attempt, scoped idempotency, lease fencing and once-only reservation settlement. Published dataset and reconciliation-result storage is included in the same terminal transaction.
- `team.worker`: independent import/collection worker with the legacy-compatible global advisory lock, generation fencing, task heartbeat and bounded spawned compute processes. Default timeout is 30 seconds and child memory ceiling is 768 MiB. Ownership loss stops compute; replacement workers invalidate older generations. A repeated publication reads the committed manifest instead of duplicating rows or storage charges.
- `team.datasets`, `core.dataset_parser`: immutable original CSV/XLSX files, explicit visible worksheet selection, normalized row storage and paginated source coordinates. Upload authorization precedes consuming the multipart body; a second authorization check occurs during admission. Multiple visible worksheets require an exact selection, even if only one contains data. Entire-workbook formula/external-link/hidden-data and ZIP/XML checks remain enabled.
- `core.mapping`, `team.reconciliations`: explicit dataset header mapping, immutable published rule versions, durable collection-run admission, atomic groups/coverage publication and per-row source lookup. Rule publication requires `rules.publish`; run submission requires `runs.create` and `data.read`. A fresh rule ID is a new version, and resubmission with a new request key creates a distinct run. There is no mutable latest-rule alias.
- `core.period_policy`, `team.periods`: reusable immutable collection profiles saved from manager-published rules; immutable calendar/cutoff definitions, explicit source declarations, configuration-hash confirmation, optimistic version checks and explicit successful-run activation. The executor never changes a period pointer. Older jobs may finish as historical candidates.
- `core.case_policy`, `team.cases`, `team.case_api`: atomic case batches for every non-exact period result, operator claim and evidence submission, withdrawal, independent approval/rejection and reopening. Case and period versions are checked together; evidence references belong to immutable rows in the case's own group. Full event history is append-only and paginated. Closing requires current results, complete human declarations and no unresolved cases.
- `team.reports`: closed-period XLSX snapshots with stored bytes and SHA-256, literal text amounts/identifiers, permission-controlled downloads and unchanged historical reports after reopening.
- `frontend`: account setup/login, workspace/member administration, import and source browsing, explicit mapping/profile publication, period input confirmation and activation, case handling, distinct-account review and report download. The reviewer can inspect the exact frozen evidence across source pages with file names, headers and row coordinates; missing evidence blocks approval visibly. Event pagination exposes the complete history. Frontend implementation/build completion is separate from final browser acceptance.
- `team.local`, `scripts/team_portable_launcher.py`: loopback entry, first administrator setup, owned PostgreSQL/API/worker processes, start/stop, backup and restore to a separate sibling installation. Restored sessions are invalidated and execution stays paused until explicit local confirmation; public verification is summarized in evidence/verification.json.
- `core.business`: collection-only whole-group 1:1 / 1:N / N:1 reconciliation on explicit composite keys and currency, exact Decimal arithmetic and input coverage. Refund/settlement stages are rejected rather than simulated. The core result retains `source_completeness=not_attested`; the period's human declarations are stored and assessed separately.
- Frozen migrations preserve the schema from the actual v0.1/v0.2 sources. Team migration assigns old records to an explicit legacy workspace and does not invent historical actors. Old files and stored exports are not recomputed.

Developer startup, on a dedicated development database:

```powershell
# In backend, with DATABASE_URL configured privately for a development database.
python -m team.cli migrate
python -m team.cli bootstrap --login owner --claim-legacy
# Password is prompted without echo. Omit --claim-legacy to avoid assigning old-data access.
python -m uvicorn team.app:app --host 127.0.0.1 --port 8001
# In a second terminal with the same private DATABASE_URL:
python -m team.worker
# For one claim/execute cycle: python -m team.worker --once
```

The default team origin is `https://localhost`; use an HTTPS reverse proxy and explicitly configure `TEAM_ALLOWED_ORIGINS` and trusted hosts before browser access. The local Windows package serves the team UI on loopback with explicit first-run administrator setup. No example password, user data or production database credentials are bundled. Do not migrate the only copy of live data: validate a backup and work on an isolated copy first. Migration refuses the legacy executor's active advisory lock. The old executor refuses a team database and only migrates legacy databases through revision 0002.

Supported local recovery commands:

```powershell
python -m team.cli set-active --login member --disable
python -m team.cli set-active --login member --enable
python -m team.cli reset-password --login member
```

The recovery console uses deployment-level database access. Recovery actions are audited as local-console actions; they are not falsely attributed to a signed-in user.

API request rules:

- Login and mutations require an allowed Origin plus `X-Reconcile-Client: web`.
- Successful login sets an HttpOnly/SameSite session cookie and returns the CSRF token. HTTPS team mode additionally sets Secure; the loopback-only local mode uses HTTP. Mutations require `X-CSRF-Token`. An authenticated same-origin request to `GET /api/v1/session/csrf` recovers the same token after reload, without invalidating other tabs.
- Account/workspace/member commands require `Idempotency-Key`; same key and different content returns 409. Password-bearing requests never create a fast password digest in idempotency metadata.
- Member edits use `expected_version`; cross-workspace IDs return 404. Viewer role has result-summary access only, not raw source, exports, audit or job status.
- Queue methods require a fresh short database transaction, full permission checks and business-resource authorization callbacks when integrated. The worker holds advisory lock 732914622 and stops compute on lease loss. API status reads never return raw job request/result contents.
- `POST /api/v1/workspaces/{workspace}/datasets` accepts multipart `file`, optional exact `worksheet` query value and `Idempotency-Key`; it returns 202 plus dataset/job IDs. A duplicate request returns the same IDs. Each accepted file remains available after a parse failure or cancellation and remains charged to workspace storage.
- `GET .../datasets/{id}`, `/rows?after=0&limit=50`, and `/source` require `data.read`; row pages are byte-bounded as well as count-bounded. `WORKSHEET_REQUIRED` includes worksheet choices in the authorized dataset error details. Failed parsing never exposes a partial ready dataset. Source completeness is declared explicitly in the implemented business-period workflow.

Current ingestion limits retain 10 MiB/file, 10,000 rows and 50 columns; up to 20 worksheet declarations are inspected. Only one worksheet and one file per dataset are supported in this checkpoint. Multi-file dataset composition and scheduled connectors remain pending. The Windows memory ceiling uses [Job Object process memory limits](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information); Linux uses RLIMIT_AS. Actual worker lifecycle tests were run on Windows; Linux/container resource behavior and memory-exhaustion stress remain separate checks.

Collection workflow (schema revision `0009`):

1. Import both datasets and wait for `ready`.
2. `POST .../collection-rules` with `Idempotency-Key` and the definition below. This is explicit confirmation/publication, available to a workspace manager. Header names must match exactly. Compiled column IDs, raw mapping, parser/engine/mapping versions and source hashes are frozen together.
3. `POST .../collection-runs` with `{"rule_id":"<published-version-id>"}` and a fresh key. It returns run/job IDs; the independent worker computes without database credentials in its child process.
4. Read `GET .../collection-runs/{id}` for status, hashes and summary. `GET .../collection-runs/{id}/groups?after=0&limit=50` returns bounded group summaries, row counts and five issue previews; it does not embed all source members.
5. `GET .../collection-runs/{id}/groups/{group_id}/issues?after=0&limit=50` pages all issues. `/rows?side=left&after=0&limit=50` (or `right`) returns original cells and physical source coordinates and additionally requires `data.read`. Use returned cursors, which are exclusive ordinals for rows/groups and offsets for issues. All paths start with `/api/v1/workspaces/{workspace}`.

```json
{
  "definition": {
    "name": "Order/payment collection",
    "left_dataset_id": "<ready-order-dataset>",
    "right_dataset_id": "<ready-payment-dataset>",
    "left_mapping": {
      "key_columns": ["order"], "amount_column": "amount", "event_id_column": "event",
      "currency": {"constant": "CNY"}, "source_namespace": "orders"
    },
    "right_mapping": {
      "key_columns": ["order"], "amount_column": "amount", "event_id_column": "event",
      "currency": {"constant": "CNY"}, "source_namespace": "payments"
    },
    "rule": {"stage_id": "collection", "aggregation": "1:N", "tolerance": "0"}
  }
}
```

Currency can instead use `{"column":"currency"}`. The confirmed code must have the intended business meaning; the engine never infers currency conversion. Mapping trims ASCII edge whitespace only; it preserves case, leading zeroes and decimal text. Missing/invalid amounts, native boolean/date cells and scientific notation remain invalid values, not zero. Source event IDs are required to detect duplicates. A source ID may also serve as the business key. Same dataset on both sides is rejected.

A replay with the same key returns the original IDs only after checking present permission; different request content conflicts. Previously completed runs remain immutable after new rules or later runs. `technical_coverage=complete` means every supplied row was assigned, while `source_completeness=not_attested` means the business has not established that all required source material was supplied. No case approval or business-period close is implied by a successful task.

Period workflow:

- `POST .../collection-profiles` with `{"source_rule_id":"<published-rule>","name":"Collection policy v1"}` requires `rules.publish` and an idempotency key. It saves the confirmed named mappings, currency, source namespaces, business rule and engine/mapping versions without binding the next period to the original datasets. Profiles are immutable; new policy is a new explicitly selected profile ID. Names have no “latest” lookup semantics.
- `POST .../periods` with `{"definition":{"name":"October collections","start_date":"2026-10-01","end_date":"2026-10-31","timezone":"Asia/Shanghai","cutoff_at":"2026-11-01T00:00:00+08:00"}}` creates a draft. Both dates are inclusive local business dates; frozen UTC bounds are `[start_inclusive,end_exclusive)`. The explicit offset-aware cutoff must cover the final full day. Calendar definitions cannot be edited in place. The range is 1–366 days; ambiguous/nonexistent local midnight boundaries are rejected. `tzdata` is explicitly installed for Windows.
- `POST .../periods/{id}/revisions` requires `period.inputs`, `expected_version` and the definition below. Both datasets must be present and ready. A missing source cannot be replaced by an invented empty file or zero amount. This resolves named mappings against the new headers, freezes input hashes and role bindings, and returns the exact server-generated `config_hash`. The derived rule records profile/revision provenance; an operator cannot change the manager's policy through the binding endpoint.
- `POST .../periods/{id}/confirm` with `{"expected_version":2,"config_hash":"<returned-hash>"}` explicitly confirms that immutable revision. Complete declarations require notes. Incomplete or unattested source sets may still be computed, but their declared completeness remains visible and does not imply business acceptance.
- `POST .../periods/{id}/runs` with `{"expected_version":3}` creates a candidate using `runs.create`. Job completion leaves the active pointer unchanged. `POST .../periods/{id}/activate` with `{"expected_version":4,"run_id":"<successful-candidate>"}` selects it with `period.activate`. Only a run submitted for that exact period and its currently confirmed revision can be activated; standalone and cross-period runs are rejected.
- Every successful mutation returns the latest period version; send that version with the next command. A stale version conflicts. Same-key/same-body replay returns the original response after current authorization checks, even if the period subsequently changed; clients should refresh the period after resolving a replay.
- `GET .../periods`, `.../periods/{id}` and `.../periods/{id}/runs` require `results.read`. Full `/revisions` and `/collection-profiles` require `data.read` because they include mappings and attestation notes. Lists use returned cursors. Period summaries expose completeness status without declaration notes.

```json
{
  "expected_version": 1,
  "definition": {
    "profile_id": "<immutable-profile-version>",
    "orders_dataset_id": "<ready-orders>",
    "payments_dataset_id": "<ready-payments>",
    "attestations": {
      "orders": {"status":"complete", "note":"Order export includes the declared period through the cutoff."},
      "payments": {"status":"incomplete", "note":"One payment channel export is still missing."}
    }
  }
}
```

The third declaration status is `not_attested`; it may have an empty note. The period assessment is `attested_complete` only if both declarations are complete, `incomplete` if either is incomplete, otherwise `not_attested`. This is a human source-scope declaration with actor/time and immutable hashes. Current mappings have no business-date field: the service does not filter rows by the period dates or independently prove that the export covers them. The core's immutable `source_completeness=not_attested` is preserved separately from the period declaration.

Changing inputs or selecting another profile creates a new immutable revision, returns the period to draft and clears its confirmation. Any previous active result remains available with `active_run_stale=true`. Already admitted historical jobs may finish, but cannot replace the active result automatically or be activated against a different revision. All candidates and configuration revisions remain queryable. Confirmation and activation actions are audited. Period closing now requires a successful active run, complete source declarations and all its cases independently approved. The case and period workflows are described below.

Case review and reports:

Every non-exact result, including tolerance matches, creates a case atomically with the period run. Standalone runs have no period cases. Activation of pre-case historical candidates explicitly backfills the batch under quota. Replacing a period result requires `acknowledged_open_cases` equal to the current unresolved count; old cases remain historical and never donate approvals to a new run.

`GET .../periods/{id}/cases` lists the active run, optionally with `run_id` for history. `GET .../cases/{id}` returns authorized detail and the last 50 events. `/cases/{id}/events?after=0&limit=50` pages the full append-only history. Viewer listings omit submitted explanations and source evidence.

`POST .../cases/{id}/{action}` requires `expected_case_version`, `expected_period_version`, and `payload`. Actions: `claim` with `{}`; `submit` with `{code,text,evidence:[{dataset_id,row_id}]}`; `withdraw`, `approve`, `reject`, `reopen` with `{reason}`. Only the assigned operator submits, only the submitter withdraws, and review always requires another user, even if one account has both roles. Evidence must identify immutable rows in the case's own group. Accepted differences explain the original financial result without changing it. `needs_external_followup` cannot be approved closed.

`POST .../periods/{id}/close` requires `period.close`, `expected_version` and `reason`; incomplete declarations or unresolved current cases block closing. `/reopen` requires `period.reopen` and a reason. Reopening a case in a closed period requires that additional permission and reopens both atomically. Direct input revision on a closed period is rejected until reopened.

`POST .../periods/{id}/reports` with `expected_version` freezes a closed-period XLSX and returns its SHA-256. `/periods/{id}/reports` lists saved versions; `/reports/{id}/source` downloads immutable bytes. These require `data.export`. Amounts and IDs are written as text, formula interpretation is disabled, and reports remain unchanged after reopening a period or case.

Local delivery: the isolated Windows package starts its own PostgreSQL, API and compute worker; first visit creates the administrator and a workspace. `启动.cmd`, `停止.cmd`, `备份.cmd` and `恢复备份.cmd` operate on that installation. Restores create a separate sibling copy, invalidate restored sessions and hold automatic execution until the local user explicitly confirms resumption. The v0.2 data directory is never automatically adopted. The local package still requires a distinct reviewer account; it does not label self-review as independent review. A real model endpoint remains optional and unconfigured.

Validation status:

The full backend suite completed on 2026-11-17 with **558 passed, 0 failures, 0 errors and 0 skipped**. TypeScript checking and the production frontend build also passed. The distinct-account browser flow, cross-page frozen evidence, actual Chinese CMD start/stop, persisted case/report restart and isolated backup/restore checks passed on this Windows host. Final package files match the tested installation. Evidence and hashes are consolidated in `evidence/verification.json`; this certifies the local collection scope only. Raw historical evidence remains private; this export includes a sanitized verification summary only.

```powershell
# TEST_DATABASE_URL must target a disposable PostgreSQL database named reconcile_test.
python -m pytest tests -q
```

Some legacy/team API fixtures rebuild `public`; migration/queue fixtures use disposable schemas. Never point these fixtures at a user's database. Windows tests exercise real compute-child timeout/cancellation, executor lock-connection termination and takeover fencing. Lost COMMIT acknowledgement is fault-injected after an actual database commit. Broader system faults/capacity measurements, v0.3 Linux/Docker deployment, independent clean Windows, a real business pilot and real model checks remain unverified. The existing v0.2 Docker/browser evidence does not certify the new team modules. No model endpoint is configured for the local collection workflow; explicit mappings and deterministic calculation work without one.

Further expansion: profile version comparison, multi-file source composition, programmatic source-range checks, refund/settlement stages, source-correction linkage, organizational deployment and real-data pilot. The delivered collection workflow does not claim these extensions. The worker dispatches versioned import and collection contracts. The standalone collection API still supports one immutable dataset pair per rule. The period API reuses a manager-published profile across new pairs, freezing a separate bound rule and input configuration for every revision. Cumulative refund checks must include earlier refunds through the fixed cutoff; missing history stays incomplete.

Primary references used for implementation decisions: [Argon2 API](https://argon2-cffi.readthedocs.io/en/stable/api.html), [OWASP session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html), [PostgreSQL locking](https://www.postgresql.org/docs/17/sql-select.html). Parameter and performance claims require measurements in the deployment environment.
