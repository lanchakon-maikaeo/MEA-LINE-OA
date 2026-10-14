# faoj-prod and faoj-staging (Q10): Ubuntu 24.04 on Ampere A1, or E4/E5 on the trial credit
# (ADR 0015), Docker and the faoj-install helper from cloud-init. /srv/faoj is a data volume where data_gb > 0, otherwise a folder on the boot
# volume (staging: OCI volumes are at least 50 GB, which the 200 GB budget cannot fit twice).
# Hosts are never replaced in place (E.2.1 guard 5).

data "oci_identity_availability_domains" "ads" {
  compartment_id = var.tenancy_ocid
}

data "oci_core_images" "ubuntu" {
  compartment_id           = var.tenancy_ocid
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "24.04"
  shape                    = "VM.Standard.A1.Flex"
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

# x86 image for E4/E5 hosts on the trial credit (ADR 0015); the release images are multi-arch.
data "oci_core_images" "ubuntu_amd" {
  compartment_id           = var.tenancy_ocid
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "24.04"
  shape                    = "VM.Standard.E5.Flex"
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

locals {
  ad        = data.oci_identity_availability_domains.ads.availability_domains[0].name
  image     = var.ubuntu_image_ocid != "" ? var.ubuntu_image_ocid : data.oci_core_images.ubuntu.images[0].id
  image_amd = data.oci_core_images.ubuntu_amd.images[0].id
  data      = { for name, h in var.hosts : name => h if h.data_gb > 0 }
  # Only hosts with instance = true get a VM; a parked host keeps its data volume (ADR 0015).
  vms      = { for name, h in var.hosts : name => h if h.instance }
  attached = { for name, h in local.data : name => h if h.instance }
}

resource "oci_core_instance" "host" {
  for_each            = local.vms
  compartment_id      = oci_identity_compartment.faoj.id
  availability_domain = local.ad
  display_name        = each.key
  shape               = each.value.shape
  shape_config {
    ocpus         = each.value.ocpus
    memory_in_gbs = each.value.memory_gb
  }
  source_details {
    source_type             = "image"
    source_id               = each.value.shape == "VM.Standard.A1.Flex" ? local.image : local.image_amd
    boot_volume_size_in_gbs = each.value.boot_gb
  }
  create_vnic_details {
    subnet_id = oci_core_subnet.public.id
    # The web_ip_host gets the reserved address below instead of an ephemeral one.
    assign_public_ip = each.key != var.web_ip_host
    nsg_ids          = [oci_core_network_security_group.hosts.id]
    hostname_label   = replace(each.key, "faoj-", "")
  }
  agent_config {
    plugins_config {
      name          = "Bastion"
      desired_state = "ENABLED"
    }
  }
  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    user_data = base64encode(templatefile("${path.module}/templates/cloud-init.yaml.tftpl", {
      env         = each.value.env
      name        = each.key
      data_volume = each.value.data_gb > 0
    }))
  }
  lifecycle {
    prevent_destroy = true
    # A newer Ubuntu image or edited cloud-init must not rebuild a running host.
    ignore_changes = [source_details[0].source_id, metadata]
  }
  # The quota allows A1 (and, while the credit lasts, the E4/E5 hosts) in the compartment;
  # created in parallel, the hosts met the zeroed quota.
  depends_on = [oci_limits_quota.always_free]
}

resource "oci_core_volume" "data" {
  for_each            = local.data
  compartment_id      = oci_identity_compartment.faoj.id
  availability_domain = local.ad
  display_name        = "${each.key}-data"
  size_in_gbs         = each.value.data_gb
  lifecycle {
    prevent_destroy = true
  }
}

resource "oci_core_volume_attachment" "data" {
  for_each        = local.attached
  attachment_type = "paravirtualized"
  instance_id     = oci_core_instance.host[each.key].id
  volume_id       = oci_core_volume.data[each.key].id
  device          = "/dev/oracleoci/oraclevdb"
}

# Reserved public IP for the staff web (ADR 0015 Q7): set web_ip_host to another OCI host to move
# the address with it, so <ip>.sslip.io stays the same. LINE reaches the host through ngrok (Q2).
data "oci_core_vnic_attachments" "web" {
  count          = var.web_ip_host == "" ? 0 : 1
  compartment_id = oci_identity_compartment.faoj.id
  instance_id    = oci_core_instance.host[var.web_ip_host].id
}

data "oci_core_private_ips" "web" {
  count   = var.web_ip_host == "" ? 0 : 1
  vnic_id = data.oci_core_vnic_attachments.web[0].vnic_attachments[0].vnic_id
}

resource "oci_core_public_ip" "web" {
  count          = var.web_ip_host == "" ? 0 : 1
  compartment_id = oci_identity_compartment.faoj.id
  display_name   = "faoj-web"
  lifetime       = "RESERVED"
  private_ip_id  = [for ip in data.oci_core_private_ips.web[0].private_ips : ip.id if ip.is_primary][0]
  lifecycle {
    prevent_destroy = true
  }
  # The ephemeral address must be gone first (assign_public_ip = false on the host).
  depends_on = [oci_core_instance.host]
}

check "move_off_credit_hosts" {
  assert {
    condition     = length(local.credit_hosts) == 0 || timecmp(plantimestamp(), "${var.move_by}T00:00:00+07:00") < 0
    error_message = "It is past move_by (${var.move_by}) and E4/E5 hosts still run on the trial credit that ends ${var.credit_until}: move production off them now (ADR 0015)."
  }
}
