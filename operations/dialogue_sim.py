"""บทสนทนาจำลอง: synthetic LINE dialogues against the real bot (PROJECT.md research proposal).

The pipeline follows ASCRIBE/S-ICOPD and GraphTOD (intent graph → sampled path → two agents
talk → rule validation → retry with feedback), with one change: only the requester is
simulated. The other agent is the bot itself, reached through the same ingestion
(`line.store_events`) and turn handler (`conversation.handle_inbox`) as real LINE events, so a
dialogue that passes here is one the real bot holds.

- The graph (fixtures/dialogue_graph_v1.json) lists, per bot step, what a person may do next and
  how often, plus personas, scenarios and Thai text banks. A new bot step or a new kind of user
  is a change to that file (ADR 0009: design for change).
- A random walk over the graph, held to the scenario's goal, is the path. Templates turn each
  move into Thai text, a button press, a location or a photo. No model is called.
- Retry with feedback, at two levels. Before sending, each text is checked against the
  dialogue's labels under the active policy (a meter story must not name a transformer, a
  hazard it does not have, or การใช้ไฟฟ้า words) and drawn again when it fails. After sending,
  when the bot does not move on, the person repairs the way a person would (press the button
  instead of typing, use digits), and the repair is counted as friction. A failure on the
  bot's side is a finding and is never retried away.
- Rules check every reply (LINE limits, buttons where a choice is asked, the humanizer style)
  and the outcome (the case reached the division the policy names, hazards got P0 and the
  guidance, การใช้ไฟฟ้า text never reached the AI, staff alerts carry no requester text, the
  anonymous reporter stays anonymous).

Everything runs inside one transaction that is rolled back, each dialogue in its own savepoint,
with the AI and outbound sending switched off: nothing is kept and nothing reaches LINE. Rows a
dialogue locks (such as the case number counter) are released when its savepoint rolls back.
"""

import json
import random
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone

from operations import (
    ai_chat,
    answers,
    conversation,
    dialogue_writer,
    employees,
    identity,
    line,
    line_menu_catalog,
    options,
    policy,
    services,
    staff_email,
    staff_line,
)
from operations.contracts import Actor, CommandFailed
from operations.models import (
    Case,
    CaseTriage,
    ChannelIdentity,
    Conversation,
    ConversationDraft,
    ConversationMessage,
    Decision,
    District,
    Division,
    Employee,
    EquipmentType,
    NotificationDelivery,
    PostbackToken,
    RegistrationRequest,
    Requester,
    StaffProfile,
    WebhookInbox,
)

GRAPH = Path(settings.BASE_DIR) / "operations" / "fixtures" / "dialogue_graph_v1.json"
D = ConversationDraft.Status
K = Case.Kind
ACTORS = {"registered", "anonymous", "newcomer", "staff"}
TERMINAL = {"submitted", "discarded", "reg_pending", "staff_done", "info_sent"}
# Steps where the bot asks for a choice: its reply must carry buttons.
CHOICE_STEPS = {
    "kind",
    "topic",
    "category",
    "item",
    "more",
    "summary",
    "location",
    "reg_district",
    "reg_kind",
}
EQUIPMENT = ("meter", "transformer", "other")
STORY_TOPICS = (Case.Topic.USAGE, Case.Topic.SUGGESTION)
TO_THAI = str.maketrans("0123456789", "๐๑๒๓๔๕๖๗๘๙")
TO_ARABIC = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
# A requester types Thai, maybe English, digits and emoji; other scripts are model noise.
FOREIGN = re.compile(
    r"[^\u0e00-\u0e7f\x00-\x7f\u2018-\u201d\u2026\u2600-\u27bf\ufe0f\U0001f300-\U0001faff]"
)
NGRAM = 10  # a run of this many characters of the requester's text counts as a leak
CHECKS = {
    "goal": "ไปถึงปลายทางของสถานการณ์",
    "reply": "ทุกข้อความได้คำตอบ และคำตอบอยู่ในขอบเขตของ LINE",
    "buttons": "ขั้นที่ให้เลือกมีปุ่มให้กด",
    "style": "ข้อความบอทไม่มีลักษณะที่ห้ามตามแนว humanizer",
    "summary": "หน้าสรุปแสดงทุกคำตอบตามลำดับที่ถาม",
    "case": "ข้อมูลในเรื่องตรงกับที่ผู้แจ้งตอบ",
    "owner": "เรื่องไปถึงกองที่กติกากำหนด",
    "split": "แยกเรื่องตามกองครบ และบอกเลขทุกเรื่อง",
    "hazard": "แจ้งอันตรายได้ระดับ P0 และคำแนะนำเร่งด่วนทันที",
    "hazard_overflag": "ไม่ได้แจ้งอันตรายแต่ได้ P0 (ยอมรับได้ตามกติกา)",
    "ai_private": "ข้อความเรื่องการใช้ไฟฟ้าไม่ถูกส่งให้ AI",
    "staff_private": "ข้อความเด้งถึงเจ้าหน้าที่ไม่มีชื่อหรือข้อความของผู้แจ้ง",
    "anonymous": "ผู้แจ้งไม่ระบุตัวตนไม่มีชื่อ และตำแหน่งตรงกับที่ส่ง",
    "stale_button": "กดปุ่มเก่าแล้วบอทไม่ทำตาม และถามคำถามล่าสุดอีกครั้ง",
    "isolation": "ผู้แจ้งไม่เห็นเรื่องของคนอื่น",
    "idempotent": "กดยืนยันซ้ำได้เรื่องเดียว",
    "registration": "การลงทะเบียนได้ผลตามวิธีที่เลือก",
    "staff": "เจ้าหน้าที่เชื่อม LINE ดูงาน และทำขั้นที่อนุญาตได้",
    "answer": "ถามคำถามระหว่างทางแล้วได้คำตอบจากคลังคำตอบ และบอทถามคำถามเดิมต่อ",
    "info": "ระหว่างเจ้าหน้าที่รอข้อมูลเพิ่ม ข้อความถึงเรื่องเฉพาะเมื่อเป็นข้อมูล คำทักทายและคำถามไม่ถูกส่งเอง",
}
WARNINGS = {"hazard_overflag"}


class Finding(Exception):
    """The bot failed a rule (name, detail); the dialogue stops here."""


class Skipped(Exception):
    """The simulator cannot run this dialogue (e.g. no staff account in a division)."""


# --- The graph -----------------------------------------------------------------------------------


def load_graph(path=None):
    """The graph document, validated; a broken file is refused with every problem listed."""
    path = Path(path) if path else GRAPH
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValidationError(f"อ่านกราฟขั้นสนทนาไม่ได้: {exc}") from exc
    validate_graph(doc)
    return doc


def validate_graph(doc):
    problems = []
    if doc.get("is_synthetic") is not True:
        problems.append("กราฟต้องระบุว่าเป็นข้อมูลจำลอง (is_synthetic: true)")
    for key in ("max_turns", "max_repairs", "max_redraws", "detour_rate"):
        if not isinstance(doc.get("limits", {}).get(key), int | float):
            problems.append(f"limits ต้องมี {key}")
    for step, moves in doc.get("steps", {}).items():
        for move in moves:
            if not (isinstance(move, list) and len(move) == 2 and move[1] >= 0):
                problems.append(f"ขั้น {step}: ต้องเป็น [ทางเลือก, น้ำหนัก]")
            elif move[0] not in INTENTS:
                problems.append(f"ขั้น {step}: ไม่รู้จักทางเลือก {move[0]}")
    for name, detour in doc.get("detours", {}).items():
        if name not in DETOURS:
            problems.append(f"ไม่รู้จักทางแยก {name}")
        if detour.get("weight", 0) < 0 or not detour.get("steps"):
            problems.append(f"ทางแยก {name}: ต้องมีน้ำหนักและขั้นที่ใช้")
    if not doc.get("personas"):
        problems.append("ต้องมีผู้แจ้งจำลองอย่างน้อยหนึ่งแบบ")
    for category, count in doc.get("catalogue", {}).get("at_least", {}).items():
        if category not in EQUIPMENT or not isinstance(count, int) or not 0 <= count <= 50:
            problems.append(f"catalogue: {category} ต้องเป็นหมวดของและจำนวน 0-50")
    choices = {k: {v for v, _, _ in o.choices} for k, o in options.OPTIONS.items()}
    for scenario in doc.get("scenarios", []):
        sid = scenario.get("id", "?")
        if scenario.get("actor") not in ACTORS:
            problems.append(f"สถานการณ์ {sid}: ผู้แจ้งต้องเป็น {', '.join(sorted(ACTORS))}")
        if scenario.get("kind") not in (None, *K.values):
            problems.append(f"สถานการณ์ {sid}: ไม่รู้จักประเภท {scenario.get('kind')}")
        for topic in scenario.get("topics", []):
            if topic not in (*Case.Topic.values, "none"):
                problems.append(f"สถานการณ์ {sid}: ไม่รู้จักหัวข้อ {topic}")
        for key, value in scenario.get("options", {}).items():
            if value not in choices.get(key, ()):
                problems.append(f"สถานการณ์ {sid}: ตัวเลือกระบบ {key}={value} ใช้ไม่ได้")
    texts = doc.get("texts", {})
    for name in ("description", "hazard", "polite", "offtopic", "purpose", "name", "places"):
        if name not in texts:
            problems.append(f"texts ต้องมี {name}")
    if problems:
        raise ValidationError(problems)


def graph_step(step):
    return "field" if step.startswith("field:") else step


# --- What the policy says (the oracle) ---------------------------------------------------------


def expected_owner(doc, kind, category, item=None):
    """The division the policy's responsibility table names, read straight from the document
    (not through the routing code it checks); unclear → the intake desk or the coordinator."""
    units = doc.get("units", {})
    if units.get("intake"):
        return units["intake"]
    owners = {
        e["owner"]
        for e in doc.get("responsibility", [])
        if kind in e["kinds"]
        and ((item and item in e.get("items", [])) or e.get("category") == category)
    }
    return owners.pop() if len(owners) == 1 else units.get("coordinator")


def _words_in(doc, text):
    lowered = text.lower()
    return {e["term"] for e in doc.get("equipment_terms", []) if e["term"].lower() in lowered}


def named_categories(doc, text):
    lowered = text.lower()
    return {e["category"] for e in doc.get("equipment_terms", []) if e["term"].lower() in lowered}


def names_hazard(doc, text):
    return any(term in text for term in doc.get("hazard_terms", []))


def leak(text, sources):
    """A run of NGRAM characters of any source found in `text` (short sources whole), or ""."""
    for source in sources:
        compact = " ".join(str(source).split())
        if len(compact) < NGRAM:
            if len(compact) >= 4 and compact in text:
                return compact
            continue
        for start in range(len(compact) - NGRAM + 1):
            piece = compact[start : start + NGRAM]
            if piece in text:
                return piece
    return ""


