// Locate the 2,936 faces checkMesh put in meshQualityFaces on the layered coolant.
//
// reconstructParMesh did not carry the sets over, so they are recomputed here from the
// reconstructed ASCII polyMesh, using OpenFOAM's own algorithms:
//   primitiveMeshFaceCentresAndAreas, primitiveMeshCellCentresAndVols, checkFaceOrthogonality.
//
// Three gates, all printed. If they do not match the figures checkMesh printed, the output is
// not to be used:
//   total volume            5.28057e-05 m3
//   max non-orthogonality   76.6676 deg      average 7.85233
//   faces over 65 deg       2348             over 70 deg 2096
//
// Phase 1 parses faces once, computes face centre + area vector, writes them to a binary
// scratch file and accumulates the cell-centre estimate. Phases 2 and 3 stream that binary
// file, so points are only ever in memory during phase 1 and nothing is parsed twice.
//
// g++ -O2 -o badfaces badfaces.cpp -lm

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <cstdint>
#include <vector>
#include <string>
#include <algorithm>

static const double VSMALL = 1.0e-300;
static const double ROOTVSMALL = 1.0e-18;

// ---------------------------------------------------------------- char stream

struct Reader {
    FILE* fh = nullptr;
    std::vector<char> buf;
    size_t have = 0, pos = 0;
    bool eof = false;

    explicit Reader(const std::string& path) : buf(1 << 22) {
        fh = fopen(path.c_str(), "rb");
        if (!fh) { fprintf(stderr, "cannot open %s\n", path.c_str()); exit(2); }
    }
    ~Reader() { if (fh) fclose(fh); }

    inline int get() {
        if (pos >= have) {
            have = fread(buf.data(), 1, buf.size(), fh);
            pos = 0;
            if (have == 0) { eof = true; return -1; }
        }
        return (unsigned char)buf[pos++];
    }
    inline void unget() { if (pos > 0) pos--; }

    // Move past the FoamFile block's closing brace.
    void skipHeader() {
        int c;
        while ((c = get()) != -1) if (c == '}') return;
        fprintf(stderr, "no FoamFile header found\n");
        exit(2);
    }

    // Next integer, skipping whitespace, parentheses, commas and // comments.
    inline long long nextInt() {
        int c;
        for (;;) {
            c = get();
            if (c == -1) { fprintf(stderr, "eof reading int\n"); exit(2); }
            if ((c >= '0' && c <= '9') || c == '-') { unget(); break; }
            if (c == '/') {                       // // comment to end of line
                while ((c = get()) != -1 && c != '\n') {}
                continue;
            }
        }
        bool neg = false;
        c = get();
        if (c == '-') { neg = true; c = get(); }
        long long v = 0;
        while (c >= '0' && c <= '9') { v = v * 10 + (c - '0'); c = get(); }
        unget();
        return neg ? -v : v;
    }

