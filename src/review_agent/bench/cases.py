"""Ground-truth benchmark: synthetic injected defects + clean changes.

Each case is a tiny repo in two states (before/after) plus the truths that
the after-state contains. Diffs are *generated* from the two states, never
hand-written, so there are no hunk-header counting errors to make.

Every Truth carries a `snippet` that must appear on its claimed line; the
loader refuses to start if any doesn't. That keeps the line numbers honest
against the author's own mistakes.

Clean cases have no truths: every finding on them is a false positive.
Without them precision cannot be measured, so they are not optional.

This is a validation-study seed, not a public benchmark: it is small, it
was written by the same person who wrote the heuristic baseline (so that
baseline's score is an upper bound on itself), and it covers Python only.
"""
from __future__ import annotations

from dataclasses import dataclass

from review_agent.schemas import Category


@dataclass(frozen=True)
class Truth:
    file: str
    line: int  # 1-based, in the AFTER version of `file`
    category: Category
    snippet: str  # must appear on that line
    note: str = ""


@dataclass(frozen=True)
class Case:
    id: str
    before: dict[str, str]
    after: dict[str, str]
    truths: tuple[Truth, ...] = ()
    # Set for cases mined from a real repository: `after`/`before` then hold
    # only the touched files (for the diff), and materialize() extracts the
    # full tree at git_ref so the context engine can see callers elsewhere.
    git_repo: str | None = None
    git_ref: str | None = None
    note: str = ""

    @property
    def clean(self) -> bool:
        return not self.truths


SEC, LOG, PERF = Category.SECURITY, Category.LOGIC, Category.PERFORMANCE

