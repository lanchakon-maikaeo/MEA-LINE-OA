variable "tenancy_ocid" {
  description = "Filled in by Resource Manager."
  type        = string
}

variable "region" {
  description = "Home region (Q15: Singapore). Always Free compute exists only there."
  type        = string
  default     = "ap-singapore-1"
}

variable "compartment_name" {
  type    = string
  default = "mea-faoj"
}

variable "ssh_public_key" {
  description = "Operator's SSH public key (Bastion sessions use it to reach the hosts)."
  type        = string
}

variable "admin_cidrs" {
  description = "Public addresses allowed to open Bastion sessions, e.g. [\"203.0.113.7/32\"]."
  type        = list(string)
  validation {
    condition     = length(var.admin_cidrs) > 0 && alltrue([for c in var.admin_cidrs : can(cidrnetmask(c)) && c != "0.0.0.0/0"])
    error_message = "List explicit CIDRs; 0.0.0.0/0 is not allowed."
  }
}

variable "alert_emails" {
  description = "Alert recipients: the owner and the Teams channel's e-mail address (Q12 a+c)."
  type        = list(string)
}

variable "hosts" {
  description = "faoj-prod and faoj-staging (hosts.auto.tfvars.json); together within Always Free."
  type = map(object({
    env       = string
    ocpus     = number
    memory_gb = number
    boot_gb   = number
    data_gb   = number
  }))
  validation {
    condition     = sum([for h in values(var.hosts) : h.ocpus]) <= 2 && sum([for h in values(var.hosts) : h.memory_gb]) <= 12
    error_message = "Ampere A1 Always Free is 2 OCPUs and 12 GB for the whole tenancy (E.2.1)."
  }
  validation {
    condition     = sum([for h in values(var.hosts) : h.boot_gb + h.data_gb]) <= 200 && alltrue([for h in values(var.hosts) : h.boot_gb >= 47])
    error_message = "Block storage (boot + data) is 200 GB for the tenancy; a boot volume is at least 47 GB."
  }
}

variable "ubuntu_image_ocid" {
  description = "Optional: pin an image; empty takes the newest Canonical Ubuntu 24.04 for A1."
  type        = string
  default     = ""
}

variable "create_budget" {
  description = "false when the zero-cost budget was already made by hand (as the sign-up guide says)."
  type        = bool
  default     = false
}
