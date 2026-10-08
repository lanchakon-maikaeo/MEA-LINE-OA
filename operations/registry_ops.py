"""Writes to the registry (api_confirmed, M11 Q2): stable operations and no blind retry.

SAP cannot be connected today (G06), so production stays manual_reference and no real adapter
exists. This module is the contract a future adapter has to keep, proven against
`SimulatedRegistry`:

- one operation per idempotency key: the same key and request return the first outcome, the same
  key with another request is refused;
- an operation is `unknown` from the moment it is sent until the registry answers; a timeout
  leaves it unknown, and it is settled by asking the registry for the operation ID
  (`reconcile_external`), never by sending it again;
- a request that provably never reached the registry (connection refused) may be sent again;
- a callback that arrives twice is recorded once.

Calls to the registry never run inside a database transaction.
"""

import hashlib
import json

from django.db import transaction
from django.utils import timezone

from operations.contracts import ErrorCode, fail
from operations.models import ExternalOperation

S = ExternalOperation.Status
FINAL = (S.CONFIRMED, S.FAILED)


class AdapterTimeout(Exception):
    """No answer in time: the registry may or may not have done it."""


class AdapterUnavailable(Exception):
    """The request never reached the registry (e.g. connection refused)."""


class AdapterRejected(Exception):
    """The registry refused the request; sending it again changes nothing."""


def fingerprint(kind, request):
    canonical = json.dumps({"kind": kind, "request": request}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _note(op, event, **data):
    op.history = [*op.history, {"at": timezone.now().isoformat(), "event": event, **data}][-50:]


def _settle(op, status, reference="", error=""):
    op.status = status
    if reference:
        op.external_reference = reference[:64]
    if error:
        op.last_error = error[:200]
    if status == S.CONFIRMED and op.confirmed_at is None:
        op.confirmed_at = timezone.now()


def _claim(kind, key, request, case):
    """Lock the operation and decide whether it may be sent now (inside a transaction)."""
    digest = fingerprint(kind, request)
    op, created = ExternalOperation.objects.select_for_update().get_or_create(
        idempotency_key=key,
        defaults={"kind": kind, "fingerprint": digest, "request": request, "case": case},
    )
    if not created and op.fingerprint != digest:
        fail(ErrorCode.IDEMPOTENCY_CONFLICT, "This key was used for another registry request.")
    return op


def submit(kind, key, request, adapter, case=None):
    """Send once; the same key returns the first outcome; unknown is asked about, not resent."""
    with transaction.atomic():
        op = _claim(kind, key, request, case)
        if op.status in FINAL:
            return op
        if op.status == S.UNKNOWN:
            send = False
        else:
            # In flight counts as unknown: a concurrent caller asks instead of sending again.
            op.status = S.UNKNOWN
            op.attempts += 1
            _note(op, "sent", attempt=op.attempts)
            op.save()
            send = True
    if not send:
        op = reconcile_external(op.operation_id, adapter)
        if op.status == S.UNKNOWN:
            fail(ErrorCode.EXTERNAL_OUTCOME_UNKNOWN, "The registry has not answered; ask again.")
        if op.status != S.PENDING:
            return op
        return submit(kind, key, request, adapter, case)  # proven not done: now it may go
    try:
        reference = adapter.post(str(op.operation_id), kind, request)
    except AdapterTimeout:
        outcome = (S.UNKNOWN, "", "timeout")
    except AdapterUnavailable as exc:
        outcome = (S.PENDING, "", f"not delivered: {exc}")
    except AdapterRejected as exc:
        outcome = (S.FAILED, "", f"rejected: {exc}")
    else:
        outcome = (S.CONFIRMED, reference, "")
    with transaction.atomic():
        op = ExternalOperation.objects.select_for_update().get(pk=op.pk)
        if op.status == S.UNKNOWN:  # a callback may have settled it meanwhile
            _settle(op, *outcome)
        _note(op, "answer", status=outcome[0], error=outcome[2])
        op.save()
    return op


def reconcile_external(operation_id, adapter):
    """Ask the registry what happened to an unknown operation; never sends it."""
    with transaction.atomic():
        op = ExternalOperation.objects.select_for_update().get(operation_id=operation_id)
        if op.status != S.UNKNOWN:
            return op
        op.queries += 1
        op.save(update_fields=["queries", "updated_at"])
    try:
        status, detail = adapter.status(str(op.operation_id))
    except (AdapterTimeout, AdapterUnavailable) as exc:
        status, detail = "unknown", type(exc).__name__
    with transaction.atomic():
        op = ExternalOperation.objects.select_for_update().get(pk=op.pk)
        if op.status == S.UNKNOWN:
            if status == "confirmed":
                _settle(op, S.CONFIRMED, reference=detail)
            elif status == "failed":
                _settle(op, S.FAILED, error=detail)
            elif status == "not_found":
                _settle(op, S.PENDING, error="registry has no such operation")
        _note(op, "status query", answer=status)
        op.save()
    return op


def on_callback(operation_id, callback_id, status, reference="", error=""):
    """The registry tells us an outcome; the same callback twice is recorded once, and a
    callback never overturns a final outcome (the conflict is kept in the history)."""
    with transaction.atomic():
        op = ExternalOperation.objects.select_for_update().filter(operation_id=operation_id).first()
        if op is None:
            fail(ErrorCode.NOT_FOUND)
        if callback_id in op.callbacks:
            return op, False
        op.callbacks = [*op.callbacks, callback_id][-100:]
        if op.status in FINAL:
            if (status == "confirmed") != (op.status == S.CONFIRMED):
                _note(op, "conflicting callback", callback=callback_id, status=status)
        elif status == "confirmed":
            _settle(op, S.CONFIRMED, reference=reference)
        elif status == "failed":
            _settle(op, S.FAILED, error=error)
        _note(op, "callback", callback=callback_id, status=status)
        op.save()
        return op, True


class SimulatedRegistry:
    """A registry stand-in for tests and the demo: it applies an operation once per operation ID
    (as a real API's own idempotency would) and can be told to time out or refuse."""

    def __init__(self, post=(), status=()):
        self.post_plan = list(post)  # "ok", "timeout_before", "timeout_after", "down", "reject"
        self.status_plan = list(status)  # "ok", "timeout"
        self.applied = {}
        self.writes = 0
        self.posts = 0

    def post(self, operation_id, kind, request):
        self.posts += 1
        behaviour = self.post_plan.pop(0) if self.post_plan else "ok"
        if behaviour == "down":
            raise AdapterUnavailable("connection refused")
        if behaviour == "reject":
            raise AdapterRejected("material not found")
        if behaviour == "timeout_before":
            raise AdapterTimeout
        if operation_id not in self.applied:
            self.writes += 1
            self.applied[operation_id] = f"SIM-SAP-{self.writes:06d}"
        if behaviour == "timeout_after":
            raise AdapterTimeout
        return self.applied[operation_id]

    def status(self, operation_id):
        behaviour = self.status_plan.pop(0) if self.status_plan else "ok"
        if behaviour == "timeout":
            raise AdapterTimeout
        if operation_id in self.applied:
            return "confirmed", self.applied[operation_id]
        return "not_found", ""
