import os

from sqlalchemy import create_engine, text


def test_database_connection() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])

    try:
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1")).scalar_one()
            assert result == 1
    finally:
        engine.dispose()
