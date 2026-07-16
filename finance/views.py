"""Finance API (ТЗ §14.4)."""
from decimal import Decimal, InvalidOperation

from django.db.models import Q, Sum
from rest_framework import serializers, status as http
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require
from common.audit import audit
from common.errors import ApiError
from common.idempotency import idempotent_command
from common.money import money_dict
from common.pagination import DefaultPagination
from finance import services as finance_service
from finance.models import (
    FinancialAccount, FinancialObligation, LedgerTransaction, Payment, ReconciliationImport,
    ReconciliationRow, Refund,
)


class AccountSerializer(serializers.ModelSerializer):
    balance = serializers.SerializerMethodField()

    class Meta:
        model = FinancialAccount
        fields = ["id", "code", "name", "kind", "currency", "company", "supplier",
                  "is_active", "balance"]

    def get_balance(self, obj):
        debit = obj.entries.filter(direction="debit").aggregate(t=Sum("amount"))["t"] or 0
        credit = obj.entries.filter(direction="credit").aggregate(t=Sum("amount"))["t"] or 0
        return str(debit - credit)


class ObligationSerializer(serializers.ModelSerializer):
    outstanding = serializers.SerializerMethodField()

    class Meta:
        model = FinancialObligation
        fields = ["id", "order", "service", "direction", "due_date", "currency",
                  "original_amount", "paid_amount", "refunded_amount", "outstanding",
                  "status", "created_at"]
        read_only_fields = ["id", "paid_amount", "refunded_amount", "status", "created_at"]

    def get_outstanding(self, obj) -> str:
        return str(obj.outstanding_amount)


class PaymentSerializer(serializers.ModelSerializer):
    money = serializers.SerializerMethodField()
    allocations = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = ["id", "direction", "order", "payer_person", "payer_company", "supplier",
                  "method", "amount", "currency", "money", "provider_transaction_id",
                  "status", "confirmed_at", "comment", "allocations", "created_at",
                  "version"]
        read_only_fields = ["id", "status", "confirmed_at", "created_at", "version"]

    def get_money(self, obj):
        return money_dict(obj.amount, obj.currency)

    def get_allocations(self, obj):
        return [{"obligation": str(a.obligation_id), "amount": str(a.amount)}
                for a in obj.allocations.all()]


class RefundSerializer(serializers.ModelSerializer):
    class Meta:
        model = Refund
        fields = ["id", "payment", "obligation", "aftersale_case", "currency",
                  "original_paid", "supplier_penalty", "agency_service_fee",
                  "other_withholdings", "refund_amount", "formula_snapshot", "status",
                  "executed_at", "created_at"]
        read_only_fields = ["id", "refund_amount", "formula_snapshot", "status",
                            "executed_at", "created_at"]


class FinanceOverviewView(APIView):
    permission_classes = [require("finance.view")]

    def get(self, request):
        tenant_id = request.user.tenant_id
        receivable = FinancialObligation.objects.filter(
            tenant_id=tenant_id, direction="client_receivable",
            status__in=["open", "partial"],
        ).values("currency").annotate(total=Sum("original_amount") - Sum("paid_amount"))
        payable = FinancialObligation.objects.filter(
            tenant_id=tenant_id, direction="supplier_payable",
            status__in=["open", "partial"],
        ).values("currency").annotate(total=Sum("original_amount") - Sum("paid_amount"))
        recent = Payment.objects.filter(tenant_id=tenant_id).order_by("-created_at")[:10]
        return Response({
            "client_receivable": [money_dict(r["total"], r["currency"]) for r in receivable],
            "supplier_payable": [money_dict(r["total"], r["currency"]) for r in payable],
            "recent_payments": PaymentSerializer(recent, many=True).data,
        })


class AccountListView(APIView):
    permission_classes = [require("finance.view")]

    def get(self, request):
        accounts = FinancialAccount.objects.filter(tenant_id=request.user.tenant_id,
                                                   archived_at__isnull=True)
        return Response(AccountSerializer(accounts, many=True).data)


