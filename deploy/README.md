# Deploying to Fargate

`taskdef.json` registers both containers as one task: nginx on port 80 with the
backend on loopback beside it.

## Sizing

**256 CPU / 512 MB** — the smallest combination Fargate accepts. Measured on the
built images, serving the four sample PDFs:

| | idle | 40 concurrent downloads |
| --- | --- | --- |
| backend | 65 MB | 79 MB (73 MB anonymous) |
| nginx | 9 MB | 10 MB |

So ~89 MB against a 512 MB limit, roughly 5× headroom. Downloads are streamed
in 64 KB chunks with `proxy_buffering off`, so memory does not scale with file
size — a 2 GB file costs the same as a 2 KB one. CPU is the likelier limit
before memory: 0.25 vCPU is shared between uvicorn and nginx, so if the service
starts queueing under load, raise `cpu` to 512 (which allows memory to stay at
512, or 1024 if wanted) before touching memory.

This headroom depends on the service staying read-only. An upload path would
change the picture sharply: `s3transfer`'s defaults hold 8 MB parts, ten at a
time, per upload — three concurrent 120 MB uploads measured at 469 MB, which
does not fit here.

## Before the first deploy

Both log groups must exist — the execution role is not granted
`logs:CreateLogGroup`, so `awslogs` will not create them:

```bash
aws logs create-log-group --log-group-name /prod/serdsafely/backend --region us-east-2
aws logs create-log-group --log-group-name /prod/serdsafely/nginx --region us-east-2
aws logs put-retention-policy --log-group-name /prod/serdsafely/backend --retention-in-days 30 --region us-east-2
aws logs put-retention-policy --log-group-name /prod/serdsafely/nginx --retention-in-days 30 --region us-east-2
```

Roles:

- **Execution role** (`serdsafe-ecs-execution`) — pulls the images and writes
  the log streams. The managed `AmazonECSTaskExecutionRolePolicy` covers it.
- **Task role** (`serdsafe-task`) — what the application itself may do. See
  `task-role-policy.json`: read the `/prod` parameters, list the served prefix,
  and read objects under it. No write permission anywhere.

Then:

```bash
aws ecs register-task-definition --cli-input-json file://deploy/taskdef.json --region us-east-2
```

## Load balancer

Point the target group at the **nginx** container, port 80, target type `ip`
(required by `awsvpc`). Health check path `/api/healthz`.

That path is free of logging noise in both directions: nginx drops requests
from `ELB-HealthChecker` out of its access log by user agent, and the backend
logs health checks at DEBUG, which is below the default level and so never
emitted at all.

## Things that will bite

- **`cpuArchitecture` must stay `X86_64`.** The images are built
  `--platform linux/amd64`; on an ARM64 task they fail with `exec format error`.
- **`BACKEND_HOST=127.0.0.1`.** Both containers share one network namespace
  under `awsvpc`, so the backend is on loopback, not on a container name. The
  image defaults to `backend` for docker compose, which will not resolve here.
- **Never set `SERDSAFE_LOCAL_STACK`** in a deployed task. It redirects every
  AWS call — Parameter Store, S3 and CloudWatch — to an emulator that is not
  there.
- **`CLOUDWATCH_LOGS_ENABLED=false` can be overridden by Parameter Store.**
  Parameters are applied after the environment, so a
  `/prod/cloudwatch_logs_enabled` set to `true` would win and the app would
  ship every line a second time, alongside `awslogs`. Either leave that
  parameter absent, or set it to `false`.
- **Turning on `debug` costs log volume.** With the `awslogs` driver everything
  on stdout is billed, and the application's own health-check filter does not
  apply to it. At the default INFO level health checks emit nothing at all, so
  this only matters while debugging.

## Deploying with CloudFormation

`ecs-service.yaml` is the whole service as one stack: the cluster, the task
definition above, both roles, an internet-facing Application Load Balancer
with its security groups, listeners and target group, the service, an
autoscaling target and a CPU alarm.

It also writes the application's own configuration: `/<PlatformEnv>/s3_bucket`
and `/<PlatformEnv>/s3_prefix` in Parameter Store, from the `S3BucketName` and
`S3Prefix` stack parameters. That is load-bearing rather than convenient —
`load_parameters()` treats an empty path as fatal and exits, so a stack that
created the service but not the parameters would deploy, fail every task, and
roll back. CloudFormation owns those two values once created; edit the stack
parameter, not the console.

