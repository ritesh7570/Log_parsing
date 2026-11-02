from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

LEGACY_LOG_LINE = re.compile(
    r"^(?P<timestamp>\d{2}-\d{2}-\d{4}_\d{2}:\d{2}:\d{2}\.\d{3})\s*\|"
    r"\s*(?P<perf_timer>[^|]+?)\s*\|"
    r"\s*(?P<thread_id>[^|]+?)\s*\|"
    r"\s*(?P<level>[^|]+?)\s*\|"
    r"\s*(?P<payload>.*)$"
)
COMPONENT_LOG_LINE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s*\[(?P<component>[^\]]+)\]\s*\[(?P<level>[^\]]+)\]\s*\|\|"
    r"\s*(?:ThreadID:\s*(?P<thread_id>[^|]+?)\s*\|\|\s*)?(?P<payload>.*)$"
)

OPERATOR_FUNCTION = re.compile(r"(?P<function>.+?::operator\s*\(\))(?P<message>.*)$")
QUALIFIED_FUNCTION = re.compile(
    r"(?P<function>(?:[A-Za-z_~][A-Za-z0-9_~<>]*::)+"
    r"[A-Za-z_~][A-Za-z0-9_~<>]*)"
    r"(?:(?:\s+)|(?::(?=[A-Za-z])))?"
    r"(?P<message>.*)$"
)


@dataclass(frozen=True)
class LogRecord:
    record_number: int
    source_line: int
    timestamp: str
    timestamp_iso: str
    perf_timer: float | None
    thread_id: str
    level: str
    component: str
    qualified_function: str
    function_name: str
    class_scope: str
    message: str
    raw_message: str
    raw_line: str


