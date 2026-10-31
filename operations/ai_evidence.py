"""Evaluation evidence carried to a machine that cannot evaluate (user, 24 Oct 2569, option ก).

Production refuses evaluations (they replay drafts in a demo database), so the machine that ran
the gate on the same code signs the report with an Ed25519 key only it holds. Production keeps
the public keys (AI_EVIDENCE_PUBLIC_KEYS) and accepts the report when the signature, the code
running here and the configuration all match. The configuration is compared by content, since
numbers, row ids and key rotation times differ between databases; production still tests the
connection with its own key before a switch. Extraction versions only for now (Q7).
"""

import base64
import hashlib
import json
import re
import subprocess
from decimal import Decimal
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from config.environment import require_demo_database
from operations import ai, ai_eval, identity
from operations.models import AIConfigVersion, AICredential, AIExample, AIProfile

KIND = "faoj-ai-evidence"
SCHEMA = 1
MAX_BYTES = 5 * 1024 * 1024
TASKS = (AIConfigVersion.Task.EXTRACT,)


# --- What must match ---------------------------------------------------------------------------


def _profile(profile):
    return {
        "model_id": profile.model_id,
        "timeout_seconds": profile.timeout_seconds,
        "max_tokens": profile.max_tokens,
        "temperature": str(profile.temperature),
        "use_json_schema": profile.use_json_schema,
        "base_url": profile.credential.base_url,
    }


def portable_config(version):
    """The configuration by content: what the evaluation measured, the same on every machine."""
    return {
        "task": version.task,
        "pipeline": version.pipeline,
        "few_shot": version.few_shot,
        "typed_fields": ai.reads_typed(version),
        **({"menu_fields": True} if ai.reads_menu(version) else {}),
        **({"slip_facts": True} if ai.reads_slips(version) else {}),
        "runtime": [settings.AI_TOTAL_BUDGET_SECONDS, settings.AI_HEDGE_AFTER_SECONDS],
        "chain": [_profile(p) for p in version.chain()],
        "screen": _profile(version.screen_profile) if version.screen_profile else None,
    }


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def portable_digest(version):
    return _digest(portable_config(version))


def examples_summary():
    """The few-shot examples by content. They differ on production once real ones are reviewed
    there; the user chose (Q5) to import anyway and warn."""
    rows = sorted(
        AIExample.objects.values("text", "draft", "requests", "is_active"),
        key=lambda row: row["text"],
    )
    return {"digest": _digest(rows), "count": sum(1 for row in rows if row["is_active"])}


# --- Signing (the evaluating machine) ----------------------------------------------------------


def _canonical(bundle):
    return json.dumps(bundle, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def public_key_text(public_key):
    return base64.b64encode(
        public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()


def key_id(public_text):
    return hashlib.sha256(public_text.encode()).hexdigest()[:16]


def new_private_key():
    """A new signing key as PEM, and its public half as it goes into AI_EVIDENCE_PUBLIC_KEYS."""
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem, public_key_text(private.public_key())


def _commit():
    """Best effort, for people reading the file: the digest is what is checked."""
    try:
        run = {"cwd": settings.BASE_DIR, "capture_output": True, "text": True, "timeout": 10}
        head = subprocess.run(["git", "rev-parse", "HEAD"], **run).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "operations", "config", "uv.lock"], **run
        ).stdout.strip()
        return f"{head}{'-dirty' if dirty else ''}" if head else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def export(version, private_pem):
    """The signed evidence of this version's current passing evaluation."""
    require_demo_database(settings.APP_ENV, settings.DATABASES["default"])
    if version.task not in TASKS:
        raise ValidationError("Only extraction evidence can be carried for now.")
    report = version.eval_result or {}
    current = version.eval_passed and ai_eval.gate_passed(report, version)
    if report.get("imported") or not current:
        raise ValidationError(
            f"v{version.number} has no passing evaluation of this code; evaluate it again first."
        )
    private = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValidationError("The signing key is not an Ed25519 key.")
    public = public_key_text(private.public_key())
    bundle = {
        "kind": KIND,
        "schema": SCHEMA,
        "task": version.task,
        "source_version": version.number,
        "source_digest": ai_eval.source_digest(),
        "portable_digest": portable_digest(version),
        "config": portable_config(version),
        "examples": examples_summary(),
        "commit": _commit(),
        "signed_at": timezone.now().isoformat(),
        "report": report,
    }
    signature = base64.b64encode(private.sign(_canonical(bundle))).decode()
    return {"bundle": bundle, "key_id": key_id(public), "signature": signature}


