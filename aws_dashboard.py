#!/usr/bin/env python3
"""
aws_dashboard.py - Streamlit GUI for the AWS environment overview tool

Requirements:
    pip install streamlit boto3 pandas

Run:
    streamlit run aws_dashboard.py

Auth: choose in the sidebar between (a) a named AWS CLI profile from your
standard credential chain (~/.aws/credentials, SSO, instance role), or
(b) entering an Access Key ID / Secret Access Key directly. Keys entered
in the sidebar stay in memory for the session only and are never written
to disk by this script.
Read-only — no resources are created, modified, or deleted.
"""

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError, BotoCoreError
except ImportError:
    st.error("boto3 is required. Install with: pip install boto3")
    st.stop()


st.set_page_config(page_title="AWS Environment Dashboard", page_icon="☁️", layout="wide")


# ---------- helpers ----------

def safe(label, fn):
    try:
        return fn(), None
    except ClientError as e:
        return None, f"{e.response['Error']['Code']}: {e.response['Error']['Message']}"
    except (BotoCoreError, Exception) as e:
        return None, str(e)


def warn_if_error(err):
    if err:
        st.caption(f"⚠️ {err}")


@st.cache_resource(show_spinner=False)
def get_session(profile, region, access_key=None, secret_key=None, session_token=None):
    kwargs = {}
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
        if session_token:
            kwargs["aws_session_token"] = session_token
    elif profile and profile != "default":
        kwargs["profile_name"] = profile
    if region:
        kwargs["region_name"] = region
    return boto3.Session(**kwargs)


@st.cache_data(ttl=120, show_spinner=False)
def fetch_account(_session):
    sts = _session.client("sts")
    ident, e1 = safe("identity", lambda: sts.get_caller_identity())
    iam = _session.client("iam")
    summary, e2 = safe("iam summary", lambda: iam.get_account_summary()["SummaryMap"])
    return ident, summary, e1, e2


@st.cache_data(ttl=60, show_spinner=False)
def fetch_ec2(_session, region):
    ec2 = _session.client("ec2", region_name=region)

    def list_instances():
        rows = []
        eni_rows = []
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate():
            for res in page["Reservations"]:
                for inst in res["Instances"]:
                    name = next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), "")
                    sgs = ", ".join(sg["GroupName"] for sg in inst.get("SecurityGroups", []))
                    enis = inst.get("NetworkInterfaces", [])
                    rows.append({
                        "Name": name,
                        "Instance ID": inst["InstanceId"],
                        "Type": inst["InstanceType"],
                        "State": inst["State"]["Name"],
                        "Private IP": inst.get("PrivateIpAddress", ""),
                        "Public IP": inst.get("PublicIpAddress", ""),
                        "VPC ID": inst.get("VpcId", ""),
                        "Subnet ID": inst.get("SubnetId", ""),
                        "Security Groups": sgs,
                        "ENI Count": len(enis),
                        "AZ": inst.get("Placement", {}).get("AvailabilityZone", ""),
                        "Launch Time": inst.get("LaunchTime"),
                    })
                    for eni in enis:
                        eni_rows.append({
                            "Instance ID": inst["InstanceId"],
                            "Instance Name": name,
                            "ENI ID": eni["NetworkInterfaceId"],
                            "Private IP": eni.get("PrivateIpAddress", ""),
                            "Public IP": eni.get("Association", {}).get("PublicIp", ""),
                            "Subnet ID": eni.get("SubnetId", ""),
                            "Status": eni.get("Status", ""),
                            "Security Groups": ", ".join(g["GroupName"] for g in eni.get("Groups", [])),
                        })
        return pd.DataFrame(rows), pd.DataFrame(eni_rows)

    result, e1 = safe("describe_instances", list_instances)
    instances_df, enis_df = result if result else (None, None)

    def list_volumes():
        paginator = ec2.get_paginator("describe_volumes")
        vols = []
        for page in paginator.paginate():
            vols.extend(page["Volumes"])
        return vols

    volumes, e2 = safe("describe_volumes", list_volumes)

    eips, e3 = safe("describe_addresses", lambda: ec2.describe_addresses()["Addresses"])

    return instances_df, enis_df, volumes, eips, [e1, e2, e3]


