# Troubleshooting

Nine failure scenarios with reproduction steps, expected symptoms, investigation
commands, root cause analysis, recovery and prevention.

Each scenario states whether it was executed in this workspace. Docker and
Kubernetes are not installed on the machine where this project was built, so
container- and cluster-specific steps are documented with the exact commands but
were not run here. Nothing below claims otherwise.

Quick reference:

| Symptom | First command |
| --- | --- |
| Process exits at startup | read the last 20 lines of the log |
| Pod restarts repeatedly | `kubectl -n contentops logs <pod> --previous` |
| Pod never becomes ready | `kubectl -n contentops describe pod <pod>` (events) |
| Callers get connection errors | `kubectl -n contentops get endpointslices` |
| `POST /evaluate` returns 502 | `curl /health`, then check the generator |
| CI red | reproduce locally with `ruff`, `pytest`, `docker build` |
| Rollout stuck | `kubectl rollout status`, then `describe` the new pod |
| Pod `OOMKilled` | `kubectl top pod`, `describe pod` |
| Reports missing | `scripts/list-backups.sh`, `scripts/validate-backup.sh` |

---

## 1. Application container fails to start

**Reproduce (not executed here; no container runtime available):**

```bash
docker build -t contentops-agent:broken .
docker run --rm -e LLM_ENABLED=true contentops-agent:broken
```

Process-level equivalent, which **was** executed:

```bash
$ LLM_ENABLED=true DATABASE_PATH=/tmp/x.db uvicorn app.main:app --port 8099
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
  Value error, LLM_ENABLED=true requires LLM_API_KEY to be set.
  Leave LLM_ENABLED=false to use deterministic heuristics instead.
```

**Symptoms:** the container exits within a second; `docker compose ps` shows
`Exited (1)`. There is no request served, so health checks never pass.

**Investigate:**

```bash
docker compose ps
docker compose logs app --tail=50
docker inspect contentops-agent --format '{{.State.ExitCode}} {{.State.Error}}'
```

The last log line is the cause. Common ones:

| Message | Meaning |
| --- | --- |
| `LLM_ENABLED=true requires LLM_API_KEY` | secret not mounted, or flag left on |
| `url must start with http://` | bad base URL in the environment |
| `unable to open database file` | `DATABASE_PATH` points at a directory the UID cannot write |
| `ModuleNotFoundError: app` | wrong working directory or missing `COPY app ./app` |
| `Address already in use` | something else holds port 8000 |

**Root cause:** fail-fast configuration validation. Settings are parsed and the
database is opened during startup, before the server starts listening.

**Recover:** fix the variable and restart (`docker compose up -d --force-recreate`).

**Prevent:** keep `.env.example` current, validate configuration in CI by importing
the app, and never default to a credential. A missing optional integration must not
abort startup — only required configuration does.

---

## 2. Kubernetes pod enters CrashLoopBackOff

**Reproduce (not executed here):**

```bash
kubectl -n contentops set env deployment/contentops-agent LLM_ENABLED=true
kubectl -n contentops get pods -w        # STATUS: CrashLoopBackOff, RESTARTS grows
```

**Symptoms:** restart count climbs, backoff grows from 10s to 5m, the container
exits before the readiness probe can pass.

**Investigate:**

```bash
kubectl -n contentops get pods
kubectl -n contentops describe pod <pod> | sed -n '/Events/,$p'
kubectl -n contentops logs <pod> --previous
kubectl -n contentops get deployment contentops-agent -o yaml | grep -A20 'envFrom'
```

`--previous` is the important flag: the current container may not have logged
anything yet. A `ValidationError` in the previous log means environment, not code.

**Root cause:** configuration error, an unwritable volume, or a missing Secret that
is *not* referenced as optional. Startup validation is intentional; a service that
starts with a broken database would report healthy while failing every write.

**Recover:**

```bash
kubectl -n contentops set env deployment/contentops-agent LLM_ENABLED=false
kubectl -n contentops rollout status deployment/contentops-agent
```

**Prevent:** keep required config in the ConfigMap, mark optional secrets
`optional: true` (this project does), and use `kubectl diff -k k8s/` before applying.

---

## 3. Readiness probe fails

**Verified in this workspace** at the process level; the Kubernetes part is the same
HTTP check.

**Reproduce:**

```bash
cp data/contentops.db /tmp/readiness/db.sqlite
DATABASE_PATH=/tmp/readiness/db.sqlite uvicorn app.main:app --port 8096 &
chmod 000 /tmp/readiness/db.sqlite
curl -i http://127.0.0.1:8096/ready
```

Observed:

```
ready ok=200
ready broken=503
{"status":"not_ready","checks":[{"name":"sqlite","required":true,
 "status":"unavailable","detail":"unable to open database file"}, ...]}
health broken=200
ready recovered=200
```

**Symptoms:** `READY 0/1`, pod is `Running` but removed from the Service endpoints
so callers get 503 from the Service or connection refused on direct pod access.

**Investigate:**

