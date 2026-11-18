"""P0-3 / P1-4 / P1-5: the admin plane must not do its expensive work on the loop.

Three separate defects, one shared theme — something slow ran where every other
request could see it:

* `POST /admin/bind` held one process-wide `RLock` across the Argon2 digest *and*
  the `admin.json`/`devices.json` writes, with no rate limit at all.
* `POST /admin/api/login` (and the step-up device routes) verified the password
  inline, so four parallel logins froze the loop for the sum of their digests.
* `SessionStore` only forgot a row when its own token was presented, so issued
  and rotated-away rows accumulated for the life of the process.

The loop tests below do not trust "it is async now": they patch the hasher with
a *thread-blocking* fake and sample the event loop from a concurrent task. If
the digest moves back onto the loop, the sampled gap jumps to the sleep length
and the assertion fails.
"""

from __future__ import annotations

import asyncio
import time

from argon2.exceptions import VerificationError

from vade.admin import auth as auth_module
from vade.admin.app import create_admin_app
from vade.admin.auth import SWEEP_EVERY, SessionStore, verify_password
from vade.admin.setup import SetupService

from .conftest import PASSWORD, bearer, client_for, make_context
from .test_bind import NEW_PASSWORD, hello, p256_device, post, run, seal

#: Long enough that "on the loop" is unmistakable, short enough to keep the
#: suite honest. The ticker samples every 5 ms, so a free loop reports ~10 ms.
DIGEST_S = 0.40
#: 2x margin: the failure mode is a gap of ~DIGEST_S, the pass mode ~0.05 s.
MAX_GAP_S = 0.20


def _slowest_gap(gaps: list[float]) -> float:
    """The worst scheduling gap the ticker saw, or a failure that says why.

    A loop blocked for the whole request produces *zero* samples (the ticker
    cannot even finish its first sleep until the request returns), and `max()`
    on that would raise a `ValueError` that says nothing useful.
    """
    assert len(gaps) >= 3, f"the loop never ticked during the request: {len(gaps)} samples"
    return max(gaps)


# --------------------------------------------------------------------- fakes
class _InstrumentedHasher:
    """Stand-in for `argon2.PasswordHasher` whose `verify` really blocks a thread.

    `time.sleep` releases the GIL, so this is exactly the shape of the real
    call: CPU + a memory-heavy digest, measured from outside as elapsed time.
    It also counts concurrent entries, which is how `Semaphore(2)` is proven.
    """

    def __init__(self, delay: float, *, accept: str | None = None) -> None:
        self.delay = delay
        #: `None` accepts any password; a string accepts only that one (so a
        #: correct-password request can still succeed while wrong ones fail).
        self.accept = accept
        self.calls = 0
        self.active = 0
        self.peak = 0

    def verify(self, stored: str, password: str) -> bool:
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
        finally:
            self.active -= 1
        if self.accept is not None and password != self.accept:
            raise VerificationError("wrong password")
        return True

    def check_needs_rehash(self, stored: str) -> bool:
        # "The stored hash is current", so a successful login does not add a
        # second digest to the measurement.
        return False


async def _ticker(*, stop: asyncio.Event, gap: float = 0.005) -> list[float]:
    """Sample the event loop's scheduling gaps until `stop` is set."""
    gaps: list[float] = []
    loop = asyncio.get_running_loop()
    last = loop.time()
    while not stop.is_set():
        await asyncio.sleep(gap)
        now = loop.time()
        gaps.append(now - last)
        last = now
    return gaps


async def _measured(coro, *, timeout: float = 10.0):
    """Run `coro` while a ticker watches the loop; return (result, gaps)."""
    stop = asyncio.Event()
    ticker = asyncio.create_task(_ticker(stop=stop))
    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
    finally:
        stop.set()
        gaps = await ticker
    return result, gaps


# ------------------------------------------------------------------ P0-3 bind
def test_a_bind_digest_does_not_stall_the_event_loop(tmp_path, monkeypatch):
    """`POST /admin/bind` verifies in a worker thread, not in the loop."""
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)
    hasher = _InstrumentedHasher(DIGEST_S)
    monkeypatch.setattr(auth_module, "_hasher", hasher)

    async def scenario():
        client = client_for(app)
        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(client, ticket)
        key, pub = p256_device()
        body = seal(info, ticket, device_key=key, device_pub=pub)

        response, gaps = await _measured(post(client, body))
        assert response.status_code == 200, response.text
        assert hasher.calls == 1
        assert _slowest_gap(gaps) < MAX_GAP_S, f"event loop stalled: gaps={gaps}"
        assert len(ctx.devices) == 1

    run(scenario())


