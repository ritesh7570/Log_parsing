# Technical Decisions

## Why ZIP uploads are streamed
The system accepts large archives that may be several gigabytes in size. Streaming uploads avoids unbounded memory usage, supports chunked client connections, and allows the server to reject oversized payloads early.

## Why log parsing is line-by-line
Large log files can exceed memory limits when read wholesale. A line-by-line parser preserves exact source-line tracking, keeps memory bounded, and matches the project's need for timestamp, level, function, malformed-line analysis, and platform-specific bracketed log formats without materializing all records in-memory.

## Why unsupported components are filtered before parsing
The Phase 1 scope deliberately limits processing to DriverWrapper, DServer, and OTX paths. Excluded directories such as CONXApp and ServerAPIConnector are retained only for metadata visibility and are not passed to the normal parser or fingerprint pipeline. This prevents false positives, avoids unsupported formats, and keeps error clusters aligned to the verified scope.

## Why fingerprint clustering uses disk-backed aggregation
The original grouping logic is deterministic and correct. However, retaining every raw record and occurrence list in a Python dictionary is unsafe for multi-GB workloads. SQLite keeps the semantic behavior while reducing memory pressure and preserving global aggregation across multiple files and components.

## Why Neptune is disabled initially
Neptune is explicitly out of scope for this milestone. The project intentionally keeps the pipeline deterministic and local-first to focus on ZIP discovery, parsing, and clustering without dependencies on external infrastructure.

## Why vendor report generation is excluded
This project provides log parsing and error clustering infrastructure. Vendor report generation and RCA workflows are separate concerns and are deliberately not implemented here.

## How component provenance is preserved
Each occurrence retains component metadata, source archive path, source file, timestamp, thread ID, and line number. This preserves traceability even when identical errors are observed in multiple components or files.
