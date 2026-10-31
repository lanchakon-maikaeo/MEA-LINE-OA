"""เอกสารความรู้ (ADR 0037 Q9, Q13, Q21, Q24 to Q26): text the free chat agent may search.

Staff upload text that is not secret; the system refuses it when it finds personal data, and an
admin other than the uploader approves it. Only an approved document reaches Pinecone, as chunks
of an index with integrated embedding (text field `chunk_text`), and retiring it deletes those
chunks again. A search sends only the masked question. Everything that talks to Pinecone stays
off until ตัวเลือกระบบ free_chat_docs is on and the key and index host are set: the owner opens
a Starter account (no card) later, and the system never upgrades a plan.
"""

import json
import urllib.error
import urllib.request

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from operations import identity, options, pii
from operations.models import KnowledgeDoc

MAX_TEXT = 20000
CHUNK = 900  # characters per record; paragraphs stay together when they fit
TOP_K = 3
MIN_SCORE = 0.35  # Q15: a weaker hit means "no information"; the score is never shown
API_VERSION = "2025-04"
TIMEOUT = 6
# What a document may not hold (Q25). A link is not personal data; replies never carry one.
REFUSED_KINDS = set(pii.KINDS) - {"link"}


class PineconeError(Exception):
    """Pinecone is off, unset, over quota or down: the agent answers that it has no document."""


def enabled():
    return (
        options.get(options.FREE_CHAT_DOCS) == "on"
        and bool(settings.PINECONE_API_KEY)
        and bool(settings.PINECONE_INDEX_HOST)
    )


def personal_lines(text):
    """[(line number, [what was found])] for each line that holds personal data."""
    return found_in(text.splitlines(), REFUSED_KINDS)


def found_in(lines, kinds):
    """[(line number, [what the staff page calls each kind found])] (masking, no AI)."""
    words = {word: kind for kind, (word, _) in pii.KINDS.items()}
    masker, found = pii.Masker.for_ai(), []
    for number, line in enumerate(lines, start=1):
        masked = masker.mask(line)
        hits = {words[m[1]] for m in pii.LABEL.finditer(masked) if m[0] not in line} & kinds
        if hits:
            found.append((number, [pii.KINDS[k][1] for k in sorted(hits, key=pii.RANK.get)]))
    return found


def _clean(title, text):
    title = " ".join((title or "").split())[:200]
    text = "\n".join(line.rstrip() for line in (text or "").replace("\r", "").split("\n")).strip()
    if not title or not text:
        raise ValidationError("ต้องมีชื่อเอกสารและเนื้อหา")
    if len(text) > MAX_TEXT:
        raise ValidationError(f"เนื้อหายาวได้ไม่เกิน {MAX_TEXT:,} ตัวอักษร แบ่งเป็นหลายเอกสารได้")
    found = personal_lines(text)
    if found:
        where = "; ".join(f"บรรทัด {n}: {' '.join(kinds)}" for n, kinds in found[:5])
        raise ValidationError(f"พบข้อมูลส่วนบุคคล ลบออกก่อนอัปโหลด ({where})")
    return title, text


@transaction.atomic
def upload(actor, title, text, reason, doc=None):
    """A new document, or a new revision of `doc`, waiting for approval (staff, audited)."""
    if not identity.staff_scope(actor).has_any_access:
        raise PermissionDenied("เฉพาะเจ้าหน้าที่")
    reason = identity._require_reason(reason)
    title, text = _clean(title, text)
    if doc is None:
        doc = KnowledgeDoc(uploaded_by=actor)
    else:
        doc = KnowledgeDoc.objects.select_for_update().get(pk=doc.pk)
        if doc.status == KnowledgeDoc.Status.APPROVED:
            _remove(doc)  # the old text leaves Pinecone until the new one is approved
        doc.revision += 1
        doc.uploaded_by = actor
        doc.approved_by, doc.approved_at = None, None
    doc.title, doc.text, doc.status = title, text, KnowledgeDoc.Status.DRAFT
    doc.save()
    identity.audit(actor, "knowledge.uploaded", doc, reason, title=title, revision=doc.revision)
    return doc


@transaction.atomic
def approve(actor, doc, reason):
    """An admin who did not upload this revision approves it; then it goes to Pinecone."""
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    doc = KnowledgeDoc.objects.select_for_update().get(pk=doc.pk)
    if doc.status != KnowledgeDoc.Status.DRAFT:
        raise ValidationError("เอกสารนี้ไม่ได้รอรับรอง")
    if doc.uploaded_by_id == actor.pk:
        raise ValidationError("ผู้รับรองต้องเป็นคนละคนกับผู้อัปโหลด")
    doc.status = KnowledgeDoc.Status.APPROVED
    doc.approved_by, doc.approved_at = actor, timezone.now()
    doc.save(update_fields=["status", "approved_by", "approved_at"])
    identity.audit(actor, "knowledge.approved", doc, reason, revision=doc.revision)
    sync(doc)
    return doc


