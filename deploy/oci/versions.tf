# OCI Always Free stack for faoj (M12, PROJECT.md E.2.1). Run in OCI Resource Manager: upload this
# folder as a zip, Plan, read the plan, Apply. Every resource here is inside the Always Free
# budget; deploy/server/oci/budget.json and test_m12 keep the numbers in step.
terraform {
  required_version = ">= 1.5.0"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 9.8.0, < 10.0.0"
    }
  }
}

provider "oci" {
  region = var.region
}