It is meant to run against an empty account. Three things it cannot create
for itself:

- **The container images.** `BackendImage` and `NginxImage` must already be
  pullable. Build and push them first; see the repository root README.
- **A VPC and two subnets.** Passed as `VpcId` and `SubnetIds`, per the
  interface this service was specified with. A brand-new account's default
  VPC satisfies this — it has an internet gateway and a public subnet in
  every availability zone, which is what the internet-facing load balancer
  needs.
- **A domain.** The HTTPS listener needs a certificate, and a certificate
  needs a name you own. Either pass `CertificateArn`, or pass `DomainName`
  and `HostedZoneId` and the stack will request one and validate it through
  DNS. A `Rules` block rejects the stack up front if neither is supplied,
  rather than failing several minutes in on a malformed certificate ARN.

### From a hosted zone

```bash
aws cloudformation deploy \
  --template-file deploy/ecs-service.yaml \
  --stack-name app-serdsafe-prod01 \
  --region us-east-2 \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      VpcId=vpc-xxxxxxxx \
      SubnetIds=subnet-aaaaaaaa,subnet-bbbbbbbb \
      DomainName=files.example.com \
      HostedZoneId=Z0123456789ABCDEFGHIJ
```

That is the whole thing: certificate, DNS record, load balancer, cluster,
service. `CAPABILITY_NAMED_IAM` is required because both roles are given
explicit names.

With a certificate already issued, swap the last two overrides for
`CertificateArn=arn:aws:acm:...` and point DNS at the `LoadBalancerDnsName`
output yourself.

### Layout

The default puts the tasks and the load balancer in the same subnets with
`AssignPublicIp: ENABLED`, because that is the only arrangement that works in
a default VPC — there is no NAT gateway for a private task to reach ECR
through. The tasks are not exposed by this: their security group accepts
traffic from the load balancer's security group and nothing else.

For a VPC with private subnets, pass the public ones as
`LoadBalancerSubnetIds`, the private ones as `SubnetIds`, and set
`AssignPublicIp=DISABLED`.

The log groups are created by the stack, so the manual `create-log-group`
step above applies only to the plain `register-task-definition` path. Both
carry `DeletionPolicy: Retain` — deleting the stack should not destroy the
logs you would want in order to understand why.

### What was decided, and where to argue with it

- **Health check grace period, 120s.** Has to cover the slowest legitimate
  start: the backend clears its own health check (30s start period, then a
  30s interval), only then does nginx start, and two ALB checks 15s apart
  follow. A cold start lands near 90s. Anything shorter kills healthy tasks
  during a deploy.
- **100% / 200% deployment.** Both old tasks serve until both new ones are
  healthy, so capacity never dips. At two tasks the doubling costs nothing
  worth trading availability for; revisit if the desired count grows.
- **Circuit breaker on, with rollback.** The failures that matter here — a
  bad image, a task role that cannot reach the bucket — fail identically on
  every retry, so retrying only lengthens the outage.
- **`MinCapacity: 0`.** As specified, and it means autoscaling is permitted
  to take the service to zero tasks, which is an outage. The target-tracking
  policy will not do it on its own (it stops at 1), but a scheduled action or
  a manual call can. Set it to 1 or 2 if this service is meant to always be
  serving.
- **Alarm at 80%, autoscaling target at 60%.** The alarm fires when scaling
  is failing to keep up, or has hit `MaxCapacity: 8` — not every time it
  works as intended.
- **HTTP 80 is a 301 to 443.** An ALB cannot forward from one listener to
  another; the client re-requests over TLS, and no plaintext request reaches
  a target.
- **`CreateEcsCluster`.** CloudFormation cannot adopt a cluster it did not
  create, so deploying into an existing one means setting this to `false`.
  Left `true` against an existing cluster, the stack fails with
  "already exists".
- **`s3_prefix` is written as well as `s3_bucket`.** Only the latter was
  asked for, but the task role is scoped to `S3Prefix` while the application
  falls back to its own built-in default when the parameter is absent.
  Overriding `S3Prefix` alone would grant access to one prefix and read
  another, and every download would 403. Writing both from the same stack
  parameter keeps the grant and the behaviour in step.
- **No ALB access logs.** Worth adding, and deliberately absent: the bucket
  policy needs a different principal depending on the region's age — a
  service principal in newer regions, a hardcoded regional ELB account ID in
  older ones, `us-east-2` included. Baking that table in would have made the
  template less portable than leaving it out, which is the opposite of the
  point.
