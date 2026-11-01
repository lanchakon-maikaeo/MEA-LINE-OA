"""Automatic publication with durable retry progress and verified per-user links."""

import hashlib
import json
import logging
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection

from operations import identity, line_api, line_menu
from operations.models import ChannelIdentity, LineMenuRelease

logger = logging.getLogger(__name__)
LOCK = 1650250051


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def pictures():
    root = Path(settings.BASE_DIR) / "operations/line_assets"
    manifest = json.loads((root / "menu-manifest.json").read_text())
    if manifest["drawing_sha256"] != _hash(line_menu.drawing()):
        raise ValidationError("ภาพเมนูไม่ตรงรุ่นโปรแกรม ต้อง build_line_menu_images ก่อนปล่อยรุ่น")
    return {page: (root / f"{page}.jpg").read_bytes() for page in line_menu.PAGES}


def audience():
    result = {}
    rows = ChannelIdentity.objects.filter(is_following=True).select_related(
        "staff_user", "requester__employee"
    )
    for row in rows:
        result[row.external_id] = line_menu.page_for_identity(row)
    return result


def _default():
    try:
        return line_api.call("/v2/bot/user/all/richmenu").get("richMenuId", "")
    except line_api.LineApiError as exc:
        if exc.status == 404:
            return ""
        raise


def _linked_menu(user_id):
    try:
        return line_api.call(f"/v2/bot/user/{user_id}/richmenu").get("richMenuId")
    except line_api.LineApiError as exc:
        if exc.status != 404:
            raise
        return None


def _is_friend(user_id):
    """LINE accepts a link for an account that blocked or left the OA but keeps no menu for it."""
    try:
        line_api.call(f"/v2/bot/profile/{user_id}")
    except line_api.LineApiError as exc:
        if exc.status != 404:
            raise
        return False
    return True


def _allowed_menu_ids(release, page):
    role = page.partition("-")[0]
    tabs = line_menu.ROLE_TABS.get(role)
    pages = [f"{role}-{tab}" for tab in tabs] if tabs else [page]
    return {release.menus[p] for p in pages if p in release.menus}


def _links_match(release, people):
    for user_id, page in people.items():
        if _linked_menu(user_id) not in _allowed_menu_ids(release, page):
            return False
    return True


def publish(actor, images, reason):
    if actor is not None:
        identity.require_admin(actor)
    actor = actor or identity.system_user()
    reason = identity._require_reason(reason)
    if not settings.LINE_CHANNEL_ACCESS_TOKEN:
        raise ValidationError("ยังไม่ได้ตั้งค่า LINE")
    if set(images) != set(line_menu.PAGES):
        raise ValidationError("ต้องมีภาพครบทุกหน้า")
    kinds = {p: line_menu._picture(blob) for p, blob in images.items()}
    menus = {p: line_menu.rich_menu(p) for p in images}
    digest = _hash(
        {
            "menus": menus,
            "images": {p: hashlib.sha256(b).hexdigest() for p, b in images.items()},
            "channel": hashlib.sha256(settings.LINE_CHANNEL_SECRET.encode()).hexdigest(),
        }
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK])
        if not cursor.fetchone()[0]:
            raise ValidationError("มีการอัปเดตเมนูอยู่แล้ว")
    try:
        release, _ = LineMenuRelease.objects.get_or_create(digest=digest)
        people = audience()
        audience_hash = _hash(people)
        if (
            release.status == "complete"
            and release.audience_hash == audience_hash
            and _links_match(release, people)
        ):
            return release.menus["user"]
        try:
            # Preflight before changing any live default or alias; save the old references.
            aliases = {
                a["richMenuAliasId"]: a["richMenuId"]
                for a in line_api.call("/v2/bot/richmenu/alias/list").get("aliases", [])
            }
            if not release.previous:
                old = line_api.call("/v2/bot/richmenu/list").get("richmenus", [])
                release.previous = {
                    "default": _default(),
                    "aliases": aliases,
                    "menus": [
                        m["richMenuId"] for m in old if str(m.get("name", "")).startswith("faoj-")
                    ],
                }
                release.save()
            release.status, release.error = "publishing", ""
            release.save()
            for page, spec in menus.items():
                if page not in release.menus:
                    release.menus[page] = line_api.call("/v2/bot/richmenu", spec)["richMenuId"]
                    release.save()
                if page not in release.uploaded:
                    line_api.call(
                        f"/v2/bot/richmenu/{release.menus[page]}/content",
                        images[page],
                        base=settings.LINE_DATA_API_BASE,
                        content_type=kinds[page],
                    )
                    release.uploaded.append(page)
                    release.save()
            for page in images:
                if page == "user":
                    continue
                name = line_menu.alias(page)
                if aliases.get(name) == release.menus[page]:
                    continue
                if name in aliases:
                    line_api.call(
                        f"/v2/bot/richmenu/alias/{name}", {"richMenuId": release.menus[page]}
                    )
                else:
                    line_api.call(
                        "/v2/bot/richmenu/alias",
                        {"richMenuAliasId": name, "richMenuId": release.menus[page]},
                    )
            line_api.call(f"/v2/bot/user/all/richmenu/{release.menus['user']}", method="POST")
            unverified = 0
            for user_id, page in people.items():
                target = f"/v2/bot/user/{user_id}/richmenu"
                allowed = _allowed_menu_ids(release, page)
                linked = _linked_menu(user_id)
                if linked not in allowed:
                    line_api.call(f"{target}/{release.menus[page]}", method="POST")
                    linked = _linked_menu(user_id)
                if linked not in allowed:
                    if _is_friend(user_id):
                        unverified += 1
                    else:  # its unfollow never reached us; it must not hold every release back
                        ChannelIdentity.objects.filter(channel="line", external_id=user_id).update(
                            is_following=False
                        )
            if unverified:
                raise ValidationError(f"ยังตรวจยืนยันเมนูไม่ได้ {unverified} บัญชี เก็บเมนูเก่าไว้และจะลองใหม่")
            for old in release.previous.get("menus", []):
                if old in release.menus.values():
                    continue
                try:
                    line_api.call(f"/v2/bot/richmenu/{old}", method="DELETE")
                except line_api.LineApiError as exc:
                    if exc.status != 404:
                        raise
            release.status, release.audience_hash, release.error = "complete", audience_hash, ""
            release.save()
            identity.audit(actor, "line_menu.published", release, reason, rich_menus=release.menus)
            return release.menus["user"]
        except (line_api.LineApiError, OSError, ValidationError) as exc:
            release.status, release.error = "partial", str(exc)[:200]
            release.save()
            identity.audit(actor, "line_menu.publish_failed", release, reason, error=release.error)
            raise ValidationError(f"อัปเดตเมนูยังไม่ครบ จะลองต่อจากสถานะเดิม: {exc}") from exc
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK])


def sync():
    from operations import line_menu_catalog

    if not settings.LINE_CHANNEL_ACCESS_TOKEN or not settings.OUTBOUND_ENABLED:
        return
    line_menu_catalog.sync()
    publish(None, pictures(), "อัปเดตเมนูตามรุ่นโปรแกรมและสิทธิ์ LINE")


def run_loop():
    import time

    while True:
        try:
            sync()
        except Exception:
            logger.exception("Automatic LINE menu update incomplete; retry in five minutes")
        finally:
            connection.close()
        time.sleep(300)
