from django.urls import path

from . import views

urlpatterns = [
    path("send/", views.SendEmailView.as_view(), name="send-email"),
    path("send-batch/", views.SendBatchEmailView.as_view(), name="send-batch-email"),
    path("rate-status/", views.RateLimitStatusView.as_view(), name="rate-status"),
    path("reset-rate/", views.ResetRateLimitView.as_view(), name="reset-rate"),
]
