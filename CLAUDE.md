# CLAUDE.md

Follow [`AGENTS.md`](AGENTS.md) — it is the source of truth for rules, commands and security invariants.

Quick reminders for Claude Code sessions in this repo:

- Frontend design is **frozen** (`DESIGN.md`); only functional/security/a11y/integration fixes there.
- Python envs live in `backend/.venv` and `research-agent/.venv` (Python 3.12). Use the venv interpreter, not the system Python 3.14.
- Tests need the isolated services from `AGENTS.md` (ports 55432 / 56379). Never touch the compose volumes.
- Docker Desktop may need to be started first; other projects on this machine use ports 8004/8080/5433/6379.
- Track work in `TASKS.md`, document findings in `SECURITY_REVIEW.md`, and keep `TEST_PLAN.md` honest about what was and was not tested.
