# News Bot refactor deployment

This release changes only application code/tests. It does not require a database schema migration.
The production `.env` and `runtime/` directory must be preserved from the live server.

## Safety

1. Stop the service before replacing code.
2. Create a full tar backup of `/opt/news-bot-v1`, including `.env`, DB, Telegram sessions and MAX session.
3. Extract this release into a temporary directory.
4. Copy the release code over the live project while preserving `.env` and `runtime/`.
5. Run compile and tests before starting the service.
6. Start the service and inspect logs.
7. If anything is wrong, restore the full backup and restart.

## Functional contract

- Event resolver has only `NEW`, `EXISTING`, `UNKNOWN` states.
- `UNKNOWN` never becomes `NEW` or `DUPLICATE` and is retried by the collector.
- Existing event always goes through one update classifier.
- `score < 80` never produces an UPDATE publication.
- `score >= 80` is the only classifier result eligible for UPDATE composition.
- Duplicate/paraphrased material is never published as a new event.
- Digest and analytical material is rejected before publication; generated analytical/digest output is also rejected.
- Provider/API/JSON failures are retryable errors, not editorial decisions.
- Telegram source watermark advances only after successful message processing.
- Event `updated_at` is not touched merely by duplicate attachment, so duplicates do not keep events artificially fresh.
- Priority is normalized to 0–10.
