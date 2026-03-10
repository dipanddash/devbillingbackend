from django.urls import path
from .views import (
    SyncHealthView,
    SyncSnapshotView,
    SyncPushView,
    SyncStatusView,
    SyncTriggerView,
    SyncQueueView,
)

urlpatterns = [
    path("health/", SyncHealthView.as_view()),
    path("snapshot/", SyncSnapshotView.as_view()),
    path("push/", SyncPushView.as_view()),
    path("status/", SyncStatusView.as_view()),
    path("trigger/", SyncTriggerView.as_view()),
    path("queue/", SyncQueueView.as_view()),
]