CASES: list[Case] = [
    # ------------------------------------------------------------ security
    Case(
        id="sec-path-traversal",
        before={"app/files.py": 'import os\n\nBASE = "/srv/files"\n\n\ndef read_file(filename):\n    safe = os.path.basename(filename)\n    with open(os.path.join(BASE, safe)) as fh:\n        return fh.read()\n'},
        after={"app/files.py": 'import os\n\nBASE = "/srv/files"\n\n\ndef read_file(filename):\n    with open(os.path.join(BASE, filename)) as fh:\n        return fh.read()\n'},
        truths=(Truth("app/files.py", 7, SEC, "os.path.join(BASE, filename)", "basename() sanitisation removed"),),
    ),
    Case(
        id="sec-auth-guard-removed",
        before={"app/accounts.py": 'from app.auth import current_user\n\n\ndef delete_account(request, account_id):\n    user = current_user(request)\n    if not user.is_admin:\n        raise PermissionError("admin only")\n    db.delete("accounts", account_id)\n    return {"deleted": account_id}\n'},
        after={"app/accounts.py": 'from app.auth import current_user\n\n\ndef delete_account(request, account_id):\n    user = current_user(request)\n    db.delete("accounts", account_id)\n    return {"deleted": account_id, "by": user.id}\n'},
        truths=(Truth("app/accounts.py", 6, SEC, "db.delete", "admin check removed before destructive op"),),
    ),
    Case(
        id="sec-hardcoded-secret",
        before={"app/billing.py": 'import os\n\nSTRIPE_KEY = os.environ["STRIPE_KEY"]\n\n\ndef charge(amount):\n    return stripe.charge(STRIPE_KEY, amount)\n'},
        after={"app/billing.py": 'import os\n\nSTRIPE_KEY = "sk_live_51HxQ9zAbCdEf0123456789"\n\n\ndef charge(amount):\n    return stripe.charge(STRIPE_KEY, amount)\n'},
        truths=(Truth("app/billing.py", 3, SEC, "sk_live_"),),
    ),
    Case(
        id="sec-sql-injection",
        before={"app/users.py": 'def get_user(cursor, user_id):\n    cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))\n    return cursor.fetchone()\n'},
        after={"app/users.py": 'def get_user(cursor, user_id):\n    cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")\n    return cursor.fetchone()\n'},
        truths=(Truth("app/users.py", 2, SEC, 'f"SELECT'),),
    ),
    Case(
        id="sec-shell-injection",
        before={"app/images.py": 'import subprocess\n\n\ndef thumbnail(path):\n    subprocess.run(["convert", path, "-resize", "100x100", path + ".thumb.png"], check=True)\n'},
        after={"app/images.py": 'import subprocess\n\n\ndef thumbnail(path):\n    subprocess.run(f"convert {path} -resize 100x100 {path}.thumb.png", shell=True, check=True)\n'},
        truths=(Truth("app/images.py", 5, SEC, "shell=True"),),
    ),
    Case(
        id="sec-unsafe-deserialization",
        before={"app/session.py": 'import json\n\n\ndef load_session(raw):\n    return json.loads(raw)\n'},
        after={"app/session.py": 'import pickle\n\n\ndef load_session(raw):\n    return pickle.loads(raw)\n'},
        truths=(Truth("app/session.py", 5, SEC, "pickle.loads"),),
    ),
    # --------------------------------------------------------------- logic
    Case(
        id="logic-off-by-one",
        before={"app/cart.py": 'def total(prices):\n    s = 0\n    for i in range(len(prices)):\n        s += prices[i]\n    return s\n'},
        after={"app/cart.py": 'def total(prices):\n    s = 0\n    for i in range(len(prices) + 1):\n        s += prices[i]\n    return s\n'},
        truths=(Truth("app/cart.py", 3, LOG, "len(prices) + 1"),),
    ),
    Case(
        id="logic-none-deref",
        before={"app/mail.py": 'def email_for(db, user_id):\n    user = db.get(user_id)\n    if user is None:\n        return None\n    return user.email\n'},
        after={"app/mail.py": 'def email_for(db, user_id):\n    user = db.get(user_id)\n    return user.email\n'},
        truths=(Truth("app/mail.py", 3, LOG, "user.email", "None guard removed; db.get can return None"),),
    ),
    Case(
        id="logic-swallowed-exception",
        before={"app/store.py": 'def save(record):\n    db.insert(record)\n'},
        after={"app/store.py": 'def save(record):\n    try:\n        db.insert(record)\n    except Exception:\n        pass\n'},
        truths=(Truth("app/store.py", 4, LOG, "except Exception"),),
    ),
    Case(
        id="logic-mutable-default",
        before={"app/tags.py": 'def add_tag(tag, tags=None):\n    if tags is None:\n        tags = []\n    tags.append(tag)\n    return tags\n'},
        after={"app/tags.py": 'def add_tag(tag, tags=[]):\n    tags.append(tag)\n    return tags\n'},
        truths=(Truth("app/tags.py", 1, LOG, "tags=[]"),),
    ),
    Case(
        id="logic-stale-closure",
        before={"app/handlers.py": 'def make_handlers(ids):\n    handlers = []\n    for i in ids:\n        handlers.append(lambda i=i: process(i))\n    return handlers\n'},
        after={"app/handlers.py": 'def make_handlers(ids):\n    handlers = []\n    for i in ids:\n        handlers.append(lambda: process(i))\n    return handlers\n'},
        truths=(Truth("app/handlers.py", 4, LOG, "lambda: process(i)", "late-binding closure; `for` line is unchanged context"),),
    ),
    Case(
        id="logic-toctou",
        before={"app/once.py": 'import os\n\n\ndef create_once(path, data):\n    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)\n    with os.fdopen(fd, "w") as fh:\n        fh.write(data)\n'},
        after={"app/once.py": 'import os\n\n\ndef create_once(path, data):\n    if not os.path.exists(path):\n        with open(path, "w") as fh:\n            fh.write(data)\n'},
        truths=(Truth("app/once.py", 5, LOG, "os.path.exists"),),
    ),
    # ---------------------------------------------------------- performance
    Case(
        id="perf-n-plus-one",
        before={"app/orders.py": 'def order_totals(db, order_ids):\n    rows = db.query("SELECT id, total FROM orders WHERE id IN %s", (tuple(order_ids),))\n    return {r.id: r.total for r in rows}\n'},
        after={"app/orders.py": 'def order_totals(db, order_ids):\n    totals = {}\n    for oid in order_ids:\n        row = db.query("SELECT total FROM orders WHERE id = %s", (oid,))\n        totals[oid] = row.total\n    return totals\n'},
        truths=(Truth("app/orders.py", 4, PERF, 'db.query("SELECT total'),),
    ),
    Case(
        id="perf-quadratic-membership",
        before={"app/active.py": 'def active_users(users, active_ids):\n    active = set(active_ids)\n    return [u for u in users if u.id in active]\n'},
        after={"app/active.py": 'def active_users(users, active_ids_list):\n    result = []\n    for u in users:\n        if u.id in active_ids_list:\n            result.append(u)\n    return result\n'},
        truths=(Truth("app/active.py", 4, PERF, "in active_ids_list"),),
    ),
    # ---------------------------------------------------------------- clean
    Case(
        id="clean-rename",
        before={"app/geom.py": "def calc(a, b):\n    r = a * b\n    return r\n"},
        after={"app/geom.py": "def calc(width, height):\n    area = width * height\n    return area\n"},
    ),
    Case(
        id="clean-docstring",
        before={"app/text.py": 'def slugify(text):\n    return text.lower().replace(" ", "-")\n'},
        after={"app/text.py": 'def slugify(text):\n    """Lowercase and replace spaces with dashes."""\n    return text.lower().replace(" ", "-")\n'},
    ),
    Case(
        id="clean-extract-helper",
        before={"app/report.py": 'def report(items):\n    total = sum(i.price * i.qty for i in items)\n    return f"Total: {total:.2f}"\n'},
        after={"app/report.py": 'def line_total(item):\n    return item.price * item.qty\n\n\ndef report(items):\n    total = sum(line_total(i) for i in items)\n    return f"Total: {total:.2f}"\n'},
    ),
    Case(
        id="clean-add-logging",
        before={"app/jobs.py": "import logging\n\nlog = logging.getLogger(__name__)\n\n\ndef start(job):\n    job.run()\n"},
        after={"app/jobs.py": 'import logging\n\nlog = logging.getLogger(__name__)\n\n\ndef start(job):\n    log.info("starting job %s", job.id)\n    job.run()\n'},
    ),
    Case(
        id="clean-add-test-file",
        before={"app/mathx.py": "def double(x):\n    return x * 2\n"},
        after={
            "app/mathx.py": "def double(x):\n    return x * 2\n",
            "tests/test_mathx.py": "from app.mathx import double\n\n\ndef test_double():\n    assert double(2) == 4\n",
        },
    ),
    Case(
        id="clean-type-hints",
        before={"app/home.py": 'import sys\nimport os\n\n\ndef home(user):\n    return os.path.join("/home", user)\n'},
        after={"app/home.py": 'import os\nimport sys\n\n\ndef home(user: str) -> str:\n    return os.path.join("/home", user)\n'},
    ),
]


