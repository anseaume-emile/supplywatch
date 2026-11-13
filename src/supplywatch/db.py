"""Database models. SQLite by default (one file in /data), any SQLAlchemy URL works (e.g. Postgres)."""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from . import config


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


def norm_mpn(mpn: str) -> str:
    return "".join((mpn or "").upper().split())


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20))  # git | upload | a365
    external_id: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    variant: Mapped[str] = mapped_column(String(200), default="")
    build_qty: Mapped[int] = mapped_column(Integer, default=10)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[str] = mapped_column(String(100), default="")  # git commit "abc1234 2026-09-28"
    location: Mapped[str] = mapped_column(Text, default="")  # repo/path of the .PrjPcb
    synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    lines: Mapped[list[BomLine]] = relationship(back_populates="project", cascade="all, delete-orphan")


class BomLine(Base):
    __tablename__ = "bom_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    component: Mapped[str] = mapped_column(String(300), default="")  # e.g. CMP-001-00001-5
    description: Mapped[str] = mapped_column(Text, default="")
    designators: Mapped[list] = mapped_column(JSON, default=list)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    # ordered list of {"mfr":..., "mpn":...}; index 0 is the preferred part
    part_choices: Mapped[list] = mapped_column(JSON, default=list)
    footprint: Mapped[str] = mapped_column(String(300), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    mpn_source: Mapped[str] = mapped_column(String(20), default="")  # schematic | activebom | upload | a365
    project: Mapped[Project] = relationship(back_populates="lines")


class Part(Base):
    """One manufacturer part number, shared across all projects that use it."""

    __tablename__ = "parts"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)  # normalised MPN
    mpn: Mapped[str] = mapped_column(String(200))
    manufacturer: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    lifecycle: Mapped[str] = mapped_column(String(100), default="")
    lifecycle_source: Mapped[str] = mapped_column(String(40), default="")
    lead_days: Mapped[int | None] = mapped_column(Integer, nullable=True)  # best (shortest) known factory lead
    lead_source: Mapped[str] = mapped_column(String(40), default="")
    total_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)  # sum of authorised stock
    coo: Mapped[str] = mapped_column(String(100), default="")
    coo_source: Mapped[str] = mapped_column(String(40), default="")
    hts: Mapped[str] = mapped_column(String(60), default="")
    eccn: Mapped[str] = mapped_column(String(60), default="")
    rohs: Mapped[str] = mapped_column(String(60), default="")  # compliance.normalise() label
    rohs_source: Mapped[str] = mapped_column(String(40), default="")
    reach: Mapped[str] = mapped_column(String(60), default="")
    reach_source: Mapped[str] = mapped_column(String(40), default="")
    datasheet: Mapped[str] = mapped_column(Text, default="")
    octopart_url: Mapped[str] = mapped_column(Text, default="")
    not_found: Mapped[bool] = mapped_column(Boolean, default=False)
    refreshed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    offers: Mapped[list[Offer]] = relationship(back_populates="part", cascade="all, delete-orphan")


class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(20))  # trustedparts | digikey | mouser | farnell | nexar
    seller: Mapped[str] = mapped_column(String(200))
    authorized: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sku: Mapped[str] = mapped_column(String(200), default="")
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stock_note: Mapped[str] = mapped_column(String(100), default="")
    on_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    moq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    packaging: Mapped[str] = mapped_column(String(100), default="")
    lead_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prices: Mapped[list] = mapped_column(JSON, default=list)  # [{"qty":1,"price":0.1,"currency":"CHF"}]
    url: Mapped[str] = mapped_column(Text, default="")
    seller_country: Mapped[str] = mapped_column(String(10), default="")
    warehouse: Mapped[str] = mapped_column(String(300), default="")  # as reported by the source, e.g. "Liège, Belgium"
    updated: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    part: Mapped[Part] = relationship(back_populates="offers")


class StockSnapshot(Base):
    """Stock per region at one supply refresh, split by trusted (authorised) and other sellers."""

    __tablename__ = "stock_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id", ondelete="CASCADE"), index=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    region: Mapped[str] = mapped_column(String(30))  # see warehouses.REGIONS
    trusted: Mapped[bool] = mapped_column(Boolean)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    sellers: Mapped[int] = mapped_column(Integer, default=0)


class CooOverride(Base):
    """Country of origin entered by procurement. Survives every refresh and wins over API data."""

    __tablename__ = "coo_overrides"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    coo: Mapped[str] = mapped_column(String(100))
    note: Mapped[str] = mapped_column(Text, default="")
    by: Mapped[str] = mapped_column(String(200), default="")
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class KV(Base):
    """Small key/value store: OAuth tokens, quota counters, last errors."""

    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))  # bom_sync | supply_refresh
    started: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | ok | partial | error
    summary: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[list] = mapped_column(JSON, default=list)
    triggered_by: Mapped[str] = mapped_column(String(200), default="schedule")


class NexarUsage(Base):
    __tablename__ = "nexar_usage"
    __table_args__ = (UniqueConstraint("month"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    parts: Mapped[int] = mapped_column(Integer, default=0)


_engine = None
SessionLocal: sessionmaker | None = None


def init(url: str | None = None):
    global _engine, SessionLocal
    url = url or config.settings.database_url
    kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
    _engine = create_engine(url, future=True, **kw)
    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _fk(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    SessionLocal = sessionmaker(_engine, expire_on_commit=False, future=True)
    return _engine


def _add_missing_columns(engine):
    """create_all() doesn't alter existing tables; add columns introduced since the database was created."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}"
                if isinstance(default, str):
                    ddl += " DEFAULT '" + default.replace("'", "''") + "'"
                conn.execute(text(ddl))


@contextmanager
def session():
    if SessionLocal is None:
        raise RuntimeError("db.init() has not been called")
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def kv_get(s, key: str, default=None):
    row = s.get(KV, key)
    return row.value if row else default


def kv_set(s, key: str, value):
    row = s.get(KV, key)
    if row:
        row.value = value
    else:
        s.add(KV(key=key, value=value))


def usage_this_month(s) -> NexarUsage:
    month = utcnow().strftime("%Y-%m")
    row = s.scalar(select(NexarUsage).where(NexarUsage.month == month))
    if not row:
        row = NexarUsage(month=month, parts=0)
        s.add(row)
        s.flush()
    return row
