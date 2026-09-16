import asyncio
import random
import os
import re
import logging
from datetime import datetime, timedelta
from collections import deque
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SearchRequest
from telethon.tl.functions.channels import (
    GetParticipantRequest,
    GetParticipantsRequest,
    GetSimilarChannelsRequest,
)
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    ChannelParticipantsSearch,
    InputMessagesFilterEmpty,
    InputPeerEmpty,
    ChannelParticipantBanned,
    ChannelParticipantLeft,
    Channel,
    Chat,
)
from telethon.tl.functions.contacts import SearchRequest as ContactsSearchRequest
from database import Session, Group, Member, GroupMember, ScrapeJob
from categories import classify_group, get_category_seeds

TGME_RE = re.compile(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})", re.IGNORECASE)


def is_group(entity) -> bool:
    """True = supergroupe ou groupe basique. False = canal broadcast (on ignore)."""
    if isinstance(entity, Channel):
        return bool(entity.megagroup)
    return isinstance(entity, Chat)

logger = logging.getLogger(__name__)


class AccountManager:
    def __init__(self, accounts_config: list[dict]):
        self.accounts = accounts_config
        self.current_idx = 0
        self.clients: dict[int, TelegramClient] = {}  # keyed by index
        self.ban_until: dict[int, datetime] = {}

    async def get_client(self) -> tuple[TelegramClient, int]:
        for _ in range(len(self.accounts)):
            idx = self.current_idx % len(self.accounts)
            self.current_idx += 1

            if idx in self.ban_until and self.ban_until[idx] > datetime.utcnow():
                logger.warning(f"Compte {idx+1} banni jusqu'à {self.ban_until[idx]}, rotation...")
                continue

            if idx not in self.clients:
                acc = self.accounts[idx]
                client = TelegramClient(
                    StringSession(acc["session"]),
                    int(acc["api_id"]),
                    acc["api_hash"],
                    request_retries=3,
                    connection_retries=3,
                )
                await client.connect()
                if not await client.is_user_authorized():
                    logger.error(f"Compte {idx+1} non autorisé, session invalide.")
                    continue
                self.clients[idx] = client

            return self.clients[idx], idx

        raise RuntimeError("Aucun compte disponible")

    async def mark_flood(self, idx: int, seconds: int):
        self.ban_until[idx] = datetime.utcnow() + timedelta(seconds=seconds + 30)
        if idx in self.clients:
            await self.clients[idx].disconnect()
            del self.clients[idx]

    async def disconnect_all(self):
        for client in self.clients.values():
            try:
                await client.disconnect()
            except Exception:
                pass
        self.clients.clear()


