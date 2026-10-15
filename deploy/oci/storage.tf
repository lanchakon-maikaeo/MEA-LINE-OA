# Object Storage for backups (Q13), OCIR for release images (Q3), and a Vault key for the backup
# passphrases' safe copy. The account stays Always Free only (Q14), so Object Storage is 20 GB
# across tiers: both buckets use Standard (Infrequent Access would add retrieval charges).

data "oci_objectstorage_namespace" "ns" {
  compartment_id = var.tenancy_ocid
}

resource "oci_objectstorage_bucket" "b" {
  for_each       = toset(["faoj-backup", "faoj-files"])
  compartment_id = oci_identity_compartment.faoj.id
  namespace      = data.oci_objectstorage_namespace.ns.namespace
  name           = each.key
  access_type    = "NoPublicAccess"
  storage_tier   = "Standard"
  versioning     = "Disabled"
}

resource "oci_artifacts_container_repository" "r" {
  for_each       = toset(["faoj/app", "faoj/db"])
  compartment_id = oci_identity_compartment.faoj.id
  display_name   = each.key
  is_public      = false
}

resource "oci_kms_vault" "faoj" {
  compartment_id = oci_identity_compartment.faoj.id
  display_name   = "faoj-vault"
  vault_type     = "DEFAULT"
}

resource "oci_kms_key" "secrets" {
  compartment_id      = oci_identity_compartment.faoj.id
  display_name        = "faoj-secrets"
  management_endpoint = oci_kms_vault.faoj.management_endpoint
  protection_mode     = "SOFTWARE"
  key_shape {
    algorithm = "AES"
    length    = 32
  }
}
