# Safe Story rebuild

The production database is never modified by an audit or template run.
A rebuild is applied only from an explicit, validated plan.

## Policy

- Story = one concrete real-world episode, not a topic/person/country.
- RAW message is canonical; generated facts/articles are secondary.
- Same episode + no material new fact = DUPLICATE, no publication.
- Same episode + material new fact = UPGRADE, publication allowed.
- Different episode = NEW Story.
- Uncertain semantic identity = defer/review; never auto-create or auto-merge.
- Historical publications stay attached to their original event.

## Production sequence

1. Stop `news-bot.service`.
2. Make a SQLite backup using `Connection.backup()` or a file copy while stopped.
3. Copy `stories-rebuild-source.json` to the server.
4. Run read-only audit:

```bash
python tools/rebuild_stories.py --db runtime/news.db --export /root/stories-rebuild-source.json --report /root/story-audit.json
```

5. Create a review template:

```bash
python tools/rebuild_stories.py --db runtime/news.db --export /root/stories-rebuild-source.json --template /root/rebuild-plan.json
```

6. Edit the plan. Only `action: split` or `action: rebuild` is applied. Every message must occur in exactly one group and every fact must cite a message in that group.
7. Validate without applying by omitting `--apply` and using a small local copy first.
8. Apply only the reviewed plan:

```bash
python tools/rebuild_stories.py --db runtime/news.db --export /root/stories-rebuild-source.json --plan /root/rebuild-plan.json --apply
```

9. Run `PRAGMA integrity_check;` and count checks before starting the service.

The tool deliberately does not invent semantic decisions. The semantic plan is a review artifact, so an LLM may propose it later, but APPLY accepts only the explicit validated plan.