class TelegramScraper:
    def __init__(self, accounts_config: list[dict], job_id: int):
        self.am = AccountManager(accounts_config)
        self.job_id = job_id
        self.min_delay = float(os.getenv("MIN_DELAY", 2))
        self.max_delay = float(os.getenv("MAX_DELAY", 6))
        self.max_members = int(os.getenv("MAX_MEMBERS_PER_GROUP", 500))
        self.max_depth = int(os.getenv("MAX_DEPTH", 3))
        self.req_before_pause = int(os.getenv("REQUESTS_BEFORE_PAUSE", 30))
        self.long_pause_min = int(os.getenv("LONG_PAUSE_MIN", 60))
        self.long_pause_max = int(os.getenv("LONG_PAUSE_MAX", 180))
        self.req_counter = 0
        self._stop = False

    def stop(self):
        self._stop = True

    def _log(self, msg: str):
        db = Session()
        try:
            job = db.query(ScrapeJob).get(self.job_id)
            if job:
                ts = datetime.utcnow().strftime("%H:%M:%S")
                job.log = (job.log or "") + f"[{ts}] {msg}\n"
                db.commit()
            logger.info(msg)
        finally:
            db.close()

    def _update_job(self, **kwargs):
        db = Session()
        try:
            job = db.query(ScrapeJob).get(self.job_id)
            if job:
                for k, v in kwargs.items():
                    setattr(job, k, v)
                db.commit()
        finally:
            db.close()

    async def _sleep(self):
        await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))
        self.req_counter += 1
        if self.req_counter >= self.req_before_pause:
            pause = random.uniform(self.long_pause_min, self.long_pause_max)
            self._log(f"Pause anti-ban: {pause:.0f}s...")
            await asyncio.sleep(pause)
            self.req_counter = 0

    async def _with_flood_retry(self, coro_fn, acc_idx: int, retries: int = 3):
        """Exécute une coroutine et gère le FloodWait avec rotation."""
        for attempt in range(retries):
            try:
                return await coro_fn()
            except errors.FloodWaitError as e:
                self._log(f"FloodWait {e.seconds}s (compte {acc_idx+1}), rotation...")
                await self.am.mark_flood(acc_idx, e.seconds)
                wait = min(e.seconds, 90)
                await asyncio.sleep(wait)
                if attempt < retries - 1:
                    raise StopIteration  # signal pour rechoisir le client
                raise
        return None

    # ------------------------------------------------------------------ #
    #  Scraping des membres d'un groupe                                    #
    # ------------------------------------------------------------------ #

    async def scrape_group_members(self, username: str, depth: int = 0) -> list[dict]:
        if self._stop:
            return []

        db = Session()
        try:
            group = db.query(Group).filter_by(username=username).first()
            if not group:
                group = Group(username=username, depth=depth, status="scraping")
                db.add(group)
                db.commit()
            elif group.status == "done":
                self._log(f"@{username} déjà scrappé, skip")
                return []
            else:
                group.status = "scraping"
                db.commit()
        finally:
            db.close()

        self._log(f"Scraping membres @{username} (profondeur {depth})...")
        self._update_job(current_group=username)

        members_found = []

        try:
            client, acc_idx = await self.am.get_client()
            entity = await client.get_entity(username)

            if not is_group(entity):
                self._log(f"@{username}: canal broadcast ignoré (groups only)")
                db = Session()
                try:
                    g = db.query(Group).filter_by(username=username).first()
                    if g:
                        g.status = "error"
                        g.error_msg = "canal broadcast, pas un groupe"
                        db.commit()
                finally:
                    db.close()
                return []

            # Récupérer description + classifier
            description = ""
            try:
                from telethon.tl.functions.channels import GetFullChannelRequest
                full = await client(GetFullChannelRequest(entity))
                description = full.full_chat.about or ""
            except Exception:
                pass

            db = Session()
            try:
                group = db.query(Group).filter_by(username=username).first()
                if group:
                    group.title = getattr(entity, "title", username)
                    group.member_count = getattr(entity, "participants_count", None)
                    group.description = description
                    group.category = classify_group(username, group.title or "", description)
                    db.commit()
            finally:
                db.close()

            await self._sleep()
            offset = 0
            limit = 100

            while len(members_found) < self.max_members and not self._stop:
                try:
                    participants = await client(GetParticipantsRequest(
                        entity,
                        ChannelParticipantsSearch(""),
                        offset=offset,
                        limit=limit,
                        hash=0,
                    ))
                except errors.FloodWaitError as e:
                    self._log(f"FloodWait {e.seconds}s, rotation compte...")
                    await self.am.mark_flood(acc_idx, e.seconds)
                    await asyncio.sleep(min(e.seconds, 60))
                    try:
                        client, acc_idx = await self.am.get_client()
                        entity = await client.get_entity(username)
                    except Exception:
                        break
                    continue
                except errors.ChatAdminRequiredError:
                    self._log(f"@{username}: droits admin requis")
                    break
                except (errors.ChannelPrivateError, errors.InviteHashInvalidError):
                    self._log(f"@{username}: groupe privé/invalide")
                    break
                except Exception as e:
                    self._log(f"Erreur GetParticipants @{username}: {e}")
                    break

                if not participants.users:
                    break

                db = Session()
                try:
                    for user in participants.users:
                        if not user.username:
                            continue
                        member_data = {
                            "telegram_id": str(user.id),
                            "username": user.username,
                            "first_name": user.first_name or "",
                            "last_name": user.last_name or "",
                        }
                        members_found.append(member_data)

                        if not db.query(Member).filter_by(telegram_id=str(user.id)).first():
                            db.add(Member(**member_data))

                        if not db.query(GroupMember).filter_by(
                            group_username=username,
                            member_telegram_id=str(user.id),
                        ).first():
                            db.add(GroupMember(
                                group_username=username,
                                member_telegram_id=str(user.id),
                            ))
                    db.commit()
                finally:
                    db.close()

                offset += len(participants.users)
                if len(participants.users) < limit:
                    break

                await self._sleep()

        except Exception as e:
            self._log(f"Erreur scraping @{username}: {e}")
            db = Session()
            try:
                g = db.query(Group).filter_by(username=username).first()
                if g:
                    g.status = "error"
                    g.error_msg = str(e)
                    db.commit()
            finally:
                db.close()
            return members_found

        db = Session()
        try:
            g = db.query(Group).filter_by(username=username).first()
            if g:
                g.status = "done"
                g.scraped_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()

        self._log(f"@{username}: {len(members_found)} membres avec @")
        self._update_job(members_found=self._get_job_members_count() + len(members_found))
        return members_found

    def _get_job_members_count(self) -> int:
        db = Session()
        try:
            job = db.query(ScrapeJob).get(self.job_id)
            return job.members_found or 0 if job else 0
        finally:
            db.close()

    # ------------------------------------------------------------------ #
    #  Découverte de groupes similaires (fonctionne dès le 1er groupe)   #
    # ------------------------------------------------------------------ #

    async def find_similar_groups(self, group_entity) -> list[str]:
        """
        Telegram recommande des groupes similaires via getSimilarChannels.
        Fonctionne dès le groupe seed, sans aucun autre contexte.
        """
        found = []
        await self._sleep()
        try:
            client, acc_idx = await self.am.get_client()
            result = await client(GetSimilarChannelsRequest(group_entity))
            for chat in result.chats:
                uname = getattr(chat, "username", None)
                if uname and is_group(chat):
                    found.append(uname)
            self._log(f"getSimilarChannels: {len(found)} groupe(s) (canaux exclus)")
        except errors.FloodWaitError as e:
            await self.am.mark_flood(acc_idx, e.seconds)
            await asyncio.sleep(min(e.seconds, 60))
        except Exception as e:
            logger.debug(f"find_similar_groups: {e}")
        return found

    # ------------------------------------------------------------------ #
    #  Parsing des bios des membres pour extraire des liens t.me          #
    # ------------------------------------------------------------------ #

    async def extract_groups_from_bio(self, user_entity) -> list[str]:
        """
        Récupère la bio complète d'un utilisateur et extrait tous les liens
        t.me/username. Fonctionne dès le départ, sans contexte supplémentaire.
        """
        found = []
        await self._sleep()
        try:
            client, acc_idx = await self.am.get_client()
            full = await client(GetFullUserRequest(user_entity))
            bio = (full.full_user.about or "").strip()
            if bio:
                for match in TGME_RE.findall(bio):
                    found.append(match)
        except errors.FloodWaitError as e:
            await self.am.mark_flood(acc_idx, e.seconds)
            await asyncio.sleep(min(e.seconds, 60))
        except Exception as e:
            logger.debug(f"extract_groups_from_bio: {e}")
        return found

    # ------------------------------------------------------------------ #
    #  messages.search from_id (efficace quand plusieurs groupes connus)  #
    # ------------------------------------------------------------------ #

    async def find_user_groups_via_search(self, user_entity) -> list[str]:
        """
        Cherche les messages de l'utilisateur dans les dialogues accessibles.
        Limité au début (1 seul groupe), mais grandit avec le nombre de
        groupes découverts (les groupes publics sont accessibles sans rejoindre).
        """
        found = []
        await self._sleep()
        try:
            client, acc_idx = await self.am.get_client()
            result = await client(SearchRequest(
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
            for chat in result.chats:
                uname = getattr(chat, "username", None)
                if uname and is_group(chat):
                    found.append(uname)
        except errors.FloodWaitError as e:
            await self.am.mark_flood(acc_idx, e.seconds)
            await asyncio.sleep(min(e.seconds, 60))
        except Exception as e:
            logger.debug(f"find_user_groups_via_search: {e}")
        return found

    # ------------------------------------------------------------------ #
    #  Découverte par mots-clés (contacts.search)                        #
    # ------------------------------------------------------------------ #

    async def discover_by_keywords(self, queries: list[str]) -> list[str]:
        """
        Utilise contacts.search pour trouver des groupes/canaux publics
        correspondant à des mots-clés. Ne nécessite aucun contexte préalable.
        """
        found = []
        try:
            client, acc_idx = await self.am.get_client()
            for q in queries:
                if self._stop:
                    break
                await self._sleep()
                try:
                    res = await client(ContactsSearchRequest(q=q, limit=50))
                    groups_only = [c for c in res.chats if getattr(c, "username", None) and is_group(c)]
                    for chat in groups_only:
                        found.append(chat.username)
                    self._log(f"contacts.search '{q}': {len(groups_only)}/{len(res.chats)} groupes (canaux exclus)")
                except errors.FloodWaitError as e:
                    await self.am.mark_flood(acc_idx, e.seconds)
                    await asyncio.sleep(min(e.seconds, 60))
                except Exception as e:
                    logger.debug(f"contacts.search '{q}': {e}")
        except Exception as e:
            logger.debug(f"discover_by_keywords: {e}")
        return list(set(found))

    # ------------------------------------------------------------------ #
    #  Enregistrement d'un nouveau groupe découvert                       #
    # ------------------------------------------------------------------ #

    def _enqueue_group(
        self,
        username: str,
        depth: int,
        queue: deque,
        visited: set[str],
        source: str = "",
    ):
        if username in visited:
            return
        db = Session()
        try:
            if not db.query(Group).filter_by(username=username).first():
                db.add(Group(username=username, depth=depth, status="pending"))
                db.commit()
        finally:
            db.close()
        queue.append((username, depth))
        self._log(f"Nouveau groupe: @{username}{f' (via {source})' if source else ''}")

    # ------------------------------------------------------------------ #
    #  Boucle principale                                                   #
    # ------------------------------------------------------------------ #

    async def run(self, seed_group: str, category_seeds: list[str] | None = None):
        """
        seed_group : groupe de départ (vide = "" pour démarrer uniquement par catégorie)
        category_seeds : liste de requêtes contacts.search à lancer au démarrage
        """
        seed = seed_group.lstrip("@")
        self._log(f"Démarrage{f' depuis @{seed}' if seed else ' par catégories'}")

        queue: deque[tuple[str, int]] = deque()
        if seed:
            queue.append((seed, 0))
        visited_groups: set[str] = set()

        # -- Amorçage par mots-clés (contacts.search) --
        if category_seeds:
            self._log(f"Recherche par {len(category_seeds)} requêtes mots-clés...")
            kw_groups = await self.discover_by_keywords(category_seeds)
            for g in kw_groups:
                queue.append((g, 0))
            self._log(f"{len(kw_groups)} groupes trouvés via mots-clés")

        while queue and not self._stop:
            group_username, depth = queue.popleft()
            if group_username in visited_groups:
                continue
            if depth > self.max_depth:
                continue
            visited_groups.add(group_username)

            # --- Scraping des membres ---
            members = await self.scrape_group_members(group_username, depth)

            self._update_job(groups_found=len(visited_groups))

            # --- Groupes similaires (Telegram algo, fonctionne dès le 1er groupe) ---
            try:
                client, _ = await self.am.get_client()
                group_entity = await client.get_entity(group_username)
                similar = await self.find_similar_groups(group_entity)
                for g in similar:
                    self._enqueue_group(g, depth + 1, queue, visited_groups,
                                        f"similaire à @{group_username}")
            except Exception as e:
                logger.debug(f"similar groups error: {e}")

            if depth >= self.max_depth:
                continue

            # --- Découverte via les membres (bio + messages.search) ---
            sample = members[:40]
            self._log(f"Analyse de {len(sample)} membres de @{group_username}...")

            try:
                client, _ = await self.am.get_client()
            except RuntimeError:
                self._log("Aucun compte disponible, arrêt.")
                break

            for i, member in enumerate(sample):
                if self._stop:
                    break

                try:
                    user_entity = await client.get_entity(int(member["telegram_id"]))
                except Exception:
                    continue

                # Bio → liens t.me (efficace dès le départ)
                bio_groups = await self.extract_groups_from_bio(user_entity)
                for g in bio_groups:
                    self._enqueue_group(g, depth + 1, queue, visited_groups,
                                        f"bio de @{member['username']}")

                # messages.search from_id (grandit avec les groupes connus)
                search_groups = await self.find_user_groups_via_search(user_entity)
                for g in search_groups:
                    self._enqueue_group(g, depth + 1, queue, visited_groups,
                                        f"messages @{member['username']}")

                if (i + 1) % 10 == 0:
                    self._log(f"Progression membres: {i+1}/{len(sample)}")

        await self.am.disconnect_all()
        self._update_job(status="done", finished_at=datetime.utcnow())
        self._log("Scraping terminé !")
