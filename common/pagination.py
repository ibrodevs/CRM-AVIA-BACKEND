"""Пагинация по ТЗ §4.1: page/page_size, максимум 100."""
from rest_framework.pagination import PageNumberPagination


class DefaultPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100
