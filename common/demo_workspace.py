from datetime import timedelta
from decimal import Decimal

from django.utils import timezone


def seed_full_workspace(*, tenant, users, primary_person, primary_company, agreement, report):
    """Fill every API-backed CRM area with stable, repeatable demo records."""
    from aftersales.models import AfterSaleCase, AfterSaleHistoryEntry, AfterSaleQuote
    from calendar_app.models import CalendarEvent, Trip, TripConflict
    from communications.models import ChatThread, Message, ThreadParticipant
    from crm.models import ClientProfile, Company, Person, SettlementProfile
    from documents.models import Document, DocumentTemplate
    from finance.models import (
        FinancialAccount,
        FinancialObligation,
        LedgerEntry,
        LedgerTransaction,
        Payment,
    )
    from integrations.models import IntegrationErrorCode, IntegrationIncident, IntegrationLog
    from notifications.models import Notification, NotificationRule
    from offers.models import Proposal, ProposalItem, ProposalTemplate, ProposalVariant, ServiceCard
    from orders.models import Order, OrderStatusHistory, OrderTask
    from orders.services import create_order
    from services.models import OrderService, ServiceExtraCatalogItem
    from suppliers.models import Supplier, SupplierMarkupRule
    from workforce.models import MotivationRule, Shift, ShiftOperation, SlaInstance, SlaPolicy

    now = timezone.now()
    admin = users["admin"]
    operator = users["operator"]
    manager = users["manager"]
    accountant = users["accountant"]

    def mark(label, created):
        report["created" if created else "skipped"].append(label)

    people = [primary_person]
    for surname, given, middle, email, phone, city in [
        ("Садыкова", "Айжан", "Руслановна", "a.sadykova@example.com", "+996700220101", "Бишкек"),
        ("Ким", "Денис", "Александрович", "d.kim@example.com", "+996700220102", "Ош"),
        ("Абдрахманов", "Ильяс", "Муратович", "i.abdrakhmanov@example.com", "+996700220103", "Бишкек"),
        ("Петрова", "Елена", "Игоревна", "e.petrova@example.com", "+996700220104", "Каракол"),
        ("Токтосунова", "Мээрим", "Азизовна", "m.toktosunova@example.com", "+996700220105", "Бишкек"),
    ]:
        person, created = Person.objects.get_or_create(
            tenant=tenant,
            email=email,
            defaults={
                "surname": surname,
                "given_name": given,
                "middle_name": middle,
                "latin_surname": surname.upper(),
                "latin_given_name": given.upper(),
                "phone": phone,
                "city": city,
                "citizenship": "KG",
                "created_by": admin,
            },
        )
        mark(f"person:{email}", created)
        profile, created = ClientProfile.objects.get_or_create(
            tenant=tenant,
            person=person,
            defaults={"status": "active", "source": "website", "assigned_manager": manager, "created_by": admin},
        )
        mark(f"client:{email}", created)
        people.append(person)

    companies = [primary_company]
    for tax_id, legal, short, mode, balance, limit in [
        ("01508201920221", "ОсОО «Номад Логистик»", "Номад Логистик", "credit", "2500.00", "12000.00"),
        ("01508201920222", "ЗАО «Кыргыз Телеком Сервис»", "КТС", "credit", "0.00", "25000.00"),
        ("01508201920223", "ОсОО «Грин Энерджи Азия»", "Грин Энерджи", "deposit", "8400.00", "15000.00"),
    ]:
        company, created = Company.objects.get_or_create(
            tenant=tenant,
            tax_id=tax_id,
            defaults={
                "legal_name": legal,
                "short_name": short,
                "type": "llc",
                "legal_address": "Бишкек, Кыргызская Республика",
                "phone": "+996312900200",
                "email": f"office-{tax_id[-2:]}@example.com",
                "director": "Директор компании",
                "assigned_manager": manager,
                "created_by": admin,
            },
        )
        mark(f"company:{tax_id}", created)
        settlement, created = SettlementProfile.objects.get_or_create(
            tenant=tenant,
            company=company,
            defaults={
                "mode": mode,
                "currency": "USD",
                "deposit_balance": Decimal(balance),
                "credit_limit": Decimal(limit),
                "credit_days": 30,
            },
        )
        mark(f"settlement:{tax_id}", created)
        companies.append(company)

    suppliers = {}
    supplier_specs = [
        ("Demo GDS", "gds", ["avia", "rail"], True),
        ("Global Hotels Connect", "hotel_chain", ["hotel"], True),
        ("Silk Road Transfers", "dmc", ["transfer", "bus"], False),
        ("Central Asia Tours", "tour_operator", ["tour", "visa", "insurance"], False),
        ("Airport Services KG", "other", ["aeroexpress", "lounge"], False),
    ]
    for name, org_type, kinds, is_global in supplier_specs:
        supplier, created = Supplier.objects.get_or_create(
            tenant=tenant,
            name=name,
            defaults={
                "status": "active",
                "organization_type": org_type,
                "service_kinds": kinds,
                "currencies": ["USD", "KGS"],
                "is_global": is_global,
                "communication_methods": ["api", "email"],
                "created_by": admin,
            },
        )
        mark(f"supplier:{name}", created)
        suppliers[name] = supplier
        for kind in kinds:
            rule, created = SupplierMarkupRule.objects.get_or_create(
                tenant=tenant,
                supplier=supplier,
                service_kind=kind,
                amount_type="percent",
                defaults={"amount_value": Decimal("5.00"), "priority": 10, "created_by": admin},
            )
            mark(f"markup:{name}:{kind}", created)

    order_specs = [
        ("Командировка в Стамбул", people[0], None, "individual", "in_progress", "service_selection", "high", 5, 9, "FRU", "IST", operator),
        ("Отпуск на Иссык-Куле", people[1], None, "individual", "awaiting_confirmation", "booking", "normal", 12, 18, "FRU", "IKU", operator),
        ("Деловая поездка в Алматы", None, companies[1], "corporate", "awaiting_payment", "ticketing", "urgent", 3, 5, "FRU", "ALA", manager),
        ("Конференция в Дубае", None, companies[2], "corporate", "paid", "documents", "high", 20, 24, "FRU", "DXB", manager),
        ("Групповой тур в Ташкент", None, companies[3], "group", "needs_review", "booking", "urgent", 8, 13, "FRU", "TAS", operator),
        ("Встреча партнёров в Москве", people[2], None, "individual", "completed", "completed", "normal", -20, -16, "FRU", "SVO", operator),
        ("Семейная поездка в Анталью", people[3], None, "group", "on_hold", "service_selection", "normal", 32, 42, "FRU", "AYT", operator),
        ("Рабочая поездка в Бишкек", None, companies[0], "corporate", "new", "created", "normal", 2, 3, "OSS", "FRU", manager),
    ]
    orders = []
    for purpose, person, company, request_type, status, stage, priority, start_delta, end_delta, origin, destination, assigned in order_specs:
        order = Order.objects.filter(tenant=tenant, purpose=purpose).first()
        created = order is None
        if created:
            order = create_order(
                tenant_id=tenant.id,
                user=assigned,
                data={
                    "request_type": request_type,
                    "client_person": person,
                    "client_company": company,
                    "agreement": agreement if company == primary_company else None,
                    "purpose": purpose,
                    "priority": priority,
                    "preferred_channel": "telegram",
                    "planned_start": (now + timedelta(days=start_delta)).date(),
                    "planned_end": (now + timedelta(days=end_delta)).date(),
                    "base_currency": "USD",
                    "route": {
                        "kind": "round_trip",
                        "points": [
                            {"location_code": origin, "location_type": "airport", "location_name": origin},
                            {"location_code": destination, "location_type": "airport", "location_name": destination},
                        ],
                    },
                    "participants": [{"person": person or primary_person, "role": "passenger", "is_contact": True}],
                },
            )
        Order.objects.filter(pk=order.pk).update(status=status, stage=stage, priority=priority, operator=assigned, is_group=request_type == "group")
        order.refresh_from_db()
        mark(f"order:{purpose}", created)
        _, history_created = OrderStatusHistory.objects.get_or_create(
            order=order,
            to_status=status,
            defaults={"from_status": "new", "to_stage": stage, "changed_by": assigned, "reason": "Демонстрационный сценарий"},
        )
        mark(f"order-history:{purpose}", history_created)
        orders.append(order)

    service_specs = [
        (0, "avia", "FRU → IST · TK 347", "issued", "Demo GDS", "420.00", 5, 5),
        (0, "hotel", "Golden Horn Hotel · 4 ночи", "booked", "Global Hotels Connect", "360.00", 5, 9),
        (1, "rail", "Бишкек → Балыкчы · поезд 607", "confirmed", "Demo GDS", "48.00", 12, 12),
        (1, "transfer", "Аэропорт → отель · минивэн", "proposed", "Silk Road Transfers", "55.00", 12, 12),
        (2, "bus", "Бишкек → Алматы · бизнес-класс", "booked", "Silk Road Transfers", "90.00", 3, 3),
        (2, "tour", "Обзорная программа по Алматы", "proposed", "Central Asia Tours", "180.00", 4, 4),
        (3, "aeroexpress", "Экспресс до аэропорта", "issued", "Airport Services KG", "32.00", 20, 20),
        (3, "lounge", "Бизнес-зал аэропорта DXB", "confirmed", "Airport Services KG", "85.00", 20, 20),
        (4, "insurance", "Групповая туристическая страховка", "approval", "Central Asia Tours", "240.00", 8, 13),
        (4, "visa", "Визовая поддержка группы", "proposed", "Central Asia Tours", "510.00", 8, 13),
        (5, "avia", "FRU → SVO · SU 1883", "issued", "Demo GDS", "510.00", -20, -16),
        (5, "hotel", "Mercure Moscow · 3 ночи", "issued", "Global Hotels Connect", "450.00", -20, -16),
        (6, "transfer", "Трансфер для семьи · комфорт", "searching", "Silk Road Transfers", "120.00", 32, 32),
        (7, "avia", "OSS → FRU · K9 716", "proposed", "Demo GDS", "95.00", 2, 2),
    ]
    services = []
    for order_index, kind, title, status, supplier_name, amount, start_delta, end_delta in service_specs:
        service, created = OrderService.objects.get_or_create(
            tenant=tenant,
            order=orders[order_index],
            title=title,
            defaults={
                "kind": kind,
                "status": status,
                "supplier": suppliers[supplier_name],
                "source": "api",
                "currency": "USD",
                "supplier_cost": Decimal(amount) * Decimal("0.90"),
                "agency_fee": Decimal(amount) * Decimal("0.05"),
                "markup": Decimal(amount) * Decimal("0.05"),
                "client_total": Decimal(amount),
                "starts_at": now + timedelta(days=start_delta),
                "ends_at": now + timedelta(days=end_delta, hours=5),
                "created_by": admin,
            },
        )
        mark(f"service:{title}", created)
        services.append(service)

    for order, title, due_delta, priority in [
        (orders[0], "Подтвердить отель у поставщика", 1, "high"),
        (orders[2], "Проверить поступление оплаты", 0, "critical"),
        (orders[4], "Уточнить документы участников группы", 2, "high"),
        (orders[7], "Связаться с заказчиком", 0, "normal"),
    ]:
        task, created = OrderTask.objects.get_or_create(
            tenant=tenant,
            order=order,
            title=title,
            defaults={"assignee": order.operator, "due_at": now + timedelta(days=due_delta), "priority": priority, "created_by": admin},
        )
        mark(f"task:{title}", created)

    proposal_template, created = ProposalTemplate.objects.get_or_create(
        tenant=tenant,
        code="standard-demo",
        template_version=1,
        defaults={"name": "Стандартное коммерческое предложение", "body": "Маршрут, услуги и итоговая стоимость", "status": "published", "created_by": admin},
    )
    mark("proposal-template:standard", created)
    for index, status in enumerate(["draft", "prepared", "sent", "approved"], start=1):
        order = orders[index - 1]
        proposal, created = Proposal.objects.get_or_create(
            tenant=tenant,
            number=f"KP-DEMO-{index:03d}",
            defaults={
                "order": order,
                "type": "standard",
                "purpose": order.purpose,
                "status": status,
                "currency": "USD",
                "valid_until": now + timedelta(days=7 + index),
                "template": proposal_template,
                "created_by": order.operator,
            },
        )
        mark(f"proposal:{proposal.number}", created)
        variant, variant_created = ProposalVariant.objects.get_or_create(
            tenant=tenant,
            proposal=proposal,
            sequence=1,
            defaults={"name": "Оптимальный вариант", "status": "approved" if status == "approved" else "proposed", "created_by": order.operator},
        )
        mark(f"proposal-variant:{proposal.number}", variant_created)
        order_services = list(order.services.all())
        for item_index, service in enumerate(order_services, start=1):
            item, item_created = ProposalItem.objects.get_or_create(
                tenant=tenant,
                variant=variant,
                service=service,
                defaults={
                    "title": service.title,
                    "description": service.kind,
                    "quantity": 1,
                    "price_amount": service.client_total or 0,
                    "price_currency": service.currency,
                    "created_by": order.operator,
                },
            )
            mark(f"proposal-item:{proposal.number}:{item_index}", item_created)

    case_specs = [
        (orders[0], services[0], "refund", "review", "AS-DEMO-001", "Возврат авиабилета"),
        (orders[2], services[4], "exchange", "awaiting_client_approval", "AS-DEMO-002", "Обмен автобусного билета"),
        (orders[5], services[10], "certificate", "completed", "AS-DEMO-003", "Справка о перелёте"),
    ]
    cases = []
    for order, service, kind, status, number, label in case_specs:
        case, created = AfterSaleCase.objects.get_or_create(
            tenant=tenant,
            number=number,
            defaults={
                "order": order,
                "service": service,
                "type": kind,
                "responsible": operator,
                "supplier": service.supplier,
                "status": status,
                "deadline": now + timedelta(days=3),
                "currency": "USD",
                "financial_snapshot": {"service_total": str(service.client_total or 0)},
                "created_by": operator,
            },
        )
        mark(f"aftersale:{number}", created)
        quote, quote_created = AfterSaleQuote.objects.get_or_create(
            tenant=tenant,
            case=case,
            quote_version=1,
            defaults={
                "source": "manual",
                "currency": "USD",
                "original_paid": service.client_total or 0,
                "supplier_penalty": Decimal("25.00"),
                "agency_service_fee": Decimal("10.00"),
                "refund_total": max(Decimal("0"), (service.client_total or 0) - Decimal("35.00")),
                "details": {"label": label},
                "created_by": operator,
            },
        )
        mark(f"aftersale-quote:{number}", quote_created)
        if case.current_quote_id is None:
            case.current_quote = quote
            case.save(update_fields=["current_quote"])
        history, history_created = AfterSaleHistoryEntry.objects.get_or_create(
            case=case,
            action="created",
            defaults={"actor": operator, "details": {"label": label}},
        )
        mark(f"aftersale-history:{number}", history_created)
        cases.append(case)

    document_specs = [
        (orders[0], services[0], "ticket", "signed", "Электронный билет TK 347", "TKT-347-001", "420.00"),
        (orders[0], services[1], "voucher", "generated", "Ваучер Golden Horn Hotel", "VCH-1001", "360.00"),
        (orders[2], services[4], "invoice", "accounting", "Счёт на оплату поездки", "INV-2026-101", "270.00"),
        (orders[3], services[6], "itinerary_receipt", "signed", "Маршрутная квитанция DXB", "RCPT-2026-77", "117.00"),
        (orders[4], services[8], "insurance_policy", "generated", "Страховой полис группы", "INS-2026-55", "240.00"),
        (orders[5], services[10], "certificate", "signed", "Справка о совершённом перелёте", "CERT-2026-12", "510.00"),
        (orders[6], None, "contract", "signing", "Договор на туристическое обслуживание", "CTR-2026-44", None),
    ]
    documents = []
    for order, service, kind, status, title, number, amount in document_specs:
        document, created = Document.objects.get_or_create(
            tenant=tenant,
            document_number=number,
            defaults={
                "order": order,
                "service": service,
                "person": order.client_person,
                "company": order.client_company,
                "kind": kind,
                "status": status,
                "title": title,
                "source": "generated",
                "document_date": now.date(),
                "amount": Decimal(amount) if amount else None,
                "currency": "USD" if amount else "",
                "requires_signing": status in {"signing", "signed"},
                "metadata": {"demo": True},
                "created_by": admin,
            },
        )
        mark(f"document:{number}", created)
        documents.append(document)
    for code, name, kind in [("invoice-demo", "Счёт", "invoice"), ("voucher-demo", "Ваучер", "voucher"), ("act-demo", "Акт выполненных работ", "act")]:
        template, created = DocumentTemplate.objects.get_or_create(
            tenant=tenant,
            code=code,
            template_version=1,
            defaults={"name": name, "kind": kind, "body": f"Шаблон документа: {name}", "status": "published", "published_at": now, "created_by": admin},
        )
        mark(f"document-template:{code}", created)

    accounts = {}
    for code, name, kind in [
        ("BANK-USD", "Расчётный счёт USD", "bank"),
        ("CASH-USD", "Касса офиса", "cash"),
        ("DEPOSIT-USD", "Резервный депозит", "deposit"),
        ("CLIENT-USD", "Дебиторская задолженность", "client_receivable"),
        ("SUPPLIER-USD", "Расчёты с поставщиками", "supplier_payable"),
        ("REVENUE-USD", "Доходы агентства", "revenue"),
    ]:
        account, created = FinancialAccount.objects.get_or_create(
            tenant=tenant,
            code=code,
            currency="USD",
            defaults={"name": name, "kind": kind, "created_by": accountant},
        )
        mark(f"finance-account:{code}", created)
        accounts[code] = account

    for code, amount in [("CASH-USD", Decimal("1850.00")), ("DEPOSIT-USD", Decimal("25000.00"))]:
        transaction, created = LedgerTransaction.objects.get_or_create(
            tenant=tenant,
            kind="opening_balance",
            description=f"Начальный остаток {code}",
            defaults={"occurred_at": now - timedelta(days=30), "posted_by": accountant},
        )
        mark(f"ledger-opening:{code}", created)
        if created:
            LedgerEntry.objects.create(transaction=transaction, account=accounts[code], direction="debit", amount=amount, currency="USD")
            LedgerEntry.objects.create(transaction=transaction, account=accounts["REVENUE-USD"], direction="credit", amount=amount, currency="USD")

    obligations = []
    payables = []
    for index, order in enumerate(orders[:6]):
        amount = sum((service.client_total or 0) for service in order.services.all()) or Decimal("100.00")
        status = "partial" if index == 1 else ("settled" if index in {3, 5} else "open")
        paid = amount / 2 if status == "partial" else (amount if status == "settled" else Decimal("0"))
        obligation, created = FinancialObligation.objects.get_or_create(
            tenant=tenant,
            order=order,
            direction="client_receivable",
            defaults={
                "service": order.services.first(),
                "due_date": (now + timedelta(days=index - 2)).date(),
                "currency": "USD",
                "original_amount": amount,
                "paid_amount": paid,
                "status": status,
                "created_by": accountant,
            },
        )
        mark(f"obligation:{order.number}", created)
        obligations.append(obligation)
        payable, payable_created = FinancialObligation.objects.get_or_create(
            tenant=tenant,
            order=order,
            direction="supplier_payable",
            defaults={
                "service": order.services.first(),
                "due_date": (now + timedelta(days=index + 2)).date(),
                "currency": "USD",
                "original_amount": amount * Decimal("0.90"),
                "status": "open" if status != "settled" else "settled",
                "paid_amount": amount * Decimal("0.90") if status == "settled" else Decimal("0"),
                "created_by": accountant,
            },
        )
        if not payable.service_id:
            payable.service = order.services.first()
            payable.save(update_fields=["service", "updated_at"])
        mark(f"payable:{order.number}", payable_created)
        payables.append(payable)

    for index, obligation in enumerate(obligations[:4], start=1):
        amount = min(obligation.original_amount, Decimal("150.00") + Decimal(index * 75))
        payment, created = Payment.objects.get_or_create(
            tenant=tenant,
            provider_transaction_id=f"DEMO-PAY-{index:03d}",
            defaults={
                "direction": "incoming",
                "order": obligation.order,
                "payer_person": obligation.order.client_person,
                "payer_company": obligation.order.client_company,
                "method": "bank_transfer" if index % 2 else "card",
                "amount": amount,
                "currency": "USD",
                "status": "confirmed" if index != 4 else "pending",
                "confirmed_at": now - timedelta(days=index) if index != 4 else None,
                "confirmed_by": accountant if index != 4 else None,
                "comment": "Тестовый платёж из backend",
                "created_by": accountant,
            },
        )
        mark(f"payment:{index}", created)
        transaction, tx_created = LedgerTransaction.objects.get_or_create(
            tenant=tenant,
            order=obligation.order,
            kind="payment",
            description=f"Оплата по заказу {obligation.order.number}",
            defaults={"occurred_at": now - timedelta(days=index), "payment": payment, "posted_by": accountant},
        )
        mark(f"ledger:{index}", tx_created)
        if tx_created:
            LedgerEntry.objects.create(transaction=transaction, account=accounts["BANK-USD"], direction="debit", amount=amount, currency="USD")
            LedgerEntry.objects.create(transaction=transaction, account=accounts["CLIENT-USD"], direction="credit", amount=amount, currency="USD")

    for index, obligation in enumerate(payables[:3], start=1):
        amount = min(obligation.original_amount, Decimal("90.00") + Decimal(index * 60))
        supplier = obligation.service.supplier if obligation.service_id else None
        payment, created = Payment.objects.get_or_create(
            tenant=tenant,
            provider_transaction_id=f"DEMO-OUT-{index:03d}",
            defaults={
                "direction": "outgoing",
                "order": obligation.order,
                "supplier": supplier,
                "method": "bank_transfer",
                "amount": amount,
                "currency": "USD",
                "status": "confirmed" if index == 1 else "pending",
                "confirmed_at": now - timedelta(hours=index * 5) if index == 1 else None,
                "confirmed_by": accountant if index == 1 else None,
                "comment": "Тестовая оплата поставщику из backend",
                "created_by": accountant,
            },
        )
        mark(f"outgoing-payment:{index}", created)

    for index, order in enumerate(orders[:5], start=1):
        thread_type = "client" if index != 3 else "supplier"
        thread, created = ChatThread.objects.get_or_create(
            tenant=tenant,
            type=thread_type,
            order=order,
            title=f"{order.number} · {order.purpose}",
            defaults={"service": order.services.first(), "external_channel": "telegram" if thread_type == "client" else "email", "created_by": operator},
        )
        mark(f"chat:{order.number}", created)
        participant, participant_created = ThreadParticipant.objects.get_or_create(
            tenant=tenant,
            thread=thread,
            user=operator,
            defaults={"role": "operator", "created_by": operator},
        )
        mark(f"chat-participant:{order.number}", participant_created)
        if order.client_person:
            external, external_created = ThreadParticipant.objects.get_or_create(
                tenant=tenant,
                thread=thread,
                person=order.client_person,
                defaults={"role": "client", "external_identity": order.client_person.email, "created_by": operator},
            )
            mark(f"chat-client:{order.number}", external_created)
        for body, author_user, author_external in [
            ("Здравствуйте! Подбор вариантов по вашему заказу начат.", operator, ""),
            ("Спасибо, ожидаю варианты и итоговую стоимость.", None, order.client_name if hasattr(order, "client_name") else "Клиент"),
        ]:
            message, message_created = Message.objects.get_or_create(
                tenant=tenant,
                thread=thread,
                body=body,
                defaults={"author_user": author_user, "author_external": author_external, "delivery_state": "delivered", "created_by": author_user or operator},
            )
            mark(f"message:{order.number}:{body[:10]}", message_created)

    notification_specs = [
        ("critical", "orders", "Просрочен отклик по новой заявке", orders[7], "Требуется связаться с заказчиком"),
        ("high", "finance", "Срок оплаты по заказу", orders[2], "Проверьте поступление платежа"),
        ("medium", "documents", "Документ ожидает подписания", orders[6], "Договор готов к подписанию"),
        ("high", "integrations", "Ошибка ответа поставщика", orders[4], "Доступен повтор запроса или смена поставщика"),
        ("low", "communications", "Новое сообщение клиента", orders[1], "Клиент уточнил время трансфера"),
    ]
    for priority, source, title, order, body in notification_specs:
        notification, created = Notification.objects.get_or_create(
            tenant=tenant,
            user=admin,
            event_type=f"demo.{source}.{order.number}",
            defaults={
                "priority": priority,
                "source": source,
                "title": title,
                "body": body,
                "resource_type": "order",
                "resource_id": str(order.id),
                "deep_link": f"/orders/{order.id}",
            },
        )
        mark(f"notification:{source}:{order.number}", created)
    for event_type, name, priority in [("order.created", "Новые заказы", "high"), ("payment.overdue", "Просрочки оплат", "critical"), ("integration.failed", "Ошибки поставщиков", "high")]:
        rule, created = NotificationRule.objects.get_or_create(
            tenant=tenant,
            event_type=event_type,
            name=name,
            defaults={"priority": priority, "recipients": {"roles": ["admin", "operator"]}, "channels": ["desktop"], "created_by": admin},
        )
        mark(f"notification-rule:{event_type}", created)

    for index, order in enumerate(orders[:7]):
        start = now + timedelta(days=order_specs[index][7], hours=9)
        end = now + timedelta(days=order_specs[index][8], hours=19)
        trip, created = Trip.objects.get_or_create(
            tenant=tenant,
            order=order,
            defaults={
                "title": order.purpose,
                "starts_at": start,
                "ends_at": end,
                "status": "completed" if order.status == "completed" else ("upcoming" if start > now else "in_progress"),
                "criticality": "high" if order.priority in {"high", "urgent"} else "normal",
                "computed_at": now,
                "created_by": admin,
            },
        )
        mark(f"trip:{order.number}", created)
        event, event_created = CalendarEvent.objects.get_or_create(
            tenant=tenant,
            order=order,
            kind="order_trip",
            title=order.purpose,
            defaults={
                "description": f"Поездка по заказу {order.number}",
                "starts_at": start,
                "ends_at": end,
                "timezone": "Asia/Bishkek",
                "assignee": order.operator,
                "scope": "team",
                "priority": order.priority,
                "notification_method": "desktop",
                "created_by": admin,
            },
        )
        mark(f"calendar:{order.number}", event_created)
        if order.status == "needs_review":
            conflict, conflict_created = TripConflict.objects.get_or_create(
                trip=trip,
                kind="documents_missing",
                defaults={"severity": "warning", "details": {"message": "Не у всех участников заполнены документы"}},
            )
            mark(f"trip-conflict:{order.number}", conflict_created)

    error_codes = [
        ("PROVIDER_TIMEOUT", "Тайм-аут поставщика", "timeout", "high", True),
        ("AUTH_EXPIRED", "Истёк токен авторизации", "authentication", "critical", False),
        ("PRICE_CHANGED", "Цена изменилась", "availability", "medium", True),
    ]
    for code, title, category, severity, retry_safe in error_codes:
        error_code, created = IntegrationErrorCode.objects.get_or_create(
            code=code,
            defaults={"title": title, "description": title, "category": category, "default_severity": severity, "recommended_action": "Проверить соединение и повторить запрос", "is_retry_safe": retry_safe},
        )
        mark(f"integration-code:{code}", created)
    for index, (code, _, _, severity, _) in enumerate(error_codes):
        incident, created = IntegrationIncident.objects.get_or_create(
            tenant=tenant,
            correlation_id=f"demo-incident-{index + 1}",
            defaults={
                "error_code": code,
                "severity": severity,
                "provider_adapter": "demo-gds",
                "supplier": suppliers["Demo GDS"],
                "operation": "search" if index != 1 else "booking",
                "order": orders[index + 2],
                "service": orders[index + 2].services.first(),
                "sanitized_error": error_codes[index][1],
                "status": "open" if index != 2 else "assigned",
                "assignee": admin,
                "occurrences": index + 1,
            },
        )
        mark(f"incident:{code}", created)
        log, log_created = IntegrationLog.objects.get_or_create(
            tenant=tenant,
            correlation_id=f"demo-log-{index + 1}",
            defaults={
                "supplier": suppliers["Demo GDS"],
                "provider_adapter": "demo-gds",
                "operation": incident.operation,
                "http_status": 504 if code == "PROVIDER_TIMEOUT" else 401 if code == "AUTH_EXPIRED" else 409,
                "result": "error",
                "error_code": code,
                "duration_ms": 1200 + index * 350,
                "retries": index,
            },
        )
        mark(f"integration-log:{code}", log_created)

    sla_policy, created = SlaPolicy.objects.get_or_create(
        tenant=tenant,
        event_type="order.created",
        service_kind="",
        priority="",
        defaults={"response_minutes": 15, "resolution_minutes": 240, "created_by": admin},
    )
    mark("sla-policy:orders", created)
    for order in orders[-2:]:
        instance, instance_created = SlaInstance.objects.get_or_create(
            tenant=tenant,
            policy=sla_policy,
            resource_type="order",
            resource_id=str(order.id),
            defaults={"assignee": order.operator, "started_at": order.created_at, "response_deadline": order.created_at + timedelta(minutes=15), "created_by": admin},
        )
        mark(f"sla:{order.number}", instance_created)
    shift, created = Shift.objects.get_or_create(
        tenant=tenant,
        user=operator,
        status="open",
        defaults={"started_at": now - timedelta(hours=4), "opening_balance": Decimal("500.00"), "currency": "USD", "created_by": operator},
    )
    mark("shift:operator", created)
    for index, service in enumerate(services[:4], start=1):
        operation, operation_created = ShiftOperation.objects.get_or_create(
            shift=shift,
            kind="issue" if service.status == "issued" else "service_added",
            resource_type="service",
            resource_id=str(service.id),
            defaults={"amount": service.client_total, "currency": service.currency},
        )
        mark(f"shift-operation:{index}", operation_created)
    motivation, created = MotivationRule.objects.get_or_create(
        tenant=tenant,
        service_kind="*",
        defaults={"fee_percent": Decimal("10"), "markup_percent": Decimal("5"), "commission_percent": Decimal("5"), "effective_from": now.date(), "created_by": admin},
    )
    mark("motivation-rule:default", created)

    for kind, name, fee in [("avia", "Дополнительный багаж", "25.00"), ("hotel", "Ранний заезд", "35.00"), ("transfer", "Детское кресло", "10.00")]:
        item, created = ServiceExtraCatalogItem.objects.get_or_create(
            tenant=tenant,
            kind=kind,
            code=f"demo-{kind}",
            defaults={"name": name, "stage": "before_booking", "default_fee": Decimal(fee), "currency": "USD", "created_by": admin},
        )
        mark(f"service-extra:{kind}", created)
    card, created = ServiceCard.objects.get_or_create(
        tenant=tenant,
        order=orders[0],
        service=services[0],
        defaults={
            "kind": "avia",
            "scenario": "standard",
            "status": "delivered",
            "valid_until": now + timedelta(days=2),
            "price_snapshot": {"amount": str(services[0].client_total), "currency": "USD"},
            "content": {"title": services[0].title},
            "created_by": operator,
        },
    )
    mark("service-card:avia", created)
