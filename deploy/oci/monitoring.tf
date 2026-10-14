# Alerts (Q12 a+c, E.9): one topic mailed to the owner and to the Teams channel's address. The
# hosts post faoj_ops metrics every minute (faoj metrics); a missing heartbeat means the host or
# Docker is down. Alert text names the runbook, never a person or a LINE user ID.

resource "oci_ons_notification_topic" "alerts" {
  compartment_id = oci_identity_compartment.faoj.id
  name           = "faoj-alerts"
  description    = "faoj operations alerts (PROJECT.md E.9 runbooks)"
}

resource "oci_ons_subscription" "email" {
  for_each       = toset(var.alert_emails)
  compartment_id = oci_identity_compartment.faoj.id
  topic_id       = oci_ons_notification_topic.alerts.id
  protocol       = "EMAIL"
  endpoint       = each.value
}

locals {
  # Only envs with a VM on OCI: staging runs on the Mac (ADR 0015) and posts no OCI metrics.
  # Two hosts share an env while one moves.
  envs = toset([for h in values(local.vms) : h.env])
  # name => [env or "all", query condition, severity, runbook]
  alarms = merge(
    { for env in local.envs : "host-down-${env}" => [env, "heartbeat[1m]{env = \"${env}\"}.absent()", "CRITICAL", "RB01"] },
    { for env in local.envs : "disk-${env}" => [env, "disk_used_percent[5m]{env = \"${env}\"}.max() > 80", "WARNING", "RB01"] },
    { for env in local.envs : "memory-${env}" => [env, "mem_used_percent[5m]{env = \"${env}\"}.mean() > 85", "WARNING", "RB01"] },
    { for env in local.envs : "unhealthy-${env}" => [env, "containers_unhealthy[5m]{env = \"${env}\"}.min() > 0", "CRITICAL", "RB01"] },
    { for env in local.envs : "backup-age-${env}" => [env, "backup_age_seconds[10m]{env = \"${env}\"}.min() > 93600", "CRITICAL", "RB05"] },
    { for env in local.envs : "certificate-${env}" => [env, "cert_days_left[1h]{env = \"${env}\"}.max() < 14", "WARNING", "RB01"] },
    {
      "wal-archive-production" = ["production", "wal_archive_age_seconds[5m]{env = \"production\"}.min() > 600", "CRITICAL", "RB05"]
      "outbox-production"      = ["production", "outbox_oldest_pending_seconds[5m]{env = \"production\"}.min() > 300", "CRITICAL", "RB01"]
      "dead-letter-production" = ["production", "outbox_dead_letter[5m]{env = \"production\"}.max() > 0", "WARNING", "RB03"]
      "inbox-production"       = ["production", "inbox_pending_oldest_seconds[5m]{env = \"production\"}.min() > 300", "CRITICAL", "RB01"]
      "line-quota-production"  = ["production", "line_push_used_percent[1h]{env = \"production\"}.max() >= 80", "WARNING", "RB03"]
      "external-unknown"       = ["production", "external_unknown_oldest_seconds[15m]{env = \"production\"}.max() > 3600", "WARNING", "RB04"]
      "oci-quota"              = ["tenancy", "quota_over[1h]{env = \"tenancy\"}.max() > 0", "CRITICAL", "E.2.1"]
    },
  )
}

resource "oci_monitoring_alarm" "a" {
  for_each              = local.alarms
  compartment_id        = oci_identity_compartment.faoj.id
  metric_compartment_id = oci_identity_compartment.faoj.id
  display_name          = "faoj-${each.key}"
  namespace             = "faoj_ops"
  query                 = each.value[1]
  severity              = each.value[2]
  body                  = "faoj ${each.value[0]}: ${each.key}. Follow ${each.value[3]} in PROJECT.md E.9; this alert does not mean anyone has responded."
  destinations          = [oci_ons_notification_topic.alerts.id]
  is_enabled            = true
  pending_duration      = "PT5M"
  message_format        = "ONS_OPTIMIZED"
  # A firing alarm repeats every 4 hours until it clears.
  repeat_notification_duration = "PT4H"
}
