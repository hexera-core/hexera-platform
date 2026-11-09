// Which of the two chip bodies is actually bonded to the plate?
//
// Reads the decomposed BINARY chip mesh and reports where the chip_to_solid and chip_to_air
// faces sit. If every chip_to_solid face is at one body's surface, the other body has no
// conduction path in the delivered mesh, which is a solvability question and not a resolution
// one. Format is faceCompactList + vectorField, LSB label=32 scalar=64, so a patch's faces are
// two seeks away rather than a scan.
//
// g++ -O2 -o chipcontact chipcontact.cpp -lm

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <cstdint>
#include <string>
#include <vector>
#include <map>
#include <algorithm>

// Find, from `from`, the next ASCII integer that is followed by optional whitespace then '('.
// Returns the count and sets dataOff to the first byte after '('.
static long long listAt(FILE* fh, long long from, long long* dataOff) {
    char buf[4096];
    for (int attempt = 0; attempt < 4096; attempt++) {
        if (fseeko(fh, (off_t)from, SEEK_SET) != 0) return -1;
        size_t n = fread(buf, 1, sizeof(buf), fh);
        if (n == 0) return -1;
        for (size_t i = 0; i + 1 < n; i++) {
            if (buf[i] < '0' || buf[i] > '9') continue;
            if (i > 0 && (buf[i-1] >= '0' && buf[i-1] <= '9')) continue;
            size_t j = i;
            long long v = 0;
            while (j < n && buf[j] >= '0' && buf[j] <= '9') { v = v*10 + (buf[j]-'0'); j++; }
            size_t k = j;
            while (k < n && (buf[k]==' '||buf[k]=='\n'||buf[k]=='\r'||buf[k]=='\t')) k++;
            if (k < n && buf[k] == '(') { *dataOff = from + (long long)k + 1; return v; }
        }
        from += (long long)n - 16;
    }
    return -1;
}

static long long afterHeader(FILE* fh) {
    // the FoamFile block's closing brace, then the // * * * separator line
    char buf[8192];
    fseeko(fh, 0, SEEK_SET);
    size_t n = fread(buf, 1, sizeof(buf), fh);
    for (size_t i = 0; i < n; i++) if (buf[i] == '}') return (long long)i + 1;
    return 0;
}

struct Patch { std::string name; long long start, n; };

// Parse the ASCII boundary dict: name { ... nFaces N; startFace S; ... }
static std::vector<Patch> readBoundary(const std::string& path) {
    std::vector<Patch> out;
    FILE* fh = fopen(path.c_str(), "rb");
    if (!fh) return out;
    std::string txt;
    char b[65536];
    size_t n;
    while ((n = fread(b, 1, sizeof(b), fh)) > 0) txt.append(b, n);
    fclose(fh);
    size_t p = txt.find("// * * *");
    if (p == std::string::npos) p = 0;
    // walk tokens: an identifier followed by '{'
    while (true) {
        size_t br = txt.find('{', p);
        if (br == std::string::npos) break;
        // identifier immediately before
        size_t e = br;
        while (e > 0 && isspace((unsigned char)txt[e-1])) e--;
        size_t s = e;
        while (s > 0 && (isalnum((unsigned char)txt[s-1]) || txt[s-1]=='_')) s--;
        std::string name = txt.substr(s, e - s);
        size_t close = txt.find('}', br);
        if (close == std::string::npos) break;
        std::string body = txt.substr(br, close - br);
        long long nf = -1, sf = -1;
        size_t q = body.find("nFaces");
        if (q != std::string::npos) nf = atoll(body.c_str() + q + 6);
        q = body.find("startFace");
        if (q != std::string::npos) sf = atoll(body.c_str() + q + 9);
        if (!name.empty() && nf >= 0 && sf >= 0) {
            Patch pt; pt.name = name; pt.start = sf; pt.n = nf; out.push_back(pt);
        }
        p = close + 1;
    }
    return out;
}

