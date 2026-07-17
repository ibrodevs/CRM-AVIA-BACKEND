from django.db import models

from common.models import TenantModel


class HotelStay(TenantModel):
    service = models.OneToOneField(
        "services.OrderService", on_delete=models.CASCADE, related_name="hotel_stay"
    )
    property_name = models.CharField(max_length=255)
    property_code = models.CharField(max_length=64, blank=True)
    city = models.CharField(max_length=100, blank=True)
    address = models.TextField(blank=True)
    stars = models.PositiveSmallIntegerField(null=True, blank=True)
    check_in = models.DateField()
    check_out = models.DateField()
    free_cancellation_until = models.DateTimeField(null=True, blank=True)
    payment_model = models.CharField(max_length=16, default="prepay")
    supplier_confirmation = models.CharField(max_length=64, blank=True)
    nightly_breakdown = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "hotels_stay"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(check_out__gt=models.F("check_in")), name="check_hotel_dates"
            ),
        ]


class HotelRoom(TenantModel):
    stay = models.ForeignKey(HotelStay, on_delete=models.CASCADE, related_name="rooms")
    room_ref = models.CharField(max_length=32)
    room_type = models.CharField(max_length=100)
    meal_plan = models.CharField(max_length=16, blank=True)
    capacity_adults = models.PositiveSmallIntegerField(default=2)
    capacity_children = models.PositiveSmallIntegerField(default=0)
    price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)

    class Meta:
        db_table = "hotels_room"
        constraints = [
            models.UniqueConstraint(fields=["stay", "room_ref"], name="uniq_room_ref"),
        ]


class HotelPlacement(TenantModel):
    """Размещение участника: один участник не размещён в двух комнатах на
    пересекающиеся даты (ТЗ §30 — контроль в save + exclusion-инвариант)."""

    room = models.ForeignKey(HotelRoom, on_delete=models.CASCADE, related_name="placements")
    participant = models.ForeignKey(
        "orders.OrderParticipant", on_delete=models.PROTECT, related_name="hotel_placements"
    )
    is_child = models.BooleanField(default=False)
    status = models.CharField(max_length=10, default="active")

    class Meta:
        db_table = "hotels_placement"
        constraints = [
            models.UniqueConstraint(
                fields=["room", "participant"],
                condition=models.Q(status="active"),
                name="uniq_room_participant",
            ),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError

        stay = self.room.stay
        overlapping = HotelPlacement.objects.filter(
            participant=self.participant,
            status="active",
            room__stay__check_in__lt=stay.check_out,
            room__stay__check_out__gt=stay.check_in,
        ).exclude(pk=self.pk)
        if overlapping.exists():
            raise ValidationError("Участник уже размещён в другой комнате на эти даты")

    def save(self, *args, **kwargs):
        self.clean()

        active = self.room.placements.filter(status="active").exclude(pk=self.pk)
        adults = sum(1 for p in active if not p.is_child) + (0 if self.is_child else 1)
        children = sum(1 for p in active if p.is_child) + (1 if self.is_child else 0)
        if adults > self.room.capacity_adults or children > self.room.capacity_children:
            from django.core.exceptions import ValidationError

            raise ValidationError("Превышена вместимость комнаты")
        super().save(*args, **kwargs)
