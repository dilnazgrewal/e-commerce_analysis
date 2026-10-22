import numpy as np
import pytest

from app.data.calibrate import fit_bgnbd, snap_price


def simulate_bgnbd(n: int, r: float, alpha: float, a: float, b: float, T: float, seed: int = 0):
    """Draw customers from a known BG/NBD process: returns (x, t_x, T) arrays."""
    rng = np.random.default_rng(seed)
    lam = rng.gamma(r, 1 / alpha, n)
    p = rng.beta(a, b, n)
    x, t_x = np.zeros(n), np.zeros(n)
    for i in range(n):
        t = 0.0
        while True:
            t += rng.exponential(1 / lam[i])
            if t > T:
                break
            x[i] += 1
            t_x[i] = t
            if rng.random() < p[i]:
                break
    return x, t_x, np.full(n, T)


def test_bgnbd_fit_recovers_known_parameters():
    true = {"r": 0.8, "alpha": 30.0, "a": 0.6, "b": 3.0}
    x, t_x, T = simulate_bgnbd(6000, **true, T=500)
    fit = fit_bgnbd(x, t_x, T)
    # The mean purchase rate r/alpha and mean dropout a/(a+b) are well identified.
    assert fit["r"] / fit["alpha"] == pytest.approx(true["r"] / true["alpha"], rel=0.15)
    assert fit["a"] / (fit["a"] + fit["b"]) == pytest.approx(true["a"] / (true["a"] + true["b"]), rel=0.25)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(253.0, 249), (1512.0, 1499), (12_960.0, 12_999), (8.0, 9), (73_400.0, 72_999)],
)
def test_snap_price_uses_indian_price_points(raw, expected):
    assert snap_price(raw) == expected
