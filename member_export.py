"""
Export des membres ou des expéditeurs d'un groupe / communauté Telegram.

Deux modes :
  - export_group_members()   : tous les membres inscrits (GetParticipants)
  - list_forum_topics()      : liste les topics d'une communauté
  - export_topic_senders()   : @users ayant posté dans un topic (GetReplies)
"""
import asyncio
import logging
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.channels import (
    GetParticipantsRequest,
    GetFullChannelRequest,
    GetForumTopicsRequest,
)
from telethon.tl.functions.messages import GetRepliesRequest
from telethon.tl.types import (
    ChannelParticipantsSearch,
    Channel,
    Chat,
)

logger = logging.getLogger(__name__)


def _is_group_or_community(entity) -> bool:
    if isinstance(entity, Channel):
        return bool(entity.megagroup)
    return isinstance(entity, Chat)


async def export_group_members(
    username: str,
    accounts_config: list[dict],
    progress_cb=None,
    limit: int = 0,
) -> dict:
    """
    Retourne :
    {
      "group": { username, title, member_count, is_community },
      "members": [ { telegram_id, username, first_name, last_name }, ... ],
      "total": int,
      "errors": [ ... ]
    }

    limit : 0 = tous les membres (pagination complète)
    progress_cb(str) : callback de progression pour le streaming SSE
    """

    def log(msg: str):
        logger.info(msg)
        if progress_cb:
            progress_cb(msg)

    errors_list: list[str] = []
    members: list[dict] = []

    acc = accounts_config[0]
    client = TelegramClient(
        StringSession(acc["session"]),
        int(acc["api_id"]),
        acc["api_hash"],
        request_retries=3,
        connection_retries=3,
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Session invalide")

    try:
        target = username.lstrip("@")
        log(f"Résolution de @{target}...")

        try:
            entity = await client.get_entity(target)
        except Exception as e:
            raise RuntimeError(f"@{target} introuvable : {e}")

        if not _is_group_or_community(entity):
            raise RuntimeError(f"@{target} est un canal broadcast, pas un groupe/communauté")

        # Infos du groupe
        title = getattr(entity, "title", target)
        member_count = getattr(entity, "participants_count", None)
        is_community = False

        try:
            full = await client(GetFullChannelRequest(entity))
            is_community = getattr(entity, "forum", False)
            member_count = full.full_chat.participants_count or member_count
        except Exception:
            pass

        group_type = "communauté" if is_community else "groupe"
        log(f"@{target} — {title} ({group_type}, ~{member_count} membres)")
        log("Récupération des membres en cours...")

        offset = 0
        batch_size = 200
        seen_ids: set[str] = set()

        while True:
            try:
                result = await client(GetParticipantsRequest(
                    channel=entity,
                    filter=ChannelParticipantsSearch(""),
                    offset=offset,
                    limit=batch_size,
                    hash=0,
                ))
            except errors.ChatAdminRequiredError:
                errors_list.append("Droits administrateur requis pour lister les membres")
                break
            except errors.ChannelPrivateError:
                errors_list.append("Groupe/communauté privé, accès impossible")
                break
            except errors.FloodWaitError as e:
                log(f"FloodWait {e.seconds}s, pause...")
                await asyncio.sleep(min(e.seconds, 120))
                continue
            except Exception as e:
                errors_list.append(str(e))
                break

            if not result.users:
                break

            new_in_batch = 0
            for user in result.users:
                uid = str(user.id)
                if uid in seen_ids:
                    continue
                seen_ids.add(uid)

                # On inclut tous les membres, même sans @username
                member = {
                    "telegram_id": uid,
                    "username": user.username or "",
                    "first_name": user.first_name or "",
                    "last_name": user.last_name or "",
                }
                members.append(member)
                new_in_batch += 1

            offset += len(result.users)

            public_count = sum(1 for m in members if m["username"])
            log(f"  {len(members)} membres récupérés ({public_count} avec @username)...")

            if len(result.users) < batch_size:
                break

            if limit and len(members) >= limit:
                members = members[:limit]
                break

            await asyncio.sleep(1.5)

        public_members = [m for m in members if m["username"]]
        log(f"Terminé : {len(members)} membres au total, {len(public_members)} avec @username public")

        return {
            "group": {
                "username": target,
                "title": title,
                "member_count": member_count,
                "is_community": is_community,
            },
            "members": members,
            "public_only": public_members,
            "total": len(members),
            "total_public": len(public_members),
            "errors": errors_list,
        }

    finally:
        await client.disconnect()


# ------------------------------------------------------------------ #
#  Liste les topics d'une communauté (forum)                          #
# ------------------------------------------------------------------ #

async def list_forum_topics(username: str, accounts_config: list[dict]) -> dict:
    """
    Retourne la liste des topics d'une communauté Telegram.
    {
      "group": { username, title, is_community },
      "topics": [ { id, title, top_msg_id, unread_count } ]
    }
    """
    acc = accounts_config[0]
    client = TelegramClient(
        StringSession(acc["session"]),
        int(acc["api_id"]),
        acc["api_hash"],
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Session invalide")

    try:
        target = username.lstrip("@")
        entity = await client.get_entity(target)

        is_community = getattr(entity, "forum", False)
        title = getattr(entity, "title", target)

        topics_out = []
        if is_community:
            offset_date = 0
            offset_id = 0
            offset_topic = 0

            while True:
                res = await client(GetForumTopicsRequest(
                    channel=entity,
                    q="",
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=100,
                ))
                for t in res.topics:
                    topics_out.append({
                        "id": t.id,
                        "title": t.title,
                        "top_msg_id": t.top_message,
                        "unread_count": getattr(t, "unread_count", 0),
                    })
                if len(res.topics) < 100:
                    break
                last = res.topics[-1]
                offset_topic = last.id
                offset_id = last.top_message
                await asyncio.sleep(1)

        return {
            "group": {"username": target, "title": title, "is_community": is_community},
            "topics": topics_out,
        }
    finally:
        await client.disconnect()


# ------------------------------------------------------------------ #
#  Export des expéditeurs uniques d'un topic                          #
# ------------------------------------------------------------------ #

async def export_topic_senders(
    username: str,
    topic_msg_id: int,
    topic_title: str,
    accounts_config: list[dict],
    progress_cb=None,
) -> dict:
    """
    Récupère tous les utilisateurs ayant posté dans un topic (thread) de forum.
    Utilise GetRepliesRequest pour paginer les messages du topic.

    Retourne :
    {
      "group": { username, title, is_community },
      "topic": { id, title },
      "members": [ { telegram_id, username, first_name, last_name } ],
      "total": int,
      "total_public": int,
      "errors": [ ... ]
    }
    """

    def log(msg: str):
        logger.info(msg)
        if progress_cb:
            progress_cb(msg)

    errors_list: list[str] = []
    seen_ids: set[str] = {}
    members: list[dict] = []
    seen_ids = set()

    acc = accounts_config[0]
    client = TelegramClient(
        StringSession(acc["session"]),
        int(acc["api_id"]),
        acc["api_hash"],
        request_retries=3,
        connection_retries=3,
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Session invalide")

    try:
        target = username.lstrip("@")
        entity = await client.get_entity(target)
        title = getattr(entity, "title", target)

        log(f"Topic « {topic_title} » (id={topic_msg_id}) dans @{target}...")
        log("Récupération des messages en cours...")

        offset_id = 0
        batch_size = 100

        while True:
            try:
                res = await client(GetRepliesRequest(
                    peer=entity,
                    msg_id=topic_msg_id,
                    offset_id=offset_id,
                    offset_date=0,
                    add_offset=0,
                    limit=batch_size,
                    max_id=0,
                    min_id=0,
                    hash=0,
                ))
            except errors.FloodWaitError as e:
                log(f"FloodWait {e.seconds}s, pause...")
                await asyncio.sleep(min(e.seconds, 120))
                continue
            except Exception as e:
                errors_list.append(str(e))
                break

            if not res.messages:
                break

            for msg in res.messages:
                sender = getattr(msg, "from_id", None)
                if sender is None:
                    continue
                uid = str(getattr(sender, "user_id", None) or getattr(sender, "channel_id", None) or "")
                if not uid or uid in seen_ids:
                    continue
                seen_ids.add(uid)

                # Résoudre le nom depuis res.users
                user_obj = next((u for u in res.users if str(u.id) == uid), None)
                members.append({
                    "telegram_id": uid,
                    "username": getattr(user_obj, "username", "") or "" if user_obj else "",
                    "first_name": getattr(user_obj, "first_name", "") or "" if user_obj else "",
                    "last_name": getattr(user_obj, "last_name", "") or "" if user_obj else "",
                })

            public_count = sum(1 for m in members if m["username"])
            log(f"  {len(res.messages)} messages traités — {len(members)} expéditeurs uniques ({public_count} avec @username)...")

            if len(res.messages) < batch_size:
                break

            offset_id = res.messages[-1].id
            await asyncio.sleep(1.5)

        public_members = [m for m in members if m["username"]]
        log(f"Terminé : {len(members)} expéditeurs uniques, {len(public_members)} avec @username")

        return {
            "group": {"username": target, "title": title, "is_community": True},
            "topic": {"id": topic_msg_id, "title": topic_title},
            "members": members,
            "public_only": public_members,
            "total": len(members),
            "total_public": len(public_members),
            "errors": errors_list,
        }

    finally:
        await client.disconnect()
