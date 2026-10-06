# Benchmark results (2026-10-06)

## Politeness under scale-out

12 wants, cold cache, rate limit 0.2s (5 req/s), fake latency 80 ms.

| design | workers | sweep (s) | calls to Wizz | peak req/s | mean req/s |
|---|---:|---:|---:|---:|---:|
| shared | 1 | 21.69 | 109 | 6 | 5.02 |
| shared | 2 | 22.12 | 111 | 6 | 5.02 |
| shared | 4 | 22.71 | 114 | 6 | 5.02 |
| shared | 8 | 23.28 | 117 | 6 | 5.03 |
| naive | 1 | 22.06 | 109 | 5 | 4.94 |
| naive | 2 | 11.79 | 114 | 10 | 9.67 |
| naive | 4 | 7.12 | 118 | 20 | 16.58 |
| naive | 8 | 4.38 | 126 | 40 | 28.78 |

## Pipeline throughput with a warm cache

200 jobs over 40 wants, every timetable already cached: only queue, CPU and database work is left.

| workers | sweep (s) | jobs/s | speed-up | calls to Wizz |
|---:|---:|---:|---:|---:|
| 1 | 1.57 | 127.69 | 1.00× | 0 |
| 2 | 1.41 | 141.57 | 1.11× | 0 |
| 4 | 1.35 | 148.67 | 1.16× | 0 |
| 8 | 1.49 | 134.00 | 1.05× | 0 |

## Recovery from worker crashes (SQLite backend)

4 workers, a random one SIGKILLed every 0.6s, lease 4s.

| kills | mid-job | jobs | lost | dead-lettered | completed twice | candidates vs clean run | recovery p50 / p95 / max (s) |
|---:|---:|---:|---:|---:|---:|---|---|
| 51 | 24 | 36 | 0 | 0 | 0 | 965 identical (0 missing, 0 extra) | 4.22 / 7.81 / 7.97 |

## AWS backend (SQS + DynamoDB on moto)

A sweep of 10 wants, as every 15 minutes: 97 calls to Wizz (the timetable cache has expired), 668.00 DynamoDB write units and 209.00 read units (most candidates already known), 20 SQS requests. The very first sweep writes 719.00 units.

Chaos: 28 kills (11 mid-job) over 20 jobs — 0 lost, 0 completed twice, 0 dead-lettered.

Estimated monthly cost, eu-central-1, sweeping every 15 minutes:

| worker concurrency | Lambda GB-s | list price | with always-free tier |
|---:|---:|---:|---:|
| 1 | 113,609 | $4.17 | $1.56 |
| 2 | 218,369 | $5.57 | $1.56 |
