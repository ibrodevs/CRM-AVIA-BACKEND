"""Каталог permission-кодов (ТЗ §5.3) и стартовые роли.

Права хранятся в БД (RolePermission) и расширяемы; каталог задаёт известные
коды, их описания и наполнение системных ролей при bootstrap/seed.
"""

PERMISSIONS: dict[str, str] = {
    # Заказы
    "orders.view": "Просмотр заказов",
    "orders.create": "Создание заказов",
    "orders.change": "Изменение заказов",
    "orders.delete": "Архивация заказов",
    "orders.reassign": "Переназначение ответственного",
    "orders.change_status": "Смена статуса заказа",
    # Услуги (область по типу услуги — UserServiceAccess)
    "services.search": "Поиск услуг",
    "services.book": "Бронирование",
    "services.issue": "Выписка",
    "services.exchange": "Обмен",
    "services.refund": "Возврат",
    "services.cancel": "Аннуляция",
    "services.correct_document": "Корректировка представления документа",
    "services.send_document": "Отправка документов клиенту",
    # КП и карточки
    "offers.view": "Просмотр КП",
    "offers.create": "Создание КП",
    "offers.change": "Изменение КП",
    "offers.send": "Отправка КП/карточек",
    "offers.approve": "Согласование КП",
    "offers.archive": "Архивация КП",
    "offers.manage_templates": "Управление шаблонами КП",
    # Финансы
    "finance.view": "Просмотр финансов",
    "finance.create_payment": "Создание платежа",
    "finance.approve_payment": "Подтверждение платежа",
    "finance.refund": "Проведение возврата",
    "finance.reconcile": "Сверка",
    "finance.export": "Экспорт финансовых данных",
    # Документы
    "documents.view": "Просмотр документов",
    "documents.view_sensitive": "Просмотр паспортов и банковских документов",
    "documents.upload": "Загрузка документов",
    "documents.generate": "Генерация документов",
    "documents.sign": "Подписание документов",
    "documents.void": "Аннулирование документов",
    "documents.send": "Отправка документов",
    # Поставщики
    "suppliers.view": "Просмотр поставщиков",
    "suppliers.change": "Изменение поставщиков",
    "suppliers.manage_credentials": "Управление API-доступами поставщиков",
    "suppliers.manage_markup": "Управление правилами наценок",
    # Коммуникации
    "communications.view_internal": "Просмотр внутренних чатов",
    "communications.view_client": "Просмотр клиентских чатов",
    "communications.send": "Отправка сообщений",
    "communications.moderate": "Модерация сообщений",
    # CRM
    "crm.view": "Просмотр клиентов и компаний",
    "crm.change": "Изменение клиентов и компаний",
    "crm.view_person_documents": "Просмотр полных паспортных данных",
    "crm.force_create_duplicate": "Создание лица при возможном дубле",
    # Администрирование
    "users.manage": "Управление пользователями",
    "roles.manage": "Управление ролями",
    "settings.manage": "Управление настройками",
    "integrations.manage": "Управление интеграциями и credentials",
    "audit.view": "Просмотр аудита",
}

# Наполнение стартовых ролей (ТЗ §5.3: admin, operator, accountant, manager).
SYSTEM_ROLES: dict[str, dict] = {
    "admin": {
        "name": "Администратор",
        "permissions": sorted(PERMISSIONS.keys()),
    },
    "operator": {
        "name": "Оператор",
        "permissions": [
            "orders.view", "orders.create", "orders.change", "orders.change_status",
            "services.search", "services.book", "services.issue", "services.cancel",
            "services.send_document",
            "offers.view", "offers.create", "offers.change", "offers.send",
            "documents.view", "documents.upload", "documents.generate", "documents.send",
            "communications.view_internal", "communications.view_client", "communications.send",
            "crm.view", "crm.change",
            "suppliers.view",
        ],
    },
    "accountant": {
        "name": "Бухгалтер",
        "permissions": [
            "orders.view",
            "finance.view", "finance.create_payment", "finance.approve_payment",
            "finance.refund", "finance.reconcile", "finance.export",
            "documents.view", "documents.upload", "documents.generate", "documents.sign",
            "documents.void", "documents.send",
            "crm.view",
            "suppliers.view",
            "communications.view_internal", "communications.send",
        ],
    },
    "manager": {
        "name": "Менеджер",
        "permissions": [
            "orders.view", "orders.create", "orders.change", "orders.change_status",
            "orders.reassign",
            "services.search",
            "offers.view", "offers.create", "offers.change", "offers.send", "offers.approve",
            "documents.view",
            "communications.view_internal", "communications.view_client", "communications.send",
            "crm.view", "crm.change",
            "suppliers.view",
            "finance.view",
        ],
    },
}
