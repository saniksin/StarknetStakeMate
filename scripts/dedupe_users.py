"""One-shot deduplication of ``users`` rows that share a ``user_id``.

Background
----------
The original schema declared ``user_id`` as a plain ``Integer`` without
``unique=True``. ``get_or_create_user`` raced on parallel ``/start``
handlers — both saw "no row" and both inserted, ending up with two
distinct ``id`` rows that share the same ``user_id``. SQLAlchemy's
``scalar_one_or_none()`` then crashed every helper that looked the user
up by ``user_id``, taking down the notification loop.

What this script does
---------------------
For every ``user_id`` group with more than one row:

  1. Pick the "best" row to keep using a heuristic that prefers data over
     emptiness (``tracking_data`` set → ``notification_config`` set →
     highest ``id`` as tiebreaker).
  2. Delete the other rows in the group.
  3. Print a summary so the operator can audit what changed.

After the dedup the next bot boot's ``initialize_db`` will succeed in
installing the UNIQUE INDEX on ``users.user_id`` and the race is closed
for good (subsequent concurrent INSERTs hit ``IntegrityError`` which
``get_or_create_user`` now catches and re-fetches).

Usage
-----
Inside the bot container::

    docker exec -it stakemate-bot python -m scripts.dedupe_users

Or against a local DB file::

    python -m scripts.dedupe_users /path/to/users.db

Pass ``--dry-run`` to see what *would* be deleted without touching the
database.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def _score(row: tuple) -> tuple:
    """Sort key — higher tuple = "better" row.

    Order of preference:
      1. tracking_data populated (real validators / delegations)
      2. notification_config populated (alert thresholds set)
      3. claim_reward_msg populated (legacy threshold)
      4. user_language present
      5. user_name present
      6. highest id (most recently inserted, last-write-wins tiebreaker)
    """
    (
        row_id, _user_id, user_name, user_language,
        tracking_data, claim_reward_msg, notification_config,
    ) = row
    return (
        bool(tracking_data and tracking_data not in ('{"validators": [], "delegations": []}',)),
        bool(notification_config),
        bool(claim_reward_msg),
        bool(user_language),
        bool(user_name),
        row_id,
    )


def dedupe(db_path: Path, *, dry_run: bool) -> None:
    if not db_path.is_file():
        sys.exit(f"DB file not found: {db_path}")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # 1. Find user_ids with duplicates.
    cur.execute(
        "SELECT user_id, COUNT(*) AS cnt FROM users "
        "GROUP BY user_id HAVING cnt > 1 ORDER BY user_id"
    )
    dupe_groups = cur.fetchall()
    if not dupe_groups:
        print("No duplicates found.")
        con.close()
        return

    print(f"Found {len(dupe_groups)} duplicate group(s):")
    for g in dupe_groups:
        print(f"  user_id={g['user_id']} → {g['cnt']} rows")

    total_to_delete: list[int] = []
    summary: list[str] = []

    for g in dupe_groups:
        cur.execute(
            "SELECT id, user_id, user_name, user_language, tracking_data, "
            "claim_reward_msg, notification_config "
            "FROM users WHERE user_id = ? ORDER BY id",
            (g["user_id"],),
        )
        rows = [tuple(r) for r in cur.fetchall()]
        ranked = sorted(rows, key=_score, reverse=True)
        keep = ranked[0]
        drop = ranked[1:]
        total_to_delete.extend(r[0] for r in drop)
        summary.append(
            f"  user_id={g['user_id']}: keeping id={keep[0]}, "
            f"deleting ids={[r[0] for r in drop]}"
        )

    print()
    print("Plan:")
    for line in summary:
        print(line)
    print()
    print(f"Total rows to delete: {len(total_to_delete)}")

    if dry_run:
        print("\n--dry-run set: no changes made.")
        con.close()
        return

    placeholders = ",".join("?" * len(total_to_delete))
    cur.execute(f"DELETE FROM users WHERE id IN ({placeholders})", total_to_delete)
    con.commit()
    print(f"\nDeleted {cur.rowcount} duplicate row(s).")

    # Verify
    cur.execute(
        "SELECT user_id, COUNT(*) AS cnt FROM users "
        "GROUP BY user_id HAVING cnt > 1"
    )
    remaining = cur.fetchall()
    if remaining:
        print(f"WARNING: {len(remaining)} duplicate group(s) still present:")
        for r in remaining:
            print(f"  user_id={r['user_id']} → {r['cnt']} rows")
    else:
        print("OK: no duplicates left. Restart the bot to install the UNIQUE INDEX.")
    con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "db_path",
        nargs="?",
        default="/app/files/users.db",
        type=Path,
        help="SQLite DB path (default: /app/files/users.db inside the container)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without touching the database.",
    )
    args = parser.parse_args()
    dedupe(args.db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
