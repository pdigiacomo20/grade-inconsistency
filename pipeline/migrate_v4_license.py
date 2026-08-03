from __future__ import annotations

import argparse
from datetime import UTC, datetime

from pipeline.config import load_config
from pipeline.dynamodb import DynamoStore


def migrate(config_path: str) -> dict[str, int]:
    config = load_config(config_path)
    store = DynamoStore(
        region_name=config.aws_region,
        endpoint_url=config.dynamodb_endpoint_url,
        reviews_table=config.reviews_table,
        outcomes_table=config.outcomes_table,
        articles_table=config.articles_table,
    )
    migrated = 0
    already_had_license = 0
    already_marked_missing = 0
    timestamp = datetime.now(UTC).isoformat()

    for review in store.list_reviews():
        if str(review.get("license") or "").strip():
            already_had_license += 1
            continue
        if review.get("license_missing_reason") == "pre_v4_migration":
            already_marked_missing += 1
            continue
        review["license"] = ""
        review["license_missing_since"] = timestamp
        review["license_missing_reason"] = "pre_v4_migration"
        review["updated_at"] = timestamp
        store.put_review(review)
        migrated += 1

    return {
        "migrated": migrated,
        "already_had_license": already_had_license,
        "already_marked_missing": already_marked_missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Add v4 review license markers to existing DynamoDB reviews.")
    parser.add_argument("--config", default="config.yml", help="Path to YAML config file.")
    args = parser.parse_args()
    print(migrate(args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
