"""Monthly cost model for the proposed Lumen deployments in AWS Asia Pacific (Mumbai, ap-south-1).

Every unit price below was read from AWS's official public price-list files
(https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<Offer>/current/ap-south-1/index.json), on-demand USD, on the
publication date given. Nothing here calls AWS with credentials or creates a resource. Re-run after re-reading the files.

    python deploy/aws/cost_estimate.py [--inr-per-usd 94.25]

What is NOT included (unknown until measured or not part of AWS): LLM and search provider usage, the domain name, taxes (GST),
Route 53 query charges, S3/backup-copy charges, support plans, and any data-transfer-out above the free tier.
"""
import argparse

H = 730  # hours per month used for hourly prices

PRICES = {  # (usd, unit, offer, published)
    "fargate_arm_vcpu_h": (0.02383, "vCPU-hour", "AmazonECS", "2026-11-11"),
    "fargate_arm_gb_h": (0.00261, "GB-hour", "AmazonECS", "2026-11-11"),
    "fargate_x86_vcpu_h": (0.04256, "vCPU-hour", "AmazonECS", "2026-11-11"),
    "fargate_x86_gb_h": (0.004655, "GB-hour", "AmazonECS", "2026-11-11"),
    "rds_t4g_micro_single_h": (0.021, "hour", "AmazonRDS (PostgreSQL)", "2026-11-13"),
    "rds_t4g_micro_multi_h": (0.042, "hour", "AmazonRDS (PostgreSQL)", "2026-11-13"),
    "rds_gp3_single_gb_mo": (0.131, "GB-month", "AmazonRDS", "2026-11-13"),
    "rds_gp3_multi_gb_mo": (0.262, "GB-month", "AmazonRDS", "2026-11-13"),
    "rds_backup_extra_gb_mo": (0.095, "GB-month beyond the free allocation", "AmazonRDS", "2026-11-13"),
    "elasticache_t4g_micro_redis_h": (0.02, "node-hour", "AmazonElastiCache", "2026-11-13"),
    "alb_h": (0.0239, "hour", "AWSELB", "2026-11-11"),
    "alb_lcu_h": (0.008, "LCU-hour", "AWSELB", "2026-11-11"),
    "ipv4_h": (0.005, "public IPv4 address-hour", "AmazonVPC", "2026-11-05"),
    "nat_h": (0.056, "hour", "AmazonEC2 (NatGateway)", "2026-11"),
    "nat_gb": (0.056, "GB processed", "AmazonEC2 (NatGateway)", "2026-11"),
    "secret_mo": (0.40, "secret-month", "AWSSecretsManager", "2026-11-11"),
    "secret_api_10k": (0.05, "10,000 API calls", "AWSSecretsManager", "2026-11-11"),
    "cw_ingest_gb": (0.67, "GB ingested", "AmazonCloudWatch", "2026-11-04"),
    "cw_store_gb_mo": (0.03, "GB-month", "AmazonCloudWatch", "2026-11-04"),
    "cw_alarm_mo": (0.10, "alarm-month", "AmazonCloudWatch", "2026-11-04"),
    "ecr_gb_mo": (0.10, "GB-month", "AmazonECR", "2026-11-13"),
    "ec2_t4g_medium_h": (0.0224, "hour (Linux on-demand)", "AmazonEC2", "2026-11"),
    "ebs_gp3_gb_mo": (0.0912, "GB-month", "AmazonEC2", "2026-11"),
    "ebs_snapshot_gb_mo": (0.05, "GB-month", "AmazonEC2", "2026-11"),
}
P = {k: v[0] for k, v in PRICES.items()}

# Workload assumptions: ESTIMATES to be replaced by measurements from the load test (Milestone B, step 6).
ASSUME = {
    "tasks": {  # name: (vCPU, GB)
        "frontend (nginx)": (0.25, 0.5),
        "backend": (0.25, 0.5),
        "agent + cache-redis sidecar": (0.5, 2.0),
    },
    "db_gb": 20, "log_gb_per_month": 2.0, "log_retention_gb": 2.0, "alarms": 10, "secrets": 7, "ecr_gb": 1.5,
    "alb_avg_lcu": 1.0, "nat_gb_per_month": 5.0, "vm_ebs_gb": 40, "vm_snapshot_gb": 20,
}


def fargate(arch, tasks, replicas=1):
    vc, gb = P[f"fargate_{arch}_vcpu_h"], P[f"fargate_{arch}_gb_h"]
    return {n: (c * vc + m * gb) * H * (replicas if n != "frontend (nginx)" else 1) for n, (c, m) in tasks.items()}


