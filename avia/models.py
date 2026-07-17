from django.db import models

from common.models import TenantModel


class FlightItinerary(TenantModel):
    service = models.OneToOneField(
        "services.OrderService", on_delete=models.CASCADE, related_name="flight_itinerary"
    )
    kind = models.CharField(max_length=12, default="one_way")

    class Meta:
        db_table = "avia_itinerary"


class FlightSegment(TenantModel):
    itinerary = models.ForeignKey(FlightItinerary, on_delete=models.CASCADE, related_name="segments")
    sequence = models.PositiveSmallIntegerField()
    origin = models.CharField(max_length=3)
    destination = models.CharField(max_length=3)
    scheduled_departure = models.DateTimeField()
    scheduled_arrival = models.DateTimeField()
    actual_departure = models.DateTimeField(null=True, blank=True)
    actual_arrival = models.DateTimeField(null=True, blank=True)
    departure_timezone = models.CharField(max_length=63, blank=True)
    arrival_timezone = models.CharField(max_length=63, blank=True)
    airline = models.CharField(max_length=3)
    operating_carrier = models.CharField(max_length=3, blank=True)
    flight_number = models.CharField(max_length=8)
    aircraft = models.CharField(max_length=32, blank=True)
    departure_terminal = models.CharField(max_length=8, blank=True)
    arrival_terminal = models.CharField(max_length=8, blank=True)
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    stops = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "avia_segment"
        constraints = [
            models.UniqueConstraint(fields=["itinerary", "sequence"], name="uniq_avia_segment_sequence"),
        ]
        ordering = ["sequence"]


class AviaFare(TenantModel):
    service = models.ForeignKey("services.OrderService", on_delete=models.CASCADE, related_name="avia_fares")
    passenger = models.ForeignKey(
        "services.ServicePassenger",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="avia_fares",
    )
    cabin = models.CharField(max_length=16)
    booking_class = models.CharField(max_length=2, blank=True)
    brand = models.CharField(max_length=64, blank=True)
    baggage = models.CharField(max_length=32, blank=True)
    hand_luggage = models.CharField(max_length=32, blank=True)
    refundable = models.BooleanField(null=True, blank=True)
    exchangeable = models.BooleanField(null=True, blank=True)
    validating_carrier = models.CharField(max_length=3, blank=True)
    fare_rules = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "avia_fare"


class AviaBooking(TenantModel):
    """Бронь: provider + locator + environment уникальны (ТЗ §30)."""

    class Status(models.TextChoices):
        PENDING = "pending"
        BOOKED = "booked"
        TICKETED = "ticketed"
        CANCELLED = "cancelled"
        UNKNOWN = "unknown"
        FAILED = "failed"

    service = models.ForeignKey(
        "services.OrderService", on_delete=models.PROTECT, related_name="avia_bookings"
    )
    provider_adapter = models.CharField(max_length=100)
    environment = models.CharField(max_length=16, default="sandbox")
    locator = models.CharField(max_length=16, blank=True)
    client_request_id = models.CharField(max_length=80)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    ticketing_deadline = models.DateTimeField(null=True, blank=True)
    provider_snapshot = models.JSONField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "avia_booking"
        constraints = [
            models.UniqueConstraint(
                fields=["provider_adapter", "locator", "environment"],
                condition=~models.Q(locator=""),
                name="uniq_avia_booking_locator",
            ),
            models.UniqueConstraint(fields=["tenant", "client_request_id"], name="uniq_avia_client_request"),
        ]


class Ticket(TenantModel):
    """Билет: уникальный validating carrier + номер (ТЗ §9.1, §30)."""

    booking = models.ForeignKey(AviaBooking, on_delete=models.PROTECT, related_name="tickets")
    passenger = models.ForeignKey(
        "services.ServicePassenger", null=True, blank=True, on_delete=models.PROTECT, related_name="tickets"
    )
    validating_carrier = models.CharField(max_length=3)
    ticket_number = models.CharField(max_length=20)
    status = models.CharField(max_length=16, default="issued")
    issued_at = models.DateTimeField(null=True, blank=True)
    document = models.ForeignKey(
        "documents.Document", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "avia_ticket"
        constraints = [
            models.UniqueConstraint(
                fields=["validating_carrier", "ticket_number"], name="uniq_ticket_number"
            ),
        ]


class EMD(TenantModel):
    booking = models.ForeignKey(AviaBooking, on_delete=models.PROTECT, related_name="emds")
    passenger = models.ForeignKey(
        "services.ServicePassenger", null=True, blank=True, on_delete=models.PROTECT, related_name="emds"
    )
    emd_number = models.CharField(max_length=20)
    validating_carrier = models.CharField(max_length=3)
    reason = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=16, default="issued")

    class Meta:
        db_table = "avia_emd"
        constraints = [
            models.UniqueConstraint(fields=["validating_carrier", "emd_number"], name="uniq_emd_number"),
        ]


class SeatAssignment(TenantModel):
    """Место: один active assignment на сегмент+место; пассажир не имеет
    два active места на сегменте (ТЗ §30)."""

    segment = models.ForeignKey(FlightSegment, on_delete=models.CASCADE, related_name="seat_assignments")
    passenger = models.ForeignKey(
        "services.ServicePassenger", on_delete=models.CASCADE, related_name="seat_assignments"
    )
    seat_number = models.CharField(max_length=5)
    status = models.CharField(max_length=10, default="active")
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)

    class Meta:
        db_table = "avia_seat_assignment"
        constraints = [
            models.UniqueConstraint(
                fields=["segment", "seat_number"],
                condition=models.Q(status="active"),
                name="uniq_active_seat",
            ),
            models.UniqueConstraint(
                fields=["segment", "passenger"],
                condition=models.Q(status="active"),
                name="uniq_passenger_seat_per_segment",
            ),
        ]


class AncillaryAssignment(TenantModel):
    """Багаж/питание/страхование по пассажиру и сегменту (ТЗ §9.1)."""

    class Kind(models.TextChoices):
        BAGGAGE = "baggage"
        MEAL = "meal"
        INSURANCE = "insurance"

    service = models.ForeignKey(
        "services.OrderService", on_delete=models.CASCADE, related_name="avia_ancillaries"
    )
    passenger = models.ForeignKey(
        "services.ServicePassenger", on_delete=models.CASCADE, related_name="ancillaries"
    )
    segment = models.ForeignKey(
        FlightSegment, null=True, blank=True, on_delete=models.CASCADE, related_name="ancillaries"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    code = models.CharField(max_length=32, blank=True)
    description = models.CharField(max_length=255, blank=True)
    quantity = models.PositiveSmallIntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    status = models.CharField(max_length=12, default="selected")

    class Meta:
        db_table = "avia_ancillary"