NUMBER_WORDS = {
    "หนึ่ง": "1",
    "สอง": "2",
    "สาม": "3",
    "สี่": "4",
    "ห้า": "5",
    "หก": "6",
    "เจ็ด": "7",
    "แปด": "8",
    "เก้า": "9",
    "สิบ": "10",
}


def _pairs(text):
    compact = "".join(ch for ch in text if ch.isalnum() or "\u0e00" <= ch <= "\u0e7f")
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def overlap(source, written):
    """How much of the source's wording a rewrite keeps (Thai has no spaces between words,
    so character pairs stand in for words)."""
    pairs = _pairs(source)
    return len(pairs & _pairs(written)) / len(pairs) if pairs else 1.0


def numbers_in(text):
    """(digits in the text, digits a rewrite may use for numbers written as words)."""
    digits = set(re.findall(r"\d+", text.translate(TO_ARABIC)))
    return digits, {value for word, value in NUMBER_WORDS.items() if word in text}


def weighted_order(rng, rows):
    """Names in a random order where heavier rows tend to come first (Efraimidis-Spirakis), so
    only the first move that applies is realised and nothing is built for the others."""
    keyed = [(rng.random() ** (1 / weight), name) for name, weight in rows if weight > 0]
    return [name for _, name in sorted(keyed, reverse=True)]


# --- Records -------------------------------------------------------------------------------------


@dataclass
class Move:
    intent: str
    event: dict
    shown: str
    advance: bool = True  # expected to move the dialogue on
    repair: str = ""  # what a person tries next when the bot does not move on


@dataclass
class Goal:
    scenario: dict
    actor: str
    kind: str = ""
    topic: str = ""  # the truth: meter / transformer / other / usage / suggestion / none
    choose: str = "topic"
    lines: list = field(default_factory=list)  # [{"item", "quantity", "serials"}]
    fields: dict = field(default_factory=dict)  # name → planned text, or None to skip
    hazard: bool = False
    negated_hazard: bool = False
    severe: bool | None = None
    finish: str = "confirm"
    restart: bool = False
    registration: str = ""
    dual: bool = False
    district: object = None
    location: tuple | None = None
    info: bool = False  # after the case, staff ask for more and the person answers in the chat


# --- One dialogue --------------------------------------------------------------------------------


