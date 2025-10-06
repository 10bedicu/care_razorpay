from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet


class HealthCheckViewSet(ViewSet):
    @action(detail=False, methods=["GET"])
    def ping(self, request):
        return Response({"status": "ok"})
