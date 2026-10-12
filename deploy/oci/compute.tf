# faoj-prod and faoj-staging (Q10): Ubuntu 24.04 on Ampere A1, a data volume at /srv/faoj, Docker
# and the faoj-install helper from cloud-init. Hosts are never replaced in place (E.2.1 guard 5).

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

locals {
  ad    = data.oci_identity_availability_domains.ads.availability_domains[0].name
  image = var.ubuntu_image_ocid != "" ? var.ubuntu_image_ocid : data.oci_core_images.ubuntu.images[0].id
}

resource "oci_core_instance" "host" {
  for_each            = var.hosts
  compartment_id      = oci_identity_compartment.faoj.id
  availability_domain = local.ad
  display_name        = each.key
  shape               = "VM.Standard.A1.Flex"
  shape_config {
    ocpus         = each.value.ocpus
    memory_in_gbs = each.value.memory_gb
  }
  source_details {
    source_type             = "image"
    source_id               = local.image
    boot_volume_size_in_gbs = each.value.boot_gb
  }
  create_vnic_details {
    subnet_id        = oci_core_subnet.public.id
    assign_public_ip = true
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
      env  = each.value.env
      name = each.key
    }))
  }
  lifecycle {
    prevent_destroy = true
    # A newer Ubuntu image or edited cloud-init must not rebuild a running host.
    ignore_changes = [source_details[0].source_id, metadata]
  }
}

resource "oci_core_volume" "data" {
  for_each            = var.hosts
  compartment_id      = oci_identity_compartment.faoj.id
  availability_domain = local.ad
  display_name        = "${each.key}-data"
  size_in_gbs         = each.value.data_gb
  lifecycle {
    prevent_destroy = true
  }
}

resource "oci_core_volume_attachment" "data" {
  for_each        = var.hosts
  attachment_type = "paravirtualized"
  instance_id     = oci_core_instance.host[each.key].id
  volume_id       = oci_core_volume.data[each.key].id
  device          = "/dev/oracleoci/oraclevdb"
}
