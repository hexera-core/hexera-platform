"""
TWO OF MY SOLVERS DISAGREE. ESTABLISH WHICH ONE IS WRONG BEFORE REPORTING EITHER.

MESH_QUALITY_CBOX2B.md section 7 says the 5-cell channel is +2.18% in fully developed Nusselt and
the layered mesh -4.02%, a 6.2 point gap, and the whole "drop the boundary layers" recommendation
rests on that gap. The closed-form Graetz solver says +0.54% and -0.73%, a 1.27 point gap.

Both cannot be right. Three independent tests, each of which the correct solver must pass:

  A  GRID CONVERGENCE of Nusselt. A correct second-order FV discretisation must show error
     falling as 1/N^2 with an observed order converging to 2, and must extrapolate to 8.2353.
     If Nu(5) really is +2.18%, the sequence from N=5 upward has to pass through it.

  B  THE FLOW RATE, which the record states as EXACTLY +2/N^2 and measures as +8.0% at N=5.
     This shares all the machinery with the energy solve except the boundary condition, so if
     the flow result reproduces exactly and the energy one does not, the fault is isolated to
     the energy boundary condition.

  C  THE ONE-WALL-HEATED CASE. If the old solver heated one wall and left the other adiabatic,
     its target is Nu = 5.3846, not 8.2353. Computing both says whether the old number is a
     different problem rather than a different answer.

Also reported: the exact analytical wall-gradient argument the record leans on, checked term by
term, because if the discrete wall gradient really is exact at any resolution then the ONLY error
channel is the bulk temperature quadrature, and that is computable in closed form and must match.
"""
import numpy as np

CELL = 20.1263e-6
W = 5.0 * CELL
DH = 2.0 * W
K = 0.606
Q = 1.0e5
RHO, CP = 997.0, 4180.0


def geom(n):
    dy = np.full(n, W / n)
    f = np.concatenate([[0.0], np.cumsum(dy)])
    yc = 0.5 * (f[:-1] + f[1:])
    return dy, yc


def diff_matrix(dy, yc, coef=1.0):
    n = len(dy)
    D = np.zeros((n, n))
    for i in range(n - 1):
        w = coef / (yc[i + 1] - yc[i])
        D[i, i] += w; D[i + 1, i + 1] += w
        D[i, i + 1] -= w; D[i + 1, i] -= w
    return D


# ---------------------------------------------------------------- A: Nu grid convergence
def nu_developed(n):
    """fully developed Nu, constant flux both walls, cell-centred FV, one-sided wall gradient."""
    dy, yc = geom(n)
    D = diff_matrix(dy, yc, K)
    u1 = 6.0 * (yc / W) * (1.0 - yc / W)
    M1 = RHO * CP * u1 * dy
    b = np.zeros(n); b[0] += Q; b[-1] += Q
    alpha1 = b.sum() / M1.sum()
    psi = np.linalg.lstsq(D, b - alpha1 * M1, rcond=None)[0]
    Tw = psi[0] + Q * dy[0] / (2.0 * K)
    Tb = float(M1 @ psi) / M1.sum()
    return DH * Q / (K * (Tw - Tb))


EXACT_NU = 8.235294117647059          # 140/17 for constant flux, parallel plates, both walls
print("=" * 94)
print("A  GRID CONVERGENCE OF FULLY DEVELOPED NUSSELT.  exact = 140/17 = %.9f" % EXACT_NU)
print("=" * 94)
print("%6s %16s %12s %12s %10s" % ("N", "Nu", "error %", "err x N^2", "order"))
prev_e = None
prev_n = None
for n in (5, 10, 20, 40, 80, 160, 320, 640, 1280):
    nu = nu_developed(n)
    e = nu / EXACT_NU - 1.0
    order = "" if prev_e is None else "%10.4f" % (np.log(abs(prev_e / e)) / np.log(n / prev_n))
    print("%6d %16.9f %11.4f%% %12.5f %s" % (n, nu, 100.0 * e, e * n * n, order))
    prev_e, prev_n = e, n

print()
print("  If the observed order goes to 2 and err x N^2 goes to a constant, the discretisation is")
print("  a correct second-order scheme and Nu(5) is whatever it is. +2.18%% at N=5 would need")
print("  err x N^2 = 0.545, and +0.54%% needs 0.135.")

# ---------------------------------------------------------------- B: the flow rate claim
def flow_error(n):
    """
    Fully developed slot flow, same FV discretisation. Record claims the flow rate error is
    EXACTLY +2/N^2. mu d2u/dy2 = -dp/dx with u=0 at both walls, imposed as a one-sided gradient
    through the wall face in the usual FV way.
    """
    dy, yc = geom(n)
    D = diff_matrix(dy, yc, 1.0)
    # Dirichlet u=0 at both walls via the half-cell distance
    D[0, 0] += 1.0 / (dy[0] / 2.0)
    D[-1, -1] += 1.0 / (dy[-1] / 2.0)
    rhs = np.ones(n) * 1.0                       # -dp/dx / mu = 1, per unit volume
    u = np.linalg.solve(D / dy[:, None] * dy[:, None], rhs * dy)   # D u = rhs*dy
    qd = float(u @ dy)                           # flow rate per unit depth
    exact = W ** 3 / 12.0                        # integral of y(W-y)/2 = W^3/12
    return qd / exact - 1.0


