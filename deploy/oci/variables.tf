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
  description = "Hosts (hosts.auto.tfvars.json). A1 hosts together stay within Always Free; an E4/E5 host runs on the trial credit until credit_until (ADR 0015)."
  type = map(object({
    env       = string
    shape     = optional(string, "VM.Standard.A1.Flex")
    instance  = optional(bool, true) # false: keep the host's volumes, create no VM (ADR 0015)
    ocpus     = number
    memory_gb = number
    boot_gb   = number
    data_gb   = number
  }))
  validation {
    condition     = alltrue([for h in values(var.hosts) : contains(["VM.Standard.A1.Flex", "VM.Standard.E5.Flex", "VM.Standard.E4.Flex"], h.shape)])
    error_message = "Shapes: VM.Standard.A1.Flex (Always Free) or, on the trial credit only, VM.Standard.E5.Flex / VM.Standard.E4.Flex."
  }
  validation {
    condition     = sum(concat([0], [for h in values(var.hosts) : h.ocpus if h.shape == "VM.Standard.A1.Flex"])) <= 2 && sum(concat([0], [for h in values(var.hosts) : h.memory_gb if h.shape == "VM.Standard.A1.Flex"])) <= 12
    error_message = "Ampere A1 Always Free is 2 OCPUs and 12 GB for the whole tenancy (E.2.1)."
  }
  validation {
    condition     = sum(concat([0], [for h in values(var.hosts) : h.boot_gb + h.data_gb if h.shape == "VM.Standard.A1.Flex"])) <= 200 && alltrue([for h in values(var.hosts) : h.boot_gb >= 47])
    error_message = "Block storage of the A1 hosts (boot + data) is 200 GB for the tenancy; a boot volume is at least 47 GB."
  }
  validation {
    condition     = alltrue([for h in values(var.hosts) : h.data_gb == 0 || h.data_gb >= 50])
    error_message = "A data volume is at least 50 GB in OCI; use data_gb = 0 to keep /srv/faoj on the boot volume."
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

# Trial credit (ADR 0015): SGD 400 that ends on 10 Nov 2026. The account stays FREE_TIER, so
# nothing is ever charged to the card; past the date or the caps below, plan refuses credit hosts.
variable "credit_until" {
  description = "Last day (exclusive, Asia/Bangkok) the trial credit may run E4/E5 hosts; YYYY-MM-DD."
  type        = string
  default     = "2026-11-10"
}

variable "credit_caps" {
  description = "Most the credit hosts may use together; the quota allows exactly what hosts ask for."
  type = object({
    ocpus     = number
    memory_gb = number
    block_gb  = number
  })
  default = { ocpus = 4, memory_gb = 32, block_gb = 200 }
}

variable "credit_budget_amount" {
  description = "Trial credit in the account currency (SGD); 0 skips the credit budget."
  type        = number
  default     = 400
}

variable "credit_alert_percents" {
  description = "Mail when actual credit use passes these percentages of credit_budget_amount."
  type        = list(number)
  default     = [50, 80]
}

variable "web_ip_host" {
  description = "Host that holds the reserved public IP for the staff web (<ip>.sslip.io, ADR 0015 Q7); moving it to another OCI host keeps the address. Empty: every host gets an ephemeral IP."
  type        = string
  default     = ""
  validation {
    condition     = var.web_ip_host == "" || can(regex("^faoj-[a-z0-9-]+$", var.web_ip_host))
    error_message = "web_ip_host is a host name from hosts, e.g. faoj-prod-e5."
  }
}

variable "move_by" {
  description = "Planned day to be off the credit hosts (ADR 0015 Q3: three days before credit_until); plan warns from then on."
  type        = string
  default     = "2026-11-07"
}
