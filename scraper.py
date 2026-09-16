import asyncio
import random
import os
import logging
from datetime import datetime, timedelta
from collections import deque
from telethon import TelegramClient, errors
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantsSearch, InputPeerChannel
from database import Session, Account, Group, Member, GroupMember, ScrapeJob

logger = logging.getLogger(__name__)


class AccountManager:
    def __init__(self, accounts_config: list[dict]):
        self.accounts = accounts_config
        self.current_idx = 0
        self.clients: dict[str, TelegramClient] = {}
        self.req_counts: dict[str, int] = {}

    def _session_path(self, phone: str) -> str:
        os.makedirs("sessions", exist_ok=True)
        return f"sessions/{phone.replace('+', '')}"

    async def get_client(self) -> TelegramClient:
        db = Session()
        try:
            for _ in range(len(self.accounts)):
                idx = self.current_idx % len(self.accounts)
                self.current_idx += 1
                acc = self.accounts[idx]
                phone = acc["phone"]

                db_acc = db.query(Account).filter_by(phone=phone).first()
                if db_acc and db_acc.ban_until and db_acc.ban_until > datetime.utcnow():
                    logger.warning(f"Compte {phone} banni jusqu'à {db_acc.ban_until}, rotation...")
                    continue

                if phone not in self.clients:
                    client = TelegramClient(
                        self._session_path(phone),
                        int(acc["api_id"]),
                        acc["api_hash"],
                        request_retries=3,
                        connection_retries=3,
                    )
                    await client.connect()
                    if not await client.is_user_authorized():
                        logger.error(f"Compte {phone} non autorisé, session manquante.")
                        continue
                    self.clients[phone] = client
                    self.req_counts[phone] = 0

                return self.clients[phone], phone
            raise RuntimeError("Aucun compte disponible")
        finally:
            db.close()

    async def mark_flood(self, phone: str, seconds: int):
        db = Session()
        try:
            acc = db.query(Account).filter_by(phone=phone).first()
            if acc:
                acc.ban_until = datetime.utcnow() + timedelta(seconds=seconds + 60)
                db.commit()
            if phone in self.clients:
                await self.clients[phone].disconnect()
                del self.clients[phone]
        finally:
            db.close()

    async def disconnect_all(self):
        for client in self.clients.values():
            await client.disconnect()
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
                self._log(f"Groupe @{username} déjà scrappé, skip")
                return []
            else:
                group.status = "scraping"
                db.commit()
        finally:
            db.close()

        self._log(f"Scraping @{username} (profondeur {depth})...")
        self._update_job(current_group=username)

        members_found = []
        offset = 0
        limit = 100

        try:
            client, phone = await self.am.get_client()
            entity = await client.get_entity(username)

            db = Session()
            try:
                group = db.query(Group).filter_by(username=username).first()
                if group:
                    group.title = getattr(entity, "title", username)
                    group.member_count = getattr(entity, "participants_count", None)
                    db.commit()
            finally:
                db.close()

            await self._sleep()

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
                    self._log(f"FloodWait {e.seconds}s sur {phone}, rotation compte...")
                    await self.am.mark_flood(phone, e.seconds)
                    await asyncio.sleep(min(e.seconds, 60))
                    try:
                        client, phone = await self.am.get_client()
                        entity = await client.get_entity(username)
                    except Exception:
                        break
                    continue
                except errors.ChatAdminRequiredError:
                    self._log(f"@{username}: droits admin requis pour lister les membres")
                    break
                except (errors.ChannelPrivateError, errors.InviteHashInvalidError):
                    self._log(f"@{username}: groupe privé ou invalide")
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

                        existing = db.query(Member).filter_by(telegram_id=str(user.id)).first()
                        if not existing:
                            db.add(Member(**member_data))

                        link = db.query(GroupMember).filter_by(
                            group_username=username,
                            member_telegram_id=str(user.id)
                        ).first()
                        if not link:
                            db.add(GroupMember(group_username=username, member_telegram_id=str(user.id)))

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
                group = db.query(Group).filter_by(username=username).first()
                if group:
                    group.status = "error"
                    group.error_msg = str(e)
                    db.commit()
            finally:
                db.close()
            return members_found

        db = Session()
        try:
            group = db.query(Group).filter_by(username=username).first()
            if group:
                group.status = "done"
                group.scraped_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()

        self._log(f"@{username}: {len(members_found)} membres avec @ trouvés")
        db = Session()
        try:
            job = db.query(ScrapeJob).get(self.job_id)
            if job:
                job.members_found = (job.members_found or 0) + len(members_found)
                db.commit()
        finally:
            db.close()

        return members_found

    async def find_user_groups(self, username: str) -> list[str]:
        if self._stop:
            return []

        await self._sleep()
        try:
            client, phone = await self.am.get_client()
            entity = await client.get_entity(username)

            common_chats = []
            try:
                result = await client.get_common_chats(entity)
                for chat in result:
                    if hasattr(chat, "username") and chat.username:
                        common_chats.append(chat.username)
            except errors.FloodWaitError as e:
                self._log(f"FloodWait {e.seconds}s get_common_chats, rotation...")
                await self.am.mark_flood(phone, e.seconds)
                await asyncio.sleep(min(e.seconds, 60))
            except Exception as e:
                logger.debug(f"get_common_chats @{username}: {e}")

            return common_chats

        except Exception as e:
            logger.debug(f"find_user_groups @{username}: {e}")
            return []

    async def run(self, seed_group: str):
        self._log(f"Démarrage depuis @{seed_group}")

        queue: deque[tuple[str, int]] = deque()
        queue.append((seed_group.lstrip("@"), 0))
        visited_groups: set[str] = set()

        while queue and not self._stop:
            group_username, depth = queue.popleft()
            if group_username in visited_groups:
                continue
            if depth > self.max_depth:
                continue
            visited_groups.add(group_username)

            members = await self.scrape_group_members(group_username, depth)

            db = Session()
            try:
                job = db.query(ScrapeJob).get(self.job_id)
                if job:
                    job.groups_found = len(visited_groups)
                    db.commit()
            finally:
                db.close()

            if depth < self.max_depth:
                self._log(f"Recherche des groupes communs pour {len(members)} membres...")

                for i, member in enumerate(members[:50]):
                    if self._stop:
                        break
                    groups = await self.find_user_groups(member["username"])
                    for g in groups:
                        if g not in visited_groups:
                            db = Session()
                            try:
                                exists = db.query(Group).filter_by(username=g).first()
                                if not exists:
                                    db.add(Group(username=g, depth=depth + 1, status="pending"))
                                    db.commit()
                            finally:
                                db.close()
                            queue.append((g, depth + 1))
                            self._log(f"Nouveau groupe découvert: @{g}")

                    if i % 10 == 0:
                        self._log(f"Progression membres: {i+1}/{min(50, len(members))}")

        await self.am.disconnect_all()
        self._update_job(status="done", finished_at=datetime.utcnow())
        self._log("Scraping terminé !")
