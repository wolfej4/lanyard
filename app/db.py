import os
from sqlalchemy import event as sa_event
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

url = settings.database_url
if url.startswith("postgres://"):
    url = "postgresql+psycopg://" + url[len("postgres://"):]
elif url.startswith("postgresql://"):
    url = "postgresql+psycopg://" + url[len("postgresql://"):]

if url.startswith("sqlite"):
    path = url.split("///", 1)[-1]
    if path and path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @sa_event.listens_for(engine, "connect")
    def _sqlite_pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()
else:
    engine = create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
