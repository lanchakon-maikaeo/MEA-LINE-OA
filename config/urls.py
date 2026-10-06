from django.urls import path

from operations import views

urlpatterns = [
    path("", views.index, name="index"),
    path("health/live/", views.live, name="health-live"),
    path("health/ready/", views.ready, name="health-ready"),
]