# --- Checking (the machine that imports) -------------------------------------------------------


def _public_keys():
    return {key_id(text): text for text in settings.AI_EVIDENCE_PUBLIC_KEYS}


def _signed(bundle, signature, signer):
    text = _public_keys().get(signer)
    if text is None:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(text)).verify(
            base64.b64decode(signature), _canonical(bundle)
        )
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def imported_passes(report, version):
    """Imported evidence still counts: signed by a key kept here, of the code running here and of
    this version's configuration, and its measurements pass the gate."""
    try:
        meta = report["imported"]
        # The report the gate reads is the one that was signed: any edit breaks the signature.
        bundle = {**meta["bundle"], "report": {k: v for k, v in report.items() if k != "imported"}}
        return (
            bundle["kind"] == KIND
            and bundle["schema"] == SCHEMA
            and bundle["task"] == version.task
            and bundle["task"] in TASKS
            and _signed(bundle, meta["signature"], meta["key_id"])
            and bundle["source_digest"] == ai_eval.source_digest()
            and bundle["portable_digest"] == portable_digest(version)
            and ai_eval.evidence_passes(
                bundle["report"], version, number=bundle["source_version"], config_matches=True
            )
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _read(raw):
    if len(raw) > MAX_BYTES:
        raise ValidationError("ไฟล์ใหญ่เกินไปสำหรับผลประเมิน")
    try:
        data = json.loads(raw)
        bundle, signature, signer = data["bundle"], data["signature"], data["key_id"]
        if bundle["kind"] != KIND or not isinstance(bundle["report"], dict):
            raise ValueError
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationError("ไฟล์นี้ไม่ใช่ผลประเมินที่ส่งออกจากเครื่องที่ประเมิน") from exc
    if bundle["schema"] != SCHEMA:
        raise ValidationError("ไฟล์ผลประเมินเป็นรูปแบบที่ระบบรุ่นนี้ไม่รู้จัก")
    if bundle["task"] not in TASKS:
        raise ValidationError("ตอนนี้นำเข้าผลประเมินได้เฉพาะงานอ่านข้อความ")
    if not settings.AI_EVIDENCE_PUBLIC_KEYS:
        raise ValidationError("เครื่องนี้ยังไม่ได้ตั้งกุญแจตรวจลายเซ็นผลประเมิน")
    if not _signed(bundle, signature, signer):
        raise ValidationError("ลายเซ็นของไฟล์ไม่ถูกต้อง หรือเซ็นด้วยกุญแจที่เครื่องนี้ไม่รู้จัก")
    if bundle["source_digest"] != ai_eval.source_digest():
        raise ValidationError(
            "ผลประเมินนี้ทำกับโค้ดคนละรุ่นกับที่เครื่องนี้ใช้อยู่ ประเมินใหม่ที่ commit ของรุ่นที่ติดตั้งแล้วส่งออกอีกครั้ง"
        )
    return bundle, signature, signer


def _credential(base_url):
    credential = AICredential.objects.filter(base_url=base_url).order_by("pk").first()
    if credential is None:
        raise ValidationError(f"เครื่องนี้ยังไม่มีคีย์ API ของ {base_url} เพิ่มคีย์ก่อนนำเข้า")
    return credential


def _local_profile(actor, entry):
    credential = _credential(entry["base_url"])
    found = (
        AIProfile.objects.filter(
            credential=credential,
            model_id=entry["model_id"],
            timeout_seconds=entry["timeout_seconds"],
            max_tokens=entry["max_tokens"],
            temperature=Decimal(entry["temperature"]),
            use_json_schema=entry["use_json_schema"],
        )
        .order_by("pk")
        .first()
    )
    return found or ai.create_profile(
        actor,
        credential,
        entry["model_id"],
        entry["timeout_seconds"],
        entry["max_tokens"],
        entry["temperature"],
        entry["use_json_schema"],
    )


def _local_version(actor, bundle):
    """This machine's version with the evaluated configuration: an existing one (not rejected),
    else a new candidate numbered here that says where it came from (Q4)."""
    wanted = bundle["portable_digest"]
    for version in (
        AIConfigVersion.objects.filter(task=bundle["task"])
        .exclude(status=AIConfigVersion.Status.REJECTED)
        .order_by("-number")
    ):
        if portable_digest(version) == wanted:
            return version
    config = bundle["config"]
    version = ai.create_version(
        actor,
        [_local_profile(actor, entry) for entry in config["chain"]],
        f"นำเข้าผลประเมินจาก v{bundle['source_version']} ของเครื่องที่ประเมิน",
        config["few_shot"],
        config["pipeline"],
        _local_profile(actor, config["screen"]) if config["screen"] else None,
        config["task"],
        config["typed_fields"],
        config.get("menu_fields", False),
        config.get("slip_facts", False),
    )
    if portable_digest(version) != wanted:
        raise ValidationError("สร้างรุ่นบนเครื่องนี้ให้ตรงกับที่ประเมินไม่ได้ (ค่าระยะเวลาของระบบต่างกัน)")
    return version


def examples_warning(version):
    """Why the examples this version retrieves here are not the evaluated ones, or ""."""
    meta = (version.eval_result or {}).get("imported") or {}
    if not meta or meta.get("examples_match", True) or not version.few_shot:
        return ""
    evaluated = meta.get("bundle", {}).get("examples", {}).get("count", 0)
    return (
        f"ตัวอย่างที่ AI ค้นมาใส่ prompt บนเครื่องนี้ต่างจากตอนประเมิน "
        f"(ตอนประเมินมี {evaluated} ตัวอย่าง เครื่องนี้มี {meta.get('examples_count_here', 0)}) "
        "ผลจริงอาจไม่เท่าผลประเมิน"
    )


@transaction.atomic
def import_evidence(actor, raw, reason):
    """Attach signed evidence to the matching version here (made if missing). The version still
    needs a connection test here before anyone switches to it."""
    identity.require_ai_editor(actor)
    reason = identity._require_reason(reason)
    bundle, signature, signer = _read(raw)
    version = _local_version(actor, bundle)
    version = AIConfigVersion.objects.select_for_update().get(pk=version.pk)
    here = examples_summary()
    meta = {
        "bundle": {k: v for k, v in bundle.items() if k != "report"},
        "signature": signature,
        "key_id": signer,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "examples_match": here["digest"] == bundle["examples"]["digest"],
        "examples_count_here": here["count"],
        "at": timezone.now().isoformat(),
        "by": actor.username,
    }
    stored = {**bundle["report"], "imported": meta}
    if not imported_passes(stored, version):
        raise ValidationError("ผลประเมินในไฟล์ไม่ผ่านเกณฑ์ของระบบรุ่นนี้ จึงใช้เปิดรุ่นนี้ไม่ได้")
    version.eval_passed = True
    version.eval_result = stored
    version.save(update_fields=["eval_passed", "eval_result"])
    identity.audit(
        actor,
        "ai.evidence_imported",
        version,
        reason,
        number=version.number,
        source_version=bundle["source_version"],
        commit=bundle.get("commit", ""),
        key_id=signer,
        file_sha256=meta["file_sha256"],
        examples_match=meta["examples_match"],
    )
    return version, examples_warning(version)


# --- Releases (ADR 0038): every task's evidence in one file, kept under the same label ----------

RELEASE_SCHEMA = 2
RELEASE_LABEL = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.\d{1,3}$")
# What an evaluation read from the data of the machine it ran on; the machine that imports it
# must hold the same, or the import is refused (owner, 31 ต.ค. 2569).
DATA_NAMES = {
    "policy": "กติกาคัดกรองที่ใช้อยู่",
    "bank": "คลังคำตอบ",
    "duties": "หน้าที่ของกอง",
    "examples": "ตัวอย่างที่ระบบค้นมาให้ AI",
}


def data_here(task, few_shot=0):
    from operations import answers, duties, policy

    if task == AIConfigVersion.Task.TRIAGE:
        active = policy.active_policy()
        return {"policy": {"code": active.code, "digest": active.digest} if active else None}
    if task == AIConfigVersion.Task.ANSWER:
        return {"bank": answers.bank_digest()}
    if task == AIConfigVersion.Task.ROUTING:
        return {"duties": duties.digest()}
    if task == AIConfigVersion.Task.EXTRACT and few_shot:
        return {"examples": examples_summary()["digest"]}
    return {}


def export_release(label, private_pem):
    """The signed evidence of a release whose every task passed or had nothing to evaluate."""
    from operations import ai_release

    require_demo_database(settings.APP_ENV, settings.DATABASES["default"])
    found = ai_release.members(label)
    if set(found) != set(ai_release.TASKS):
        raise ValidationError(f"Release {label} does not have every task.")
    for task, version in found.items():
        if (version.eval_result or {}).get("provenance"):
            raise ValidationError(f"{label} was imported here; export it where it was evaluated.")
        if not ai_release.not_ready(version) and ai_release.eval_blocker(version):
            raise ValidationError(f"{label}: {task} has no passing evaluation of this code.")
    private = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValidationError("The signing key is not an Ed25519 key.")
    public = public_key_text(private.public_key())
    bundle = {
        "kind": KIND,
        "schema": RELEASE_SCHEMA,
        "release": label,
        "source_digest": ai_eval.source_digest(),
        "commit": _commit(),
        "signed_at": timezone.now().isoformat(),
        "members": [
            {
                "task": task,
                "config": portable_config(found[task]),
                "data": data_here(task, found[task].few_shot),
                "report": found[task].eval_result or {},
            }
            for task in ai_release.TASKS
        ],
    }
    signature = base64.b64encode(private.sign(_canonical(bundle))).decode()
    return {"bundle": bundle, "key_id": key_id(public), "signature": signature}


def is_release(raw):
    """Whether a file is a release's evidence (ADR 0038) rather than one earlier version's."""
    try:
        return json.loads(raw)["bundle"]["schema"] == RELEASE_SCHEMA
    except (ValueError, KeyError, TypeError):
        return False


def _read_release(raw):
    if len(raw) > MAX_BYTES:
        raise ValidationError("ไฟล์ใหญ่เกินไปสำหรับผลประเมิน")
    try:
        data = json.loads(raw)
        bundle, signature, signer = data["bundle"], data["signature"], data["key_id"]
        if bundle["kind"] != KIND or bundle["schema"] != RELEASE_SCHEMA:
            raise ValueError
        if not RELEASE_LABEL.match(bundle["release"]) or not isinstance(bundle["members"], list):
            raise ValueError
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationError("ไฟล์นี้ไม่ใช่ผลประเมินรุ่นการตั้งค่า AI จากเครื่องที่ประเมิน") from exc
    if not settings.AI_EVIDENCE_PUBLIC_KEYS:
        raise ValidationError("เครื่องนี้ยังไม่ได้ตั้งกุญแจตรวจลายเซ็นผลประเมิน")
    if not _signed(bundle, signature, signer):
        raise ValidationError("ลายเซ็นของไฟล์ไม่ถูกต้อง หรือเซ็นด้วยกุญแจที่เครื่องนี้ไม่รู้จัก")
    if bundle["source_digest"] != ai_eval.source_digest():
        raise ValidationError("ผลประเมินนี้ทำกับโปรแกรมคนละรุ่นกับที่เครื่องนี้ใช้อยู่")
    return bundle, signature, signer


def _here(report, version):
    """The signed report bound to this machine's version: its number and configuration digest
    are local, everything measured stays as signed."""
    local = {"version": version.number, "config_digest": ai_eval.config_digest(version)}
    report = {**report, **local}
    if isinstance(report.get("free_chat"), dict):
        report["free_chat"] = {**report["free_chat"], **local}
    return report


@transaction.atomic
def import_release(actor, raw, reason):
    """A release evaluated on the evaluating machine, under its own label (ADR 0038 Q17). It
    still needs a connection test here with this machine's keys before it serves."""
    from operations import ai_release

    identity.require_ai_editor(actor)
    reason = identity._require_reason(reason)
    bundle, signature, signer = _read_release(raw)
    label = bundle["release"]
    if AIConfigVersion.objects.filter(release=label).exists():
        raise ValidationError(f"นำเข้ารุ่น {label} แล้ว")
    entries = {m["task"]: m for m in bundle["members"]}
    if set(entries) != set(ai_release.TASKS):
        raise ValidationError("ไฟล์นี้มีผลประเมินไม่ครบทุกงาน")
    wanted = {
        entry["base_url"]
        for m in entries.values()
        for entry in [*m["config"]["chain"], m["config"]["screen"]]
        if entry
    }
    ready = set(
        AICredential.objects.filter(base_url__in=wanted)
        .exclude(encrypted_key="")
        .values_list("base_url", flat=True)
    )
    hosts = {urlsplit(url).hostname for url in wanted - ready}
    if hosts:
        raise ValidationError(f"เพิ่มผู้ให้บริการ {' และ '.join(sorted(hosts))} พร้อมคีย์ก่อนนำเข้า")
    differ = []
    for task, member in entries.items():
        if member["report"].get("not_ready"):
            continue
        here = data_here(task, member["config"]["few_shot"])
        differ += [DATA_NAMES[k] for k, v in member["data"].items() if here.get(k) != v]
    if differ:
        raise ValidationError(
            f"ข้อมูลบนเครื่องนี้ต่างจากตอนประเมิน: {' '.join(dict.fromkeys(differ))} "
            "ทำข้อมูลบนเครื่องที่ประเมินให้ตรงกันแล้วประเมินใหม่"
        )
    provenance = {
        "release": label,
        "commit": bundle.get("commit", ""),
        "key_id": signer,
        "signed_at": bundle.get("signed_at", ""),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "at": timezone.now().isoformat(),
        "by": actor.username,
    }
    for task in ai_release.TASKS:
        config = entries[task]["config"]
        version = ai.create_version(
            actor,
            [_local_profile(actor, entry) for entry in config["chain"]],
            f"รุ่น {label}",
            config["few_shot"],
            config["pipeline"],
            _local_profile(actor, config["screen"]) if config["screen"] else None,
            task,
            config["typed_fields"],
            config.get("menu_fields", False),
            config.get("slip_facts", False),
        )
        if portable_digest(version) != _digest(config):
            raise ValidationError("สร้างรุ่นบนเครื่องนี้ให้ตรงกับที่ประเมินไม่ได้ (ค่าระยะเวลาของระบบต่างกัน)")
        AIConfigVersion.objects.filter(pk=version.pk).update(release=label)
        version.refresh_from_db()
        report = entries[task]["report"]
        if report.get("not_ready"):
            AIConfigVersion.objects.filter(pk=version.pk).update(
                eval_result={**report, "provenance": provenance}
            )
            continue
        version = ai.record_eval(version, {**_here(report, version), "provenance": provenance})
        if ai_release.eval_blocker(version):
            raise ValidationError(f"ผลประเมินของงาน{AIConfigVersion.Task(task).label}ใช้กับเครื่องนี้ไม่ได้")
    identity.audit(
        actor,
        "ai.release_imported",
        ai_release.members(label)[AIConfigVersion.Task.EXTRACT],
        reason,
        **provenance,
    )
    return label
