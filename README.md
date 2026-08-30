# News Bot v2

Telegram polling (5 min) → one SQLite → dedup/event/upgrade → LLM → MAX, with the Admin Bot inside the same process.

- Only polling; no LIVE listener, no polling safety-net.
- Dedup window: 6 hours.
- Duplicate never goes to MAX.
- Upgrade is a new MAX publication linked to the same event and, when possible, sent as a reply to the previous MAX post.
- One main SQLite database for pipeline and administration.
- Admin Bot manages sources and shows status/statistics.
- First poll of a newly added source sets a watermark and does not replay history.
- `.env` and `runtime/` are deliberately excluded from deployment sync so existing Telegram/MAX sessions and environment stay untouched.
