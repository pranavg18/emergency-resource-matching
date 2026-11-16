"""Day 1 health-check skeleton; business endpoints arrive in later phases."""
from common.config import Settings
from common.core import app_base


def create_app():
    app = app_base("request-service")
    app.config["SETTINGS"] = Settings.from_env()
    return app