def test_concurrent_binds_do_not_serialise_on_the_digest(tmp_path, monkeypatch):
    """Two independent tickets bind at once; neither waits on the other's digest.

    Under the old lock the second `complete()` could not even reach its own
    digest, and both ran on the loop besides. The proof is structural: two
    digests of `DIGEST_S` complete in about one digest's time.
    """
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)
    hasher = _InstrumentedHasher(DIGEST_S)
    monkeypatch.setattr(auth_module, "_hasher", hasher)

    async def scenario():
        client = client_for(app)
        bodies = []
        for _ in range(2):
            ticket = ctx.setup.create_ticket()["ticket"]
            info = await hello(client, ticket)
            key, pub = p256_device()
            bodies.append(seal(info, ticket, device_key=key, device_pub=pub))

        started = asyncio.get_running_loop().time()
        responses, gaps = await _measured(
            asyncio.gather(*(post(client, body) for body in bodies))
        )
        elapsed = asyncio.get_running_loop().time() - started
        assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
        assert hasher.peak == 2, hasher.peak
        assert elapsed < DIGEST_S * 1.8, elapsed
        assert _slowest_gap(gaps) < MAX_GAP_S, f"gaps={gaps}"
        assert len(ctx.devices) == 2

    run(scenario())


def test_bind_spends_the_login_budget_per_ip(tmp_path, monkeypatch):
    """Five failed binds lock the address out with 429 — the login budget.

    The hasher rejects instantly here: the budget, not the digest, is under
    test. The ticket must survive every failure (verify-then-delete).
    """
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)
    monkeypatch.setattr(auth_module, "_hasher", _InstrumentedHasher(0.0, accept=""))

    async def scenario():
        client = client_for(app)
        ticket = ctx.setup.create_ticket()["ticket"]
        key, pub = p256_device()

        body = None
        for attempt in range(5):
            # Each attempt needs a fresh hello: the challenge is single-use even
            # when the rest of the request fails.
            body = seal(await hello(client, ticket), ticket, device_key=key, device_pub=pub)
            response = await post(client, body)
            assert response.status_code == 401, response.text
            assert response.json()["code"] == "BIND_UNAUTHORIZED"

        # 先验后删: nothing was consumed and nothing was enrolled.
        assert ctx.setup.peek_ticket(ticket) is not None
        assert len(ctx.devices) == 0

        # The 6th attempt is refused before the envelope is even opened, so it
        # costs no CPU and cannot touch the ticket.
        blocked = await post(client, body)
        assert blocked.status_code == 429, blocked.text
        assert blocked.json()["code"] == "ADMIN_LOCKED"
        assert ctx.setup.peek_ticket(ticket) is not None

        # It is the *same* bucket: login from this address is locked out too.
        challenge = (await client.post("/admin/api/login/challenge", json={})).json()
        logged = await client.post(
            "/admin/api/login", json={"challenge_id": challenge["challenge_id"], "password": PASSWORD}
        )
        assert logged.status_code == 429, logged.text
        assert logged.json()["code"] == "ADMIN_LOCKED"

    run(scenario())


def test_bind_failures_count_toward_the_global_twenty_per_five_minutes(tmp_path, monkeypatch):
    """20 failures across addresses lock the plane, exactly as login does."""
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)
    monkeypatch.setattr(auth_module, "_hasher", _InstrumentedHasher(0.0, accept=""))

    async def fail_five(address: str) -> None:
        client = client_for(app, ip=address)
        ticket = ctx.setup.create_ticket()["ticket"]
        key, pub = p256_device()
        for _ in range(5):
            body = seal(await hello(client, ticket), ticket, device_key=key, device_pub=pub)
            response = await post(client, body)
            assert response.status_code == 401, response.text
        assert ctx.setup.peek_ticket(ticket) is not None

    async def scenario():
        for octet in range(1, 5):  # 4 addresses x 5 failures = the global 20
            await fail_five(f"203.0.113.{octet}")
        assert ctx._locked_until > time.time()

        # A brand-new address is still refused: the ledger is global, and that
        # is the DoS trade-off login already made.
        fresh = client_for(app, ip="198.51.100.7")
        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(fresh, ticket)
        key, pub = p256_device()
        body = seal(info, ticket, device_key=key, device_pub=pub)
        response = await post(fresh, body)
        assert response.status_code == 429, response.text
        assert response.json()["code"] == "ADMIN_LOCKED"
        assert len(ctx.devices) == 0

    run(scenario())