@transaction.atomic
def retire(actor, doc, reason):
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    doc = KnowledgeDoc.objects.select_for_update().get(pk=doc.pk)
    if doc.status == KnowledgeDoc.Status.RETIRED:
        raise ValidationError("เอกสารนี้เลิกใช้แล้ว")
    _remove(doc)
    doc.status = KnowledgeDoc.Status.RETIRED
    doc.save(update_fields=["status"])
    identity.audit(actor, "knowledge.retired", doc, reason)
    return doc


def chunks(text):
    """Paragraph-sized pieces of at most CHUNK characters."""
    pieces, current = [], ""
    for paragraph in [p.strip() for p in text.split("\n\n") if p.strip()]:
        while len(paragraph) > CHUNK:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(paragraph[:CHUNK])
            paragraph = paragraph[CHUNK:]
        if current and len(current) + len(paragraph) + 2 > CHUNK:
            pieces.append(current)
            current = ""
        current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        pieces.append(current)
    return pieces


def _ids(doc, count):
    return [f"doc-{doc.pk}-{n}" for n in range(count)]


def sync(doc):
    """Send an approved document to Pinecone; a failure is kept on the document, not raised."""
    if not enabled():
        KnowledgeDoc.objects.filter(pk=doc.pk).update(sync_error="ยังไม่ได้เปิดการเชื่อม Pinecone")
        return False
    pieces = chunks(doc.text)
    records = [
        {"_id": record_id, "chunk_text": piece, "title": doc.title, "doc": doc.pk}
        for record_id, piece in zip(_ids(doc, len(pieces)), pieces, strict=True)
    ]
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records).encode()
    try:
        if doc.chunks > len(pieces):
            _post("/vectors/delete", {"ids": _ids(doc, doc.chunks)[len(pieces) :]})
        _post(
            f"/records/namespaces/{settings.PINECONE_NAMESPACE}/upsert",
            body,
            "application/x-ndjson",
        )
    except PineconeError as exc:
        KnowledgeDoc.objects.filter(pk=doc.pk).update(sync_error=str(exc)[:200])
        return False
    KnowledgeDoc.objects.filter(pk=doc.pk).update(
        chunks=len(pieces), synced_at=timezone.now(), sync_error=""
    )
    return True


def _remove(doc):
    if not doc.chunks:
        return
    if not enabled():
        raise ValidationError("เอกสารนี้อยู่บน Pinecone แต่การเชื่อมปิดอยู่ เปิดการเชื่อมก่อนจึงลบออกได้")
    try:
        _post("/vectors/delete", {"ids": _ids(doc, doc.chunks)})
    except PineconeError as exc:
        raise ValidationError(f"ลบออกจาก Pinecone ไม่สำเร็จ ({exc})") from exc
    doc.chunks, doc.synced_at = 0, None
    KnowledgeDoc.objects.filter(pk=doc.pk).update(chunks=0, synced_at=None)


def search(query):
    """[{"title", "text"}] of the approved documents nearest to the masked `query`."""
    if not enabled():
        raise PineconeError("off")
    data = _post(
        f"/records/namespaces/{settings.PINECONE_NAMESPACE}/search",
        {
            "query": {"inputs": {"text": query[:500]}, "top_k": TOP_K},
            "fields": ["chunk_text", "title", "doc"],
        },
    )
    hits = ((data or {}).get("result") or {}).get("hits") or []
    live = set(
        KnowledgeDoc.objects.filter(status=KnowledgeDoc.Status.APPROVED).values_list(
            "pk", flat=True
        )
    )
    found = []
    for hit in hits:
        fields = hit.get("fields") or {}
        if (hit.get("_score") or 0) < MIN_SCORE or fields.get("doc") not in live:
            continue  # a weak hit, or a document retired since (never trust the index alone)
        found.append(
            {"title": str(fields.get("title", ""))[:200], "text": fields.get("chunk_text", "")}
        )
    return found


def _post(path, payload, content_type="application/json"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    request = urllib.request.Request(  # noqa: S310 - the owner's configured https index host
        f"https://{settings.PINECONE_INDEX_HOST}{path}",
        data=body,
        method="POST",
        headers={
            "Api-Key": settings.PINECONE_API_KEY,
            "Content-Type": content_type,
            "X-Pinecone-API-Version": API_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise PineconeError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise PineconeError(type(exc).__name__) from exc
    try:
        return json.loads(raw) if raw else {}
    except ValueError as exc:
        raise PineconeError("unreadable answer") from exc
