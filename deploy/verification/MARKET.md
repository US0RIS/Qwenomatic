# Constrained-market verification — October 3, 2026

Status: implementation and local simulation mechanisms verified. Real-world
business validity remains unestablished. No feature or market assumption was
promoted on the basis of simulated profit.

## Local checks

`market-tests.txt` records **318 passed, 1 skipped, 3 deselected**. The skipped
test is the separate full-default evidence campaign, which was then run
explicitly and passed. Three existing tests cannot run in this workspace:
the protected-venv ownership test requires writable `/run`, and two inference
transport tests require listening sockets. GitHub CI runs those on its normal
Linux/Windows runners and separately exercises the Linux kernel and broker.
Local simulation tests use the repository's existing test-only empty network
boundary. These receipts do not claim local kernel or actual Qwen validation.

JavaScript syntax and `git diff --check` also passed.

New checks cover shared finite customers; immutable market history; rollback
and restart of cash, demand and delivery capacity; independent market draws;
lagged surveys; adverse competition/quality; finite working capital; late
refunds and dispute fees; defaults without fake revenue; reversal of pending
invoice postings; imputed compute versus cash; full 17/2/1 operation; matched
campaigns; chronological/customer holdout; holdout non-interference; censoring;
and malformed or duplicate evidence.

## Full default experiment

`market-default-campaign.json` contains the predeclared plan, SHA-256 inventory
of all 88 runtime/configuration source files, both arm summaries and full audit
results. Every inventoried file matched the final local tested source.

- Seed: 20261003.
- Active duration: two 24-hour generations per arm, 600-second ticks.
- Population: 17 business, 2 research, 1 red team in both arms.
- Backend: scripted policy emulator; no actual Qwen inference.
- Arms: normal evolution versus an unchanged initial population.
- All pending payments and refunds were settled after the active window.
- Treatment net: **−571.362558 simulated USD**.
- Control net: **−959.492675 simulated USD**.
- Post-baseline difference: **+388.130117 simulated USD**.
- Pre-treatment baseline difference: zero.
- Both ledgers passed hash-chain, accounting and selection replay.
- Both arms retained the required role counts; no policy violations or step exceptions.

This one seed produces no confidence interval. It shows that evolution reduced
the modeled loss in this run. It does not show profitability or establish what
real customers will do. Sales, quality, delivery effort and most other market
inputs remain assumptions even though payment fees and labor costs have cited
reference anchors.

The local original ledgers were retained during verification. The
`market-evidence` CI job reproduces this full default comparison and uploads the
plan, reports, both original ledgers and nested research ledgers as a downloadable
artifact for 30 days. Tests and commands to reproduce it are in `MARKET.md`.