class Dialogue:
    def __init__(self, graph, world, scenario, persona, rng, number, writer=None):
        self.graph, self.world, self.persona, self.rng = graph, world, persona, rng
        self.number = number
        self.writer = writer or dialogue_writer.TemplateWriter()
        self.rewrites = []  # what the model wrote for each text, and why a try was refused
        self.texts = graph["texts"]
        self.limits = graph["limits"]
        self.doc = world.policy.doc
        self.goal = Goal(scenario=scenario, actor=scenario["actor"])
        self.user_id = "U" + uuid.UUID(int=rng.getrandbits(128)).hex
        self.ts = int(timezone.now().timestamp() * 1000)
        self.turns, self.path, self.checks = [], [], {}
        self.repairs, self.redraws, self.detours = {}, 0, []
        self.detours_left = persona.get("detours", 0)
        self.history = []  # every bot reply, oldest first
        self.flags = {}
        self.typed = {}  # field → text sent
        self.private_texts = []  # (draft, text) typed into an การใช้ไฟฟ้า draft
        self.polite = rng.choice(self.texts["polite"])
        self.requester = self.employee = self.staff_user = self.target = self.decoy = None
        self.target_texts = []
        self.info_case = self.info_reason = None  # staff asked for more (info goals)
        self.cases = []
        self.status, self.note = "passed", ""

    # -- setup ------------------------------------------------------------------------------

    def setup(self):
        scenario, goal, rng = self.goal.scenario, self.goal, self.rng
        for key, value in scenario.get("options", {}).items():
            if options.get(key) != value:
                options.change(None, key, value, "บทสนทนาจำลอง (ย้อนกลับทั้งหมดหลังจบ)")
        goal.kind = scenario.get("kind", "")
        goal.finish = scenario.get("finish", "confirm")
        goal.choose = scenario.get("choose", "topic")
        goal.district = rng.choice(self.world.districts)
        goal.registration = scenario.get("registration", "")
        goal.dual = rng.random() < scenario.get("dual", 0)
        goal.info = bool(scenario.get("info"))
        if goal.actor == "staff":
            self._setup_staff()
            return
        if goal.actor == "registered":
            self._register_directly()
        if goal.actor == "newcomer":
            self.employee = self._employee(goal.district, Division.objects.get(code="กคว."))
        if goal.kind:
            self._plan()
        goal.restart = goal.finish == "confirm" and rng.random() < 0.15

    def _register_directly(self):
        self.requester = Requester.objects.create(
            display_name=self._name(), district=self.goal.district, approved_by=self.world.system
        )
        ChannelIdentity.objects.create(
            channel=ChannelIdentity.Channel.LINE,
            external_id=self.user_id,
            requester=self.requester,
            status=ChannelIdentity.Status.ACTIVE,
        )

    def _name(self):
        names = self.texts["name"]
        return f"{self.rng.choice(names['first'])} {self.rng.choice(names['last'])}"

    def _employee(self, district, division=None):
        rng = self.rng
        proof = f"{rng.randint(1, 28):02d}/{rng.randint(1, 12):02d}/25{rng.randint(10, 45)}"
        row = Employee.objects.create(
            employee_id=f"SIM{rng.randint(10**5, 10**6 - 1)}",
            district=district,
            division=division,
            check_label="วันเกิด (วว/ดด/ปปปป)",
            check_hash=employees.digest(proof),
            is_synthetic=True,
        )
        row.proof = proof  # the simulator's own memory; only the hash is stored
        return row

    def _plan(self):
        goal, scenario, rng = self.goal, self.goal.scenario, self.rng
        if goal.kind in (K.WITHDRAWAL, K.RETURN):
            low, high = scenario.get("lines", [1, 1])
            categories = [c for c in scenario.get("categories", EQUIPMENT) if self.world.items[c]]
            if not categories:
                raise Skipped("ยังไม่มีรายการของในหมวดที่สถานการณ์นี้ใช้")
            used = set()
            for _ in range(rng.randint(low, high)):
                choices = [
                    i for i in self.world.items[rng.choice(categories)] if i.code not in used
                ]
                if choices:
                    item = rng.choice(choices)
                    used.add(item.code)
                    goal.lines.append(self._line(item))
        else:
            topics = [
                t for t in scenario.get("topics", []) if t not in EQUIPMENT or self.world.items[t]
            ]
            goal.topic = rng.choice(topics)
            if goal.topic in EQUIPMENT and goal.choose == "topic" and self.world.items[goal.topic]:
                if rng.random() < scenario.get("item", 0):
                    goal.lines.append(self._line(rng.choice(self.world.items[goal.topic])))
            if goal.topic not in STORY_TOPICS:
                goal.hazard = rng.random() < scenario.get("hazard", 0)
                goal.negated_hazard = not goal.hazard and rng.random() < scenario.get(
                    "negated_hazard", 0
                )
        self._plan_fields()

    def _line(self, item):
        goal, rng = self.goal, self.rng
        line_ = {"item": item, "quantity": None, "serials": []}
        if goal.kind == K.PROBLEM:
            if item.is_serialized and rng.random() < 0.5:
                line_["serials"] = self._serials(rng.randint(1, 2))
            return line_
        if item.is_serialized or not item.is_divisible:
            quantity = str(rng.randint(1, 3 if goal.kind == K.RETURN else 5))
        else:
            quantity = rng.choice(["5", "12", "7.5", "20"])
        line_["quantity"] = quantity
        if goal.kind == K.RETURN and item.is_serialized:
            line_["serials"] = self._serials(int(quantity))
        return line_

    def _serials(self, count):
        return [f"SIM{self.rng.randint(10**6, 10**7 - 1)}" for _ in range(count)]

    def _plan_fields(self):
        goal, rng, texts = self.goal, self.rng, self.texts
        planned = {"related_case_number": None}
        if goal.kind == K.WITHDRAWAL:
            planned["purpose"] = self._compose(texts["purpose"])
            planned["needed_by"] = None
            if rng.random() < 0.6:
                due = timezone.localdate() + timedelta(days=rng.randint(3, 40))
                planned["needed_by"] = f"{due.day:02d}/{due.month:02d}/{due.year + 543}"
        elif goal.kind == K.RETURN:
            planned["condition"] = self._compose(texts["condition"])
            planned["reason"] = self._compose(texts["reason"])
            planned["source_reference"] = None
            if rng.random() < 0.4:
                planned["source_reference"] = self._compose(texts["source_reference"], polite=False)
        elif goal.topic in STORY_TOPICS:
            planned["description"] = self._compose(texts["description"][goal.topic])
            planned["impact"] = None
            chance = 0.6 if goal.topic == Case.Topic.USAGE else 0.3
            if rng.random() < chance:
                planned["impact"] = self._compose(texts[f"{goal.topic}_impact"])
        else:
            planned["description"] = self._compose(texts["description"][goal.topic], story=True)
            goal.severe = rng.choice([True, False, None])
            planned["impact"] = None
            if goal.severe is not None:
                planned["impact"] = self._compose(
                    texts["impact"]["severe" if goal.severe else "minor"]
                )
        goal.fields = planned

    # -- realising text, with the generator's retry ------------------------------------------

    def _fill(self, template):
        rng = self.rng
        return template.format(
            house=f"{rng.randint(1, 299)}/{rng.randint(1, 30)}",
            soi=rng.randint(1, 120),
            place=rng.choice(self.texts["places"]),
            n=rng.randint(2, 9),
            ca=f"9{rng.randint(10**10, 10**11 - 1)}",
            phone=f"09{rng.randint(10**7, 10**8 - 1)}",
            doc=rng.randint(10**5, 10**6 - 1),
        )

    def _compose(self, bank, story=False, polite=True):
        """Draw a text until it agrees with the dialogue's labels. A failed draw is the
        feedback: that template is left out of the next draw. With a model writer, the text
        that agrees is then rewritten the way a person types (`_rewrite`)."""
        goal, rng = self.goal, self.rng
        left = list(bank)
        for _ in range(int(self.limits["max_redraws"])):
            if not left:
                break
            template = rng.choice(left)
            text = self._fill(template)
            if story and goal.hazard:
                text += " " + rng.choice(self.texts["hazard"].get(goal.topic, ["อันตราย"]))
            elif story and goal.negated_hazard:
                text += " " + rng.choice(self.texts["negated_hazard"])
            courteous = polite and rng.random() < self.persona.get("polite", 0)
            if not self._disagrees(text, story):
                if self.writer.name != "templates":
                    return self._rewrite(text, story, courteous)
                return text + (self.polite if courteous else "")
            self.redraws += 1
            left.remove(template)
        raise Skipped(f"แม่แบบข้อความไม่พอให้ตรงกับป้ายของบทสนทนา ({bank[0][:30]}…)")

    def _rewrite(self, text, story, courteous):
        """The model's version when it keeps every label; otherwise the reason goes back to
        the model and it tries again. After the last try the template text is used."""
        ending = f"ลงท้ายด้วย {self.polite.strip()}" if courteous else "ไม่ต้องมีคำลงท้าย"
        style = f"{self.persona.get('writing', 'พิมพ์สั้น ภาษาพูด')} {ending}"
        fallback = text + (self.polite if courteous else "")
        entry = {"from": fallback, "to": fallback, "by": "template", "tries": 0, "refused": []}
        feedback = []
        for _ in range(int(self.limits["max_redraws"])):
            written = self.writer.rewrite(text, style, feedback)
            if written is None:
                break
            entry["tries"] += 1
            reason = self._unfaithful(text, written, story, style)
            if not reason:
                entry.update(to=written, by=self.writer.name)
                break
            entry["refused"].append(reason)
            feedback.append((written, reason))
        self.rewrites.append(entry)
        return entry["to"]

    def _unfaithful(self, source, written, story, style=""):
        """Why a rewrite no longer tells the same story, or ""."""
        goal, current = self.goal, self.world.policy
        if not written:
            return "ไม่มีข้อความ"
        instructions = [style, self.persona.get("label", ""), "ข้อความเดิม", "วิธีพิมพ์"]
        if leak(written, instructions) or "ลงท้ายด้วย" in written:
            return "มีคำสั่งเรื่องวิธีพิมพ์ปนมากับข้อความ"
        reason = self._disagrees(written, story)
        if reason:
            return reason
        if FOREIGN.search(written):
            return "มีอักษรภาษาอื่นที่ไม่ใช่ไทยหรืออังกฤษปนมา"
        refuse = self.texts.get("not_requester", [])
        if any(phrase in written for phrase in refuse):
            return "เขียนเป็นคำตอบของเจ้าหน้าที่ ไม่ใช่ข้อความของผู้แจ้ง"
        if overlap(source, written) < self.limits.get("rewrite_overlap", 0.5):
            return "เนื้อความเปลี่ยนไปจากข้อความเดิมมากเกินไป"
        if len(written) > self.limits.get("rewrite_length", 2.0) * len(source) + 20:
            return "ยาวกว่าข้อความเดิมมาก อาจเพิ่มเรื่องที่ไม่มี"
        for rule in self.texts.get("keep_meaning", []):
            if any(w in source for w in rule["when"]) and not any(
                w in written for w in rule["need"]
            ):
                return rule["why"]
        numbers, allowed = numbers_in(source)
        found = set(re.findall(r"\d+", written.translate(TO_ARABIC)))
        if numbers - found:
            return f"ตัวเลข {', '.join(sorted(numbers - found))} หายไป"
        if found - numbers - allowed:
            return f"มีตัวเลขใหม่ {', '.join(sorted(found - numbers - allowed))}"
        lost = named_categories(self.doc, source) - named_categories(self.doc, written)
        if lost and goal.topic not in STORY_TOPICS:  # a story topic decides by itself
            terms = sorted(
                {e["term"] for e in self.doc["equipment_terms"] if e["category"] in lost}
                & _words_in(self.doc, source)
            )
            return f"ชื่ออุปกรณ์ ({', '.join(terms)}) หายไป"
        if policy.usage_text(source, current) and not policy.usage_text(written, current):
            return "คำของเรื่องการใช้ไฟฟ้าหายไป"
        found = policy.hazard_keywords({"text": written}, current)
        if story and goal.hazard and not found:
            said = [t for t in self.doc.get("hazard_terms", []) if t in source][:2]
            return f"คำที่บอกอันตราย ({', '.join(said)}) หายไป"
        if story and goal.negated_hazard and found:
            return "อ่านแล้วเหมือนมีอันตราย ทั้งที่ข้อความเดิมบอกว่าไม่มี"
        return ""

    def _disagrees(self, text, story):
        """Why this text would mislead the oracle under the active policy, or ""."""
        goal, current = self.goal, self.world.policy
        if goal.topic in STORY_TOPICS:
            if goal.topic == Case.Topic.SUGGESTION and policy.usage_text(text, current):
                return "ข้อเสนอแนะมีคำของเรื่องการใช้ไฟฟ้า"
            return ""
        if policy.usage_text(text, current):
            return "มีคำของเรื่องการใช้ไฟฟ้า"
        if names_hazard(self.doc, text) and not (story and (goal.hazard or goal.negated_hazard)):
            return "มีคำอันตรายที่บทสนทนานี้ไม่มี"
        named = named_categories(self.doc, text)
        if not (story and goal.kind == K.PROBLEM):
            return "มีคำของอุปกรณ์ในข้อความที่ไม่ใช่เรื่องเล่า" if named else ""
        truth = {goal.topic} if goal.topic in EQUIPMENT else set()
        if named - truth:
            other = sorted(
                e["term"]
                for e in self.doc["equipment_terms"]
                if e["category"] in named - truth and e["term"].lower() in text.lower()
            )
            return f"มีคำของอุปกรณ์หมวดอื่น ({', '.join(other)})"
        return ""

    def digits(self, text):
        if self.rng.random() < self.persona.get("thai_digits", 0):
            return text.translate(TO_THAI)
        return text

    # -- events -----------------------------------------------------------------------------

    def event(self, event_type, **extra):
        self.ts += 1000
        event = {
            "type": event_type,
            "mode": "active",
            "timestamp": self.ts,
            "webhookEventId": uuid.UUID(int=self.rng.getrandbits(128)).hex.upper()[:26],
            "deliveryContext": {"isRedelivery": False},
            "source": {"type": "user", "userId": self.user_id},
            "replyToken": uuid.UUID(int=self.rng.getrandbits(128)).hex,
        }
        event.update(extra)
        return event

    def message(self, **message):
        return self.event(
            "message", message={"id": str(self.rng.randint(10**12, 10**13 - 1)), **message}
        )

    def text(self, intent, value, **kwargs):
        return Move(intent, self.message(type="text", text=value), value, **kwargs)

    def press(self, intent, label, reply=None, **kwargs):
        """Press a button the bot offered in its last reply (or in `reply`)."""
        data = self.button(label, reply)
        if data is None:
            offered = ", ".join(self.buttons(reply or self.last)) or "ไม่มีปุ่ม"
            raise Finding("buttons", f"ไม่มีปุ่ม '{label}' ให้กด (มี: {offered})")
        return self.postback(intent, data, f"[กด] {label}", **kwargs)

    def postback(self, intent, data, shown, **kwargs):
        return Move(intent, self.event("postback", postback={"data": data}), shown, **kwargs)

    def location(self, intent):
        lat = round(self.rng.uniform(13.55, 13.95), 6)
        lng = round(self.rng.uniform(100.35, 100.85), 6)
        event = self.message(type="location", latitude=lat, longitude=lng, address="ตำแหน่งจำลอง")
        return Move(intent, event, f"[ตำแหน่ง] {lat}, {lng}"), (lat, lng)

    @property
    def last(self):
        return self.history[-1] if self.history else []

    @staticmethod
    def buttons(reply):
        found = {}
        for message in reply or []:
            for item in message.get("quickReply", {}).get("items", []):
                action = item["action"]
                found[action["label"]] = (
                    conversation.LOCATION
                    if action["type"] == "location"
                    else action.get("data") or action.get("uri")
                )
            if message.get("type") == "flex":
                for bubble in message["contents"]["contents"]:
                    for button in bubble["body"]["contents"]:
                        found[button["action"]["label"]] = button["action"]["data"]
        return found

    def button(self, label, reply=None):
        offered = self.buttons(reply if reply is not None else self.last)
        if label in offered:
            return offered[label]
        for shown, data in offered.items():  # long names are cut with an ellipsis
            if shown.endswith("…") and label.startswith(shown[:-1]):
                return data
        return None

    @staticmethod
    def said(reply):
        return "\n".join(m.get("text", m.get("altText", "")) for m in reply or [])

    # -- talking to the real bot ------------------------------------------------------------

    def send(self, move, step):
        """The event goes through the webhook's storage and the turn handler, as from LINE;
        the reply is kept, never sent."""
        line.store_events([move.event])
        inbox = WebhookInbox.objects.get(webhook_event_id=move.event["webhookEventId"])
        reply = conversation.handle_inbox(inbox.pk)
        self.path.append((graph_step(step), move.intent))
        self.turns.append({"role": "user", "step": step, "intent": move.intent, "said": move.shown})
        self.turns.append(
            {"role": "bot", "said": self.said(reply), "buttons": list(self.buttons(reply))}
        )
        self.history.append(reply)
        self._check_reply(move, step, reply)
        return reply

    def _check_reply(self, move, step, reply):
        if not reply:
            raise Finding("reply", f"ขั้น {step}: '{move.shown}' ไม่ได้คำตอบ")
        if len(reply) > 5:
            raise Finding("reply", f"ขั้น {step}: ตอบ {len(reply)} ข้อความ เกิน 5")
        style = self.graph.get("style", {})
        banned = [*style.get("banned", []), *style.get("banned_phrases", [])]
        for message in reply:
            if message.get("type") != "text":
                continue
            text = message["text"]
            items = message.get("quickReply", {}).get("items", [])
            if not text.strip() or len(text) > 5000:
                raise Finding("reply", f"ขั้น {step}: ข้อความว่างหรือยาวเกิน 5000")
            if len(items) > 13 or any(len(i["action"]["label"]) > 20 for i in items):
                raise Finding("reply", f"ขั้น {step}: ปุ่มเกิน 13 ปุ่มหรือชื่อปุ่มยาวเกิน 20")
            found = next((b for b in banned if b in text), None)
            if found:
                raise Finding("style", f"ขั้น {step}: มี '{found}' ใน '{text[:60]}'")
        self.checks.setdefault("reply", True)
        self.checks.setdefault("style", True)

    # -- observing where the dialogue is ----------------------------------------------------

    def identity_row(self):
        return ChannelIdentity.objects.filter(
            channel=ChannelIdentity.Channel.LINE, external_id=self.user_id
        ).first()

    def draft(self):
        chat = Conversation.objects.filter(identity=self.identity_row()).first()
        draft = chat.active_draft if chat else None
        return draft if draft is not None and draft.status == D.OPEN else None

    def observe(self):
        """The bot's step from its own records (robust to wording changes); the person still
        acts only on what the reply shows."""
        goal = self.goal
        if goal.actor == "staff":
            return self._observe_staff()
        row = self.identity_row()
        if row is None:
            return "start"
        if self.flags.get("info_asked"):
            sent = ConversationMessage.objects.filter(case=self.info_case).exists()
            return "info_sent" if sent else "awaiting_info"
        drafts = ConversationDraft.objects.filter(identity=row)
        if goal.finish == "confirm" and drafts.filter(status=D.SUBMITTED).exists():
            return "submitted"
        if (
            goal.finish == "discard"
            and drafts.filter(status=D.DISCARDED).exists()
            and not drafts.filter(status=D.OPEN).exists()
        ):
            return "discarded"
        draft = self.draft()
        if draft is not None:
            return draft.step
        if self.flags.get("usage_choice_pending"):
            return "usage_menu"
        prompt = Conversation.objects.filter(identity=row).values_list("prompt", flat=True).first()
        if prompt and prompt.startswith("reg_"):
            return prompt
        pending = RegistrationRequest.objects.filter(
            identity=row, status=RegistrationRequest.Status.PENDING
        ).exists()
        if pending and goal.registration == "other_way":
            return "reg_pending"  # The review is pending even though problem reporting is enabled.
        if row.status == ChannelIdentity.Status.ACTIVE:
            return "idle"
        return "reg_pending" if pending else "start"

    def _observe_staff(self):
        row = self.identity_row()
        if row is None or not row.is_following:
            return "staff_start"
        prompt = Conversation.objects.filter(identity=row).values_list("prompt", flat=True).first()
        if prompt in ("staff_emp", "staff_check"):
            return prompt
        if row.staff_user_id is None:
            return "staff_unlinked"
        for flag, step in (
            ("menu", "staff_ready"),
            ("detail", "staff_menu"),
            ("push", "staff_detail"),
            ("done", "staff_alerted"),
        ):
            if not self.flags.get(flag):
                return step
        return "staff_done"

    # -- choosing the next move -------------------------------------------------------------

    def choose(self, step):
        detour = self._detour(step)
        if detour is not None:
            return detour
        for intent in weighted_order(self.rng, self.graph["steps"].get(graph_step(step), [])):
            move = INTENTS[intent](self)
            if move is not None:
                return move
        return None

    def _detour(self, step):
        if self.detours_left <= 0 or self.rng.random() >= self.limits["detour_rate"]:
            return None
        self.at = step  # where the detour starts; some detours depend on it
        rows = [
            (name, detour["weight"])
            for name, detour in self.graph.get("detours", {}).items()
            if graph_step(step) in detour["steps"]
        ]
        for name in weighted_order(self.rng, rows):
            move = DETOURS[name](self)
            if move is not None:
                self.detours_left -= 1
                self.detours.append(name)
                return move
        return None

    # -- the walk -----------------------------------------------------------------------------

    def run(self):
        self.setup()
        step = self.observe()
        while len(self.path) < self.limits["max_turns"]:
            if step in TERMINAL:
                if step == "submitted" and not self.flags.get("double"):
                    self.flags["double"] = True
                    detour = self._detour(step)
                    if detour is not None:
                        step = self._take(detour, step)
                        continue
                if step == "submitted" and self.goal.info and not self.flags.get("info_asked"):
                    self._request_info()
                    step = self.observe()
                    continue
                break
            move = self.choose(step)
            if move is None:
                raise Finding("goal", f"กราฟไม่มีทางไปต่อที่ขั้น {step}")
            step = self._take(move, step)
        else:
            raise Finding("goal", f"เกิน {self.limits['max_turns']} ตาโดยยังไม่ถึงปลายทาง")
        self.checks.setdefault("goal", True)
        self.outcome(step)

    def _take(self, move, step):
        reply = self.send(move, step)
        after = self.observe()
        if move.intent in AFTER:
            AFTER[move.intent](self, step, after, reply)
        if after == "summary" and step != "summary":
            _after_summary(self, step, after, reply)
        if move.intent in DETOURS:
            return after
        tries = 0
        while move.advance and after == step and not self._progressed(move, reply):
            if graph_step(step) in CHOICE_STEPS and not self.buttons(reply):
                raise Finding("buttons", f"ขั้น {step}: บอทถามซ้ำแต่ไม่มีปุ่มให้เลือก")
            if tries >= self.limits["max_repairs"] or not move.repair:
                raise Finding("goal", f"ขั้น {step}: บอทไม่ไปต่อหลัง '{move.shown}'")
            tries += 1
            self.repairs[graph_step(step)] = self.repairs.get(graph_step(step), 0) + 1
            move = INTENTS[move.repair](self)
            reply = self.send(move, step)
            after = self.observe()
        return after

    @staticmethod
    def _progressed(move, reply):
        if move.intent == "search_item":
            return any(label != "ย้อนกลับ" for label in Dialogue.buttons(reply))
        # Staff steps after the alert are tracked in the dialogue's memory; AFTER checks them.
        return move.intent in ("press_staff_action", "type_my_cases")

    # -- outcome checks -----------------------------------------------------------------------

    def passed(self, name):
        self.checks.setdefault(name, True)

    def fail(self, name, detail):
        self.checks[name] = False
        if name in WARNINGS:
            self.note = (self.note + " / " if self.note else "") + detail
            return
        raise Finding(name, detail)

    def outcome(self, step):
        goal = self.goal
        if goal.actor == "staff":
            return self._staff_outcome()
        if goal.registration:
            self._registration_outcome()
        if step == "reg_pending":
            return
        row = self.identity_row()
        requester = row.requester or Requester.objects.filter(anonymous_identity=row).first()
        if goal.finish == "discard":
            if requester and Case.objects.filter(requester=requester).exists():
                self.fail("case", "ยกเลิกร่างแล้วแต่มีเรื่องเกิดขึ้น")
            self.passed("case")
            return
        self.cases = list(
            Case.objects.filter(requester=requester)
            .select_related("owner_division", "requester")
            .order_by("number")
        )
        if not self.cases:
            self.fail("case", "ยืนยันแล้วแต่ไม่มีเรื่อง")
        self._case_outcome()
        self._owner_outcome()
        self._hazard_outcome()
        self._privacy_outcome()
        if self.flags.get("info_asked"):
            self._info_outcome()

    def _request_info(self):
        """Staff of the owning division ask for more, the way the web does (services)."""
        case = Case.objects.filter(requester=self.requester).order_by("number").first()
        people = [
            u
            for u in staff_email.members(case.owner_division_id)
            if identity.staff_scope(u).has_any_access
        ]
        if not people:
            raise Skipped(f"ไม่มีบัญชีเจ้าหน้าที่ในกอง {case.owner_division.code}")
        reason = self.rng.choice(self.texts["info_request"])
        for action, payload in (("start_triage", None), ("request_info", {"reason": reason})):
            try:
                services.perform_action(
                    Actor.staff(people[0]),
                    case.pk,
                    action,
                    expected_revision=Case.objects.get(pk=case.pk).revision,
                    idempotency_key=f"dialogue-sim-info-{self.number:06d}-{action}",
                    payload=payload,
                )
            except CommandFailed:
                if action == "request_info":
                    raise Skipped(f"ขอข้อมูลเพิ่มในเรื่อง {case.number} ไม่ได้") from None
        self.info_case, self.info_reason = case, reason
        self.flags["info_asked"] = case.number

    def _info_outcome(self):
        sent = list(ConversationMessage.objects.filter(case=self.info_case))
        if [m.text for m in sent] != [self.flags.get("info")]:
            self.fail("info", f"ข้อความที่ถึงเรื่อง {[m.text[:30] for m in sent]}")
        self.passed("info")

    def _confirm_reply(self):
        for turn in reversed(self.turns):
            if turn["role"] == "bot" and "ยื่นเรื่องแล้ว" in turn["said"]:
                return turn["said"]
        return ""

    def _case_outcome(self):
        goal = self.goal
        got = sorted(
            (line_.item.code, line_.requested_quantity, list(line_.serials or []))
            for case in self.cases
            for line_ in case.lines.select_related("item")
        )
        wanted = sorted(
            (p["item"].code, Decimal(p["quantity"]) if p["quantity"] else None, p["serials"])
            for p in goal.lines
        )
        if got != wanted:
            self.fail("case", f"รายการของไม่ตรง: ได้ {got} ต้องการ {wanted}")
        case = self.cases[0]
        stored = {
            "purpose": case.purpose,
            "needed_by": case.needed_by.isoformat() if case.needed_by else "",
            "condition": case.condition,
            "reason": case.return_reason,
            "source_reference": case.source_reference,
            "description": case.description,
            "impact": case.impact,
        }
        for name, typed in self.typed.items():
            if name not in stored:
                continue
            expected = typed.strip()
            if name == "needed_by":
                expected = conversation._parse_date(typed).isoformat()
            if stored[name] != expected:
                self.fail("case", f"ช่อง {name} ไม่ตรง: '{stored[name]}' / '{expected}'")
        topic = self._submitted_topic()
        if case.kind != goal.kind or case.topic != topic:
            self.fail(
                "case", f"ประเภท/หัวข้อไม่ตรง: {case.kind}/{case.topic} ต้องการ {goal.kind}/{topic}"
            )
        self.passed("case")

    def _submitted_topic(self):
        goal = self.goal
        if goal.kind != K.PROBLEM or goal.choose != "topic":
            return ""
        if goal.lines or goal.topic not in EQUIPMENT:
            return goal.topic
        # The general menu deliberately groups transformers with other equipment.  If the
        # requester skips the catalogue item, the selected group is the only known topic.
        return "meter" if goal.topic == "meter" else "other"

    def _owner_outcome(self):
        goal = self.goal
        if goal.kind == K.PROBLEM:
            if goal.lines:
                category, item = goal.lines[0]["item"].category, goal.lines[0]["item"].code
            else:
                category, item = (self._submitted_topic(), None)
            wanted = {expected_owner(self.doc, K.PROBLEM, category, item)}
        else:
            wanted = {
                expected_owner(self.doc, goal.kind, p["item"].category, p["item"].code)
                for p in goal.lines
            }
        got = {c.owner_division.code for c in self.cases}
        if got != wanted:
            self.fail("owner", f"กองเจ้าภาพ {sorted(got)} แต่กติกากำหนด {sorted(wanted)}")
        self.passed("owner")
        if goal.kind != K.PROBLEM:
            if len(self.cases) != len(wanted):
                self.fail("split", f"ได้ {len(self.cases)} เรื่อง แต่มี {len(wanted)} กอง")
            reply = self._confirm_reply()
            missing = [c.number for c in self.cases if c.number not in reply]
            if missing:
                self.fail("split", f"คำตอบตอนยืนยันไม่บอกเลข {missing}")
            self.passed("split")

    def _hazard_outcome(self):
        goal = self.goal
        floors = dict(
            CaseTriage.objects.filter(case__in=self.cases).values_list(
                "case__number", "priority_floor"
            )
        )
        if goal.hazard:
            if set(floors.values()) != {"P0"}:
                self.fail("hazard", f"แจ้งอันตรายแต่ได้ระดับ {floors}")
            if "คำแนะนำเร่งด่วน" not in self._confirm_reply():
                self.fail("hazard", "แจ้งอันตรายแต่ไม่มีคำแนะนำเร่งด่วนในคำตอบ")
            self.passed("hazard")
        elif "P0" in floors.values():
            text = self.typed.get("description", "")[:40]
            self.fail("hazard_overflag", f"ไม่ได้แจ้งอันตรายแต่ได้ P0 ({text})")

    def _privacy_outcome(self):
        goal = self.goal
        own = [t for t in self.typed.values() if t] + [
            c.requester.display_name for c in self.cases if not c.requester.is_anonymous
        ]
        for case in self.cases:
            for kind in staff_line.IMPORTANT:
                found = leak(staff_line._text(kind, case), own)
                if found:
                    self.fail("staff_private", f"ข้อความเด้ง {kind} มี '{found}' ของผู้แจ้ง")
        self.passed("staff_private")
        if goal.topic == Case.Topic.USAGE:
            for draft, text in self.private_texts:
                if not ai_chat.private_text(draft, text):
                    self.fail("ai_private", f"ข้อความ '{text[:30]}' อาจถูกส่งให้ AI")
            now = timezone.now()
            for case in self.cases:
                with override_settings(AI_ENABLED=True):
                    asked = policy.maybe_request_ai(case, now)
                if not policy.private_topic(case) or asked is not None:
                    self.fail("ai_private", f"เรื่อง {case.number} จะถูกส่งให้ AI คัดกรอง")
            self.passed("ai_private")
        if goal.actor == "anonymous":
            case = self.cases[0]
            if not case.requester.is_anonymous or str(case.requester) != "ไม่ระบุตัวตน":
                self.fail("anonymous", f"ผู้แจ้งไม่ใช่ผู้แจ้งไม่ระบุตัวตน ({case.requester})")
            if (case.latitude, case.longitude) != _decimal_point(goal.location):
                self.fail("anonymous", f"ตำแหน่ง {case.latitude},{case.longitude} ไม่ตรงกับที่ส่ง")
            self.passed("anonymous")

    def _registration_outcome(self):
        goal = self.goal
        row = self.identity_row()
        registration = RegistrationRequest.objects.filter(identity=row).order_by("-pk").first()
        if registration is None:
            self.fail("registration", "ไม่มีคำขอลงทะเบียน")
        if goal.registration == "employee":
            decider = registration.decided_by.username if registration.decided_by else ""
            if (
                row.status != ChannelIdentity.Status.ACTIVE
                or decider != identity.SYSTEM_USERNAME
                or registration.employee_id != self.employee.pk
            ):
                self.fail("registration", f"รหัสพนักงานตรงแต่ไม่ได้อนุมัติโดยระบบ ({decider})")
        elif registration.status != RegistrationRequest.Status.PENDING:
            self.fail("registration", f"เลือกวิธีอื่นแต่สถานะคือ {registration.status}")
        sent = self.flags.get("reg_location")
        if sent and (registration.latitude, registration.longitude) != _decimal_point(sent):
            self.fail("registration", "ตำแหน่งที่เก็บกับคำขอไม่ตรงกับที่ส่ง")
        self.passed("registration")

    # -- staff ------------------------------------------------------------------------------

    def _setup_staff(self):
        goal, rng = self.goal, self.rng
        topic = rng.choice(goal.scenario.get("topics", ["meter"]))
        text = self._fill(rng.choice(self.texts["description"][topic]))
        if topic not in STORY_TOPICS:
            text += " " + rng.choice(self.texts["hazard"][topic])
        reporter = self.world.reporter
        result = services.submit_case(
            Actor.for_requester(reporter),
            idempotency_key=f"dialogue-sim-{self.number:06d}-{rng.getrandbits(32):08x}",
            payload={
                "kind": "problem",
                "topic": topic,
                "description": text,
                # 25 Oct 2569 Q11: a usage report names its meter and the meter's district.
                **(
                    {"meters": [{"number": "SIM123456", "district": reporter.district.code}]}
                    if topic == Case.Topic.USAGE
                    else {}
                ),
            },
        )
        self.target = Case.objects.select_related("owner_division").get(pk=result.case_id)
        self.target_texts = [text, reporter.display_name]
        people = [
            u
            for u in staff_email.members(self.target.owner_division_id)
            if identity.staff_scope(u).has_any_access
        ]
        if not people:
            raise Skipped(f"ไม่มีบัญชีเจ้าหน้าที่ในกอง {self.target.owner_division.code}")
        self.staff_user = people[0]
        self.employee = self._employee(None, self.target.owner_division)
        StaffProfile.objects.filter(user=self.staff_user).update(
            employee_id=self.employee.employee_id
        )
        if goal.dual:
            self._register_directly()

    def _staff_outcome(self):
        row = self.identity_row()
        if row.staff_user_id != self.staff_user.pk:
            self.fail("staff", "LINE ไม่ได้เชื่อมกับบัญชีเจ้าหน้าที่")
        action = self.flags.get("acted")
        if (
            action
            and not Decision.objects.filter(
                case=self.target, action=action, actor=self.staff_user
            ).exists()
        ):
            self.fail("staff", f"กด '{action}' ใน LINE แต่ไม่มีบันทึกการตัดสิน")
        self.passed("staff")
        self.passed("staff_private")

    # -- the record ---------------------------------------------------------------------------

    def record(self):
        goal = self.goal
        return {
            "id": f"sim-{self.number:05d}",
            "is_synthetic": True,
            "scenario": goal.scenario["id"],
            "persona": self.persona["id"],
            "status": self.status,
            "note": self.note,
            "labels": {
                "actor": goal.actor,
                "kind": goal.kind,
                "topic": goal.topic,
                "choose": goal.choose,
                "hazard": goal.hazard,
                "negated_hazard": goal.negated_hazard,
                "severe_impact": goal.severe,
                "lines": [
                    {"item": p["item"].code, "quantity": p["quantity"], "serials": p["serials"]}
                    for p in goal.lines
                ],
                "fields": dict(self.typed),
                "dual": goal.dual,
            },
            "cases": [
                {
                    "number": c.number,
                    "kind": c.kind,
                    "topic": c.topic,
                    "owner": c.owner_division.code,
                }
                for c in self.cases
            ],
            "checks": self.checks,
            "path": [list(p) for p in self.path],
            "repairs": self.repairs,
            "redraws": self.redraws,
            "writer": self.writer.name,
            "rewrites": self.rewrites,
            "detours": self.detours,
            "turns": self.turns,
        }


