"""Entry point: `python app.py` for development, `gunicorn app:app` in Docker."""

from streamfinder import config, create_app

app = create_app()

if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, threaded=True)
