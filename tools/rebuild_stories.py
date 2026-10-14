#!/usr/bin/env python3
"""Safe Story rebuild tool.

The tool never calls an LLM and never mutates the database in audit/dry-run mode.
A semantic plan is an explicit JSON artifact. This separation is intentional:
LLM classification is allowed to propose a plan, but APPLY consumes only a
reviewed plan and validates every message/fact reference against SQLite.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def load_export(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("stories"), list):
        raise ValueError("invalid stories export: expected object with stories[]")
    return data


def load_db(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def audit(db, export):
    report = {
        "export_stories": len(export["stories"]),
        "stories_with_multiple_messages": 0,
        "stories_with_zero_facts": 0,
        "facts_with_missing_source_message": 0,
        "messages_missing_from_db": 0,
        "publication_count": 0,
        "issues": [],
    }
    seen_messages = set()
    for s in export["stories"]:
        sid = int(s["story_id"])
        msgs = s.get("messages") or []
        facts = s.get("facts") or []
        pubs = s.get("publications") or []
        if len(msgs) > 1:
            report["stories_with_multiple_messages"] += 1
        if not facts:
            report["stories_with_zero_facts"] += 1
        report["publication_count"] += len(pubs)
        for m in msgs:
            mid = int(m["message_id"])
            seen_messages.add(mid)
            row = db.execute("SELECT id FROM messages WHERE id=?", (mid,)).fetchone()
            if not row:
                report["messages_missing_from_db"] += 1
                report["issues"].append({"story_id": sid, "message_id": mid, "issue": "message_missing"})
        for f in facts:
            smid = f.get("source_message_id")
            if smid is None:
                report["facts_with_missing_source_message"] += 1
                report["issues"].append({"story_id": sid, "fact_id": f.get("id"), "issue": "fact_without_source"})
            elif int(smid) not in {int(m["message_id"]) for m in msgs}:
                report["facts_with_missing_source_message"] += 1
                report["issues"].append({"story_id": sid, "fact_id": f.get("id"), "issue": "fact_source_not_in_story"})
    return report


def validate_plan(db, plan):
    if not isinstance(plan, dict) or plan.get("format") != "newsbot-story-rebuild-plan-v1":
        raise ValueError("invalid plan format")
    seen = set()
    issues = []
    for item in plan.get("stories", []):
        sid = int(item["story_id"])
        groups = item.get("groups") or []
        if not groups:
            issues.append({"story_id": sid, "issue": "no_groups"})
            continue
        for g in groups:
            mids = [int(x) for x in g.get("message_ids", [])]
            if not mids:
                issues.append({"story_id": sid, "issue": "empty_group"})
            for mid in mids:
                if mid in seen:
                    issues.append({"message_id": mid, "issue": "message_in_multiple_groups"})
                seen.add(mid)
                if not db.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone():
                    issues.append({"story_id": sid, "message_id": mid, "issue": "message_missing"})
            for f in g.get("facts", []):
                smid = f.get("source_message_id")
                text = str(f.get("fact_text", "")).strip()
                if not text or smid is None or int(smid) not in mids:
                    issues.append({"story_id": sid, "issue": "invalid_fact", "fact": f})
    return issues


def make_plan_template(export_path, out_path):
    export = load_export(export_path)
    plan = {
        "format": "newsbot-story-rebuild-plan-v1",
        "created_at": now(),
        "source_export": str(export_path),
        "policy": {
            "story_definition": "one concrete real-world episode",
            "uncertain": "review",
            "raw_is_canonical": True,
            "publications_are_historical": True,
        },
        "stories": [],
    }
    for s in export["stories"]:
        msgs = s.get("messages") or []
        if len(msgs) <= 1:
            continue
        # Only multi-message stories are proposed for semantic review. No
        # automatic merge/split decision is made here.
        plan["stories"].append({
            "story_id": int(s["story_id"]),
            "action": "review",
            "groups": [{
                "keep_story_id": int(s["story_id"]),
                "message_ids": [int(m["message_id"]) for m in msgs],
                "title": s.get("title") or "",
                "facts": [],
            }],
            "reason": "multi_message_story_requires_semantic_review",
        })
    Path(out_path).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_plan(db, plan):
    issues = validate_plan(db, plan)
    if issues:
        raise ValueError("plan validation failed: " + json.dumps(issues[:20], ensure_ascii=False))
    changed = 0
    created = 0
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS story_rebuild_log(
            id INTEGER PRIMARY KEY,
            plan_created_at TEXT NOT NULL,
            old_story_id INTEGER NOT NULL,
            new_story_id INTEGER NOT NULL,
            message_ids_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        for item in plan.get("stories", []):
            if item.get("action") not in {"split", "rebuild"}:
                continue
            old_id = int(item["story_id"])
            old = db.execute("SELECT * FROM events WHERE id=?", (old_id,)).fetchone()
            if not old:
                raise ValueError(f"story/event {old_id} missing")
            groups = item["groups"]
            keep = None
            for g in groups:
                if int(g.get("keep_story_id", 0) or 0) == old_id:
                    keep = g
                    break
            if keep is None:
                raise ValueError(f"story {old_id}: exactly one group must keep original story id")

            # Snapshot publication-linked message ids. Historical publications
            # are never reassigned to a new story.
            pub_article_ids = [r[0] for r in db.execute(
                "SELECT article_id FROM publications WHERE event_id=?", (old_id,)
            ).fetchall()]
            pub_msg_ids = set()
            if pub_article_ids:
                q = ",".join("?" * len(pub_article_ids))
                pub_msg_ids = {int(r[0]) for r in db.execute(
                    f"SELECT message_id FROM articles WHERE id IN ({q})", pub_article_ids
                ).fetchall()}

            # Remove old story facts; they are rebuilt from raw-backed plan facts.
            db.execute("DELETE FROM story_facts WHERE event_id=?", (old_id,))
            db.execute("DELETE FROM event_messages WHERE event_id=?", (old_id,))

            new_event_ids = []
            for idx, g in enumerate(groups):
                target = old_id if int(g.get("keep_story_id", 0) or 0) == old_id else None
                if target is None:
                    target = db.execute(
                        """INSERT INTO events(created_at,updated_at,title,category,priority,confidence,status,published_at,last_max_message_id)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (old["created_at"], old["updated_at"], g.get("title") or old["title"],
                         g.get("category") or old["category"], old["priority"], old["confidence"],
                         "new", None, None),
                    ).lastrowid
                    created += 1
                new_event_ids.append(target)
                mids = [int(x) for x in g["message_ids"]]
                for mid in mids:
                    db.execute("INSERT INTO event_messages(event_id,message_id) VALUES(?,?)", (target, mid))
                for f in g.get("facts", []):
                    db.execute("""INSERT INTO story_facts(event_id,fact_text,source_message_id,created_at)
                                  VALUES(?,?,?,?)""", (target, str(f["fact_text"]).strip(), int(f["source_message_id"]), now()))
                db.execute("INSERT INTO story_rebuild_log(plan_created_at,old_story_id,new_story_id,message_ids_json,created_at) VALUES(?,?,?,?,?)",
                           (plan.get("created_at", now()), old_id, target, json.dumps(mids), now()))
                changed += 1

            # Original story remains the historical publication owner. Its
            # status/publication fields are retained; new split stories start
            # as new/unpublished until a future message updates them.
            if old_id in new_event_ids:
                db.execute("UPDATE events SET updated_at=? WHERE id=?", (now(), old_id))
    return {"groups_applied": changed, "new_stories": created}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--export", required=True)
    ap.add_argument("--template", help="write review template and exit")
    ap.add_argument("--plan", help="reviewed rebuild plan")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", help="write audit JSON")
    args = ap.parse_args()
    export = load_export(args.export)
    db = load_db(args.db)
    try:
        report = audit(db, export)
        if args.report:
            Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.template:
            make_plan_template(args.export, args.template)
            print(f"template: {args.template}")
        if args.apply:
            if not args.plan:
                raise SystemExit("--apply requires --plan")
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            issues = validate_plan(db, plan)
            if issues:
                raise SystemExit("PLAN INVALID:\n" + json.dumps(issues[:50], ensure_ascii=False, indent=2))
            print(json.dumps(apply_plan(db, plan), ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
