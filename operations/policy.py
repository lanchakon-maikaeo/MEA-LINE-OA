"""M06 policy engine: routing (R01-R10), priority (P0-P3) and acknowledgement clocks; M09 adds
the service calendar and work deadlines (กรอบเวลา) with their pauses.

Three parts stay apart (B.1, ADR 0002): facts (from the case, a hazard keyword flag, the AI
triage task and staff), rules (an imported, versioned policy) and people's decisions
(operations/routing.py). Everything here only proposes: no function changes a case's owner,
status or confirmed priority. Agreed design: PROJECT.md "การตัดสินใจออกแบบ M06".
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from operations import identity, orchestration, servicetime, workflow
from operations.models import (
    AckClock,
    AIConfigVersion,
    AITriageRun,
    AuthorityGrant,
    Case,
    CaseLine,
    CaseTriage,
    ChildWork,
    DeadlinePause,
    Decision,
    Division,
    EquipmentType,
    Handoff,
    OutboxEvent,
    PolicyVersion,
    Priority,
    Recommendation,
    RoleGrant,
    RoutingReview,
    Section,
)

PRIORITIES = tuple(Priority.values)  # most urgent first
DEMO_POLICY = Path(settings.BASE_DIR) / "operations" / "fixtures" / "policy_demo_pe_3.json"
DEADLINE_KINDS = (
    "ack",
    "child_accept",
    "first_response",
    "resolve",
    "child_result",
    "approval",
    "issue",
    "inspection",
)
EVENT_TRIAGE_AI = orchestration.EVENT_TRIAGE_AI
# A backlog this old means the system is not processing work: escalation waits (S12).
STUCK_SECONDS = 600
FACT_NAMES = ("equipment", "hazard_reported", "severe_impact", "registry_only")
STAFF_ONLY_FACTS = ("serial_mismatch", "shortage")
TEXT_FIELDS = ("purpose", "condition", "return_reason", "description", "impact", "intake_note")
S = Case.Status
K = Case.Kind


def rank(priority):
    """0 = P0 (most urgent); an empty level ranks after every level."""
    return PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES)


def more_urgent(*levels):
    present = [p for p in levels if p in PRIORITIES]
    return min(present, key=rank) if present else ""


# --- Policy versions ---------------------------------------------------------------------------


def _parse_moment(value, name):
    if value is None:
        return None
    moment = parse_datetime(str(value))
    if moment is None or timezone.is_naive(moment):
        raise ValidationError(f"{name} must be an ISO date-time with a time zone.")
    return moment


def digest_of(document):
    canonical = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# หมวดเรื่อง: the equipment categories plus การใช้ไฟฟ้า (real structure Q3), which has no items.
TOPICS = dict(Case.Topic.choices)
USAGE = Case.Topic.USAGE
SUGGESTION = Case.Topic.SUGGESTION
NO_ITEMS = (USAGE, SUGGESTION)  # topics a requester tells as a story, without items


def _validate_sections(entries):
    """Q12: {kinds, category | items, division, section} proposes a แผนก inside the owner."""
    if not isinstance(entries, list):
        raise ValidationError("sections is a list.")
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not set(entry.get("kinds", [])) <= set(K.values)
            or not entry.get("kinds")
            or bool(entry.get("category")) == bool(entry.get("items"))
            or (entry.get("category") and entry["category"] not in TOPICS)
        ):
            raise ValidationError(
                "sections entries are {kinds, category | items, division, section}."
            )
        found = Section.objects.filter(
            division__code=entry.get("division"), name=entry.get("section"), is_active=True
        ).exists()
        if not found:
            raise ValidationError(
                f"Unknown section {entry.get('division')} / {entry.get('section')}."
            )


def validate_document(document):
    """Reject a policy the engine could misread; division codes must exist."""
    if not isinstance(document, dict):
        raise ValidationError("A policy is a JSON object.")
    for key in ("code", "effective_from", "units", "responsibility", "priority"):
        if key not in document:
            raise ValidationError(f"Policy is missing {key}.")
    _parse_moment(document["effective_from"], "effective_from")
    _parse_moment(document.get("effective_until"), "effective_until")
    units = document["units"]
    if not units.get("coordinator"):
        raise ValidationError("units.coordinator is required.")
    # ADR 0008: intake and registry units are optional (the real structure has neither).
    codes = {units[name] for name in ("intake", "coordinator", "registry") if units.get(name)}
    for entry in document["responsibility"]:
        if entry.get("rule") not in {"R01", "R02", "R03", "R04", "R05"}:
            raise ValidationError("Responsibility entries use R01-R05.")
        if not set(entry.get("kinds", [])) or not set(entry["kinds"]) <= set(K.values):
            raise ValidationError(f"{entry['rule']}: unknown case kinds.")
        if bool(entry.get("category")) == bool(entry.get("items")):
            raise ValidationError(f"{entry['rule']}: give a category or a list of items.")
        if entry.get("category") and entry["category"] not in TOPICS:
            raise ValidationError(f"{entry['rule']}: unknown category.")
        for name in ("effective_from", "effective_until"):
            _parse_moment(entry.get(name), f"{entry['rule']} {name}")
        codes.add(entry.get("owner"))
    known = set(
        Division.objects.filter(code__in=[c for c in codes if c]).values_list("code", flat=True)
    )
    if None in codes or codes - known:
        raise ValidationError(f"Unknown division codes: {sorted(c for c in codes - known if c)}")
    priority = document["priority"]
    minutes = priority.get("ack_minutes", {})
    if set(minutes) != {*PRIORITIES, "none"} or any(
        not isinstance(v, int) or v <= 0 for v in minutes.values()
    ):
        raise ValidationError("ack_minutes needs a positive whole number for P0-P3 and none.")
    if any(level not in PRIORITIES for level in priority.get("floors", {}).values()):
        raise ValidationError("Priority floors must be P0-P3.")
    for entry in document.get("equipment_terms", []):
        if (
            not isinstance(entry, dict)
            or not str(entry.get("term", "")).strip()
            or entry.get("category") not in TOPICS
            or not isinstance(entry.get("items", []), list)
        ):
            raise ValidationError("equipment_terms entries are {term, category, items?}.")
    _validate_sections(document.get("sections", []))
    for name in ("remind_after", "escalate_after"):
        if not isinstance(priority.get(name), int) or priority[name] <= 0:
            raise ValidationError(f"{name} must be a positive whole number.")
    if priority["escalate_after"] <= priority["remind_after"]:
        raise ValidationError("Escalation must come after the reminder.")
    if "calendar" in document:
        problems = servicetime.validate(document["calendar"])
        if problems:
            raise ValidationError(f"calendar: {' '.join(problems)}")
    for kind, table in (document.get("deadlines") or {}).items():
        if kind not in DEADLINE_KINDS:
            raise ValidationError(f"Unknown deadline {kind}.")
        if set(table) != {*PRIORITIES, "none"} or any(
            not isinstance(v, int) or v <= 0 for v in table.values()
        ):
            raise ValidationError(f"deadline {kind} needs positive minutes for P0-P3 and none.")
    if not isinstance(document.get("emergency_guidance", ""), str):
        raise ValidationError("emergency_guidance is text.")
    return document


def load_document(path=DEMO_POLICY):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


@transaction.atomic
def import_policy(document, actor=None):
    """Store a policy once; the same code with different content is refused (import a new code)."""
    if actor is not None:
        identity.require_admin(actor)
    validate_document(document)
    digest = digest_of(document)
    existing = PolicyVersion.objects.filter(code=document["code"]).first()
    if existing is not None:
        if existing.digest != digest:
            raise ValidationError(f"{document['code']} was imported with different content.")
        return existing
    version = PolicyVersion.objects.create(
        code=document["code"],
        document=document,
        digest=digest,
        is_synthetic=bool(document.get("is_synthetic")),
        effective_from=_parse_moment(document["effective_from"], "effective_from"),
        effective_until=_parse_moment(document.get("effective_until"), "effective_until"),
        imported_by=actor,
    )
    identity.audit(
        actor,
        "policy.imported",
        version,
        actor_label="" if actor else "manage.py",
        code=version.code,
        digest=digest,
    )
    return version


def active_policy():
    return PolicyVersion.objects.filter(status=PolicyVersion.Status.ACTIVE).first()


def activate_policy(actor, version, approved_by_name, approval_reference, reason):
    """Install an approved rule set; open cases get new proposals (owners/priorities stay).

    The rule owner approves; the admin only installs (B.8). `actor` None = manage.py on a
    demo database.
    """
    if actor is not None:
        identity.require_admin(actor)
    reason = identity._require_reason(reason)
    approved_by_name = identity._require_reason(approved_by_name)
    approval_reference = identity._require_reason(approval_reference)
    with transaction.atomic():
        version = PolicyVersion.objects.select_for_update().get(pk=version.pk)
        if version.status == PolicyVersion.Status.ACTIVE:
            raise ValidationError("This policy version is already active.")
        previous = list(
            PolicyVersion.objects.select_for_update().filter(status=PolicyVersion.Status.ACTIVE)
        )
        for old in previous:
            old.status = PolicyVersion.Status.RETIRED
            old.save(update_fields=["status"])
        version.status = PolicyVersion.Status.ACTIVE
        version.approved_by_name = approved_by_name
        version.approval_reference = approval_reference
        version.activated_by = actor
        version.activated_at = timezone.now()
        version.save(
            update_fields=[
                "status",
                "approved_by_name",
                "approval_reference",
                "activated_by",
                "activated_at",
            ]
        )
        identity.audit(
            actor,
            "policy.activated",
            version,
            reason,
            actor_label="" if actor else "manage.py",
            code=version.code,
            previous=[p.code for p in previous],
            approved_by=approved_by_name,
            reference=approval_reference,
        )
    return version, reevaluate_open_cases()


def reevaluate_open_cases():
    """New proposals for every open case under the active policy (Q20); one transaction each."""
    changed = 0
    for case_id in Case.objects.exclude(status__in=workflow.TERMINAL).values_list("pk", flat=True):
        with transaction.atomic():
            case = Case.objects.select_for_update().select_related("owner_division").get(pk=case_id)
            changed += bool(refresh(case, "policy_changed"))
    return changed


@dataclass
class Policy:
    version: PolicyVersion

    @property
    def doc(self):
        return self.version.document

    @property
    def units(self):
        return self.doc["units"]

    def in_effect(self, now):
        start = self.version.effective_from
        end = self.version.effective_until
        return start <= now and (end is None or now < end)

    def entry_in_effect(self, entry, now):
        start = _parse_moment(entry.get("effective_from"), "effective_from")
        end = _parse_moment(entry.get("effective_until"), "effective_until")
        return (start is None or start <= now) and (end is None or now < end)

    def ack_minutes(self, priority):
        table = (self.doc.get("deadlines") or {}).get("ack") or self.doc["priority"]["ack_minutes"]
        return table[priority if priority in PRIORITIES else "none"]

    def deadline_minutes(self, kind, priority):
        """Minutes for a work deadline, or None when this policy defines none (M06 policies)."""
        table = (self.doc.get("deadlines") or {}).get(kind)
        if table is None:
            return self.ack_minutes(priority) if kind == "child_accept" else None
        return table[priority if priority in PRIORITIES else "none"]

    @property
    def calendar(self):
        return servicetime.Calendar(self.doc.get("calendar"))

    @property
    def emergency_guidance(self):
        return self.doc.get("emergency_guidance", "")

    def floor_for(self, facts):
        floors = self.doc["priority"].get("floors", {})
        basis = [name for name in sorted(floors) if (facts.get(name) or {}).get("value") is True]
        return more_urgent(*(floors[name] for name in basis)), basis


def current_policy():
    version = active_policy()
    return Policy(version) if version else None


# --- Facts -------------------------------------------------------------------------------------


def _fact(value, source, evidence=""):
    return {"value": value, "source": source, "evidence": evidence}


def case_text(case):
    return {name: getattr(case, name) for name in TEXT_FIELDS if getattr(case, name)}


def _negated(text, start, negations):
    """A negation word right before the match, within the same whitespace-separated chunk."""
    window = re.split(r"\s", text[max(0, start - 15) : start])[-1]
    return any(negation in window for negation in negations)


def hazard_keywords(texts, policy):
    """First unnegated hazard term with its context, or "" (over-flagging is accepted).

    Reads the raw text on purpose: missing a hazard is worse than flagging too much (Q39).
    """
    if policy is None:
        return ""
    terms = sorted(policy.doc.get("hazard_terms", []), key=len, reverse=True)
    negations = policy.doc.get("negations", [])
    for text in texts.values():
        for term in terms:
            for match in re.finditer(re.escape(term), text):
                if _negated(text, match.start(), negations):
                    continue
                return text[max(0, match.start() - 20) : match.end() + 20].strip()
    return ""


def _strip_blocks(text):
    """Code blocks and JSON objects are never how a requester names equipment."""
    from operations import ai_harness

    text = ai_harness.CODE_BLOCK.sub("\n", text or "")
    return ai_harness.JSON_BLOB.sub(
        lambda m: "\n" if re.search(r"\"\s*:", m.group()) else m.group(), text
    )


def undirected_text(text):
    """The requester's text without any line the fixed M05 rules mark as a directive (Q39).

    Code blocks and JSON go first; then a whole line is dropped when any of its segments has
    markup or software words or matches the harness cut list ("ระบบ:", "ให้ระบบ" ...). Lines,
    not segments: the segmenter splits a role tag such as "[assistant]" from the words after
    it, which must go with it. No model is called; a real equipment name on such a line is
    lost too and the case goes to people (R10)."""
    from operations import ai_harness, ai_screen

    kept = []
    for line in _strip_blocks(text).split("\n"):
        parts = ai_screen.segments(line)
        if any(ai_screen.rule_flags(p) or ai_harness.minimize(p.text)[1] for p in parts):
            continue
        kept.append(line)
    return "\n".join(kept)


def _bounded(token):
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", re.I)


def _rows(pairs):
    rows = []
    for category, item in pairs:
        row = {"category": category, "item": item}
        if row not in rows:
            rows.append(row)
    return rows


def _label_rows(rows):
    return ", ".join(f"{r['category']}:{r['item'] or '-'}" for r in rows) or "ไม่พบ"


def equipment_terms(text, policy, catalog):
    """คำเรียกอุปกรณ์ (Q32-Q36): rows found, with candidates when a term is wider than one item.

    Longer terms win over the shorter terms they contain; negated terms are skipped; a term
    that maps to several active items gives the category only and lists the candidates.
    """
    if policy is None or not text:
        return [], []
    active = {item.code: item.category for item in catalog}
    negations = policy.doc.get("negations", [])
    spans = []
    for entry in policy.doc.get("equipment_terms", []):
        for match in re.finditer(re.escape(entry["term"]), text, re.I):
            if not _negated(text, match.start(), negations):
                spans.append((match.start(), match.end(), entry))
    accepted = []
    for start, end, entry in sorted(spans, key=lambda s: (s[0] - s[1], s[0])):
        if all(end <= a_start or start >= a_end for a_start, a_end, _ in accepted):
            accepted.append((start, end, entry))
    pairs, candidates = [], []
    for _, _, entry in sorted(accepted, key=lambda s: s[0]):
        category = entry["category"]
        items = [code for code in entry.get("items", []) if active.get(code) == category]
        if len(items) == 1:
            pairs.append((category, items[0]))
        else:
            pairs.append((category, None))
            if len(items) > 1:
                candidates.append({"category": category, "items": items, "term": entry["term"]})
    specific = {category for category, item in pairs if item}
    pairs = [(c, i) for c, i in pairs if i or c not in specific]  # a named item beats its category
    return _rows(pairs), candidates


def _combine_text(terms, ai_rows):
    """Level 6 (Q34): term list and AI agree, or only one of them speaks; otherwise conflict."""
    if not terms and not ai_rows:
        return [], "", False
    if not ai_rows:
        return terms, "terms", False
    if not terms:
        return ai_rows, "ai", False
    categories = {r["category"] for r in terms}
    if categories != {r["category"] for r in ai_rows}:
        return [], "terms+ai", True
    pairs = []
    for category in sorted(categories):
        named_t = {r["item"] for r in terms if r["category"] == category and r["item"]}
        named_a = {r["item"] for r in ai_rows if r["category"] == category and r["item"]}
        if named_t and named_a and named_t != named_a:
            return [], "terms+ai", True
        named = sorted(named_t | named_a)
        pairs += [(category, item) for item in named] or [(category, None)]
    return _rows(pairs), "terms+ai", False


def derive_equipment(data, policy, catalog, ai_equipment=()):
    """Equipment for a case without lines, from the highest source that finds any (Q34):
    lines > serial in the requester's history > item code in the text > related case > text.
    Lower sources that disagree are noted. `data` is plain data, so evaluation and production
    run the same code."""
    if data["lines"]:
        return {**_fact(_rows((line["category"], line["item"]) for line in data["lines"]), "case")}
    topic = data.get("topic") or ""
    if topic in NO_ITEMS:
        # การใช้ไฟฟ้า and ข้อเสนอแนะ have no items: the requester's topic decides.
        return _fact(_rows([(topic, None)]), "case")
    catalog_category = {item.code: item.category for item in catalog}
    text = "\n".join(undirected_text(t) for t in data["texts"].values())
    by_serial = {}
    for line in data.get("history", []):
        for serial in line.get("serials", []):
            by_serial.setdefault(str(serial), set()).add(line["item_code"])
    serial_pairs = []
    for serial, codes in by_serial.items():
        if len(codes) == 1 and _bounded(serial).search(text):
            code = next(iter(codes))
            if code in catalog_category:
                serial_pairs.append((catalog_category[code], code))
    code_pairs = [
        (category, code)
        for code, category in catalog_category.items()
        if _bounded(code).search(text)
    ]
    related_pairs = [
        (catalog_category[line["item_code"]], line["item_code"])
        for line in data.get("related", [])
        if line["item_code"] in catalog_category
    ]
    terms, candidates = equipment_terms(text, policy, catalog)
    ai_rows = _rows((r["category"], r.get("item")) for r in ai_equipment or ())
    text_rows, text_source, conflict = _combine_text(terms, ai_rows)
    code_rows = _rows(code_pairs)
    code_conflict = False
    if code_rows and text_rows:
        # A typed code that disagrees with the words around it goes to people: a code is easy
        # to inject and outranks the words otherwise (red-team T04/T06, Q34 amended).
        _, _, code_conflict = _combine_text(code_rows, text_rows)
    levels = [
        ("serial", _rows(serial_pairs)),
        ("code", [] if code_conflict else code_rows),
        ("related", _rows(related_pairs)),
        (text_source or "terms", [] if code_conflict else text_rows),
        (
            "case",
            _rows([(topic, None)]) if topic else [],
        ),  # the chosen topic, when nothing names more
    ]
    chosen = next(((source, rows) for source, rows in levels if rows), None)
    notes = []
    if code_conflict:
        notes.append(
            f"รหัสในข้อความขัดกับคำในข้อความ: {_label_rows(code_rows)} / {_label_rows(text_rows)}"
        )
    if chosen is None:
        fact = _fact([], text_source or "case")
        if conflict:
            fact["conflict"] = {"terms": terms, "ai": ai_rows}
            notes.append(
                f"ข้อเท็จจริงอุปกรณ์ขัดกัน: คำเรียกอุปกรณ์ {_label_rows(terms)} / AI {_label_rows(ai_rows)}"
            )
    else:
        source, rows = chosen
        fact = _fact(rows, source)
        for other, other_rows in levels:
            if other != source and other_rows and other_rows != rows:
                notes.append(f"แหล่งอื่นพบ ({other}): {_label_rows(other_rows)}")
    if conflict and chosen is not None:
        notes.append(f"คำเรียกอุปกรณ์กับ AI ไม่ตรงกัน: {_label_rows(terms)} / {_label_rows(ai_rows)}")
    if candidates and (chosen is None or chosen[0] in ("terms", "terms+ai")):
        fact["candidates"] = candidates
        notes.append(
            "รายการที่เป็นไปได้: "
            + "; ".join(f"{c['term']} → {', '.join(c['items'])}" for c in candidates)
        )
    if notes:
        fact["notes"] = notes
    return fact


def build_facts(data, staff, ai_facts, policy, catalog):
    """Facts with their source; staff > case data > AI and keyword (which only add flags)."""
    staff = staff or {}
    ai_facts = ai_facts or {}

    def from_staff(name):
        entry = staff.get(name)
        if entry is None:
            return None
        return _fact(entry["value"], "staff", entry.get("reason", ""))

    equipment_fact = from_staff("equipment") or derive_equipment(
        data, policy, catalog, ai_facts.get("equipment")
    )
    keyword = hazard_keywords(data["texts"], policy)
    ai_hazard = ai_facts.get("hazard_reported") or {}
    if staff.get("hazard_reported"):
        hazard = from_staff("hazard_reported")
    elif ai_hazard.get("value") is True:
        hazard = _fact(True, "ai", ai_hazard.get("quote", ""))
    elif keyword:
        hazard = _fact(True, "keyword", keyword)
    else:
        hazard = _fact(False, "case")

    ai_impact = ai_facts.get("severe_impact") or {}
    if staff.get("severe_impact"):
        severe = from_staff("severe_impact")
    elif ai_impact.get("value") in (True, False):
        severe = _fact(ai_impact["value"], "ai", ai_impact.get("quote", ""))
    else:
        severe = _fact(None, "case")

    ai_registry = ai_facts.get("registry_only") or {}
    if staff.get("registry_only"):
        registry = from_staff("registry_only")
    elif ai_registry.get("value") is True and data["kind"] == K.PROBLEM:
        registry = _fact(True, "ai", ai_registry.get("quote", ""))
    else:
        registry = _fact(False, "case")

    facts = {
        "kind": data["kind"],
        "equipment": equipment_fact,
        "hazard_reported": hazard,
        "severe_impact": severe,
        "registry_only": registry,
        "needed_by": data.get("needed_by"),
    }
    # M07: withdrawals read ของไม่พอ from the inventory and a receipt mismatch from deliveries
    # (case data); a person's confirmation still wins.
    for name in STAFF_ONLY_FACTS:
        facts[name] = from_staff(name) or data.get(name) or _fact(False, "case")
    return facts


def case_data(case):
    """What the rules read from the database; history is this requester's other cases only."""
    lines = [
        {"category": line.item.category, "item": line.item.code}
        for line in case.lines.select_related("item").order_by("line_no")
    ]
    history = [
        {"item_code": code, "serials": serials}
        for code, serials in CaseLine.objects.filter(case__requester_id=case.requester_id)
        .exclude(case=case)
        .exclude(serials=[])
        .order_by("-case__submitted_at")
        .values_list("item__code", "serials")[:500]
    ]
    related = []
    if case.related_case_id:
        related = [
            {"item_code": code, "serials": serials}
            for code, serials in CaseLine.objects.filter(case_id=case.related_case_id).values_list(
                "item__code", "serials"
            )
        ]
    data = {
        "kind": case.kind,
        "topic": case.topic,
        "lines": lines,
        "texts": case_text(case),
        "needed_by": case.needed_by.isoformat() if case.needed_by else None,
        "history": history,
        "related": related,
    }
    if case.kind == K.WITHDRAWAL:
        from operations import fulfilment  # local: fulfilment builds on this module

        data.update(fulfilment.case_facts(case))
    elif case.kind == K.RETURN:
        from operations import returns  # local: returns builds on this module

        data.update(returns.case_facts(case))
    return data


