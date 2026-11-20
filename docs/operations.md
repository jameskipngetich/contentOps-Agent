# Operations

Commands below assume the repository root and a Linux host. `scripts/*.sh` all
start with `set -Eeuo pipefail`, validate their arguments and exit non-zero on
failure.

## Local service

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8080
scripts/check-health.sh            # /health, /ready
scripts/check-health.sh --metrics  # also asserts the metrics endpoint works
```

`APP_URL` overrides the default `http://127.0.0.1:8080`. `127.0.0.1` is used
instead of `localhost` because `localhost` may resolve to IPv6 first.

## Docker

```bash
docker compose up -d --build
docker compose ps
scripts/view-logs.sh
docker compose --profile monitoring up -d    # Prometheus on :9090
```

The app container runs as UID 10001, has a read-only root filesystem with `/tmp`
as tmpfs, and reports its own health through a standard-library HTTP check. Logs are
capped at three 10 MB files by the Compose logging options.

## Kubernetes walkthrough

Pick one local cluster. kind is lighter on memory than Minikube with a full
addon set, so it is the default in the scripts.

```bash
kind create cluster --name contentops
scripts/k8s-deploy.sh
kubectl -n contentops port-forward svc/contentops-agent 8080:80
```

For Minikube, build inside its Docker daemon instead of loading an image:

```bash
minikube start --memory=2048
eval $(minikube docker-env)
scripts/k8s-deploy.sh
```

### Inspect

```bash
kubectl -n contentops get pods -o wide
kubectl -n contentops describe pod <pod>
kubectl -n contentops logs deploy/contentops-agent --tail=50
kubectl -n contentops get events --sort-by=.lastTimestamp | tail
scripts/k8s-status.sh          # nodes, pods, services, endpoints, PVC, events, top
```

### Liveness vs readiness

`/health` answers "is this process alive" and never calls the database or another
service, so an upstream outage cannot cause a restart loop. `/ready` answers "can
this pod serve requests" and requires SQLite; when it cannot, it answers **503** with
`status: not_ready` and the failing dependency in the body. The status code matters:
Kubernetes probes act on the code, not the body, so a 200 would keep an unusable pod
in the Service. External services are listed in the readiness body but do not make
the pod unready; a missing Variant Generator should degrade one endpoint, not remove
every pod from the Service.

### Replica scaling

```bash
scripts/k8s-scale.sh 3
kubectl -n contentops get pods -w
scripts/k8s-scale.sh 2
```

The script refuses to scale above 10 and asks for confirmation before scaling to 0.

### Rolling update and rollback

```bash
kubectl -n contentops set image deployment/contentops-agent api=contentops-agent:0.2.0
kubectl -n contentops rollout status deployment/contentops-agent --timeout=120s
kubectl -n contentops rollout history deployment/contentops-agent
kubectl -n contentops rollout undo deployment/contentops-agent
```

`maxUnavailable: 0` with `maxSurge: 1` means a new pod must pass readiness before an
old one is removed. To exercise rollback, push a broken image tag; the rollout will
stall, and `rollout undo` returns to the previous ReplicaSet.

`revisionHistoryLimit: 3` keeps the last three ReplicaSets for rollback.

### Persistent data

The Deployment mounts a `ReadWriteOnce` PVC at `/data`. `fsGroup: 10001` makes the
volume writable for the non-root user; if a StorageClass ignores `fsGroup` (some
hostPath provisioners do), the pod fails with a permission error on `/data` and the
pod logs show `sqlite3.OperationalError: unable to open database file`. Use a
StorageClass with `fsGroup` support, or set `fsGroupChangePolicy: Always`.

## Monitoring

With Compose:

```bash
docker compose --profile monitoring up -d
curl -s http://localhost:8080/metrics | grep ^contentops_
open http://localhost:9090            # Prometheus UI
```

Useful queries:

```promql
sum by (status) (rate(contentops_evaluations_total[5m]))
histogram_quantile(0.95, sum by (le) (rate(contentops_http_request_duration_seconds_bucket[5m])))
sum by (path, status) (rate(contentops_http_requests_total{status=~"5.."}[5m]))
sum by (platform) (increase(contentops_failed_checks_total[1h]))
contentops_reports_stored
```

