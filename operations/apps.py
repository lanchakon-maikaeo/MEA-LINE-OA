from django.apps import AppConfig


class OperationsConfig(AppConfig):
    name = "operations"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from django.contrib.auth.signals import user_logged_in

        from operations.middleware import record_login_time

        user_logged_in.connect(record_login_time, dispatch_uid="operations.record_login_time")
