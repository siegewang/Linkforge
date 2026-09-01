import time
import logging
from threading import Lock

logger = logging.getLogger(__name__)

_lock = Lock()
_active_tasks = {}
_completed_tasks_history = {}

def start_task(task_id: str, name: str, total: int, icon: str = "fa-gears"):
    """Register or restart an active background task."""
    with _lock:
        _active_tasks[task_id] = {
            "id": task_id,
            "name": name,
            "icon": icon,
            "status": "running",
            "total": max(1, total),
            "current": 0,
            "current_item": "Initializing...",
            "start_time": time.time(),
            "updated_time": time.time(),
            "percentage": 0,
            "queue_remaining": total
        }
        # Clean from completed
        _completed_tasks_history.pop(task_id, None)
    logger.info(f"[TaskQueue] Started task '{name}' (ID: {task_id}, Total: {total})")

def update_progress(task_id: str, current: int, current_item: str = None):
    """Update current progress and active processing item."""
    with _lock:
        task = _active_tasks.get(task_id)
        if task:
            task["current"] = current
            if current_item is not None:
                task["current_item"] = current_item
            task["updated_time"] = time.time()
            task["percentage"] = min(100, int((task["current"] / task["total"]) * 100))
            task["queue_remaining"] = max(0, task["total"] - task["current"])

def complete_task(task_id: str, final_message: str = "Completed successfully"):
    """Mark task as complete and keep in transient history for 6 seconds."""
    with _lock:
        task = _active_tasks.pop(task_id, None)
        if task:
            task["status"] = "completed"
            task["percentage"] = 100
            task["current"] = task["total"]
            task["queue_remaining"] = 0
            task["current_item"] = final_message
            task["completed_time"] = time.time()
            _completed_tasks_history[task_id] = task
    logger.info(f"[TaskQueue] Completed task '{task_id}'")

def get_tasks_status():
    """Return JSON status of all active and recently completed tasks."""
    now = time.time()
    with _lock:
        # Prune completed tasks older than 6 seconds
        expired = [k for k, v in _completed_tasks_history.items() if now - v.get("completed_time", 0) > 6]
        for k in expired:
            _completed_tasks_history.pop(k, None)
            
        running_tasks = list(_active_tasks.values())
        recently_done = list(_completed_tasks_history.values())
        
        all_visible = running_tasks + recently_done
        total_remaining = sum(t.get("queue_remaining", 0) for t in running_tasks)
        
        return {
            "is_processing": len(running_tasks) > 0,
            "tasks": all_visible,
            "total_queued": total_remaining
        }