# ----------------------------------------------------------- cross-file cases
#
# The diff touches one file and looks benign on its own; whether it is a
# defect is decided by an UNCHANGED second file -- a caller that hands it
# untrusted input or None, a callee that does not do what its name says, a
# compliance comment at a call site. Each defect has a clean mirror: the
# same diff with the other file making it safe. Context should raise recall
# on the first set and lower false positives on the second; a diff-only
# reviewer cannot tell them apart.

_SAFE_JOIN_FAKE = 'import os\n\n\ndef safe_join(base, name):\n    # TODO: normalise and check containment\n    return os.path.join(base, name)\n'
_SAFE_JOIN_REAL = ('import os\n\n\ndef safe_join(base, name):\n    path = os.path.realpath(os.path.join(base, name))\n'
                   '    if not path.startswith(os.path.realpath(base) + os.sep):\n        raise ValueError("path escapes base")\n    return path\n')
_UPLOAD_BEFORE = 'import os\n\nUPLOADS = "/srv/uploads"\n\n\ndef store(filename, data):\n    path = os.path.join(UPLOADS, filename)\n    with open(path, "wb") as fh:\n        fh.write(data)\n'
_UPLOAD_AFTER = 'from app.util import safe_join\n\nUPLOADS = "/srv/uploads"\n\n\ndef store(filename, data):\n    path = safe_join(UPLOADS, filename)\n    with open(path, "wb") as fh:\n        fh.write(data)\n'

_REPORTS_BEFORE = 'import os\n\nREPORTS = "/srv/reports"\n'
_REPORTS_AFTER = 'import os\n\nREPORTS = "/srv/reports"\n\n\ndef read_report(name):\n    with open(os.path.join(REPORTS, name)) as fh:\n        return fh.read()\n'
_WEB_UNTRUSTED = 'from app.reports import read_report\n\n\ndef report_view(request):\n    name = request.args["name"]\n    return read_report(name)\n'
_WEB_ALLOWLIST = ('from app.reports import read_report\n\nALLOWED = {"daily.csv", "weekly.csv"}\n\n\ndef report_view(request):\n'
                  '    name = request.args["name"]\n    if name not in ALLOWED:\n        raise KeyError(name)\n    return read_report(name)\n')

_USERS_BEFORE = '# user helpers\n'
_USERS_AFTER = '# user helpers\n\n\ndef display_name(user):\n    return user.profile.name or user.email\n'
_API_NONE = ('from app.db import db\nfrom app.users import display_name\n\n\ndef whoami(request):\n'
             '    user = db.get(request.user_id)  # None once the account is deleted\n    return {"name": display_name(user)}\n')
