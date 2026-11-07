"""
The crossover depends on the metric, so compute it under three of them before recommending.

Mean Nusselt over the channel is a GENEROUS metric for a coarse mesh, because a coarse mesh
under-predicts Nu in the entrance and over-predicts it downstream, and those cancel in the mean.
A cold plate with chip hot spots cares about LOCAL wall temperature, so also report the worst
local error and the length-weighted RMS local error.
"""
import numpy as np

CELL = 20.1263e-6
W = 5.0 * CELL
DH = 2.0 * W
L = 12.8e-3
RHO, CP, K = 997.0, 4180.0, 0.606
PR = 6.0
MU = PR * K / CP
Q = 1.0e5


def g_uniform(n):
    return np.full(n, W / n)


def g_layers(nlay, first=4.63e-6, ratio=1.5, ncore=5):
    t = [first * ratio ** i for i in range(nlay)]
    return np.array(t + [(W - 2.0 * sum(t)) / ncore] * ncore + t[::-1])


def g_overall(overall, nlay, ratio=1.5, ncore=5):
    w = np.array([ratio ** i for i in range(nlay)])
    t = list(overall * w / w.sum())
    return np.array(t + [(W - 2.0 * overall) / ncore] * ncore + t[::-1])


def modes(dy):
    n = len(dy)
    f = np.concatenate([[0.0], np.cumsum(dy)])
    yc = 0.5 * (f[:-1] + f[1:])
    D = np.zeros((n, n))
    for i in range(n - 1):
        w = K / (yc[i + 1] - yc[i])
        D[i, i] += w; D[i + 1, i + 1] += w
        D[i, i + 1] -= w; D[i + 1, i] -= w
    u1 = 6.0 * (yc / W) * (1.0 - yc / W)
    M1 = RHO * CP * u1 * dy
    sM = M1.sum()
    b = np.zeros(n); b[0] += Q; b[-1] += Q
    a1 = b.sum() / sM
    psi = np.linalg.lstsq(D, b - a1 * M1, rcond=None)[0]
    wall_c = Q * dy[0] / (2.0 * K)
    dT_fd = psi[0] + wall_c - float(M1 @ psi) / sM
    R = np.sqrt(M1)
    S = D / R[:, None] / R[None, :]
    lam, V = np.linalg.eigh(0.5 * (S + S.T))
    PHI = V / R[:, None]
    c = PHI.T @ (M1 * (-psi))
    g = PHI[0, :] - (M1 @ PHI) / sM
    C = c * g
    k = lam > 1e-12 * max(lam.max(), 1.0)
    return dT_fd, lam[k], C[k]


def dT_z(m, ubar, z):
    dT_fd, lam, C = m
    return dT_fd + (np.exp(-np.minimum(np.outer(z, lam) / ubar, 700.0)) @ C)


G = {"ref": g_uniform(400), "mesh": g_uniform(5),
     "lay3nom": g_layers(3), "lay2": g_layers(2), "lay3ach": g_overall(1.67e-5, 3)}
M = {k: modes(v) for k, v in G.items()}
# z stations: log-spaced so the entrance is properly represented, then trapezoid weights
z = np.concatenate([np.linspace(1e-6, 1e-4, 400), np.linspace(1e-4, L, 4000)[1:]])
wz = np.gradient(z)


def metrics(name, ubar):
    dr = dT_z(M["ref"], ubar, z)
    dn = dT_z(M[name], ubar, z)
    nur, nun = DH * Q / (K * dr), DH * Q / (K * dn)
    mean_err = (DH * Q / (K * np.average(dn, weights=wz))) / (DH * Q / (K * np.average(dr, weights=wz))) - 1.0
    loc = nun / nur - 1.0
    rms = float(np.sqrt(np.average(loc ** 2, weights=wz)))
    return 100 * mean_err, 100 * float(np.max(np.abs(loc))), 100 * rms


COMP = ["mesh", "lay3nom", "lay2", "lay3ach"]
print("=" * 104)
print("THREE METRICS.  mean = error in length-mean Nu.  worst = max |local error| over 0..L.")
print("                rms  = length-weighted RMS local error.  Local error is what a hot spot sees.")
print("=" * 104)
print("%6s %9s | %-23s | %-23s | %-23s | %-23s" % ("Re", "x*_exit", "mesh (no layers)", "lay3 nominal", "lay2", "lay3 ACHIEVED"))
print("%6s %9s | %7s %7s %7s | %7s %7s %7s | %7s %7s %7s | %7s %7s %7s"
      % ("", "", "mean", "worst", "rms", "mean", "worst", "rms", "mean", "worst", "rms", "mean", "worst", "rms"))
print("-" * 104)
for Re in (10, 30, 100, 153, 226, 300, 600, 1000, 2000):
    ub = Re * MU / (RHO * DH)
    line = "%6d %9.4f |" % (Re, L / (DH * Re * PR))
    for c in COMP:
        a, b_, r = metrics(c, ub)
        line += " %6.2f%% %6.1f%% %6.2f%% |" % (a, b_, r)
    print(line)

print()
print("=" * 104)
print("CROSSOVER UNDER EACH METRIC, mesh vs lay3 ACHIEVED (the stack the real mesh has)")
print("=" * 104)
for mi, label in ((0, "mean Nu"), (1, "worst local"), (2, "rms local")):
    def gp(Re):
        ub = Re * MU / (RHO * DH)
        return abs(metrics("mesh", ub)[mi]) - abs(metrics("lay3ach", ub)[mi])
    lo, hi = 1.0, 1.0e6
    if gp(lo) * gp(hi) > 0:
        print("  %-12s NO crossover in [1, 1e6]; %s wins throughout"
              % (label, "layers" if gp(lo) > 0 else "no layers"))
    else:
        g0 = gp(lo)
        for _ in range(60):
            mid = np.sqrt(lo * hi)
            if gp(mid) * g0 > 0:
                lo = mid
            else:
                hi = mid
        rc = np.sqrt(lo * hi)
        print("  %-12s crossover at Re = %7.1f   (x* = %.4f)" % (label, rc, L / (DH * rc * PR)))
print()
print("  Above the crossover layers are more accurate; below it the plain mesh is.")
print("DONE")
