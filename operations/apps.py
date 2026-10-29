from django.apps import AppConfig


class OperationsConfig(AppConfig):
    name = "operations"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from django.contrib.auth.signals import user_logged_in

        from operations.middleware import record_login_time

        user_logged_in.connect(record_login_time, dispatch_uid="operations.record_login_time")

        from django.db.models.signals import post_migrate

        post_migrate.connect(
            _publish_problem_flow, sender=self, dispatch_uid="operations.problem_flow"
        )


def _publish_problem_flow(sender, verbosity=1, **kwargs):
    """ADR 0030 Q24: on production the division carousel flow is tested and published after
    each migrate until it has been once (problem_pick.autopublish does nothing elsewhere)."""
    from operations import problem_pick

    problem_pick.autopublish(print if verbosity else None)
