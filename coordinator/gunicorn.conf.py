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
