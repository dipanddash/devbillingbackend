from django.urls import path
from .views import (
    TableListView,
    TableSessionCreateView,
    ActiveSessionListView,
    TableCreateView
)

urlpatterns = [

    path("list/", TableListView.as_view()),

    path("session/create/", TableSessionCreateView.as_view()),

    path("session/active/", ActiveSessionListView.as_view()),
path(
  "session/active/table/<uuid:table_id>/",
  ActiveSessionListView.as_view()
),
    path("create/", TableCreateView.as_view()),
]
