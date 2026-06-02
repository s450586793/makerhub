from __future__ import annotations

from app.core.store import JsonStore
from app.services.auth import AuthManager
from app.services.crawler import LegacyCrawlerBridge
from app.services.local_organizer import LocalOrganizerService
from app.services.remote_refresh import RemoteRefreshManager
from app.services.source_library import SourceLibraryManager
from app.services.subscriptions import SubscriptionManager
from app.services.task_state import TaskStateStore


store = JsonStore()
crawler = LegacyCrawlerBridge()
auth_manager = AuthManager(store=store)
task_state_store = TaskStateStore()
subscription_manager = SubscriptionManager(
    archive_manager=crawler.manager,
    store=store,
    task_store=task_state_store,
)
local_organizer = LocalOrganizerService(
    store=store,
    task_store=task_state_store,
)
source_library_manager = SourceLibraryManager(
    store=store,
    task_store=task_state_store,
)
remote_refresh_manager = RemoteRefreshManager(
    store=store,
    task_store=task_state_store,
    archive_manager=crawler.manager,
)
