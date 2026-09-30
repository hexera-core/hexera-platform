# Responsibility: Let an idle worker VM take itself out of its managed instance group, so a VM running a job is never the one removed.
# Owns: the idle clock, the drain-then-remove sequence, and the rules for when a worker may remove or recycle its own VM.
# Boundaries: it acts only on the VM it runs on, and only while that VM's worker holds no job; a job on a VM that goes away
#             anyway is still handed back by application/worker_handoff.py.
"""Idle self-retire.

WHY THE AUTOSCALER NO LONGER REMOVES WORKERS. A Compute Engine autoscaler that scales IN picks the
VM to delete itself, and it cannot be told which VMs are idle. Shared dev, 2026-09-30: one rehearsal
job (bc5ddb08) was moved three times in 30 minutes. At 08:29:52 UTC the autoscaler went from 3 to 2
("worker_demand was equal to 2") and deleted the VM running the job's solver check, not the idle
one. At 09:00:13, 21 seconds after the 30-minute scale-in window from the first removal ran out, it went
from 2 to 1 and deleted the VM that had picked the job up two minutes earlier. Sizing on queued +
running work (worker_demand) got the NUMBER right both times. What went wrong was the CHOICE of
VM, and only the VM itself knows whether it is busy.

So the deploy sets the autoscaler to scale OUT only (create-worker-fleet.sh), and removal is done
here, by the one party that knows. The autoscaler still decides HOW MANY - it computes its
recommended size in every mode - and the idle worker decides WHICH: a worker that has held no job
for WORKER_IDLE_RETIRE_MINUTES, in a group larger than the autoscaler recommends and above its
floor, deletes its own VM from the group (instanceGroupManagers.deleteInstances, which also lowers
the group's target size so nothing replaces it). Leaving while the recommendation is still higher
would only make the autoscaler add a VM back.

THE SAME MECHANISM MAKES A FLEET DEPLOY GENTLE. A proactive roll onto a new template replaces VMs
whatever they are running, which was the second of that job's three moves (08:57:57, run
36690862673). The deploy now rolls OPPORTUNISTICALLY once the fleet can retire itself: new VMs start
on the new template, and a worker left on an old one recreates its own VM
(instanceGroupManagers.recreateInstances) once it is idle. A worker that is busy finishes its job
on the code it started with, then moves.

THE ORDER IS THE SAFETY PROPERTY - a clean drain first:
  1. idle long enough, and the group's own state allows it - larger than the autoscaler wants,
     above its floor (read fresh from the Compute API);
  2. take the fleet-wide retire lock in Redis, so two idle workers cannot both remove themselves
     from a group one above its floor;
  3. stop taking work (cancel the consumer on the sizing queue) and confirm the consumer stopped;
  4. wait a moment, then check again that this worker holds nothing - a job that arrived between
     the idle check and the cancel is kept and run here, and the retire is called off - and that
     the geometry-check worker on the same VM is running nothing either;
  5. read the group again under the lock, and only then remove or recycle this VM.
Any failure after step 3 resumes taking work. A VM whose removal was accepted but which is still
running ten minutes later also resumes, so a worker can never sit alive and deaf.

WHEN IT DOES NOTHING. Off when WORKER_IDLE_RETIRE_MINUTES is 0; off on anything that is not a VM
created by a zonal managed instance group (a laptop, docker compose, Cloud Run); off for a worker
that does not consume the queue the fleet is sized on (the geometry-check worker beside the
simulation worker). Removal also waits for the group itself to say so: an autoscaler that still
scales in (mode ON) keeps that job, and a group with no autoscaler has a size an operator fixed.

WHAT IT NEEDS. compute.instanceGroupManagers.get and .update and compute.autoscalers.get for the
VM's own identity - the one grant create-worker-fleet.sh applies, or prints for an owner to apply.
Without it every attempt is refused with 403 and said loudly, with the command, and the worker
keeps working: the cost of a missing grant is idle VMs, never a lost job.
"""
from __future__ import annotations