def _decimal_point(point):
    return tuple(Decimal(str(v)).quantize(Decimal("0.000001")) for v in point)


# --- Intents: what the person does at a step (None = does not apply now) ------------------------
# Only the move that is chosen is built, so a function may change the dialogue's memory.


def _types(d):
    return d.rng.random() < d.persona.get("type_choice", 0)


def _next_line(d):
    draft = d.draft()
    done = len(draft.data.get("lines", [])) if draft else 0
    return d.goal.lines[done] if done < len(d.goal.lines) else None


def _field(d):
    draft = d.draft()
    return draft.step.split(":", 1)[1] if draft and draft.step.startswith("field:") else ""


def i_follow(d):
    return Move("follow", d.event("follow"), "[เพิ่มเพื่อน]")


def i_press_as_employee(d):
    """ลงทะเบียนในฐานะ (15 ต.ค. 2569 Q12): an employee, or one who will pick another way there."""
    if d.goal.actor != "newcomer" or d.goal.registration not in ("employee", "other_way"):
        return None
    return d.press("press_as_employee", "พนักงาน กฟน.")


def i_press_as_other(d):
    if d.goal.actor != "newcomer" or d.goal.registration in ("employee", "other_way"):
        return None
    return d.press("press_as_other", "อื่น ๆ")


