/**
 * Who to go after, and why, split by the work they do rather than by sector.
 *
 * This file holds COMPANIES and TITLES. It deliberately holds no names of
 * people: names and job titles go stale within months, and a list of people
 * assembled from memory is a list of plausible fiction. Apollo's search
 * returns real, current people for those companies and titles, so the
 * division of labour is: judgement here, facts from the API.
 *
 * Two sources feed each segment. Hand-curated seeds live below; the bulk
 * comes from companies.generated.ts, produced by a 14-agent research run
 * (7 industry researchers over live web sources, 7 adversarial verifiers
 * killing mega-corps, dead companies and unconfirmable domains). Generated
 * entries win on a name collision because their domains were spot-checked.
 *
 * The mega-corps that used to sit in the seeds (Medtronic, Carrier, Baker
 * Hughes...) are gone on purpose: a seed-stage vendor does not get a trial
 * through that procurement, and every giant on the list dilutes the segments
 * built around people who can say yes in a week.
 *
 * `domain` is filled in only where it came from a source. An empty domain is
 * a task, not a guess: guessing domains is how the first list ended up with
 * addresses that hard bounced.
 */
import { GENERATED, type GeneratedCompany } from "./companies.generated";
import { EXISTING, type ExistingCompany } from "./companies.existing";

export interface TargetCompany {
  name: string;
  /** Empty until confirmed from a real source. Never guessed. */
  domain?: string;
  note?: string;
}

export interface Segment {
  key: string;
  label: string;
  /** The reason meshing costs them, in the terms they would use. */
  thesis: string;
  /** Job titles to search for at these companies. */
  titles: string[];
  companies: TargetCompany[];
}

/** Titles that recur across most segments. */
const ENGINEERING_LEADERSHIP = [
  "Chief Technology Officer",
  "VP of Engineering",
  "Head of Engineering",
  "Director of Engineering",
  "Chief Engineer",
];

const SIMULATION_PRACTITIONERS = [
  "CFD Engineer",
  "Simulation Engineer",
  "Senior CFD Engineer",
  "Principal Engineer",
  "Director of Simulation",
  "Head of CAE",
  "CAE Manager",
  "Engineering Analysis Manager",
];

/**
 * Generated entries first, then any seed whose name is not already present.
 * Generated wins because its domain was verified by an agent that saw it in a
 * source; the seeds predate that check.
 */
function merged(
  seed: TargetCompany[],
  generated: GeneratedCompany[] | undefined,
  existing?: ExistingCompany[],
): TargetCompany[] {
  const out = new Map<string, TargetCompany>();
  for (const g of generated ?? []) {
    out.set(g.name.toLowerCase(), {
      name: g.name,
      domain: g.domain || undefined,
      note: [g.tier, g.hq, g.size].filter(Boolean).join(" · "),
    });
  }
  // Companies we already have contacts at belong in the same segments: the
  // point of searching them again is finding the 4-5 OTHER people worth
  // reaching, not re-finding the one founder already on file.
  for (const e of existing ?? []) {
    const k = e.name.toLowerCase();
    if (!out.has(k)) {
      out.set(k, {
        name: e.name,
        domain: e.domain || undefined,
        note: `on list · ${e.people} contact${e.people === 1 ? "" : "s"} (${e.roles})`,
      });
    }
  }
  for (const s of seed) {
    const k = s.name.toLowerCase();
    if (!out.has(k)) out.set(k, s);
  }
  return [...out.values()];
}

