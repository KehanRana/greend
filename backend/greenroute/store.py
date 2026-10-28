import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import NullPool


def make_engine(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(path, 0o600)
    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def pragmas(connection, _):
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
    return engine


def ready(engine):
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if version != "005":
            raise RuntimeError("Run database migrations")
        conn.execute(text("SELECT id FROM endpoints LIMIT 1"))


def utcnow():
    return datetime.now(timezone.utc).isoformat()


from contextlib import contextmanager


@contextmanager
def immediate(engine):
    """Serialize read-modify-write operations across API and worker processes."""
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