def i_press_anonymous_kind(d):
    return d.press("press_anonymous_kind", "ไม่ระบุตัวตน") if d.goal.actor == "anonymous" else None


def i_type_name(d):
    return d.text("type_name", d._name()) if d.goal.actor == "newcomer" else None


def i_press_anonymous(d):
    return d.press("press_anonymous", "แจ้งแบบไม่ระบุตัวตน") if d.goal.actor == "anonymous" else None


def i_press_district(d):
    return d.press("press_district", d.goal.district.name)


def i_type_employee_id(d):
    if d.employee is None or (d.goal.actor == "newcomer" and d.goal.registration != "employee"):
        return None
    return d.text("type_employee_id", d.employee.employee_id)


def i_press_other_way(d):
    return d.press("press_other_way", "ใช้วิธีอื่น") if d.goal.registration == "other_way" else None


def i_type_proof(d):
    return d.text("type_proof", d.digits(d.employee.proof))


def i_send_location(d):
    move, point = d.location("send_location")
    if d.observe() == "reg_location":
        d.flags["reg_location"] = point
    else:
        d.goal.location = point
    return move


def i_skip_location(d):
    return d.press("skip_location", "ข้าม") if "ข้าม" in d.buttons(d.last) else None


def i_press_new(d):
    if d.goal.kind == K.PROBLEM and d.goal.topic == Case.Topic.USAGE:
        d.flags["usage_choice_pending"] = True
        return d.postback("press_new", "m=problem&topic=usage", "[กด] แจ้งปัญหาการใช้ไฟฟ้า")
    return d.postback("press_new", "m=new", "[กด] ส่งเรื่องใหม่")


def i_type_new(d):
    if d.goal.kind == K.PROBLEM and d.goal.topic == Case.Topic.USAGE:
        d.flags["usage_choice_pending"] = True
        return d.postback("type_new", "m=problem&topic=usage", "แจ้งปัญหาการใช้ไฟฟ้า")
    return d.text("type_new", "ส่งเรื่องใหม่", repair="press_new")


def i_press_kind(d):
    return d.press("press_kind", conversation.KIND_LABELS[d.goal.kind])


def i_type_kind_number(d):
    if not _types(d):
        return None
    number = str(conversation.KIND_ORDER.index(d.goal.kind) + 1)
    return d.text("type_kind_number", d.digits(number), repair="press_kind")


def i_press_topic(d):
    if d.goal.choose != "topic":
        return None
    return d.press("press_topic", conversation._topic_menu_label(_topic_choice(d.goal)))


def i_type_topic_number(d):
    topics = conversation._topics()
    choice = _topic_choice(d.goal)
    if d.goal.choose != "topic" or choice not in topics or not _types(d):
        return None
    number = str(topics.index(choice) + 1)
    return d.text("type_topic_number", d.digits(number), repair="press_topic")


def _topic_choice(goal):
    if goal.topic in EQUIPMENT:
        return "meter" if goal.topic == "meter" else "other"
    return goal.topic


def i_press_usage_choice(d):
    labels = [
        label for label in d.buttons(d.last) if label not in {"ย้อนกลับ", "หน้าหลัก", "ติดตามเรื่อง"}
    ]
    if not labels:
        return None
    return d.press("press_usage_choice", labels[0])


