from rest_framework.pagination import PageNumberPagination


class DefaultPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_page_size(self, request):
        if self.page_size_query_param in request.query_params:
            return super().get_page_size(request)
        if getattr(request.user, "is_authenticated", False):
            from accounts.models import UserPreference

            size = UserPreference.objects.filter(user=request.user).values_list("page_size", flat=True).first()
            if size in (10, 25, 50, 100):
                return size
        return self.page_size
