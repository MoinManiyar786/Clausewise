"""Entry point: `python run.py`, then open http://127.0.0.1:8000

Serves with Waitress, a production-grade WSGI server that runs on Windows,
macOS and Linux. Set FLASK_DEBUG=1 to use Flask's reloader during development.
"""
import logging
import os

from app import create_app

app = create_app()


def main() -> None:
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    if os.getenv("FLASK_DEBUG") == "1":
        app.run(host=host, port=port, debug=True)  # local development only
        return
    try:
        from waitress import serve
    except ImportError:
        logging.getLogger(__name__).warning("waitress not installed; using Flask's development server.")
        app.run(host=host, port=port)
        return
    serve(app, host=host, port=port, threads=8, ident="")  # ident="" hides the server banner


if __name__ == "__main__":
    main()
