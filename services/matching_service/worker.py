import json
import threading
import time

import pika

from common.config import Settings
from common.core import APIError, Conflict, correlation, log
from common.messaging import connection, publish, topology


class Worker:
    def __init__(self, matcher):
        self.matcher = matcher
        self.settings = Settings.from_env()
        self.stop = threading.Event()
        self.threads = []

    def handle(self, message, release=False):
        if not isinstance(message, dict) or not isinstance(message.get("request_id"), str):
            raise ValueError("invalid message")
        attempt = message.get("attempt", 0)
        if type(attempt) is not int or attempt < 0:
            raise ValueError("invalid attempt")
        if release:
            if not isinstance(message.get("resource_id"), str):
                raise ValueError("invalid resource_id")
            self.matcher.release(message["resource_id"], message["request_id"], attempt)
            return
        try:
            result = self.matcher.match(message["request_id"])
        except Conflict:
            return  # Already closed.
        except APIError as exc:
            if exc.status < 500:
                raise
            log("match_retry", request_id=message["request_id"], reason=str(exc))
            publish("retry", message)  # Storage errors do not consume search attempts.
            return
        if not result["matched"]:
            message["attempt"] = attempt + 1
            if message["attempt"] >= self.settings.max_attempts:
                self.matcher.mark_unmatched(message["request_id"])
            else:
                publish("retry", message)

    def consume(self):
        while not self.stop.is_set():
            conn = None
            try:
                conn = connection()
                ch = conn.channel()
                topology(ch)
                ch.basic_qos(prefetch_count=1)

                def callback(channel, method, properties, raw):
                    if self.stop.is_set():
                        return  # Closing this connection will requeue undelivered work.
                    correlation.set(properties.correlation_id or "consumer")
                    log("consume", redelivered=method.redelivered,
                        correlation_id=properties.correlation_id)
                    if self.stop.wait(self.settings.match_delay_ms / 1000):
                        conn.close()
                        return
                    try:
                        self.handle(json.loads(raw), method.routing_key == "request.closed")
                    except (pika.exceptions.AMQPError, OSError):
                        # Failed retry confirm: leave the original unacked by closing the connection.
                        conn.close()
                        return
                    except APIError as exc:
                        if exc.status >= 500 and self.stop.wait(2):
                            conn.close()
                            return
                        channel.basic_nack(method.delivery_tag, requeue=exc.status >= 500)
                    except Exception as exc:
                        log("dead_letter", error=type(exc).__name__)
                        channel.basic_reject(method.delivery_tag, requeue=False)
                    else:
                        # SIGTERM stops new work, but a completed handler still owns
                        # this delivery. Ack before the consumer closes its connection.
                        channel.basic_ack(method.delivery_tag)
                        log("consume_acked", correlation_id=properties.correlation_id)

                ch.basic_consume("match.requests", callback)
                ch.basic_consume("match.released", callback)
                while conn.is_open and not self.stop.is_set():
                    conn.process_data_events(time_limit=1)
            except (pika.exceptions.AMQPError, OSError) as exc:
                log("consumer_reconnect", error=type(exc).__name__)
            finally:
                if conn and conn.is_open:
                    try:
                        conn.close()
                    except (pika.exceptions.AMQPError, OSError):
                        pass  # The broker requeues any unacknowledged delivery.
            self.stop.wait(2)

    def sweeper(self):
        while not self.stop.is_set():
            try:
                self.matcher.sweep(publish)
            except Exception as exc:
                log("sweep_failed", error=type(exc).__name__)
            self.stop.wait(self.settings.sweep_interval_s)

    def start(self):
        for target in (self.consume, self.sweeper):
            thread = threading.Thread(target=target, name=target.__name__, daemon=True)
            self.threads.append(thread)
            thread.start()

    def shutdown(self, timeout=25):
        """Drain on the owning threads; Pika connections are not thread-safe."""
        self.stop.set()
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(max(0, deadline - time.monotonic()))
        pending = [thread.name for thread in self.threads if thread.is_alive()]
        log("worker_shutdown", drained=not pending, pending=pending)
        return not pending
