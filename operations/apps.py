from django.apps import AppConfig


class OperationsConfig(AppConfig):
    name = "operations"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed

        from operations import access_log
        from operations.middleware import record_login_time

        user_logged_in.connect(record_login_time, dispatch_uid="operations.record_login_time")
        # Log การใช้งาน (31 Oct 2569 Q14, Q15): sign-ins, sign-outs and failed sign-ins.
        user_logged_in.connect(access_log.record_login, dispatch_uid="operations.audit_login")
        user_logged_out.connect(access_log.record_logout, dispatch_uid="operations.audit_logout")
        user_login_failed.connect(
            access_log.record_login_failed, dispatch_uid="operations.audit_login_failed"
        )

        from django.db.models.signals import post_migrate

        post_migrate.connect(
            _publish_problem_flow, sender=self, dispatch_uid="operations.problem_flow"
        )
        post_migrate.connect(_link_roster, sender=self, dispatch_uid="operations.link_roster")


def _publish_problem_flow(sender, verbosity=1, **kwargs):
    """ADR 0030 Q24: on production the division carousel flow is tested and published after
    each migrate until it has been once (problem_pick.autopublish does nothing elsewhere)."""
    from operations import problem_pick

    problem_pick.autopublish(print if verbosity else None)


def _link_roster(sender, **kwargs):
    """ADR 0050 Q1: web accounts not yet tied to ทะเบียนพนักงาน are matched after each migrate,
    so the accounts made before this release get their สังกัด on deploy. It changes nothing for
    an account already tied or one that matches nobody. ADR 0053 Q6: then every LINE that passed
    the employee check joins the staff account holding the same รหัสพนักงาน, if it has none."""
    from operations import roster_match, staff_line

    roster_match.link_all()
    staff_line.tie_all()