def test_two_first_run_binds_do_not_interleave_admin_json(tmp_path, monkeypatch):
    """Only one bind may create `admin.json`; the other is refused, ticket intact.

    The commit (and its `admin.json` write) deliberately runs *outside* the bind
    lock, so two first-run binds must not interleave: the loser's rollback would
    otherwise unlink the winner's finished document. `BIND_SETUP_LOCKED` (423,
    retriable, already in `schema/device-bind.json`) is that refusal.
    """
    ctx = make_context(tmp_path, initialize=False)
    app = create_admin_app(ctx)
    original = SetupService.begin_initialize_bound

    def slow_initialize(self, *, password, ip):
        # Widen the commit window: the winner is inside it while the second
        # request reaches its reservation (0.15 s instead of a file write).
        time.sleep(0.15)
        return original(self, password=password, ip=ip)

    monkeypatch.setattr(SetupService, "begin_initialize_bound", slow_initialize)

    async def scenario():
        client = client_for(app)
        entries = []  # (ticket, body, signing key, device pubkey)
        for _ in range(2):
            ticket = ctx.setup.create_ticket()["ticket"]
            info = await hello(client, ticket)
            key, pub = p256_device()
            body = seal(info, ticket, password=NEW_PASSWORD, device_key=key, device_pub=pub)
            entries.append((ticket, body, key, pub))

        responses = await asyncio.gather(*(post(client, body) for _, body, _, _ in entries))
        statuses = sorted(response.status_code for response in responses)
        assert statuses == [200, 423], [response.text for response in responses]

        winner = next(r for r in responses if r.status_code == 200)
        loser = next(r for r in responses if r.status_code == 423)
        assert loser.json()["code"] == "BIND_SETUP_LOCKED"
        assert winner.json()["mode"] == "device"

        # One document, one device, one session — and the refused ticket still
        # works, which is the whole point of refusing instead of racing.
        assert ctx.setup.initialized() is True
        assert len(ctx.devices) == 1
        index = [response.status_code for response in responses].index(423)
        refused_ticket, _, refused_key, refused_pub = entries[index]
        assert ctx.setup.peek_ticket(refused_ticket) is not None
        retried = await post(
            client, seal(await hello(client, refused_ticket), refused_ticket,
                         password=NEW_PASSWORD, device_key=refused_key, device_pub=refused_pub)
        )
        assert retried.status_code == 200, retried.text
        assert len(ctx.devices) == 2

    run(scenario())


# --------------------------------------------------------------- P1-4 login
def test_four_concurrent_logins_keep_the_loop_ticking(tmp_path, monkeypatch):
    """Four logins at once: the loop keeps ticking and at most two digests run.

    `elapsed` also proves the cap is real — four digests of `DIGEST_S` with two
    slots take two waves, not four (>= 2x) and not eight (<= 3x).
    """
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)
    hasher = _InstrumentedHasher(DIGEST_S)
    monkeypatch.setattr(auth_module, "_hasher", hasher)

    async def one_login(client):
        challenge = (await client.post("/admin/api/login/challenge", json={})).json()
        return await client.post(
            "/admin/api/login",
            json={"challenge_id": challenge["challenge_id"], "password": PASSWORD},
        )

    async def scenario():
        clients = [client_for(app) for _ in range(4)]
        started = asyncio.get_running_loop().time()
        responses, gaps = await _measured(
            asyncio.gather(*(one_login(client) for client in clients)), timeout=20.0
        )
        elapsed = asyncio.get_running_loop().time() - started

        assert [r.status_code for r in responses] == [200] * 4, [r.text for r in responses]
        assert hasher.calls == 4
        assert hasher.peak == 2, f"concurrent digests peaked at {hasher.peak}"
        assert _slowest_gap(gaps) < MAX_GAP_S, f"event loop stalled: gaps={gaps}"
        assert DIGEST_S * 1.8 <= elapsed <= DIGEST_S * 4.0, elapsed

    run(scenario())


