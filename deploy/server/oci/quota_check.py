"""make oci-quota-check (M12, E.2.1 guard 3): read-only count of the tenancy's resources against
the Always Free budget; exit 1 and a metric when anything is over or about to be.

Runs hourly on faoj-prod inside the pinned OCI CLI image with the instance principal (inspect/
read only). `evaluate` is pure so it is tested without OCI (operations.tests.test_m12_operations).
"""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

GB = 1024**3


def evaluate(usage, budget):
    """Problems as (level, text); level "over" breaks the budget, "near" is an early warning."""
    problems = []
    for shape in sorted(set(usage["shapes"]) - set(budget["allowed_shapes"])):
        problems.append(("over", f"shape not in the budget: {shape}"))
    checks = [
        ("A1 OCPUs", usage["a1_ocpus"], budget["a1_ocpus"]),
        ("A1 memory GB", usage["a1_memory_gb"], budget["a1_memory_gb"]),
        ("block storage GB", usage["block_storage_gb"], budget["block_storage_gb"]),
        ("volume backups", usage["volume_backups"], budget["volume_backups"]),
        ("object storage GB", usage["object_storage_gb"], budget["object_storage_gb"]),
        ("load balancers", usage["load_balancers"], budget["load_balancers"]),
    ]
    for name, used, limit in checks:
        if used > limit:
            problems.append(("over", f"{name}: {used} > {limit}"))
    if (
        budget["object_storage_gb_alert"]
        < usage["object_storage_gb"]
        <= budget["object_storage_gb"]
    ):
        problems.append(
            (
                "near",
                f"object storage GB: {usage['object_storage_gb']} of {budget['object_storage_gb']}",
            )
        )
    for repo, count in sorted(usage["ocir_images"].items()):
        if count > budget["ocir_images_per_repo"]:
            problems.append(
                ("near", f"OCIR {repo}: {count} images (keep {budget['ocir_images_per_repo']})")
            )
    return problems


def gather(tenancy, signer, region):  # pragma: no cover - needs OCI
    import oci

    config = {"region": region}
    identity = oci.identity.IdentityClient(config, signer=signer)
    compute = oci.core.ComputeClient(config, signer=signer)
    blocks = oci.core.BlockstorageClient(config, signer=signer)
    storage = oci.object_storage.ObjectStorageClient(config, signer=signer)
    artifacts = oci.artifacts.ArtifactsClient(config, signer=signer)
    lbs = oci.load_balancer.LoadBalancerClient(config, signer=signer)
    page = oci.pagination.list_call_get_all_results
    compartments = [tenancy] + [
        c.id
        for c in page(identity.list_compartments, tenancy, compartment_id_in_subtree=True).data
        if c.lifecycle_state == "ACTIVE"
    ]
    domains = [d.name for d in identity.list_availability_domains(tenancy).data]
    namespace = storage.get_namespace().data
    usage = {
        "shapes": [],
        "a1_ocpus": 0,
        "a1_memory_gb": 0,
        "block_storage_gb": 0,
        "volume_backups": 0,
        "object_storage_gb": 0,
        "load_balancers": 0,
        "ocir_images": {},
    }
    size = 0
    for cid in compartments:
        for instance in page(compute.list_instances, cid).data:
            if instance.lifecycle_state in ("TERMINATED", "TERMINATING"):
                continue
            usage["shapes"].append(instance.shape)
            if instance.shape == "VM.Standard.A1.Flex":
                usage["a1_ocpus"] += instance.shape_config.ocpus
                usage["a1_memory_gb"] += instance.shape_config.memory_in_gbs
        for ad in domains:
            for boot in page(
                blocks.list_boot_volumes, availability_domain=ad, compartment_id=cid
            ).data:
                if boot.lifecycle_state != "TERMINATED":
                    usage["block_storage_gb"] += boot.size_in_gbs
        for volume in page(blocks.list_volumes, compartment_id=cid).data:
            if volume.lifecycle_state != "TERMINATED":
                usage["block_storage_gb"] += volume.size_in_gbs
        usage["volume_backups"] += len(page(blocks.list_volume_backups, cid).data)
        usage["volume_backups"] += len(page(blocks.list_boot_volume_backups, cid).data)
        for bucket in page(storage.list_buckets, namespace, cid).data:
            detail = storage.get_bucket(namespace, bucket.name, fields=["approximateSize"]).data
            size += detail.approximate_size or 0
        usage["load_balancers"] += len(page(lbs.list_load_balancers, cid).data)
        for image in page(artifacts.list_container_images, cid).data.items:
            usage["ocir_images"][image.repository_name] = (
                usage["ocir_images"].get(image.repository_name, 0) + 1
            )
    usage["object_storage_gb"] = round(size / GB, 2)
    return usage


def main():  # pragma: no cover - needs OCI
    import oci

    budget = json.loads((Path(__file__).with_name("budget.json")).read_text())
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    region = os.environ.get("OCI_REGION", "ap-singapore-1")
    usage = gather(signer.tenancy_id, signer, region)
    problems = evaluate(usage, budget)
    over = [text for level, text in problems if level == "over"]
    print(json.dumps({"usage": usage, "problems": problems}, indent=2))
    compartment = os.environ.get("OCI_COMPARTMENT_OCID", "")
    if compartment:
        monitoring = oci.monitoring.MonitoringClient(
            {"region": region},
            signer=signer,
            service_endpoint=f"https://telemetry-ingestion.{region}.oraclecloud.com",
        )
        stamp = datetime.now(UTC)
        values = {
            "quota_over": len(over),
            "quota_near": len(problems) - len(over),
            "a1_ocpus": usage["a1_ocpus"],
            "block_storage_gb": usage["block_storage_gb"],
            "object_storage_gb": usage["object_storage_gb"],
        }
        monitoring.post_metric_data(
            oci.monitoring.models.PostMetricDataDetails(
                metric_data=[
                    oci.monitoring.models.MetricDataDetails(
                        namespace="faoj_ops",
                        compartment_id=compartment,
                        name=name,
                        dimensions={"env": "tenancy"},
                        datapoints=[
                            oci.monitoring.models.Datapoint(timestamp=stamp, value=float(value))
                        ],
                    )
                    for name, value in values.items()
                ]
            )
        )
    sys.exit(1 if over else 0)


if __name__ == "__main__":
    main()