int main(int argc, char** argv) {
    if (argc < 2) { fprintf(stderr, "usage: chipcontact <case dir> [region]\n"); return 2; }
    std::string base = argv[1];
    std::string region = (argc > 2) ? argv[2] : "chip";

    // accumulate per-patch extents and a y histogram at 10 um
    std::map<std::string, long long> cnt;
    std::map<std::string, double> ymin, ymax, xmin, xmax, zmin, zmax, area;
    std::map<std::string, std::map<int, long long> > yh;

    for (int pr = 0; pr < 256; pr++) {
        char d[1024];
        snprintf(d, sizeof(d), "%s/processor%d/constant/%s/polyMesh", base.c_str(), pr, region.c_str());
        std::string bpath = std::string(d) + "/boundary";
        FILE* t = fopen(bpath.c_str(), "rb");
        if (!t) { if (pr == 0) { fprintf(stderr, "no %s\n", bpath.c_str()); return 2; } break; }
        fclose(t);
        std::vector<Patch> pats = readBoundary(bpath);

        // points
        std::vector<double> pts;
        {
            FILE* fh = fopen((std::string(d) + "/points").c_str(), "rb");
            long long off;
            long long np = listAt(fh, afterHeader(fh), &off);
            pts.resize(3 * (size_t)np);
            fseeko(fh, (off_t)off, SEEK_SET);
            if (fread(pts.data(), sizeof(double), 3 * (size_t)np, fh) != 3 * (size_t)np) {
                fprintf(stderr, "short points read on proc %d\n", pr); return 2;
            }
            fclose(fh);
        }
        // faces, compact
        FILE* ff = fopen((std::string(d) + "/faces").c_str(), "rb");
        long long off1, nOff, off2, nV;
        nOff = listAt(ff, afterHeader(ff), &off1);
        nV = listAt(ff, off1 + 4 * nOff, &off2);
        if (nOff < 0 || nV < 0) { fprintf(stderr, "faces parse failed proc %d\n", pr); return 2; }

        for (size_t ip = 0; ip < pats.size(); ip++) {
            if (pats[ip].name.compare(0, 9, "processor") == 0) continue;
            const std::string& nm = pats[ip].name;
            // the offsets for faces [start, start+n]
            std::vector<int32_t> offs((size_t)pats[ip].n + 1);
            fseeko(ff, (off_t)(off1 + 4 * pats[ip].start), SEEK_SET);
            if (fread(offs.data(), 4, offs.size(), ff) != offs.size()) {
                fprintf(stderr, "short offs read\n"); return 2;
            }
            long long v0 = offs[0], v1 = offs[offs.size()-1];
            std::vector<int32_t> conn((size_t)(v1 - v0));
            fseeko(ff, (off_t)(off2 + 4 * v0), SEEK_SET);
            if (fread(conn.data(), 4, conn.size(), ff) != conn.size()) {
                fprintf(stderr, "short conn read\n"); return 2;
            }
            for (long long f = 0; f < pats[ip].n; f++) {
                int nv = offs[f+1] - offs[f];
                const int32_t* fv = &conn[(size_t)(offs[f] - v0)];
                double cx = 0, cy = 0, cz = 0;
                for (int k = 0; k < nv; k++) {
                    cx += pts[3*(size_t)fv[k]];
                    cy += pts[3*(size_t)fv[k]+1];
                    cz += pts[3*(size_t)fv[k]+2];
                }
                cx /= nv; cy /= nv; cz /= nv;
                // area by fan from the first vertex
                double ax = 0, ay = 0, az = 0;
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
                if (!cnt.count(nm)) {
                    ymin[nm]=1e300; ymax[nm]=-1e300; xmin[nm]=1e300; xmax[nm]=-1e300;
                    zmin[nm]=1e300; zmax[nm]=-1e300; area[nm]=0;
                }
                cnt[nm]++;
                area[nm] += sqrt(ax*ax+ay*ay+az*az);
                ymin[nm]=std::min(ymin[nm],cy); ymax[nm]=std::max(ymax[nm],cy);
                xmin[nm]=std::min(xmin[nm],cx); xmax[nm]=std::max(xmax[nm],cx);
                zmin[nm]=std::min(zmin[nm],cz); zmax[nm]=std::max(zmax[nm],cz);
                yh[nm][(int)llround(cy * 1e4)]++;    // 0.1 mm bins
            }
        }
        fclose(ff);
        fprintf(stderr, "proc %d done\n", pr);
    }

    printf("\n%-16s %10s %12s %22s %22s %22s\n",
           "patch", "faces", "area mm2", "y range (m)", "x range (m)", "z range (m)");
    for (std::map<std::string,long long>::iterator it = cnt.begin(); it != cnt.end(); ++it) {
        const std::string& nm = it->first;
        printf("%-16s %10lld %12.2f  [%+.5f %+.5f]  [%+.5f %+.5f]  [%+.5f %+.5f]\n",
               nm.c_str(), it->second, area[nm]*1e6,
               ymin[nm], ymax[nm], xmin[nm], xmax[nm], zmin[nm], zmax[nm]);
    }
    printf("\ny distribution at 0.1 mm, per patch\n");
    for (std::map<std::string, std::map<int,long long> >::iterator it = yh.begin();
         it != yh.end(); ++it) {
        printf("  %s\n", it->first.c_str());
        std::vector<std::pair<long long,int> > v;
        for (std::map<int,long long>::iterator j = it->second.begin(); j != it->second.end(); ++j)
            v.push_back(std::make_pair(j->second, j->first));
        std::sort(v.rbegin(), v.rend());
        for (size_t k = 0; k < v.size() && k < 10; k++)
            printf("     y = %+.4f m   %10lld  %6.2f%%\n",
                   v[k].second / 1e4, v[k].first, 100.0 * v[k].first / cnt[it->first]);
    }
    return 0;
}
