import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { ownerIdFromSession } from "@/lib/auth/session";

// CAD Repair System evidence.
//
// Everything on this page is a measured result, not marketing copy: the turntables under
// /repair-poc/ are renders of real corpus files before and after the pipeline's repair wrote
// them, and every number below comes from the corpus run rows (re-inspection of the written
// file, never the repair's own claim). The page is static evidence of what the system does today;
// it drives no repair itself. The live interaction happens in the run review surface.

type Showcase = {
  slug: string;
  title: string;
  corpus: string;
  file: string;
  before: { heading: string; points: string[] };
  after: { heading: string; points: string[] };
  receipt: string[];
};

const SHOWCASES: readonly Showcase[] = [
  {
    slug: "occt",
    title: "Torn shells, sewn back together",
    corpus: "OpenCascade 7.8.0 bug-report corpus",
    file: "trj4_k1_geo-pe-203.stp",
    before: {
      heading: "56 open shells",
      points: [
        "52 faces, 429 edges — a real translator failure, not an authored fixture",
        "every rip located and named as an edge target (shown in red)",
      ],
    },
    after: {
      heading: "0 open shells",
      points: [
        "sew_faces kept; the shape closes into a solid",
        "credited only after re-reading the written STEP file",
      ],
    },
    receipt: [
      "sew_faces → kept",
      "re-inspect → 56 → 0 open_shell",
      "per-operation caps respected",
      "rollback armed → unused",
    ],
  },
  {
    slug: "gen",
    title: "Sliver faces, swept clean",
    corpus: "Labelled generated-defect corpus (blind run)",
    file: "4d46eb9f57e6e9e055c0f2b560d64254.step",
    before: {
      heading: "6 sliver faces · 20 sliver edges",
      points: [
        "sub-millimetre Boolean residue on a ~380 mm part (marked in red)",
        "the engine saw only the geometry — never the answer key",
      ],
    },
    after: {
      heading: "0 small_face left behind",
      points: [
        "fix_small_faces kept; faces 70 → 64, every removal accounted to the kernel history",
        "labels consulted only after the run, to score it",
      ],
    },
    receipt: [
      "fix_small_faces → kept",
      "re-inspect → 6 → 0 small_face",
      "face removals declared and checked",
      "oversized sweep → declined",
    ],
  },
] as const;

const CORPUS_STATS = [
  { value: "362", label: "OpenCascade files inspected" },
  { value: "430", label: "defects repaired, credited by re-inspection" },
  { value: "0", label: "damaged results written" },
] as const;

export default async function RepairPage() {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <p className="page__eyebrow">Repair evidence</p>
          <h1 className="page__title">CAD Repair System</h1>
          <p className="page__intro">
            Broken CAD stops a mesh before it starts. The CAD Repair System locates each defect,
            repairs only what it can name, and credits a fix only by re-reading the file it
            wrote. The examples below are real corpus files; red marks the located defects.
          </p>
        </div>
      </div>

      <div className="summary-grid">
        {CORPUS_STATS.map((stat) => (
          <div className="metric-card repair-poc__stat" key={stat.label}>
            <b>{stat.value}</b>
            <p>{stat.label}</p>
          </div>
        ))}
      </div>

      {SHOWCASES.map((show) => (
        <section className="repair-poc__case" key={show.slug}>
          <div className="repair-poc__case-head">
            <h2>{show.title}</h2>
            <p className="repair-poc__source">
              <span>{show.corpus}</span>
              <span aria-hidden="true">·</span>
              <span className="repair-poc__file" title={show.file}>{show.file}</span>
            </p>
          </div>
          <div className="repair-poc__pair">
            {(["before", "after"] as const).map((phase) => (
              <figure className={`repair-poc__panel repair-poc__panel--${phase}`} key={phase}>
                <video
                  autoPlay
                  loop
                  muted
                  playsInline
                  preload="metadata"
                  src={`/repair-poc/${show.slug}-${phase}.mp4`}
                />
                <figcaption>
                  <span className="repair-poc__phase">{phase}</span>
                  <b>{show[phase].heading}</b>
                  <ul>
                    {show[phase].points.map((point) => (
                      <li key={point}>{point}</li>
                    ))}
                  </ul>
                </figcaption>
              </figure>
            ))}
          </div>
          <ul className="repair-poc__receipt">
            {show.receipt.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </section>
      ))}

      <p className="repair-poc__rule">
        A repair is credited only by re-reading the written file — never by the repair reporting
        its own success. Intentional openings (bolt holes, vents, paired rims) are refused, not
        sealed.
      </p>

      {/* Scoped styling on top of the dashboard sheets: tokens come from tokens.css/theme.css. */}
      <style>{`
        .repair-poc__stat{gap:.3rem}
        .repair-poc__stat b{font:600 1.5rem/1.1 var(--sans);letter-spacing:-.01em;color:var(--text-bright)}
        .repair-poc__stat p{margin:0;color:var(--text-muted);font-size:var(--fs-md);line-height:1.45}
        .repair-poc__case{margin-top:1.25rem;display:grid;gap:.875rem}
        .repair-poc__case-head{display:grid;gap:.3rem;min-width:0}
        .repair-poc__case-head h2{font:600 var(--fs-lg)/1.3 var(--sans);color:var(--text-bright);margin:0}
        .repair-poc__source{display:flex;align-items:baseline;gap:.5rem;min-width:0;margin:0;
          color:var(--text-faint);font-size:var(--fs-sm)}
        .repair-poc__source>span{flex:none}
        .repair-poc__file{flex:0 1 auto!important;min-width:0;max-width:13rem;cursor:help;overflow:hidden;
          text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono);font-size:var(--fs-xs);
          color:var(--text-muted)}
        .repair-poc__pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(20rem,1fr));gap:.875rem}
        .repair-poc__panel{margin:0;background:var(--surface-1);border:1px solid var(--border);
          border-radius:var(--r-md);overflow:hidden}
        .repair-poc__panel video{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;
          background:var(--viewer-canvas);border-bottom:1px solid var(--divider)}
        .repair-poc__panel figcaption{padding:.875rem 1rem 1rem;display:grid;gap:.375rem}
        .repair-poc__panel figcaption b{font:600 var(--fs-base)/1.35 var(--sans);color:var(--text-bright)}
        .repair-poc__panel ul{margin:0;padding-left:1rem;color:var(--text-muted);
          font-size:var(--fs-md);line-height:1.55}
        .repair-poc__phase{display:inline-flex;align-items:center;gap:.4rem;width:max-content;
          font:500 var(--fs-sm)/1 var(--sans);color:var(--text-muted);text-transform:capitalize}
        .repair-poc__phase::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--text-faint)}
        .repair-poc__panel--before .repair-poc__phase::before{background:var(--danger)}
        .repair-poc__panel--after .repair-poc__phase::before{background:var(--ok)}
        .repair-poc__receipt{margin:0;padding:0;list-style:none;display:flex;flex-wrap:wrap;gap:.375rem}
        .repair-poc__receipt li{font-family:var(--mono);font-size:var(--fs-xs);line-height:1;
          padding:.4rem .55rem;border:1px solid var(--divider);border-radius:var(--r-sm);
          background:var(--surface-1);color:var(--text-muted)}
        .repair-poc__rule{margin-top:1.5rem;max-width:62ch;color:var(--text-faint);
          font-size:var(--fs-md);line-height:1.6;padding-top:1rem;border-top:1px solid var(--divider)}
      `}</style>
    </div>
  );
}
