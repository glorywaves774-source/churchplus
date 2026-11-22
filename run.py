"""Entry point for `flask run` (development) and Gunicorn (`gunicorn run:app`)."""
from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    app.run()
