import { notFound, redirect } from "next/navigation";

import { auth } from "@/auth";
import { ownerIdFromSession } from "@/lib/auth/session";

// CAD repair proof of concept.
//
// Everything on this page is a measured result, not marketing copy: the turntables under
// /repair-poc/ are renders of real corpus files before and after the pipeline's repair wrote
// them, and every number below comes from the corpus run rows (re-inspection of the written
// file, never the repair's own claim). The page is static evidence of what the flagged engine
// does today; it drives no repair itself. Gated by CAD_REPAIR_POC, the same posture as the
// pipeline's own CAD_REPAIR_ENABLED: off everywhere a deployment does not say otherwise.

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

export default async function RepairPocPage() {
  if (process.env.CAD_REPAIR_POC !== "true") {
    notFound();
  }
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <p className="page__eyebrow">Proof of concept</p>
          <h1 className="page__title">CAD repair</h1>
          <p className="page__intro">
            Broken CAD stops a mesh before it starts. The repair engine locates each defect,
            repairs only what it can name, and credits a fix only by re-reading the file it
            wrote. Both parts below are real corpus files; red marks the located defects.
          </p>
        </div>
      </div>

      <div className="summary-grid">
        {CORPUS_STATS.map((stat) => (
          <div className="metric-card" key={stat.label}>
            <p className="label">{stat.label}</p>
            <b>{stat.value}</b>
          </div>
        ))}
      </div>

      {SHOWCASES.map((show) => (
        <section className="repair-poc__case" key={show.slug}>
          <div className="repair-poc__case-head">
            <h2>{show.title}</h2>
            <p className="label">
              {show.corpus} · {show.file}
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
        .repair-poc__case{margin-top:2rem;display:grid;gap:1rem}
        .repair-poc__case-head h2{font-family:var(--display);font-weight:600;font-size:1.15rem;margin:0 0 .25rem}
        .repair-poc__pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(20rem,1fr));gap:1rem}
        .repair-poc__panel{margin:0;background:rgba(18,20,20,.78);border:1px solid var(--border);
          border-radius:var(--radius);overflow:hidden}
        .repair-poc__panel video{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:#0b0e14}
        .repair-poc__panel figcaption{padding:.9rem 1.1rem;display:grid;gap:.4rem}
        .repair-poc__panel ul{margin:0;padding-left:1.05rem;color:var(--text-muted);
          font-size:.85rem;line-height:1.55}
        .repair-poc__phase{font-family:var(--mono);font-size:.6rem;letter-spacing:.18em;text-transform:uppercase}
        .repair-poc__panel--before .repair-poc__phase{color:#e5484d}
        .repair-poc__panel--after .repair-poc__phase{color:#46a758}
        .repair-poc__receipt{margin:0;padding:0;list-style:none;display:flex;flex-wrap:wrap;gap:.5rem}
        .repair-poc__receipt li{font-family:var(--mono);font-size:.72rem;padding:.35rem .7rem;
          border:1px solid var(--border);border-radius:999px;color:var(--text-muted)}
        .repair-poc__rule{margin-top:2rem;max-width:44rem;color:var(--text-muted);
          font-size:.9rem;line-height:1.65;border-left:2px solid var(--border);padding-left:1rem}
      `}</style>
    </div>
  );
}