class TransactionListView(GenericAPIView):
    permission_classes = [require("finance.view")]
    pagination_class = DefaultPagination

    def get(self, request):
        qs = LedgerTransaction.objects.filter(
            tenant_id=request.user.tenant_id).prefetch_related("entries__account")
        if order_id := request.query_params.get("order"):
            qs = qs.filter(order_id=order_id)
        page = self.paginate_queryset(qs.order_by("-occurred_at"))
        return self.get_paginated_response([
            {"id": t.id, "occurred_at": t.occurred_at, "kind": t.kind,
             "description": t.description,
             "entries": [{"account": e.account.code, "direction": e.direction,
                          "amount": str(e.amount), "currency": e.currency}
                         for e in t.entries.all()]}
            for t in page
        ])


class ObligationListCreateView(GenericAPIView):
    permission_classes = [require("finance.view")]
    pagination_class = DefaultPagination
    serializer_class = ObligationSerializer

    def get(self, request):
        qs = FinancialObligation.objects.filter(tenant_id=request.user.tenant_id,
                                                archived_at__isnull=True)
        params = request.query_params
        if order_id := params.get("order"):
            qs = qs.filter(order_id=order_id)
        if direction := params.get("direction"):
            qs = qs.filter(direction=direction)
        if ob_status := params.get("status"):
            qs = qs.filter(status=ob_status)
        if params.get("overdue") in ("true", "1"):
            from django.utils import timezone

            qs = qs.filter(due_date__lt=timezone.now().date(),
                           status__in=["open", "partial"])
        page = self.paginate_queryset(qs.order_by("due_date"))
        return self.get_paginated_response(ObligationSerializer(page, many=True).data)

    def post(self, request):
        self.permission_classes = [require("finance.create_payment")]
        self.check_permissions(request)
        serializer = ObligationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        obligation = serializer.save(tenant_id=request.user.tenant_id,
                                     created_by=request.user)
        audit("finance.obligation_created", actor=request.user, resource=obligation,
              request=request)
        return Response(ObligationSerializer(obligation).data,
                        status=http.HTTP_201_CREATED)


class PaymentListCreateView(GenericAPIView):
    permission_classes = [require("finance.view")]
    pagination_class = DefaultPagination
    serializer_class = PaymentSerializer

    def get(self, request):
        qs = Payment.objects.filter(tenant_id=request.user.tenant_id,
                                    archived_at__isnull=True)
        params = request.query_params
        if order_id := params.get("order"):
            qs = qs.filter(order_id=order_id)
        if pay_status := params.get("status"):
            qs = qs.filter(status=pay_status)
        page = self.paginate_queryset(qs.order_by("-created_at"))
        return self.get_paginated_response(PaymentSerializer(page, many=True).data)

    @idempotent_command("finance.payment_create")
    def post(self, request):
        self.permission_classes = [require("finance.create_payment")]
        self.check_permissions(request)
        serializer = PaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = serializer.save(tenant_id=request.user.tenant_id,
                                  created_by=request.user)
        audit("finance.payment_created", actor=request.user, resource=payment,
              request=request)
        return Response(PaymentSerializer(payment).data, status=http.HTTP_201_CREATED)


class PaymentConfirmView(APIView):
    permission_classes = [require("finance.approve_payment")]

    @idempotent_command("finance.payment_confirm")
    def post(self, request, payment_id):
        payment = Payment.objects.filter(pk=payment_id,
                                         tenant_id=request.user.tenant_id).first()
        if payment is None:
            raise ApiError(code="NOT_FOUND", message="Платёж не найден", status_code=404)
        payment = finance_service.confirm_payment(
            payment_id=payment.pk, user=request.user,
            allocations=request.data.get("allocations"),
            expected_version=request.data.get("version"), request=request,
        )
        return Response(PaymentSerializer(payment).data)


class PaymentAllocateView(APIView):
    permission_classes = [require("finance.approve_payment")]

    def post(self, request, payment_id):
        from django.db import transaction

        payment = Payment.objects.filter(pk=payment_id,
                                         tenant_id=request.user.tenant_id).first()
        if payment is None:
            raise ApiError(code="NOT_FOUND", message="Платёж не найден", status_code=404)
        allocations = request.data.get("allocations", [])
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(pk=payment.pk)
            for allocation in allocations:
                try:
                    amount = Decimal(str(allocation.get("amount")))
                except (InvalidOperation, TypeError):
                    raise ApiError(code="VALIDATION_ERROR", message="Некорректная сумма",
                                   status_code=400) from None
                finance_service.allocate_payment(
                    payment=payment, obligation_id=allocation.get("obligation"),
                    amount=amount, user=request.user,
                )
            finance_service._maybe_mark_order_paid(payment.order, request.user)
        audit("finance.payment_allocated", actor=request.user, resource=payment,
              request=request)
        return Response(PaymentSerializer(payment).data)


