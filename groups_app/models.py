"""Групповые заказы и списки пассажиров (ТЗ §11)."""
from django.db import models

from common.models import TenantModel


class PassengerGroup(TenantModel):
    type = models.CharField(max_length=32, default="tourist")  # tourist/corporate/sport/...
    company = models.ForeignKey("crm.Company", null=True, blank=True,
                                on_delete=models.PROTECT, related_name="passenger_groups")
    name = models.CharField(max_length=255)
    owner = models.ForeignKey("accounts.User", null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="owned_groups")

    class Meta:
        db_table = "groups_passenger_group"


class GroupSubgroup(TenantModel):
    group = models.ForeignKey(PassengerGroup, on_delete=models.CASCADE,
                              related_name="subgroups")
    name = models.CharField(max_length=150)

    class Meta:
        db_table = "groups_subgroup"
        constraints = [
            models.UniqueConstraint(fields=["group", "name"], name="uniq_subgroup_name"),
        ]


class GroupOrder(TenantModel):
    """Групповой заказ: classic block или split individual (ТЗ §11)."""

    class Scenario(models.TextChoices):
        CLASSIC_BLOCK = "classic_block"
        SPLIT_INDIVIDUAL = "split_individual"

    class Status(models.TextChoices):
        PREPARING = "preparing"              # подготовка запроса
        REQUEST_SENT = "request_sent"        # запрос поставщику
        QUOTED = "quoted"                    # получены условия
        DEPOSIT_PENDING = "deposit_pending"  # ожидание депозита
        CONFIRMED = "confirmed"              # блок подтверждён
        NAMES_PENDING = "names_pending"      # ожидание списка
        NAMES_SUBMITTED = "names_submitted"
        PARTIALLY_TICKETED = "partially_ticketed"
        TICKETED = "ticketed"
        REDUCED = "reduced"                  # сокращение блока
        CANCELLED = "cancelled"

    order = models.OneToOneField("orders.Order", on_delete=models.PROTECT,
                                 related_name="group_order")
    group = models.ForeignKey(PassengerGroup, null=True, blank=True,
                              on_delete=models.PROTECT, related_name="group_orders")
    scenario = models.CharField(max_length=20, choices=Scenario.choices,
                                default=Scenario.CLASSIC_BLOCK)
    airline = models.CharField(max_length=3, blank=True)
    supplier = models.ForeignKey("suppliers.Supplier", null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="group_orders")
    requested_seats = models.PositiveSmallIntegerField(default=0)
    confirmed_seats = models.PositiveSmallIntegerField(default=0)
    deposit_deadline = models.DateTimeField(null=True, blank=True)
    names_deadline = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices,
                              default=Status.PREPARING)
    split_state = models.JSONField(null=True, blank=True)  # распределение split workflow

    class Meta:
        db_table = "groups_group_order"


GROUP_ORDER_TRANSITIONS: dict[str, set[str]] = {
    GroupOrder.Status.PREPARING: {GroupOrder.Status.REQUEST_SENT, GroupOrder.Status.CANCELLED},
    GroupOrder.Status.REQUEST_SENT: {GroupOrder.Status.QUOTED, GroupOrder.Status.CANCELLED},
    GroupOrder.Status.QUOTED: {GroupOrder.Status.DEPOSIT_PENDING, GroupOrder.Status.CANCELLED},
    GroupOrder.Status.DEPOSIT_PENDING: {GroupOrder.Status.CONFIRMED, GroupOrder.Status.CANCELLED},
    GroupOrder.Status.CONFIRMED: {GroupOrder.Status.NAMES_PENDING, GroupOrder.Status.REDUCED,
                                  GroupOrder.Status.CANCELLED},
    GroupOrder.Status.NAMES_PENDING: {GroupOrder.Status.NAMES_SUBMITTED,
                                      GroupOrder.Status.REDUCED, GroupOrder.Status.CANCELLED},
    GroupOrder.Status.NAMES_SUBMITTED: {GroupOrder.Status.PARTIALLY_TICKETED,
                                        GroupOrder.Status.TICKETED,
                                        GroupOrder.Status.REDUCED, GroupOrder.Status.CANCELLED},
    GroupOrder.Status.PARTIALLY_TICKETED: {GroupOrder.Status.TICKETED,
                                           GroupOrder.Status.REDUCED,
                                           GroupOrder.Status.CANCELLED},
    GroupOrder.Status.TICKETED: {GroupOrder.Status.REDUCED},
    GroupOrder.Status.REDUCED: {GroupOrder.Status.TICKETED, GroupOrder.Status.CANCELLED},
    GroupOrder.Status.CANCELLED: set(),
}


