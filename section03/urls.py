from django.urls import path

from . import views

urlpatterns = [
    path("orders/", views.TenantOrderListView.as_view(), name="tenant-orders"),
    path("products/", views.TenantProductListView.as_view(), name="tenant-products"),
]
