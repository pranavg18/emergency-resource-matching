"""Give the queue consumer the same graceful shutdown lifecycle as HTTP work."""
import signal

bind = "0.0.0.0:8000"
workers = 1
worker_class = "gthread"
threads = 8
graceful_timeout = 40


def post_worker_init(worker):
    consumer = worker.wsgi.extensions["worker"]
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        consumer.stop.set()
        previous(signum, frame)

    signal.signal(signal.SIGTERM, terminate)


def worker_exit(server, worker):
    app = getattr(worker, "wsgi", None)
    if app is not None:
        app.extensions["worker"].shutdown(timeout=25)