def input_digest(case):
    """What the AI reads: kind, items and the case text (not identities, staff facts or status)."""
    lines = [
        [line.item.code, str(line.requested_quantity or ""), list(line.serials)]
        for line in case.lines.select_related("item").order_by("line_no")
    ]
    payload = {"kind": case.kind, "lines": lines, "text": case_text(case)}
    return digest_of(payload)


def latest_ai_run(case):
    return (
        AITriageRun.objects.filter(
            case=case, status=AITriageRun.Status.DONE, input_digest=input_digest(case)
        )
        .order_by("-pk")
        .first()
    )


def active_catalog():
    return list(EquipmentType.objects.filter(is_active=True).order_by("code"))


def case_facts(case, state, ai_run, policy):
    return build_facts(
        case_data(case),
        state.staff_facts,
        ai_run.facts if ai_run else {},
        policy,
        active_catalog(),
    )


# --- Engine (pure: facts + policy -> proposal) -------------------------------------------------

NEXT_ACTIONS = {
    "R01": "เสนอให้กองมิเตอร์ตอบรับงานเบิกและตรวจตามสิทธิ์ ยังไม่อนุมัติหรือจ่ายของ",
    "R02": "เสนอให้กองหม้อแปลงตอบรับงานเบิกตามงานและข้อมูลผลกระทบ ไม่เดาสเปกทดแทน",
    "R03": "เสนอให้กองอุปกรณ์อื่นตอบรับหลังตรวจว่ารายการอยู่ในบัญชีรับผิดชอบ",
    "R04": "ประสานรับคืน ตรวจจำนวน และรอผู้มีสิทธิ์ตรวจสภาพ ก่อนถือว่าพร้อมจ่าย",
    "R05": "ให้ผู้รับผิดชอบคัดกรองอาการ ผลกระทบ และสิ่งที่ยังไม่ทราบ",
    "R06": "สรุปส่วนต่างและเอกสารให้กองทะเบียน ไม่แก้ข้อมูลทางการเอง",
    "R07": "คงเจ้าภาพเดิมจนงานส่งมอบจบ ให้กองทะเบียนตรวจทะเบียนเป็นงานย่อย",
    "R09": "จุดรับเรื่องกลางประสานเรื่องหลัก แยกงานย่อยให้แต่ละกอง (งานย่อยจริงใน M09)",
    "R10": "ถามข้อมูลที่จำเป็นและให้คนตรวจ ไม่ส่งเข้ากองที่เดาขึ้นเอง",
}


