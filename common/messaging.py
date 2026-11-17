"""Short-lived publisher connections are thread-local by construction."""
import json

import pika

from common.config import Settings, get_int


def connection():
    params = pika.URLParameters(Settings.from_env().rabbitmq_url)
    params.socket_timeout = 3
    params.stack_timeout = 5
    params.blocked_connection_timeout = 3
    params.heartbeat = 30
    params.connection_attempts = 1
    return pika.BlockingConnection(params)


def topology(channel):
    channel.exchange_declare(exchange="emergency", exchange_type="direct", durable=True)
    channel.queue_declare(queue="match.dead", durable=True)
    for queue, routing in (("match.requests", "request.created"),
                           ("match.released", "request.closed")):
        channel.queue_declare(queue=queue, durable=True,
                              arguments={"x-dead-letter-exchange": "",
                                         "x-dead-letter-routing-key": "match.dead"})
        channel.queue_bind(queue=queue, exchange="emergency", routing_key=routing)
    channel.queue_declare(queue="match.retry", durable=True, arguments={
        "x-message-ttl": get_int("RETRY_DELAY_MS", 5000, min_value=1),
        "x-dead-letter-exchange": "emergency", "x-dead-letter-routing-key": "request.created"})


def publish(event, message):
    conn = connection()
    try:
        ch = conn.channel()
        topology(ch)
        ch.confirm_delivery()
        ch.basic_publish(exchange="" if event == "retry" else "emergency",
                         routing_key="match.retry" if event == "retry" else event,
                         body=json.dumps(message), mandatory=True,
                         properties=pika.BasicProperties(delivery_mode=2,
                                                         content_type="application/json",
                                                         correlation_id=message.get("correlation_id")))
    finally:
        if conn.is_open:
            conn.close()
