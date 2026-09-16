/**
 * Minimal, dependency-free XLSX reader.
 *
 * An .xlsx file is a ZIP containing XML parts. We only need enough of both
 * formats to pull a rectangular grid out of each worksheet, so this reads the
 * ZIP central directory, inflates the parts we care about, and does a
 * shallow parse of the SpreadsheetML.
 *
 * Deliberately not a general-purpose library: no formulas, no styles, no dates
 * beyond the serial-number conversion, no streaming. It handles the shape of
 * file we actually ingest (a header row plus data rows) and throws loudly on
 * anything it does not understand.
 */
import { inflateRawSync } from "node:zlib";
import { readFileSync } from "node:fs";

const EOCD_SIG = 0x06054b50;
const EOCD64_LOCATOR_SIG = 0x07064b50;
const EOCD64_SIG = 0x06064b50;
const CEN_SIG = 0x02014b50;

type ZipEntry = { name: string; method: number; compressedSize: number; localHeaderOffset: number };

function findEocd(buf: Buffer): number {
  // EOCD is at least 22 bytes and the comment field is capped at 64 KiB.
  const min = Math.max(0, buf.length - 22 - 0xffff);
  for (let i = buf.length - 22; i >= min; i--) {
    if (buf.readUInt32LE(i) === EOCD_SIG) return i;
  }
  throw new Error("Not a ZIP archive: end-of-central-directory record not found");
}

function readCentralDirectory(buf: Buffer): ZipEntry[] {
  const eocd = findEocd(buf);
  let entryCount = buf.readUInt16LE(eocd + 10);
  let cdOffset = buf.readUInt32LE(eocd + 16);

  // ZIP64: the 32-bit fields saturate and the real values live in the ZIP64 EOCD.
  if (cdOffset === 0xffffffff || entryCount === 0xffff) {
    const locator = eocd - 20;
    if (locator < 0 || buf.readUInt32LE(locator) !== EOCD64_LOCATOR_SIG) {
      throw new Error("ZIP64 archive missing its end-of-central-directory locator");
    }
    const eocd64 = Number(buf.readBigUInt64LE(locator + 8));
    if (buf.readUInt32LE(eocd64) !== EOCD64_SIG) {
      throw new Error("ZIP64 end-of-central-directory record is malformed");
    }
    entryCount = Number(buf.readBigUInt64LE(eocd64 + 32));
    cdOffset = Number(buf.readBigUInt64LE(eocd64 + 48));
  }

  const entries: ZipEntry[] = [];
  let p = cdOffset;
  for (let i = 0; i < entryCount; i++) {
    if (buf.readUInt32LE(p) !== CEN_SIG) {
      throw new Error(`Corrupt ZIP: bad central-directory signature at entry ${i}`);
    }
    const method = buf.readUInt16LE(p + 10);
    const compressedSize = buf.readUInt32LE(p + 20);
    const nameLen = buf.readUInt16LE(p + 28);
    const extraLen = buf.readUInt16LE(p + 30);
    const commentLen = buf.readUInt16LE(p + 32);
    const localHeaderOffset = buf.readUInt32LE(p + 42);
    const name = buf.toString("utf8", p + 46, p + 46 + nameLen);
    entries.push({ name, method, compressedSize, localHeaderOffset });
    p += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

function extract(buf: Buffer, entry: ZipEntry): string {
  // The central directory's sizes are authoritative, but the name/extra lengths
  // in the *local* header are the ones that locate the data, and they can
  // differ from the central copy. Always re-read them here.
  const lh = entry.localHeaderOffset;
  const nameLen = buf.readUInt16LE(lh + 26);
  const extraLen = buf.readUInt16LE(lh + 28);
  const start = lh + 30 + nameLen + extraLen;
  const raw = buf.subarray(start, start + entry.compressedSize);
  if (entry.method === 0) return raw.toString("utf8");
  if (entry.method === 8) return inflateRawSync(raw).toString("utf8");
  throw new Error(`Unsupported ZIP compression method ${entry.method} for ${entry.name}`);
}

const XML_ENTITIES: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
};

function decodeXml(s: string): string {
  return s.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (whole, body: string) => {
    if (body[0] === "#") {
      const code = body[1] === "x" || body[1] === "X"
        ? parseInt(body.slice(2), 16)
        : parseInt(body.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : whole;
    }
    return XML_ENTITIES[body] ?? whole;
  });
}

/** Concatenated text of every <t> descendant — shared strings may be split across runs. */
function sharedStringText(siFragment: string): string {
  let out = "";
  const re = /<t\b[^>]*?(\/>|>([\s\S]*?)<\/t>)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(siFragment))) out += m[1] === "/>" ? "" : decodeXml(m[2]);
  return out;
}

function parseSharedStrings(xml: string): string[] {
  const out: string[] = [];
  const re = /<si\b[^>]*?(?:\/>|>([\s\S]*?)<\/si>)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(xml))) out.push(m[1] === undefined ? "" : sharedStringText(m[1]));
  return out;
}

/** "BC7" -> 54 (zero-based column index). */
function columnIndex(ref: string): number {
  const letters = /^([A-Z]+)/.exec(ref)?.[1] ?? "A";
  let n = 0;
  for (const ch of letters) n = n * 26 + (ch.charCodeAt(0) - 64);
  return n - 1;
}

