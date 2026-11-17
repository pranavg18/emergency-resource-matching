# Application and storage contract

The application endpoints below are implemented in the Aksha branch. Live use
depends on coordinator routing, which is not yet implemented in Pranav's Day 1 PR.

| Method and path | Body / query | Result |
|---|---|---|
| `POST /requests` | `type`, `location`, optional `details` | 201 request record |
| `GET /requests/{id}` | None | 200 request record |
| `PUT /requests/{id}/status` | `status` | 200 updated request |
| `POST /resources` | `type`, `location`, integer `availability >= 0` | 201 resource record |
| `GET /resources` | Optional `type` and `status` query filters | 200 `{"resources": [...]}` |
| `PUT /resources/{id}/status` | `status` and/or `availability` | 200 updated resource |
| `POST /match/{requestId}` | None | 200 `matched`, with request/resource on success |

Types: `water`, `food`, `medicine`, `transportation`, `shelter`. Locations contain
finite numeric `lat` (-90 to 90), `lon` (-180 to 180), and optional string `label`.
Request status updates permit cancellation before completion, `MATCHED` to
`IN_PROGRESS`, and `IN_PROGRESS` to `COMPLETED`. Matching assigns `MATCHED`;
clients cannot assign it directly. Closed requests cannot be rematched.

Errors: 400 invalid input, 404 missing record, 409 conflicting transition or CAS,
503 unavailable storage. Responses echo `X-Request-ID`. A broker outage after a
successful storage write still returns 201 with `queued: false`; the sweeper
retries publication. Matching uses attempt tokens and CAS to fence concurrent
claims; repeated matching and release do not allocate or restore capacity twice.

## Coordinator handoff contract

The application adapter expects the coordinator to expose:

- `GET /data/{key}`: storage envelope with `key`, JSON-string `value`, integer
  `version`, boolean `deleted`, and optional `updated_at`.
- `PUT /data/{key}`: body `{"value": "<JSON text>", "if_version": 0}` for
  creation, or the last observed version for an update. A metadata-only response
  with `key` and new `version` is supported; the adapter retains the submitted value.
- `GET /data?prefix=request:` and `GET /data?prefix=resource:`: a `records` array
  of storage envelopes. The application adapter filters resource types after decoding.
- `GET /readyz`: successful JSON response when storage routing is ready.

Pranav's node API already implements the storage operations at `/internal/data`.
Routing must preserve its string values and envelopes, surface conflicts as 409,
and missing records as 404. Network failures, server errors, and malformed success
responses become 503 at the application boundary. Existing Day 1 coordinator
readiness checks only registry startup; it must be extended alongside routing.

## Messaging and configuration

The durable `emergency` direct exchange routes `request.created` to `match.requests`
and `request.closed` to `match.released`. Failed searches use `match.retry` with a
TTL before redelivery; poison messages go to `match.dead`. Publication is confirmed
before acknowledging an original message that needs retry.

Shared defaults come from `common.config.Settings`: radius 50 km, five search
attempts, zero artificial matching delay, and a 30-second sweep interval.
`RETRY_DELAY_MS` defaults to 5000. Compose supplies its local RabbitMQ URL and
forwards the settings in `.env.example`. Changing retry TTL on an existing queue
requires a coordinated queue migration because RabbitMQ declarations must agree.
