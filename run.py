"""Local entry point: `python run.py` then open http://127.0.0.1:8000"""
import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    # Debug is off by default; never enable it in production.
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")),
            debug=os.getenv("FLASK_DEBUG") == "1")