# ADR 0008: the same rules where there is no intake desk or registry unit.
NEXT_ACTIONS_DIRECT = {
    "R06": "กองเจ้าภาพตรวจเอกสารและบันทึกทะเบียนของตัวเอง ไม่แก้ข้อมูลทางการโดยไม่มีเอกสาร",
    "R07": "คงเจ้าภาพเดิมจนงานส่งมอบจบ และตรวจทะเบียนของกองตัวเอง",
    "R09": "กองเจ้าภาพมอบส่วนที่เป็นของกองอื่นเป็นงานย่อย",
    "R10": "ถามข้อมูลที่จำเป็นและให้ผู้ประสานระดับฝ่ายเลือกกอง ไม่ส่งเข้ากองที่เดาขึ้นเอง",
}


def _match(policy, kind, equipment, now):
    """("ok", rule, owner) or (problem, rule, None) for one piece of equipment."""
    entries = [
        e
        for e in policy.doc["responsibility"]
        if kind in e["kinds"]
        and (
            (equipment.get("item") and equipment["item"] in e.get("items", []))
            or (e.get("category") and e["category"] == equipment.get("category"))
        )
    ]
    if not entries:
        return "no_rule", None, None
    live = [e for e in entries if policy.entry_in_effect(e, now)]
    if not live:
        return "expired", entries[0]["rule"], None
    owners = {e["owner"] for e in live}
    if len(owners) > 1:
        return "conflict", "/".join(sorted({e["rule"] for e in live})), None
    return "ok", live[0]["rule"], owners.pop()