def common(lines, n_task_ips):
    lines["ALB (hourly + 1 LCU assumed)"] = P["alb_h"] * H + ASSUME["alb_avg_lcu"] * P["alb_lcu_h"] * H
    lines["Public IPv4: ALB (2 AZ addresses)"] = 2 * P["ipv4_h"] * H
    if n_task_ips:
        lines[f"Public IPv4: {n_task_ips} Fargate tasks (no NAT)"] = n_task_ips * P["ipv4_h"] * H
    lines["Secrets Manager (7 secrets)"] = ASSUME["secrets"] * P["secret_mo"] + 0.05
    lines["CloudWatch Logs (2 GB/mo) + 10 alarms"] = ASSUME["log_gb_per_month"] * P["cw_ingest_gb"] + ASSUME["log_retention_gb"] * P["cw_store_gb_mo"] + ASSUME["alarms"] * P["cw_alarm_mo"]
    lines["ECR (1.5 GB)"] = ASSUME["ecr_gb"] * P["ecr_gb_mo"]
    lines["ElastiCache Redis t4g.micro (rate limits, noeviction)"] = P["elasticache_t4g_micro_redis_h"] * H


def scenario_recommended(arch="arm"):
    L = {f"Fargate {arch.upper()}: {n}": v for n, v in fargate(arch, ASSUME["tasks"]).items()}
    L["RDS PostgreSQL t4g.micro single-AZ"] = P["rds_t4g_micro_single_h"] * H
    L["RDS gp3 storage 20 GB (backups within free allocation)"] = ASSUME["db_gb"] * P["rds_gp3_single_gb_mo"]
    common(L, 3)
    return L


def scenario_hardened(arch="arm"):
    L = {}
    for n, v in fargate(arch, ASSUME["tasks"], replicas=2).items():
        L[f"Fargate {arch.upper()}: {n}" + ("" if n.startswith("frontend") else " x2")] = v
    L["RDS PostgreSQL t4g.micro Multi-AZ"] = P["rds_t4g_micro_multi_h"] * H
    L["RDS gp3 storage 20 GB Multi-AZ"] = ASSUME["db_gb"] * P["rds_gp3_multi_gb_mo"]
    common(L, 0)
    L["NAT gateway (hourly)"] = P["nat_h"] * H
    L["NAT gateway (5 GB processed)"] = ASSUME["nat_gb_per_month"] * P["nat_gb"]
    return L


def scenario_vm():
    return {
        "EC2 t4g.medium (everything in Docker Compose)": P["ec2_t4g_medium_h"] * H,
        "EBS gp3 40 GB": ASSUME["vm_ebs_gb"] * P["ebs_gp3_gb_mo"],
        "EBS snapshots (20 GB)": ASSUME["vm_snapshot_gb"] * P["ebs_snapshot_gb_mo"],
        "Public IPv4 (1 address)": P["ipv4_h"] * H,
        "Secrets Manager (7 secrets)": ASSUME["secrets"] * P["secret_mo"] + 0.05,
        "CloudWatch Logs (2 GB/mo) + 5 alarms": ASSUME["log_gb_per_month"] * P["cw_ingest_gb"] + ASSUME["log_retention_gb"] * P["cw_store_gb_mo"] + 5 * P["cw_alarm_mo"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inr-per-usd", type=float, default=94.25, help="ECB reference rate 2026-11-13 via Frankfurter")
    a = ap.parse_args()
    out = {}
    for title, lines in [
        ("A. Recommended: ECS Fargate (ARM) + RDS + ElastiCache, tasks in security-group-locked public subnets", scenario_recommended("arm")),
        ("A-x86. Same as A but x86 Fargate (fallback if ARM images are not validated)", scenario_recommended("x86")),
        ("B. Hardened: private subnets + NAT, Multi-AZ RDS, 2 backend and 2 agent tasks (HA)", scenario_hardened("arm")),
        ("C. Budget: one EC2 t4g.medium with Docker Compose (no managed DB/Redis, no PITR)", scenario_vm()),
    ]:
        total = sum(lines.values())
        out[title] = total
        print(f"\n{title}")
        for k, v in lines.items():
            print(f"   {k:<62} ${v:>8.2f}")
        print(f"   {'TOTAL / month':<62} ${total:>8.2f}   = INR {total * a.inr_per_usd:>9,.0f}  (at {a.inr_per_usd} INR/USD)")
    print("\nNot included: LLM/search provider usage, domain name, GST, Route 53 hosted zone (~$0.50 list price, not read from the price file), data transfer out above the free tier, S3 copies.")


if __name__ == "__main__":
    main()
