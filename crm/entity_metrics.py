"""Live entity totals. Amounts in different currencies are never added together."""
from django.db.models import Count, F, Sum


def customer_metrics(tenant_id, **customer):
    from finance.models import FinancialObligation
    from orders.models import Order
    from services.models import OrderService

    orders = Order.objects.filter(tenant_id=tenant_id, archived_at__isnull=True, **customer)
    services = OrderService.objects.filter(tenant_id=tenant_id, archived_at__isnull=True, order__in=orders).exclude(status__in=['cancelled', 'refunded'])
    obligations = FinancialObligation.objects.filter(tenant_id=tenant_id, archived_at__isnull=True, order__in=orders, direction='client_receivable', status__in=['open', 'partial'])
    return {
        'orders': orders.count(),
        'spent': {row['currency']: str(row['amount'] or 0) for row in services.values('currency').annotate(amount=Sum('client_total'))},
        'debt': {row['currency']: str(row['amount'] or 0) for row in obligations.values('currency').annotate(amount=Sum(F('original_amount') - F('paid_amount')))},
    }


def supplier_metrics(supplier):
    from aftersales.models import AfterSaleCase
    from services.models import OrderService

    services = OrderService.objects.filter(tenant_id=supplier.tenant_id, supplier=supplier, archived_at__isnull=True)
    counts = {row['status']: row['total'] for row in services.values('status').annotate(total=Count('id'))}
    latest = services.order_by('-updated_at').values_list('updated_at', flat=True).first()
    return {'bookings': sum(counts.get(status, 0) for status in ['booked', 'confirmed', 'issued']), 'issues': counts.get('issued', 0), 'refunds': AfterSaleCase.objects.filter(tenant_id=supplier.tenant_id, supplier=supplier, type='refund', status='completed', archived_at__isnull=True).count(), 'last_used': latest}