export type Cell = string | number | boolean | null;
export type Sheet = { name: string; rows: Cell[][] };

function parseSheet(xml: string, shared: string[]): Cell[][] {
  const rows: Cell[][] = [];
  const rowRe = /<row\b([^>]*?)(?:\/>|>([\s\S]*?)<\/row>)/g;
  let rowMatch: RegExpExecArray | null;

  while ((rowMatch = rowRe.exec(xml))) {
    const body = rowMatch[2];
    // Honor r="N" so blank rows in the middle of the sheet keep their position.
    const declaredIndex = /\br="(\d+)"/.exec(rowMatch[1])?.[1];
    const rowIndex = declaredIndex ? Number(declaredIndex) - 1 : rows.length;
    while (rows.length <= rowIndex) rows.push([]);
    if (!body) continue;

    const cells = rows[rowIndex];
    const cellRe = /<c\b([^>]*?)(?:\/>|>([\s\S]*?)<\/c>)/g;
    let cellMatch: RegExpExecArray | null;
    let cursor = 0;

    while ((cellMatch = cellRe.exec(body))) {
      const attrs = cellMatch[1];
      const inner = cellMatch[2] ?? "";
      const ref = /\br="([A-Z]+\d+)"/.exec(attrs)?.[1];
      const col = ref ? columnIndex(ref) : cursor;
      cursor = col + 1;
      while (cells.length <= col) cells.push(null);

      const type = /\bt="([^"]+)"/.exec(attrs)?.[1] ?? "n";
      if (type === "inlineStr") {
        cells[col] = sharedStringText(inner) || null;
        continue;
      }
      const vRaw = /<v\b[^>]*?(?:\/>|>([\s\S]*?)<\/v>)/.exec(inner)?.[1];
      if (vRaw === undefined || vRaw === "") {
        cells[col] = null;
        continue;
      }
      const v = decodeXml(vRaw);
      switch (type) {
        case "s": {
          const idx = Number(v);
          cells[col] = shared[idx] ?? null;
          break;
        }
        case "b":
          cells[col] = v === "1";
          break;
        case "str":
          cells[col] = v;
          break;
        case "e":
          cells[col] = null; // formula error cell
          break;
        default: {
          const num = Number(v);
          cells[col] = Number.isFinite(num) ? num : v;
        }
      }
    }
  }
  return rows;
}

/**
 * Sheet order in workbook.xml is the order the user sees; the r:id on each
 * <sheet> points into workbook.xml.rels, which names the actual part. Going
 * through the rels (rather than assuming sheet1.xml is the first tab) is what
 * keeps tab names attached to the right grid.
 */
export function readXlsx(filePath: string): Sheet[] {
  const buf = readFileSync(filePath);
  const entries = readCentralDirectory(buf);
  const byName = new Map(entries.map((e) => [e.name, e]));

  const get = (name: string): string | null => {
    const entry = byName.get(name);
    return entry ? extract(buf, entry) : null;
  };

  const workbookXml = get("xl/workbook.xml");
  if (!workbookXml) throw new Error("Not an XLSX file: xl/workbook.xml is missing");

  const relsXml = get("xl/_rels/workbook.xml.rels") ?? "";
  const relTargets = new Map<string, string>();
  const relRe = /<Relationship\b([^>]*)\/>/g;
  let relMatch: RegExpExecArray | null;
  while ((relMatch = relRe.exec(relsXml))) {
    const attrs = relMatch[1];
    const id = /\bId="([^"]+)"/.exec(attrs)?.[1];
    let target = /\bTarget="([^"]+)"/.exec(attrs)?.[1];
    if (!id || !target) continue;
    target = decodeXml(target).replace(/^\/?(xl\/)?/, "");
    relTargets.set(id, `xl/${target}`);
  }

  const sharedXml = get("xl/sharedStrings.xml");
  const shared = sharedXml ? parseSharedStrings(sharedXml) : [];

  const sheets: Sheet[] = [];
  const sheetRe = /<sheet\b([^>]*)\/>/g;
  let sheetMatch: RegExpExecArray | null;
  let ordinal = 0;

  while ((sheetMatch = sheetRe.exec(workbookXml))) {
    const attrs = sheetMatch[1];
    ordinal++;
    const name = decodeXml(/\bname="([^"]*)"/.exec(attrs)?.[1] ?? `Sheet${ordinal}`);
    const rid = /\br:id="([^"]+)"/.exec(attrs)?.[1];
    const part = (rid && relTargets.get(rid)) || `xl/worksheets/sheet${ordinal}.xml`;
    const sheetXml = get(part);
    if (!sheetXml) continue;
    sheets.push({ name, rows: parseSheet(sheetXml, shared) });
  }

  if (sheets.length === 0) throw new Error("XLSX contained no readable worksheets");
  return sheets;
}

/** Header row + object rows, with the header text as keys. */
export function sheetToObjects(sheet: Sheet): Record<string, Cell>[] {
  const [header, ...body] = sheet.rows;
  if (!header) return [];
  const keys = header.map((h, i) => (h == null ? `col_${i}` : String(h).trim()));
  return body
    .filter((row) => row.some((cell) => cell !== null && cell !== ""))
    .map((row) => {
      const obj: Record<string, Cell> = {};
      keys.forEach((key, i) => {
        obj[key] = row[i] ?? null;
      });
      return obj;
    });
}
