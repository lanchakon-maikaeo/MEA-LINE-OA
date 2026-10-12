# E.2.1 guards 1–2: one compartment, quotas that refuse anything outside the free budget, and a
# budget that mails on the first cent (optional here because the sign-up guide creates it).

resource "oci_identity_compartment" "faoj" {
  compartment_id = var.tenancy_ocid
  name           = var.compartment_name
  description    = "faoj staging and production (M12, Always Free only)"
  enable_delete  = false
}

resource "oci_limits_quota" "always_free" {
  compartment_id = var.tenancy_ocid
  name           = "faoj-always-free"
  description    = "E.2.1: only Ampere A1 within 2 OCPU / 12 GB and 200 GB block storage"
  statements = [
    "zero compute-core quotas in compartment ${var.compartment_name}",
    "set compute-core quota standard-a1-core-count to 2 in compartment ${var.compartment_name}",
    "zero compute-memory quotas in compartment ${var.compartment_name}",
    "set compute-memory quota standard-a1-memory-count to 12 in compartment ${var.compartment_name}",
    "set block-storage quota total-storage-gb to 200 in compartment ${var.compartment_name}",
    "zero load-balancer quotas in compartment ${var.compartment_name}",
  ]
  depends_on = [oci_identity_compartment.faoj]
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
