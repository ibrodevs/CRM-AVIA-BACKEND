from accounts.permissions import has_permission
from common.errors import ApiError
from documents.models import Document


def documents_visible_to(user):
    queryset = Document.objects.filter(tenant_id=user.tenant_id, archived_at__isnull=True)
    if not has_permission(user, "documents.view_sensitive"):
        queryset = queryset.filter(is_confidential=False)
    return queryset


def get_document_or_404(user, document_id) -> Document:
    document = documents_visible_to(user).filter(pk=document_id).first()
    if document is None:
        raise ApiError(code="NOT_FOUND", message="Документ не найден", status_code=404)
    return document
