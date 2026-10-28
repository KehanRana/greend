from alembic import context
from greenroute.config import Settings
from greenroute.store import make_engine

settings = Settings.from_env()
engine = make_engine(settings.database)
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
