from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, sessionmaker, relationship
from datetime import datetime


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    phone = Column(String, unique=True, nullable=False)
    api_id = Column(String, nullable=False)
    api_hash = Column(String, nullable=False)
    session_file = Column(String)
    is_active = Column(Boolean, default=True)
    request_count = Column(Integer, default=0)
    last_used = Column(DateTime)
    ban_until = Column(DateTime, nullable=True)


class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    title = Column(String)
    description = Column(Text, nullable=True)
    member_count = Column(Integer)
    scraped_at = Column(DateTime)
    depth = Column(Integer, default=0)
    status = Column(String, default="pending")  # pending, scraping, done, error
    error_msg = Column(Text, nullable=True)
    category = Column(String, default="other")  # ofm, carding, crypto, voip, ...


class Member(Base):
    __tablename__ = "members"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    discovered_at = Column(DateTime, default=datetime.utcnow)


class GroupMember(Base):
    __tablename__ = "group_members"
    id = Column(Integer, primary_key=True)
    group_username = Column(String, ForeignKey("groups.username"))
    member_telegram_id = Column(String, ForeignKey("members.telegram_id"))


class ScrapeJob(Base):
    __tablename__ = "scrape_jobs"
    id = Column(Integer, primary_key=True)
    seed_group = Column(String, nullable=False)
    status = Column(String, default="running")  # running, paused, done, error
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    groups_found = Column(Integer, default=0)
    members_found = Column(Integer, default=0)
    current_group = Column(String, nullable=True)
    log = Column(Text, default="")


engine = create_engine("sqlite:///scraper.db", echo=False)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


def get_db():
    db = Session()
    try:
        yield db
    finally:
        db.close()
