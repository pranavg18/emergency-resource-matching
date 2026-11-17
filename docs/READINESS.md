# Submission readiness — 17 November 2026

**Ready-by cutoff: Wednesday, 18 November 2026. Submission: Thursday, 19 November.**
The previous 20 November date is superseded by this schedule.

## Current state

Aksha's application layer is being submitted for Pranav's review on top of his
storage/registry branch. Pranav's storage implementation retains its API contract,
configuration, ports, tests, and commit history. Application unit tests use a fake
storage implementation where a live coordinator is not needed.

The accepted stack is still incomplete: coordinator `/data` routing, replication,
failure handling, recovery sync, and compatible Kubernetes deployment remain to
be integrated. This document records a plan, not a claim that live acceptance has
passed. Docker Desktop's Linux engine was unavailable during the initial
17 November check.

The local full prototype is preserved on `codex/day-2-implementation` at `523e766`.
Use it to inform implementation and tests, but do not replace Pranav's committed
storage code wholesale or reuse its earlier test results as evidence for this
branch. The prototype and reviewed branch have different storage contracts.

## Review order

1. Review and approve Pranav's [storage PR #1](https://github.com/pranavg18/emergency-resource-matching/pull/1) against its exact current revision.
2. Merge #1 only after its required approval and checks are satisfied. Preserve
   individual commits with a merge commit or rebase, as the implementation plan
   specifies.
3. Keep Aksha's application PR based on `pranav` while #1 is open. After #1 merges,
   retarget it to `main`, check the resulting diff, and rerun checks if its revision
   changes.
4. Pranav reviews Aksha's application PR. Approval and passing checks are required
   before merge; no automatic merge is authorized by this checklist.
5. Apply the same reciprocal review gate to storage completion and deployment PRs.

## Validation recorded on 17 November

- Ruff passed; Compose configuration validation passed.
- Unit suite: **73 passed**, including 21 storage adapter tests. These exercise
  Pranav's real Flask/SQLite API, with coordinator routing simulated by a test
  transport. One test covers the full create/match/complete/release lifecycle.
- CLI and checker help commands run successfully with the documented ports.
- Pranav's PR #1 was approved at `0869818`; no merge was performed.
- Linux process tests must pass in GitHub Actions on the proposed branch revision.
  Full live Compose/Kubernetes acceptance remains outstanding.

## Work to finish by Wednesday

| Work | Driver / reviewer | Completion evidence |
|---|---|---|
| Application layer and storage adapter | Aksha / Pranav | Reviewable commits; unit tests; HTTP contract tests for opaque JSON values, CAS conflicts, and upstream errors |
| Coordinator routing and scatter-gather scans | Pranav / Aksha | Create/get/update through the public API; keys distributed across three shards; adapter and coordinator agree on envelopes |
| Replication, failure detection, failover, recovery sync | Pranav / Aksha | Primary outage permits reads/writes through an eligible replica; restart restores identical contents; stale replicas are not promoted |
| Application/RabbitMQ/Ingress Kubernetes resources | Aksha / Pranav | Images built from the reviewed revision; working routes, probes, environment, secrets, and persistent broker storage |
| Coordinator/storage Kubernetes resources | Pranav / Aksha | One coordinator, three shards, six storage nodes, persistent volumes, bootstrap and readiness verified |
| Combined demo and documentation | Both; one runs, the other verifies | Nine required demonstrations pass; evidence and commands match the final merged revision |

**Wednesday morning:** clear the two initial reviews and finish coordinator routing.
Run the public API core path immediately so contract mismatches surface before
failure testing.

**Wednesday afternoon:** integrate replication/recovery and the compatible
Kubernetes deployment. Run concurrency, broker outage, storage failure/recovery,
pod replacement, and scaling checks against the combined revision.

**Wednesday evening:** freeze the submission candidate only when required reviews,
checks, and demonstrations are complete. Commit any remaining fixes and evidence,
have the other contributor review them, and rehearse the submission from the final
revision. An unmet gate remains a blocker; the date alone does not establish readiness.

## Consistency gates for every PR

- Base the branch on the reviewed predecessor and inspect the final diff. Preserve
  each contributor's commits; avoid force-pushing shared branches.
- Storage values remain opaque JSON strings on the wire. Application code works
  with decoded objects through the adapter. CAS uses `if_version`; scans expose a
  `records` collection. Confirm error mapping and readiness behavior together.
- Keep Compose ports `8088`, `8008`, and `15678`, its existing project name, and
  storage configuration aligned with README and client commands. Avoid changing
  database files or volumes to make a test pass.
- Run Ruff, isolated unit tests, Linux process integration tests, and Compose
  configuration validation on the actual proposed revision. GitHub Actions checks
  must refer to that revision, not an earlier commit.
- Full-stack claims require a live run on the combined code. Record the revision,
  environment, command, outcome, and any limitations alongside the evidence.

## Submission acceptance

The final rehearsal must demonstrate request creation, resource registration,
matching, shard distribution, asynchronous processing, storage failure, recovery,
pod recovery, and scaling. After concurrent matching, verify reciprocal
assignments, unique claims, and nonnegative availability. Confirm public Ingress
access from the machine used for the presentation.

Keep screenshots/logs and the exact reproducible commands with the final revision.
Do not claim completion based only on unit tests, a healthy gateway, or the old
prototype. Thursday should be reserved for final verification and submission;
required implementation and review work should be complete on Wednesday.