class GroupBlock(TenantModel):
    """Блок мест группового заказа."""

    group_order = models.ForeignKey(GroupOrder, on_delete=models.CASCADE,
                                    related_name="blocks")
    name = models.CharField(max_length=150)
    seats = models.PositiveSmallIntegerField()
    fare_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    fare_currency = models.CharField(max_length=3, blank=True)
    details = models.JSONField(default=dict, blank=True)  # рейс/даты/сегменты

    class Meta:
        db_table = "groups_block"
        constraints = [
            models.UniqueConstraint(fields=["group_order", "name"], name="uniq_block_name"),
        ]


class GroupPassengerAssignment(TenantModel):
    """Назначение пассажира в блок. Один пассажир не может быть в двух
    конфликтующих блоках одного группового заказа (ТЗ §11)."""

    block = models.ForeignKey(GroupBlock, on_delete=models.CASCADE,
                              related_name="assignments")
    participant = models.ForeignKey("orders.OrderParticipant", on_delete=models.PROTECT,
                                    related_name="group_assignments")
    seat_number = models.CharField(max_length=8, blank=True)
    baggage = models.CharField(max_length=32, blank=True)
    fare_code = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=16, default="assigned")
    # assigned/validated/submitted/ticketed/replaced/removed
    ticket_number = models.CharField(max_length=20, blank=True)
    validation_errors = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "groups_passenger_assignment"
        constraints = [
            models.UniqueConstraint(
                fields=["block", "participant"],
                condition=~models.Q(status__in=["replaced", "removed"]),
                name="uniq_block_participant",
            ),
        ]


class GroupRequest(TenantModel):
    """Запрос поставщику по групповому заказу."""

    group_order = models.ForeignKey(GroupOrder, on_delete=models.CASCADE,
                                    related_name="requests")
    subject = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, default="draft")  # draft/sent/answered

    class Meta:
        db_table = "groups_request"


class GroupSupplierResponse(TenantModel):
    request = models.ForeignKey(GroupRequest, on_delete=models.CASCADE,
                                related_name="responses")
    body = models.TextField(blank=True)
    quoted_fare = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    conditions = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "groups_supplier_response"


class RosterImportJob(TenantModel):
    """Импорт списка пассажиров с preview и reconcile (ТЗ §11)."""

    class Status(models.TextChoices):
        UPLOADED = "uploaded"
        PARSED = "parsed"
        PREVIEW_READY = "preview_ready"
        APPLIED = "applied"
        FAILED = "failed"

    order = models.ForeignKey("orders.Order", on_delete=models.CASCADE,
                              related_name="roster_imports")
    file_name = models.CharField(max_length=255, blank=True)
    column_mapping = models.JSONField(default=dict, blank=True)
    raw_rows = models.JSONField(default=list, blank=True)      # исходные строки (не перезаписываются)
    parsed_rows = models.JSONField(default=list, blank=True)   # нормализованные строки
    preview = models.JSONField(null=True, blank=True)          # diff same/changed/new/missing/conflict
    decisions = models.JSONField(null=True, blank=True)        # решения per row/field
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.UPLOADED)
    errors = models.JSONField(default=list, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "groups_roster_import"


class RosterMergeHistory(models.Model):
    """История каждого merge (ТЗ §11)."""

    id = models.BigAutoField(primary_key=True)
    import_job = models.ForeignKey(RosterImportJob, on_delete=models.CASCADE,
                                   related_name="merge_history")
    person = models.ForeignKey("crm.Person", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="+")
    row_index = models.PositiveIntegerField()
    decision = models.CharField(max_length=16)  # keep_current/use_incoming/merge/add/ignore
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    applied_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    applied_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "groups_roster_merge_history"
