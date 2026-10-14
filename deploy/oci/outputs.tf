locals {
  public_ips = { for name, i in oci_core_instance.host : name => (
    name == var.web_ip_host ? oci_core_public_ip.web[0].ip_address : i.public_ip
  ) }
}

output "hosts" {
  description = "Staff web name (sslip.io) and address of each host. LINE's webhook goes through the ngrok static domain (ADR 0015 Q2), not this name."
  value = { for name, i in oci_core_instance.host : name => {
    public_ip   = local.public_ips[name]
    public_host = "${replace(local.public_ips[name], ".", "-")}.sslip.io"
    staff_url   = "https://${replace(local.public_ips[name], ".", "-")}.sslip.io/"
    reserved_ip = name == var.web_ip_host
    instance_id = i.id
  } }
}

output "compartment_ocid" {
  value = oci_identity_compartment.faoj.id
}

output "object_storage" {
  description = "For compose.env: BACKUP_S3_ENDPOINT / BACKUP_DB_BUCKET; restic uses s3:https://<endpoint>/faoj-files/<env>."
  value = {
    namespace   = data.oci_objectstorage_namespace.ns.namespace
    s3_endpoint = "${data.oci_objectstorage_namespace.ns.namespace}.compat.objectstorage.${var.region}.oraclecloud.com"
    buckets     = [for b in oci_objectstorage_bucket.b : b.name]
  }
}

output "registry" {
  description = "OCIR paths for CI (push) and the hosts (pull)."
  value = {
    host = "${var.region}.ocir.io"
    app  = "${var.region}.ocir.io/${data.oci_objectstorage_namespace.ns.namespace}/faoj/app"
    db   = "${var.region}.ocir.io/${data.oci_objectstorage_namespace.ns.namespace}/faoj/db"
  }
}

output "bastion_id" {
  value = oci_bastion_bastion.faoj.id
}

output "alerts_topic_id" {
  value = oci_ons_notification_topic.alerts.id
}

output "vault" {
  value = { id = oci_kms_vault.faoj.id, key_id = oci_kms_key.secrets.id }
}
