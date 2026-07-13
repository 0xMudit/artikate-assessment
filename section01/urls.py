from django.urls import path

from . import views

urlpatterns = [
    path(
        "orders/summary/",
        views.OrderSummaryBrokenView.as_view(),
        name="order-summary-broken",
    ),
    path(
        "orders/summary/fixed/",
        views.OrderSummaryFixedView.as_view(),
        name="order-summary-fixed",
    ),
    path(
        "orders/profiler-compare/",
        views.ProfilerComparisonView.as_view(),
        name="profiler-compare",
    ),
    path("customers/", views.CustomerListView.as_view(), name="customer-list"),
]
