"""Short-lived, per-user reuse of identical flight searches; no cross-account tokens."""
import hashlib
import json
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from finance.models import ExchangeRate
from integrations.adapters import get_adapter
from services.models import SearchSession
from suppliers.models import Supplier, SupplierCredential, SupplierMarkupRule, SupplierSearchPriority


def flight_routing_stamp(tenant_id):
    # Store metadata only. No API keys, passenger data or provider tokens enter the stamp.
    rows = {}
    for model in (Supplier, SupplierCredential, SupplierMarkupRule, SupplierSearchPriority, ExchangeRate):
        rows[model.__name__] = list(model.objects.filter(tenant_id=tenant_id).order_by('id').values_list('id', 'updated_at'))
    rows['environments'] = {key: get_adapter(key).configured_environment() for key in ('ttbooking',)}
    rows['mock'] = settings.ALLOW_MOCK_ADAPTER
    rows['pricing_day'] = timezone.localdate().isoformat()
    return hashlib.sha256(json.dumps(rows, default=str, sort_keys=True).encode()).hexdigest()


def reusable_flight_search(*, user, order, criteria, stamp):
    now = timezone.now()
    candidates = SearchSession.objects.filter(
        tenant_id=user.tenant_id, user=user, order=order, kind='avia', criteria=criteria,
        created_at__gte=now - timedelta(minutes=4), archived_at__isnull=True,
        job__payload__routing_stamp=stamp,
    ).filter(
        Q(status__in=['pending', 'running'], job__status__in=['queued', 'running'])
        | Q(status='completed', job__status='succeeded', job__completed_at__gte=now - timedelta(seconds=60), expires_at__gt=now)
    ).select_related('job').order_by('-created_at')
    for session in candidates[:5]:
        if session.status == 'completed':
            # Empty/error answers are not cached. Expired offers must never reappear.
            if not session.offers.exists() or session.offers.filter(expires_at__lte=now).exists():
                continue
        return session
    return None
