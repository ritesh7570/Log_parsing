from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable

UUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s|]+")
HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
QUOTED = re.compile(r"(['\"])(?:\\.|(?!\1).)*\1")
POINTER = re.compile(r"\b[0-9a-fA-F]{12,16}\b")
INTEGER = re.compile(r"(?<![A-Za-z_])[-+]?\d+(?![A-Za-z_])")
WHITESPACE = re.compile(r"\s+")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as source:
        for line in source:
            if line.strip():
                records.append(json.loads(line))
    return records


def normalize_message(message: str) -> str:
    value = message.strip()
    value = UUID.sub("<UUID>", value)
    value = WINDOWS_PATH.sub("<PATH>", value)
    value = HEX.sub("<HEX>", value)
    value = POINTER.sub("<POINTER>", value)
    value = QUOTED.sub("<VALUE>", value)
    value = INTEGER.sub("<N>", value)
    value = WHITESPACE.sub(" ", value)
    return value.strip()


def make_fingerprint(record: dict[str, Any]) -> tuple[str, str]:
    qualified_function = record.get("qualified_function") or "<UNRESOLVED_FUNCTION>"
    normalized_message = normalize_message(record.get("message") or "")
    return qualified_function, normalized_message


def fingerprint_id(key: tuple[str, str]) -> str:
    digest = hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()
    return f"fp_{digest[:24]}"


class SQLiteClusterStore:
    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            handle, temp_name = tempfile.mkstemp(prefix="log_clusters_", suffix=".db")
            Path(temp_name).touch()
            self._owned = True
            self.db_path = temp_name
            self._temp_handle = handle
        else:
            self.db_path = str(db_path)
            self._owned = False
            self._temp_handle = None

        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS error_clusters (
                fingerprint_id TEXT PRIMARY KEY,
                qualified_function TEXT NOT NULL,
                function_name TEXT,
                class_scope TEXT,
                normalized_message TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL,
                first_timestamp TEXT,
                last_timestamp TEXT,
                representative_error TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS error_occurrences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint_id TEXT NOT NULL,
                component TEXT,
                source_file TEXT,
                source_line INTEGER,
                timestamp TEXT,
                thread_id TEXT,
                message TEXT,
                raw_message TEXT,
                FOREIGN KEY(fingerprint_id) REFERENCES error_clusters(fingerprint_id)
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_error_occurrences_fingerprint ON error_occurrences(fingerprint_id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_error_occurrences_component ON error_occurrences(component)"
        )
        self.conn.commit()

    def add_occurrence(self, record: dict[str, Any], *, component: str = "UNKNOWN") -> None:
        if record.get("level") != "ERROR":
            raise ValueError("Task 30 received a non-ERROR record.")

        key = make_fingerprint(record)
        fingerprint = fingerprint_id(key)
        self.conn.execute(
            """
            INSERT INTO error_clusters (
                fingerprint_id, qualified_function, function_name, class_scope,
                normalized_message, occurrence_count, first_timestamp, last_timestamp,
                representative_error
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(fingerprint_id) DO UPDATE SET
                occurrence_count = occurrence_count + 1,
                first_timestamp = MIN(COALESCE(first_timestamp, excluded.first_timestamp), excluded.first_timestamp),
                last_timestamp = MAX(COALESCE(last_timestamp, excluded.last_timestamp), excluded.last_timestamp),
                representative_error = COALESCE(representative_error, excluded.representative_error)
            """,
            (
                fingerprint,
                key[0],
                record.get("function_name") or "",
                record.get("class_scope") or "",
                key[1],
                record.get("timestamp_iso"),
                record.get("timestamp_iso"),
                json.dumps(record, ensure_ascii=False),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO error_occurrences (
                fingerprint_id, component, source_file, source_line, timestamp, thread_id, message, raw_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                component,
                record.get("source_file") or "",
                record.get("source_line") or 0,
                record.get("timestamp_iso") or record.get("timestamp"),
                record.get("thread_id") or "",
                record.get("message") or "",
                record.get("raw_message") or "",
            ),
        )

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
        if self._temp_handle is not None:
            try:
                import os
                os.close(self._temp_handle)
            except OSError:
                pass
            if self._owned and Path(self.db_path).exists():
                Path(self.db_path).unlink(missing_ok=True)

    def export_clusters(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT c.fingerprint_id, c.qualified_function, c.function_name, c.class_scope,
                   c.normalized_message, c.occurrence_count, c.first_timestamp, c.last_timestamp,
                   c.representative_error
            FROM error_clusters c
            ORDER BY c.occurrence_count DESC, c.qualified_function ASC, c.normalized_message ASC
            """
        ).fetchall()

        clusters: list[dict[str, Any]] = []
        for row in rows:
            fingerprint_id_value, qualified_function, function_name, class_scope, normalized_message, occurrence_count, first_timestamp, last_timestamp, representative_error = row
            components = self.conn.execute(
                "SELECT DISTINCT component FROM error_occurrences WHERE fingerprint_id = ? ORDER BY component ASC",
                (fingerprint_id_value,),
            ).fetchall()
            cluster = {
                "fingerprint_id": fingerprint_id_value,
                "qualified_function": qualified_function,
                "function_name": function_name,
                "class_scope": class_scope,
                "normalized_message": normalized_message,
                "occurrence_count": occurrence_count,
                "first_timestamp": first_timestamp,
                "last_timestamp": last_timestamp,
                "components": [component[0] for component in components if component[0]],
                "representative_error": json.loads(representative_error) if representative_error else None,
            }
            clusters.append(cluster)
        return clusters


def cluster_errors(records: Iterable[dict[str, Any]], *, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    store = SQLiteClusterStore(db_path=db_path)
    try:
        for record in records:
            if record.get("level") != "ERROR":
                raise ValueError("Task 30 received a non-ERROR record.")
            store.add_occurrence(record, component=str(record.get("component") or "UNKNOWN"))
        store.conn.commit()
        return store.export_clusters()
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize, fingerprint, and cluster ERROR records.")
    parser.add_argument("--errors", required=True)
    parser.add_argument("--output-directory", required=True)
    args = parser.parse_args()

    output = Path(args.output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    records = read_jsonl(Path(args.errors))
    clusters = cluster_errors(records)

    (output / "error_clusters.json").write_text(
        json.dumps(clusters, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    report = {
        "input_error_count": len(records),
        "distinct_cluster_count": len(clusters),
        "clustered_occurrence_count": sum(c["occurrence_count"] for c in clusters),
        "all_inputs_are_error": all(r.get("level") == "ERROR" for r in records),
    }
    (output / "fingerprint_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("========== TASK 30: FINGERPRINTING ==========")
    print(f"ERROR records: {len(records)}")
    print(f"Distinct issues: {len(clusters)}")
    for cluster in clusters:
        print(
            f"[{cluster['occurrence_count']}x] {cluster['fingerprint_id']} | "
            f"{cluster['qualified_function']} | {cluster['normalized_message']}"
        )
    print("=============================================")


if __name__ == "__main__":
    main()
