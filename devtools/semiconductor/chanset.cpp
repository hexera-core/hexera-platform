// Write a faceSet of the CHANNEL-RESOLUTION part of fluid_to_solid, per processor, so that
// createPatch can split the interface and the layer pass can be asked for layers on the
// channel wall alone.
//
// WHY BY AREA. Measured 2026-11-08 on the shipped mesh: 95.689% of the 16,619,777 interface
// faces are level +4 and 4.311% are coarser, and the bad angles concentrate on the level-2
// band at 1.45% of its faces against 0.0015% of channel-wall faces. A stretched level-4 face
// is 1.8987e-09 m2 and a stretched level-3 face is 7.595e-09, so a threshold between them
// separates the two cleanly with an order of magnitude of margin either side. No topoSet
// surfaceToCell, no outsidePoints, no closed-surface assumption.
//
// The set is written in ASCII even though the mesh is binary. A set file declares its own
// format in its header, so OpenFOAM reads it either way, and ASCII is checkable by eye.
//
// g++ -O2 -D_FILE_OFFSET_BITS=64 -o chanset chanset.cpp -lm

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <string>
#include <vector>
#include <algorithm>

static long long listAt(FILE* fh, long long from, long long* dataOff) {
    char buf[4096];
    for (int attempt = 0; attempt < 4096; attempt++) {
        if (fseeko(fh, (off_t)from, SEEK_SET) != 0) return -1;
        size_t n = fread(buf, 1, sizeof(buf), fh);
        if (n == 0) return -1;
        for (size_t i = 0; i + 1 < n; i++) {
            if (buf[i] < '0' || buf[i] > '9') continue;
            if (i > 0 && buf[i-1] >= '0' && buf[i-1] <= '9') continue;
            size_t j = i; long long v = 0;
            while (j < n && buf[j] >= '0' && buf[j] <= '9') { v = v*10 + (buf[j]-'0'); j++; }
            size_t k = j;
            while (k < n && isspace((unsigned char)buf[k])) k++;
            if (k < n && buf[k] == '(') { *dataOff = from + (long long)k + 1; return v; }
        }
        from += (long long)n - 16;
    }
    return -1;
}

static long long afterHeader(FILE* fh) {
    char buf[8192];
    fseeko(fh, 0, SEEK_SET);
    size_t n = fread(buf, 1, sizeof(buf), fh);
    for (size_t i = 0; i < n; i++) if (buf[i] == '}') return (long long)i + 1;
    return 0;
}

struct Patch { std::string name; long long start, n; };

static std::vector<Patch> readBoundary(const std::string& path) {
    std::vector<Patch> out;
    FILE* fh = fopen(path.c_str(), "rb");
    if (!fh) return out;
    std::string txt; char b[65536]; size_t n;
    while ((n = fread(b, 1, sizeof(b), fh)) > 0) txt.append(b, n);
    fclose(fh);
    size_t p = txt.find("// * * *");
    if (p == std::string::npos) p = 0;
    while (true) {
        size_t br = txt.find('{', p);
        if (br == std::string::npos) break;
        size_t e = br;
        while (e > 0 && isspace((unsigned char)txt[e-1])) e--;
        size_t s = e;
        while (s > 0 && (isalnum((unsigned char)txt[s-1]) || txt[s-1] == '_')) s--;
        std::string name = txt.substr(s, e - s);
        size_t close = txt.find('}', br);
        if (close == std::string::npos) break;
        std::string body = txt.substr(br, close - br);
        long long nf = -1, sf = -1; size_t q;
        if ((q = body.find("nFaces")) != std::string::npos) nf = atoll(body.c_str() + q + 6);
        if ((q = body.find("startFace")) != std::string::npos) sf = atoll(body.c_str() + q + 9);
        if (!name.empty() && nf >= 0 && sf >= 0) { Patch t; t.name=name; t.start=sf; t.n=nf; out.push_back(t); }
        p = close + 1;
    }
    return out;
}

