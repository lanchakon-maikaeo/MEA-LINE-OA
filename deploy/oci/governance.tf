# E.2.1 guards 1–2: one compartment, quotas that refuse anything outside the free budget, and a
# budget that mails on the first cent (optional here because the sign-up guide creates it).

resource "oci_identity_compartment" "faoj" {
  compartment_id = var.tenancy_ocid
  name           = var.compartment_name
  description    = "faoj staging and production (M12, Always Free only)"
  enable_delete  = false
}

locals {
  credit_quota_names = { "VM.Standard.E5.Flex" = "standard-e5", "VM.Standard.E4.Flex" = "standard-e4" }
  credit_shapes      = keys(local.credit_quota_names)
  credit_hosts       = { for name, h in var.hosts : name => h if contains(local.credit_shapes, h.shape) }
  # plantimestamp() is UTC; the credit ends at midnight Bangkok time of credit_until.
  credit_active = timecmp(plantimestamp(), "${var.credit_until}T00:00:00+07:00") < 0
  credit_use = {
    for shape in local.credit_shapes : shape => {
      ocpus     = sum(concat([0], [for h in values(local.credit_hosts) : h.ocpus if h.shape == shape]))
      memory_gb = sum(concat([0], [for h in values(local.credit_hosts) : h.memory_gb if h.shape == shape]))
    }
  }
  credit_block_gb = sum(concat([0], [for h in values(local.credit_hosts) : h.boot_gb + h.data_gb]))
  # Quota lines that let exactly the credit hosts in, and only while the credit lasts (ADR 0015).
  credit_quota = local.credit_active ? flatten([
    for shape, use in local.credit_use : use.ocpus == 0 ? [] : [
      "set compute-core quota ${local.credit_quota_names[shape]}-core-count to ${use.ocpus} in compartment ${var.compartment_name}",
      "set compute-memory quota ${local.credit_quota_names[shape]}-memory-count to ${use.memory_gb} in compartment ${var.compartment_name}",
    ]
  ]) : []
}

resource "oci_limits_quota" "always_free" {
  compartment_id = var.tenancy_ocid
  name           = "faoj-always-free"
  description    = "E.2.1: Ampere A1 within 2 OCPU / 12 GB and 200 GB block; ADR 0015: E4/E5 hosts on the trial credit until its end"
  statements = concat([
    "zero compute-core quotas in compartment ${var.compartment_name}",
    # Both the per-AD and the regional A1 counts: "zero" clears the regional ones too, and the
    # first apply (12 Oct 2026) failed with QuotaExceeded on standard-a1-core-regional-count.
    "set compute-core quota standard-a1-core-count to 2 in compartment ${var.compartment_name}",
    "set compute-core quota standard-a1-core-regional-count to 2 in compartment ${var.compartment_name}",
    "zero compute-memory quotas in compartment ${var.compartment_name}",
    "set compute-memory quota standard-a1-memory-count to 12 in compartment ${var.compartment_name}",
    "set compute-memory quota standard-a1-memory-regional-count to 12 in compartment ${var.compartment_name}",
    "set block-storage quota total-storage-gb to ${200 + (local.credit_active ? local.credit_block_gb : 0)} in compartment ${var.compartment_name}",
    "zero load-balancer quotas in compartment ${var.compartment_name}",
  ], local.credit_quota)
  depends_on = [oci_identity_compartment.faoj]
  lifecycle {
    precondition {
      condition     = length(local.credit_hosts) == 0 || local.credit_active
      error_message = "The trial credit ended on ${var.credit_until}: move off the E4/E5 hosts and remove them from hosts.auto.tfvars.json (ADR 0015)."
    }
    precondition {
      condition = (
        sum(concat([0], [for u in values(local.credit_use) : u.ocpus])) <= var.credit_caps.ocpus &&
        sum(concat([0], [for u in values(local.credit_use) : u.memory_gb])) <= var.credit_caps.memory_gb &&
        local.credit_block_gb <= var.credit_caps.block_gb
      )
      error_message = "Credit hosts exceed credit_caps (${var.credit_caps.ocpus} OCPU, ${var.credit_caps.memory_gb} GB, ${var.credit_caps.block_gb} GB block)."
    }
  }
}

resource "oci_budget_budget" "zero_cost" {
  count          = var.create_budget ? 1 : 0
  compartment_id = var.tenancy_ocid
  display_name   = "faoj-zero-cost"
  amount         = 1
  reset_period   = "MONTHLY"
  target_type    = "COMPARTMENT"
  targets        = [var.tenancy_ocid]
}

resource "oci_budget_alert_rule" "zero_cost" {
  for_each       = var.create_budget ? toset(["ACTUAL", "FORECAST"]) : toset([])
  budget_id      = oci_budget_budget.zero_cost[0].id
  display_name   = "faoj-any-${lower(each.key)}-cost"
  type           = each.key
  threshold      = 0.01
  threshold_type = "ABSOLUTE"
  recipients     = join(",", var.alert_emails)
  message        = "OCI cost above zero for the faoj tenancy: check for resources outside the Always Free budget (PROJECT.md E.2.1)."
}

# ADR 0015: mail at 50% and 80% of the trial credit spent by the faoj compartment. The credit
# cannot become a card charge (the account is never upgraded); this only warns before it runs out.
resource "oci_budget_budget" "credit" {
  count          = var.credit_budget_amount > 0 && length(local.credit_hosts) > 0 ? 1 : 0
  compartment_id = var.tenancy_ocid
  display_name   = "faoj-trial-credit"
  amount         = var.credit_budget_amount
  reset_period   = "MONTHLY"
  target_type    = "COMPARTMENT"
  targets        = [oci_identity_compartment.faoj.id]
}

resource "oci_budget_alert_rule" "credit" {
  for_each       = var.credit_budget_amount > 0 && length(local.credit_hosts) > 0 ? toset([for p in var.credit_alert_percents : tostring(p)]) : toset([])
  budget_id      = oci_budget_budget.credit[0].id
  display_name   = "faoj-credit-${each.key}pct"
  type           = "ACTUAL"
  threshold      = tonumber(each.key)
  threshold_type = "PERCENTAGE"
  recipients     = join(",", var.alert_emails)
  message        = "faoj has used ${each.key}% of the OCI trial credit (ends ${var.credit_until}). Plan the move off the E4/E5 host (ADR 0015)."
}
