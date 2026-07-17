import uuid


class RequestIDMiddleware:
    HEADER = "HTTP_X_REQUEST_ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get(self.HEADER, "")

        request.request_id = (
            incoming if 8 <= len(incoming) <= 64 and incoming.isprintable() else uuid.uuid4().hex
        )
        response = self.get_response(request)
        response["X-Request-ID"] = request.request_id
        return response
