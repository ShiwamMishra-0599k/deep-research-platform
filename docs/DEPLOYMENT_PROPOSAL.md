# Production deployment proposal (design only; NOT planned: the project runs on a zero budget)

> Under the owner's INR 0 rule nothing in this document will be deployed or paid for. It stays as a design and cost-analysis artifact that shows how the system would be hosted.

Status: **proposal only.** No AWS resource exists, no credential has been used, no charge has been incurred. Approval of the architecture
and the monthly cost below is required before any resource is created (see section 9).

Costs come from AWS's official public price-list files for Asia Pacific (Mumbai), read on 2026-11-15 (publication dates 2026-11-04 to 2026-11-14), and
are reproducible with `python deploy/aws/cost_estimate.py`. The INR figures use the ECB reference rate of 94.25 INR per USD (2026-11-13) and will move.

## 1. Options evaluated

| | A. Recommended | B. Hardened | C. Budget VM |
|---|---|---|---|
| Shape | ECS Fargate (ARM) x3 tasks, RDS PostgreSQL, ElastiCache Redis, ALB | A plus private subnets and NAT, Multi-AZ RDS, 2 backend and 2 agent tasks | One EC2 t4g.medium running Docker Compose, Caddy for TLS |
| **Monthly (USD)** | **$102.60** | $168.57 | $29.40 |
| **Monthly (INR)** | **about 9,670** | about 15,890 | about 2,770 |
| Managed backups and point-in-time recovery | Yes (RDS) | Yes | **No** (pg_dump or snapshots only) |
| Isolated `noeviction` Redis for rate limits | Yes (ElastiCache) | Yes | A second container on the same host |
| Private DB and Redis, no public exposure | Yes | Yes | Same host, firewall only |
| Survives one instance or AZ failure | No (single AZ database; tasks restart automatically) | Yes | No |
| Zero-downtime deploy and rollback | Yes (rolling, circuit breaker) | Yes | Brief downtime |
| Fits the brief's list (Secrets Manager, task roles, backup restore, alarms) | Fully | Fully | Partly |

**Recommendation: A.** It is the cheapest option that delivers everything requested. C is the right choice only if cost outranks recoverability, and it
gives up point-in-time recovery and managed isolation. B adds availability that a portfolio demo with no users does not need yet; it can be reached from A later.
x86 instead of ARM Fargate raises A to $120.75 (about 11,380 INR); ARM is the default but needs the arm64 images validated first (section 6).

## 2. Architecture (option A)

```
Internet --HTTPS--> ALB (ACM certificate, TLS 1.2+, HTTP redirects to HTTPS)
                      |  /api/*  -> backend (Fargate)         /* -> frontend (nginx, static build)
                      v
        VPC, 2 AZs.  Public subnets: ALB, Fargate tasks (inbound ONLY from the ALB security group)
                     Private subnets (no internet route): RDS PostgreSQL, ElastiCache Redis
backend --X-Internal-Token--> agent task (not behind the ALB; reachable only from the backend security group)
agent task = agent container + redis:7 sidecar (tool cache, allkeys-lru, disposable)
ElastiCache Redis = rate limits only (maxmemory-policy noeviction); RDS PostgreSQL 16 (gp3, 7-day automated backups, PITR)
Secrets Manager -> injected at task start through the task execution role; each task role can read only its own secrets
CloudWatch Logs + alarms; ECR for images; one hosted zone for the domain
```

Why tasks sit in public subnets: a NAT gateway costs $40.88 per month before traffic and is the largest single saving between A and B. The tasks get public IPv4
addresses ($3.65 each per month) but accept no inbound traffic except from the ALB. See the security trade-off in section 4.

## 3. Monthly cost, option A (USD, from `cost_estimate.py`)

