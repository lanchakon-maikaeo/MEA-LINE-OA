# Who may do what (M12 Q2/Q3). Groups and policies only: the people and service users, and their
# API keys, auth tokens and S3 secret keys, are created by the owner in the console so no
# credential ever sits in Terraform state.

locals {
  c = "compartment ${var.compartment_name}"
  groups = {
    "faoj-readers"        = "Read-only view for the developer assistant: no objects, no secrets (Q2 b)"
    "faoj-ci"             = "GitHub Actions: push and prune images in OCIR only (Q3 a)"
    "faoj-deployers"      = "Hosts pull release images from OCIR"
    "faoj-backup-writers" = "Production backups (pgBackRest, restic) through the S3 API"
    "faoj-backup-readers" = "Restore drills on staging and the weekly copy outside OCI (Q13)"
  }
}

resource "oci_identity_group" "g" {
  for_each       = local.groups
  compartment_id = var.tenancy_ocid
  name           = each.key
  description    = each.value
}

resource "oci_identity_dynamic_group" "hosts" {
  compartment_id = var.tenancy_ocid
  name           = "faoj-hosts"
  description    = "faoj-prod and faoj-staging (instance principal)"
  matching_rule  = "ALL {instance.compartment.id = '${oci_identity_compartment.faoj.id}'}"
}

resource "oci_identity_policy" "faoj" {
  compartment_id = var.tenancy_ocid
  name           = "faoj"
  description    = "faoj least-privilege policy (M12)"
  statements = [
    # Hosts: metrics in, and the read-only counts of make oci-quota-check (E.2.1 guard 3).
    "Allow dynamic-group faoj-hosts to use metrics in ${local.c} where target.metrics.namespace = 'faoj_ops'",
    "Allow dynamic-group faoj-hosts to read metrics in tenancy",
    "Allow dynamic-group faoj-hosts to inspect compartments in tenancy",
    "Allow dynamic-group faoj-hosts to read instances in tenancy",
    "Allow dynamic-group faoj-hosts to read volume-family in tenancy",
    "Allow dynamic-group faoj-hosts to read buckets in tenancy",
    "Allow dynamic-group faoj-hosts to read objectstorage-namespaces in tenancy",
    "Allow dynamic-group faoj-hosts to read repos in tenancy",
    "Allow dynamic-group faoj-hosts to inspect load-balancers in tenancy",
    # Developer assistant: see configuration and health, never data, objects or secrets.
    "Allow group faoj-readers to inspect all-resources in tenancy",
    "Allow group faoj-readers to read instance-family in ${local.c}",
    "Allow group faoj-readers to read volume-family in ${local.c}",
    "Allow group faoj-readers to read virtual-network-family in ${local.c}",
    "Allow group faoj-readers to read buckets in ${local.c}",
    "Allow group faoj-readers to read repos in ${local.c}",
    "Allow group faoj-readers to read metrics in ${local.c}",
    "Allow group faoj-readers to read alarms in ${local.c}",
    "Allow group faoj-readers to read ons-topics in ${local.c}",
    "Allow group faoj-readers to read orm-stacks in ${local.c}",
    "Allow group faoj-readers to read orm-jobs in ${local.c}",
    "Allow group faoj-readers to read quotas in tenancy",
    # CI: images only.
    "Allow group faoj-ci to manage repos in ${local.c}",
    "Allow group faoj-deployers to read repos in ${local.c}",
    # Backups: the two buckets, nothing else.
    "Allow group faoj-backup-writers to read buckets in ${local.c} where any {target.bucket.name = 'faoj-backup', target.bucket.name = 'faoj-files'}",
    "Allow group faoj-backup-writers to manage objects in ${local.c} where any {target.bucket.name = 'faoj-backup', target.bucket.name = 'faoj-files'}",
    "Allow group faoj-backup-readers to read buckets in ${local.c} where any {target.bucket.name = 'faoj-backup', target.bucket.name = 'faoj-files'}",
    "Allow group faoj-backup-readers to read objects in ${local.c} where any {target.bucket.name = 'faoj-backup', target.bucket.name = 'faoj-files'}",
  ]
  depends_on = [oci_identity_group.g, oci_identity_dynamic_group.hosts]
}