import enum
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: The queue the fleet is SIZED on (celery_app.py task_routes; create-queue-depth-publisher.sh).
#: Only a worker consuming it may retire the VM - the geometry-check worker shares the VM and must not.
SIZING_QUEUE = "simulation_jobs"

#: How often the idle clock is looked at. Cheap: a length of an in-process set.
POLL_SECONDS = 20.0
#: How long after a stay decision the group is read again, so a worker idling at the floor reads
#: the Compute API once a minute rather than on every poll.
GROUP_RECHECK_SECONDS = 60.0
#: A worker left on an outdated template recycles itself after this much idleness - short, because
#: a deploy should take effect soon, and non-zero, because a worker that just finished a job is
#: often about to be handed the next one.
RECYCLE_IDLE_SECONDS = 120.0
#: How long the consumer gets to confirm it has stopped taking work.
DRAIN_CONFIRM_SECONDS = 10.0
#: How long, after the consumer stopped, a delivery already on its way is given to land.
DRAIN_SETTLE_SECONDS = 3.0
#: The fleet-wide lock is HELD past a successful removal, until it expires: the next worker then
#: reads a group whose target size already counts this one's removal.
RETIRE_LOCK_SECONDS = 180
#: A recycled VM needs a boot, a docker install and an image pull before it takes work again, so
#: the next recycle waits for that, keeping at most one worker out of service at a time.
RECYCLE_LOCK_SECONDS = 420
#: After the Compute API refused (a missing grant), how long before trying again.
REFUSED_BACKOFF_SECONDS = 600.0
#: After any other failure, how long before trying again.
ERROR_BACKOFF_SECONDS = 120.0
#: A VM whose removal was accepted and which is still running after this long takes work again.
STILL_HERE_SECONDS = 600.0

#: What the VM's identity needs, in the order the grant command lists them.
REQUIRED_PERMISSIONS = ("compute.instanceGroupManagers.get", "compute.instanceGroupManagers.update",
                        "compute.autoscalers.get")
#: The custom role create-worker-fleet.sh creates and binds. Named here so the error a worker logs
#: names the same role the deploy does.
SELF_RETIRE_ROLE = "hexeraWorkerSelfRetire"


class Action(str, enum.Enum):
    STAY = "stay"
    RETIRE = "retire"      # delete this VM from the group; the group's target size drops by one
    RECYCLE = "recycle"    # recreate this VM on the group's current template; the size is unchanged


@dataclass(frozen=True)
class GroupState:
    """What the group's own settings say, read fresh from the Compute API."""

    target_size: int
    #: The autoscaler's floor. None when the group has no autoscaler: its size was fixed by hand.
    min_size: int | None
    #: How many VMs the autoscaler wants now. It computes this in every mode, scale-out-only
    #: included, and holds a peak for its stabilization period (about ten minutes) before letting
    #: it fall. A worker leaves only while the group is ABOVE it: removing a VM the autoscaler still
    #: wants would only make it add one back. None until the autoscaler has computed one.
    recommended_size: int | None
    #: The autoscaler scales OUT only, so removing idle VMs is the workers' job. False while it
    #: still scales in itself (mode ON) or is off - then a worker removing itself would fight it.
    workers_scale_in: bool
    #: The group's update policy is OPPORTUNISTIC: a VM on an old template is replaced only when
    #: something recreates it. Under a PROACTIVE roll the group replaces it by itself.
    opportunistic: bool
    #: The template the group puts new VMs on, by name. None while it runs two versions (a canary):
    #: then "outdated" has no single meaning and nothing is recycled.
    target_template: str | None


