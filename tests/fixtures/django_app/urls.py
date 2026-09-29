"""Django/DRF fixture used by the scanner tests."""

from django.urls import path, re_path
from rest_framework import routers

from . import views

router = routers.DefaultRouter()
router.register(r"plots", views.PlotViewSet, basename="plot")

urlpatterns = [
    path("reports/<int:report_id>/", views.report_detail, name="report-detail"),
    re_path(r"^exports/$", views.export_list, name="export-list"),
] + router.urls