def match(policy, kind, item, now):
    """("ok" | problem, rule, owner code) for one item of a request (ADR 0008 splitting)."""
    return _match(policy, kind, {"category": item.category, "item": item.code}, now)


def _label(equipment):
    category = TOPICS.get(equipment.get("category"), "ไม่ทราบหมวด")
    return f"{category} {equipment['item']}" if equipment.get("item") else category


def evaluate(policy, facts, current_owner, now):
    """The proposal for these facts. Precedence (B.3): urgent hazard screening → R10 → R09 →
    R07 → R06 → R01-R05; R08 only adds a support unit and a decision maker."""
    hazard = facts["hazard_reported"]["value"] is True
    severe = facts["severe_impact"]["value"]
    kind = facts["kind"]
    notes, missing = [], []
    floor, floor_basis = policy.floor_for(facts) if policy else ("", [])
    if hazard:
        missing.append("ผลประเมินของผู้รับผิดชอบว่าอันตรายที่รายงานเกิดจริงหรือไม่")
        notes.append("รายงานอันตรายยังไม่ใช่การยืนยันว่ามีเหตุจริง ไม่สั่งงานภาคสนาม")
    if policy is None or not policy.in_effect(now):
        desk = (policy.units.get("intake") or policy.units.get("coordinator")) if policy else None
        return {
            "rules": ["R10"],
            "proposed_owner": desk,
            "support_units": [],
            "child_work_owners": [],
            "suggested_priority": "P0" if hazard else "",
            "priority_floor": floor,
            "floor_basis": floor_basis,
            "missing": missing,
            "next_action": NEXT_ACTIONS["R10"],
            "notes": ["ไม่มีกติกาที่มีผล ณ เวลานี้ ทุกเรื่องให้คนตรวจ", *notes],
        }
    units = policy.units
    # ADR 0008: without a central intake, unclear cases go to the coordinator and nobody is a
    # registry unit; each division keeps its own registry work.
    intake, registry = units.get("intake"), units.get("registry")
    desk = intake or units["coordinator"]
    owners, problems = {}, []
    equipment = facts["equipment"]["value"] or []
    if not equipment:
        problems.append("ไม่ทราบอุปกรณ์ที่เกี่ยวข้อง")
        missing.append("อุปกรณ์ที่เกี่ยวข้อง")
    for row in equipment:
        status, rule, owner = _match(policy, kind, row, now)
        if status == "ok":
            owners.setdefault(owner, set()).add(rule)
        elif status == "expired":
            problems.append(f"กติกา {rule} สำหรับ{_label(row)}หมดอายุหรือยังไม่มีผล")
        elif status == "conflict":
            problems.append(f"กติกา {rule} สำหรับ{_label(row)}ขัดกัน")
        else:
            problems.append(f"ไม่พบกติกาที่ชัดสำหรับ{_label(row)}")
            if row.get("category") == EquipmentType.Category.OTHER and not row.get("item"):
                missing.append("รหัสหรือชื่อรายการของ")
    serial_mismatch = facts["serial_mismatch"]["value"] is True
    registry_only = facts["registry_only"]["value"] is True and kind == K.PROBLEM
    support, child = [], []
    notes.extend(facts["equipment"].get("notes", []))
    if problems:
        rules, proposed = ["R10"], desk
        notes.extend(problems)
    elif len(owners) > 1:
        if intake:
            rules, proposed = ["R09"], intake
            support = child = sorted(owners)
        else:
            # The owner keeps the case and gives the other divisions their part as child work.
            proposed = current_owner if current_owner in owners else next(iter(owners))
            rules = ["R09"]
            support = child = sorted(o for o in owners if o != proposed)
        notes.append(
            "งานย่อยต่อกอง: "
            + ", ".join(f"{o} ({'/'.join(sorted(r))})" for o, r in sorted(owners.items()))
        )
    elif serial_mismatch and current_owner != intake:
        rules, proposed = ["R07"], current_owner
        support = [registry] if registry else []
        notes.append("ไม่โยนทั้งเรื่องไปกองทะเบียน เจ้าภาพขั้นส่งมอบยังรับผิดชอบ")
    elif registry_only and registry:
        rules, proposed = ["R06"], registry
        support = [o for o in sorted(owners) if o != registry]
    elif registry_only:
        ((proposed, _),) = owners.items()  # the division records its own registry facts
        rules = ["R06"]
    else:
        ((proposed, owner_rules),) = owners.items()
        rules = sorted(owner_rules)
        if kind == K.PROBLEM:
            notes.append("ไม่มีข้อเท็จจริงขอแก้ทะเบียนล้วน จึงไม่เข้า R06")
    if serial_mismatch and intake and current_owner == intake:
        notes.append("เลขไม่ตรงระหว่างส่งมอบใช้เมื่อมีกองเจ้าภาพขั้นรับ/จ่ายแล้ว (R07)")
    if facts["shortage"]["value"] is True and rules != ["R10"]:
        rules = [*rules, "R08"]
        if registry and registry not in support:
            support = [*support, registry]
        notes.append("ของไม่พอไม่ใช่เหตุลดระดับหรือเปลี่ยนเจ้าภาพ ผู้มีอำนาจจัดสรรเป็นผู้ตัดสิน")
    if hazard:
        suggested = "P0"
    elif rules == ["R10"]:
        suggested = ""
    elif severe is True:
        suggested = "P1"
    elif kind == K.PROBLEM and "R06" not in rules:
        suggested = "P2" if severe is False else ""
        if severe is None:
            missing.append("ผลกระทบต่องานหรือผู้ใช้ไฟ")
    elif facts.get("needed_by"):
        suggested = "P2"
    else:
        suggested = "P3"
    if not suggested:
        notes.append("ข้อมูลยังไม่พอ ระดับเป็นรอคัดกรอง ไม่แทนด้วย P3")
    if any(facts[n]["source"] == "ai" for n in FACT_NAMES if isinstance(facts.get(n), dict)):
        notes.append("มีข้อเท็จจริงจากข้อเสนอของ AI ที่ยังไม่ได้ยืนยัน")
    actions = NEXT_ACTIONS if intake or registry else {**NEXT_ACTIONS, **NEXT_ACTIONS_DIRECT}
    next_action = " / ".join(actions[r] for r in rules if r in actions)
    if "R08" in rules:
        next_action += " / ให้ผู้มีอำนาจตรวจผลกระทบและพิจารณาแผนจัดสรร"
    return {
        "rules": rules,
        "proposed_owner": proposed,
        "support_units": support,
        "child_work_owners": child,
        "suggested_priority": suggested,
        "priority_floor": floor,
        "floor_basis": floor_basis,
        "missing": missing,
        "next_action": next_action,
        "notes": notes,
    }


