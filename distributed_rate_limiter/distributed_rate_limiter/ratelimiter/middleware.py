from .fixed_window_atomic import is_allowed
from django.http import JsonResponse

class RateLimitMiddleware:

    def __init__(self, get_response) -> None:
        self.get_response = get_response

    def __call__(self, request):

        user_id = self.get_client_ip(request)

        if not is_allowed(user_id):

            return JsonResponse({
                "error": "Too many requests",
                "detail": "Rate limit exceeded. Try again later." 
            }, status=429)

        response = self.get_reponse(request)

        return response
    

    def get_client_ip(self, request):

        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            ip = x_forwarded_for.split(",")[0]
        else:
            ip = request.META.get("REMOTE_ADDR")

        return ip
    