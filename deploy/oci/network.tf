# Public only on 80/443 (LINE has no fixed source address); SSH only from the Bastion inside the
# subnet. The subnet uses its own security list so the VCN default (SSH from anywhere) never applies.

resource "oci_core_vcn" "faoj" {
  compartment_id = oci_identity_compartment.faoj.id
  cidr_blocks    = ["10.20.0.0/16"]
  display_name   = "faoj-vcn"
  dns_label      = "faoj"
}

resource "oci_core_internet_gateway" "faoj" {
  compartment_id = oci_identity_compartment.faoj.id
  vcn_id         = oci_core_vcn.faoj.id
  display_name   = "faoj-igw"
  enabled        = true
}

resource "oci_core_route_table" "public" {
  compartment_id = oci_identity_compartment.faoj.id
  vcn_id         = oci_core_vcn.faoj.id
  display_name   = "faoj-public"
  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.faoj.id
  }
}

resource "oci_core_security_list" "egress_only" {
  compartment_id = oci_identity_compartment.faoj.id
  vcn_id         = oci_core_vcn.faoj.id
  display_name   = "faoj-egress-only"
  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }
}

resource "oci_core_subnet" "public" {
  compartment_id    = oci_identity_compartment.faoj.id
  vcn_id            = oci_core_vcn.faoj.id
  cidr_block        = "10.20.1.0/24"
  display_name      = "faoj-public"
  dns_label         = "pub"
  route_table_id    = oci_core_route_table.public.id
  security_list_ids = [oci_core_security_list.egress_only.id]
}

resource "oci_core_network_security_group" "hosts" {
  compartment_id = oci_identity_compartment.faoj.id
  vcn_id         = oci_core_vcn.faoj.id
  display_name   = "faoj-hosts"
}

locals {
  ingress = {
    http  = { port = 80, source = "0.0.0.0/0" }
    https = { port = 443, source = "0.0.0.0/0" }
    ssh   = { port = 22, source = oci_core_subnet.public.cidr_block } # Bastion's private endpoint
  }
}

resource "oci_core_network_security_group_security_rule" "ingress" {
  for_each                  = local.ingress
  network_security_group_id = oci_core_network_security_group.hosts.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = each.value.source
  source_type               = "CIDR_BLOCK"
  description               = each.key
  tcp_options {
    destination_port_range {
      min = each.value.port
      max = each.value.port
    }
  }
}

resource "oci_core_network_security_group_security_rule" "path_mtu" {
  network_security_group_id = oci_core_network_security_group.hosts.id
  direction                 = "INGRESS"
  protocol                  = "1"
  source                    = "0.0.0.0/0"
  source_type               = "CIDR_BLOCK"
  description               = "ICMP fragmentation needed (path MTU discovery)"
  icmp_options {
    type = 3
    code = 4
  }
}

resource "oci_bastion_bastion" "faoj" {
  bastion_type                 = "STANDARD"
  compartment_id               = oci_identity_compartment.faoj.id
  target_subnet_id             = oci_core_subnet.public.id
  name                         = "faojbastion"
  client_cidr_block_allow_list = var.admin_cidrs
  max_session_ttl_in_seconds   = 10800
}