```bash
kubectl -n contentops describe pod <pod> | grep -A5 Readiness
kubectl -n contentops get endpointslices -l kubernetes.io/service-name=contentops-agent
kubectl -n contentops exec <pod> -- python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/ready').read().decode())"
```

**Root cause:** a required dependency is unavailable, almost always the database
volume: wrong mount path, permissions (`fsGroup` not honoured), or a full disk. The
body names the failing dependency, and `/health` stays 200 on purpose so the pod is
not restarted for a problem a restart cannot fix.

**Recover:** restore the volume permissions or mount, then the pod recovers without
a restart because readiness is evaluated continuously.

**Prevent:** pin `fsGroup: 10001` (already set), give the PVC real capacity, and keep
liveness independent of the database.

---

## 4. The Service cannot reach the application

**Verified locally:** with the app stopped, `curl` and `scripts/check-health.sh`
both fail; when it is running, both succeed. The cluster commands below were not run.

**Reproduce in a cluster:**

```bash
kubectl -n contentops run client --rm -it --image=curlimages/curl --restart=Never -- \
  curl -sS -m 3 http://contentops-agent.contentops.svc.cluster.local/health
```

**Symptoms:** `Connection refused`, `no such host`, or a 503 from the Service while
the pods themselves are `Running`.

**Investigate, in this order:**

```bash
kubectl -n contentops get pods -o wide           # Running and Ready?
kubectl -n contentops get svc contentops-agent   # selector and port
kubectl -n contentops get endpointslices -l kubernetes.io/service-name=contentops-agent
kubectl -n contentops get pods --show-labels
```

1. No endpoints → the selector does not match the pod labels, or no pod is ready.
2. Endpoints present but refused → the Service `targetPort` does not match the
   container port name (`http` → 8000).
3. `no such host` → wrong namespace in the DNS name; use
   `<service>.<namespace>.svc.cluster.local`.

**Root cause:** label/selector drift, a port mismatch, or an unready pod. The
Service is a label selector, not a pointer to a pod.

**Recover:** correct the manifest (`kubectl apply -k k8s/`) and confirm endpoints
appear.

**Prevent:** deploy Services and Deployments together through kustomize (as here), and
run `scripts/k8s-status.sh` after every rollout.

---

## 5. External Variant Generator API becomes unavailable

**Verified in this workspace.**

**Reproduce:**

```bash
VARIANT_GENERATOR_MODE=http \
VARIANT_GENERATOR_BASE_URL=http://127.0.0.1:59999 \
VARIANT_GENERATOR_MAX_RETRIES=2 \
uvicorn app.main:app --port 8098

curl -sS -X POST http://127.0.0.1:8098/api/v1/evaluate \
  -H 'content-type: application/json' \
  -d '{"source":{"title":"t","content":"generated content needs checks"}}'
```

Observed: HTTP 502 in about 0.6s (three attempts) with

```json
{"error":{"code":"upstream_unavailable","message":"The Variant Generator could not return variants.",
 "request_id":"98da8cb1...","details":{"service":"variant_generator",
 "operation":"generate_variants","kind":"connection","status_code":null,
 "attempts":3,"message":"connection failed: All connection attempts failed"}}}
```

`GET /health` kept returning 200 and the service kept serving other requests.

**Symptoms:** only requests that need generation fail; `/health` and `/ready` stay
green. Integration metrics show `outcome="failure"` and rising latency.

**Investigate:**

```bash
curl -m 3 http://<generator-host>:8000/docs            # is it up at all?
docker compose logs app | grep integration
curl -s http://localhost:8080/metrics | grep integration_requests_total
grep -i "integration failed" /var/log/...              # service, attempt, detail
```

**Root cause:** DNS, wrong base URL, the upstream crashing, or the LLM provider
failing upstream (the generator returns 500 in that case).

**Recover:** restore the upstream. Retries mean short blips self-heal. If the outage
is long, callers can still submit pre-generated variants inline, so evaluation
continues without the generator.

**Prevent:** keep `/ready` independent of external services (done), set timeouts
shorter than caller deadlines, alert on `contentops_integration_requests_total`
failures, and keep the mock mode available for local work.

---

## 6. CI pipeline fails

**Not executed here** (no GitHub runner or container runtime). Reproduce the jobs
locally; each failure below is a command you can run.

**Reproduce the pipeline locally:**

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest --cov=app --cov-report=term-missing --cov-fail-under=75
python - <<'PY'
import pathlib, yaml
for p in pathlib.Path("k8s").glob("*.yaml"):
    list(yaml.safe_load_all(p.read_text()))