_API_GUARDED = ('from app.db import db\nfrom app.users import display_name\n\n\ndef whoami(request):\n    user = db.get(request.user_id)\n'
                '    if user is None:\n        return {"name": None}\n    return {"name": display_name(user)}\n')

_NET_NONE = ('import requests\n\n\ndef fetch(url):\n    """Return the response, or None if the request failed."""\n    try:\n'
             '        return requests.get(url, timeout=5)\n    except requests.RequestException:\n        return None\n')
_NET_RAISES = ('import requests\n\n\ndef fetch(url):\n    """Return the response; raises requests.RequestException on failure."""\n'
               '    response = requests.get(url, timeout=5)\n    response.raise_for_status()\n    return response\n')
_JOBS_BEFORE = 'from app.net import fetch\n\n\ndef run(job):\n    job.result = None\n'
_JOBS_AFTER = 'from app.net import fetch\n\n\ndef run(job):\n    data = fetch(job.url).json()\n    job.result = data\n'

_COUNTER_BEFORE = 'count = 0\n'
_COUNTER_AFTER = 'count = 0\n\n\ndef bump():\n    global count\n    count = count + 1\n'
_WORKER_THREADS = ('from concurrent.futures import ThreadPoolExecutor\n\nfrom app.counter import bump\n\n\ndef process_all(items):\n'
                   '    with ThreadPoolExecutor(max_workers=8) as pool:\n        for item in items:\n            pool.submit(handle, item)\n\n\n'
                   'def handle(item):\n    item.process()\n    bump()\n')
_WORKER_SERIAL = 'from app.counter import bump\n\n\ndef process_all(items):\n    for item in items:\n        item.process()\n        bump()\n'

_PERMS_BEFORE = 'def can_edit(user, record):\n    return user.id == record.owner_id\n'
_PERMS_AFTER = 'def can_edit(user, record):\n    if user.is_staff:\n        return True\n    return user.id == record.owner_id\n'
_BILLING = ('from app.perms import can_edit\n\n\ndef amend_invoice(user, invoice, changes):\n'
            '    # Compliance: invoices are immutable to staff; only the owner may amend.\n    if not can_edit(user, invoice):\n'
            '        raise PermissionError("owner only")\n    invoice.apply(changes)\n')
_NOTES = ('from app.perms import can_edit\n\n\ndef edit_note(user, note, text):\n    # Staff moderate all notes; owners edit their own.\n'
          '    if not can_edit(user, note):\n        raise PermissionError("not allowed")\n    note.text = text\n')

