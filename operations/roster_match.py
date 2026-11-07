"""สังกัดของบัญชีเว็บขึ้นเองจากทะเบียนพนักงาน (ADR 0050 Q1, Q9).

A web account an admin made has no รหัสพนักงาน, so its สังกัด stayed N/A. The system now finds
its roster row by itself: first by the account's LINE when that LINE passed the employee check
or an admin tied it, then by the account's email equal to the email of exactly one roster row.
Never by name: the roster keeps first names only and they repeat. When the รหัสพนักงาน already
sits in an account the system made from LINE (`line-employee-…`), the สังกัด follows the roster
at once but the two accounts are not merged: merging moves LINE, roles and cases and cannot be
undone, so an admin presses รวมบัญชี on the account page.
"""

from dataclasses import dataclass

from django.db import transaction

from operations import identity, roster, units
from operations.models import ChannelIdentity, Employee, Requester, StaffProfile

PROVEN = (Requester.EmployeeLink.VERIFIED, Requester.EmployeeLink.ADMIN)
HOW = {"line": "LINE ที่ยืนยันตัวตนแล้ว", "email": "อีเมลที่ตรงกับทะเบียน"}


@dataclass
class Match:
    employee: Employee | None = None
    how: str = ""  # "id" (already tied), "line" or "email"
    reason: str = ""  # why there is no match, for the account page
    merge_from: object = None  # the `line-employee-…` account holding the same ID


def _skipped(user):
    return user.username.startswith(identity.LINE_ACCOUNT) or (
        user.username == identity.SYSTEM_USERNAME
    )


def find(user):
    """The roster row of this account, or why there is none."""
    profile = StaffProfile.objects.filter(user=user).first()
    if profile is None:
        return Match(reason="บัญชีนี้ไม่มีข้อมูลเจ้าหน้าที่")
    if profile.employee_id:
        employee = Employee.objects.filter(employee_id=profile.employee_id).first()
        if employee is None:
            return Match(reason=f"ไม่พบรหัสพนักงาน {profile.employee_id} ในทะเบียน")
        return Match(employee, "id")
    line = (
        ChannelIdentity.objects.filter(staff_user=user)
        .select_related("requester__employee")
        .first()
    )
    requester = line.requester if line else None
    if (
        requester is not None
        and requester.employee_id
        and requester.employee_link in PROVEN
        and requester.employee.is_active
    ):
        return Match(requester.employee, "line")
    if not user.email:
        return Match(reason="บัญชีนี้ไม่มีอีเมล และยังไม่ได้เชื่อม LINE ที่ยืนยันตัวตนกับทะเบียนแล้ว")
    rows = list(
        Employee.objects.filter(email_hash=roster.email_digest(user.email), is_active=True)[:2]
    )
    if len(rows) > 1:
        return Match(reason="อีเมลของบัญชีตรงกับพนักงานมากกว่าหนึ่งคนในทะเบียน")
    if not rows:
        return Match(reason="อีเมลของบัญชีไม่ตรงกับใครในทะเบียนพนักงาน")
    return Match(rows[0], "email")


def status(user):
    """What the account page shows: the match, the merge it waits for, or why there is none."""
    if _skipped(user):
        return Match()
    match = find(user)
    if match.employee is None or match.how == "id":
        return match
    holders = list(
        StaffProfile.objects.filter(employee_id=match.employee.employee_id)
        .exclude(user=user)
        .select_related("user")
    )
    if any(not _skipped(p.user) for p in holders):
        return Match(reason=f"รหัสพนักงาน {match.employee.employee_id} อยู่ในบัญชีเว็บอื่นแล้ว")
    if holders:
        match.merge_from = holders[0].user
    return match


@transaction.atomic
def link(user, actor=None):
    """Tie the account to its roster row when the ID is free, else only take the roster's
    สังกัด. Returns the Match the account page shows."""
    match = status(user)
    if match.employee is None or match.how == "id":
        return match
    employee = match.employee
    units.bind_from_roster(user, employee)
    if match.merge_from is not None:
        return match
    profile = StaffProfile.objects.select_for_update().get(user=user)
    profile.employee_id = employee.employee_id
    profile.save(update_fields=["employee_id"])
    identity.audit(
        actor,
        "staff.employee_id_matched",
        user,
        f"จับคู่จาก{HOW[match.how]}",
        actor_label="" if actor else "system",
        value=employee.employee_id,
        method=match.how,
    )
    return match


def link_all(actor=None):
    """Every active web account not yet tied (after each roster import, and once on deploy)."""
    profiles = (
        StaffProfile.objects.filter(user__is_active=True, employee_id="")
        .exclude(user__username__startswith=identity.LINE_ACCOUNT)
        .exclude(user__username=identity.SYSTEM_USERNAME)
        .select_related("user")
    )
    return sum(bool(link(p.user, actor).employee) for p in profiles)


def link_requester(requester, actor=None):
    """A requester was just tied to the roster: the web account on their LINE follows."""
    for line in ChannelIdentity.objects.filter(requester=requester, staff_user__isnull=False):
        link(line.staff_user, actor)
