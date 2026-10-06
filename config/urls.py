from django.contrib.auth import views as auth_views
from django.urls import path

from operations import views

urlpatterns = [
    path("", views.index, name="index"),
    path("health/live/", views.live, name="health-live"),
    path("health/ready/", views.ready, name="health-ready"),
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(
            template_name="operations/login.html", redirect_authenticated_user=True
        ),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "accounts/password/change/",
        views.StaffPasswordChangeView.as_view(),
        name="password_change",
    ),
    path("console/", views.console, name="console"),
    path("divisions/<str:code>/", views.division_detail, name="division-detail"),
    path("console/admin/users/", views.admin_users, name="admin-users"),
    path("console/admin/users/new/", views.admin_user_new, name="admin-user-new"),
    path("console/admin/users/<int:pk>/", views.admin_user_detail, name="admin-user-detail"),
    path(
        "console/admin/users/<int:pk>/<slug:action>/",
        views.admin_user_action,
        name="admin-user-action",
    ),
    path("console/admin/registrations/", views.admin_registrations, name="admin-registrations"),
    path(
        "console/admin/registrations/<int:pk>/<slug:action>/",
        views.admin_registration_action,
        name="admin-registration-action",
    ),
    path("console/admin/divisions/", views.admin_divisions, name="admin-divisions"),
    path(
        "console/admin/divisions/<int:pk>/<slug:action>/",
        views.admin_division_action,
        name="admin-division-action",
    ),
]
