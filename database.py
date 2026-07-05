from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./thrivecart.db")

# Railway provides PostgreSQL URLs as postgres:// but SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from models import Subscription, PaymentEvent, OverdueNotification  # noqa: F401
    from sqlalchemy import text, inspect
    Base.metadata.create_all(bind=engine)
    # Add subscription_type column if upgrading from older schema
    with engine.connect() as conn:
        cols = [c["name"] for c in inspect(engine).get_columns("subscriptions")]
        if "subscription_type" not in cols:
            conn.execute(text("ALTER TABLE subscriptions ADD COLUMN subscription_type VARCHAR DEFAULT 'recurring'"))
            conn.commit()
