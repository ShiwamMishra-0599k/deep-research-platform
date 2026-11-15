# Portfolio evidence audit: what convinces a technical reviewer, and what is still missing

Written for the owner. It lists what a recruiter or interviewer can verify today, the gaps ranked by how much they weaken the project, what has been done about each, and who has to act. Nothing here is a claim about users, deployment or model quality: none exist.

## What a reviewer can verify in ten minutes

| Step | Where | What it shows |
|---|---|---|
| 1 | [`README.md`](../README.md) | What it is, the CI badge, and a verification table that says which claims are proven and which are not |
| 2 | [`CASE_STUDY.md`](CASE_STUDY.md) | Decisions with trade-offs and three defects found by measurement, with the wrong theories and corrections |
| 3 | [`DIAGRAMS.md`](DIAGRAMS.md) | Seven architecture and behaviour diagrams (all validated with Mermaid's parser) |
| 4 | [`API.md`](API.md) + [`openapi.json`](openapi.json) | API reference that fails the build if it drifts |
| 5 | [`PERFORMANCE.md`](PERFORMANCE.md) | Measured limits, with run-to-run spread and what was not measured |
| 6 | GitHub Actions | Lint, security scans, backend, agent, frontend, Docker and a native ARM64 job, all green on the release branch |
| 7 | `python scripts/ci_local.py <stage>` | The same checks, reproducible locally |

## Gaps, ranked

| # | Gap | Why it matters | Status | Who acts |
|---|---|---|---|---|
| 1 | **No real language-model answer-quality result.** The benchmark (29 questions), grading code and plan exist; no model has answered them | For AI Engineer roles this is the single most expected piece of evidence. "I built an agent" without an evaluation invites the question "how do you know it works?" | Not done. Zero-budget route: a small local model through Ollama (about 3 to 4 GB download, slow on this machine, hours for the full set). The result would be labelled with the model, hardware and date, with confidence intervals | **You: approve installing Ollama and one model.** Then I run a pilot of 3 cases |
| 2 | **The default branch shows old work.** `main` has the original code plus one deletion; all hardening, tests, CI and documentation are on `release-readiness/ci-validation` | A reviewer opens the repository on `main`. They will not see the CI badge's workflow, the tests or the documents unless they find the branch | Branch is complete and green | **You: authorise the merge** (or a pull request to review). I will not touch `main` without that |
| 3 | **No LICENSE file** | Without a licence the code is "all rights reserved"; many reviewers and companies will not read or reuse it, and it signals unfamiliarity with open source | Not added: it is a legal choice | **You: choose a licence** (MIT is the usual portfolio choice) |
| 4 | **No way to run it without provider keys** | A reviewer who clones the repository cannot see it work unless they have keys | Planned: a keyless demo mode with the scripted model, clearly labelled as scripted; a real-model mode via Ollama once approved | I can build the scripted demo now |
| 5 | **No demo video or walkthrough script** | Interviewers often prefer a 2-minute screen recording to cloning anything | Script written ([`DEMO_SCRIPT.md`](DEMO_SCRIPT.md)); recording needs a person | **You: record it** after #1 or #4 |
| 6 | **Screenshots come from the scripted model** | Honest and labelled in the README, but not real research output | Replace after #1 | After #1 |
| 7 | `docs/PROJECT_OVERVIEW.md` is stale (its own banner says it predates the hardening) | Out-of-date documents undermine the up-to-date ones | Not changed: it is already on `main`, and removing a public file needs your approval | **You: refresh or remove** |
| 8 | GitHub repository settings: description, topics, pinned repository | First impression on the profile page | Not touched: these are account settings | **You** |
| 9 | Benchmark reference facts are author-written | A reviewer may ask who checked them | Documented as needing your spot-check before any result is published | **You: spot-check the 29 cases** |
| 10 | Multi-worker scaling and long soak tests | Shows the limits beyond one process | Harness modes built (`--replicas`, `--soak-seconds`); measurements were disrupted by the host running out of memory; see `PERFORMANCE.md` | I can retry when the machine is calmer |

## Claims to avoid in interviews and on the CV

Do not say or imply: that Lumen is deployed, has users or traffic, was benchmarked against a real model, scales to any number of users, saves any money, or has any accuracy figure for answers.
What can be said accurately: it is a tested, documented, locally verified system; it has a working CI pipeline; load testing found and fixed three real defects (database pool deadlock, circuit-breaker amplification, shutdown hang), each with a regression test; the evaluation method is designed and the answer-quality run is pending a free local model.

## Interview preparation stays out of the repository

Talking points and CV bullets for this project belong in a private document, not in the public repository (the earlier interview-preparation notes were removed from `main` at your request). If you want them, say so and they will be written outside the repository.
