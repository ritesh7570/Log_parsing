from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config


def load_clusters(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def extract_results(response: dict[str, Any]) -> list[dict[str, Any]]:
    results = response.get("results", [])
    if isinstance(results, list):
        return results
    return []


def create_client(profile: str | None, region: str, endpoint: str):
    session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
    return session.client(
        "neptunedata",
        endpoint_url=f"https://{endpoint}:8182",
        config=Config(connect_timeout=20, read_timeout=60, retries={"max_attempts": 1}),
    )


def exact_candidates(client, qualified_name: str) -> list[dict[str, Any]]:
    response = client.execute_open_cypher_query(
        openCypherQuery="""
            MATCH (f:Function)
            WHERE f.qualified_name = $qualifiedName
            RETURN id(f) AS function_id,
                   f.qualified_name AS qualified_name,
                   f.function_name AS function_name,
                   f.class_name AS class_name,
                   f.signature AS signature,
                   f.relative_path AS relative_path,
                   f.start_line AS start_line,
                   f.end_line AS end_line,
                   f.content_hash AS content_hash
            ORDER BY f.relative_path, f.start_line
        """,
        parameters=json.dumps({"qualifiedName": qualified_name}),
    )
    return extract_results(response)


def function_name_candidates(client, function_name: str) -> list[dict[str, Any]]:
    response = client.execute_open_cypher_query(
        openCypherQuery="""
            MATCH (f:Function)
            WHERE f.function_name = $functionName
            RETURN id(f) AS function_id,
                   f.qualified_name AS qualified_name,
                   f.function_name AS function_name,
                   f.class_name AS class_name,
                   f.signature AS signature,
                   f.relative_path AS relative_path,
                   f.start_line AS start_line,
                   f.end_line AS end_line,
                   f.content_hash AS content_hash
            ORDER BY f.qualified_name, f.relative_path, f.start_line
        """,
        parameters=json.dumps({"functionName": function_name}),
    )
    return extract_results(response)


def resolve_cluster(client, cluster: dict[str, Any]) -> dict[str, Any]:
    qualified_name = cluster.get("qualified_function") or ""
    function_name = cluster.get("function_name") or (qualified_name.split("::")[-1] if qualified_name else "")

    exact = exact_candidates(client, qualified_name) if qualified_name else []
    if len(exact) == 1:
        status, method, candidates = "RESOLVED", "EXACT_QUALIFIED_NAME", exact
    elif len(exact) > 1:
        status, method, candidates = "AMBIGUOUS", "EXACT_QUALIFIED_NAME", exact
    else:
        fallback = function_name_candidates(client, function_name) if function_name else []
        if len(fallback) == 1:
            status, method, candidates = "RESOLVED", "UNIQUE_FUNCTION_NAME_FALLBACK", fallback
        elif len(fallback) > 1:
            status, method, candidates = "AMBIGUOUS", "FUNCTION_NAME_FALLBACK", fallback
        else:
            status, method, candidates = "UNRESOLVED", "NO_MATCH", []

    return {
        "fingerprint_id": cluster["fingerprint_id"],
        "occurrence_count": cluster["occurrence_count"],
        "qualified_function": qualified_name,
        "normalized_message": cluster["normalized_message"],
        "representative_error": cluster["representative_error"],
        "anchor_resolution": {
            "status": status,
            "match_method": method,
            "candidate_count": len(candidates),
            "selected_function": candidates[0] if status == "RESOLVED" else None,
            "candidates": candidates,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve clustered log errors to Neptune Function nodes.")
    parser.add_argument("--clusters", required=True)
    parser.add_argument("--endpoint", required=True, help="Neptune hostname only, without https:// or :8182")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--output-directory", required=True)
    args = parser.parse_args()

    output = Path(args.output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    clusters = load_clusters(Path(args.clusters))
    client = create_client(args.profile, args.region, args.endpoint)
    resolved = [resolve_cluster(client, cluster) for cluster in clusters]

    (output / "resolved_issues.json").write_text(
        json.dumps(resolved, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    status_counts: dict[str, int] = {}
    for item in resolved:
        status = item["anchor_resolution"]["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    report = {
        "cluster_count": len(clusters),
        "resolution_status_counts": status_counts,
        "all_clusters_accounted_for": len(resolved) == len(clusters),
    }
    (output / "anchor_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("========== TASK 31: NEPTUNE ANCHORS ==========")
    for item in resolved:
        anchor = item["anchor_resolution"]
        print(
            f"{item['fingerprint_id']} | {anchor['status']} | "
            f"{anchor['match_method']} | candidates={anchor['candidate_count']} | "
            f"{item['qualified_function']}"
        )
    print(f"Summary: {status_counts}")
    print("==============================================")


if __name__ == "__main__":
    main()