    // Next double, skipping whitespace, parentheses and // comments.
    inline double nextDouble() {
        int c;
        for (;;) {
            c = get();
            if (c == -1) { fprintf(stderr, "eof reading double\n"); exit(2); }
            if ((c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.') { unget(); break; }
            if (c == '/') { while ((c = get()) != -1 && c != '\n') {} continue; }
        }
        // assemble the token then strtod it, so exponents and all spellings are exact
        char tok[64];
        int n = 0;
        for (;;) {
            c = get();
            if (c == -1) break;
            if ((c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.' ||
                c == 'e' || c == 'E') {
                if (n < 63) tok[n++] = (char)c;
            } else { unget(); break; }
        }
        tok[n] = 0;
        return strtod(tok, nullptr);
    }
};

static bool headerSays(const std::string& path, const char* needle) {
    FILE* f = fopen(path.c_str(), "rb");
    if (!f) return false;
    char h[4096];
    size_t n = fread(h, 1, sizeof(h) - 1, f);
    h[n] = 0;
    fclose(f);
    return strstr(h, needle) != nullptr;
}

// ---------------------------------------------------------------- geometry

struct V3 { double x, y, z; };

static inline V3 operator-(const V3& a, const V3& b) { return {a.x-b.x, a.y-b.y, a.z-b.z}; }
static inline V3 operator+(const V3& a, const V3& b) { return {a.x+b.x, a.y+b.y, a.z+b.z}; }
static inline V3 operator*(double s, const V3& a) { return {s*a.x, s*a.y, s*a.z}; }
static inline double dot(const V3& a, const V3& b) { return a.x*b.x + a.y*b.y + a.z*b.z; }
static inline V3 cross(const V3& a, const V3& b) {
    return {a.y*b.z - a.z*b.y, a.z*b.x - a.x*b.z, a.x*b.y - a.y*b.x};
}
static inline double mag(const V3& a) { return sqrt(dot(a, a)); }

// OpenFOAM primitiveMeshFaceCentresAndAreas, for one face.
static inline void faceCentreAndArea(const int32_t* fv, int nv, const double* pts,
                                     V3& fCtr, V3& fArea)
{
    if (nv == 3) {
        V3 a = {pts[3*fv[0]], pts[3*fv[0]+1], pts[3*fv[0]+2]};
        V3 b = {pts[3*fv[1]], pts[3*fv[1]+1], pts[3*fv[1]+2]};
        V3 c = {pts[3*fv[2]], pts[3*fv[2]+1], pts[3*fv[2]+2]};
        fCtr = (1.0/3.0) * (a + b + c);
        fArea = 0.5 * cross(b - a, c - a);
        return;
    }
    V3 sumN = {0, 0, 0}, sumAc = {0, 0, 0}, fCentre = {0, 0, 0};
    double sumA = 0.0;
    for (int i = 0; i < nv; i++) {
        const double* p = pts + 3*fv[i];
        fCentre.x += p[0]; fCentre.y += p[1]; fCentre.z += p[2];
    }
    fCentre = (1.0/double(nv)) * fCentre;
    for (int i = 0; i < nv; i++) {
        const double* p0 = pts + 3*fv[i];
        const double* p1 = pts + 3*fv[(i+1) % nv];
        V3 a = {p0[0], p0[1], p0[2]};
        V3 b = {p1[0], p1[1], p1[2]};
        V3 c = a + b + fCentre;
        V3 n = cross(b - a, fCentre - a);
        double A = mag(n);
        sumN = sumN + n;
        sumA += A;
        sumAc = sumAc + (A * c);
    }
    if (sumA < ROOTVSMALL) { fCtr = fCentre; fArea = {0, 0, 0}; }
    else { fCtr = (1.0/(3.0*sumA)) * sumAc; fArea = 0.5 * sumN; }
}

// ---------------------------------------------------------------- main

int main(int argc, char** argv) {
    std::string dir = (argc > 1) ? argv[1] : ".";
    std::string scratch = (argc > 2) ? argv[2] : "/tmp/facegeom.bin";
    std::string outcsv = (argc > 3) ? argv[3] : "badfaces.csv";

    // ---- points
    fprintf(stderr, "[1/5] points\n");
    long long nPts;
    std::vector<double> pts;
    {
        Reader r(dir + "/points");
        r.skipHeader();
        nPts = r.nextInt();
        pts.resize(3 * (size_t)nPts);
        for (long long i = 0; i < 3 * nPts; i++) pts[(size_t)i] = r.nextDouble();
    }
    fprintf(stderr, "      %lld points\n", nPts);

    // ---- owner / neighbour
    fprintf(stderr, "[2/5] owner, neighbour\n");
    std::vector<int32_t> own, nei;
    {
        Reader r(dir + "/owner");
        r.skipHeader();
        long long n = r.nextInt();
        own.resize((size_t)n);
        for (long long i = 0; i < n; i++) own[(size_t)i] = (int32_t)r.nextInt();
    }
    {
        Reader r(dir + "/neighbour");
        r.skipHeader();
        long long n = r.nextInt();
        nei.resize((size_t)n);
        for (long long i = 0; i < n; i++) nei[(size_t)i] = (int32_t)r.nextInt();
    }
    const long long nFaces = (long long)own.size();
    const long long nInternal = (long long)nei.size();
    long long nCells = 0;
    for (size_t i = 0; i < own.size(); i++) nCells = std::max(nCells, (long long)own[i] + 1);
    for (size_t i = 0; i < nei.size(); i++) nCells = std::max(nCells, (long long)nei[i] + 1);
    fprintf(stderr, "      %lld faces, %lld internal, %lld cells\n", nFaces, nInternal, nCells);

    // The owner header's own note says what these must be. Refuse to run on anything else.
    if (nPts != 112948037 || nCells != 102536327 ||
        nFaces != 317772267 || nInternal != 301149851) {
        fprintf(stderr, "PARSE GATE FAILED: expected 112948037 / 102536327 / 317772267 / 301149851\n");
        return 3;
    }
    fprintf(stderr, "      parse gate OK, matches the owner header note\n");

    // ---- phase 1: face geometry to disk, accumulate cell centre estimate
    fprintf(stderr, "[3/5] face geometry -> %s\n", scratch.c_str());
    std::vector<double> cEst(3 * (size_t)nCells, 0.0);
    std::vector<int32_t> nCellFaces((size_t)nCells, 0);
    {
        const bool compact = headerSays(dir + "/faces", "faceCompactList");
        fprintf(stderr, "      faces are %s\n", compact ? "faceCompactList" : "faceList");
        FILE* out = fopen(scratch.c_str(), "wb");
        if (!out) { fprintf(stderr, "cannot write %s\n", scratch.c_str()); return 2; }
        std::vector<double> obuf;
        obuf.reserve(6 * 65536);
        std::vector<int32_t> fv(64);

        auto emit = [&](long long facei, const V3& c, const V3& a) {
            obuf.push_back(c.x); obuf.push_back(c.y); obuf.push_back(c.z);
            obuf.push_back(a.x); obuf.push_back(a.y); obuf.push_back(a.z);
            int32_t o = own[(size_t)facei];
            cEst[3*(size_t)o] += c.x; cEst[3*(size_t)o+1] += c.y; cEst[3*(size_t)o+2] += c.z;
            nCellFaces[(size_t)o]++;
            if (facei < nInternal) {
                int32_t nb = nei[(size_t)facei];
                cEst[3*(size_t)nb] += c.x; cEst[3*(size_t)nb+1] += c.y; cEst[3*(size_t)nb+2] += c.z;
                nCellFaces[(size_t)nb]++;
            }
            if (obuf.size() >= 6 * 65536) {
                fwrite(obuf.data(), sizeof(double), obuf.size(), out);
                obuf.clear();
            }
        };

        if (!compact) {
            Reader r(dir + "/faces");
            r.skipHeader();
            long long n = r.nextInt();
            if (n != nFaces) { fprintf(stderr, "faces count %lld != owner %lld\n", n, nFaces); return 2; }
            for (long long f = 0; f < n; f++) {
                int nv = (int)r.nextInt();
                if (nv > (int)fv.size()) fv.resize(nv);
                for (int k = 0; k < nv; k++) fv[k] = (int32_t)r.nextInt();
                V3 c, a;
                faceCentreAndArea(fv.data(), nv, pts.data(), c, a);
                emit(f, c, a);
            }
        } else {
            // offsets list then flat vertex list
            Reader r(dir + "/faces");
            r.skipHeader();
            long long nOff = r.nextInt();
            std::vector<int64_t> offs((size_t)nOff);
            for (long long i = 0; i < nOff; i++) offs[(size_t)i] = r.nextInt();
            long long nv_total = r.nextInt();
            std::vector<int32_t> verts((size_t)nv_total);
            for (long long i = 0; i < nv_total; i++) verts[(size_t)i] = (int32_t)r.nextInt();
            if (nOff - 1 != nFaces) { fprintf(stderr, "compact offsets %lld vs faces %lld\n", nOff-1, nFaces); return 2; }
            for (long long f = 0; f < nFaces; f++) {
                int nv = (int)(offs[(size_t)f+1] - offs[(size_t)f]);
                V3 c, a;
                faceCentreAndArea(&verts[(size_t)offs[(size_t)f]], nv, pts.data(), c, a);
                emit(f, c, a);
            }
        }
        if (!obuf.empty()) fwrite(obuf.data(), sizeof(double), obuf.size(), out);
        fclose(out);
    }
    pts.clear(); pts.shrink_to_fit();
    for (long long c = 0; c < nCells; c++) {
        double k = 1.0 / double(nCellFaces[(size_t)c]);
        cEst[3*(size_t)c] *= k; cEst[3*(size_t)c+1] *= k; cEst[3*(size_t)c+2] *= k;
    }
    nCellFaces.clear(); nCellFaces.shrink_to_fit();

    // ---- phase 2: cell centres and volumes
    fprintf(stderr, "[4/5] cell centres and volumes\n");
    std::vector<double> cCtr(3 * (size_t)nCells, 0.0);
    std::vector<double> cVol((size_t)nCells, 0.0);
    {
        FILE* in = fopen(scratch.c_str(), "rb");
        std::vector<double> ib(6 * 65536);
        long long facei = 0;
        size_t got;
        while ((got = fread(ib.data(), sizeof(double), ib.size(), in)) > 0) {
            size_t nf = got / 6;
            for (size_t i = 0; i < nf; i++, facei++) {
                V3 fc = {ib[6*i], ib[6*i+1], ib[6*i+2]};
                V3 fa = {ib[6*i+3], ib[6*i+4], ib[6*i+5]};
                int32_t o = own[(size_t)facei];
                V3 ce = {cEst[3*(size_t)o], cEst[3*(size_t)o+1], cEst[3*(size_t)o+2]};
                double pyr3 = dot(fa, fc - ce);
                V3 pc = (0.75) * fc + (0.25) * ce;
                cCtr[3*(size_t)o]   += pyr3 * pc.x;
                cCtr[3*(size_t)o+1] += pyr3 * pc.y;
                cCtr[3*(size_t)o+2] += pyr3 * pc.z;
                cVol[(size_t)o] += pyr3;
                if (facei < nInternal) {
                    int32_t nb = nei[(size_t)facei];
                    V3 cn = {cEst[3*(size_t)nb], cEst[3*(size_t)nb+1], cEst[3*(size_t)nb+2]};
                    double p2 = dot(fa, cn - fc);
                    V3 pc2 = (0.75) * fc + (0.25) * cn;
                    cCtr[3*(size_t)nb]   += p2 * pc2.x;
                    cCtr[3*(size_t)nb+1] += p2 * pc2.y;
                    cCtr[3*(size_t)nb+2] += p2 * pc2.z;
                    cVol[(size_t)nb] += p2;
                }
            }
        }
        fclose(in);
        if (facei != nFaces) { fprintf(stderr, "phase2 saw %lld faces not %lld\n", facei, nFaces); return 2; }
    }
    double totVol = 0.0, minVol = 1e300, maxVol = -1e300;
    for (long long c = 0; c < nCells; c++) {
        double v = cVol[(size_t)c];
        if (fabs(v) > VSMALL) {
            cCtr[3*(size_t)c] /= v; cCtr[3*(size_t)c+1] /= v; cCtr[3*(size_t)c+2] /= v;
        } else {
            cCtr[3*(size_t)c] = cEst[3*(size_t)c];
            cCtr[3*(size_t)c+1] = cEst[3*(size_t)c+1];
            cCtr[3*(size_t)c+2] = cEst[3*(size_t)c+2];
        }
        double vv = v / 3.0;
        cVol[(size_t)c] = vv;
        totVol += vv;
        minVol = std::min(minVol, vv);
        maxVol = std::max(maxVol, vv);
    }
    cEst.clear(); cEst.shrink_to_fit();
    printf("GATE total volume   %.6e m3   (checkMesh 5.28057e-05)\n", totVol);
    printf("GATE min cell vol   %.6e        (checkMesh 4.72424e-16)\n", minVol);
    printf("GATE max cell vol   %.6e        (checkMesh 2.19804e-10)\n", maxVol);

    // ---- phase 3: non-orthogonality on internal faces
    fprintf(stderr, "[5/5] non-orthogonality\n");
    const double RAD = 180.0 / M_PI;
    double maxAng = 0.0, sumAng = 0.0;
    long long n65 = 0, n70 = 0, counted = 0;
    FILE* csv = fopen(outcsv.c_str(), "w");
    fprintf(csv, "face,angle_deg,x,y,z,own_vol,nei_vol,area_mag\n");
    {
        FILE* in = fopen(scratch.c_str(), "rb");
        std::vector<double> ib(6 * 65536);
        long long facei = 0;
        size_t got;
        while ((got = fread(ib.data(), sizeof(double), ib.size(), in)) > 0) {
            size_t nf = got / 6;
            for (size_t i = 0; i < nf; i++, facei++) {
                if (facei >= nInternal) continue;
                V3 fc = {ib[6*i], ib[6*i+1], ib[6*i+2]};
                V3 fa = {ib[6*i+3], ib[6*i+4], ib[6*i+5]};
                int32_t o = own[(size_t)facei], nb = nei[(size_t)facei];
                V3 d = {cCtr[3*(size_t)nb] - cCtr[3*(size_t)o],
                        cCtr[3*(size_t)nb+1] - cCtr[3*(size_t)o+1],
                        cCtr[3*(size_t)nb+2] - cCtr[3*(size_t)o+2]};
                double dDotS = dot(d, fa) / (mag(d) * mag(fa) + ROOTVSMALL);
                if (dDotS > 1.0) dDotS = 1.0;
                if (dDotS < -1.0) dDotS = -1.0;
                double ang = acos(dDotS) * RAD;
                sumAng += ang; counted++;
                if (ang > maxAng) maxAng = ang;
                if (ang > 70.0) n70++;
                if (ang > 65.0) {
                    n65++;
                    fprintf(csv, "%lld,%.4f,%.9g,%.9g,%.9g,%.6e,%.6e,%.6e\n",
                            facei, ang, fc.x, fc.y, fc.z,
                            cVol[(size_t)o], cVol[(size_t)nb], mag(fa));
                }
            }
        }
        fclose(in);
    }
    fclose(csv);
    printf("GATE max non-ortho  %.4f deg    (checkMesh 76.6676)\n", maxAng);
    printf("GATE avg non-ortho  %.5f deg    (checkMesh 7.85233)\n", sumAng / double(counted));
    printf("GATE faces > 65     %lld             (checkMesh 2348)\n", n65);
    printf("GATE faces > 70     %lld             (checkMesh 2096)\n", n70);
    printf("internal faces counted %lld\n", counted);
    printf("wrote %s\n", outcsv.c_str());
    return 0;
}