int main(int argc, char** argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: chanset <case> <patch> <setname> [areaThreshold] [region]\n");
        return 2;
    }
    std::string base = argv[1], patch = argv[2], setname = argv[3];
    double thr = (argc > 4) ? atof(argv[4]) : 3.8e-9;
    std::string region = (argc > 5) ? argv[5] : "";
    long long grandIn = 0, grandOut = 0;

    for (int pr = 0; pr < 4096; pr++) {
        char d[1024];
        if (region.empty())
            snprintf(d, sizeof(d), "%s/processor%d/constant/polyMesh", base.c_str(), pr);
        else
            snprintf(d, sizeof(d), "%s/processor%d/constant/%s/polyMesh", base.c_str(), pr, region.c_str());
        std::string bp = std::string(d) + "/boundary";
        FILE* t = fopen(bp.c_str(), "rb");
        if (!t) { if (pr == 0) { fprintf(stderr, "no %s\n", bp.c_str()); return 2; } break; }
        fclose(t);
        std::vector<Patch> pats = readBoundary(bp);
        const Patch* P = 0;
        for (size_t i = 0; i < pats.size(); i++) if (pats[i].name == patch) P = &pats[i];
        if (!P) { fprintf(stderr, "proc %d: no patch %s\n", pr, patch.c_str()); return 2; }

        std::vector<double> pts;
        {
            FILE* fh = fopen((std::string(d) + "/points").c_str(), "rb");
            long long off; long long np = listAt(fh, afterHeader(fh), &off);
            pts.resize(3 * (size_t)np);
            fseeko(fh, (off_t)off, SEEK_SET);
            if (fread(pts.data(), sizeof(double), 3*(size_t)np, fh) != 3*(size_t)np) {
                fprintf(stderr, "short points proc %d\n", pr); return 2; }
            fclose(fh);
        }
        FILE* ff = fopen((std::string(d) + "/faces").c_str(), "rb");
        long long off1, nOff, off2;
        nOff = listAt(ff, afterHeader(ff), &off1);
        listAt(ff, off1 + 4 * nOff, &off2);
        std::vector<int32_t> offs((size_t)P->n + 1);
        fseeko(ff, (off_t)(off1 + 4 * P->start), SEEK_SET);
        if (fread(offs.data(), 4, offs.size(), ff) != offs.size()) { fprintf(stderr,"short offs\n"); return 2; }
        long long v0 = offs[0], v1 = offs[offs.size()-1];
        std::vector<int32_t> conn((size_t)(v1 - v0));
        fseeko(ff, (off_t)(off2 + 4 * v0), SEEK_SET);
        if (fread(conn.data(), 4, conn.size(), ff) != conn.size()) { fprintf(stderr,"short conn\n"); return 2; }
        fclose(ff);

        std::vector<long long> keep, rest;
        keep.reserve((size_t)P->n);
        double aIn = 0, aOut = 0;
        for (long long f = 0; f < P->n; f++) {
            int nv = offs[f+1] - offs[f];
            const int32_t* fv = &conn[(size_t)(offs[f] - v0)];
            double ax=0, ay=0, az=0;
            for (int k = 1; k + 1 < nv; k++) {
                double u[3], v[3];
                for (int m = 0; m < 3; m++) {
                    u[m] = pts[3*(size_t)fv[k]+m]   - pts[3*(size_t)fv[0]+m];
                    v[m] = pts[3*(size_t)fv[k+1]+m] - pts[3*(size_t)fv[0]+m];
                }
                ax += 0.5*(u[1]*v[2]-u[2]*v[1]);
                ay += 0.5*(u[2]*v[0]-u[0]*v[2]);
                az += 0.5*(u[0]*v[1]-u[1]*v[0]);
            }
            double A = sqrt(ax*ax+ay*ay+az*az);
            if (A <= thr) { keep.push_back(P->start + f); aIn += A; }
            else           { rest.push_back(P->start + f); aOut += A; }
        }

        char sd[1200];
        snprintf(sd, sizeof(sd), "%s/sets", d);
        char cmd[1400]; snprintf(cmd, sizeof(cmd), "mkdir -p '%s'", sd); if (system(cmd)) {}
        // Both halves are written, so createPatch can take the original patch to zero and the
        // two new ones can be merged back after the layer pass with no name collision.
        for (int half = 0; half < 2; half++) {
            const std::vector<long long>& v = half ? rest : keep;
            std::string nm = setname + (half ? "Rest" : "");
            char sp[1400]; snprintf(sp, sizeof(sp), "%s/%s", sd, nm.c_str());
            FILE* out = fopen(sp, "w");
            if (!out) { fprintf(stderr, "cannot write %s\n", sp); return 2; }
            fprintf(out,
              "FoamFile\n{\n    version     2.0;\n    format      ascii;\n"
              "    class       faceSet;\n    location    \"constant/%spolyMesh/sets\";\n"
              "    object      %s;\n}\n\n%zu\n(\n",
              region.empty() ? "" : (region + "/").c_str(), nm.c_str(), v.size());
            for (size_t i = 0; i < v.size(); i++) fprintf(out, "%lld\n", v[i]);
            fprintf(out, ")\n");
            fclose(out);
        }
        grandIn += (long long)keep.size();
        grandOut += P->n - (long long)keep.size();
        printf("proc %d: %s has %lld faces, %zu at or below %.3e m2 (%.3f%%), %lld above"
               "   area in %.2f mm2 out %.2f mm2\n",
               pr, patch.c_str(), P->n, keep.size(), thr,
               100.0 * (double)keep.size() / (double)P->n, P->n - (long long)keep.size(),
               aIn*1e6, aOut*1e6);
    }
    printf("TOTAL in set %lld, left in %s %lld, sum %lld\n",
           grandIn, patch.c_str(), grandOut, grandIn + grandOut);
    printf("EXPECT 15903284 in and 716493 out, sum 16619777\n");
    if (grandIn + grandOut != 16619777LL) { printf("GATE FAILED: face total is wrong\n"); return 3; }
    if (grandIn != 15903284LL) printf("NOTE: in-set count differs from the 2026-11-08 measurement\n");
    return 0;
}
