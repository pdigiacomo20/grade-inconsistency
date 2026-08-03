# v4 Database Backup

Created: 2026-08-03T09:36:40-07:00

This backup was created immediately before adding review-level License fields.

Files:

- `dynamodb-local-data-v4.tar`: full DynamoDB Local Docker volume snapshot.
- `reviews-v4.json`: DynamoDB JSON scan of the `reviews` table.
- `outcomes-v4.json`: DynamoDB JSON scan of the `outcomes` table.
- `articles-v4.json`: DynamoDB JSON scan of the `articles` table.

To restore the full local DynamoDB volume:

```bash
cd /home/pd/grade-inconsistency/grade-inconsistency
docker compose stop dynamodb
docker run --rm -v grade-inconsistency_dynamodb-data:/data alpine sh -c 'rm -rf /data/*'
docker run --rm -v grade-inconsistency_dynamodb-data:/data -v /home/pd/grade-inconsistency/grade-inconsistency/dynamodb-backups/v4-database:/backup alpine tar -xf /backup/dynamodb-local-data-v4.tar -C /data
docker compose up -d dynamodb
```

The JSON files are for inspection or custom table-level recovery. The tar file is the authoritative full local database backup.
