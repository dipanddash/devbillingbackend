import token
from rest_framework import generics
from django.utils import timezone
from django.db.models import Prefetch
import random
from rest_framework.exceptions import ValidationError

from accounts.permissions import IsAdminOrStaff
from .models import Table, TableSession
from .serializers import TableSerializer, TableSessionSerializer

class TableListView(generics.ListAPIView):

    queryset = (
        Table.objects
        .all()
        .order_by("number")
        .prefetch_related(
            Prefetch(
                "sessions",
                queryset=(
                    TableSession.objects
                    .filter(
                        is_active=True,
                        closed_at__isnull=True,
                        table__status="OCCUPIED",
                    )
                    .order_by("-created_at")
                ),
                to_attr="active_sessions",
            )
        )
    )
    serializer_class = TableSerializer
    permission_classes = [IsAdminOrStaff]

class TableCreateView(generics.CreateAPIView):

    queryset = Table.objects.all()
    serializer_class = TableSerializer
    permission_classes = [IsAdminOrStaff]


class TableSessionCreateView(generics.CreateAPIView):

    queryset = TableSession.objects.all()
    serializer_class = TableSessionSerializer
    permission_classes = [IsAdminOrStaff]

    def perform_create(self, serializer):

        table = serializer.validated_data["table"]

        # Check if table already occupied
        if TableSession.objects.filter(
            table=table,
            is_active=True
        ).exists():
            raise ValidationError("Table already occupied")

        # Generate token
        while True:
            token = f"T-{random.randint(1000,9999)}"
            if not TableSession.objects.filter(token_number=token).exists():
                break


        serializer.save(
            token_number=token
        )

        # Mark table occupied
        table.status = "OCCUPIED"
        table.save()

class ActiveSessionListView(generics.ListAPIView):

    serializer_class = TableSessionSerializer
    permission_classes = [IsAdminOrStaff]

    def get_queryset(self):

        table_id = self.kwargs.get("table_id")

        qs = TableSession.objects.filter(
            is_active=True,
            closed_at__isnull=True,
            table__status="OCCUPIED",
        )

        if table_id:
            qs = qs.filter(table_id=table_id)

        return qs.select_related("table")