| Line | USD |
|---|---|
| Fargate ARM: frontend 0.25 vCPU/0.5 GB, backend 0.25/0.5, agent plus cache sidecar 0.5/2 GB | 23.11 |
| RDS PostgreSQL db.t4g.micro single-AZ + 20 GB gp3 (backups within the free allocation) | 17.95 |
| ElastiCache Redis cache.t4g.micro (rate limits) | 14.60 |
| ALB (hourly plus 1 LCU assumed) | 23.29 |
| Public IPv4 addresses: 2 for the ALB, 3 for the tasks | 18.25 |
| Secrets Manager (7 secrets), CloudWatch Logs (2 GB) and 10 alarms, ECR | 5.40 |
| **Total** | **102.60 (about 9,670 INR)** |

**Not included:** LLM and search provider usage (unknown until the live evaluation measures it; this will probably be the largest variable cost), the domain name,
GST, the Route 53 hosted zone (about $0.50, list price not read from the price file), data transfer above the free tier, S3 copies, and the restore tests in section 7
(a few hours of a micro instance, cents).

**Assumptions to be replaced by measurements:** task sizes, 1 average ALB LCU, 2 GB of logs per month, 20 GB of data. Sizing is a guess until the load test in section 8; the agent
(LangGraph, PyMuPDF) is the likeliest to need more memory. Upgrading the agent to 1 vCPU and 2 GB adds about $9 per month.

## 4. Security implications

* **Network:** only the ALB is public. DB and Redis are in subnets with no internet route; security groups allow only the backend or agent task groups. The agent is not behind the ALB.
* **TLS:** ACM public certificate on the ALB (ACM public certificates are free per AWS documentation; this was not read from the price file), modern TLS policy, HTTP redirects to HTTPS, HSTS from the app. ElastiCache in-transit encryption means `REDIS_URL` must use `rediss://`; the code path is verified in the plan but not yet tested against a TLS Redis.
* **Secrets and IAM:** the seven secrets live in Secrets Manager and reach containers as injected environment variables; task roles are least-privilege and separate per service. The existing `ENVIRONMENT=production` validator refuses to start on weak or missing settings.
* **Trade-off in A:** tasks have public IPs and unrestricted outbound access (they must reach LLM and search providers). The defences are the SSRF guard (blocks private, loopback and link-local targets, and explicitly lists the ECS task-credentials endpoint 169.254.170.2 and the instance metadata address 169.254.169.254; covered by `test_url_guard`) and no inbound path. If you want egress control and no public task IPs, that is B (NAT, +$66 per month) and still would not filter destinations; a firewall that does costs more.
* **Not included, available on request:** AWS WAF (a few dollars per month plus rules), GuardDuty, a separate AWS account for production.
* **Known application gaps that remain after deployment:** `AUTH_MODE=dual` until you end the migration window, no email verification or password reset, expired sessions not purged (needs a scheduled task), no per-user quotas.

## 5. Application deployment and rollback

1. GitHub Actions (once the push is unblocked) builds `linux/arm64` images, runs the existing tests, pushes to ECR tagged with the commit SHA.
2. A one-off ECS task runs `python -m app.migrate` (advisory-locked, idempotent) before the service update; a manual RDS snapshot is taken first.
3. ECS rolling update with the deployment circuit breaker and automatic rollback; health checks use `/health/ready` (backend) and `/health` (agent). `stopTimeout` is raised so open SSE streams can finish (graceful shutdown to be verified).
4. **Rollback:** redeploy the previous task definition (one command); schema changes are additive, and the downgrade path is tested in `test_migrations`.
5. Credential rotation: use the RDS master-secret rotation in Secrets Manager; the app already builds its URL from `DB_*` components, so a rotation needs a task restart, not a code change. This is **not** the Docker-volume rotation script, which stays a local-development tool.

## 6. Things that must be verified before the first deploy

