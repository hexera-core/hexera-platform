// How much of fluid_to_solid is channel-resolution wall and how much is coarse plenum wall.
// Reads the face centre + area vector pairs that badfaces.cpp already wrote, for the boundary
// faces only. Boundary faces are [nInternalFaces, nFaces) and real patches come first and
// contiguously, so fluid_to_solid is the first 16,619,777 of them.
//
// g++ -O3 -o patcharea patcharea.cpp -lm

#include <cstdio>
#include <cstdint>
#include <cmath>
#include <vector>
#include <map>

static const long long N_INTERNAL = 301149851LL;
static const long long N_F2S      = 16619777LL;
static const double P = 25.1578e-6;                 // level +4

int main(int argc, char** argv) {
    const char* path = (argc > 1) ? argv[1] : "/home/rehaan/qa/facegeom.bin";
    FILE* fh = fopen(path, "rb");
    if (!fh) { fprintf(stderr, "cannot open %s\n", path); return 2; }
    if (fseeko(fh, (off_t)(N_INTERNAL * 6 * (long long)sizeof(double)), SEEK_SET) != 0) {
        fprintf(stderr, "seek failed\n"); return 2;
    }
    // bucket by the implied in-plane cell size, assuming the y side is 3x the x/z side
    std::map<int, long long> lev;
    std::map<int, double> lev_area;
    std::vector<double> ib(6 * 65536);
    long long seen = 0;
    double tot = 0.0;
    while (seen < N_F2S) {
        size_t want = (size_t)std::min<long long>((long long)ib.size() / 6, N_F2S - seen);
        size_t got = fread(ib.data(), sizeof(double), want * 6, fh);
        if (got == 0) break;
        size_t nf = got / 6;
        for (size_t i = 0; i < nf; i++) {
            double ax = ib[6*i+3], ay = ib[6*i+4], az = ib[6*i+5];
            double A = sqrt(ax*ax + ay*ay + az*az);
            tot += A;
            // a face of a level-L cell, stretched 3x in y, has area either
            //   s*3s (x- or z-normal)  or  s*s (y-normal), with s = P * 2^(4-L)
            // classify by the smaller candidate side so both orientations land together
            double s = sqrt(A / 3.0);
            double s2 = sqrt(A);
            double cand = (fabs(ay) > fabs(ax) && fabs(ay) > fabs(az)) ? s2 : s;
            int L = (int)llround(4.0 - log2(cand / P));
            lev[L] += 1;
            lev_area[L] += A;
        }
        seen += (long long)nf;
    }
    fclose(fh);
    printf("fluid_to_solid faces read: %lld of %lld\n", seen, N_F2S);
    printf("total area: %.6e m2 = %.2f mm2\n\n", tot, tot * 1e6);
    printf("%6s %8s %14s %8s %14s %8s\n",
           "level", "cell um", "faces", "share", "area mm2", "share");
    for (std::map<int, long long>::iterator it = lev.begin(); it != lev.end(); ++it) {
        int L = it->first;
        double cell = P * pow(2.0, 4 - L) * 1e6;
        printf("%6d %8.3f %14lld %7.3f%% %14.2f %7.3f%%\n",
               L, cell, it->second, 100.0 * it->second / seen,
               lev_area[L] * 1e6, 100.0 * lev_area[L] / tot);
    }
    long long fine = 0; double fineA = 0;
    for (std::map<int, long long>::iterator it = lev.begin(); it != lev.end(); ++it)
        if (it->first >= 4) { fine += it->second; fineA += lev_area[it->first]; }
    printf("\nlevel 4 and finer: %lld faces (%.3f%%), %.2f mm2 (%.3f%%)\n",
           fine, 100.0 * fine / seen, fineA * 1e6, 100.0 * fineA / tot);
    printf("coarser than level 4: %lld faces (%.3f%%)\n",
           seen - fine, 100.0 * (seen - fine) / seen);
    printf("\nif layers go on level-4 wall only, extruded faces fall from\n");
    printf("  15,880,692 to about %lld, a factor of %.2f\n",
           (long long)(fine * (15880692.0 / (double)seen)),
           (double)seen / (double)fine);
    printf("  and the 47,232,886 layer cells to about %lld\n",
           (long long)(47232886.0 * fine / (double)seen));
    return 0;
}