def i_press_discard(d):
    return d.press("press_discard", "ยกเลิกร่าง") if d.goal.finish == "discard" else None


def i_press_category(d):
    line_ = _next_line(d)
    if line_ is None:
        return None
    item = line_["item"]
    group = item.line_group or ("meter" if item.category == "meter" else "other")
    return d.press("press_category", conversation.METER_GROUPS[group])


def i_type_category_number(d):
    line_ = _next_line(d)
    if line_ is None or not _types(d):
        return None
    item = line_["item"]
    group = item.line_group or ("meter" if item.category == "meter" else "other")
    number = str(list(conversation.METER_GROUPS).index(group) + 1)
    return d.text("type_category_number", d.digits(number), repair="press_category")


def i_press_lines_done(d):
    if _next_line(d) is not None:
        return None
    label = "ไปต่อ" if "ไปต่อ" in d.buttons(d.last) else "ข้าม (ไม่ระบุของ)"
    return d.press("press_lines_done", label)


def i_press_item(d):
    line_ = _next_line(d)
    if line_ is None or d.button(line_["item"].name) is None and d.observe() == "item_search":
        return None
    return d.press("press_item", line_["item"].name)


def i_press_no_model(d):
    if _next_line(d) is not None or d.goal.kind != K.PROBLEM:
        return None
    return d.press("press_no_model", "ไม่ระบุรุ่น")


def i_press_back(d):
    if d.flags.get("back") or _next_line(d) is None:
        return None
    d.flags["back"] = True
    return d.press("press_back", "ย้อนกลับ")


