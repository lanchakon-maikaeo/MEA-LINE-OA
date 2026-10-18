"""Actor-bound MCP tools for LINE. Models prepare; a human confirmation executes.

The MCP session uses the SDK's memory transport inside the application. No public
endpoint, shared service token or model-supplied actor can acquire another user's scope.
"""

import json
import re
from datetime import timedelta

from asgiref.sync import async_to_sync, sync_to_async
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from mcp import types
from mcp.server import Server
from mcp.shared.memory import create_connected_server_and_client_session

from operations import identity, services, staff_line, work_orders, workflow
from operations.contracts import Actor
from operations.models import ChannelIdentity, Conversation, EquipmentType, Requester


def schema(**properties):
    return {"type": "object", "properties": properties, "additionalProperties": False}


TEXT = {"type": "string", "maxLength": 2000}
PAGE = {"type": "integer", "minimum": 1, "maximum": 10000}
TOOLS = [
    types.Tool(
        name="get_context",
        description="Current permitted LINE choices and work commands.",
        inputSchema=schema(),
    ),
    types.Tool(
        name="list_cases",
        description=(
            "List own cases, or staff cases within current division permissions. Open cases first."
        ),
        inputSchema=schema(page=PAGE, scope={"type": "string", "enum": ["own", "staff"]}),
    ),
    types.Tool(
        name="get_case",
        description="Open a case by its exact number within own or staff permissions.",
        inputSchema={
            **schema(number=TEXT, scope={"type": "string", "enum": ["own", "staff"]}),
            "required": ["number"],
        },
    ),
    types.Tool(
        name="admin_directory",
        description="Read administrator registration requests or LINE users.",
        inputSchema=schema(view={"type": "string", "enum": ["pending", "users"]}, page=PAGE),
    ),
    types.Tool(
        name="list_orders",
        description="List work orders visible to the linked staff account.",
        inputSchema=schema(page=PAGE),
    ),
    types.Tool(
        name="list_equipment",
        description="Search active equipment and its configured LINE group.",
        inputSchema=schema(search=TEXT, page=PAGE),
    ),
    types.Tool(
        name="prepare_action",
        description=(
            "Prepare a displayed choice or a supported business command for human "
            "confirmation of changes. Fixed service menus open without confirmation. "
            "Never executes it. New withdrawal/return/problem descriptions "
            "should use the existing extraction flow instead."
        ),
        inputSchema={**schema(command=TEXT), "required": ["command"]},
    ),
]


