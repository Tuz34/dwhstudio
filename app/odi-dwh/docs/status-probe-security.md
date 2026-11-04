# Status probe limits

`GET /api/status` remains public and retains its successful JSON shape. Each
process shares one probe cache and one pool of at most eleven probe workers.
Successful results expire thirty seconds after batch dispatch, including queue
and execution time. Delayed collection never extends that lifetime; completed
checks also become false when this deadline passes while a peer remains hung.
At most sixty requests per
rolling minute enter the status handler's probe path; excess requests receive a
fixed HTTP 429 response and Retry-After. Query strings and client-provided headers
do not create separate budgets or cache entries.

A refresh is single-flight across threads. Requests wait at most two seconds for
that batch, then report unfinished checks as false. An unfinished batch is never
replaced, even after repeated timeouts; this prevents unbounded worker/queue growth.
Individual checker exceptions become false without exposing exception text.
Fixing a permanently stuck underlying checker may require process restart.

Limits are per process, not distributed. A deployment with N workers can perform
N concurrent batches. Use a bounded worker count and an aggregate reverse-proxy
rate limit for a multi-process deployment. This patch does not turn health probes
into a distributed monitoring service. It changes no write endpoint or approval
gate. Cache reset in the shared test fixture is test isolation only; production
has no caller-controlled reset or cache-busting option.
