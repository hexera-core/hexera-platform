# Responsibility: Pin the worker's start-up check of its mesh access (adapters/mesh_execution/access_check): every permission the mesh job and the big-machine VM route call is asked of Google as the worker, and each one missing is named with the command that grants it.
# Boundaries: Google's answers are faked; nothing is called.
from __future__ import annotations

from meshpipeline.adapters.mesh_execution import access_check as A


class _Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Google:
    """Grants what it is told to, per resource; reads the job's account."""

    def __init__(self, job=(), account=(), project=(), fail: str = ""):
        self.grants = {"run.googleapis.com": set(job), "iam.googleapis.com": set(account),
                       "cloudresourcemanager.googleapis.com": set(project)}
        self.fail = fail
        self.asked: list[str] = []

    def post(self, url, json, headers, timeout):
        self.asked.append(url)
        if self.fail and self.fail in url:
            return _Resp({}, 503)
        host = url.split("/")[2]
        return _Resp({"permissions": [p for p in json["permissions"] if p in self.grants[host]]})

    def get(self, url, headers, timeout):
        return _Resp({"template": {"template": {"serviceAccount": "dev-mesh@p.iam.gserviceaccount.com"}}})


def _check(g):
    return A.check(project="p", region="us-central1", job="dev-mesh", me="w@p.iam.gserviceaccount.com",
                   token="t", session=g)


ALL = (A.JOB_RUN + A.JOB_READ, A.ACT_AS, A.VM_CREATE)


def test_a_worker_holding_everything_passes():
    got = _check(_Google(*ALL))
    assert got.ok and len(got.checked) == 3


def test_the_job_it_can_run_but_not_read_is_named_with_its_command():
    """Shared dev on 26 Oct: jobsExecutorWithOverrides only - every big-machine pass refused 403."""
    g = _Google(A.JOB_RUN, A.ACT_AS, A.VM_CREATE)
    got = _check(g)
    assert not got.ok
    assert [m[0] for m in got.missing] == ["run.jobs.get"]
    assert "--role roles/run.viewer" in got.missing[0][2] and "serviceAccount:w@p.iam.gserviceaccount.com" in got.missing[0][2]
    # without the read, the job's account is unknown, so actAs is not asked
    assert not any(u.split("/")[2] == "iam.googleapis.com" for u in g.asked)
    assert all(line.startswith(A.TAG) for line in A.words(got))


def test_act_as_and_the_vm_are_named_too():
    got = _check(_Google(A.JOB_RUN + A.JOB_READ, (), ("compute.instances.delete",)))
    missing = {m[0] for m in got.missing}
    assert "iam.serviceAccounts.actAs" in missing and "compute.instances.create" in missing
    act = next(m for m in got.missing if m[0] == "iam.serviceAccounts.actAs")
    assert "service-accounts add-iam-policy-binding dev-mesh@p.iam.gserviceaccount.com" in act[2]


def test_a_question_that_could_not_be_asked_is_said_never_raised():
    got = _check(_Google(*ALL, fail="cloudresourcemanager"))
    assert not got.ok and got.unknown and not got.missing
