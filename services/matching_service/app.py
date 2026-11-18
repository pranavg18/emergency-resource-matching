from common.config import Settings
from common.core import app_base
from common.storage import StorageClient
from services.matching_service.engine import Matcher
from services.matching_service.worker import Worker


def create_app(storage=None, start_background=False):
    storage = storage or StorageClient()
    app = app_base("matching-service", storage.ready)
    app.config["SETTINGS"] = Settings.from_env()
    matcher = Matcher(storage)
    app.extensions["matcher"] = matcher
    worker = Worker(matcher)
    app.extensions["worker"] = worker

    @app.post("/match/<request_id>")
    def match(request_id):
        return matcher.match(request_id)

    if start_background:
        worker.start()
    return app
