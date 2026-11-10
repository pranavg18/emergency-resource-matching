# Viva Notes

Talking points worth having ready for the demo and viva. Add to this as we build. Section numbers refer to `IMPLEMENTATION_PLAN.md`.

## The matching consumer does not drain on shutdown

**What we do:** on SIGTERM, matching-service drops its RabbitMQ connection without finishing the message it is working on. RabbitMQ redelivers that message to another pod.

**Why it is safe:** the consumer is idempotent (§4.6). A pod killed between claiming a resource (step 4) and marking the request matched (step 5) is exactly the case the design handles: the next pod finds `candidate_resource_id` on the request, sees the request id already in that resource's `assigned_request_ids`, and finishes step 5 without claiming twice.

**The line to say:** shutdown and crash take the same path, so every rollout or scale-down of matching-service exercises our crash-recovery logic, not only the demo. The side effect is one redelivered message per stopped pod, which is harmless.

**Where it shows on screen:** `redelivered=true` in another matching pod's log after `kubectl delete pod` (§10, Kubernetes recovery).
