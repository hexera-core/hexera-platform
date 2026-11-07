"""
DO LAYERS HELP OR HURT, AS A FUNCTION OF REYNOLDS NUMBER?

MESH_QUALITY_CBOX2B.md section 7 measured that the layers cost 6.2 points of Nusselt, then said
that holds only while the flow is thermally developed, and that above Re ~226 the thermal entrance
length exceeds the longest straight channel run so layers start earning their cost. The second
half was REASONING, not measurement: the solver behind the first half solves the fully developed
problem and cannot speak about a developing one. So solve the developing one.

This is the Graetz problem: fully developed parabolic velocity, developing temperature, constant
wall heat flux on both walls. Fully developed velocity with developing temperature is the right
model because the thermal entrance length is Pr times the hydrodynamic one and Pr is 6, so the
velocity is established long before the temperature.

Solved on the ACTUAL grids with the same cell-centred finite volume discretisation and the same
one-sided wall gradient OpenFOAM uses, so what comes out is the mesh's error, not a model's error.

SOLVED IN CLOSED FORM, not marched. The semi-discrete system is

    M dT/dz = -D T + b          M = diag(rho cp u_i dy_i),  D = D^T >= 0,  D 1 = 0

so with T = (alpha z 1 + psi) + sum_n c_n phi_n exp(-lambda_n z), where D phi = lambda M phi,
everything is exact in z and there is no streamwise discretisation error to argue about.

The sweep is then almost free, because of a scaling that is worth stating. M is proportional to
ubar, and alpha is proportional to 1/ubar, so alpha*M*1 and hence psi do not depend on ubar at all.
That is the statement that fully developed Nusselt is Reynolds independent, falling out of the
algebra. And lambda_n = lambda_n(ubar=1)/ubar while c_n g_n is invariant, so ONE eigendecomposition
per grid gives every Reynolds number.

Layer stack from the dict that actually ran: nSurfaceLayers 3, expansionRatio 1.5,
firstLayerThickness 4.63e-06 absolute, so 4.63 + 6.945 + 10.4175 = 21.9925 um per wall, inserted by
compressing the existing cells so the 5 originals survive thinner. The run ACHIEVED 2.36 layers and
1.67e-05 m overall, 75.9% of target, over all 25,685,941 faces, so lay2 and lay3 bracket reality.
"""
import numpy as np

CELL = 20.1263e-6
W    = 5.0 * CELL
DH   = 2.0 * W
L    = 12.8e-3

RHO, CP, K = 997.0, 4180.0, 0.606
PR = 6.0
MU = PR * K / CP
Q  = 1.0e5                      # Nu is linear in q, so its value cannot matter


def g_uniform(n):
    return np.full(n, W / n)


def g_layers(nlay, first=4.63e-6, ratio=1.5, ncore=5):
    t = [first * ratio ** i for i in range(nlay)]
    core = W - 2.0 * sum(t)
    if core <= 0:
        raise ValueError("layers do not fit in the channel")
    return np.array(t + [core / ncore] * ncore + t[::-1])


def g_layers_overall(overall, nlay, ratio=1.5, ncore=5):
    """nlay layers summing to `overall` per wall at the given ratio, to hit a measured thickness."""
    w = np.array([ratio ** i for i in range(nlay)])
    t = list(overall * w / w.sum())
    core = W - 2.0 * overall
    return np.array(t + [core / ncore] * ncore + t[::-1])


def build(dy):
    """assemble D (as positive semi-definite), M1 (ubar=1), and the wall-flux vector."""
    n = len(dy)
    f = np.concatenate([[0.0], np.cumsum(dy)])
    yc = 0.5 * (f[:-1] + f[1:])
    D = np.zeros((n, n))
    for i in range(n - 1):
        w = K / (yc[i + 1] - yc[i])
        D[i, i] += w; D[i + 1, i + 1] += w
        D[i, i + 1] -= w; D[i + 1, i] -= w
    u1 = 6.0 * (yc / W) * (1.0 - yc / W)          # ubar = 1
    M1 = RHO * CP * u1 * dy
    b = np.zeros(n); b[0] += Q; b[-1] += Q
    return yc, D, M1, b