* **arm64 images (partly verified, `docs/PERFORMANCE.md` section 5).** Backend: builds and its native dependencies load. Agent: builds; PyMuPDF and numpy load on `aarch64`; the rest of its stack was not completed under emulation. A native arm64 CI job (free for public repositories) is the planned proof. Not relevant unless something is ever deployed.
* **SIGTERM (verified for the backend image, local Linux container; `docs/PERFORMANCE.md` section 3).** With open streams, SIGTERM stops new connections and in-flight streams finish (8 of 8, exit 0). The image now bounds the drain with `SHUTDOWN_GRACE_SECONDS` (default 25; keep it below the orchestrator's stop timeout, 30 s by default and at most 120 s on Fargate): overdue streams are cancelled, each leaves an "interrupted, please ask again" note, and the container exits 0 (measured 6.2 s) instead of being killed. A research can still last up to 300 s, so a deploy during one ends it, cleanly. Not yet tested for the agent image.
* `rediss://` and a Redis AUTH token work with the rate-limit client (the pool, warm-up and Lua scripts). **Still untested.** Redis behaviour under stalls is measured (`docs/PERFORMANCE.md` section 3a): the breaker is now half-open, so a sub-second Redis stall no longer causes seconds of refused requests in fail-closed mode (single user: 4.7 s of refusals before, none after). The 0.25 s command timeout remains tight and needs tuning against a real deployment's Redis.
* **Connection limits (measured and changed).** A load test found that the backend's connection pool (10 plus 10) was exhausted at 50 to 100 concurrent researches; this is fixed (an admission gate of 16 slots per backend process, below the pool of 20). Each backend task can therefore use up to 20 connections, plus 2 for the periodic session cleanup. `db.t4g.micro` derives `max_connections` from its 1 GiB of memory (read the real value from the instance). Confirm that 22 per backend task times the replicas, plus the migration task, stays well below it.
* **Sizing evidence is local only:** one backend and one agent worker each saturate about one CPU core at roughly 4.6 to 6.6 researches per second against a fake LLM, and the backend grew about 0.8 MB per open stream. Real model latency will change the picture; these figures only say where our own code runs out of CPU.

## 7. Backups and recovery

RDS automated backups with 7-day retention give point-in-time recovery; backup storage up to the provisioned size is free. **A configured backup is not evidence.** After the first deploy I will:
1. restore the latest snapshot and a point-in-time target to a **new, temporary** instance;
2. compare row counts and run the application smoke test against it;
3. record the restore time, then delete the temporary instance.

Disaster recovery (region loss) is out of scope for A: it is a single region and a single AZ database. The RPO is minutes (PITR) and the RTO is the restore time measured above.

## 8. Monitoring and validation

* Logs: structured application logs and ECS logs in CloudWatch, 30-day retention; no tokens, cookies, passwords or full queries are logged (tests exist for token and key leakage; a log-content review is part of validation).
* Alarms (10): ALB 5xx and unhealthy targets, ECS running count, RDS CPU, free storage and connections, ElastiCache memory and **evictions (must stay 0)**, and a billing alarm.
* **Gaps I will state honestly:** request-latency and LLM token and cost metrics do not exist in the application yet and must be added (token usage needs instrumentation in the agent). Error tracking beyond CloudWatch (for example Sentry) would send data to a third party and needs your decision.
* Load test: against the fake LLM to measure our own overhead and limits (concurrency, DB pool, Redis), at a deliberately modest scale; real-provider load is limited by cost and provider rate limits.
* Production validation: end-to-end run on the deployed URL (auth and cookie flags, research with citations, SSE through the ALB, persistence, error paths, security headers, TLS grade), plus the restore test. Real-provider runs use the spend-gated evaluation harness.

## 9. What I need from you before creating anything

1. **Approve** option A (or choose B or C or request changes) and the cost of about 102.60 USD (9,670 INR) per month plus provider usage.
2. **AWS access:** an account you own and a way for me to deploy without pasting long-term keys into chat (an SSO profile or an IAM role limited to this project). Confirm that I may create the resources listed in section 2. Set an AWS budget alert of your chosen amount.
3. **Region:** Mumbai (ap-south-1) is assumed for Indian users and these prices; confirm.
4. **Domain name** and where DNS is managed.
5. **Provider keys and a spend cap** for the live evaluation and production (OpenRouter or OpenAI, and Exa or Tavily); I will not use any key without a stated limit.
6. Decisions on error tracking (CloudWatch only versus a third-party service) and whether to add WAF.

Until then I will keep preparing what needs no spending: infrastructure-as-code (Terraform) and deployment scripts as reviewable files, the application changes in section 6, and the load-test harness.
