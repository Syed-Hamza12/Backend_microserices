from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Business

from .models import JobTask


class JobStatusView(APIView):
    def get(self, request, job_id):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            job = JobTask.objects.get(business=business, pk=job_id)
        except JobTask.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NOT_FOUND", "message": "Job not found."}},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {
                "success": True,
                "data": {
                    "id": job.id,
                    "type": job.type,
                    "status": job.status,
                    "result": job.result,
                    "error": job.error,
                    "created_at": job.created_at,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                },
            }
        )
