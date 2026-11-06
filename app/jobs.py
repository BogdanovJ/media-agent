"""Submit a deterministic Job to the in-cluster API using the mounted workload template."""
import os
from pathlib import Path

import requests
import yaml

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TEMPLATE = Path("/opt/media-agent/config/worker-job.yaml")


def submit(root, directory, src, action, candidate):
    namespace = (SERVICE_ACCOUNT / "namespace").read_text().strip()
    url = f"https://kubernetes.default.svc/apis/batch/v1/namespaces/{namespace}/jobs"
    options = dict(headers={"Authorization": "Bearer " + (SERVICE_ACCOUNT / "token").read_text().strip()},
                   verify=str(SERVICE_ACCOUNT / "ca.crt"), timeout=15, allow_redirects=False)
    response = requests.get(url, params={"labelSelector": "app=media-agent-worker"}, **options)
    response.raise_for_status()
    jobs = response.json()["items"]
    name = "media-" + directory.parent.name[:12] + "-" + directory.name[:32]
    if any(job["metadata"]["name"] == name for job in jobs) or len(jobs) >= 2:
        return
    # Admission also enforces a hard two-Job quota across controller restarts.
    job = yaml.safe_load(TEMPLATE.read_text())
    job["metadata"]["name"] = name
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    container["image"] = os.environ["WORKER_IMAGE"]
    container["args"] = [str(src), action] + ([str(candidate)] if candidate else [])
    writable, = [m for m in container["volumeMounts"] if m["mountPath"] == "/work"]
    writable["subPath"] = str(directory.relative_to(root))
    response = requests.post(url, json=job, **options)
    if response.status_code != 409:
        response.raise_for_status()