class RefundListCreateView(GenericAPIView):
    permission_classes = [require("finance.view")]
    pagination_class = DefaultPagination
    serializer_class = RefundSerializer

    def get(self, request):
        qs = Refund.objects.filter(tenant_id=request.user.tenant_id).order_by("-created_at")
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(RefundSerializer(page, many=True).data)

    @idempotent_command("finance.refund_create")
    def post(self, request):
        self.permission_classes = [require("finance.refund")]
        self.check_permissions(request)
        data = request.data
        payment = None
        if payment_id := data.get("payment"):
            payment = Payment.objects.filter(pk=payment_id,
                                             tenant_id=request.user.tenant_id).first()
            if payment is None:
                raise ApiError(code="NOT_FOUND", message="Платёж не найден",
                               status_code=404)
        obligation = None
        if obligation_id := data.get("obligation"):
            obligation = FinancialObligation.objects.filter(
                pk=obligation_id, tenant_id=request.user.tenant_id).first()
        try:
            refund = finance_service.build_refund(
                tenant_id=request.user.tenant_id,
                currency=str(data.get("currency", "USD")),
                original_paid=Decimal(str(data.get("original_paid", "0"))),
                supplier_penalty=Decimal(str(data.get("supplier_penalty", "0"))),
                agency_service_fee=Decimal(str(data.get("agency_service_fee", "0"))),
                other_withholdings=Decimal(str(data.get("other_withholdings", "0"))),
                payment=payment, obligation=obligation, user=request.user,
            )
        except InvalidOperation:
            raise ApiError(code="VALIDATION_ERROR", message="Некорректные суммы",
                           status_code=400) from None
        audit("finance.refund_created", actor=request.user, resource=refund,
              request=request)
        return Response(RefundSerializer(refund).data, status=http.HTTP_201_CREATED)


class RefundExecuteView(APIView):
    permission_classes = [require("finance.refund")]

    @idempotent_command("finance.refund_execute")
    def post(self, request, refund_id):
        refund = Refund.objects.filter(pk=refund_id,
                                       tenant_id=request.user.tenant_id).first()
        if refund is None:
            raise ApiError(code="NOT_FOUND", message="Возврат не найден", status_code=404)
        refund = finance_service.execute_refund(refund_id=refund.pk, user=request.user,
                                                request=request)
        return Response(RefundSerializer(refund).data)


class CashflowView(APIView):
    permission_classes = [require("finance.view")]

    def get(self, request):
        from django.db.models.functions import TruncDate

        qs = Payment.objects.filter(tenant_id=request.user.tenant_id,
                                    status=Payment.Status.CONFIRMED)
        if date_from := request.query_params.get("from"):
            qs = qs.filter(confirmed_at__date__gte=date_from)
        if date_to := request.query_params.get("to"):
            qs = qs.filter(confirmed_at__date__lte=date_to)
        rows = (qs.annotate(date=TruncDate("confirmed_at"))
                .values("date", "direction", "currency")
                .annotate(total=Sum("amount")).order_by("date"))
        return Response({"cashflow": [
            {"date": r["date"], "direction": r["direction"],
             "money": money_dict(r["total"], r["currency"])} for r in rows
        ]})


class EconomicsView(APIView):
    permission_classes = [require("finance.view")]

    def get(self, request):
        from services.models import OrderService

        qs = OrderService.objects.filter(tenant_id=request.user.tenant_id,
                                         status__in=["issued", "confirmed"])
        rows = qs.values("kind", "currency").annotate(
            revenue=Sum("client_total"), cost=Sum("supplier_cost"),
            fees=Sum("agency_fee"), markup=Sum("markup"),
        )
        return Response({"by_kind": [
            {"kind": r["kind"], "currency": r["currency"],
             "revenue": str(r["revenue"] or 0), "cost": str(r["cost"] or 0),
             "fees": str(r["fees"] or 0), "markup": str(r["markup"] or 0)}
            for r in rows
        ]})