export const SEGMENTS: Segment[] = [
  {
    key: "yc",
    label: "YC companies",
    thesis:
      "Fellow YC companies at any size, from pre-seed to Boom Supersonic. The batch connection makes these warm rather than cold: Bookface, the founder directory, and 'fellow YC founder' in the first line. Founders are the target contact, because at a YC company the founder IS the decision.",
    titles: [
      "Founder",
      "Co-Founder",
      "CEO",
      "Chief Executive Officer",
      "CTO",
      "Chief Technology Officer",
      "President",
      "Chief Engineer",
      "VP of Engineering",
      "Head of Engineering",
    ],
    companies: merged([], GENERATED["yc"], EXISTING["yc"]),
  },

  {
    key: "cfd-consultancies",
    label: "CFD and simulation consultancies",
    thesis:
      "They bill by the hour for work that is mostly mesh preparation, so hours saved are margin rather than convenience. They also mesh unfamiliar geometry constantly, which is the hardest case and the one they cannot template away. Smallest sale, shortest cycle, and they can evaluate it on a live job in a week.",
    titles: [
      "Founder",
      "Owner",
      "Managing Director",
      "Principal Engineer",
      "Technical Director",
      ...SIMULATION_PRACTITIONERS,
    ],
    companies: merged(
      [
        { name: "Resolved Analytics", domain: "resolvedanalytics.com", note: "Durham NC, multi-physics" },
        { name: "TotalSim", domain: "totalsim.us", note: "CFD outsourcing across 11+ industries" },
        { name: "Ozen Engineering", domain: "ozeninc.com", note: "meshing, solver setup, post" },
        { name: "EnginSoft USA", domain: "enginsoftusa.com", note: "FEA and CFD consulting" },
        { name: "CFD Research Corporation", note: "Huntsville AL, aerospace R&D" },
        { name: "Fidelis Engineering Associates", note: "Troy MI" },
        { name: "Arcofluid Consulting", note: "Orlando FL" },
        { name: "Tridiagonal Solutions", note: "San Antonio TX" },
        { name: "Air Flow Sciences", note: "Livonia MI" },
        { name: "Simulent Consulting", note: "Toronto" },
        { name: "FEMTO Engineering", note: "Munster, Germany" },
        { name: "Volupe AB", note: "Sweden" },
        { name: "Termofluids", note: "Barcelona" },
        { name: "Windtech Consultants", note: "Sydney, CFD and wind tunnel" },
        { name: "Desanco", note: "Melbourne" },
        { name: "Aerotherm", note: "South Africa" },
        { name: "Cape CFD", note: "Cape Town" },
        { name: "Megagenix", note: "Singapore" },
        { name: "NING Research", note: "Singapore" },
        { name: "MegaFlow", note: "Taiwan" },
        { name: "Niha Solutions", note: "India, thermal and CFD" },
        { name: "DPR Engenharia e Simulacao", note: "Brazil" },
        { name: "Precision Labs", note: "Chile" },
        { name: "Quest Consultants", note: "Norman OK" },
        { name: "Synthetik Technologies", note: "Austin TX, defence modelling" },
        { name: "Combustion Science and Engineering", note: "Columbia MD, fire modelling" },
      ],
      GENERATED["consultancies"],
    ),
  },

  {
    key: "biomedical",
    label: "Medical devices and patient-specific simulation",
    thesis:
      "Patient-specific geometry means the mesh is new every single case, so hand meshing does not merely cost time, it does not scale at all. Cardiovascular work is the sharpest version: vasculature, valves, stents, blood pumps, with pulsatile flow, hemolysis constraints and thin walls. Hexera's own site names this vertical.",
    titles: [
      ...ENGINEERING_LEADERSHIP,
      ...SIMULATION_PRACTITIONERS,
      "Director of Computational Modeling",
      "Head of Modeling and Simulation",
      "R&D Engineer",
    ],
    companies: merged(
      [
        { name: "HeartFlow", note: "patient-specific coronary CFD is the product itself" },
        { name: "Materialise", note: "medical image to model" },
        { name: "Elucid", note: "coronary imaging analysis" },
        { name: "Cleerly", note: "coronary imaging analysis" },
      ],
      GENERATED["medical"],
    ),
  },

  {
    key: "hvac-thermal",
    label: "HVAC, data centre cooling and electronics thermal",
    thesis:
      "Airflow through geometry that changes with every building, rack layout or product revision. Data centre cooling is the fast-moving end: AI density has made thermal design a bottleneck, budgets are large, and the geometry is re-meshed constantly.",
    titles: [
      ...ENGINEERING_LEADERSHIP,
      ...SIMULATION_PRACTITIONERS,
      "Thermal Engineer",
      "Head of Thermal",
      "Director of Thermal Engineering",
    ],
    companies: merged(
      [
        { name: "LiquidStack", note: "immersion cooling" },
        { name: "Asetek", note: "liquid cooling" },
      ],
      GENERATED["hvac-thermal"],
    ),
  },

  {
    key: "aero-startups",
    label: "Aerospace and defence",
    thesis:
      "Launch, in-space propulsion, hypersonics, eVTOL, engines and airframes at Series A-C, where one aero lead owns the whole simulation stack and can trial a tool without procurement. Includes the first list's aerospace companies, there to find the other four people beside the founder already on file.",
    titles: [
      ...ENGINEERING_LEADERSHIP,
      ...SIMULATION_PRACTITIONERS,
      "Head of Aerodynamics",
      "Propulsion Engineer",
      "Aerodynamics Engineer",
    ],
    companies: merged([], GENERATED["aero"], EXISTING["aero"]),
  },

  {
    key: "auto-motorsport",
    label: "Automotive, EV and motorsport engineering",
    thesis:
      "Motorsport is the sweet spot: race constructors and engineering consultancies mesh weekly under fixed deadlines, and the aero department head decides tooling. Around them, hypercar makers, EV powertrain startups and automotive aero specialists with the same shape of problem.",
    titles: [
      ...ENGINEERING_LEADERSHIP,
      ...SIMULATION_PRACTITIONERS,
      "Head of Aerodynamics",
      "Technical Director",
      "Chief Designer",
      "Vehicle Dynamics Engineer",
    ],
    companies: merged([], GENERATED["auto"], EXISTING["auto"]),
  },

  {
    key: "semiconductors",
    label: "Semiconductors and chip thermal",
    thesis:
      "Package and board-level thermal means conjugate heat transfer on geometry that changes every revision, which is the worst case for hand-built meshes. All fifty are from the original list; the search here is for the thermal and mechanical engineers beside the founders already on file.",
    titles: [
      ...ENGINEERING_LEADERSHIP,
      ...SIMULATION_PRACTITIONERS,
      "Thermal Engineer",
      "Head of Thermal",
      "Director of Mechanical Engineering",
      "Packaging Engineer",
    ],
    companies: merged([], undefined, EXISTING["semiconductors"]),
  },

  {
    key: "turbomachinery-energy",
    label: "Energy, turbomachinery and cleantech hardware",
    thesis:
      "Fusion and advanced nuclear startups simulate constantly and are drowning in geometry; hydrogen, electrolyzers, tidal and heat exchangers all live on conjugate heat transfer and rotating machinery, meshed to resolve boundary layers or the answer is wrong rather than late.",
    titles: [
      ...ENGINEERING_LEADERSHIP,
      ...SIMULATION_PRACTITIONERS,
      "Head of Aerodynamics",
      "Turbomachinery Engineer",
      "Thermal Engineer",
    ],
    companies: merged([], GENERATED["energy"]),
  },

  {
    key: "marine",
    label: "Marine, naval and offshore engineering",
    thesis:
      "Hulls, foils, propellers and floating platforms: free-surface CFD on geometry that changes with every design round. The design offices are small, mesh weekly, and the naval architect who feels the pain is usually a partner in the firm.",
    titles: [
      ...ENGINEERING_LEADERSHIP,
      ...SIMULATION_PRACTITIONERS,
      "Naval Architect",
      "Head of Hydrodynamics",
      "Hydrodynamics Engineer",
      "Technical Director",
    ],
    companies: merged([], GENERATED["marine"]),
  },

  {
    key: "gaming",
    label: "Game engines and real-time simulation",
    thesis:
      "Watertight collision meshes and fluid volumes, named on Hexera's own site. A different buyer with a different vocabulary, so it needs its own email rather than the CFD one. Worth a small test before committing to it.",
    titles: [
      "Technical Director",
      "Lead Technical Artist",
      "Head of Technology",
      "Principal Engineer",
      "Director of Technology",
    ],
    companies: [],
  },
];

export function segment(key: string): Segment | undefined {
  return SEGMENTS.find((s) => s.key === key);
}

/** Domains we can actually search on today, deduplicated. */
export function domainsFor(key: string): string[] {
  return [
    ...new Set(
      (segment(key)?.companies ?? [])
        .map((c) => c.domain)
        .filter((d): d is string => Boolean(d)),
    ),
  ];
}

/** Companies still needing a confirmed domain before they can be searched. */
export function needsDomain(key: string): string[] {
  return (segment(key)?.companies ?? []).filter((c) => !c.domain).map((c) => c.name);
}
