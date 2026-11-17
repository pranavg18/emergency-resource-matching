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
