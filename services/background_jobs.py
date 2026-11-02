"""Jupiter-run background work: "do X, then call me when it's done" (marker 174).

Work Jupiter can do itself (writing, research, planning, summarising: the normal chat pipeline) runs server-side here,
so it finishes even if the user closes the app. It reuses the EXISTING task tracker (services/task_notifications.py):

    AWAITING_WORKER -> RUNNING (job started) -> COMPLETED (a reply was produced) | FAILED (error / empty reply)

The completion check registered for these trackers is ``jupiter:chat_reply``: "Jupiter's own chat pipeline produced
a non-empty reply for this work". Completion therefore needs real evidence like any other tracker; the committed
transition is what rings a Jupiter call (when the user asked for one), so nobody is called before the work is done.
Work for Claude Code / Codex / builds / scripts still waits for that worker to report (not run here).

Bounded: one job at a time per account, at most ``MAX_CONCURRENT`` overall, and a ``JOB_TIMEOUT`` after which the
job is reported FAILED. The reply is saved into the same chat (server chat store) and kept as the task's result, so the
in-app card and the answered call can tell the user what was done. Nothing is persisted for private / no-persist chats.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable

JUPITER_CHECK = "jupiter:chat_reply"
MAX_CONCURRENT = 3
JOB_TIMEOUT = 15 * 60

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_owner_locks: dict[str, threading.Lock] = {}
_owner_guard = threading.Lock()


def _owner_lock(owner: str) -> threading.Lock:
    with _owner_guard:
        return _owner_locks.setdefault(owner, threading.Lock())


def run_job(store, owner: str, task: dict, work: str, run: Callable[[str], str], *,
            save_reply: Callable[[str], None] | None = None, on_finished: Callable[[], None] | None = None,
            clock: Callable[[], float] = time.monotonic) -> str:
    """Run one job synchronously (the thread target; also called directly by tests). Returns the final status."""
    from services.activity_tracker import tracker
    from services.task_notifications import NotificationError
    task_id, run_id = task["task_id"], task["run_id"]
    lease = tracker.begin(owner, "background")
    final = "FAILED"
    try:
        with _owner_lock(owner), _slots:
            try:
                store.transition(task_id=task_id, owner=owner, run_id=run_id, event_id=f"jupiter-start-{run_id}",
                                 status="RUNNING")
            except NotificationError:
                return "SKIPPED"                                   # cancelled/finished before the job started
            started = clock()
            reply = ""
            try:
                reply = str(run(work) or "").strip()
            except Exception:
                reply = ""
            if reply and clock() - started <= JOB_TIMEOUT:
                store.save_result(owner, task_id, reply)
                if save_reply is not None:
                    try:
                        save_reply(reply)
                    except Exception:
                        pass                                      # the result is still kept on the task
                try:
                    store.transition(task_id=task_id, owner=owner, run_id=run_id, event_id=f"jupiter-done-{run_id}",
                                     status="COMPLETED",
                                     evidence={"validation": {"command_or_check": JUPITER_CHECK, "passed": True}})
                    final = "COMPLETED"
                except NotificationError:
                    final = "SKIPPED"                             # e.g. cancelled while running: never call
            else:
                try:
                    store.transition(task_id=task_id, owner=owner, run_id=run_id, event_id=f"jupiter-fail-{run_id}",
                                     status="FAILED", action_required="Jupiter could not finish this. Try again.")
                except NotificationError:
                    final = "SKIPPED"
    finally:
        tracker.end(lease)
        if on_finished is not None:
            try:
                on_finished()
            except Exception:
                pass
    return final


def start_job(store, owner: str, task: dict, work: str, run: Callable[[str], str], **kwargs) -> threading.Thread:
    thread = threading.Thread(target=run_job, args=(store, owner, task, work, run), kwargs=kwargs,
                              name=f"jupiter-background-{task['task_id'][:8]}", daemon=True)
    thread.start()
    return thread


def chat_reply_saver(owner: str, chat_id: str, task_id: str, title: str) -> Callable[[str], None]:
    """Append the finished reply to the same chat in the server chat store (picked up by every device on sync)."""
    def save(reply: str) -> None:
        if not chat_id:
            return
        from storage.sqlite_store import SQLiteStore
        store = SQLiteStore(user_id=owner)
        try:
            if chat_id in set(store.get_deleted_chat_ids()):
                return                                             # never revive a deleted chat
            stamp = datetime.now(timezone.utc).isoformat()
            existing = {chat["chat_id"]: chat for chat in store.get_chats()}.get(chat_id)
            store.save_chat_metadata(chat_id, (existing or {}).get("title") or title[:60] or "Background task", stamp,
                                     int(bool((existing or {}).get("pinned"))), int(bool((existing or {}).get("archived"))),
                                     skip_deleted=True)
            store.save_message(message_id=f"bg-{task_id}", chat_id=chat_id, sender="ai", text=reply, timestamp=stamp,
                               attachments=[], capability_metadata={"background_task": {"task_id": task_id}})
        finally:
            store.close()
    return save