def i_search_item(d):
    """Part of the name first; when the item is not among the results, the whole code, as a
    person narrows a search."""
    line_ = _next_line(d)
    if line_ is None or d.button(line_["item"].name) is not None:
        return None
    item = line_["item"]
    tried = d.flags.setdefault("searched", set())
    query = item.code if tried else item.name[: max(3, len(item.name) // 2)]
    tried.add(query)
    return d.text("search_item", query)


def i_type_quantity(d):
    line_ = _next_line(d)
    if line_ is None or line_["quantity"] is None:
        return None
    styles = d.persona.get("quantity", {"digits": 1})
    style = d.rng.choices(list(styles), weights=list(styles.values()))[0]
    quantity, unit = line_["quantity"], line_["item"].unit
    words = d.texts.get("number_words", {})
    if style == "words" and quantity in words:
        value = words[quantity] + unit  # a person may write the number out; the bot asks again
    elif style == "thai":
        value = quantity.translate(TO_THAI)
    elif style == "unit":
        value = f"{quantity} {unit}"
    else:
        value = quantity
    return d.text("type_quantity", value, repair="type_quantity_digits")


def i_type_quantity_digits(d):
    line_ = _next_line(d)
    return d.text("type_quantity_digits", line_["quantity"]) if line_ else None


def i_type_serials(d):
    line_ = _next_line(d)
    if line_ is None or not line_["serials"]:
        return None
    return d.text("type_serials", " ".join(line_["serials"]))


def i_press_skip_serials(d):
    line_ = _next_line(d)
    if line_ is None or line_["serials"] or d.goal.kind == K.RETURN:
        return None
    return d.press("press_skip_serials", "ข้าม")


def i_type_sap_number(d):
    """The SAP slip a withdrawal or return follows (23 Oct 2569): one synthetic number per
    dialogue, typed again if the bot asks again."""
    number = d.flags.setdefault("sap_number", f"45{d.rng.randint(10**7, 10**8 - 1)}")
    return d.text("type_sap_number", number)


def i_type_meter_number(d):
    """เรื่องการใช้ไฟฟ้า (25 Oct 2569 Q11): one synthetic meter number per dialogue."""
    number = d.flags.setdefault("meter_number", f"SIM{d.rng.randint(10**5, 10**6 - 1)}")
    return d.text("type_meter_number", number)


def i_type_district(d):
    """เขตของเครื่องวัด: the name of a real district, as a person would type it."""
    from operations.models import District

    names = [
        n.replace("การไฟฟ้านครหลวงเขต", "") for n in District.objects.values_list("name", flat=True)
    ]
    return d.text("type_district", d.rng.choice(sorted(names) or ["วัดเลียบ"]))


def i_type_entry(d):
    """The typed fields the bot asks for a line (typed_lines), in one message split by "/".
    What is asked comes from the draft, like the step itself; the person makes up synthetic
    meter numbers, one per unit, and a common size."""
    draft = d.draft()
    entry = (draft.data.get("entry") if draft is not None else None) or {}
    keys = entry.get("keys", [])[entry.get("at", 0) :]
    if not keys:
        return None
    line = draft.data["lines"][entry["line"]] if entry.get("line") is not None else {}
    quantity = str(int(float(entry.get("row", {}).get("quantity") or line.get("quantity") or 1)))
    values = []
    for key in keys:
        if key == "numbers":
            values.append(
                " ".join(f"SIM-{d.rng.randint(10**5, 10**6 - 1)}" for _ in range(int(quantity)))
            )
        elif key == "size":
            values.append(d.rng.choice(["5(15)A", "15(45)A", "5(100)A"]))
        elif key == "quantity":
            values.append(quantity)
        elif key == "name":
            values.append("อุปกรณ์จำลอง")
        else:
            values.append("ข้อมูลจำลอง")
    return d.text("type_entry", " / ".join(values))


def i_press_add_line(d):
    return d.press("press_add_line", "เพิ่มรายการ") if _next_line(d) is not None else None


def i_change_mind_add(d):
    """Press เพิ่มรายการ with nothing more to add, then ไปต่อ from the category step."""
    if (
        _next_line(d) is not None
        or d.flags.get("changed_mind")
        or "เพิ่มรายการ" not in d.buttons(d.last)
    ):
        return None
    d.flags["changed_mind"] = True
    return d.press("change_mind_add", "เพิ่มรายการ")


def i_press_continue(d):
    return d.press("press_continue", "ไปต่อ") if _next_line(d) is None else None


def i_type_field(d):
    name = _field(d)
    planned = d.goal.fields.get(name, "unknown")
    if planned is None:
        return None
    if planned == "unknown":
        if "ข้าม" in d.buttons(d.last):
            return None
        # A required field the graph does not know yet: answer, and say so in the record.
        planned = d.rng.choice(d.texts.get("field_generic", ["ข้อมูลทดสอบ"]))
        d.note = (d.note + " / " if d.note else "") + f"ช่องที่กราฟยังไม่รู้จัก: {name}"
    if name == "needed_by":
        planned = d.digits(planned)
    d.typed[name] = planned
    if d.goal.topic == Case.Topic.USAGE:
        d.private_texts.append((d.draft(), planned))
    return d.text("type_field", planned)


def i_press_skip_field(d):
    name = _field(d)
    if d.goal.fields.get(name, "unknown") not in (None, "unknown"):
        return None
    return d.press("press_skip_field", "ข้าม") if "ข้าม" in d.buttons(d.last) else None


def i_press_confirm(d):
    if d.goal.finish != "confirm" or (d.goal.restart and not d.flags.get("restarted")):
        return None
    return d.press("press_confirm", "ยืนยันส่งเรื่อง")


def i_press_restart(d):
    if not d.goal.restart or d.flags.get("restarted"):
        return None
    d.flags["restarted"] = True
    d.typed.clear()
    d.private_texts.clear()
    return d.press("press_restart", "กรอกใหม่")


def i_type_link_code(d):
    code = staff_line.issue_code(d.staff_user)  # what บัญชีของฉัน shows the person on the web
    return d.text("type_link_code", d.digits(f"เชื่อม {code}"))


def i_ask_employee_link(d):
    return d.text("ask_employee_link", "เชื่อมบัญชีเจ้าหน้าที่")


def i_type_staff_menu(d):
    d.flags["menu"] = True
    return d.text("type_staff_menu", d.rng.choice(sorted(conversation.STAFF_WORDS)))


def i_press_staff_case(d):
    """Open the division's case from งานของกอง; a staff-only LINE may type its number."""
    d.flags["detail"] = True
    offered = d.buttons(d.last)
    if d.target.number in offered:
        d.flags["viewed"] = d.target.number
        return d.press("press_staff_case", d.target.number)
    if not d.goal.dual:
        d.flags["viewed"] = d.target.number
        return d.text("press_staff_case", d.target.number)
    other = next((label for label in offered if conversation.CASE_NUMBER.match(label)), None)
    if other is None:
        raise Finding("staff", "งานของกองไม่มีเรื่องให้กดดู")
    d.flags["viewed"] = other
    return d.press("press_staff_case", other)


def i_press_push(d):
    """The important alert reaches the linked LINE with only the number and the division;
    pressing it shows the details as a reply."""
    planned = staff_line.plan("staff_hazard", f"sim:{d.number}", d.target, [d.staff_user])
    push = NotificationDelivery.objects.filter(
        case=d.target,
        channel=NotificationDelivery.Channel.LINE_PUSH,
        identity__external_id=d.user_id,
    ).first()
    if not planned or push is None:
        raise Finding("staff", "เรื่องด่วนของกองไม่ได้วางแผนเด้งถึง LINE ที่เชื่อมไว้")
    found = leak(push.text, d.target_texts)
    if found:
        raise Finding("staff_private", f"ข้อความเด้งมี '{found}' ของผู้แจ้ง")
    d.flags["push"] = True
    d.flags["viewed"] = d.target.number
    return d.press("press_push", f"ดูเรื่อง {d.target.number}", reply=[push.message])


def i_press_staff_action(d):
    if "acted" in d.flags:
        return None
    offered = d.buttons(d.last)
    for action, label in staff_line.LINE_ACTIONS.items():
        if label in offered and action != "request_info":
            d.flags["acted"] = action
            return d.press("press_staff_action", label)
    return None


def i_type_my_cases(d):
    if not d.goal.dual or "mine" in d.flags:
        return None
    d.flags["mine"] = True
    return d.text("type_my_cases", "เรื่องของฉัน")


def i_finish_staff(d):
    d.flags["done"] = True
    return d.text("finish_staff", "งานของกอง")


def i_type_info(d):
    """The information staff asked for; when the bot asks first, the person confirms."""
    if "ส่งถึงเจ้าหน้าที่" in d.buttons(d.last):
        d.flags["info"] = d.flags.get("offered")
        return d.press("press_send_info", "ส่งถึงเจ้าหน้าที่")
    d.flags["info"] = d._fill(d.rng.choice(d.texts["info"]))
    return d.text("type_info", d.flags["info"])


def i_press_not_info(d):
    """'ไม่ใช่ข้อมูลเพิ่ม' after the bot asked whether a question was information."""
    if "ไม่ใช่ข้อมูลเพิ่ม" not in d.buttons(d.last):
        return None
    return d.press("press_not_info", "ไม่ใช่ข้อมูลเพิ่ม", advance=False)


def i_greet_waiting(d):
    """A greeting while staff wait for information: never sent to them (once)."""
    if d.flags.get("greeted"):
        return None
    d.flags["greeted"] = True
    return d.text("greet_waiting", d.rng.choice(d.texts["greeting"]), advance=False)


def i_ask_waiting(d):
    """A question while staff wait for information: the bot asks before it goes to them
    (once)."""
    if d.flags.get("offered"):
        return None
    asks = [
        t
        for texts in d.texts.get("questions", {}).values()
        for t in texts
        if conversation.reads_as(t) == "question"
    ]
    if not asks:
        return None
    d.flags["offered"] = d.rng.choice(asks)
    return d.text("ask_waiting", d.flags["offered"], advance=False)


INTENTS = {name[2:]: fn for name, fn in list(globals().items()) if name.startswith("i_")}


# --- Detours: moves that do not move the dialogue on --------------------------------------------


def x_type_offtopic(d):
    return d.text("type_offtopic", d.rng.choice(d.texts["offtopic"]), advance=False)


def x_press_stale(d):
    draft = d.draft()
    if draft is None:
        return None
    for reply in reversed(d.history[:-1]):
        for label, data in d.buttons(reply).items():
            if not str(data).startswith("t="):
                continue
            token = PostbackToken.objects.filter(token=data[2:], draft=draft).first()
            if token is not None and token.draft_revision != draft.revision:
                return d.postback("press_stale", data, f"[กดปุ่มเก่า] {label}", advance=False)
    return None


def x_send_photo(d):
    event = d.message(type="image", contentProvider={"type": "line"})
    return Move("send_photo", event, "[รูป]", advance=False)


def x_check_mine(d):
    d.decoy = d.decoy or d.world.decoy_case(d.number)
    return d.postback("check_mine", "m=mine", "[กด] เรื่องของฉัน", advance=False)


def x_peek_case(d):
    d.decoy = d.decoy or d.world.decoy_case(d.number)
    return d.text("peek_case", d.decoy.number, advance=False)


def x_double_confirm(d):
    for reply in reversed(d.history):
        data = d.button("ยืนยันส่งเรื่อง", reply)
        if data:
            return d.postback("double_confirm", data, "[กดยืนยันซ้ำ]", advance=False)
    return None


def x_ask_question(d):
    """A question from the graph's bank whose answer is approved (คลังคำตอบ ก, ADR 0012).
    The run keeps the AI off, so this is the keyword level. Where the bot asked for a typed
    answer, only a question that reads as one, and only while ตัวเลือกระบบ chat_typed_steps
    checks typed text."""
    if answers.mode() == "off":
        return None
    typed = graph_step(getattr(d, "at", "")) == "field"
    if typed and options.get(options.CHAT_TYPED) != "check":
        return None
    bank = {
        key: [t for t in texts if not typed or conversation.reads_as(t) == "question"]
        for key, texts in d.texts.get("questions", {}).items()
    }
    usable = [
        a for a in answers.approved().filter(key__in=list(bank)) if a.keywords and bank[a.key]
    ]
    if not usable:
        return None
    answer = d.rng.choice(sorted(usable, key=lambda a: a.key))
    d.flags["asked"] = answer.key
    return d.text("ask_question", d.rng.choice(bank[answer.key]), advance=False)


def x_not_a_name(d):
    """A greeting, a question, a request or one word where the bot asks for the name
    (ตัวเลือกระบบ registration_name "full"): never a registration name."""
    if options.get(options.REGISTRATION_NAME) != "full" or not d.texts.get("not_name"):
        return None
    return d.text("not_a_name", d.rng.choice(d.texts["not_name"]), advance=False)


DETOURS = {name[2:]: fn for name, fn in list(globals().items()) if name.startswith("x_")}


# --- Checks right after a move -----------------------------------------------------------------


def _after_stale(d, step, after, reply):
    if after != step or "ปุ่มนี้" not in d.said(reply):
        d.fail("stale_button", f"ขั้น {step}: กดปุ่มเก่าแล้วไปขั้น {after}")
    d.passed("stale_button")


def _after_peek(d, step, after, reply):
    said = d.said(reply)
    if "ไม่พบเรื่อง" not in said or d.decoy.description in said:
        d.fail("isolation", f"พิมพ์เลขเรื่องของคนอื่นแล้วเห็นข้อมูล: {said[:60]}")
    d.passed("isolation")


def _after_mine(d, step, after, reply):
    if d.decoy.number in d.said(reply) or d.decoy.number in d.buttons(reply):
        d.fail("isolation", "เรื่องของฉันแสดงเรื่องของคนอื่น")
    d.passed("isolation")


def _after_double(d, step, after, reply):
    submitted = ConversationDraft.objects.filter(
        identity=d.identity_row(), status=D.SUBMITTED
    ).count()
    if submitted != 1 or "ยื่นเรื่องนี้แล้ว" not in d.said(reply):
        d.fail("idempotent", f"กดยืนยันซ้ำแล้วได้ {submitted} ร่างที่ยื่น: {d.said(reply)[:60]}")
    d.passed("idempotent")


def _after_wait(d, step, after, reply):
    if graph_step(step) in CHOICE_STEPS and after == step and not d.buttons(reply):
        d.fail("buttons", f"ขั้น {step}: หลังพิมพ์นอกเรื่องหรือส่งรูป บอทถามซ้ำแต่ไม่มีปุ่ม")


def _after_summary(d, step, after, reply):
    """The summary shows every answer, in the order the questions were asked."""
    said = d.said(reply)
    answers = [v.strip() for k, v in d.typed.items() if v and k != "needed_by"]
    places = [said.find(answer) for answer in answers]
    if -1 in places:
        missing = answers[places.index(-1)]
        d.fail("summary", f"สรุปเรื่องไม่มีคำตอบ '{missing[:30]}'")
    if places != sorted(places):
        d.fail("summary", "สรุปเรื่องเรียงคำตอบไม่ตรงกับลำดับที่ถาม")
    d.passed("summary")


def _after_question(d, step, after, reply):
    answer = answers.approved().filter(key=d.flags.get("asked")).first()
    said = d.said(reply)
    if answer is None or answer.text[:20] not in said:
        asked = answer.key if answer else "?"
        d.fail("answer", f"ขั้น {step}: ถาม '{asked}' แต่ไม่ได้คำตอบ: {said[:60]}")
    if after != step:
        d.fail("answer", f"ขั้น {step}: ถามแล้วบอทเปลี่ยนไปขั้น {after}")
    if graph_step(step) in CHOICE_STEPS and not d.buttons(reply):
        d.fail("answer", f"ขั้น {step}: ตอบแล้วไม่ถามคำถามเดิมพร้อมปุ่ม")
    d.passed("answer")


def _after_not_name(d, step, after, reply):
    districts = {district.name for district in d.world.districts}
    if after != step or districts & set(d.buttons(reply)):
        d.fail("registration", f"รับ '{d.turns[-2]['said']}' เป็นชื่อลงทะเบียน")
    d.passed("registration")


def _after_waiting(d, step, after, reply):
    """Nothing reaches the case unasked; a greeting hears what staff wait for, and anything
    else that may not be information is offered first."""
    said = d.said(reply)
    if ConversationMessage.objects.filter(case=d.info_case).exists():
        d.fail("info", f"'{d.turns[-2]['said']}' ถึงเรื่อง {d.info_case.number} โดยไม่ได้ถาม")
    if d.turns[-2]["intent"] == "greet_waiting":
        if d.info_case.number not in said or said.count(d.info_reason) != 1:
            d.fail("info", f"ทักทายแล้วบอทไม่บอก (หรือบอกซ้ำ) ว่าเจ้าหน้าที่รออะไร: {said[:60]}")
    elif d.turns[-2]["intent"] == "ask_waiting":
        if "ไม่ใช่ข้อมูลเพิ่ม" not in d.buttons(reply):
            d.fail("info", f"ถามคำถามแล้วบอทไม่ถามก่อนส่งถึงเจ้าหน้าที่: {said[:60]}")
    elif "ไม่ได้ส่งถึงเจ้าหน้าที่" not in said:
        d.fail("info", f"กด 'ไม่ใช่ข้อมูลเพิ่ม' แล้วบอทไม่บอกว่าไม่ได้ส่ง: {said[:60]}")
    d.passed("info")


def _after_info(d, step, after, reply):
    if after != "info_sent" or f"ส่งข้อมูลถึงเจ้าหน้าที่แล้ว (เรื่อง {d.info_case.number})" not in d.said(
        reply
    ):
        d.fail("info", f"ตอบข้อมูลแล้วไม่ถึงเรื่อง {d.info_case.number}: {d.said(reply)[:60]}")
    d.passed("info")


def _after_view(d, step, after, reply):
    if d.flags.get("viewed") not in d.said(reply):
        d.fail("staff", f"กดดูเรื่อง {d.flags.get('viewed')} แล้วไม่เห็นรายละเอียด")


def _after_action(d, step, after, reply):
    if "บันทึกแล้ว" not in d.said(reply):
        d.fail("staff", f"ทำขั้นใน LINE ไม่สำเร็จ: {d.said(reply)[:80]}")


def _after_mine_staff(d, step, after, reply):
    if not d.said(reply).startswith("เรื่องของฉัน"):
        d.fail("staff", "LINE ที่เป็นทั้งเจ้าหน้าที่และผู้แจ้งเปิดเรื่องของฉันไม่ได้")


AFTER = {
    "press_stale": _after_stale,
    "peek_case": _after_peek,
    "check_mine": _after_mine,
    "double_confirm": _after_double,
    "type_offtopic": _after_wait,
    "send_photo": _after_wait,
    "press_staff_case": _after_view,
    "press_push": _after_view,
    "press_staff_action": _after_action,
    "type_my_cases": _after_mine_staff,
    "ask_question": _after_question,
    "not_a_name": _after_not_name,
    "greet_waiting": _after_waiting,
    "ask_waiting": _after_waiting,
    "press_not_info": _after_waiting,
    "type_info": _after_info,
    "press_send_info": _after_info,
}


# --- The world the dialogues share --------------------------------------------------------------


PAD_PREFIX = "SIMPAD_"
PAD_UNITS = {"meter": ("เครื่อง", True), "transformer": ("เครื่อง", True), "other": ("ชิ้น", False)}


class World:
    """Reference data read once per run. Cases are made inside each dialogue's savepoint, so
    the run holds no lock between dialogues. A category with fewer items than the graph's
    `catalogue.at_least` gets synthetic ones for the run, so the bot's search step (used with a
    real-size catalogue) is walked too; they go with the rollback."""

    def __init__(self, graph=None):
        self.policy = policy.current_policy()
        if self.policy is None:
            raise ValidationError("ยังไม่มีกติกาที่ใช้งาน จำลองบทสนทนาไม่ได้")
        self.system = identity.system_user()
        self.districts = list(
            District.objects.filter(is_active=True).exclude(code=identity.UNKNOWN_DISTRICT)
        )
        if not self.districts:
            raise ValidationError("ยังไม่มีเขตที่ใช้งาน")
        self.padded = {}
        wanted = (graph or {}).get("catalogue", {}).get("at_least", {})
        for category, count in wanted.items():
            self._pad(category, count)
        line_menu_catalog.sync()
        self.policy = policy.current_policy()
        self.items = {
            # A shared item of a typed group is never offered in a list (typed_lines).
            c: list(
                EquipmentType.objects.filter(category=c, is_active=True, entry_group="").order_by(
                    "code"
                )
            )
            for c in EQUIPMENT
        }
        self.reporter = Requester.objects.create(
            display_name="ผู้แจ้งจำลองอีกคน", district=self.districts[0], approved_by=self.system
        )

    def _pad(self, category, count):
        have = EquipmentType.objects.filter(
            category=category, is_active=True, entry_group=""
        ).count()
        unit, serialized = PAD_UNITS[category]
        label = conversation.CATEGORY_LABELS[category]
        number = 0
        while have < count:
            number += 1
            code = f"{PAD_PREFIX}{category.upper()[:5]}_{number:02d}"
            if EquipmentType.objects.filter(code=code).exists():
                continue
            EquipmentType.objects.create(
                code=code,
                name=f"{label}จำลอง {number:02d}",
                category=category,
                unit=unit,
                is_serialized=serialized,
                is_synthetic=True,
            )
            have += 1
            self.padded[category] = self.padded.get(category, 0) + 1

    def decoy_case(self, number):
        """Someone else's case, for a person to try to see."""
        result = services.submit_case(
            Actor.for_requester(self.reporter),
            idempotency_key=f"dialogue-sim-decoy-{number:06d}",
            payload={"kind": "problem", "description": "เรื่องจำลองของผู้แจ้งอีกคน ห้ามคนอื่นเห็น"},
        )
        return Case.objects.get(pk=result.case_id)


# --- The run -------------------------------------------------------------------------------------


def _pick(rng, rows):
    return rng.choices(rows, weights=[r.get("weight", 1) for r in rows])[0]


def simulate(count=50, seed=1, scenarios=None, graph=None, only=None, writer=None):
    """Run `count` dialogues (or only dialogue number `only`) and roll everything back.
    Returns (records, report). Dialogue n always uses Random(f"{seed}:{n}"), so one dialogue
    can be replayed alone (exactly with the template writer; a model writes new words)."""
    if settings.APP_ENV not in {"local", "ci", "staging"}:
        raise ValidationError("บทสนทนาจำลองใช้ได้เฉพาะ local, ci และ staging")
    graph = graph or load_graph()
    pool = [s for s in graph["scenarios"] if not scenarios or s["id"] in scenarios]
    if not pool:
        raise ValidationError("ไม่พบสถานการณ์ที่เลือก")
    numbers = [only] if only is not None else range(1, count + 1)
    records = []
    with (
        override_settings(AI_ENABLED=False, OUTBOUND_ENABLED=False, STAFF_EMAIL_ENABLED=False),
        transaction.atomic(),
    ):
        world = World(graph)
        for number in numbers:
            rng = random.Random(f"{seed}:{number}")
            scenario, persona = _pick(rng, pool), _pick(rng, graph["personas"])
            dialogue = Dialogue(graph, world, scenario, persona, rng, number, writer)
            point = transaction.savepoint()
            try:
                dialogue.run()
            except Finding as finding:
                name, detail = finding.args
                dialogue.checks[name] = False
                dialogue.status, dialogue.note = "failed", f"{CHECKS.get(name, name)}: {detail}"
            except Skipped as skipped:
                dialogue.status, dialogue.note = "skipped", str(skipped)
            except Exception as exc:  # a crash inside the bot is a finding too
                dialogue.checks["reply"] = False
                dialogue.status = "failed"
                dialogue.note = f"เกิดข้อผิดพลาด {type(exc).__name__}: {exc}"[:500]
            if dialogue.status == "passed" and dialogue.note:
                dialogue.status = "warning"
            transaction.savepoint_rollback(point)
            records.append(dialogue.record())
        policy_code, padded = world.policy.doc.get("code", ""), world.padded
        transaction.set_rollback(True)
    return records, report(graph, records, seed, policy_code, padded)


def report(graph, records, seed, policy_code="", padded=None):
    edges = {(s, i) for s, moves in graph["steps"].items() for i, w in moves if w > 0}
    detours = set(graph.get("detours", {}))
    taken = {tuple(p) for r in records for p in r["path"]}
    used = {name for r in records for name in r["detours"]}
    checks, scenarios, repairs = {}, {}, {}
    for record in records:
        for name, ok in record["checks"].items():
            row = checks.setdefault(name, {"passed": 0, "failed": 0})
            row["passed" if ok else "failed"] += 1
        row = scenarios.setdefault(record["scenario"], {})
        row[record["status"]] = row.get(record["status"], 0) + 1
        for step, count in record["repairs"].items():
            repairs[step] = repairs.get(step, 0) + count
    statuses = [r["status"] for r in records]
    return {
        "graph": graph["code"],
        "policy": policy_code,
        "seed": seed,
        "dialogues": len(records),
        "passed": statuses.count("passed"),
        "warning": statuses.count("warning"),
        "failed": statuses.count("failed"),
        "skipped": statuses.count("skipped"),
        "checks": checks,
        "scenarios": scenarios,
        "coverage": {
            "edges": round(len(edges & taken) / len(edges), 3) if edges else 0,
            "detours": round(len(used & detours) / len(detours), 3) if detours else 0,
            "missed": sorted(f"{s}:{i}" for s, i in edges - taken),
        },
        "repairs": repairs,
        "redraws": sum(r["redraws"] for r in records),
        "writer": _writer_summary(records),
        "padded": padded or {},
        "turns": round(sum(len(r["path"]) for r in records) / len(records), 1) if records else 0,
        "failures": [
            {"id": r["id"], "scenario": r["scenario"], "note": r["note"]}
            for r in records
            if r["status"] in ("failed", "skipped")
        ],
    }


def _writer_summary(records):
    rewrites = [entry for r in records for entry in r.get("rewrites", [])]
    refused = {}
    for entry in rewrites:
        for reason in entry["refused"]:
            key = re.sub(r"\s*\(.*\)", "", re.sub(r"\d[\d, ]*", "N ", reason)).strip()
            refused[key] = refused.get(key, 0) + 1
    return {
        "name": records[0]["writer"] if records else "templates",
        "texts": len(rewrites),
        "by_model": sum(e["by"] != "template" for e in rewrites),
        "template_kept": sum(e["by"] == "template" for e in rewrites),
        "tries": sum(e["tries"] for e in rewrites),
        "refused": refused,
    }


def summary_lines(result):
    """A short Thai summary for the terminal."""
    coverage = result["coverage"]
    lines = [
        f"บทสนทนาจำลอง {result['dialogues']} บท (กราฟ {result['graph']}, "
        f"กติกา {result['policy']}, seed {result['seed']})",
        f"ผ่าน {result['passed']} · มีข้อสังเกต {result['warning']} · ไม่ผ่าน {result['failed']}"
        f" · ข้าม {result['skipped']}",
        f"ครอบคลุมทางในกราฟ {coverage['edges']:.0%} · ทางแยก {coverage['detours']:.0%}"
        f" · เฉลี่ย {result['turns']} ตา · ซ่อม {sum(result['repairs'].values())} ครั้ง"
        f" · สุ่มข้อความใหม่ {result['redraws']} ครั้ง",
    ]
    if result.get("padded"):
        added = ", ".join(
            f"{conversation.CATEGORY_LABELS[c]} {n} รายการ" for c, n in result["padded"].items()
        )
        lines.append(f"เพิ่มของจำลองชั่วคราวให้ถึงขั้นค้นหา: {added} (ย้อนกลับแล้ว)")
    writer = result.get("writer", {})
    if writer.get("texts"):
        lines.append(
            f"ผู้เขียนข้อความ {writer['name']}: ใช้ข้อความของโมเดล {writer['by_model']}"
            f"/{writer['texts']} ข้อความ (เรียกโมเดล {writer['tries']} ครั้ง"
            f", ใช้แม่แบบแทน {writer['template_kept']})"
        )
        for reason, count in sorted(writer["refused"].items(), key=lambda x: -x[1])[:5]:
            lines.append(f"  โมเดลเขียนไม่ผ่าน {count} ครั้ง: {reason}")
    for name, row in sorted(result["checks"].items()):
        if row["failed"]:
            lines.append(f"  ไม่ผ่าน {row['failed']} บท: {CHECKS.get(name, name)}")
    for failure in result["failures"][:10]:
        lines.append(f"  {failure['id']} ({failure['scenario']}): {failure['note']}")
    if coverage["missed"]:
        lines.append("ทางที่ยังไม่ได้เดิน: " + ", ".join(coverage["missed"][:12]))
    return lines


# --- Candidate AI evaluation rows ----------------------------------------------------------------


def triage_candidates(records, name):
    """Problem dialogues that passed, as rows in the triage evaluation format (ai_triage).
    การใช้ไฟฟ้า never goes to the AI (Q14) and suggestions name no equipment, so both are left
    out. A candidate set is for staff to review and edit; it is not frozen and gates nothing."""
    rows = []
    for record in records:
        labels = record["labels"]
        if (
            record["status"] not in ("passed", "warning")
            or labels["kind"] != K.PROBLEM
            or labels["topic"] in STORY_TOPICS
            or labels["actor"] == "staff"
            or any(line_["item"].startswith(PAD_PREFIX) for line_ in labels["lines"])
        ):
            continue  # padded items are not in any real catalogue
        fields = {k: v for k, v in labels["fields"].items() if k in ("description", "impact")}
        row = {
            "id": record["id"],
            "kind": "problem",
            "lines": [{"item_code": line_["item"]} for line_ in labels["lines"]],
            "fields": fields,
            "expected": {
                "hazard": labels["hazard"],
                "severe_impact": labels["severe_impact"] if "impact" in fields else None,
                "registry_only": False,
            },
            "tags": ["simulated", record["scenario"], record["persona"], record["writer"]],
        }
        if not labels["lines"]:
            topic = labels["topic"]
            row["expected"]["equipment"] = (
                [{"category": topic, "item": None}] if topic in EQUIPMENT else []
            )
        rows.append(row)
    return {
        "name": name,
        "frozen": None,
        "is_synthetic": True,
        "limits": (
            "ร่างจากบทสนทนาจำลองที่ใช้แม่แบบชุดเดียวกับตัวตรวจ ให้เจ้าหน้าที่ ฝอจ. ตรวจและแก้ก่อนใช้ "
            "ไม่ใช่ชุดวัดผลที่ล็อกแล้ว"
        ),
        "cases": rows,
    }
