"""
Export de tous les membres @public d'un groupe ou d'une communauté Telegram.

Fonctionne avec :
  - Supergroupes classiques
  - Communautés (forum=True, topics)
  - Groupes basiques (Chat)

Retourne la liste des membres ayant un @username public.
"""
import asyncio
import logging
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetParticipantsRequest, GetFullChannelRequest
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