def test_revoke_device_password_attempts_charge_the_ip_budget(ctx, app, monkeypatch):
    """A wrong step-up password on the revoke route is a password guess."""
    monkeypatch.setattr(auth_module, "_hasher", _InstrumentedHasher(0.0, accept=PASSWORD))

    async def scenario():
        client = client_for(app)
        challenge = (await client.post("/admin/api/login/challenge", json={})).json()
        login = await client.post(
            "/admin/api/login", json={"challenge_id": challenge["challenge_id"], "password": PASSWORD},
            headers={"X-AC-Client": "cli"},
        )
        assert login.status_code == 200, login.text
        headers = bearer(login.json()["access_token"])

        # Five wrong step-up passwords: the same bucket login uses.
        for attempt in range(5):
            response = await client.post(
                "/admin/api/devices/dev-does-not-exist/revoke",
                json={"password": f"not-the-password-{attempt}"}, headers=headers,
            )
            assert response.status_code == 401, response.text
            assert response.json()["code"] == "AUTH_FAILED"

        blocked = await client.post(
            "/admin/api/devices/dev-does-not-exist/revoke",
            json={"password": PASSWORD}, headers=headers,
        )
        assert blocked.status_code == 429, blocked.text
        assert blocked.json()["code"] == "ADMIN_LOCKED"

    run(scenario())


# ------------------------------------------------------- P1-5 session sweep
class _Clock:
    """Injectable wall clock (the task asked for a controllable one)."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_guard_sweeps_expired_access_and_refresh_rows(tmp_path):
    """100 issued-but-never-presented sessions do not stay in the table.

    Only `guard()` can clean them up: nothing ever presents those tokens, and
    the refresh rows are 30-day TTLs. The sweep is tied to guard calls, so this
    drives real requests through the app instead of calling `prune()`.
    """
    ctx = make_context(tmp_path)
    clock = _Clock()
    # Both TTLs deliberately short *and* different: the sweep must drop the
    # expired access rows and the expired refresh rows, and keep the one live
    # pair. 900/1000 with a 1100 s jump: junk is dead, the new login is not.
    ctx.sessions = SessionStore(access_ttl_s=900, refresh_ttl_s=1_000, wall=clock)
    ctx.sessions.set_device_checker(ctx.devices.is_active)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        for _ in range(100):
            ctx.sessions.issue(mode="browser", ip="127.0.0.1")
        assert ctx.sessions.active_count() == 100

        # Past both TTLs: those 100 rows are dead but still in the dict.
        clock.advance(1_100)

        challenge = (await client.post("/admin/api/login/challenge", json={})).json()
        login = await client.post(
            "/admin/api/login", json={"challenge_id": challenge["challenge_id"], "password": PASSWORD},
            headers={"X-AC-Client": "cli"},
        )
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
        headers = bearer(token)
        assert ctx.sessions.active_count() == 101

        for _ in range(SWEEP_EVERY - 1):
            alive = await client.get("/admin/api/devices", headers=headers)
            assert alive.status_code == 200, alive.text
        # Not due yet: the sweep has not run, so the dead rows are still there.
        assert ctx.sessions.active_count() == 101
        assert len(ctx.sessions._refresh) == 101

        # The SWEEP_EVERY-th guarded request prunes, and keeps the live session.
        alive = await client.get("/admin/api/devices", headers=headers)
        assert alive.status_code == 200, alive.text
        assert ctx.sessions.active_count() == 1
        assert len(ctx.sessions._refresh) == 1
        assert alive.json()["items"] == [d.public() for d in ctx.devices.all()]

    run(scenario())


def test_prune_reports_what_it_dropped_and_never_drops_a_live_row():
    clock = _Clock()
    store = SessionStore(access_ttl_s=10, refresh_ttl_s=20, wall=clock)
    store.issue(mode="browser")
    store.issue(mode="device", device_id="d1")

    clock.advance(11)
    assert store.prune() == {"access": 2, "refresh": 0}
    assert store.active_count() == 0
    # Refresh rows outlive access rows; they go on their own TTL.
    clock.advance(10)
    assert store.prune() == {"access": 0, "refresh": 2}

    live = store.issue(mode="device", device_id="d2")
    assert store.prune() == {"access": 0, "refresh": 0}
    assert store.resolve(live["access_token"]) is not None


# ----------------------------------------------------- password hash caching
def test_password_hash_is_cached_until_admin_json_changes(tmp_path, monkeypatch):
    ctx = make_context(tmp_path)
    calls = {"n": 0}
    original = SetupService.load_admin

    def counting(self):
        calls["n"] += 1
        return original(self)

    monkeypatch.setattr(SetupService, "load_admin", counting)

    first = ctx.setup.password_hash()
    assert calls["n"] == 1
    assert ctx.setup.password_hash() == first
    assert calls["n"] == 1, "the stored hash was re-parsed on an unchanged file"
    assert verify_password(PASSWORD, first) is True

    ctx.setup.change_password(PASSWORD, "fresh-battery-staple-9")
    after = ctx.setup.password_hash()
    assert after != first, "the cache served a credential that had been replaced"
    assert verify_password("fresh-battery-staple-9", after) is True
