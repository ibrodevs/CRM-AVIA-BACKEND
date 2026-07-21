from rest_framework import serializers

from documents.models import Document, DocumentVersion


class DocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = [
            "id",
            "order",
            "service",
            "person",
            "company",
            "kind",
            "status",
            "title",
            "source",
            "current_version",
            "document_date",
            "document_number",
            "amount",
            "currency",
            "requires_signing",
            "is_confidential",
            "metadata",
            "created_at",
            "version",
        ]
        read_only_fields = ["id", "status", "current_version", "created_at", "version"]

    def validate(self, attrs):
        request = self.context.get("request")
        if request is None:
            return attrs
        tenant_id = request.user.tenant_id
        order = attrs.get("order", getattr(self.instance, "order", None))
        service = attrs.get("service", getattr(self.instance, "service", None))
        person = attrs.get("person", getattr(self.instance, "person", None))
        company = attrs.get("company", getattr(self.instance, "company", None))

        if order is not None and order.tenant_id != tenant_id:
            raise serializers.ValidationError({"order": ["Заказ относится к другому tenant"]})
        if service is not None:
            if service.tenant_id != tenant_id:
                raise serializers.ValidationError({"service": ["Услуга относится к другому tenant"]})
            if order is not None and service.order_id != order.id:
                raise serializers.ValidationError({"service": ["Услуга не принадлежит выбранному заказу"]})
            if order is None:
                attrs["order"] = service.order
        if person is not None and person.tenant_id != tenant_id:
            raise serializers.ValidationError({"person": ["Физлицо относится к другому tenant"]})
        if company is not None and company.tenant_id != tenant_id:
            raise serializers.ValidationError({"company": ["Компания относится к другому tenant"]})
        return attrs


class DocumentVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentVersion
        fields = [
            "id",
            "version",
            "checksum_sha256",
            "mime_type",
            "size_bytes",
            "original_name",
            "origin",
            "scan_status",
            "correction_reason",
            "created_at",
        ]
