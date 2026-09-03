# Phase A — Rate Limiting Algorithms (Plain Python)

Before adding Redis or Django, each rate-limiting algorithm was
implemented and tested in isolation using plain Python (in-memory
dictionaries, no shared/distributed state). The goal was to fully
understand each algorithm's behavior — including its specific
weaknesses — before introducing any distributed-systems complexity.

## Algorithms

| Algorithm | Summary | Notes |
|---|---|---|
| Fixed Window | Counter reset per fixed clock slot | [notes/fixed-window.md](notes/fixed-window.md) |
| Sliding Window | Rolling log of request timestamps | [notes/sliding-window.md](notes/sliding-window.md) |
| Token Bucket | Continuously refilling token pool, allows bursts | [notes/token-bucket.md](notes/token-bucket.md) |

Each notes file covers: how the algorithm works, the code, tests run
to verify correctness, and (for fixed/sliding window) a concrete
exploit or edge case demonstrating why the algorithm behaves the way
it does.

## Files

| File | Purpose |
|---|---|
| `fixed_window_baseline.py` | Basic correctness test — 8 requests, limit=5 |
| `fixed_window_exploit.py` | Proves the window-boundary exploit (10 requests allowed across 2 windows) |
| `sliding_window_baseline.py` | Basic correctness test, same shape as fixed window |
| `sliding_window_full_gap.py` | Full-window idle gap between batches (fresh quota — expected) |
| `sliding_window_partial_gap.py` | Partial idle gap — proves sliding window closes the boundary exploit |
| `token_bucket_baseline.py` | Burst test — bucket starts full, first 5 allowed |
| `token_bucket_refill.py` | Refill timeline test — fractional token accumulation over time |

## Next step

These algorithms are migrated to Redis in [`../redis_limiter/`](../redis_limiter/),
where shared state across multiple processes introduces a real race
condition — and the fix for it.