def parse_metadata(line: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for part in line.strip().split("\t"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip().lower().replace(" ", "_")
        metadata[key] = value.strip()
    return metadata


def split_function_and_message(payload: str) -> tuple[str, str]:
    payload = payload.strip()
    if not payload:
        return "", ""

    candidates = [payload]
    if "||" in payload:
        final_candidate = payload.rsplit("||", 1)[-1]
        if final_candidate != payload:
            candidates.append(final_candidate)

    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        # The operator pattern requires this literal, so skip its regex search
        # for payloads that cannot possibly match it.
        if "operator" in candidate:
            operator_match = OPERATOR_FUNCTION.search(candidate)
            if operator_match:
                message = re.sub(r"^\s*[-:]\s*", "", operator_match.group("message"))
                return (
                    operator_match.group("function").strip(),
                    message,
                )

        # Qualified functions require at least one scope separator.
        if "::" in candidate:
            function_match = QUALIFIED_FUNCTION.search(candidate)
            if function_match:
                message = re.sub(r"^\s*[-:]\s*", "", function_match.group("message"))
                return (
                    function_match.group("function").strip(),
                    message,
                )

    return "", payload


def to_iso_timestamp(value: str) -> str:
    # Customer logs use this fixed-width format. Constructing the datetime
    # directly avoids strptime's locale lookup and format fallback per record.
    if (
        len(value) == 23
        and value[4] == "-"
        and value[7] == "-"
        and value[10] == " "
        and value[13] == ":"
        and value[16] == ":"
        and value[19] == "."
        and value[0:4].isdigit()
        and value[5:7].isdigit()
        and value[8:10].isdigit()
        and value[11:13].isdigit()
        and value[14:16].isdigit()
        and value[17:19].isdigit()
        and value[20:23].isdigit()
    ):
        try:
            parsed = datetime(
                int(value[0:4]), int(value[5:7]), int(value[8:10]),
                int(value[11:13]), int(value[14:16]), int(value[17:19]),
                int(value[20:23]) * 1000,
            )
        except ValueError:
            pass
        else:
            return parsed.isoformat(timespec="milliseconds")

    for fmt in ("%d-%m-%Y_%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.isoformat(timespec="milliseconds")
        except ValueError:
            continue
    raise ValueError(f"Unsupported timestamp format: {value!r}")


def iter_log_records(source: str | Path | TextIO, *, on_malformed: Any | None = None) -> Iterator[LogRecord]:
    file_handle: TextIO | None = None
    close_handle = False

    if isinstance(source, (str, Path)):
        file_handle = Path(source).open("r", encoding="utf-8-sig", errors="replace")
        close_handle = True
    else:
        file_handle = source

    try:
        metadata: dict[str, Any] = {}
        record_number = 0
        for source_line, raw in enumerate(file_handle, start=1):
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue

            if source_line == 1 and "IP Address:" in line:
                metadata.update(parse_metadata(line))
                continue

            if "Date_Time" in line and "MessageType" in line and "Message" in line:
                continue

            match = COMPONENT_LOG_LINE.match(line)
            if match is None:
                match = LEGACY_LOG_LINE.match(line)
            if match is None:
                if on_malformed is not None:
                    on_malformed({"source_line": source_line, "raw_line": line})
                continue

            values = match.groupdict()
            payload = values.get("payload") or ""
            qualified_function, message = split_function_and_message(payload)
            scopes = qualified_function.split("::") if qualified_function else []
            function_name = scopes[-1] if scopes else ""
            class_scope = "::".join(scopes[:-1]) if len(scopes) > 1 else ""

            if values.get("perf_timer") is not None:
                try:
                    perf_timer = float(values["perf_timer"].strip())
                except ValueError:
                    perf_timer = None
            else:
                perf_timer = None

            record_number += 1
            yield LogRecord(
                record_number=record_number,
                source_line=source_line,
                timestamp=values["timestamp"].strip(),
                timestamp_iso=to_iso_timestamp(values["timestamp"].strip()),
                perf_timer=perf_timer,
                thread_id=(values.get("thread_id") or "").strip(),
                level=(values.get("level") or "").strip().upper(),
                component=(values.get("component") or "").strip(),
                qualified_function=qualified_function,
                function_name=function_name,
                class_scope=class_scope,
                message=message.strip(),
                raw_message=payload.strip(),
                raw_line=line,
            )
    finally:
        if close_handle and file_handle is not None:
            file_handle.close()


def parse_log_file(path: str | Path) -> tuple[dict[str, Any], list[LogRecord], dict[str, Any]]:
    log_path = Path(path)
    metadata: dict[str, Any] = {"source_file": log_path.name}
    records: list[LogRecord] = []
    malformed_lines: list[dict[str, Any]] = []
    header_seen = False

    with log_path.open("r", encoding="utf-8-sig", errors="replace") as source:
        for source_line, raw in enumerate(source, start=1):
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue

            if source_line == 1 and "IP Address:" in line:
                metadata.update(parse_metadata(line))
                continue

            if "Date_Time" in line and "MessageType" in line and "Message" in line:
                header_seen = True
                continue

            match = COMPONENT_LOG_LINE.match(line)
            if match is None:
                match = LEGACY_LOG_LINE.match(line)
            if match is None:
                malformed_lines.append({"source_line": source_line, "raw_line": line})
                continue

            values = match.groupdict()
            payload = values.get("payload") or ""
            qualified_function, message = split_function_and_message(payload)
            scopes = qualified_function.split("::") if qualified_function else []
            function_name = scopes[-1] if scopes else ""
            class_scope = "::".join(scopes[:-1]) if len(scopes) > 1 else ""

            if values.get("perf_timer") is not None:
                try:
                    perf_timer = float(values["perf_timer"].strip())
                except ValueError:
                    perf_timer = None
            else:
                perf_timer = None

            records.append(
                LogRecord(
                    record_number=len(records) + 1,
                    source_line=source_line,
                    timestamp=values["timestamp"].strip(),
                    timestamp_iso=to_iso_timestamp(values["timestamp"].strip()),
                    perf_timer=perf_timer,
                    thread_id=(values.get("thread_id") or "").strip(),
                    level=(values.get("level") or "").strip().upper(),
                    component=(values.get("component") or "").strip(),
                    qualified_function=qualified_function,
                    function_name=function_name,
                    class_scope=class_scope,
                    message=message.strip(),
                    raw_message=payload.strip(),
                    raw_line=line,
                )
            )

    report = {
        "header_seen": header_seen,
        "parsed_record_count": len(records),
        "malformed_line_count": len(malformed_lines),
        "malformed_lines": malformed_lines,
        "level_counts": {},
        "blank_function_count": sum(1 for record in records if not record.qualified_function),
    }
    for record in records:
        report["level_counts"][record.level] = report["level_counts"].get(record.level, 0) + 1
    return metadata, records, report


def filter_errors(records: Iterable[LogRecord]) -> list[LogRecord]:
    return [record for record in records if record.level == "ERROR"]


def write_jsonl(path: Path, records: Iterable[LogRecord]) -> None:
    with path.open("w", encoding="utf-8") as output:
        for record in records:
            output.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse real DWrapper logs and retain only ERROR records.")
    parser.add_argument("--log", required=True)
    parser.add_argument("--output-directory", required=True)
    args = parser.parse_args()

    output = Path(args.output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)

    metadata, records, report = parse_log_file(args.log)
    errors = filter_errors(records)
    report["error_record_count"] = len(errors)

    write_jsonl(output / "parsed_records.jsonl", records)
    write_jsonl(output / "errors.jsonl", errors)
    (output / "run_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (output / "parse_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("========== TASK 29: LOG PARSING ==========")
    print(f"Parsed records: {len(records)}")
    print(f"ERROR records: {len(errors)}")
    print(f"Malformed lines: {report['malformed_line_count']}")
    print(f"Blank function identities: {report['blank_function_count']}")
    print(f"Output directory: {output}")
    print("==========================================")


if __name__ == "__main__":
    main()
