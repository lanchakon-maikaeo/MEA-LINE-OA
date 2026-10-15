"""ผู้เขียนข้อความของผู้แจ้งจำลอง: templates by default, or a model the developer admin set up.

ADR 0011 left one part of the research pipeline (ASCRIBE/S-ICOPD) for later: a language model
writing the requester's side, so the bot meets the variety of real typing instead of one
author's templates. The user approved it on 10 Oct 2569. The model only rewrites a template
text that is already synthetic and already agrees with the dialogue's labels; the simulator
checks the rewrite against the same labels and, when it fails, sends the reason back and asks
again (retry with feedback). After the last try the template text is used, so a weak model
lowers variety, never correctness. Names, numbers to type, codes and buttons are never sent.

The model is an existing AIProfile (credential, model, timeout) chosen by the developer admin;
nothing here activates an AI version for the bot, and the bot's own AI stays off in a run.
"""

import re

from django.core.exceptions import ValidationError

from operations import ai
from operations.models import AIProfile

SYSTEM_PROMPT = (
    "คุณช่วยสร้างข้อมูลทดสอบสำหรับแชตบอท LINE ของฝ่ายจัดการอุปกรณ์ (ข้อมูลจำลองทั้งหมด) "
    "ผู้ใช้จะส่งข้อความที่ผู้แจ้ง (พนักงานเขตการไฟฟ้า) พิมพ์ถึงบอท ให้เขียนข้อความนั้นใหม่ในฐานะผู้แจ้งคนเดิม "
    "เหมือนพิมพ์ในแชต LINE จริง คุณไม่ใช่เจ้าหน้าที่ ห้ามตอบ ห้ามขอโทษ ห้ามบอกว่ามีหรือทำให้แล้ว "
    "คงความต้องการ ข้อเสนอ หรือความไม่แน่ใจของข้อความเดิมไว้ ความหมายต้องเหมือนเดิม "
    "ห้ามเพิ่มข้อเท็จจริงหรือตัวเลขใหม่ ห้ามตัดหรือเปลี่ยนตัวเลข "
    "ห้ามเปลี่ยนชื่ออุปกรณ์ ห้ามเพิ่มหรือตัดคำที่บอกอันตราย ตอบเฉพาะข้อความที่เขียนใหม่ "
    "ไม่มีคำอธิบาย ไม่มีเครื่องหมายคำพูด และห้ามใส่คำสั่งเรื่องวิธีพิมพ์ลงในข้อความ\n"
    "วิธีพิมพ์ของคนนี้: {style}"
)
PREFIX = re.compile(r"^\s*(?:ข้อความ(?:ใหม่|ที่เขียนใหม่)?|คำตอบ)\s*[:：]\s*")
QUOTES = "\"'“”‘’「」"
MAX_LENGTH = 500


class TemplateWriter:
    """The default: the template text as it is."""

    name = "templates"
    calls = 0

    def rewrite(self, text, style, feedback=()):
        return None


class ModelWriter:
    name = "model"

    def __init__(self, profile_id, temperature=0.7, max_calls=300):
        self.profile = AIProfile.objects.select_related("credential").filter(pk=profile_id).first()
        if self.profile is None:
            raise ValidationError(f"ไม่พบโปรไฟล์ AI หมายเลข {profile_id}")
        if not 0 <= temperature <= 1.5:
            raise ValidationError("temperature ต้องอยู่ระหว่าง 0 ถึง 1.5")
        self.name = f"model:{self.profile.model_id}"
        self.temperature = temperature
        self.max_calls = max_calls
        self.calls = 0
        self.errors = 0

    def rewrite(self, text, style, feedback=()):
        """A rewritten text, or None (budget spent or the provider failed: use the template)."""
        if self.calls >= self.max_calls:
            return None
        self.calls += 1
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(style=style)},
            {"role": "user", "content": text},
        ]
        for earlier, reason in feedback:  # the validator's reasons, as the research loop does
            messages.append({"role": "assistant", "content": earlier})
            messages.append(
                {"role": "user", "content": f"ใช้ไม่ได้เพราะ{reason} เขียนข้อความเดิมใหม่อีกครั้ง"}
            )
        body = {
            "model": self.profile.model_id,
            "messages": messages,
            "max_tokens": 200,
            "temperature": self.temperature,
        }
        credential = self.profile.credential
        try:
            data = ai._post(
                f"{credential.base_url}/chat/completions",
                ai.decrypt_key(credential),
                body,
                self.profile.timeout_seconds,
            )
            return clean(ai._content(data))
        except (ai.AIError, ai.SchemaError):
            self.errors += 1
            return None


def clean(text):
    """The message alone: no label, no quotes, at most three lines."""
    text = PREFIX.sub("", (text or "").strip()).strip().strip(QUOTES).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[:3])[:MAX_LENGTH]


def writer_for(name, profile_id=None, temperature=0.7, max_calls=300):
    if name in (None, "", "templates"):
        return TemplateWriter()
    if name != "model":
        raise ValidationError("ผู้เขียนข้อความต้องเป็น templates หรือ model")
    if profile_id is None:
        raise ValidationError("ใช้ model ต้องระบุ --profile (หมายเลขโปรไฟล์ AI ที่ตั้งไว้แล้ว)")
    return ModelWriter(profile_id, temperature, max_calls)