# --- State, proposals and clocks ---------------------------------------------------------------


def state_for(case):
    """The M06 state row (created on first use; the caller holds the case lock)."""
    state, _ = CaseTriage.objects.select_for_update().get_or_create(case=case)
    return state


def effective_priority(state):
    """The higher of confirmed and floor, unless an authority exception waived this floor."""
    floor = state.priority_floor
    exception = state.floor_exception or {}
    basis = state.current_recommendation.floor_basis if state.current_recommendation else []
    if floor and exception.get("floor") == floor and exception.get("basis") == basis:
        floor = ""
    return more_urgent(state.confirmed_priority, floor)


def _division(code, fallback):
    division = Division.objects.filter(code=code, is_active=True).first() if code else None
    return division or fallback


def refresh(case, trigger, now=None):
    """Recompute the proposal for the case's current revision; returns True if it changed.

    Stored proposals are never edited: a different result is a new row, older rows remain as
    stale proposals (S16). Owner, status and confirmed priority are not touched.
    """
    now = now or timezone.now()
    state = state_for(case)
    policy = current_policy()
    ai_run = latest_ai_run(case)
    facts = case_facts(case, state, ai_run, policy)
    result = evaluate(policy, facts, case.owner_division.code, now)
    learned = policy is not None and policy.units.get("intake") is None
    if learned:
        from operations import ai_keywords, ai_routing, routing_words  # local: they read this

        result = routing_words.apply(case, result)  # คำที่ระบบจำ for an R10 case (ADR 0013)
        result = ai_routing.apply(case, result)  # then the routing AI's proposal (Q15)
    digest = digest_of(
        {
            "revision": case.revision,
            "policy": policy.version.pk if policy else None,
            "facts": facts,
            "result": result,
            "ai": ai_run.pk if ai_run else None,
        }
    )
    current = state.current_recommendation
    changed = current is None or current.digest != digest
    if changed:
        intake = Division.objects.filter(is_intake=True).first() or case.owner_division
        recommendation = Recommendation.objects.create(
            case=case,
            case_revision=case.revision,
            policy_version=policy.version if policy else None,
            trigger=trigger[:32],
            facts=facts,
            rules=result["rules"],
            current_owner=case.owner_division,
            proposed_owner=_division(result["proposed_owner"], intake),
            support_units=result["support_units"],
            child_work_owners=result["child_work_owners"],
            suggested_priority=result["suggested_priority"],
            priority_floor=result["priority_floor"],
            floor_basis=result["floor_basis"],
            missing=result["missing"],
            next_action=result["next_action"],
            notes=result["notes"],
            ai_run=ai_run,
            digest=digest,
        )
        outcome = ("rules", "proposed_owner_id", "suggested_priority", "priority_floor")
        if (
            trigger == "policy_changed"
            and current is not None
            and any(getattr(current, f) != getattr(recommendation, f) for f in outcome)
        ):
            state.policy_change_pending = True
        state.current_recommendation = recommendation
    state.suggested_priority = state.current_recommendation.suggested_priority
    state.priority_floor = state.current_recommendation.priority_floor
    state.effective_priority = effective_priority(state)
    state.save()
    reconcile_clocks(case, state, policy, now)
    maybe_request_ai(case, now)
    if learned:
        ai_routing.maybe_request(case, result, now)
        ai_keywords.maybe_request(case, result, now)  # 15 ต.ค. 2569 Q9: words for the handoff
    return changed


def section_for(case, owner_code=None, current=None):
    """Q12: the แผนก the policy proposes inside the owner division, or None (the division
    chooses). The first matching entry wins; a named item beats nothing, a category any item."""
    current = current if current is not None else current_policy()
    entries = current.doc.get("sections", []) if current else []
    owner_code = owner_code or case.owner_division.code
    rows = [
        {"category": line.item.category, "item": line.item.code}
        for line in case.lines.select_related("item")
    ] or ([{"category": case.topic, "item": None}] if case.topic else [])
    for entry in entries:
        if entry["division"] != owner_code or case.kind not in entry["kinds"]:
            continue
        if any(
            (row["item"] and row["item"] in entry.get("items", []))
            or (entry.get("category") and entry["category"] == row["category"])
            for row in rows
        ):
            return Section.objects.filter(
                division__code=owner_code, name=entry["section"], is_active=True
            ).first()
    return None


def after_submit(case):
    refresh(case, "submitted", case.submitted_at)


def after_decision(case, action, actor_is_staff):
    """Every accepted decision: a new proposal for the new revision; a person acting on the case
    has seen a policy-change flag."""
    if actor_is_staff:
        CaseTriage.objects.filter(case=case).update(policy_change_pending=False)
    refresh(case, action)