def modes(dy):
    """
    Returns dT_fd (the fully developed wall-to-bulk difference) and the arrays
    (lam1, C) such that (Tw - Tb)(z) = dT_fd + sum_n C_n exp(-lam1_n z / ubar).
    """
    n = len(dy)
    yc, D, M1, b = build(dy)
    one = np.ones(n)
    sM = M1.sum()

    # fully developed part. alpha*M = alpha1*M1 is ubar independent, so psi is too.
    alpha1 = b.sum() / sM                          # = 2Q / (rho cp W) with ubar=1
    rhs = b - alpha1 * M1
    psi = np.linalg.lstsq(D, rhs, rcond=None)[0]   # D is singular; the constant offset cancels below

    def wb(v):                                      # the (Tw - Tb) functional
        return v[0] + Q * dy[0] / (2.0 * K) - float(M1 @ v) / sM

    def wb_nodirichlet(v):                          # same minus the wall-flux constant, for modes
        return v[0] - float(M1 @ v) / sM

    dT_fd = wb(psi)

    # generalised eigenproblem D phi = lam M1 phi, made symmetric by R = sqrt(M1)
    R = np.sqrt(M1)
    S = D / R[:, None] / R[None, :]
    S = 0.5 * (S + S.T)
    lam, V = np.linalg.eigh(S)
    PHI = V / R[:, None]                            # M1-orthonormal: PHI^T M1 PHI = I

    e0 = -psi                                       # uniform inlet T=0, so e(0) = -psi
    c = PHI.T @ (M1 * e0)
    g = np.array([wb_nodirichlet(PHI[:, k]) for k in range(n)])
    C = c * g
    keep = lam > 1e-12 * max(lam.max(), 1.0)        # drop the constant mode, which cannot matter
    return dT_fd, lam[keep], C[keep]


def mean_dT(dy_modes, ubar, Lz=L):
    """mean over 0..Lz of (Tw - Tb), in closed form."""
    dT_fd, lam1, C = dy_modes
    a = lam1 * Lz / ubar
    # (1 - exp(-a)) / a, stable for small a
    fac = np.where(a > 1e-8, (1.0 - np.exp(-np.minimum(a, 700.0))) / np.where(a > 1e-8, a, 1.0), 1.0)
    return dT_fd + float(C @ fac)


def dT_at(dy_modes, ubar, z):
    dT_fd, lam1, C = dy_modes
    return dT_fd + float(C @ np.exp(-np.minimum(lam1 * z / ubar, 700.0)))


GRIDS = {
    "ref (400)":     g_uniform(400),
    "mesh (5)":      g_uniform(5),
    "lay3 nominal":  g_layers(3),
    "lay2":          g_layers(2),
    "lay3 achieved": g_layers_overall(1.67e-5, 3),
}
MOD = {k: modes(v) for k, v in GRIDS.items()}
COMP = ["mesh (5)", "lay3 nominal", "lay2", "lay3 achieved"]

print("=" * 100)
print("GRIDS across the %.4f um channel (5 cells of %.4f um)" % (W * 1e6, CELL * 1e6))
print("=" * 100)
print("%-16s %5s %12s %12s %12s" % ("grid", "n", "first cell um", "total um", "layers/wall um"))
for k, v in GRIDS.items():
    nl = 3 if "lay3" in k else (2 if "lay2" in k else 0)
    lw = v[:nl].sum() * 1e6 if nl else 0.0
    print("%-16s %5d %13.4f %12.5f %12.4f" % (k, len(v), v[0] * 1e6, v.sum() * 1e6, lw))

print()
print("=" * 100)
print("CHECK 1  fully developed Nusselt. Analytical for constant flux, parallel plates = 8.2353")
print("         The record says the 5-cell mesh is +2.18% and the layered mesh -4.02%, from a")
print("         DIFFERENT solver, so agreement here is a real cross-check.")
print("=" * 100)
print("%-16s %14s %10s" % ("grid", "Nu developed", "error"))
for k in GRIDS:
    nu = DH * Q / (K * MOD[k][0])
    print("%-16s %14.5f %9.2f%%" % (k, nu, 100.0 * (nu / 8.2353 - 1.0)))

print()
print("=" * 100)
print("CHECK 2  the closed form against a brute-force march, mesh grid, Re=300, 4000 steps")
print("=" * 100)


def march_check(dy, ubar, nz=4000, Lz=L):
    n = len(dy)
    yc, D, M1, b = build(dy)
    M = M1 * ubar
    dz = Lz / nz
    A = np.diag(M / dz) + D
    Ainv = np.linalg.inv(A)
    T = np.zeros(n)
    acc = 0.0
    for _ in range(nz):
        T = Ainv @ (M / dz * T + b)
        acc += T[0] + Q * dy[0] / (2.0 * K) - float(M1 @ T) / M1.sum()
    return acc / nz


for k in ("mesh (5)", "lay3 nominal"):
    ub = 300.0 * MU / (RHO * DH)
    cf = mean_dT(MOD[k], ub)
    mc = march_check(GRIDS[k], ub)
    print("  %-16s closed form %.6f K   marched %.6f K   diff %+.3f%%"
          % (k, cf, mc, 100.0 * (mc / cf - 1.0)))