class Tools:
    def __init__(self, identity_id, source_text="", execute=None):
        self.identity_id = identity_id
        self.source_text = source_text
        self.execute = execute

    def actor(self):
        line = ChannelIdentity.objects.select_related("requester", "staff_user").get(
            pk=self.identity_id
        )
        staff = staff_line.staff_of(line)
        requester = line.requester if line.status == ChannelIdentity.Status.ACTIVE else None
        if requester is not None and requester.status != Requester.Status.ACTIVE:
            requester = None
        if not requester and not staff:
            raise PermissionDenied("บัญชีนี้ไม่มีสิทธิ์ใช้งาน")
        return line, requester, staff

    def call(self, name, args):
        line, requester, staff = self.actor()
        conv = Conversation.objects.get(identity=line)
        if name == "get_context":
            choices = list(conv.reply_choices.get("actions", {}))
            commands = ["ติดตามเรื่องของฉัน", "แจ้งปัญหา", "บริการ"] if requester else []
            if requester and not any(
                getattr(requester, flag, False)
                for flag in ("is_internal", "is_external", "is_anonymous")
            ):
                commands += ["เบิกอุปกรณ์", "ส่งคืนอุปกรณ์"]
            if staff:
                commands += ["งานของกอง", "เมนูเพิ่มเติม"]
                if work_orders.may_issue(staff):
                    commands += ["สั่งงาน <ผู้รับหรือกอง> <งาน> [วันที่]"]
                if identity.staff_scope(staff).is_admin:
                    commands += [
                        "แอดมิน",
                        "คำขอรออนุมัติ",
                        "ดูคำขอ <เลขคำขอ>",
                        "approve <เลขคำขอ>",
                        "reject <เลขคำขอ>",
                        "ดูผู้ใช้ <เลขผู้ใช้>",
                        "เพิ่มสิทธิ์ <เลขผู้ใช้> สมาชิกกอง|supervisor|supervisorฝ่าย <กองหรือ->",
                        "ถอนสิทธิ์ <เลขสิทธิ์>",
                    ]
            return {"choices": choices, "commands": commands, "step": conv.prompt}
        if name == "prepare_action":
            command = " ".join(args["command"].split())
            if not self.allowed(command, conv, requester, staff):
                raise PermissionDenied("เลือกจากเมนูที่แสดง หรือระบุคำสั่งงานให้ชัดเจน")
            return {"kind": "proposal", "command": command}
        if name == "execute_confirmed_action" and self.execute is not None:
            return self.execute()
        if name == "admin_directory":
            if not staff or not identity.staff_scope(staff).is_admin:
                raise PermissionDenied("เฉพาะแอดมิน")
            return {
                "kind": "admin",
                "view": args.get("view", "pending"),
                "page": args.get("page", 1),
            }
        if name in {"list_cases", "get_case"}:
            scope = args.get("scope", "own")
            if scope == "staff":
                if not staff:
                    raise PermissionDenied("ไม่มีสิทธิ์งานเจ้าหน้าที่")
                actor = Actor.staff(staff)
            else:
                if not requester:
                    raise PermissionDenied("ไม่มีบัญชีผู้แจ้ง")
                actor = Actor.for_requester(requester)
            rows = services.visible_cases(actor).order_by("-submitted_at", "-pk")
            # Display only identifiers and states; customer details never enter the model.
            if name == "get_case":
                row = rows.filter(number=args["number"].upper()).first()
                if row is None:
                    raise PermissionDenied("ไม่พบเรื่องนี้ในสิทธิ์ของคุณ")
                return {"kind": "case", "scope": scope, "number": row.number, "id": str(row.pk)}
            rows = rows.filter(status__in=workflow.OPEN)
            page = Paginator(rows, 5).get_page(args.get("page", 1))
            return {
                "kind": "cases",
                "scope": scope,
                "page": page.number,
                "pages": page.paginator.num_pages,
                "total": page.paginator.count,
                "rows": [
                    {"number": row.number, "id": str(row.pk), "status": row.get_status_display()}
                    for row in page
                ],
            }
        if name == "list_orders":
            if not staff:
                raise PermissionDenied("ไม่มีสิทธิ์งานเจ้าหน้าที่")
            page = Paginator(work_orders.visible(staff).order_by("-created_at"), 5).get_page(
                args.get("page", 1)
            )
            return {
                "kind": "orders",
                "page": page.number,
                "pages": page.paginator.num_pages,
                "total": page.paginator.count,
                "rows": [
                    {"number": row.number, "id": str(row.pk), "status": row.get_status_display()}
                    for row in page
                ],
            }
        if name == "list_equipment":
            from operations import conversation

            search = args.get("search", "").strip()
            groups = [
                group
                for group, label in conversation.METER_GROUPS.items()
                if re.search(
                    rf"(?<![A-Za-z]){re.escape(label)}(?![A-Za-z])", self.source_text, re.I
                )
            ]
            group = (
                groups[0]
                if len(groups) == 1
                else next(
                    (
                        key
                        for key, label in conversation.METER_GROUPS.items()
                        if search.casefold() in {key, label.casefold()}
                    ),
                    None,
                )
            )
            if group:
                rows = conversation._items("other" if group == "other" else "meter", group)
                search = conversation.METER_GROUPS[group]
            else:
                rows = EquipmentType.objects.filter(is_active=True, name__icontains=search)
            rows = rows.order_by("code")
            page = Paginator(rows, 5).get_page(args.get("page", 1))
            return {
                "kind": "equipment",
                "search": search,
                "page": page.number,
                "pages": page.paginator.num_pages,
                "total": page.paginator.count,
                "rows": [
                    {"code": row.code, "name": row.name, "group": row.line_group} for row in page
                ],
            }
        raise PermissionDenied("ไม่พบเครื่องมือนี้")

    def allowed(self, command, conv, requester, staff):
        from operations import admin_line, conversation, staff_orders_chat

        if not command or len(command) > 2000:
            return False
        if conv.prompt.startswith(("reg_", "staff_otp", "staff_emp", "staff_check")):
            return False
        if command in {"เชื่อมบัญชีเจ้าหน้าที่", "เลิกเชื่อมบัญชีเจ้าหน้าที่", "ลงทะเบียนพนักงาน", "ยืนยันพนักงาน"}:
            return False
        if command in conv.reply_choices.get("actions", {}):
            return True
        if requester and command in conversation.MENU_WORDS:
            if command in {"เบิกอุปกรณ์", "ส่งคืนอุปกรณ์"} and any(
                getattr(requester, flag, False)
                for flag in ("is_internal", "is_external", "is_anonymous")
            ):
                return False
            return True
        if staff and command in {
            "งานของกอง",
            "เมนูเพิ่มเติม",
            "เมนูตามสิทธิ์",
            "บริการ",
            "ติดตามเรื่องของฉัน",
            "แจ้งปัญหา",
        }:
            return True
        if staff and work_orders.may_issue(staff) and staff_orders_chat.START.match(command):
            return True
        if staff and identity.staff_scope(staff).is_admin:
            if command in {"แอดมิน", "คำขอรออนุมัติ", "รับแจ้งสมัคร เปิด", "รับแจ้งสมัคร ปิด"}:
                return True
            if any(
                pattern.fullmatch(command)
                for pattern in (
                    admin_line.COMMAND,
                    admin_line.ROLE_COMMAND,
                    admin_line.REVOKE_COMMAND,
                    admin_line.USER_COMMAND,
                )
            ):
                return True
        # A free-text form answer must remain exactly what this user wrote.
        return command == " ".join(self.source_text.split()) and bool(
            conv.prompt or conv.active_draft_id
        )

    def server(self):
        server = Server("faoj-line-work")
        offered = list(TOOLS)
        if self.execute is not None:
            offered = [
                types.Tool(
                    name="execute_confirmed_action",
                    description="Execute the server-verified human confirmation.",
                    inputSchema=schema(),
                )
            ]

        @server.list_tools()
        async def list_tools():
            return offered

        @server.call_tool()
        async def call_tool(name, arguments):
            if name not in {tool.name for tool in offered}:
                raise PermissionDenied("เครื่องมือนี้ไม่ได้เปิดให้เรียก")
            result = await sync_to_async(self.call, thread_sensitive=True)(name, arguments)
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        return server

    async def exchange(self, name, arguments):
        async with create_connected_server_and_client_session(
            self.server(), read_timeout_seconds=timedelta(seconds=10)
        ) as client:
            tools = await client.list_tools()
            if name not in {tool.name for tool in tools.tools}:
                return None
            return await client.call_tool(name, arguments)

    def invoke(self, name, arguments=None):
        try:
            result = async_to_sync(self.exchange)(name, arguments or {})
        except (ExceptionGroup, TimeoutError, OSError) as exc:
            raise PermissionDenied("MCP ไม่พร้อมใช้งาน") from exc
        if result is None or result.isError:
            raise PermissionDenied("ไม่สามารถใช้เครื่องมือนี้ตามสิทธิ์ปัจจุบัน")
        return json.loads(result.content[0].text)
