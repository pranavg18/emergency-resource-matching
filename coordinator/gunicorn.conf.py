# gunicorn settings for the coordinator.
#
# Exactly one worker process: the node registry lives in process memory, and
# with several workers each would see a different cluster. 32 threads, because
# heartbeats must always find a free thread even while writes wait on a
# shard's lock.
bind = "0.0.0.0:8000"
workers = 1
worker_class = "gthread"
threads = 32
timeout = 30
graceful_timeout = 10

# Keep idle connections open longer than any client reuses them: nodes
# heartbeat every 2 s on a pooled connection, the coordinator pools its
# connections to nodes, and ingress-nginx reuses upstream connections for
# 60 s. With gunicorn's default of 2 s the server sometimes closed a
# connection just as the client sent on it, failing that request
# ("Connection aborted", or a 502 at the Ingress for POSTs).
keepalive = 75