class ReconciliationImportView(APIView):
    permission_classes = [require("finance.reconcile")]

    def post(self, request):
        """Импорт CSV выписки: date,amount,currency,reference,counterparty."""
        import csv
        import io

        file = request.FILES.get("file")
        if file is None:
            raise ApiError(code="VALIDATION_ERROR", message="Файл file обязателен",
                           status_code=400)
        import_job = ReconciliationImport.objects.create(
            tenant_id=request.user.tenant_id, file_name=file.name,
            created_by=request.user,
        )
        reader = csv.DictReader(io.StringIO(file.read().decode("utf-8-sig")))
        rows = []
        for index, row in enumerate(reader):
            rows.append(ReconciliationRow(
                import_job=import_job, row_index=index,
                date=row.get("date") or None,
                amount=row.get("amount") or None,
                currency=(row.get("currency") or "")[:3],
                reference=row.get("reference", ""),
                counterparty=row.get("counterparty", ""),
            ))
        ReconciliationRow.objects.bulk_create(rows)
        import_job.rows_total = len(rows)
        # авто-сопоставление по сумме+валюте+reference
        matched = 0
        for row in import_job.rows.all():
            if row.amount is None:
                continue
            payment = Payment.objects.filter(
                tenant_id=request.user.tenant_id, amount=row.amount,
                currency=row.currency, status=Payment.Status.CONFIRMED,
            ).filter(Q(provider_transaction_id=row.reference)
                     | Q(provider_transaction_id="")).first()
            if payment is not None:
                row.matched_payment = payment
                row.match_type = "auto"
                row.status = "matched"
                row.save(update_fields=["matched_payment", "match_type", "status"])
                matched += 1
        import_job.rows_matched = matched
        import_job.status = "matched"
        import_job.save(update_fields=["rows_total", "rows_matched", "status"])
        audit("finance.reconciliation_imported", actor=request.user, resource=import_job,
              request=request)
        return Response({"id": str(import_job.id), "rows_total": import_job.rows_total,
                         "rows_matched": matched}, status=http.HTTP_201_CREATED)


class ReconciliationMatchView(APIView):
    permission_classes = [require("finance.reconcile")]

    def post(self, request, import_id):
        import_job = ReconciliationImport.objects.filter(
            pk=import_id, tenant_id=request.user.tenant_id).first()
        if import_job is None:
            raise ApiError(code="NOT_FOUND", message="Импорт не найден", status_code=404)
        row = import_job.rows.filter(pk=request.data.get("row")).first()
        if row is None:
            raise ApiError(code="NOT_FOUND", message="Строка не найдена", status_code=404)
        if request.data.get("ignore"):
            row.status = "ignored"
            row.save(update_fields=["status"])
        else:
            payment = Payment.objects.filter(pk=request.data.get("payment"),
                                             tenant_id=request.user.tenant_id).first()
            if payment is None:
                raise ApiError(code="NOT_FOUND", message="Платёж не найден",
                               status_code=404)
            row.matched_payment = payment
            row.match_type = "manual"
            row.status = "matched"
            row.save(update_fields=["matched_payment", "match_type", "status"])
        audit("finance.reconciliation_matched", actor=request.user, resource=import_job,
              request=request)
        return Response({"row": row.pk, "status": row.status})


class CompanyFinanceSummaryView(APIView):
    permission_classes = [require("finance.view")]

    def get(self, request, company_id):
        from crm.models import Company

        company = Company.objects.filter(pk=company_id,
                                         tenant_id=request.user.tenant_id).first()
        if company is None:
            raise ApiError(code="NOT_FOUND", message="Компания не найдена", status_code=404)
        obligations = FinancialObligation.objects.filter(
            order__client_company=company, direction="client_receivable")
        debt = obligations.filter(status__in=["open", "partial"]).values(
            "currency").annotate(total=Sum("original_amount") - Sum("paid_amount"))
        settlement = getattr(company, "settlement", None)
        return Response({
            "debt": [money_dict(d["total"], d["currency"]) for d in debt],
            "settlement": {
                "mode": settlement.mode if settlement else "prepayment",
                "deposit_balance": str(settlement.deposit_balance) if settlement else "0",
                "deposit_reserved": str(settlement.deposit_reserved) if settlement else "0",
                "credit_limit": str(settlement.credit_limit) if settlement else "0",
            },
            "orders_count": company.orders.count(),
        })