In Kubernetes the pod template carries `prometheus.io/scrape`, `prometheus.io/port`
and `prometheus.io/path` annotations for annotation-based discovery. A cluster
Prometheus with a service-account scrape config can read the same endpoints:

```yaml
- job_name: contentops-agent
  kubernetes_sd_configs: [{role: pod}]
  relabel_configs:
    - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
      action: keep
      regex: "true"
```

Grafana is not included: the four queries above cover health, latency, error rate and
evaluation outcomes, and an extra container costs memory this project does not need.

## Backup and restore

What is backed up: the SQLite database (`DATABASE_PATH`, `/data/contentops.db` in
containers) containing evaluation reports and the stored source content.

What is not: logs, Prometheus data, `/tmp`, and any configuration stored in the
environment or ConfigMap (it is in version control).

### Drill performed in this workspace

```bash
$ scripts/backup.sh
2026-11-20T02:23:26 backing up /home/daisy/ContentOps_agent/data/contentops.db
2026-11-20T02:23:26 created backups/contentops-20261120T072326Z.db (32K, 2 reports, verified)

$ scripts/list-backups.sh
FILE                                                           SIZE  MODIFIED
contentops-20261120T072326Z.db                                  32K  2026-11-20 02:23:26

$ python - <<'PY'      # simulate losing the data
import sqlite3
conn = sqlite3.connect("data/contentops.db")
conn.execute("DELETE FROM reports")
conn.commit()
PY

$ scripts/restore-backup.sh backups/contentops-20261120T072326Z.db --yes
... current database saved to .../pre-restore-20261120T072431Z.db
... restored /home/daisy/ContentOps_agent/data/contentops.db (2 reports)

$ curl -s http://127.0.0.1:8080/api/v1/reports | python -c 'import json,sys; print(json.load(sys.stdin)["count"])'
2
```

The first attempt at this drill restored a database with **zero** reports. The
restore used SQLite's backup API into the existing file, which still had a
write-ahead log containing the deleted rows; removing that log afterwards discarded
the copied pages. `restore-backup.sh` now deletes the target file and its `-wal` and
`-shm` companions before copying, and validates the result. That is why the drill is
in the scripts and not just in the documentation.

### Restoring in Kubernetes

```bash
kubectl -n contentops scale deployment/contentops-agent --replicas=0
POD=$(kubectl -n contentops get pod -l app.kubernetes.io/name=contentops-agent \
  -o jsonpath='{.items[0].metadata.name}')   # capture the name before scaling to 0
kubectl -n contentops cp backups/contentops-<stamp>.db "$POD":/data/contentops.db
kubectl -n contentops scale deployment/contentops-agent --replicas=2
```

Capture the pod name first, or run a short-lived helper pod that mounts the PVC. The
database must not be open by another replica while the file is replaced; scaling to
zero is the simple way to guarantee that.

### Limitations

- Backups are local files. Copying them off the machine is manual.
- No retention policy; prune `backups/` yourself.
- Restoring rolls back every report written after the backup, including the source
  content needed to regenerate them.
- With two replicas sharing one SQLite file, a restore requires stopping both.

## Script reference

| Script | Purpose |
| --- | --- |
| `scripts/start-local.sh` | Build and start Compose, wait for health |
| `scripts/check-health.sh` | Check `/health`, `/ready`, optionally `/metrics` |
| `scripts/view-logs.sh` | Follow Compose or Kubernetes logs |
| `scripts/k8s-deploy.sh` | Build, load into kind, apply, wait for rollout |
| `scripts/k8s-status.sh` | Nodes, pods, deployments, service endpoints, events, usage |
| `scripts/k8s-scale.sh` | Scale replicas with validation and confirmation |
| `scripts/backup.sh` | Create and verify a backup |
| `scripts/list-backups.sh` | List backups with size and time |
| `scripts/validate-backup.sh` | Integrity-check a backup file |
| `scripts/restore-backup.sh` | Guarded restore with pre-restore copy |
