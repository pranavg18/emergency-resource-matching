# gunicorn settings for a storage node.
#
# One worker process: the connection-per-thread model and the conditional
# write's transaction assume a single process owns the SQLite file. Threads
# let heartbeats and slow writes run side by side.
bind = "0.0.0.0:8080"
workers = 1
worker_class = "gthread"
threads = 8
timeout = 30
graceful_timeout = 10

# Keep idle connections open longer than any client reuses them: nodes
# heartbeat every 2 s on a pooled connection, the coordinator pools its
# connections to nodes, and ingress-nginx reuses upstream connections for
# 60 s. With gunicorn's default of 2 s the server sometimes closed a
# connection just as the client sent on it, failing that request
# ("Connection aborted", or a 502 at the Ingress for POSTs).
keepalive = 75