print()
print("=" * 94)
print("B  THE FLOW RATE. record: error is EXACTLY +2/N^2, so +8.00%% at N=5.")
print("=" * 94)
print("%6s %14s %14s %14s" % ("N", "error %", "2/N^2 %", "ratio"))
for n in (5, 10, 20, 40, 80):
    e = flow_error(n)
    pred = 2.0 / (n * n)
    print("%6d %13.6f%% %13.6f%% %14.6f" % (n, 100.0 * e, 100.0 * pred, e / pred))

# ---------------------------------------------------------------- C: one wall heated
def nu_one_wall(n):
    """constant flux on wall 0, adiabatic on wall n-1. exact Nu = 140/26 = 5.3846."""
    dy, yc = geom(n)
    D = diff_matrix(dy, yc, K)
    u1 = 6.0 * (yc / W) * (1.0 - yc / W)
    M1 = RHO * CP * u1 * dy
    b = np.zeros(n); b[0] += Q
    alpha1 = b.sum() / M1.sum()
    psi = np.linalg.lstsq(D, b - alpha1 * M1, rcond=None)[0]
    Tw = psi[0] + Q * dy[0] / (2.0 * K)
    Tb = float(M1 @ psi) / M1.sum()
    return DH * Q / (K * (Tw - Tb))


print()
print("=" * 94)
print("C  ONE WALL HEATED, other adiabatic. exact Nu = 140/26 = %.6f" % (140.0 / 26.0))
print("=" * 94)
print("%6s %16s %12s" % ("N", "Nu", "error %"))
for n in (5, 10, 20, 40, 320):
    nu = nu_one_wall(n)
    print("%6d %16.9f %11.4f%%" % (n, nu, 100.0 * (nu / (140.0 / 26.0) - 1.0)))

# ------------------------------------------- the wall gradient claim, and where the error really is
print()
print("=" * 94)
print("WHERE THE ERROR ACTUALLY IS. Split Nu error into its two possible sources.")
print("=" * 94)
print("Nu = Dh q / (k (Tw - Tb)), so the only error channels are Tw and Tb.")
print("Compare each against the exact continuum profile sampled the same way.")
print()


def split_error(n):
    dy, yc = geom(n)
    D = diff_matrix(dy, yc, K)
    u1 = 6.0 * (yc / W) * (1.0 - yc / W)
    M1 = RHO * CP * u1 * dy
    b = np.zeros(n); b[0] += Q; b[-1] += Q
    alpha1 = b.sum() / M1.sum()
    psi = np.linalg.lstsq(D, b - alpha1 * M1, rcond=None)[0]
    # exact fully developed profile, constant flux both walls, normalised the same way
    # theta(eta) = (q W / k) * f(eta), with f from integrating twice; get it by fine solve
    dyf, ycf = geom(4000)
    Df = diff_matrix(dyf, ycf, K)
    u1f = 6.0 * (ycf / W) * (1.0 - ycf / W)
    M1f = RHO * CP * u1f * dyf
    bf = np.zeros(4000); bf[0] += Q; bf[-1] += Q
    af = bf.sum() / M1f.sum()
    psif = np.linalg.lstsq(Df, bf - af * M1f, rcond=None)[0]
    # shift both to zero bulk so they are comparable
    psi = psi - float(M1 @ psi) / M1.sum()
    psif = psif - float(M1f @ psif) / M1f.sum()
    Tw_n = psi[0] + Q * dy[0] / (2.0 * K)
    Tw_f = psif[0] + Q * dyf[0] / (2.0 * K)
    # what Tw would be if the coarse grid's own first cell value were replaced by the exact
    # cell average of the fine profile over that first cell
    m = int(round(4000 / n))
    cell1_exact = float(np.mean(psif[:m]))
    Tw_mixed = cell1_exact + Q * dy[0] / (2.0 * K)
    return Tw_n, Tw_f, Tw_mixed


for n in (5, 10, 20):
    Tw_n, Tw_f, Tw_mix = split_error(n)
    print("  N=%-4d Tw(discrete)=%10.6f   Tw(exact,one-sided same dy)=%10.6f   dev %+8.4f%%"
          % (n, Tw_n, Tw_mix, 100.0 * (Tw_n / Tw_mix - 1.0)))
print()
print("  The second column uses the EXACT cell-average in the first cell with the SAME one-sided")
print("  half-cell extrapolation. If column one equals column two, the discrete solution's first")
print("  cell value is exact and the wall-gradient argument holds; any Nu error is then the bulk")
print("  temperature quadrature and nothing else.")
print("DONE")