def decide(*, holding: int, idle_seconds: float, idle_limit_seconds: float, group: GroupState,
           own_template: str | None) -> tuple[Action, str]:
    """Remove, recycle, or stay - and why, in words that go straight into the log.

    The autoscaler decides HOW MANY (its recommended size, never below its floor); the idle worker
    decides WHICH - itself, because it is the one VM known to be running nothing."""
    if holding > 0:
        return Action.STAY, f"holding {holding} job(s)"
    wanted = None
    if group.min_size is not None and group.recommended_size is not None:
        wanted = max(group.min_size, group.recommended_size)
    if (group.workers_scale_in and wanted is not None and group.target_size > wanted
            and idle_seconds >= idle_limit_seconds):
        return Action.RETIRE, (f"idle {idle_seconds / 60:.0f} min, and the autoscaler wants {wanted} "
                               f"of the group's {group.target_size} VMs (floor {group.min_size})")
    outdated = bool(group.target_template and own_template
                    and own_template != group.target_template)
    if group.opportunistic and outdated and idle_seconds >= RECYCLE_IDLE_SECONDS:
        return Action.RECYCLE, (f"idle and on template {own_template}, while the group now starts "
                                f"VMs on {group.target_template}")
    if group.min_size is None:
        return Action.STAY, "the group has no autoscaler - its size is fixed by hand"
    if not group.workers_scale_in:
        return Action.STAY, "the autoscaler still scales in by itself"
    if group.target_size <= group.min_size:
        return Action.STAY, f"the group is at its floor ({group.target_size})"
    if wanted is None or group.target_size <= wanted:
        return Action.STAY, (f"the autoscaler still wants all {group.target_size} VMs "
                             f"(recommends {group.recommended_size})")
    return Action.STAY, f"idle {idle_seconds / 60:.1f} min of {idle_limit_seconds / 60:.0f}"


# ---------------------------------------------------------------------------------------------
# the three things the retirer acts through - each a small seam, so the order can be tested
# ---------------------------------------------------------------------------------------------

class WorkerTap(Protocol):
    """The worker's own view of its work. The celery implementation lives in runtime/celery_worker.py,
    the one runtime module allowed to import celery (tests/unit/hygiene/test_vendor_sdk_confinement.py)."""

    def configured(self) -> bool: ...    # does this worker consume the queue the fleet is sized on?
    def holding(self) -> int: ...        # jobs received and not yet finished, the running one included
    def taking(self) -> bool: ...        # is it consuming that queue right now?
    def stop_taking(self) -> None: ...
    def resume_taking(self) -> None: ...
    def neighbour_holding(self) -> int | None: ...  # checks the geometry worker on this VM is running


class Group(Protocol):
    def state(self) -> GroupState: ...
    def remove_self(self) -> None: ...
    def recycle_self(self) -> None: ...


class Lock(Protocol):
    def acquire(self, ttl_seconds: int) -> bool: ...
    def release(self) -> None: ...


class Refused(RuntimeError):
    """The Compute API answered 403: this VM's identity may not manage its own group."""


# ---------------------------------------------------------------------------------------------
# the VM's identity and its group, from the metadata server and the Compute API
# ---------------------------------------------------------------------------------------------

# The metadata server by address, not by name: a container's resolver is docker's, and the address
# needs no DNS at all. Off Google Cloud it does not answer, which is how "not a fleet VM" is known.
_METADATA = "http://169.254.169.254/computeMetadata/v1/"
_COMPUTE = "https://compute.googleapis.com/compute/v1/"
_CREATED_BY = re.compile(r"projects/[^/]+/zones/([^/]+)/instanceGroupManagers/([^/]+)")


