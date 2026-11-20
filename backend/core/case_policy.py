"""Pure, versioned collection exception policy and independent review rules.

Every outcome except exact equality needs a case, including tolerated monetary
differences. A disposition explains the frozen result; it cannot change that
result or its financial amounts. Source references identify immutable dataset
rows. The database caller must check their workspace, run membership and access.

Transitions here enforce only workflow/identity guards. Their caller owns role
authorization, active-period/run checks, expected-version checks, evidence
validation/freezing, assignment updates and transactional audit events. Granting
multiple roles never removes the independent-review identity requirement.
"""
import re

from .business import STATUSES
from .errors import CoreError


CASE_POLICY_VERSION = "collection-case-1.0.0"
CASE_STATES = ("open", "in_progress", "pending_review", "closed")
RESOLUTION_CODES = (
    "timing_difference", "partial_payment", "documented_fee", "duplicate_source",
    "accepted_difference", "needs_external_followup",
)
MAX_RESOLUTION_TEXT = 4000
MAX_REASON_TEXT = 2000
MAX_EVIDENCE_REFS = 20
_ACTIONS = {"claim", "submit", "withdraw", "approve", "reject", "reopen"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ROW_ID = re.compile(r"r[1-9][0-9]{0,9}\Z")
_TRIM = " \t\r\n"


def _fail(code, message, **details):
    raise CoreError(code, message, details)


def _identifier(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _text(value, limit, code):
    if (not isinstance(value, str) or len(value) > limit
            or any((ord(char) < 32 and char not in "\t\n\r") or 127 <= ord(char) <= 159
                   or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        _fail(code, "说明必须为长度限制内的有效文本。", max_length=limit)
    result = value.strip(_TRIM)
    if not result.strip():
        _fail(code, "必须填写明确的说明。")
    return result


def needs_case(group_status):
    """Whether a known immutable collection result needs a separate case."""
    if not isinstance(group_status, str) or group_status not in STATUSES:
        _fail("INVALID_CASE_GROUP_STATUS", "无法为未知的核对结果状态确定案件策略。")
    return group_status != "exact_equal"


def validate_resolution(resolution):
    """Validate a disposition without allowing inline source/financial values.

    Evidence order is canonicalized by dataset and row id. References must be
    unique; duplicate evidence is rejected rather than silently discarded.
    """
    if not isinstance(resolution, dict) or set(resolution) != {"code", "text", "evidence"}:
        _fail("INVALID_CASE_RESOLUTION", "处置意见须包含分类、说明和来源证据引用。")
    code = resolution["code"]
    if not isinstance(code, str) or code not in RESOLUTION_CODES:
        _fail("INVALID_CASE_RESOLUTION", "处置分类无效。")
    text = _text(resolution["text"], MAX_RESOLUTION_TEXT, "INVALID_CASE_RESOLUTION")
    evidence = resolution["evidence"]
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_EVIDENCE_REFS:
        _fail("INVALID_CASE_EVIDENCE", "须提供 1 至 20 个唯一的来源记录引用。")
    references, seen = [], set()
    for reference in evidence:
        if not isinstance(reference, dict) or set(reference) != {"dataset_id", "row_id"}:
            _fail("INVALID_CASE_EVIDENCE", "来源引用只能包含资料标识和行标识。")
        dataset_id, row_id = reference["dataset_id"], reference["row_id"]
        if not _identifier(dataset_id) or not isinstance(row_id, str) or not _ROW_ID.fullmatch(row_id):
            _fail("INVALID_CASE_EVIDENCE", "来源资料标识或行标识无效。")
        identity = (dataset_id, row_id)
        if identity in seen:
            _fail("DUPLICATE_CASE_EVIDENCE", "同一来源记录不能重复引用。")
        seen.add(identity)
        references.append({"dataset_id": dataset_id, "row_id": row_id})
    references.sort(key=lambda item: (item["dataset_id"], item["row_id"]))
    return {"code": code, "text": text, "evidence": references}


def validate_reason(reason):
    """Validate a nonempty audit/review explanation, without coercing values."""
    return _text(reason, MAX_REASON_TEXT, "INVALID_CASE_REASON")


def transition(case_state, action, actor_id, assigned_to, submitted_by, resolution_code=None):
    """Return the next state or a stable CoreError; mutate no case data.

    Submit's payload must separately pass ``validate_resolution``. For approval,
    ``resolution_code`` must be the already frozen submission's valid code.
    The caller records claim assignment, submitter identity and any later audit
    changes atomically with the returned state.
    """
    if not isinstance(case_state, str) or case_state not in CASE_STATES:
        _fail("INVALID_CASE_STATE", "案件状态无效。")
    if not isinstance(action, str) or action not in _ACTIONS:
        _fail("INVALID_CASE_ACTION", "案件操作不受支持。")
    if (not _identifier(actor_id) or (assigned_to is not None and not _identifier(assigned_to))
            or (submitted_by is not None and not _identifier(submitted_by))):
        _fail("INVALID_CASE_IDENTITY", "案件操作人或处理人标识无效。")
    if resolution_code is not None and (not isinstance(resolution_code, str) or resolution_code not in RESOLUTION_CODES):
        _fail("INVALID_CASE_RESOLUTION", "处置分类无效。")

    required_state = {"claim": "open", "submit": "in_progress", "withdraw": "pending_review",
                      "approve": "pending_review", "reject": "pending_review", "reopen": "closed"}[action]
    if case_state != required_state:
        _fail("CASE_STATE_CONFLICT", "当前案件状态不允许此操作。", action=action, state=case_state)
    if action == "claim":
        if assigned_to is not None:
            _fail("CASE_ALREADY_ASSIGNED", "案件已有处理人，不能再次认领。")
        return "in_progress"
    if action == "submit":
        if assigned_to != actor_id:
            _fail("CASE_ASSIGNEE_REQUIRED", "只有已分配的处理人可以提交处置意见。")
        return "pending_review"
    if action == "withdraw":
        if submitted_by != actor_id:
            _fail("CASE_SUBMITTER_REQUIRED", "只有本次提交人可以撤回待复核意见。")
        return "in_progress"
    if action in ("approve", "reject"):
        if submitted_by is None:
            _fail("CASE_SUBMITTER_REQUIRED", "待复核意见缺少明确的提交人。")
        if actor_id == submitted_by:
            _fail("CASE_INDEPENDENT_REVIEW_REQUIRED", "复核人必须与本次提交人不同。")
        if action == "approve":
            if resolution_code is None:
                _fail("INVALID_CASE_RESOLUTION", "批准须引用已提交的有效处置分类。")
            if resolution_code == "needs_external_followup":
                _fail("CASE_EXTERNAL_FOLLOWUP_REQUIRED", "仍需外部跟进的案件不能批准关闭。")
            return "closed"
        return "in_progress"
    return "in_progress"  # Reopen keeps historical approvals and evidence in audit history.
