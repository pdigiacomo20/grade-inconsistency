from __future__ import annotations

from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key


def _to_dynamodb_value(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {str(k): _to_dynamodb_value(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_to_dynamodb_value(v) for v in value]
    return value


def _from_dynamodb_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {k: _from_dynamodb_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamodb_value(v) for v in value]
    return value


class DynamoStore:
    def __init__(
        self,
        *,
        region_name: str,
        endpoint_url: str | None,
        reviews_table: str,
        outcomes_table: str,
    ) -> None:
        kwargs: dict[str, Any] = {
            "region_name": region_name,
            "endpoint_url": endpoint_url,
        }
        if endpoint_url:
            kwargs.update({"aws_access_key_id": "local", "aws_secret_access_key": "local"})
        self.resource = boto3.resource("dynamodb", **kwargs)
        self.reviews_table_name = reviews_table
        self.outcomes_table_name = outcomes_table
        self.reviews = self.resource.Table(reviews_table)
        self.outcomes = self.resource.Table(outcomes_table)

    def ensure_tables(self) -> None:
        existing = set(self.resource.meta.client.list_tables()["TableNames"])
        if self.reviews_table_name not in existing:
            self.resource.create_table(
                TableName=self.reviews_table_name,
                KeySchema=[{"AttributeName": "pmid", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "pmid", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            ).wait_until_exists()
        if self.outcomes_table_name not in existing:
            self.resource.create_table(
                TableName=self.outcomes_table_name,
                KeySchema=[
                    {"AttributeName": "pmid", "KeyType": "HASH"},
                    {"AttributeName": "outcome_id", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "pmid", "AttributeType": "S"},
                    {"AttributeName": "outcome_id", "AttributeType": "N"},
                ],
                BillingMode="PAY_PER_REQUEST",
            ).wait_until_exists()
        self.reviews = self.resource.Table(self.reviews_table_name)
        self.outcomes = self.resource.Table(self.outcomes_table_name)

    def review_exists(self, pmid: str) -> bool:
        response = self.reviews.get_item(Key={"pmid": str(pmid)}, ProjectionExpression="pmid")
        return "Item" in response

    def put_review(self, item: dict[str, Any]) -> None:
        self.reviews.put_item(Item=_to_dynamodb_value(item))

    def replace_outcomes(self, pmid: str, items: list[dict[str, Any]]) -> None:
        existing = self.list_outcomes_for_review(pmid)
        with self.outcomes.batch_writer() as batch:
            for item in existing:
                batch.delete_item(Key={"pmid": str(pmid), "outcome_id": int(item["outcome_id"])})
            for item in items:
                batch.put_item(Item=_to_dynamodb_value(item))

    def list_reviews(self) -> list[dict[str, Any]]:
        return sorted(
            self._scan_all(self.reviews),
            key=lambda item: (str(item.get("year", "")), str(item.get("pmid", ""))),
            reverse=True,
        )

    def list_outcomes(self) -> list[dict[str, Any]]:
        return sorted(
            self._scan_all(self.outcomes),
            key=lambda item: (str(item.get("pmid", "")), int(item.get("outcome_id", 0))),
        )

    def list_outcomes_for_review(self, pmid: str) -> list[dict[str, Any]]:
        response = self.outcomes.query(
            KeyConditionExpression=Key("pmid").eq(str(pmid)),
            ScanIndexForward=True,
        )
        items = response.get("Items", [])
        while "LastEvaluatedKey" in response:
            response = self.outcomes.query(
                KeyConditionExpression=Key("pmid").eq(str(pmid)),
                ExclusiveStartKey=response["LastEvaluatedKey"],
                ScanIndexForward=True,
            )
            items.extend(response.get("Items", []))
        return [_from_dynamodb_value(item) for item in items]

    def get_review(self, pmid: str) -> dict[str, Any] | None:
        try:
            response = self.reviews.get_item(Key={"pmid": str(pmid)})
        except ClientError:
            return None
        item = response.get("Item")
        return _from_dynamodb_value(item) if item else None

    def _scan_all(self, table: Any) -> list[dict[str, Any]]:
        response = table.scan()
        items = response.get("Items", [])
        while "LastEvaluatedKey" in response:
            response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))
        return [_from_dynamodb_value(item) for item in items]