def _clock_priority(state):
    return state.effective_priority or state.suggested_priority or ""


ACK_KINDS = {AckClock.Kind.INTAKE, AckClock.Kind.HANDOFF}
TABLE = {
    AckClock.Kind.CHILD_ACCEPT: "child_accept",
    AckClock.Kind.FIRST_RESPONSE: "first_response",
    AckClock.Kind.RESOLVE: "resolve",
    AckClock.Kind.APPROVAL: "approval",
    AckClock.Kind.ISSUE: "issue",
    AckClock.Kind.INSPECTION: "inspection",
    AckClock.Kind.CHILD_RESULT: "child_result",
}


def _minutes(policy, kind, priority):
    if kind in ACK_KINDS:
        return policy.ack_minutes(priority)
    return policy.deadline_minutes(TABLE[kind], priority)


def _times(policy, priority, started_at, minutes=None, paused=0):
    """Reminder at 1x and escalation at 2x of the minutes, on the service calendar (M09 Q7)."""
    minutes = policy.ack_minutes(priority) if minutes is None else minutes
    rules = policy.doc["priority"]
    calendar = policy.calendar
    return (
        calendar.add(started_at, minutes * rules["remind_after"] + paused, priority),
        calendar.add(started_at, minutes * rules["escalate_after"] + paused, priority),
    )


def _status_in(case, statuses):
    """The case is in one of these statuses, or paused and returning to one of them."""
    return case.status in statuses or (
        case.status in workflow.PAUSED and case.resume_status in statuses
    )


def _entered(case, **filters):
    decision = case.decisions.filter(**filters).order_by("-id").first()
    return decision.created_at if decision else None


def _wanted_work_clocks(case):
    """{(kind, child_work_id): (division_id, started_at)} for FAOJ's steps running now (Q8).

    Waits on the district (information, goods on the way, confirmation) have no deadline."""
    from operations import coordination  # local: coordination builds on this module

    K_ = AckClock.Kind
    owner = case.owner_division_id
    wanted = {}
    if case.kind == K.PROBLEM:
        if _status_in(case, {S.SUBMITTED, S.TRIAGE}):
            wanted[(K_.FIRST_RESPONSE, None)] = (owner, case.submitted_at)
        if _status_in(case, {S.IN_PROGRESS}):
            start = _entered(case, action__in=["complete_triage", "dispute_resolution", "reopen"])
            if start:
                wanted[(K_.RESOLVE, None)] = (owner, start)
    elif case.kind == K.WITHDRAWAL:
        if _status_in(case, {S.AWAITING_APPROVAL}):
            start = _entered(case, action="complete_triage")
            if start:
                wanted[(K_.APPROVAL, None)] = (owner, start)
        if _status_in(case, {S.AWAITING_EXECUTION, S.IN_PROGRESS}):
            start = _entered(case, from_status=S.AWAITING_APPROVAL, to_status=S.AWAITING_EXECUTION)
            for line in case.lines.all():
                if start and line.outstanding > 0:
                    child = coordination.child_of(line)
                    key = (K_.ISSUE, child.pk if child else None)
                    wanted.setdefault(key, (child.division_id if child else owner, start))
    elif case.kind == K.RETURN:
        from operations.models import ReturnUnit

        for unit in ReturnUnit.objects.filter(
            case=case, status__in=[ReturnUnit.Status.AWAITING, ReturnUnit.Status.CONFLICT]
        ).select_related("arrival", "line"):
            child = coordination.child_of(unit.line) if unit.line_id else None
            key = (K_.INSPECTION, child.pk if child else None)
            division = child.division_id if child else owner
            arrived = unit.arrival.received_at
            if key not in wanted or arrived < wanted[key][1]:
                wanted[key] = (division, arrived)
    for child in case.child_works.all():
        if child.status in (ChildWork.Status.PENDING, ChildWork.Status.DISPUTED):
            wanted[(K_.CHILD_ACCEPT, child.pk)] = (child.division_id, child.requested_at)
        elif child.status == ChildWork.Status.ACCEPTED and not coordination.closes_itself(child):
            wanted[(K_.CHILD_RESULT, child.pk)] = (child.division_id, child.answered_at)
    return wanted


def _sync_pauses(case, now):
    """Q12: the district's turn pauses work deadlines by itself; a supervisor's pause ends with
    the hold. Returns the pauses of the case."""
    current = DeadlinePause.objects.select_for_update().filter(case=case, ended_at__isnull=True)
    for pause in current:
        over = (
            pause.kind == DeadlinePause.Kind.AWAITING_INFO and case.status != S.AWAITING_INFO
        ) or (pause.kind == DeadlinePause.Kind.SUPERVISOR and case.status != S.ON_HOLD)
        if over:
            pause.ended_at = now
            pause.save(update_fields=["ended_at"])
    if (
        case.status == S.AWAITING_INFO
        and not DeadlinePause.objects.filter(case=case, ended_at__isnull=True).exists()
    ):
        DeadlinePause.objects.create(
            case=case, kind=DeadlinePause.Kind.AWAITING_INFO, started_at=now
        )
    return list(DeadlinePause.objects.filter(case=case))


def _paused(clock, pauses, calendar):
    """Service minutes of ended pauses since the clock started, and the open pause's start."""
    total, open_from = 0, None
    for pause in pauses:
        begin = max(pause.started_at, clock.started_at)
        if pause.ended_at is None:
            open_from = begin
        elif pause.ended_at > begin:
            total += calendar.minutes_between(begin, pause.ended_at, clock.priority)
    return total, open_from


def reconcile_clocks(case, state, policy, now):
    """Clocks follow the case: intake runs while nobody started triage, a handoff clock while a
    handoff waits, and (M09) a work deadline while FAOJ has a step to do. Changing level
    re-times from the original start (no reset, B.4); pauses push the deadline out."""
    open_clocks = list(
        AckClock.objects.select_for_update().filter(case=case, stopped_at__isnull=True)
    )
    pending = Handoff.objects.filter(case=case, status=Handoff.Status.PENDING).first()
    wanted = {} if case.status in workflow.TERMINAL else _wanted_work_clocks(case)
    for clock in open_clocks:
        if clock.kind in ACK_KINDS:
            finished = (
                case.status in workflow.TERMINAL
                or (clock.kind == AckClock.Kind.INTAKE and case.status != S.SUBMITTED)
                or (
                    clock.kind == AckClock.Kind.HANDOFF
                    and (pending is None or clock.handoff_id != pending.pk)
                )
            )
        else:
            finished = (clock.kind, clock.child_work_id) not in wanted
            wanted.pop((clock.kind, clock.child_work_id), None)
        if finished:
            clock.stopped_at = now
            clock.save(update_fields=["stopped_at"])
    pauses = _sync_pauses(case, now)
    if policy is None or case.status in workflow.TERMINAL:
        return
    priority = _clock_priority(state)
    if (
        case.status == S.SUBMITTED
        and not AckClock.objects.filter(case=case, kind=AckClock.Kind.INTAKE).exists()
    ):
        intake = Division.objects.filter(is_intake=True).first() or case.owner_division
        due, escalate = _times(policy, priority, case.submitted_at)
        AckClock.objects.create(
            case=case,
            kind=AckClock.Kind.INTAKE,
            division=intake,
            started_at=case.submitted_at,
            priority=priority,
            due_at=due,
            escalate_at=escalate,
        )
    if pending is not None and not AckClock.objects.filter(handoff=pending).exists():
        due, escalate = _times(policy, priority, pending.requested_at)
        AckClock.objects.create(
            case=case,
            kind=AckClock.Kind.HANDOFF,
            handoff=pending,
            division=pending.to_division,
            started_at=pending.requested_at,
            priority=priority,
            due_at=due,
            escalate_at=escalate,
        )
    for (kind, child_id), (division_id, started_at) in wanted.items():
        minutes = _minutes(policy, kind, priority)
        if minutes is None:
            continue
        due, escalate = _times(policy, priority, started_at, minutes)
        AckClock.objects.create(
            case=case,
            kind=kind,
            child_work_id=child_id,
            division_id=division_id,
            started_at=started_at,
            priority=priority,
            due_at=due,
            escalate_at=escalate,
        )
    calendar = policy.calendar
    for clock in AckClock.objects.select_for_update().filter(case=case, stopped_at__isnull=True):
        minutes = _minutes(policy, clock.kind, priority)
        if minutes is None:
            continue
        clock.priority = priority
        paused, open_from = _paused(clock, pauses, calendar) if clock.work else (0, None)
        due, escalate = _times(policy, priority, clock.started_at, minutes, paused)
        changed = (clock.due_at, clock.escalate_at, clock.paused_minutes, clock.paused_at) != (
            due,
            escalate,
            paused,
            open_from,
        )
        if changed or clock.priority != priority:
            clock.due_at, clock.escalate_at = due, escalate
            clock.paused_minutes, clock.paused_at = paused, open_from
            clock.save(
                update_fields=["priority", "due_at", "escalate_at", "paused_minutes", "paused_at"]
            )


