"""Give the queue consumer the same graceful shutdown lifecycle as HTTP work."""
import signal

bind = "0.0.0.0:8000"
workers = 1
worker_class = "gthread"
threads = 8
graceful_timeout = 40

# Keep idle connections open longer than any client reuses them: nodes
# heartbeat every 2 s on a pooled connection, the coordinator pools its
# connections to nodes, and ingress-nginx reuses upstream connections for
# 60 s. With gunicorn's default of 2 s the server sometimes closed a
# connection just as the client sent on it, failing that request
# ("Connection aborted", or a 502 at the Ingress for POSTs).
keepalive = 75


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
