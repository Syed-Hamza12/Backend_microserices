from django.urls import path

from . import views

urlpatterns = [
    path("jobs/<int:job_id>/", views.JobStatusView.as_view(), name="job-status"),
]
