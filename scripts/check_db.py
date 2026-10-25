import os

from sqlalchemy import create_engine, text


def main() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1")).one()
        print(result)


if __name__ == "__main__":
    main()
