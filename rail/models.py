from django.db import models

from common.models import TenantModel


class RailSegment(TenantModel):
    service = models.ForeignKey(
        "services.OrderService", on_delete=models.CASCADE, related_name="rail_segments"
    )
    sequence = models.PositiveSmallIntegerField(default=1)
    train_number = models.CharField(max_length=16)
    train_type = models.CharField(max_length=32, blank=True)
    carrier = models.CharField(max_length=100, blank=True)
    origin_station = models.CharField(max_length=100)
    destination_station = models.CharField(max_length=100)
    departure_at = models.DateTimeField()
    arrival_at = models.DateTimeField()
    departure_timezone = models.CharField(max_length=63, blank=True)
    arrival_timezone = models.CharField(max_length=63, blank=True)
    carriage_number = models.CharField(max_length=8, blank=True)
    service_class = models.CharField(max_length=16, blank=True)
    bedding_included = models.BooleanField(default=False)
    meal_included = models.BooleanField(default=False)
    refund_rules = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "rail_segment"
        constraints = [
            models.UniqueConstraint(fields=["service", "sequence"], name="uniq_rail_segment_sequence"),
        ]


class RailSeatHold(TenantModel):
    """Атомарное удержание мест (ТЗ §9.2): один active hold на сегмент+место."""

    class Status(models.TextChoices):
        HELD = "held"
        BOOKED = "booked"
        RELEASED = "released"
        EXPIRED = "expired"

    segment = models.ForeignKey(RailSegment, on_delete=models.CASCADE, related_name="seat_holds")
    passenger = models.ForeignKey(
        "services.ServicePassenger",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="rail_seats",
    )
    seat_number = models.CharField(max_length=8)
    seat_type = models.CharField(max_length=16, blank=True)
    gender_requirement = models.CharField(max_length=8, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.HELD)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "rail_seat_hold"
        constraints = [
            models.UniqueConstraint(
                fields=["segment", "seat_number"],
                condition=models.Q(status__in=["held", "booked"]),
                name="uniq_active_rail_seat",
            ),
        ]