print("  (the march is first order in z, so a small positive diff is the march's error, not the")
print("   closed form's. It is here to prove the eigen expansion is not wrong, not to beat it.)")

print()
print("=" * 100)
print("THE SWEEP.  mean Nu over the %.1f mm straight run, which is what sets the chip temperature." % (L * 1e3))
print("            Nu_mean = Dh q / (k * mean_z(Tw - Tb)).  x*_exit = L/(Dh Re Pr).")
print("            x* >> 0.1 is developed, x* << 0.01 is developing over the whole length.")
print("=" * 100)
head = "%6s %9s %9s" % ("Re", "x*_exit", "ref Nu")
for c in COMP:
    head += " %15s" % c
print(head)
print("-" * 100)
rows = []
for Re in (10, 30, 100, 226, 300, 600, 1000, 2000, 5000):
    ub = Re * MU / (RHO * DH)
    xs = L / (DH * Re * PR)
    nu = {k: DH * Q / (K * mean_dT(MOD[k], ub)) for k in GRIDS}
    r = nu["ref (400)"]
    line = "%6d %9.4f %9.4f" % (Re, xs, r)
    errs = {}
    for c in COMP:
        e = 100.0 * (nu[c] / r - 1.0)
        errs[c] = e
        line += " %14.2f%%" % e
    print(line)
    rows.append((Re, xs, r, errs))

print()
print("=" * 100)
print("WHO WINS, by absolute error in mean Nusselt")
print("=" * 100)
print("%6s %11s %11s %11s %11s   %s" % ("Re", "|mesh|", "|lay3 nom|", "|lay2|", "|lay3 ach|", "best"))
print("-" * 100)
for Re, xs, r, e in rows:
    vals = [(abs(e["mesh (5)"]), "NO LAYERS"), (abs(e["lay3 nominal"]), "3 layers"),
            (abs(e["lay2"]), "2 layers"), (abs(e["lay3 achieved"]), "3 layers achieved")]
    print("%6d %10.2f%% %10.2f%% %10.2f%% %10.2f%%   %s"
          % (Re, vals[0][0], vals[1][0], vals[2][0], vals[3][0], min(vals)[1]))

print()
print("=" * 100)
print("IS THERE A CROSSOVER AT ALL?  bisect |mesh err| - |lay3 achieved err| over Re in [1, 1e6]")
print("=" * 100)


def gap(Re):
    ub = Re * MU / (RHO * DH)
    r = DH * Q / (K * mean_dT(MOD["ref (400)"], ub))
    m = DH * Q / (K * mean_dT(MOD["mesh (5)"], ub))
    l = DH * Q / (K * mean_dT(MOD["lay3 achieved"], ub))
    return abs(m / r - 1.0) - abs(l / r - 1.0)


lo, hi = 1.0, 1.0e6
glo, ghi = gap(lo), gap(hi)
print("  gap(Re=%g) = %+.6f     gap(Re=%g) = %+.6f" % (lo, glo, hi, ghi))
if glo * ghi > 0.0:
    print("  SAME SIGN at both ends: there is NO crossover anywhere in 1 <= Re <= 1e6.")
    print("  %s wins across the entire range." % ("LAYERS" if glo > 0 else "NO LAYERS"))
else:
    for _ in range(60):
        mid = np.sqrt(lo * hi)
        if gap(mid) * glo > 0.0:
            lo = mid
        else:
            hi = mid
    print("  crossover at Re = %.1f  (x* = %.4f there)"
          % (np.sqrt(lo * hi), L / (DH * np.sqrt(lo * hi) * PR)))

print()
print("=" * 100)
print("WHERE THE LAYER ERROR COMES FROM: local Nu at a few stations, Re=1000")
print("=" * 100)
ub = 1000.0 * MU / (RHO * DH)
print("%10s %10s %10s %10s %10s" % ("z mm", "ref", "mesh", "lay3 ach", "mesh-ref%"))
for zmm in (0.05, 0.2, 1.0, 3.0, 6.0, 12.8):
    z = zmm * 1e-3
    nr = DH * Q / (K * dT_at(MOD["ref (400)"], ub, z))
    nm = DH * Q / (K * dT_at(MOD["mesh (5)"], ub, z))
    nl = DH * Q / (K * dT_at(MOD["lay3 achieved"], ub, z))
    print("%10.2f %10.4f %10.4f %10.4f %9.2f%%" % (zmm, nr, nm, nl, 100.0 * (nm / nr - 1.0)))

print()
print("  Re is per channel on Dh = %.4f um.  L/Dh = %.1f.  Pr = %.1f." % (DH * 1e6, L / DH, PR))
print("DONE")