def _system_problem(now):
    stuck = sorted(
        event_type
        for event_type, seconds in orchestration.health(now)["oldest_pending_seconds"].items()
        if seconds > STUCK_SECONDS
    )
    return f"งานค้างในคิวระบบ: {', '.join(stuck)}" if stuck else ""


def sweep_clocks(now=None):
    """Remind at 1x, escalate at 2x, after checking the system works (S12). Answers that nobody
    gave (intake, handoff, child work) go to the coordinator; work deadlines go to the
    supervisors of the division that has the step (M09 Q9). Paused clocks wait.

    A reminder or escalation is an audit record plus a flag that queues show; silence is never
    taken as acceptance.
    """
    from operations import staff_email  # M10 (imported here: staff_email reads services)

    now = now or timezone.now()
    counts = {"reminded": 0, "escalated": 0, "held": 0}
    problem = None
    with transaction.atomic():
        due = (
            AckClock.objects.select_for_update(skip_locked=True)
            .filter(stopped_at__isnull=True, due_at__lte=now, paused_at__isnull=True)
            .exclude(case__is_example=True)  # ตัวอย่าง: no reminders or escalations
            .select_related("case", "division")
        )
        for clock in due:
            work = clock.work
            if clock.reminded_at is None:
                clock.reminded_at = now
                identity.audit(
                    None,
                    "case.deadline_overdue" if work else "case.ack_overdue",
                    clock.case,
                    actor_label="policy",
                    clock=clock.kind,
                    division=clock.division.code,
                    priority=clock.priority,
                )
                staff_email.plan_for_clock(clock, "overdue")
                counts["reminded"] += 1
            if clock.escalate_at <= now and clock.escalated_at is None:
                if problem is None:
                    problem = _system_problem(now)
                if problem:
                    if clock.escalation_held_at is None:
                        identity.audit(
                            None,
                            "case.escalation_held",
                            clock.case,
                            problem,
                            actor_label="policy",
                            clock=clock.kind,
                        )
                        counts["held"] += 1
                    clock.escalation_held_at, clock.hold_reason = now, problem[:200]
                else:
                    policy = current_policy()
                    clock.escalated_at = now
                    coordinator = policy.units["coordinator"] if policy else None
                    identity.audit(
                        None,
                        "case.deadline_escalated" if work else "case.ack_escalated",
                        clock.case,
                        actor_label="policy",
                        clock=clock.kind,
                        division=clock.division.code,
                        to=f"supervisors:{clock.division.code}" if work else coordinator,
                    )
                    staff_email.plan_for_clock(
                        clock,
                        "escalated",
                        f"supervisors:{clock.division.code}" if work else coordinator or "",
                    )
                    counts["escalated"] += 1
            clock.save()
    return counts


# --- Authority -------------------------------------------------------------------------------


def active_grants(user, activity, now=None):
    """Authority an admin granted, plus what membership brings (16 Oct 2569 Q4) while the
    person is still an active member and the division's work is not split."""
    now = now or timezone.now()
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return AuthorityGrant.objects.none()
    grants = AuthorityGrant.objects.filter(
        user=user,
        activity=activity,
        revoked_at__isnull=True,
        valid_from__lte=now,
    ).exclude(valid_until__lte=now)
    if identity.work_is_split():
        return grants.filter(member_of__isnull=True)
    memberships = RoleGrant.objects.filter(
        user=user, revoked_at__isnull=True, division__is_active=True
    ).values("division")
    return grants.filter(Q(member_of__isnull=True) | Q(member_of__in=memberships))


def has_authority(user, activity, division_id, now=None):
    return any(g.division_id in (None, division_id) for g in active_grants(user, activity, now))


def routing_scope(user, now=None):
    """Divisions whose cases this user may decide routes for: None = none, "all", or ids."""
    grants = list(active_grants(user, AuthorityGrant.Activity.ROUTING_DECISION, now))
    if not grants:
        return None
    if any(g.division_id is None for g in grants):
        return "all"
    return {g.division_id for g in grants}


def grant_authority(
    actor,
    user,
    activity,
    reason,
    approval_reference,
    division=None,
    valid_from=None,
    valid_until=None,
    category="",
    item=None,
    max_quantity=None,
):
    """An admin records authority the organisation approved; never to themselves (Q16).

    M07 activities may narrow to a category or an item; a quantity cap needs an item (Q2)."""
    identity.require_admin(actor)
    if actor.pk == user.pk:
        raise ValidationError("ให้อำนาจแก่ตัวเองไม่ได้ (Q16)")
    if activity not in AuthorityGrant.Activity.values:
        raise ValidationError("Unknown activity.")
    scoped = {
        AuthorityGrant.Activity.WITHDRAWAL_APPROVAL,
        AuthorityGrant.Activity.ALLOCATION,
        AuthorityGrant.Activity.CONDITION_CERTIFICATION,
    }
    if (category or item or max_quantity) and activity not in scoped:
        raise ValidationError("หมวด/รายการใช้กับอำนาจอนุมัติเบิก จัดสรร และรับรองสภาพเท่านั้น")
    if category and category not in EquipmentType.Category.values:
        raise ValidationError("Unknown category.")
    if item is not None and category and item.category != category:
        raise ValidationError("รายการของไม่อยู่ในหมวดที่เลือก")
    if max_quantity is not None:
        if activity != AuthorityGrant.Activity.WITHDRAWAL_APPROVAL or item is None:
            raise ValidationError("จำนวนสูงสุดใช้กับอำนาจอนุมัติเบิกที่ระบุรายการของ (Q2)")
        if max_quantity <= 0:
            raise ValidationError("จำนวนสูงสุดต้องมากกว่า 0")
    if not user.is_active or not identity.staff_scope(user).has_any_access:
        raise ValidationError("Grant authority to active staff only.")
    reason = identity._require_reason(reason)
    approval_reference = identity._require_reason(approval_reference)
    valid_from = valid_from or timezone.now()
    if valid_until is not None and valid_until <= valid_from:
        raise ValidationError("The end must be after the start.")
    grant = AuthorityGrant.objects.create(
        user=user,
        activity=activity,
        division=division,
        category=category or (item.category if item is not None else ""),
        item=item,
        max_quantity=max_quantity,
        valid_from=valid_from,
        valid_until=valid_until,
        reason=reason,
        approval_reference=approval_reference,
        granted_by=actor,
    )
    identity.audit(
        actor,
        "authority.granted",
        grant,
        reason,
        user=user.pk,
        activity=activity,
        division=division.code if division else None,
        category=grant.category,
        item=item.code if item else None,
        max_quantity=str(max_quantity) if max_quantity is not None else None,
        reference=approval_reference,
    )
    return grant