@st.cache_data(ttl=60, show_spinner=False)
def fetch_load_balancers(_session, region):
    """Map each EC2 instance ID to the load balancer(s)/target group(s) it's registered in."""
    elbv2 = _session.client("elbv2", region_name=region)

    lb_list, e1 = safe("describe_load_balancers", lambda: elbv2.describe_load_balancers()["LoadBalancers"])
    lb_by_arn = {lb["LoadBalancerArn"]: lb for lb in (lb_list or [])}

    tg_list, e2 = safe("describe_target_groups", lambda: elbv2.describe_target_groups()["TargetGroups"])

    instance_to_lbs = {}
    lb_rows = []
    if tg_list:
        for tg in tg_list:
            health, e3 = safe(
                f"describe_target_health({tg['TargetGroupName']})",
                lambda tg=tg: elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])["TargetHealthDescriptions"],
            )
            if not health:
                continue
            lb_names = [lb_by_arn.get(arn, {}).get("LoadBalancerName", arn) for arn in tg.get("LoadBalancerArns", [])]
            lb_name_str = ", ".join(lb_names) if lb_names else "(no LB attached)"
            for th in health:
                target_id = th["Target"]["Id"]
                port = th["Target"].get("Port", "")
                state = th["TargetHealth"]["State"]
                if target_id.startswith("i-"):
                    instance_to_lbs.setdefault(target_id, []).append(
                        f"{lb_name_str} \u2192 {tg['TargetGroupName']}:{port} ({state})"
                    )
                lb_rows.append({
                    "Load Balancer": lb_name_str,
                    "Target Group": tg["TargetGroupName"],
                    "Target": target_id,
                    "Port": port,
                    "Health": state,
                })

    lb_table = pd.DataFrame(lb_rows)
    return instance_to_lbs, lb_table, [e1, e2]


@st.cache_data(ttl=120, show_spinner=False)
def fetch_s3(_session):
    s3 = _session.client("s3")
    buckets, err = safe("list_buckets", lambda: s3.list_buckets()["Buckets"])
    rows = []
    if buckets:
        for b in buckets:
            loc, _ = safe("location", lambda b=b: s3.get_bucket_location(Bucket=b["Name"]).get("LocationConstraint") or "us-east-1")
            rows.append({"Bucket": b["Name"], "Region": loc, "Created": b["CreationDate"]})
    return pd.DataFrame(rows), err


@st.cache_data(ttl=60, show_spinner=False)
def fetch_rds(_session, region):
    rds = _session.client("rds", region_name=region)
    dbs, err = safe("describe_db_instances", lambda: rds.describe_db_instances()["DBInstances"])
    rows = []
    if dbs:
        for db in dbs:
            rows.append({
                "Identifier": db["DBInstanceIdentifier"],
                "Engine": db["Engine"],
                "Status": db["DBInstanceStatus"],
                "Class": db["DBInstanceClass"],
                "Multi-AZ": db.get("MultiAZ", False),
            })
    return pd.DataFrame(rows), err


@st.cache_data(ttl=60, show_spinner=False)
def fetch_lambda(_session, region):
    lam = _session.client("lambda", region_name=region)

    def list_fns():
        paginator = lam.get_paginator("list_functions")
        fns = []
        for page in paginator.paginate():
            fns.extend(page["Functions"])
        return fns

    fns, err = safe("list_functions", list_fns)
    rows = []
    if fns:
        for f in fns:
            rows.append({
                "Function": f["FunctionName"],
                "Runtime": f.get("Runtime", "-"),
                "Memory (MB)": f["MemorySize"],
                "Timeout (s)": f["Timeout"],
                "Last Modified": f["LastModified"],
            })
    return pd.DataFrame(rows), err


@st.cache_data(ttl=120, show_spinner=False)
def fetch_vpc(_session, region):
    ec2 = _session.client("ec2", region_name=region)
    vpcs, err = safe("describe_vpcs", lambda: ec2.describe_vpcs()["Vpcs"])
    rows = []
    if vpcs:
        for v in vpcs:
            rows.append({
                "VPC ID": v["VpcId"],
                "CIDR": v["CidrBlock"],
                "State": v["State"],
                "Default": v.get("IsDefault", False),
            })
    return pd.DataFrame(rows), err


@st.cache_data(ttl=60, show_spinner=False)
def fetch_alarms(_session, region):
    cw = _session.client("cloudwatch", region_name=region)

    def list_alarms():
        paginator = cw.get_paginator("describe_alarms")
        alarms = []
        for page in paginator.paginate():
            alarms.extend(page["MetricAlarms"])
        return alarms

    alarms, err = safe("describe_alarms", list_alarms)
    rows = []
    if alarms:
        for a in alarms:
            rows.append({
                "Alarm": a["AlarmName"],
                "State": a["StateValue"],
                "Metric": a.get("MetricName", ""),
                "Description": a.get("AlarmDescription", ""),
            })
    return pd.DataFrame(rows), err


