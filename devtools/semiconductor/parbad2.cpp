// Localise checkMesh's faces-in-error on a DECOMPOSED mesh, ASCII or binary, no reconstruct.
//
// Covers non-orthogonality AND interpolation weight, which is the gap in the 2026-11-08 work:
// there I localised the 2,349 bad angles and then ASSUMED the 405 interpolation-weight faces
// were the same faces without measuring it.
//
// WHY PER-PROCESSOR IS EXACT, NOT AN APPROXIMATION. A decomposed mesh is geometrically complete
// on each processor: every local cell has all of its faces present locally, because a face
// shared with another processor is carried on that processor patch. So local cell centres and
// volumes are exact and no halo exchange is needed. What is not available locally is the cell
// centre on the far side of a processor face, so those faces are skipped and counted, and the
// count is printed. checkMesh evaluates them through coupled patches, so expect to land at or
// just below its totals.
//
// GATES. Pass the expected cell count, internal-face count and the two criterion counts and it
// will say whether it reproduced them. Do not quote its numbers until the gates pass on the mesh
// in hand: a previous version of this reader agreed with checkMesh on every count on the chip
// region while reporting a non-orthogonality maximum of 0.0803 deg against checkMesh's
// 0.00241416, so agreement on counts does not by itself prove the geometry is right.
//
// g++ -O2 -D_FILE_OFFSET_BITS=64 -o parbad2 parbad2.cpp -lm
//
// usage: parbad2 <case> <region|-> <out.csv> [expCells] [expInternal] [exp65] [expWeight]

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <string>
#include <vector>
#include <algorithm>

static const double ROOTVSMALL = 1.0e-18;

// ------------------------------------------------------------------ format

static bool fileIsBinary(const std::string& p) {
    FILE* f = fopen(p.c_str(), "rb"); if (!f) return false;
    char h[3072]; size_t n = fread(h, 1, sizeof(h)-1, f); h[n] = 0; fclose(f);
    char* b = strstr(h, "format");
    if (!b) return false;
    char* e = strchr(b, ';');
    if (!e) return false;
    *e = 0;
    bool bin = strstr(b, "binary") != 0;
    return bin;
}

// ------------------------------------------------------------------ binary

