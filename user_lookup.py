"""
Recherche directe des groupes publics d'un utilisateur Telegram.

Stratégie combinée :
  1. searchGlobal(@username)  → groupes publics où il a posté / est mentionné
  2. messages.search from_id  → ses messages dans les dialogues accessibles
  3. Bio parsing              → liens t.me dans sa description
  4. getParticipant           → vérification réelle de l'appartenance
"""
import asyncio
import re
import logging
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SearchGlobalRequest, SearchRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import (
    InputMessagesFilterEmpty,
    InputPeerEmpty,
    Channel,
    Chat,
    ChannelParticipantBanned,
    ChannelParticipantLeft,
)


def _is_group(entity) -> bool:
    if isinstance(entity, Channel):
        return bool(entity.megagroup)
    return isinstance(entity, Chat)

logger = logging.getLogger(__name__)
TGME_RE = re.compile(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})", re.IGNORECASE)


async def lookup_user_groups(
    username: str,
    accounts_config: list[dict],
    progress_cb=None,
) -> dict:
    """
    Retourne un dict :
      {
        "user": { telegram_id, username, first_name, last_name, bio },
        "groups": [ { username, title, member_count, source } ],
        "errors": [ ... ]
      }

    progress_cb(str) est appelé pour chaque étape (pour le streaming vers le front).
    """

    def log(msg: str):
        logger.info(msg)
        if progress_cb:
            progress_cb(msg)

    results: dict[str, dict] = {}  # username → group info
    errors: list[str] = []

    # -- Connexion --
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
        # -- Résolution de l'utilisateur --
        log(f"Résolution de @{username}...")
        try:
            user_entity = await client.get_entity(username)
        except Exception as e:
            raise RuntimeError(f"Utilisateur @{username} introuvable : {e}")

        user_info = {
            "telegram_id": str(user_entity.id),
            "username": getattr(user_entity, "username", username),
            "first_name": getattr(user_entity, "first_name", "") or "",
            "last_name": getattr(user_entity, "last_name", "") or "",
            "bio": "",
        }

        # ---------------------------------------------------------------- #
        # Étape 1 : Bio                                                     #
        # ---------------------------------------------------------------- #
        log("Lecture de la bio...")
        try:
            full = await client(GetFullUserRequest(user_entity))
            bio = (full.full_user.about or "").strip()
            user_info["bio"] = bio
            for link in TGME_RE.findall(bio):
                if link not in results:
                    results[link] = {"username": link, "title": None,
                                     "member_count": None, "source": "bio"}
                    log(f"  → @{link} (bio)")
        except Exception as e:
            errors.append(f"bio: {e}")

        await asyncio.sleep(1.5)

        # ---------------------------------------------------------------- #
        # Étape 2 : searchGlobal(@username)                                #
        # Trouve les groupes PUBLICS où le mec a posté — Telegram indexe   #
        # les messages publics de tous les utilisateurs.                    #
        # ---------------------------------------------------------------- #
        log(f"searchGlobal pour @{username}...")
        offset_rate = 0
        offset_id = 0
        from telethon.tl.types import InputPeerEmpty as IPE

        for _ in range(5):  # 5 pages max
            try:
                res = await client(SearchGlobalRequest(
                    q=f"@{username}",
                    filter=InputMessagesFilterEmpty(),
                    min_date=0,
                    max_date=0,
                    offset_rate=offset_rate,
                    offset_peer=IPE(),
                    offset_id=offset_id,
                    limit=100,
                ))
            except errors.FloodWaitError as e:
                log(f"FloodWait {e.seconds}s, attente...")
                await asyncio.sleep(min(e.seconds, 60))
                break
            except Exception as e:
                errors.append(f"searchGlobal: {e}")
                break

            for chat in res.chats:
                uname = getattr(chat, "username", None)
                if uname and uname not in results and _is_group(chat):
                    title = getattr(chat, "title", uname)
                    cnt = getattr(chat, "participants_count", None)
                    results[uname] = {
                        "username": uname,
                        "title": title,
                        "member_count": cnt,
                        "source": "searchGlobal",
                    }
                    log(f"  → @{uname} — {title} (searchGlobal)")

            if not getattr(res, "next_rate", None) or len(res.messages) < 100:
                break
            offset_rate = res.next_rate
            offset_id = res.messages[-1].id if res.messages else 0
            await asyncio.sleep(2)

        # ---------------------------------------------------------------- #
        # Étape 3 : messages.search from_id (dialogues accessibles)        #
        # ---------------------------------------------------------------- #
        log("messages.search from_id...")
        try:
            res2 = await client(SearchRequest(
                peer=InputPeerEmpty(),
                q="",
                from_id=user_entity,
                top_msg_id=None,
                filter=InputMessagesFilterEmpty(),
                min_date=0,
                max_date=0,
                offset_id=0,
                add_offset=0,
                limit=100,
                max_id=0,
                min_id=0,
                hash=0,
            ))
            for chat in res2.chats:
                uname = getattr(chat, "username", None)
                if uname and uname not in results and _is_group(chat):
                    results[uname] = {
                        "username": uname,
                        "title": getattr(chat, "title", uname),
                        "member_count": getattr(chat, "participants_count", None),
                        "source": "messages.search",
                    }
                    log(f"  → @{uname} (messages.search)")
        except Exception as e:
            errors.append(f"messages.search: {e}")

        await asyncio.sleep(1.5)

        # ---------------------------------------------------------------- #
        # Étape 4 : vérification réelle de l'appartenance                  #
        # On appelle getParticipant pour chaque candidat trouvé.           #
        # ---------------------------------------------------------------- #
        log(f"Vérification de l'appartenance ({len(results)} candidats)...")
        verified: list[dict] = []

        for uname, info in list(results.items()):
            await asyncio.sleep(1)
            try:
                channel = await client.get_entity(uname)
                # Enrichissement des infos
                if info["title"] is None:
                    info["title"] = getattr(channel, "title", uname)
                if info["member_count"] is None:
                    info["member_count"] = getattr(channel, "participants_count", None)

                part = await client(GetParticipantRequest(channel, user_entity))
                p = part.participant
                if isinstance(p, (ChannelParticipantBanned, ChannelParticipantLeft)):
                    log(f"  ✗ @{uname} — banni/a quitté")
                    continue
                info["verified"] = True
                verified.append(info)
                log(f"  ✓ @{uname} — {info['title']} (membre confirmé)")

            except errors.UserNotParticipantError:
                log(f"  ✗ @{uname} — pas membre")
            except errors.FloodWaitError as e:
                log(f"  FloodWait {e.seconds}s, pause...")
                await asyncio.sleep(min(e.seconds, 60))
                # On garde quand même le candidat non vérifié
                info["verified"] = False
                verified.append(info)
            except Exception:
                # Groupe privé ou inaccessible → on garde le candidat
                info["verified"] = False
                verified.append(info)

        # Trie : membres confirmés en premier, puis par taille
        verified.sort(
            key=lambda x: (not x.get("verified", False), -(x.get("member_count") or 0))
        )

        log(f"Terminé : {len(verified)} groupe(s) trouvé(s)")
        return {"user": user_info, "groups": verified, "errors": errors}

    finally:
        await client.disconnect()