CROSSFILE_CASES: list[Case] = [
    Case(id="xf-fake-safe-join",
         before={"app/util.py": _SAFE_JOIN_FAKE, "app/upload.py": _UPLOAD_BEFORE},
         after={"app/util.py": _SAFE_JOIN_FAKE, "app/upload.py": _UPLOAD_AFTER},
         truths=(Truth("app/upload.py", 7, SEC, "safe_join(UPLOADS, filename)", "safe_join() does not sanitise; the diff looks like a fix"),)),
    Case(id="xf-clean-real-safe-join",
         before={"app/util.py": _SAFE_JOIN_REAL, "app/upload.py": _UPLOAD_BEFORE},
         after={"app/util.py": _SAFE_JOIN_REAL, "app/upload.py": _UPLOAD_AFTER}),
    Case(id="xf-untrusted-name",
         before={"app/reports.py": _REPORTS_BEFORE, "app/web.py": _WEB_UNTRUSTED},
         after={"app/reports.py": _REPORTS_AFTER, "app/web.py": _WEB_UNTRUSTED},
         truths=(Truth("app/reports.py", 7, SEC, "os.path.join(REPORTS, name)", "caller passes request.args straight through"),)),
    Case(id="xf-clean-allowlisted-name",
         before={"app/reports.py": _REPORTS_BEFORE, "app/web.py": _WEB_ALLOWLIST},
         after={"app/reports.py": _REPORTS_AFTER, "app/web.py": _WEB_ALLOWLIST}),
    Case(id="xf-caller-passes-none",
         before={"app/users.py": _USERS_BEFORE, "app/api.py": _API_NONE},
         after={"app/users.py": _USERS_AFTER, "app/api.py": _API_NONE},
         truths=(Truth("app/users.py", 5, LOG, "user.profile.name", "the only caller passes a possibly-None user"),)),
    Case(id="xf-clean-caller-guards-none",
         before={"app/users.py": _USERS_BEFORE, "app/api.py": _API_GUARDED},
         after={"app/users.py": _USERS_AFTER, "app/api.py": _API_GUARDED}),
    Case(id="xf-callee-returns-none",
         before={"app/net.py": _NET_NONE, "app/jobs.py": _JOBS_BEFORE},
         after={"app/net.py": _NET_NONE, "app/jobs.py": _JOBS_AFTER},
         truths=(Truth("app/jobs.py", 5, LOG, "fetch(job.url).json()", "fetch() returns None on failure"),)),
    Case(id="xf-clean-callee-raises",
         before={"app/net.py": _NET_RAISES, "app/jobs.py": _JOBS_BEFORE},
         after={"app/net.py": _NET_RAISES, "app/jobs.py": _JOBS_AFTER}),
    Case(id="xf-caller-threads",
         before={"app/counter.py": _COUNTER_BEFORE, "app/worker.py": _WORKER_THREADS},
         after={"app/counter.py": _COUNTER_AFTER, "app/worker.py": _WORKER_THREADS},
         truths=(Truth("app/counter.py", 6, LOG, "count = count + 1", "called from an 8-thread pool"),)),
    Case(id="xf-clean-single-thread",
         before={"app/counter.py": _COUNTER_BEFORE, "app/worker.py": _WORKER_SERIAL},
         after={"app/counter.py": _COUNTER_AFTER, "app/worker.py": _WORKER_SERIAL}),
    Case(id="xf-staff-invoice",
         before={"app/perms.py": _PERMS_BEFORE, "app/billing.py": _BILLING},
         after={"app/perms.py": _PERMS_AFTER, "app/billing.py": _BILLING},
         truths=(Truth("app/perms.py", 2, SEC, "if user.is_staff:", "caller guards invoices that staff must never amend"),)),
    Case(id="xf-clean-staff-moderates",
         before={"app/perms.py": _PERMS_BEFORE, "app/notes.py": _NOTES},
         after={"app/perms.py": _PERMS_AFTER, "app/notes.py": _NOTES}),
]

SUITES: dict[str, list[Case]] = {
    "builtin": CASES,
    "crossfile": CROSSFILE_CASES,
    "all": CASES + CROSSFILE_CASES,
}


def validate_cases(cases: list[Case]) -> list[Case]:
    """Refuse to start if any truth's snippet is not on its claimed line."""
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    for case in cases:
        for t in case.truths:
            if t.file not in case.after:
                raise ValueError(f"{case.id}: truth file {t.file!r} not in after-state")
            lines = case.after[t.file].splitlines()
            if not (1 <= t.line <= len(lines)):
                raise ValueError(f"{case.id}: truth line {t.line} out of range for {t.file} ({len(lines)} lines)")
            if t.snippet not in lines[t.line - 1]:
                raise ValueError(
                    f"{case.id}: snippet {t.snippet!r} not on {t.file}:{t.line} -> {lines[t.line - 1]!r}"
                )
    return list(cases)


def load_cases(suite: str = "builtin") -> list[Case]:
    """Return a validated suite: builtin (default), crossfile, or all."""
    if suite not in SUITES:
        raise ValueError(f"unknown suite {suite!r}; choose from {sorted(SUITES)}")
    return validate_cases(SUITES[suite])


# ------------------------------------------------------------- serialization

def case_to_dict(case: Case) -> dict:
    return {
        "id": case.id, "before": case.before, "after": case.after,
        "truths": [{"file": t.file, "line": t.line, "category": t.category.value, "snippet": t.snippet, "note": t.note}
                   for t in case.truths],
        "git_repo": case.git_repo, "git_ref": case.git_ref, "note": case.note,
    }


def case_from_dict(d: dict) -> Case:
    return Case(
        id=d["id"], before=dict(d.get("before", {})), after=dict(d.get("after", {})),
        truths=tuple(Truth(t["file"], int(t["line"]), Category(t["category"]), t["snippet"], t.get("note", ""))
                     for t in d.get("truths", [])),
        git_repo=d.get("git_repo"), git_ref=d.get("git_ref"), note=d.get("note", ""),
    )


def write_cases(cases: list[Case], path) -> None:
    import json
    from pathlib import Path

    Path(path).write_text(json.dumps([case_to_dict(c) for c in cases], indent=1, ensure_ascii=False), encoding="utf-8")


def read_cases(path) -> list[Case]:
    import json
    from pathlib import Path

    return validate_cases([case_from_dict(d) for d in json.loads(Path(path).read_text(encoding="utf-8"))])