static long long binListAt(FILE* fh, long long from, long long* dataOff) {
    char buf[4096];
    for (int a = 0; a < 4096; a++) {
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
static long long headerEnd(FILE* fh) {
    char b[8192]; fseeko(fh, 0, SEEK_SET);
    size_t n = fread(b, 1, sizeof(b), fh);
    for (size_t i = 0; i < n; i++) if (b[i] == '}') return (long long)i + 1;
    return 0;
}

// ------------------------------------------------------------------ ascii

struct Tok {
    FILE* fh; std::vector<char> buf; size_t have, pos;
    explicit Tok(const std::string& p) : buf(1<<22), have(0), pos(0) {
        fh = fopen(p.c_str(), "rb");
        if (!fh) { fprintf(stderr, "cannot open %s\n", p.c_str()); exit(2); }
    }
    ~Tok(){ if (fh) fclose(fh); }
    inline int get(){
        if (pos >= have) { have = fread(buf.data(), 1, buf.size(), fh); pos = 0; if (!have) return -1; }
        return (unsigned char)buf[pos++];
    }
    inline void unget(){ if (pos) pos--; }
    void skipHeader(){ int c; while ((c = get()) != -1) if (c == '}') return; }
    inline long long nextInt(){
        int c;
        for (;;) {
            c = get();
            if (c == -1) { fprintf(stderr, "eof reading int\n"); exit(2); }
            if ((c >= '0' && c <= '9') || c == '-') { unget(); break; }
            if (c == '/') { while ((c = get()) != -1 && c != '\n') {} }
        }
        bool neg = false; c = get();
        if (c == '-') { neg = true; c = get(); }
        long long v = 0;
        while (c >= '0' && c <= '9') { v = v*10 + (c - '0'); c = get(); }
        unget();
        return neg ? -v : v;
    }
    inline double nextDouble(){
        int c;
        for (;;) {
            c = get();
            if (c == -1) { fprintf(stderr, "eof reading double\n"); exit(2); }
            if ((c>='0'&&c<='9')||c=='-'||c=='+'||c=='.') { unget(); break; }
            if (c == '/') { while ((c = get()) != -1 && c != '\n') {} }
        }
        char t[64]; int n = 0;
        for (;;) {
            c = get();
            if (c == -1) break;
            if ((c>='0'&&c<='9')||c=='-'||c=='+'||c=='.'||c=='e'||c=='E') { if (n<63) t[n++]=(char)c; }
            else { unget(); break; }
        }
        t[n] = 0;
        return strtod(t, 0);
    }
};

// ------------------------------------------------------------------ loaders

static void loadPoints(const std::string& p, std::vector<double>& out) {
    if (fileIsBinary(p)) {
        FILE* fh = fopen(p.c_str(), "rb"); long long off;
        long long n = binListAt(fh, headerEnd(fh), &off);
        out.resize(3*(size_t)n);
        fseeko(fh, (off_t)off, SEEK_SET);
        if (fread(out.data(), sizeof(double), 3*(size_t)n, fh) != 3*(size_t)n) { fprintf(stderr,"short points\n"); exit(2); }
        fclose(fh);
    } else {
        Tok r(p); r.skipHeader();
        long long n = r.nextInt();
        out.resize(3*(size_t)n);
        for (long long i = 0; i < 3*n; i++) out[(size_t)i] = r.nextDouble();
    }
}
static void loadLabels(const std::string& p, std::vector<int32_t>& out) {
    if (fileIsBinary(p)) {
        FILE* fh = fopen(p.c_str(), "rb"); long long off;
        long long n = binListAt(fh, headerEnd(fh), &off);
        out.resize((size_t)n);
        fseeko(fh, (off_t)off, SEEK_SET);
        if (fread(out.data(), 4, (size_t)n, fh) != (size_t)n) { fprintf(stderr,"short labels\n"); exit(2); }
        fclose(fh);
    } else {
        Tok r(p); r.skipHeader();
        long long n = r.nextInt();
        out.resize((size_t)n);
        for (long long i = 0; i < n; i++) out[(size_t)i] = (int32_t)r.nextInt();
    }
}
// returns offsets (nFaces+1) and the flat vertex list
static void loadFaces(const std::string& p, long long nFaces,
                      std::vector<int64_t>& offs, std::vector<int32_t>& conn) {
    FILE* t = fopen(p.c_str(), "rb");
    char h[3072]; size_t hn = fread(h, 1, sizeof(h)-1, t); h[hn] = 0; fclose(t);
    bool compact = strstr(h, "faceCompactList") != 0;
    if (fileIsBinary(p)) {
        if (!compact) { fprintf(stderr, "binary faceList is not handled, only faceCompactList\n"); exit(2); }
        FILE* fh = fopen(p.c_str(), "rb");
        long long off1, off2;
        long long nOff = binListAt(fh, headerEnd(fh), &off1);
        binListAt(fh, off1 + 4*nOff, &off2);
        std::vector<int32_t> o32((size_t)nOff);
        fseeko(fh, (off_t)off1, SEEK_SET);
        if (fread(o32.data(), 4, o32.size(), fh) != o32.size()) { fprintf(stderr,"short offs\n"); exit(2); }
        offs.resize((size_t)nOff);
        for (long long i = 0; i < nOff; i++) offs[(size_t)i] = o32[(size_t)i];
        conn.resize((size_t)offs[nOff-1]);
        fseeko(fh, (off_t)off2, SEEK_SET);
        if (fread(conn.data(), 4, conn.size(), fh) != conn.size()) { fprintf(stderr,"short conn\n"); exit(2); }
        fclose(fh);
    } else {
        Tok r(p); r.skipHeader();
        if (compact) {
            long long nOff = r.nextInt();
            offs.resize((size_t)nOff);
            for (long long i = 0; i < nOff; i++) offs[(size_t)i] = r.nextInt();
            long long nv = r.nextInt();
            conn.resize((size_t)nv);
            for (long long i = 0; i < nv; i++) conn[(size_t)i] = (int32_t)r.nextInt();
        } else {
            long long n = r.nextInt();
            if (n != nFaces) { fprintf(stderr, "faces %lld != owner %lld\n", n, nFaces); exit(2); }
            offs.resize((size_t)n + 1);
            conn.clear(); conn.reserve((size_t)n * 4);
            offs[0] = 0;
            for (long long f = 0; f < n; f++) {
                int nv = (int)r.nextInt();
                for (int k = 0; k < nv; k++) conn.push_back((int32_t)r.nextInt());
                offs[(size_t)f+1] = (int64_t)conn.size();
            }
        }
    }
}

// ------------------------------------------------------------------ geometry

struct V3 { double x, y, z; };
static inline V3 sub(const V3&a,const V3&b){ return {a.x-b.x,a.y-b.y,a.z-b.z}; }
static inline double dot(const V3&a,const V3&b){ return a.x*b.x+a.y*b.y+a.z*b.z; }
static inline V3 crs(const V3&a,const V3&b){ return {a.y*b.z-a.z*b.y,a.z*b.x-a.x*b.z,a.x*b.y-a.y*b.x}; }
static inline double mg(const V3&a){ return sqrt(dot(a,a)); }

static void faceGeom(const int32_t* fv, int nv, const double* p, V3& c, V3& A) {
    if (nv == 3) {
        V3 a={p[3*fv[0]],p[3*fv[0]+1],p[3*fv[0]+2]};
        V3 b={p[3*fv[1]],p[3*fv[1]+1],p[3*fv[1]+2]};
        V3 d={p[3*fv[2]],p[3*fv[2]+1],p[3*fv[2]+2]};
        c={(a.x+b.x+d.x)/3.0,(a.y+b.y+d.y)/3.0,(a.z+b.z+d.z)/3.0};
        V3 n=crs(sub(b,a),sub(d,a)); A={0.5*n.x,0.5*n.y,0.5*n.z}; return;
    }
    V3 fc={0,0,0}, sN={0,0,0}, sAc={0,0,0}; double sA=0;
    for (int i=0;i<nv;i++){ const double* q=p+3*fv[i]; fc.x+=q[0]; fc.y+=q[1]; fc.z+=q[2]; }
    fc.x/=nv; fc.y/=nv; fc.z/=nv;
    for (int i=0;i<nv;i++){
        const double* q0=p+3*fv[i]; const double* q1=p+3*fv[(i+1)%nv];
        V3 a={q0[0],q0[1],q0[2]}, b={q1[0],q1[1],q1[2]};
        V3 cc={a.x+b.x+fc.x,a.y+b.y+fc.y,a.z+b.z+fc.z};
        V3 n=crs(sub(b,a),sub(fc,a)); double Am=mg(n);
        sN.x+=n.x; sN.y+=n.y; sN.z+=n.z; sA+=Am;
        sAc.x+=Am*cc.x; sAc.y+=Am*cc.y; sAc.z+=Am*cc.z;
    }
    if (sA < ROOTVSMALL) { c=fc; A={0,0,0}; }
    else { c={sAc.x/(3*sA),sAc.y/(3*sA),sAc.z/(3*sA)}; A={0.5*sN.x,0.5*sN.y,0.5*sN.z}; }
}

// ------------------------------------------------------------------ main

int main(int argc, char** argv) {
    if (argc < 4) { fprintf(stderr,"usage: parbad2 <case> <region|-> <out.csv> [expCells expInternal exp65 expWeight]\n"); return 2; }
    std::string base = argv[1];
    std::string region = (strcmp(argv[2],"-")==0) ? "" : argv[2];
    std::string outp = argv[3];
    long long eC = (argc>4)?atoll(argv[4]):-1, eI = (argc>5)?atoll(argv[5]):-1;
    long long e65 = (argc>6)?atoll(argv[6]):-1, eW = (argc>7)?atoll(argv[7]):-1;

    FILE* csv = fopen(outp.c_str(), "w");
    fprintf(csv, "proc,face,kind,angle_deg,weight,x,y,z,own_vol,nei_vol,area\n");
    long long g65=0, g70=0, gw=0, gskip=0, gint=0, gcells=0;
    double gmax=0, gsumcos=0, gvol=0; long long gn=0;

    for (int pr = 0; pr < 4096; pr++) {
        char d[1024];
        if (region.empty()) snprintf(d,sizeof(d),"%s/processor%d/constant/polyMesh",base.c_str(),pr);
        else snprintf(d,sizeof(d),"%s/processor%d/constant/%s/polyMesh",base.c_str(),pr,region.c_str());
        std::string pp = std::string(d)+"/points";
        FILE* t = fopen(pp.c_str(),"rb");
        if (!t) { if (pr==0){ fprintf(stderr,"no %s\n",pp.c_str()); return 2;} break; }
        fclose(t);

        std::vector<double> pts;  loadPoints(pp, pts);
        std::vector<int32_t> own, nei;
        loadLabels(std::string(d)+"/owner", own);
        loadLabels(std::string(d)+"/neighbour", nei);
        const long long nF=(long long)own.size(), nI=(long long)nei.size();
        std::vector<int64_t> offs; std::vector<int32_t> conn;
        loadFaces(std::string(d)+"/faces", nF, offs, conn);
        long long nC=0;
        for (size_t i=0;i<own.size();i++) nC=std::max(nC,(long long)own[i]+1);
        for (size_t i=0;i<nei.size();i++) nC=std::max(nC,(long long)nei[i]+1);

        std::vector<V3> fC((size_t)nF), fA((size_t)nF);
        for (long long f=0;f<nF;f++)
            faceGeom(&conn[(size_t)offs[(size_t)f]], (int)(offs[(size_t)f+1]-offs[(size_t)f]),
                     pts.data(), fC[(size_t)f], fA[(size_t)f]);

        std::vector<V3> cE((size_t)nC, V3{0,0,0});
        std::vector<int32_t> cnt((size_t)nC, 0);
        for (long long f=0;f<nF;f++){
            int32_t o=own[(size_t)f];
            cE[o].x+=fC[f].x; cE[o].y+=fC[f].y; cE[o].z+=fC[f].z; cnt[o]++;
            if (f<nI){ int32_t b=nei[(size_t)f]; cE[b].x+=fC[f].x; cE[b].y+=fC[f].y; cE[b].z+=fC[f].z; cnt[b]++; }
        }
        for (long long c=0;c<nC;c++){ double k=1.0/cnt[c]; cE[c].x*=k; cE[c].y*=k; cE[c].z*=k; }

        std::vector<V3> cC((size_t)nC, V3{0,0,0});
        std::vector<double> cV((size_t)nC, 0.0);
        for (long long f=0;f<nF;f++){
            int32_t o=own[(size_t)f];
            double p3=dot(fA[f], sub(fC[f], cE[o]));
            V3 pc={0.75*fC[f].x+0.25*cE[o].x, 0.75*fC[f].y+0.25*cE[o].y, 0.75*fC[f].z+0.25*cE[o].z};
            cC[o].x+=p3*pc.x; cC[o].y+=p3*pc.y; cC[o].z+=p3*pc.z; cV[o]+=p3;
            if (f<nI){ int32_t b=nei[(size_t)f];
                double q3=dot(fA[f], sub(cE[b], fC[f]));
                V3 qc={0.75*fC[f].x+0.25*cE[b].x, 0.75*fC[f].y+0.25*cE[b].y, 0.75*fC[f].z+0.25*cE[b].z};
                cC[b].x+=q3*qc.x; cC[b].y+=q3*qc.y; cC[b].z+=q3*qc.z; cV[b]+=q3; }
        }
        double vol=0;
        for (long long c=0;c<nC;c++){
            double v=cV[c];
            if (fabs(v)>1e-300){ cC[c].x/=v; cC[c].y/=v; cC[c].z/=v; } else cC[c]=cE[c];
            cV[c]=v/3.0; vol+=cV[c];
        }

        long long l65=0,l70=0,lw=0; double lmax=0;
        for (long long f=0;f<nI;f++){
            int32_t o=own[(size_t)f], b=nei[(size_t)f];
            V3 dv=sub(cC[b], cC[o]);
            double dds=dot(dv,fA[f])/(mg(dv)*mg(fA[f])+ROOTVSMALL);
            if(dds>1)dds=1; if(dds<-1)dds=-1;
            double ang=acos(dds)*180.0/M_PI;
            gsumcos+=dds; gn++;
            if(ang>lmax) lmax=ang;
            double sdo=fabs(dot(fA[f], sub(fC[f], cC[o])));
            double sdn=fabs(dot(fA[f], sub(cC[b], fC[f])));
            double w=sdn/(sdo+sdn+ROOTVSMALL);
            double wm=std::min(w,1.0-w);
            bool bad=false; const char* kind="";
            if(ang>70.0) l70++;
            if(ang>65.0){ l65++; bad=true; kind="nonortho"; }
            if(wm<0.02){ lw++; bad=true; kind=(ang>65.0)?"both":"weight"; }
            if(bad) fprintf(csv,"%d,%lld,%s,%.4f,%.6f,%.9g,%.9g,%.9g,%.6e,%.6e,%.6e\n",
                            pr,f,kind,ang,wm,fC[f].x,fC[f].y,fC[f].z,cV[o],cV[b],mg(fA[f]));
        }
        if(lmax>gmax) gmax=lmax;
        g65+=l65; g70+=l70; gw+=lw; gskip+=nF-nI; gint+=nI; gcells+=nC; gvol+=vol;
        printf("proc %d: %lld cells, %lld faces, %lld internal, vol %.6e, max %.4f, >65 %lld, >70 %lld, w<0.02 %lld\n",
               pr,nC,nF,nI,vol,lmax,l65,l70,lw);
        fflush(stdout);
    }
    fclose(csv);
    printf("\ncells %lld   internal faces %lld   skipped boundary+processor %lld   volume %.6e\n",
           gcells, gint, gskip, gvol);
    printf("max non-orthogonality %.4f deg\n", gmax);
    printf("average, OpenFOAM's definition acos(mean cos) %.5f deg\n",
           acos(gsumcos/(double)gn)*180.0/M_PI);
    printf("non-orthogonality > 65        : %lld\n", g65);
    printf("severely non-orthogonal > 70  : %lld\n", g70);
    printf("interpolation weight < 0.02   : %lld\n", gw);
    printf("wrote %s\n", outp.c_str());
    int bad = 0;
    if (eC >= 0) { printf("GATE cells      %s (%lld vs %lld)\n", gcells==eC?"OK":"FAIL", gcells, eC); if(gcells!=eC) bad=1; }
    if (eI >= 0) { printf("GATE internal   %s (%lld vs %lld)\n", gint==eI?"OK":"FAIL", gint, eI); if(gint!=eI) bad=1; }
    if (e65 >= 0) { printf("GATE >65        %s (%lld vs %lld, at or just below is expected)\n", g65<=e65?"OK":"FAIL", g65, e65); if(g65>e65) bad=1; }
    if (eW >= 0) { printf("GATE weight     %s (%lld vs %lld, at or just below is expected)\n", gw<=eW?"OK":"FAIL", gw, eW); if(gw>eW) bad=1; }
    if (bad) { printf("AT LEAST ONE GATE FAILED, do not use these positions\n"); return 3; }
    return 0;
}