@st.cache_data(ttl=300, show_spinner=False)
def fetch_health(_session):
    health = _session.client("health", region_name="us-east-1")
    events, err = safe("describe_events", lambda: health.describe_events(
        filter={"eventStatusCodes": ["open", "upcoming"]})["events"])
    rows = []
    if events:
        for e in events:
            rows.append({
                "Service": e["service"],
                "Region": e.get("region", "-"),
                "Category": e["eventTypeCategory"],
                "Status": e["statusCode"],
            })
    return pd.DataFrame(rows), err


@st.cache_data(ttl=600, show_spinner=False)
def fetch_cost(_session):
    ce = _session.client("ce", region_name="us-east-1")
    today = datetime.utcnow().date()
    start_of_month = today.replace(day=1)
    last_month_end = start_of_month - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    def get_total(start, end):
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": str(start), "End": str(end)},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
        )
        return float(resp["ResultsByTime"][0]["Total"]["UnblendedCost"]["Amount"])

    mtd, e1 = safe("mtd cost", lambda: get_total(start_of_month, today + timedelta(days=1)))
    last_month, e2 = safe("last month cost", lambda: get_total(last_month_start, start_of_month))

    def get_by_service():
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": str(start_of_month), "End": str(today + timedelta(days=1))},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        groups = resp["ResultsByTime"][0]["Groups"]
        rows = [{"Service": g["Keys"][0], "Cost": float(g["Metrics"]["UnblendedCost"]["Amount"])} for g in groups]
        df = pd.DataFrame(rows)
        return df[df["Cost"] > 0.01].sort_values("Cost", ascending=False)

    by_service, e3 = safe("cost by service", get_by_service)

    def get_daily():
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": str(start_of_month), "End": str(today + timedelta(days=1))},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        )
        rows = [{"Date": r["TimePeriod"]["Start"], "Cost": float(r["Total"]["UnblendedCost"]["Amount"])}
                for r in resp["ResultsByTime"]]
        return pd.DataFrame(rows)

    daily, e4 = safe("daily cost", get_daily)

    return mtd, last_month, by_service, daily, [e1, e2, e3, e4]


# ---------- sidebar ----------

st.sidebar.title("☁️ AWS Dashboard")

auth_method = st.sidebar.radio("Authentication", ["AWS CLI profile", "Access key / secret key"])

profile = "default"
access_key = secret_key = session_token = None

if auth_method == "AWS CLI profile":
    profile = st.sidebar.text_input("AWS profile", value="default")
else:
    access_key = st.sidebar.text_input("AWS Access Key ID", type="password")
    secret_key = st.sidebar.text_input("AWS Secret Access Key", type="password")
    session_token = st.sidebar.text_input("Session token (optional)", type="password",
                                           help="Only needed for temporary/STS credentials")

region = st.sidebar.text_input("Region", value="eu-central-1")

if st.sidebar.button("🔄 Refresh", use_container_width=True):
    st.cache_data.clear()
    st.cache_resource.clear()

st.sidebar.caption("Read-only. Keys are kept in memory for this session only — never written to disk.")

if auth_method == "Access key / secret key" and not (access_key and secret_key):
    st.info("Enter your AWS Access Key ID and Secret Access Key in the sidebar to continue.")
    st.stop()

try:
    session = get_session(profile, region, access_key, secret_key, session_token)
except Exception as e:
    st.error(f"Failed to create session: {e}")
    st.stop()

