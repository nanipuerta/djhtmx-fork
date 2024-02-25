import typing as t

from django.http import HttpRequest, HttpResponse


class Middleware:
    def __init__(self, get_response: t.Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Ensure the Repository gets deallocated"""
        response = self.get_response(request)
        if repo := getattr(request, "djhtmx", None):
            repo.unlink()
            delattr(request, "djhtmx")

        return response
