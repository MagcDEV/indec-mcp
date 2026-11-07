import pytest

from indec_mcp.ratelimit import RateLimiter, Rule, client_ip


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_burst_then_refill():
    clock = Clock()
    rl = RateLimiter([Rule(capacity=3, per_seconds=3)], None, clock=clock)  # 1 token/s
    assert [rl.check("a") for _ in range(3)] == [0, 0, 0]
    wait = rl.check("a")
    assert wait == pytest.approx(1.0)
    clock.t += 1.0
    assert rl.check("a") == 0


def test_ips_are_independent():
    rl = RateLimiter([Rule(capacity=1, per_seconds=60)], None, clock=Clock())
    assert rl.check("a") == 0
    assert rl.check("a") > 0
    assert rl.check("b") == 0


def test_daily_cap_applies_even_when_burst_bucket_has_tokens():
    clock = Clock()
    rl = RateLimiter([Rule(capacity=10, per_seconds=1), Rule(capacity=5, per_seconds=86_400)], None, clock=clock)
    for _ in range(5):
        assert rl.check("a") == 0
        clock.t += 1
    assert rl.check("a") > 3600  # out of daily tokens: wait is hours, not seconds


def test_global_bucket_limits_many_ips():
    rl = RateLimiter([Rule(capacity=100, per_seconds=1)], Rule(capacity=3, per_seconds=3), clock=Clock())
    assert [rl.check(f"ip{i}") for i in range(3)] == [0, 0, 0]
    assert rl.check("ip99") > 0


def test_rejected_requests_do_not_consume_tokens():
    clock = Clock()
    rl = RateLimiter([Rule(capacity=1, per_seconds=10)], Rule(capacity=1, per_seconds=10), clock=clock)
    assert rl.check("a") == 0
    assert rl.check("b") > 0  # global empty; b's own bucket must stay full
    clock.t += 10
    assert rl.check("b") == 0


def test_tracked_ips_are_bounded():
    rl = RateLimiter([Rule(capacity=1, per_seconds=60)], None, max_tracked_ips=2, clock=Clock())
    for ip in ("a", "b", "c"):
        rl.check(ip)
    assert list(rl._ips) == ["b", "c"]


def _scope(xff=None, client=("10.0.0.1", 1234)):
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return {"headers": headers, "client": client}


@pytest.mark.parametrize(
    "xff, hops, expected",
    [
        ("203.0.113.7", 1, "203.0.113.7"),
        # A client forging X-Forwarded-For can only prepend; the proxy appends the real IP.
        ("1.1.1.1, 203.0.113.7", 1, "203.0.113.7"),
        ("1.1.1.1, 203.0.113.7, 172.16.0.2", 2, "203.0.113.7"),
        (None, 1, "10.0.0.1"),
        ("203.0.113.7", 0, "10.0.0.1"),
    ],
)
def test_client_ip_counts_trusted_hops_from_the_right(xff, hops, expected):
    assert client_ip(_scope(xff), hops) == expected


def test_platform_ip_header_takes_precedence_over_forwarded_for():
    scope = {"headers": [(b"x-forwarded-for", b"1.1.1.1, 100.64.0.7"), (b"x-real-ip", b"203.0.113.7")],
             "client": ("10.0.0.1", 1)}
    assert client_ip(scope, 1, "X-Real-IP") == "203.0.113.7"
    # Header configured but absent: fall back to X-Forwarded-For hops.
    assert client_ip({"headers": [(b"x-forwarded-for", b"100.64.0.7")], "client": None}, 1, "x-real-ip") == "100.64.0.7"
