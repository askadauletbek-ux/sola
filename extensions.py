# extensions.py
import os
from flask_sqlalchemy import SQLAlchemy

DB_URL = os.getenv("DATABASE_URL", "sqlite:///35healthclubs.db")

engine_options = {"pool_pre_ping": True}
# На всякий случай дожимаем SSL, если забыли в URL
if DB_URL.startswith("postgresql") and "sslmode=" not in DB_URL:
    engine_options["connect_args"] = {"sslmode": "require"}

db = SQLAlchemy(engine_options=engine_options)
