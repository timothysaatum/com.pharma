import sys
import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Import settings FIRST
from app.core.config import get_settings
settings = get_settings()

# Import Base BEFORE importing models
from app.db.base import Base
from app.models import *
# Import custom types
from app.models.db_types import UUID, JSONB, ARRAY, INET


# Alembic Config
config = context.config
target_metadata = Base.metadata

# Logging setup
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def get_dialect_name():
    """Get the database dialect name from the URL"""
    url = settings.ALEMBIC_DB_URL or settings.DATABASE_URL
    if url.startswith('postgresql://') or url.startswith('postgresql+psycopg'):
        return 'postgresql'
    elif url.startswith('sqlite://'):
        return 'sqlite'
    elif url.startswith('mysql://') or url.startswith('mysql+pymysql'):
        return 'mysql'
    return 'sqlite'  # Default to SQLite


def render_item(type_, object_, autogen_context):
    """Custom renderer for SQLAlchemy types in migrations"""
    dialect = get_dialect_name()
    
    if isinstance(type_, UUID):
        if dialect == 'postgresql':
            return "UUID()"
        else:
            return "sa.String(length=36)"
    elif isinstance(type_, JSONB):
        if dialect == 'postgresql':
            return "JSONB()"
        else:
            return "sa.Text()"
    elif isinstance(type_, ARRAY):
        if dialect == 'postgresql':
            return "ARRAY()"
        else:
            return "sa.Text()"
    elif isinstance(type_, INET):
        if dialect == 'postgresql':
            return "INET()"
        else:
            return "sa.String(length=50)"
    return False


def process_revision_directives(context, revision, directives):
    """Hook to process and modify migrations before rendering"""
    pass


def run_migrations_offline() -> None:
    '''Run migrations in 'offline' mode'''
    url = settings.ALEMBIC_DB_URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=False,
        compare_server_default=False,
        render_item=render_item,
        user_module_prefix='app.models.db_types.',
        process_revision_directives=process_revision_directives,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    '''Run migrations in 'online' mode'''
    url = settings.ALEMBIC_DB_URL
    
    configuration = {
        "sqlalchemy.url": url,
        "sqlalchemy.echo": False,
    }
    
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=False,
            compare_server_default=False,
            render_as_batch=True,
            render_item=render_item,
            user_module_prefix='app.models.db_types.',
            process_revision_directives=process_revision_directives,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()