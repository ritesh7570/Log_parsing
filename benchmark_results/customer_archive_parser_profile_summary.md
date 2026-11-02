# Customer archive parser profile

Profiled the unmodified sequential path with cProfile using `customer_sample_upload.zip`.
The profiled run processed 558 files, 8,908,689 records, 1,180 ERROR occurrences, and 17 clusters. cProfile recorded 722,531,797 calls and 1,339.2 seconds; this duration is profiler-inflated and is not a benchmark comparison.

## Ranked parser costs

Times below are cProfile cumulative time unless marked self time. Parser functions are nested, so their cumulative times overlap and must not be added.

| Rank | Function / operation | Profile cost | Evidence |
|---:|---|---:|---|
| 1 | `iter_log_records` | 1,220.5 s cumulative | 8,909,247 calls; top-level line parsing and record assembly |
| 2 | `split_function_and_message` | 541.8 s cumulative; 39.4 s self | 8,908,689 calls; includes repeated regex search |
| 3 | `to_iso_timestamp` | 443.8 s cumulative; 21.2 s self | 8,908,689 calls |
| 4 | Regex `Pattern.search` | 475.7 s self | 18,333,934 calls across parser regex work |
| 5 | `datetime.strptime` / `_strptime` | 390.3 s cumulative; `_strptime` 168.5 s self | 17,817,378 calls, about two per parsed record |
| 6 | `LogRecord` dataclass construction | 40.2 s self | 8,908,689 objects |
| 7 | Regex `Pattern.match` | 38.5 s self | 27,163,834 calls |
| 8 | String `strip` | 25.6 s self | 98,218,864 calls process-wide |

Log-level extraction and both line-format regex matches are inside `iter_log_records`; individual `match` self time is much smaller than function splitting and timestamp conversion. File open calls accounted for 8.2 s self time. The C-level text iteration and decoding are not separately attributed by cProfile, so they remain inside `next()` / `iter_log_records` time; the profile does not support treating disk I/O as the leading parser cost.

## Confirmed changes selected

- Timestamp strings in the archive use fixed-width `YYYY-MM-DD HH:MM:SS.mmm`. A direct `datetime` construction fast path avoids the failed first `strptime` format and locale lookup for that common case; the original format loop remains as fallback.
- The operator regex requires the literal `operator`, and qualified-function parsing requires `::`. Cheap substring checks skip regex calls that cannot match while retaining existing regexes and their precedence.
- Candidate suffix extraction now uses `rsplit` and avoids rechecking an identical payload.

Artifacts: `customer_archive_parser.prof`, the full ranked `customer_archive_parser_profile.txt`, and `parser_profile_run.json`.

## Validation

The existing suite passed before changes and after optimization (13 passed, one existing Starlette deprecation warning). A streaming differential pass against `HEAD:Backend/parse_logs.py` also matched all 8,908,689 normalized records and malformed-line callbacks across all 558 files; it retained no full-record list.