def revoke_authority(actor, grant, reason):
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    updated = AuthorityGrant.objects.filter(pk=grant.pk, revoked_at__isnull=True).update(
        revoked_at=timezone.now(), revoked_by=actor, revoke_reason=reason
    )
    if not updated:
        raise ValidationError("This grant is already revoked.")
    identity.audit(
        actor, "authority.revoked", grant, reason, user=grant.user_id, activity=grant.activity
    )


# --- AI triage requests ----------------------------------------------------------------------


def usage_text(text, current=None):
    """Words of an การใช้ไฟฟ้า matter in free text (Q14: such text never goes to the AI)."""
    current = current if current is not None else current_policy()
    if current is None or not text:
        return False
    terms = [e["term"] for e in current.doc.get("equipment_terms", []) if e["category"] == USAGE]
    return any(term.lower() in text.lower() for term in terms)


def private_topic(case):
    """Q14: การใช้ไฟฟ้า cases often hold a customer's details, so the AI never reads them."""
    if case.topic == USAGE:
        return True
    return usage_text("\n".join(case_text(case).values()))


def maybe_request_ai(case, now):
    """Ask the active triage version once per case text (Q18); never blocks triage."""
    if not settings.AI_ENABLED or case.status in workflow.TERMINAL or private_topic(case):
        return None
    version = AIConfigVersion.objects.filter(
        task=AIConfigVersion.Task.TRIAGE, status=AIConfigVersion.Status.ACTIVE
    ).first()
    if version is None:
        return None
    from operations import ai_triage

    digest = input_digest(case)
    run, created = AITriageRun.objects.get_or_create(
        case=case,
        input_digest=digest,
        config_version=version,
        defaults={
            "case_revision": case.revision,
            "prompt_version": ai_triage.PROMPT_VERSION,
            "schema_version": ai_triage.SCHEMA_VERSION,
        },
    )
    if created:
        OutboxEvent.objects.create(
            event_type=EVENT_TRIAGE_AI,
            subject_type="ai_triage_run",
            subject_id=str(run.pk),
            case=case,
            case_revision=case.revision,
            correlation_id=run.case_id,
            payload={"ai_triage_run_id": run.pk},
            occurred_at=now,
        )
    return run


# --- Display data for M10 (B.7) ----------------------------------------------------------------

SOURCE_LABELS = {
    "case": "ข้อมูลในเรื่อง",
    "serial": "เลขเครื่องตรงกับเรื่องเดิมของผู้แจ้ง",
    "code": "รหัสรายการในข้อความ",
    "related": "รายการในเรื่องที่อ้างถึง",
    "terms": "คำเรียกอุปกรณ์ในข้อความ (ยังไม่ยืนยัน)",
    "terms+ai": "คำเรียกอุปกรณ์และ AI ตรงกัน (ยังไม่ยืนยัน)",
    "keyword": "ธงจากคำในข้อความ (ยังไม่ยืนยัน)",
    "ai": "ข้อเสนอจาก AI (ยังไม่ยืนยัน)",
    "staff": "เจ้าหน้าที่ยืนยัน",
    "stock": "ยอดคลัง",
    "delivery": "การยืนยันรับของ",
    "return": "การรับของคืน",
}


def card(case):
    """The proposal card of B.7 as plain data; the web (M10) renders it."""
    state = (
        CaseTriage.objects.filter(case=case)
        .select_related(
            "confirmed_division",
            "current_recommendation__policy_version",
            "current_recommendation__proposed_owner",
        )
        .first()
    )
    recommendation = state.current_recommendation if state else None
    if recommendation is None:
        return None
    pending = (
        Handoff.objects.filter(case=case, status=Handoff.Status.PENDING)
        .select_related("to_division")
        .first()
    )
    review = RoutingReview.objects.filter(case=case, status=RoutingReview.Status.OPEN).first()
    clocks = AckClock.objects.filter(case=case, stopped_at__isnull=True).select_related("division")
    policy = recommendation.policy_version
    return {
        "case": case.number,
        "revision": case.revision,
        "stale": recommendation.case_revision != case.revision,
        "facts": {
            name: {**value, "source_label": SOURCE_LABELS.get(value.get("source"), "")}
            for name, value in recommendation.facts.items()
            if isinstance(value, dict)
        },
        "missing": recommendation.missing,
        "current_owner": case.owner_division.code,
        "proposed_owner": recommendation.proposed_owner.code,
        "support_units": recommendation.support_units,
        "child_work_owners": recommendation.child_work_owners,
        "rules": recommendation.rules,
        "priority": {
            "suggested": state.suggested_priority,
            "floor": state.priority_floor,
            "floor_basis": recommendation.floor_basis,
            "confirmed": state.confirmed_priority,
            "confirmed_by": state.confirmed_division.code if state.confirmed_division else None,
            "effective": state.effective_priority,
            "exception": state.floor_exception or None,
        },
        "next_action": recommendation.next_action,
        "notes": recommendation.notes,
        "policy": {
            "code": policy.code if policy else None,
            "synthetic": policy.is_synthetic if policy else None,
            "approved_by": policy.approved_by_name if policy else None,
        },
        "policy_change_pending": state.policy_change_pending,
        "pending_handoff": pending.to_division.code if pending else None,
        "routing_review": review.get_trigger_display() if review else None,
        "clocks": [
            {
                "kind": c.kind,
                "label": c.get_kind_display(),
                "division": c.division.code,
                "due_at": c.due_at.isoformat(),
                "overdue": c.reminded_at is not None,
                "escalated": c.escalated_at is not None,
                "paused": c.paused_at is not None,
            }
            for c in clocks
        ],
        "hazard_alert": state.priority_floor == "P0",
        "children": [
            {
                "child": w.seq,
                "division": w.division.code,
                "rule": w.rule,
                "task": w.task,
                "status": w.status,
                "status_label": w.get_status_display(),
                "lines": sorted(line.line_no for line in w.lines.all()),
                "result": w.result_summary,
            }
            for w in case.child_works.select_related("division").prefetch_related("lines")
        ],
        "related": case.related_case.number if case.related_case_id else None,
        "linked": list(
            Case.objects.filter(related_case=case)
            .order_by("number")
            .values_list("number", flat=True)
        ),
        "problem": _problem(case),
        "decision_made": Decision.objects.filter(
            case=case,
            action__in=["set_priority", "accept_handoff", "decide_routing", "complete_triage"],
        ).exists(),
        "fulfilment": _fulfilment(case),
    }


def _problem(case):
    if case.kind != K.PROBLEM:
        return None
    from operations import problems

    return problems.summary(case)


def hazard_guidance(case):
    """M09 Q10: the emergency guidance of the active policy when a hazard was reported (floor
    P0), shown at once without waiting for AI."""
    state = CaseTriage.objects.filter(case=case).first()
    current = current_policy()
    if state is None or state.priority_floor != "P0" or current is None:
        return ""
    return current.emergency_guidance


def update_due(case):
    """กำหนดอัปเดต (M09 Q14): the open resolve deadline of a problem, shown to the requester."""
    clock = AckClock.objects.filter(
        case=case, kind=AckClock.Kind.RESOLVE, stopped_at__isnull=True
    ).first()
    return clock.due_at if clock else None


def _fulfilment(case):
    if case.kind == K.WITHDRAWAL:
        from operations import fulfilment

        return fulfilment.summary(case)
    if case.kind == K.RETURN:
        from operations import returns

        return returns.summary(case)
    return None


def queue_key(case_triage, case):
    """Order within a queue (B.4): level, then the confirmed date needed, then arrival (age)."""
    needed = case.needed_by or date.max
    return (
        rank(case_triage.effective_priority or case_triage.suggested_priority),
        needed,
        case.submitted_at,
    )
