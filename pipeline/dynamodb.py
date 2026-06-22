from __future__ import annotations

from decimal import Decimal
import re
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError


CSR_RE = re.compile(r"^CSR_(\d{4,})$")
ART_RE = re.compile(r"^ART_(\d{5,})$")


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
        return {key: _from_dynamodb_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_from_dynamodb_value(item) for item in value]
    return value


def _next_id(items: list[dict[str, Any]], field: str, pattern: re.Pattern[str], width: int, prefix: str) -> str:
    highest = 0
    for item in items:
        match = pattern.match(str(item.get(field) or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{prefix}_{highest + 1:0{width}d}"


class DynamoStore:
    def __init__(
        self,
        *,
        region_name: str,
        endpoint_url: str | None,
        reviews_table: str,
        outcomes_table: str,
        articles_table: str = "articles",
    ) -> None:
        kwargs: dict[str, Any] = {"region_name": region_name, "endpoint_url": endpoint_url}
        if endpoint_url:
            kwargs.update({"aws_access_key_id": "local", "aws_secret_access_key": "local"})
        self.resource = boto3.resource("dynamodb", **kwargs)
        self.reviews_table_name = reviews_table
        self.outcomes_table_name = outcomes_table
        self.articles_table_name = articles_table
        self.reviews = self.resource.Table(reviews_table)
        self.outcomes = self.resource.Table(outcomes_table)
        self.articles = self.resource.Table(articles_table)

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
        if self.articles_table_name not in existing:
            self.resource.create_table(
                TableName=self.articles_table_name,
                KeySchema=[{"AttributeName": "article_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "article_id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            ).wait_until_exists()
        self.reviews = self.resource.Table(self.reviews_table_name)
        self.outcomes = self.resource.Table(self.outcomes_table_name)
        self.articles = self.resource.Table(self.articles_table_name)

    def review_exists(self, pmid: str) -> bool:
        response = self.reviews.get_item(Key={"pmid": str(pmid)}, ProjectionExpression="pmid")
        return "Item" in response

    def put_review(self, item: dict[str, Any]) -> None:
        self.reviews.put_item(Item=_to_dynamodb_value(item))

    def get_review_by_pmid(self, pmid: str) -> dict[str, Any] | None:
        try:
            response = self.reviews.get_item(Key={"pmid": str(pmid)})
        except ClientError:
            return None
        item = response.get("Item")
        return _from_dynamodb_value(item) if item else None

    def get_review(self, review_id_or_pmid: str) -> dict[str, Any] | None:
        direct = self.get_review_by_pmid(review_id_or_pmid)
        if direct:
            return direct
        for review in self.list_reviews():
            if str(review.get("review_id")) == str(review_id_or_pmid):
                return review
        return None

    def list_reviews(self) -> list[dict[str, Any]]:
        reviews = self._scan_all(self.reviews)
        return sorted(
            reviews,
            key=lambda item: (
                str(item.get("review_id") or ""),
                str(item.get("year") or ""),
                str(item.get("pmid") or ""),
            ),
        )

    def next_review_id(self) -> str:
        return _next_id(self.list_reviews(), "review_id", CSR_RE, 4, "CSR")

    def put_outcome(self, item: dict[str, Any]) -> None:
        self.outcomes.put_item(Item=_to_dynamodb_value(item))

    def replace_outcomes(self, pmid: str, items: list[dict[str, Any]]) -> None:
        existing = self.list_outcomes_for_review(pmid)
        with self.outcomes.batch_writer() as batch:
            for item in existing:
                batch.delete_item(Key={"pmid": str(pmid), "outcome_id": int(item["outcome_id"])})
            for item in items:
                batch.put_item(Item=_to_dynamodb_value(item))

    def list_outcomes(self) -> list[dict[str, Any]]:
        return sorted(
            self._scan_all(self.outcomes),
            key=lambda item: (str(item.get("review_id") or ""), str(item.get("pmid") or ""), int(item.get("outcome_id", 0))),
        )

    def list_outcomes_for_review(self, pmid: str) -> list[dict[str, Any]]:
        response = self.outcomes.query(KeyConditionExpression=Key("pmid").eq(str(pmid)), ScanIndexForward=True)
        items = response.get("Items", [])
        while "LastEvaluatedKey" in response:
            response = self.outcomes.query(
                KeyConditionExpression=Key("pmid").eq(str(pmid)),
                ExclusiveStartKey=response["LastEvaluatedKey"],
                ScanIndexForward=True,
            )
            items.extend(response.get("Items", []))
        return [_from_dynamodb_value(item) for item in items]

    def get_outcome(self, pmid: str, outcome_id: int) -> dict[str, Any] | None:
        try:
            response = self.outcomes.get_item(Key={"pmid": str(pmid), "outcome_id": int(outcome_id)})
        except ClientError:
            return None
        item = response.get("Item")
        return _from_dynamodb_value(item) if item else None

    def put_article(self, item: dict[str, Any]) -> None:
        self.articles.put_item(Item=_to_dynamodb_value(item))

    def get_article(self, article_id: str) -> dict[str, Any] | None:
        try:
            response = self.articles.get_item(Key={"article_id": str(article_id)})
        except ClientError:
            return None
        item = response.get("Item")
        return _from_dynamodb_value(item) if item else None

    def list_articles(self) -> list[dict[str, Any]]:
        return sorted(self._scan_all(self.articles), key=lambda item: str(item.get("article_id") or ""))

    def list_articles_for_review(self, review_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_articles() if str(item.get("review_id")) == str(review_id)]

    def next_article_id(self) -> str:
        return _next_id(self.list_articles(), "article_id", ART_RE, 5, "ART")

    def batch_get_articles(self, article_ids: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for article_id in dict.fromkeys(str(item) for item in article_ids if item):
            article = self.get_article(article_id)
            if article:
                result[article_id] = article
        return result

    def _scan_all(self, table: Any) -> list[dict[str, Any]]:
        response = table.scan()
        items = response.get("Items", [])
        while "LastEvaluatedKey" in response:
            response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))
        return [_from_dynamodb_value(item) for item in items]
