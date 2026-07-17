from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import user_permission_codes


class MetaView(APIView):
    def get(self, request):
        from orders.models import ORDER_TRANSITIONS, Order
        from services.models import SERVICE_KINDS, OrderService

        return Response(
            {
                "user": {
                    "id": str(request.user.id),
                    "permissions": sorted(user_permission_codes(request.user)),
                    "roles": [ur.role.code for ur in request.user.user_roles.select_related("role")],
                },
                "enums": {
                    "order_statuses": [
                        {"code": code, "display": label} for code, label in Order.Status.choices
                    ],
                    "order_stages": [{"code": code, "display": label} for code, label in Order.Stage.choices],
                    "order_transitions": {k: sorted(v) for k, v in ORDER_TRANSITIONS.items()},
                    "service_kinds": SERVICE_KINDS,
                    "service_statuses": [
                        {"code": code, "display": label} for code, label in OrderService.Status.choices
                    ],
                    "priorities": [
                        {"code": code, "display": label} for code, label in Order.Priority.choices
                    ],
                    "request_types": [
                        {"code": code, "display": label} for code, label in Order.RequestType.choices
                    ],
                },
                "settings": {
                    "base_currency": request.user.tenant.base_currency,
                    "timezone": request.user.tenant.timezone,
                    "multi_city_max_segments": _setting("MULTI_CITY_MAX_SEGMENTS", 6),
                },
                "features": (request.user.tenant.settings or {}).get("features", {}),
            }
        )


def _setting(name, default):
    from django.conf import settings

    return getattr(settings, name, default)
