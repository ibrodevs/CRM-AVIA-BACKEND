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
