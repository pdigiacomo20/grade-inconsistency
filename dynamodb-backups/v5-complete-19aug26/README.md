# v5 Complete Database Backup

Created: 2026-08-19

Files:

- `dynamodb-local-data-v5-complete-19aug26.tar`: full DynamoDB Local Docker volume snapshot.
- `reviews-v5-complete-19aug26.json`: DynamoDB JSON scan of the `reviews` table.
- `outcomes-v5-complete-19aug26.json`: DynamoDB JSON scan of the `outcomes` table.
- `articles-v5-complete-19aug26.json`: DynamoDB JSON scan of the `articles` table.

To restore the full local DynamoDB volume:

```bash
cd /home/pd/grade-inconsistency/grade-inconsistency
docker compose stop dynamodb
docker run --rm -v grade-inconsistency_dynamodb-data:/data alpine sh -c 'rm -rf /data/*'
docker run --rm -v grade-inconsistency_dynamodb-data:/data -v /home/pd/grade-inconsistency/grade-inconsistency/dynamodb-backups/v5-complete-19aug26:/backup alpine tar -xf /backup/dynamodb-local-data-v5-complete-19aug26.tar -C /data
docker compose up -d dynamodb
```

The JSON files are for inspection or custom table-level recovery. The tar file is the authoritative full local database backup.