PY
docker build -t contentops-agent:local .
```

**Symptoms and causes:**

| Failing job | Likely cause |
| --- | --- |
| `lint` | unformatted file (`ruff format .` fixes it) or a real lint error |
| `test` | failing test, or coverage below the 75% gate |
| `manifests` | YAML syntax, a missing required kind, kubeconform schema mismatch |
| `docker` | image build error, or the smoke test container never answers `/health` |
| `publish` | `packages: write` missing, or the GHCR package does not allow this repo |

**Investigate:** the failing step's log is the source of truth; the job names map to
the list above. For the container smoke test, CI prints `docker logs` on failure.

**Root cause:** most commonly a change that passes on a developer machine but breaks
formatting or the container build. Tests use temporary databases, so tests that pass
locally should pass in CI unless they depend on the local clock, network or an
already-running service.

**Recover:** fix the cause, not the gate. Do not raise `--cov-fail-under` or skip
tests to get green.

**Prevent:** run the four local commands before pushing; keep one Python version
(3.12) in CI and locally.

---

## 7. Deployment update fails

**Not executed here** (no cluster).

**Reproduce:**

```bash
kubectl -n contentops set image deployment/contentops-agent api=contentops-agent:does-not-exist
kubectl -n contentops rollout status deployment/contentops-agent --timeout=60s
```

**Symptoms:** `Waiting for deployment ... rollout to finish`, new pods in
`ImagePullBackOff` or `ErrImagePull`, old pods keep serving traffic (that is the
point of `maxUnavailable: 0`).

**Investigate:**

```bash
kubectl -n contentops get pods
kubectl -n contentops describe pod <new-pod> | sed -n '/Events/,$p'
kubectl -n contentops rollout history deployment/contentops-agent
kubectl -n contentops get rs -o wide
```

`ErrImagePull` means the reference or registry credentials are wrong; `ImagePullBackOff`
means Kubernetes keeps retrying. A pod that pulls but never becomes ready means the
image starts but `/ready` fails — go to scenario 3 and read the pod logs.

**Root cause:** a bad tag, an unloadable image in kind (`kind load docker-image` not
run), or a failing readiness probe in the new version.

**Recover:**

```bash
kubectl -n contentops rollout undo deployment/contentops-agent
kubectl -n contentops rollout status deployment/contentops-agent
```

**Prevent:** deploy immutable tags, run `scripts/k8s-deploy.sh` (it loads the image
into kind before applying), keep `revisionHistoryLimit: 3` for rollback room, and
watch the rollout rather than assuming it succeeded.

---

## 8. Application exceeds resource limits

**Not executed here** (no container runtime). Baseline measured locally: the service
idles at ~59 MiB RSS, which is why the request is 128Mi and the limit 256Mi.

**Reproduce in a cluster:**

```bash
kubectl -n contentops set resources deployment/contentops-agent \
  --limits=memory=32Mi --requests=memory=32Mi
kubectl -n contentops get pods -w         # RESTARTS climbs, then CrashLoopBackOff
kubectl -n contentops describe pod <pod> | grep -A3 "Last State"
```

**Symptoms:** `OOMKilled` with exit code 137, restart loop, 503s while replicas
restart. CPU limits show up instead as high latency and throttling
(`container_cpu_cfs_throttled_periods_total`).

**Investigate:**

```bash
kubectl -n contentops top pod
kubectl -n contentops describe pod <pod> | grep -E "Limits|Requests|Last State|Reason"
kubectl -n contentops logs <pod> --previous | tail -20
kubectl -n contentops get pod <pod> -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}'
```

**Root cause:** the limit is below the working set. Since Python holds the
interpreter plus loaded modules, a 32Mi limit cannot work. Memory also grows with
concurrent requests because each report holds its variant texts.

**Recover:** restore realistic values and let the rollout replace the pods:

```bash
kubectl -n contentops set resources deployment/contentops-agent \
  --limits=cpu=500m,memory=256Mi --requests=cpu=50m,memory=128Mi
```

**Prevent:** measure first (the ~59 MiB baseline above), keep requests well under
limits, and watch `container_memory_working_set_bytes` against the limit.

---

## 9. Application data needs restoration

**Verified end to end in this workspace**, including a bug found and fixed.

**Reproduce:**

```bash
scripts/backup.sh
python - <<'PY'
import sqlite3
conn = sqlite3.connect("data/contentops.db")
conn.execute("DELETE FROM reports"); conn.commit()
PY
scripts/restore-backup.sh backups/contentops-<stamp>.db --yes
scripts/validate-backup.sh data/contentops.db
```

Observed: `restored .../data/contentops.db (2 reports)`; after restarting the service,
`GET /api/v1/reports` returned 2 reports.

**Symptoms:** `/api/v1/reports` returns fewer reports than expected, the database
file is missing or zero-length, or SQLite reports `database disk image is malformed`.

**Investigate:**

```bash
scripts/list-backups.sh
scripts/validate-backup.sh backups/<stamp>.db
python -c "import sqlite3;print(sqlite3.connect('data/contentops.db').execute('PRAGMA integrity_check').fetchone())"
```

**Root cause in this project's history:** the first restore copied a backup into an
existing database that still had a `-wal` file containing the deleted rows; deleting
that log afterwards discarded the restored pages. `restore-backup.sh` now removes the
target file and its `-wal`/`-shm` companions before copying, then validates.

**Recover:** stop the service (or scale to 0), restore, validate, start again, and
confirm `GET /api/v1/reports`.

**Prevent:** restore with the service stopped, validate every backup after writing
it (`backup.sh` does), copy backups off the machine, and rehearse the drill — this
scenario is only reliable because it was rehearsed.