st.title("AWS Environment Overview")
auth_label = profile if auth_method == "AWS CLI profile" else "access key"
st.caption(f"Auth: `{auth_label}` · Region: `{region}` · {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

try:
    ident, iam_summary, e1, e2 = fetch_account(session)
except NoCredentialsError:
    st.error("No AWS credentials found. Configure via `aws configure` or environment variables.")
    st.stop()

# ---------- top metrics row ----------

c1, c2, c3, c4 = st.columns(4)
if ident:
    c1.metric("Account ID", ident["Account"])
if iam_summary:
    c2.metric("IAM Users", iam_summary.get("Users", 0))
    c3.metric("IAM Roles", iam_summary.get("Roles", 0))
    c4.metric("MFA Devices", iam_summary.get("MFADevices", 0))
warn_if_error(e1 or e2)

tabs = st.tabs(["Compute", "Storage", "Database", "Serverless", "Network", "Alarms", "Health", "Cost"])

# Compute
with tabs[0]:
    instances_df, enis_df, volumes, eips, errs = fetch_ec2(session, region)
    lb_map, lb_table, lb_errs = fetch_load_balancers(session, region)

    if instances_df is not None and not instances_df.empty:
        # attach load balancer membership as a column
        instances_df = instances_df.copy()
        instances_df["Load Balancers"] = instances_df["Instance ID"].map(
            lambda iid: "; ".join(lb_map.get(iid, [])) or "-"
        )

        counts = instances_df["State"].value_counts()
        cols = st.columns(len(counts) if len(counts) else 1)
        for col, (state, count) in zip(cols, counts.items()):
            col.metric(state.capitalize(), count)

        st.dataframe(
            instances_df[["Name", "Instance ID", "Type", "State", "Private IP", "Public IP",
                           "AZ", "Security Groups", "Load Balancers"]],
            use_container_width=True, hide_index=True,
        )

        with st.expander("Network interfaces (per ENI)"):
            if enis_df is not None and not enis_df.empty:
                st.dataframe(enis_df, use_container_width=True, hide_index=True)
            else:
                st.caption("No network interfaces found.")

        with st.expander("Load balancer target health (raw)"):
            if lb_table is not None and not lb_table.empty:
                st.dataframe(lb_table, use_container_width=True, hide_index=True)
            else:
                st.caption("No load balancers/target groups found in this region.")
    else:
        st.info("No EC2 instances found.")

    if volumes is not None:
        unattached = [v for v in volumes if not v["Attachments"]]
        st.caption(f"EBS volumes: {len(volumes)} total, {len(unattached)} unattached")
    if eips is not None:
        unassoc = [a for a in eips if "InstanceId" not in a]
        st.caption(f"Elastic IPs: {len(eips)} total, {len(unassoc)} unassociated")
    for e in errs + lb_errs:
        warn_if_error(e)

# Storage
with tabs[1]:
    s3_df, err = fetch_s3(session)
    if s3_df is not None and not s3_df.empty:
        st.metric("Buckets", len(s3_df))
        st.dataframe(s3_df, use_container_width=True, hide_index=True)
    else:
        st.info("No S3 buckets found.")
    warn_if_error(err)

# Database
with tabs[2]:
    rds_df, err = fetch_rds(session, region)
    if rds_df is not None and not rds_df.empty:
        st.dataframe(rds_df, use_container_width=True, hide_index=True)
    else:
        st.info("No RDS instances found.")
    warn_if_error(err)

# Serverless
with tabs[3]:
    lam_df, err = fetch_lambda(session, region)
    if lam_df is not None and not lam_df.empty:
        st.metric("Functions", len(lam_df))
        st.dataframe(lam_df, use_container_width=True, hide_index=True)
    else:
        st.info("No Lambda functions found.")
    warn_if_error(err)

# Network
with tabs[4]:
    vpc_df, err = fetch_vpc(session, region)
    if vpc_df is not None and not vpc_df.empty:
        st.dataframe(vpc_df, use_container_width=True, hide_index=True)
    else:
        st.info("No VPCs found.")
    warn_if_error(err)

# Alarms
with tabs[5]:
    alarms_df, err = fetch_alarms(session, region)
    if alarms_df is not None and not alarms_df.empty:
        counts = alarms_df["State"].value_counts()
        cols = st.columns(len(counts))
        for col, (state, count) in zip(cols, counts.items()):
            col.metric(state, count)
        alarm_rows = alarms_df[alarms_df["State"] == "ALARM"]
        if not alarm_rows.empty:
            st.warning(f"{len(alarm_rows)} alarm(s) currently firing")
            st.dataframe(alarm_rows, use_container_width=True, hide_index=True)
        with st.expander("All alarms"):
            st.dataframe(alarms_df, use_container_width=True, hide_index=True)
    else:
        st.info("No CloudWatch alarms found.")
    warn_if_error(err)

# Health
with tabs[6]:
    health_df, err = fetch_health(session)
    if health_df is not None and not health_df.empty:
        st.dataframe(health_df, use_container_width=True, hide_index=True)
    elif err:
        st.info("AWS Health API requires Business/Enterprise support plan.")
    else:
        st.success("No open or upcoming health events.")
    warn_if_error(err)

# Cost
with tabs[7]:
    mtd, last_month, by_service, daily, errs = fetch_cost(session)
    c1, c2 = st.columns(2)
    if mtd is not None:
        c1.metric("Month to date", f"${mtd:,.2f}")
    if last_month is not None:
        c2.metric("Last full month", f"${last_month:,.2f}")
    if daily is not None and not daily.empty:
        st.bar_chart(daily.set_index("Date")["Cost"])
    if by_service is not None and not by_service.empty:
        st.subheader("Top services (month to date)")
        st.bar_chart(by_service.set_index("Service")["Cost"].head(10))
        st.dataframe(by_service, use_container_width=True, hide_index=True)
    for e in errs:
        warn_if_error(e)