def _metadata(path: str, timeout: float = 2.0) -> str:
    req = urllib.request.Request(_METADATA + path, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed link-local host
        return str(resp.read().decode())


def _basename(url: str | None) -> str | None:
    return url.rstrip("/").rsplit("/", 1)[-1] if url else None


@dataclass(frozen=True)
class Identity:
    project: str
    zone: str
    instance: str
    group: str
    template: str | None
    service_account: str

    @classmethod
    def from_metadata(cls, fetch: Callable[[str], str] = _metadata) -> Identity | None:
        """This VM, if it is one a zonal managed instance group created; None otherwise."""
        try:
            created_by = fetch("instance/attributes/created-by").strip()
        except urllib.error.HTTPError:
            return None       # a Compute VM, but not one a group created
        except (OSError, ValueError):
            return None       # not on Compute Engine at all (URLError is an OSError)
        match = _CREATED_BY.fullmatch(created_by)
        if not match:
            logger.info("idle self-retire: created by %s, which is not a zonal managed instance "
                        "group - off", created_by)
            return None
        try:
            template: str | None = _basename(fetch("instance/attributes/instance-template").strip())
        except (OSError, ValueError):
            template = None
        try:
            account = fetch("instance/service-accounts/default/email").strip()
        except (OSError, ValueError):
            account = ""
        return cls(project=fetch("project/project-id").strip(), zone=match.group(1),
                   instance=fetch("instance/name").strip(), group=match.group(2),
                   template=template, service_account=account)

    def grant_command(self) -> str:
        """The one command an owner runs once. The same text create-worker-fleet.sh prints.

        `;`, not `&&`: when the role already exists (a half-finished earlier grant) its create fails,
        and the binding must still be made. It also runs unchanged in PowerShell."""
        role = f"projects/{self.project}/roles/{SELF_RETIRE_ROLE}"
        return (f"gcloud iam roles create {SELF_RETIRE_ROLE} --project {self.project} "
                f"--title 'Hexera worker self-retire' --permissions {','.join(REQUIRED_PERMISSIONS)} "
                f"--stage GA ; gcloud projects add-iam-policy-binding {self.project} "
                f"--member serviceAccount:{self.service_account or '<worker service account>'} "
                f"--role {role} --condition None")


Opener = Callable[..., Any]


class GceGroup:
    """The Compute API calls, on this VM's own group and on nothing else."""

    def __init__(self, ident: Identity, *, opener: Opener = urllib.request.urlopen,
                 token: Callable[[], str] | None = None) -> None:
        self._ident = ident
        self._open = opener
        self._token = token or self._metadata_token
        self._igm = (f"{_COMPUTE}projects/{ident.project}/zones/{ident.zone}/"
                     f"instanceGroupManagers/{ident.group}")
        self._self_ref = f"zones/{ident.zone}/instances/{ident.instance}"

    @staticmethod
    def _metadata_token() -> str:
        return str(json.loads(_metadata("instance/service-accounts/default/token"))["access_token"])

    def _call(self, method: str, url: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            url, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self._token()}",
                     "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=20) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise Refused(f"{method} {url.rsplit('/', 2)[-2:]} refused (403)") from exc
            raise
        return json.loads(raw) if raw else {}

    def state(self) -> GroupState:
        igm = self._call("GET", self._igm)
        versions = igm.get("versions") or []
        if len(versions) == 1:
            target_template = _basename(versions[0].get("instanceTemplate"))
        elif not versions:
            target_template = _basename(igm.get("instanceTemplate"))
        else:
            target_template = None
        min_size: int | None = None
        recommended: int | None = None
        mode = ""
        autoscaler = (igm.get("status") or {}).get("autoscaler")
        if autoscaler:
            # status.autoscaler is a www.googleapis.com link; the path after /compute/v1/ is the
            # resource, asked of the same endpoint as everything else.
            path = autoscaler.split("/compute/v1/", 1)[-1]
            found = self._call("GET", _COMPUTE + path)
            policy = found.get("autoscalingPolicy") or {}
            min_size = int(policy.get("minNumReplicas", 0))
            mode = str(policy.get("mode", "ON"))
            if found.get("recommendedSize") is not None:
                recommended = int(found["recommendedSize"])
        return GroupState(
            target_size=int(igm.get("targetSize", 0)),
            min_size=min_size,
            recommended_size=recommended,
            workers_scale_in=mode in ("ONLY_SCALE_OUT", "ONLY_UP"),
            opportunistic=(igm.get("updatePolicy") or {}).get("type") == "OPPORTUNISTIC",
            target_template=target_template,
        )

    def remove_self(self) -> None:
        self._call("POST", f"{self._igm}/deleteInstances",
                   {"instances": [self._self_ref], "skipInstancesOnValidationError": False})

    def recycle_self(self) -> None:
        self._call("POST", f"{self._igm}/recreateInstances", {"instances": [self._self_ref]})


class RedisLock:
    """One retire at a time across the fleet, in the deployment's own keyspace."""

    _RELEASE_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('del', KEYS[1]) else return 0 end")

    def __init__(self, key: str, owner: str, client_factory: Callable[[], Any] | None = None) -> None:
        self._key = key
        self._owner = owner
        self._client_factory = client_factory or self._default_client

    @staticmethod
    def _default_client() -> Any:
        from meshpipeline.adapters._shared.redis_client import sync_client
        return sync_client(socket_timeout=5, socket_connect_timeout=5)

    def acquire(self, ttl_seconds: int) -> bool:
        try:
            return bool(self._client_factory().set(self._key, self._owner, nx=True,
                                                   ex=max(1, int(ttl_seconds))))
        except Exception as exc:  # noqa: BLE001 - no lock, no retire: staying is always safe
            logger.warning("idle self-retire: could not take the retire lock (%s) - staying",
                           type(exc).__name__)
            return False

    def release(self) -> None:
        try:
            self._client_factory().eval(self._RELEASE_LUA, 1, self._key, self._owner)
        except Exception as exc:  # noqa: BLE001 - the lock expires by itself
            logger.warning("idle self-retire: could not release the retire lock (%s) - it expires "
                           "by itself", type(exc).__name__)


# ---------------------------------------------------------------------------------------------
# the retirer
# ---------------------------------------------------------------------------------------------

class IdleRetirer:
    def __init__(self, *, tap: WorkerTap, group: Group, lock: Lock, own_template: str | None,
                 idle_limit_seconds: float, grant_hint: str = "",
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._tap = tap
        self._group = group
        self._lock = lock
        self._own_template = own_template
        self._idle_limit = float(idle_limit_seconds)
        self._grant_hint = grant_hint
        self._clock = clock
        self._sleep = sleep
        self._idle_since = clock()
        self._not_before = 0.0

    @property
    def idle_seconds(self) -> float:
        return self._clock() - self._idle_since

    def _backoff(self, seconds: float) -> None:
        self._not_before = self._clock() + seconds

    def _refused(self, exc: Refused) -> None:
        logger.error("idle self-retire: the Compute API refused this VM's identity (%s). Idle "
                     "workers cannot remove themselves, so the group will not shrink and will not "
                     "move onto a new template until an owner runs, once:\n  %s", exc,
                     self._grant_hint or f"(grant {', '.join(REQUIRED_PERMISSIONS)})")
        self._backoff(REFUSED_BACKOFF_SECONDS)

    def announce(self) -> None:
        """One read at start, so a missing grant is said at boot rather than after an idle hour."""
        try:
            group = self._group.state()
        except Refused as exc:
            self._refused(exc)
            return
        except Exception as exc:  # noqa: BLE001 - the loop tries again
            logger.warning("idle self-retire: could not read the group yet (%s)", type(exc).__name__)
            return
        logger.info("idle self-retire on: group size %d, autoscaler wants %s, floor %s, scales out "
                    "only: %s, opportunistic updates: %s - an idle worker leaves after %.0f min",
                    group.target_size, group.recommended_size, group.min_size,
                    "yes" if group.workers_scale_in else "no",
                    "yes" if group.opportunistic else "no", self._idle_limit / 60)

    def tick(self) -> Action:
        """One look. Returns the action TAKEN - STAY unless this VM is now being removed or recycled."""
        now = self._clock()
        holding = self._tap.holding()
        if holding > 0:
            self._idle_since = now
            return Action.STAY
        if now < self._not_before:
            return Action.STAY
        # nothing can be decided before the shorter of the two thresholds - no API call until then
        if now - self._idle_since < min(self._idle_limit, RECYCLE_IDLE_SECONDS):
            return Action.STAY
        try:
            group = self._group.state()
        except Refused as exc:
            self._refused(exc)
            return Action.STAY
        except Exception as exc:  # noqa: BLE001 - staying is always safe
            logger.warning("idle self-retire: could not read the group (%s: %s)",
                           type(exc).__name__, exc)
            self._backoff(ERROR_BACKOFF_SECONDS)
            return Action.STAY
        action, why = decide(holding=0, idle_seconds=now - self._idle_since,
                             idle_limit_seconds=self._idle_limit, group=group,
                             own_template=self._own_template)
        if action is Action.STAY:
            self._backoff(GROUP_RECHECK_SECONDS)
            return Action.STAY
        return self._drain_and_act(action, why)

    def _drain_and_act(self, action: Action, why: str) -> Action:
        ttl = RETIRE_LOCK_SECONDS if action is Action.RETIRE else RECYCLE_LOCK_SECONDS
        if not self._lock.acquire(ttl):
            # another worker is retiring or recycling right now; look again once it has
            self._backoff(GROUP_RECHECK_SECONDS)
            return Action.STAY
        stopped = acted = False
        try:
            self._tap.stop_taking()
            stopped = True
            deadline = self._clock() + DRAIN_CONFIRM_SECONDS
            while self._tap.taking() and self._clock() < deadline:
                self._sleep(0.5)
            if self._tap.taking():
                logger.warning("idle self-retire: the worker did not stop taking work within %.0fs "
                               "- staying", DRAIN_CONFIRM_SECONDS)
                self._backoff(ERROR_BACKOFF_SECONDS)
                return Action.STAY
            self._sleep(DRAIN_SETTLE_SECONDS)
            holding = self._tap.holding()
            if holding > 0:
                logger.info("idle self-retire: a job arrived while draining - keeping it, staying")
                self._idle_since = self._clock()
                return Action.STAY
            # The geometry worker beside this one goes down with the VM, and a check it has
            # already taken would be lost with it. Nobody answering is not a reason to stay: a VM
            # with no geometry worker, or one too broken to answer, has nothing it could finish.
            checks = self._tap.neighbour_holding()
            if checks:
                logger.info("idle self-retire: the geometry worker on this VM is running %d "
                            "check(s) - staying", checks)
                self._backoff(GROUP_RECHECK_SECONDS)
                return Action.STAY
            group = self._group.state()          # fresh, under the lock
            again, why = decide(holding=holding, idle_seconds=self.idle_seconds,
                                idle_limit_seconds=self._idle_limit, group=group,
                                own_template=self._own_template)
            if again is not action:
                logger.info("idle self-retire: the group changed while draining (%s) - staying", why)
                self._backoff(GROUP_RECHECK_SECONDS)
                return Action.STAY
            if action is Action.RETIRE:
                self._group.remove_self()
            else:
                self._group.recycle_self()
            acted = True
            logger.warning("idle self-retire: %s this VM - %s. It takes no more work; the group %s.",
                           "removing" if action is Action.RETIRE else "recycling", why,
                           "shrinks by one" if action is Action.RETIRE
                           else "recreates it on its current template")
            return action
        except Refused as exc:
            self._refused(exc)
            return Action.STAY
        except Exception as exc:  # noqa: BLE001 - resume below; staying is always safe
            logger.warning("idle self-retire: %s failed (%s: %s) - staying", action.value,
                           type(exc).__name__, exc)
            self._backoff(ERROR_BACKOFF_SECONDS)
            return Action.STAY
        finally:
            if stopped and not acted:
                self._tap.resume_taking()
            if not acted:
                self._lock.release()

    def run(self, stop: threading.Event) -> None:
        self.announce()
        while not stop.wait(POLL_SECONDS):
            try:
                taken = self.tick()
            except Exception:  # noqa: BLE001 - the loop must outlive any one look
                logger.exception("idle self-retire: unexpected failure - staying")
                continue
            if taken is Action.STAY:
                continue
            # The group is taking this VM away; the shutdown script stops the container. If that
            # has not happened after STILL_HERE_SECONDS, the removal did not go through: take work
            # again rather than sit alive and deaf.
            if stop.wait(STILL_HERE_SECONDS):
                return
            logger.error("idle self-retire: the %s was accepted but this VM is still running "
                         "after %.0f min - taking work again", taken.value, STILL_HERE_SECONDS / 60)
            self._tap.resume_taking()
            self._idle_since = self._clock()
            self._backoff(ERROR_BACKOFF_SECONDS)


_STOP = threading.Event()
#: The first wait before asking the metadata server again, doubling up to IDENTITY_RETRY_MAX_SECONDS.
IDENTITY_RETRY_SECONDS = 15.0
IDENTITY_RETRY_MAX_SECONDS = 300.0


def _resolve_identity(identity: Callable[[], Identity | None],
                      stop: threading.Event) -> Identity | None:
    """This VM's identity - asked again, for as long as it takes, when the metadata server fails
    part-way. `from_metadata` answers None only when this is definitely not a group's VM; an
    exception is a failed question, and a worker that gave up on it would never retire again."""
    delay = IDENTITY_RETRY_SECONDS
    while True:
        try:
            return identity()
        except Exception as exc:  # noqa: BLE001 - asked again below
            logger.warning("idle self-retire: could not read this VM's identity (%s) - asking again "
                           "in %.0fs", type(exc).__name__, delay)
        if stop.wait(delay):
            return None
        delay = min(delay * 2, IDENTITY_RETRY_MAX_SECONDS)


def start_idle_retire(tap: WorkerTap, *, minutes: int | None = None,
                      identity: Callable[[], Identity | None] = Identity.from_metadata
                      ) -> threading.Thread | None:
    """Called from `worker_ready` in the celery main process. Never raises."""
    try:
        if minutes is None:
            from meshpipeline.settings.runtime import WORKER_IDLE_RETIRE_MINUTES
            minutes = WORKER_IDLE_RETIRE_MINUTES
        limit_minutes: int = int(minutes)
        if limit_minutes <= 0:
            logger.info("idle self-retire: off (WORKER_IDLE_RETIRE_MINUTES=0)")
            return None
        if not tap.configured():
            # the geometry-check worker, or any worker not on the queue the fleet is sized on
            return None

        def _main() -> None:
            ident = _resolve_identity(identity, _STOP)
            if ident is None:
                logger.info("idle self-retire: not a managed-instance-group VM - off")
                return
            from meshpipeline.settings.providers import REDIS_KEY_PREFIX
            retirer = IdleRetirer(
                tap=tap, group=GceGroup(ident),
                lock=RedisLock(f"{REDIS_KEY_PREFIX}fleet:{ident.group}:retiring", ident.instance),
                own_template=ident.template, idle_limit_seconds=float(limit_minutes) * 60,
                grant_hint=ident.grant_command())
            retirer.run(_STOP)

        thread = threading.Thread(target=_main, name="idle-retire", daemon=True)
        thread.start()
        return thread
    except Exception as exc:  # noqa: BLE001 - a worker that cannot retire still works
        logger.warning("idle self-retire: could not start (%s) - off", type(exc).__name__)
        return None


__all__ = ["Action", "GceGroup", "GroupState", "Identity", "IdleRetirer", "RedisLock", "Refused",
           "SIZING_QUEUE", "WorkerTap", "decide", "start_idle_retire"]
