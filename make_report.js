"use strict";
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, LevelFormat, ExternalHyperlink,
  BorderStyle, WidthType, ShadingType, HeadingLevel,
  PageNumber, PageBreak, VerticalAlign, TableOfContents
} = require("docx");
const fs = require("fs");

// ── Dimensions ────────────────────────────────────────────────────────────────
const W = 9360; // content width DXA (US Letter 8.5" - 2" margins)

// ── Colors ────────────────────────────────────────────────────────────────────
const NAVY    = "1B3A6B";
const BLUE    = "2E75B6";
const LBLUE   = "D6E4F0";
const MGRAY   = "F2F2F2";
const DGRAY   = "595959";
const GREEN   = "375623";
const LGREEN  = "E2EFDA";
const RED     = "843C0C";
const LRED    = "FCE4D6";
const WHITE   = "FFFFFF";
const BLACK   = "000000";

// ── Border helpers ─────────────────────────────────────────────────────────────
const border1 = (color="AAAAAA") => ({ style: BorderStyle.SINGLE, size: 4, color });
const borders = (color="AAAAAA") => ({
  top: border1(color), bottom: border1(color),
  left: border1(color), right: border1(color),
});
const thickBorder = () => ({
  top:    { style: BorderStyle.SINGLE, size: 8,  color: NAVY },
  bottom: { style: BorderStyle.SINGLE, size: 8,  color: NAVY },
  left:   { style: BorderStyle.SINGLE, size: 8,  color: NAVY },
  right:  { style: BorderStyle.SINGLE, size: 8,  color: NAVY },
});

// ── Cell factory ──────────────────────────────────────────────────────────────
function cell(text, {
  width = 1872, bold = false, shade = null, color = BLACK,
  colspan = 1, rowspan = 1, align = AlignmentType.LEFT, fontSize = 20
} = {}) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    columnSpan: colspan,
    rowSpan:    rowspan,
    borders:    borders(),
    margins:    { top: 80, bottom: 80, left: 120, right: 120 },
    shading: shade ? { fill: shade, type: ShadingType.CLEAR } : undefined,
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({
      alignment: align,
      children: [new TextRun({ text: String(text), bold, color, size: fontSize, font: "Arial" })]
    })]
  });
}

function hdrCell(text, width, shade = LBLUE) {
  return cell(text, { width, bold: true, shade, color: NAVY, fontSize: 20 });
}

// ── Paragraph factories ───────────────────────────────────────────────────────
function h1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    pageBreakBefore: true,
    children: [new TextRun({ text, font: "Arial" })]
  });
}
function h2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    children: [new TextRun({ text, font: "Arial" })]
  });
}
function h3(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_3,
    children: [new TextRun({ text, font: "Arial" })]
  });
}
function para(text, { bold=false, italic=false, color=BLACK, size=22, spacing=160 }={}) {
  return new Paragraph({
    spacing: { after: spacing },
    children: [new TextRun({ text, bold, italic, color, size, font: "Arial" })]
  });
}
function bullet(text, { bold=false, sub=false }={}) {
  return new Paragraph({
    numbering: { reference: sub ? "sub-bullets" : "bullets", level: 0 },
    spacing:   { after: 80 },
    children:  [new TextRun({ text, bold, size: 22, font: "Arial" })]
  });
}
function nbsp() {
  return new Paragraph({ spacing: { after: 80 }, children: [new TextRun("")] });
}
function link(text, url) {
  return new ExternalHyperlink({
    children: [new TextRun({ text, style: "Hyperlink", size: 22, font: "Arial" })],
    link: url,
  });
}
function kv(label, value) {
  return new Paragraph({
    spacing: { after: 100 },
    children: [
      new TextRun({ text: label + ": ", bold: true, size: 22, font: "Arial" }),
      new TextRun({ text: value, size: 22, font: "Arial" }),
    ]
  });
}

// ── Horizontal rule ───────────────────────────────────────────────────────────
function rule(color=BLUE) {
  return new Paragraph({
    spacing: { before: 120, after: 120 },
    border:  { bottom: { style: BorderStyle.SINGLE, size: 6, color, space: 1 } },
    children: []
  });
}

// ── Status badge (colored text) ──────────────────────────────────────────────
function statusCell(text, ok) {
  return cell(text, { width: 900, bold: true, color: ok ? GREEN : RED,
                      shade: ok ? LGREEN : LRED, align: AlignmentType.CENTER });
}

// ── Section title box ─────────────────────────────────────────────────────────
function sectionBox(num, title) {
  return new Table({
    width: { size: W, type: WidthType.DXA },
    columnWidths: [W],
    rows: [new TableRow({ children: [
      new TableCell({
        width: { size: W, type: WidthType.DXA },
        borders: { bottom: { style: BorderStyle.SINGLE, size: 8, color: BLUE } },
        shading: { fill: NAVY, type: ShadingType.CLEAR },
        margins: { top: 100, bottom: 100, left: 200, right: 200 },
        children: [new Paragraph({
          children: [
            new TextRun({ text: `${num}. `, bold: true, size: 28, color: "90CAF9", font: "Arial" }),
            new TextRun({ text: title, bold: true, size: 28, color: WHITE, font: "Arial" })
          ]
        })]
      })
    ]})]
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// DOCUMENT CONTENT
// ─────────────────────────────────────────────────────────────────────────────

// ── Status table (pipeline metrics) ──────────────────────────────────────────
function makeStatusTable() {
  const rows = [
    ["Total PDFs in Collection", "576,384", "13 collections, 1911-2024"],
    ["Total Slices", "2,705",  "SLICE_SIZE = 200 records/slice"],
    ["Slices Completed (S3)", "160 / 391 (41%)", "job_status.json present"],
    ["Records Processed", "4,773", "Full 4-stage pipeline"],
    ["County Extraction Rate", "100%", "After new Gemini API key injection"],
    ["Wells with GPS Coords", "2,449 (51%)", "Validated Oklahoma bounding box"],
    ["RUNNING Batch Jobs", "30", "At concurrency limit"],
    ["RUNNABLE (Queued)", "2,702", "2,522 rev5 + 180 retry-slice"],
    ["New API Key Active Since", "May 21, 2026", "Second Gmail account, fresh 10k quota"],
    ["D: Drive ↔ S3 Sync", "PERFECT MATCH", "576,384 / 576,384 after Col13 fix"],
  ];
  const cols = [2800, 2200, 4360];
  return new Table({
    width: { size: W, type: WidthType.DXA },
    columnWidths: cols,
    rows: [
      new TableRow({ children: [
        hdrCell("Metric", cols[0]), hdrCell("Value", cols[1]), hdrCell("Notes", cols[2])
      ], tableHeader: true }),
      ...rows.map(([m, v, n], i) => new TableRow({ children: [
        cell(m, { width: cols[0], bold: true }),
        cell(v, { width: cols[1], shade: i % 2 === 0 ? MGRAY : WHITE,
                  bold: v.includes("100%") || v.includes("PERFECT") }),
        cell(n, { width: cols[2], shade: i % 2 === 0 ? MGRAY : WHITE }),
      ]}))
    ]
  });
}

// ── S3 verification table ─────────────────────────────────────────────────────
function makeS3Table() {
  const data = [
    [1, "54,979", "54,979", "1911-1925", "15", "180", "OK"],
    [2, "46,492", "46,492", "1926-1940", "15", "180", "OK"],
    [3, "41,545", "41,545", "1941-1950", "10", "120", "OK"],
    [4, "53,988", "53,988", "1941-1955",  "7",  "84", "OK"],
    [5, "42,338", "42,338", "1956-1960",  "5",  "60", "OK"],
    [6, "53,851", "53,851", "1961-1970", "10", "120", "OK"],
    [7, "52,855", "52,855", "1971-1979",  "9", "108", "OK"],
    [8, "49,457", "49,457", "1980-1982",  "3",  "36", "OK"],
    [9, "49,578", "49,578", "1983-1987",  "5",  "60", "OK"],
    [10,"53,492", "53,492", "1988-2000", "13", "156", "OK"],
    [11,"50,991", "50,991", "2001-2012", "12", "144", "OK"],
    [12,"19,728", "19,728", "2013-2018",  "6",  "72", "OK"],
    [13, "7,090",  "7,090", "2019-2024",  "6",  "72", "FIXED"],
  ];
  const cols = [600, 1500, 1500, 1500, 900, 1200, 2160];
  return new Table({
    width: { size: W, type: WidthType.DXA },
    columnWidths: cols,
    rows: [
      new TableRow({ tableHeader: true, children: [
        hdrCell("Col", cols[0]), hdrCell("D: Drive", cols[1]),
        hdrCell("S3 (after fix)", cols[2]), hdrCell("Years", cols[3]),
        hdrCell("Yrs", cols[4]), hdrCell("Months", cols[5]),
        hdrCell("Status", cols[6])
      ]}),
      ...data.map(([c, d, s, yr, y, m, st]) => {
        const ok = st === "OK";
        const shade = !ok ? LGREEN : (c % 2 === 0 ? MGRAY : WHITE);
        return new TableRow({ children: [
          cell(c,  { width: cols[0], shade, align: AlignmentType.CENTER }),
          cell(d,  { width: cols[1], shade, align: AlignmentType.RIGHT }),
          cell(s,  { width: cols[2], shade, align: AlignmentType.RIGHT }),
          cell(yr, { width: cols[3], shade, align: AlignmentType.CENTER }),
          cell(y,  { width: cols[4], shade, align: AlignmentType.CENTER }),
          cell(m,  { width: cols[5], shade, align: AlignmentType.CENTER }),
          statusCell(st, ok || st==="FIXED"),
        ]});
      }),
      new TableRow({ children: [
        hdrCell("TOTAL", cols[0]),
        hdrCell("576,384", cols[1]),
        hdrCell("576,384", cols[2]),
        hdrCell("1911-2024", cols[3]),
        hdrCell("", cols[4]),
        hdrCell("", cols[5]),
        hdrCell("SYNCED", cols[6]),
      ]})
    ]
  });
}

// ── API Limits table ──────────────────────────────────────────────────────────
function makeApiTable() {
  const data = [
    ["Google Gemini Flash 2.5", "10,000 req/day", "Free tier", "Daily quota exhausts with 30 concurrent tasks; rotate API keys"],
    ["Google Gemini Pro",       "Paid per token", "~$0.0035/1k chars", "Used as fallback; no daily quota"],
    ["Google Cloud Vision OCR", "1,000 calls/mo free", "~$1.50/1,000 pages", "576,384 PDFs = ~$865 total OCR cost"],
    ["AWS Batch Fargate",       "30 concurrent tasks", "$0.04048/vCPU-hr", "2 vCPU + 4 GB = ~$0.099/hr per task"],
    ["AWS S3 Storage",          "Unlimited",           "$0.023/GB/month",  "~1.15 TB of PDFs = ~$26/month"],
    ["AWS ECR",                 "500 MB free/month",   "$0.10/GB/month",   "638 MB image; clean 12 stale images (~4.5 GB)"],
    ["RDS PostgreSQL",          "db.t3.micro",         "~$12/month",       "Stop instance after pipeline completes"],
    ["AWS Secrets Manager",     "Unlimited secrets",   "$0.40/secret/month","Stores GCP service account + Gemini API key"],
  ];
  const cols = [2100, 1600, 1500, 4160];
  return new Table({
    width: { size: W, type: WidthType.DXA },
    columnWidths: cols,
    rows: [
      new TableRow({ tableHeader: true, children: [
        hdrCell("Service", cols[0]), hdrCell("Limit / Quota", cols[1]),
        hdrCell("Cost", cols[2]), hdrCell("Notes & Mitigations", cols[3])
      ]}),
      ...data.map(([s, l, c, n], i) => new TableRow({ children: [
        cell(s, { width: cols[0], bold: true, shade: i%2===0?MGRAY:WHITE }),
        cell(l, { width: cols[1], shade: i%2===0?MGRAY:WHITE }),
        cell(c, { width: cols[2], shade: i%2===0?MGRAY:WHITE }),
        cell(n, { width: cols[3], shade: i%2===0?MGRAY:WHITE }),
      ]}))
    ]
  });
}

// ── Architecture table ────────────────────────────────────────────────────────
function makeArchTable() {
  const rows = [
    ["Input Storage", "AWS S3", "osu-well-records-225989338968/pdfs/", "us-east-1"],
    ["Processing Index", "CSV in S3", "index/dataset_index.csv", "576,384 rows, 2,705 slices"],
    ["Container Registry", "AWS ECR", "osu-pipeline:latest (638 MB)", "225989338968.dkr.ecr.us-east-1.amazonaws.com"],
    ["Compute", "AWS Batch Fargate", "osu-pipeline-queue / job def :5", "2 vCPU, 4 GB RAM, 4-hr timeout"],
    ["OCR Engine", "Google Vision API", "v1/images:annotate", "Credentials in Secrets Manager"],
    ["AI Extraction", "Gemini Flash 2.5", "gemini-2.5-flash", "GOOGLE_API_KEY via Secrets Manager"],
    ["PLSS Database", "AWS RDS PostgreSQL", "Oklahomaplss", "oklahomagridlatlongdb.cz62c0sysryk.us-east-1.rds.amazonaws.com"],
    ["Result Storage", "AWS S3", "results/slice-NNNNN/", "dot_coordinates.csv, run_insights.json, job_status.json"],
    ["Map Hosting", "AWS S3 + GitHub Pages", "viewer/well_map.html", "https://akshaykumarmundrathi.github.io/osu-well-pipeline/"],
  ];
  const cols = [1800, 1800, 2600, 3160];
  return new Table({
    width: { size: W, type: WidthType.DXA },
    columnWidths: cols,
    rows: [
      new TableRow({ tableHeader: true, children: [
        hdrCell("Component", cols[0]), hdrCell("Technology", cols[1]),
        hdrCell("Identifier / Path", cols[2]), hdrCell("Details", cols[3])
      ]}),
      ...rows.map(([c, t, id, d], i) => new TableRow({ children: [
        cell(c,  { width: cols[0], bold: true, shade: i%2===0?MGRAY:WHITE }),
        cell(t,  { width: cols[1], shade: i%2===0?MGRAY:WHITE }),
        cell(id, { width: cols[2], shade: i%2===0?MGRAY:WHITE, fontSize: 18 }),
        cell(d,  { width: cols[3], shade: i%2===0?MGRAY:WHITE }),
      ]}))
    ]
  });
}

// ── Challenges table ──────────────────────────────────────────────────────────
function makeChallengesTable() {
  const rows = [
    ["Gemini Flash Quota Exhaustion", "HIGH", "10,000 req/day hit with 30 concurrent tasks; 18 slices degraded to 4-69% county rate", "Obtained 2nd Gmail API key; re-ran degraded slices; all now 100%"],
    ["ECR Container Pull Timeouts", "MED", "CannotPullContainerError during ECR network congestion; random failures", "Checkpoint system preserves progress; re-submit failed slices"],
    ["Windows Encoding (cp1252)", "LOW", "Unicode characters (arrows, checkmarks) crash Python on Windows terminal", "Set PYTHONIOENCODING=utf-8; replace non-ASCII chars with ASCII"],
    ["S3 ACL Disabled", "MED", "BucketOwnerEnforced — ACL=public-read fails with AccessControlListNotSupported", "Removed all ACL calls; use 7-day presigned URLs for access"],
    ["C: Drive Space Crisis", "HIGH", "C: reached 97% full (0.4 GB free); threatened Docker and pipeline", "Cleared 4.87 GB from %TEMP%; Docker VHDX needs admin compaction"],
    ["Bulk Submit Failure (1 slice)", "LOW", "Slice 1887 failed during bulk_submit.py execution", "Manual re-submission; verified in RUNNABLE state"],
    ["S3 Extra Files (Col 13)", "MED", "493 incorrectly-uploaded files in Col13/2022/November; S3 > local", "Batch-deleted 493 files; S3 now matches local exactly (576,384)"],
    ["bounds_invalid Rate", "HIGH", "95% of non-resolved records fail due to OCR unable to parse PLSS text", "Future: OCR pre-processing (deskew, contrast); county-centroid fallback"],
  ];
  const cols = [2200, 700, 3200, 3260];
  return new Table({
    width: { size: W, type: WidthType.DXA },
    columnWidths: cols,
    rows: [
      new TableRow({ tableHeader: true, children: [
        hdrCell("Challenge", cols[0]), hdrCell("Sev.", cols[1]),
        hdrCell("Description", cols[2]), hdrCell("Resolution", cols[3])
      ]}),
      ...rows.map(([ch, sev, desc, res], i) => new TableRow({ children: [
        cell(ch,   { width: cols[0], bold: true, shade: i%2===0?MGRAY:WHITE }),
        cell(sev,  { width: cols[1], bold: true, color: sev==="HIGH"?"C00000":sev==="MED"?"7F6000":GREEN,
                     shade: sev==="HIGH"?LRED:sev==="MED"?"FFF2CC":LGREEN, align: AlignmentType.CENTER }),
        cell(desc, { width: cols[2], shade: i%2===0?MGRAY:WHITE }),
        cell(res,  { width: cols[3], shade: i%2===0?MGRAY:WHITE }),
      ]}))
    ]
  });
}

// ── Future work table ─────────────────────────────────────────────────────────
function makeFutureTable() {
  const rows = [
    ["Gemini Pro Fallback", "HIGH", "Catch ResourceExhausted exception, retry with Pro model — no code change after pipeline fix"],
    ["Multi-Key API Rotation", "HIGH", "Cycle through multiple Gemini API keys to prevent daily quota exhaustion"],
    ["OCR Pre-processing", "HIGH", "Deskewing, contrast enhancement to reduce bounds_invalid from 95% to ~50%"],
    ["County Centroid Fallback", "MED", "When PLSS fails, use county name to assign centroid lat/lon (coarser but valid)"],
    ["Stop RDS When Done", "MED", "aws rds stop-db-instance saves ~$12/month when pipeline not running"],
    ["Clean ECR Stale Images", "LOW", "12 untagged images (~4.5 GB); use aws ecr batch-delete-image"],
    ["Docker VHDX Compaction", "LOW", "Compact WSL disk to reclaim C: space (requires admin elevation)"],
    ["Spatial Index on RDS", "MED", "Add PostGIS spatial index to speed up PLSS bounding-box queries"],
    ["Well Type Classification", "LOW", "Classify oil vs gas vs water wells from permit text using Gemini"],
    ["Expand to New Collections", "LOW", "Ingest additional OSU archival materials as they become available"],
  ];
  const cols = [2600, 800, 5960];
  return new Table({
    width: { size: W, type: WidthType.DXA },
    columnWidths: cols,
    rows: [
      new TableRow({ tableHeader: true, children: [
        hdrCell("Task", cols[0]), hdrCell("Priority", cols[1]),
        hdrCell("Description", cols[2])
      ]}),
      ...rows.map(([t, p, d], i) => new TableRow({ children: [
        cell(t, { width: cols[0], bold: true, shade: i%2===0?MGRAY:WHITE }),
        cell(p, { width: cols[1], bold: true,
                  color: p==="HIGH"?"C00000":p==="MED"?"7F6000":GREEN,
                  shade: p==="HIGH"?LRED:p==="MED"?"FFF2CC":LGREEN, align: AlignmentType.CENTER }),
        cell(d, { width: cols[2], shade: i%2===0?MGRAY:WHITE }),
      ]}))
    ]
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// BUILD DOCUMENT
// ─────────────────────────────────────────────────────────────────────────────
const doc = new Document({
  numbering: {
    config: [
      { reference: "bullets",
        levels: [{ level: 0, format: LevelFormat.BULLET, text: "•",
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
      { reference: "sub-bullets",
        levels: [{ level: 0, format: LevelFormat.BULLET, text: "o",
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 1080, hanging: 360 } } } }] },
      { reference: "numbers",
        levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.",
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
    ]
  },
  styles: {
    default: {
      document: { run: { font: "Arial", size: 22 } }
    },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 32, bold: true, color: NAVY, font: "Arial" },
        paragraph: { spacing: { before: 360, after: 120 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 26, bold: true, color: BLUE, font: "Arial" },
        paragraph: { spacing: { before: 240, after: 100 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 24, bold: true, color: DGRAY, font: "Arial" },
        paragraph: { spacing: { before: 180, after: 80 },  outlineLevel: 2 } },
    ]
  },

  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1440, right: 1260, bottom: 1440, left: 1260 }
      }
    },
    headers: {
      default: new Header({ children: [
        new Paragraph({
          alignment: AlignmentType.RIGHT,
          border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: BLUE } },
          spacing: { after: 60 },
          children: [
            new TextRun({ text: "OSU Oklahoma Well Records Pipeline  |  Technical Report  |  May 2026",
              size: 18, color: DGRAY, font: "Arial" })
          ]
        })
      ]})
    },
    footers: {
      default: new Footer({ children: [
        new Paragraph({
          alignment: AlignmentType.CENTER,
          border: { top: { style: BorderStyle.SINGLE, size: 4, color: BLUE } },
          spacing: { before: 60 },
          tabStops: [{ type: "right", position: 9360 }],
          children: [
            new TextRun({ text: "OSU Well Records Pipeline  —  Confidential",
              size: 18, color: DGRAY, font: "Arial" }),
            new TextRun({ text: "\tPage ", size: 18, color: DGRAY, font: "Arial" }),
            new TextRun({ children: [PageNumber.CURRENT], size: 18, color: DGRAY, font: "Arial" }),
            new TextRun({ text: " of ", size: 18, color: DGRAY, font: "Arial" }),
            new TextRun({ children: [PageNumber.TOTAL_PAGES], size: 18, color: DGRAY, font: "Arial" }),
          ]
        })
      ]})
    },

    children: [

      // ══════════════════════════════════════════════════════
      // TITLE PAGE
      // ══════════════════════════════════════════════════════
      new Paragraph({ spacing: { after: 600 }, children: [] }),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 100 },
        children: [new TextRun({
          text: "OSU OKLAHOMA WELL RECORDS PIPELINE",
          bold: true, size: 56, color: NAVY, font: "Arial"
        })]
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER, spacing: { after: 400 },
        border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: BLUE } },
        children: [new TextRun({
          text: "Design, Implementation, Challenges & Analysis",
          size: 32, italic: true, color: BLUE, font: "Arial"
        })]
      }),
      new Paragraph({ spacing: { after: 120 }, children: [] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 80 },
        children: [new TextRun({ text: "A Comprehensive Technical Study of", size: 26, color: DGRAY, font: "Arial" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 400 },
        children: [new TextRun({ text: "Large-Scale Historical Document Processing at Cloud Scale", size: 26, color: DGRAY, font: "Arial" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 80 },
        children: [new TextRun({ text: "Prepared by:", size: 22, color: DGRAY, font: "Arial" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 60 },
        children: [new TextRun({ text: "Akshay Kumar Mundrathi", bold: true, size: 30, color: NAVY, font: "Arial" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 40 },
        children: [new TextRun({ text: "Oklahoma State University", size: 24, color: DGRAY, font: "Arial" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 400 },
        children: [new TextRun({ text: "May 2026", bold: true, size: 24, color: BLUE, font: "Arial" })] }),
      rule(BLUE),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 60 },
        children: [new TextRun({ text: "Repository: ", size: 20, color: DGRAY, font: "Arial" }),
          link("github.com/Akshaykumarmundrathi/osu-well-pipeline",
               "https://github.com/Akshaykumarmundrathi/osu-well-pipeline")] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 60 },
        children: [new TextRun({ text: "Live Map: ", size: 20, color: DGRAY, font: "Arial" }),
          link("akshaykumarmundrathi.github.io/osu-well-pipeline",
               "https://akshaykumarmundrathi.github.io/osu-well-pipeline/")] }),
      new Paragraph({ alignment: AlignmentType.CENTER,
        children: [new TextRun({ text: "S3 Map: ", size: 20, color: DGRAY, font: "Arial" }),
          link("osu-well-records.s3.amazonaws.com/viewer/well_map.html",
               "https://osu-well-records-225989338968.s3.amazonaws.com/viewer/well_map.html")] }),

      // ══════════════════════════════════════════════════════
      // TABLE OF CONTENTS
      // ══════════════════════════════════════════════════════
      new Paragraph({ children: [new PageBreak()] }),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 200 },
        children: [new TextRun({ text: "TABLE OF CONTENTS", bold: true, size: 32, color: NAVY, font: "Arial" })]
      }),
      new TableOfContents("", { hyperlink: true, headingStyleRange: "1-2" }),

      // ══════════════════════════════════════════════════════
      // 1. EXECUTIVE SUMMARY
      // ══════════════════════════════════════════════════════
      h1("1. Executive Summary"),
      para("The OSU Oklahoma Well Records Pipeline is a cloud-scale document processing system designed to digitize and georeferentiate 576,384 historical oil and gas well permit PDFs spanning 113 years of Oklahoma drilling history (1911–2024). The pipeline extracts machine-readable coordinates from hand-typed, scanned permit forms using a 4-stage AI pipeline: image conversion, OCR, AI text extraction, and PLSS-to-GPS coordinate resolution."),
      para("The system processes documents at cloud scale using AWS Batch with 30 concurrent Fargate containers, Google Cloud Vision for OCR, Google Gemini Flash 2.5 for county extraction, and a PostgreSQL PLSS database for coordinate lookups. Results are visualized through an interactive satellite map (Esri World Imagery with roads/labels overlay) hosted on both AWS S3 and GitHub Pages."),

      h2("Key Statistics as of May 22, 2026"),
      makeStatusTable(),
      nbsp(),
      para("All 18 previously-degraded slices (affected by Gemini Flash daily quota exhaustion) have been re-processed and now show 100% county extraction rates following injection of a new API key from a second Gmail account.", { italic: true, color: GREEN }),

      // ══════════════════════════════════════════════════════
      // 2. ARCHITECTURE
      // ══════════════════════════════════════════════════════
      h1("2. System Architecture"),
      para("The pipeline follows a linear Extract-Transform-Load (ETL) pattern, with each stage running independently inside an AWS Fargate container. Checkpointing ensures that any task failure or timeout can be safely re-submitted without reprocessing already-completed records."),

      h2("2.1 Infrastructure Components"),
      makeArchTable(),
      nbsp(),

      h2("2.2 Data Flow"),
      bullet("PDF ingestion: 576,384 PDFs uploaded from D: drive (ExportedFolderContents 1–13) to S3 prefix pdfs/ExportedFolderContents_N/YYYY/MM - Month/"),
      bullet("Index generation: aws/generate_index.py scans S3 and writes index/dataset_index.csv (576,384 rows)"),
      bullet("Slice creation: SLICE_SIZE=200 produces 2,705 slices; each slice is one AWS Batch job"),
      bullet("Job submission: aws/bulk_submit.py submits array jobs with JOB_INDEX_OFFSET=7 (arrayIndex + 7 = slice number)"),
      bullet("Per-slice checkpoint: processing_status.csv written to S3 after each record; allows safe re-submission"),
      bullet("Output per slice: results/slice-NNNNN/dot_coordinates.csv (coordinates), run_insights.json (stats), job_status.json (completion flag)"),
      bullet("Map generation: visualizer/merge_well_locations.py aggregates all completed slices into a GeoJSON FeatureCollection"),
      nbsp(),

      h2("2.3 Checkpoint & Resume Logic"),
      para("Each Fargate task reads its assigned slice's processing_status.csv from S3. Records with status=done are skipped. Only records in pending or failed state are processed. This means:"),
      bullet("A timed-out task can be re-submitted: it will resume from where it left off"),
      bullet("Quota-exhausted tasks can be re-submitted after quota reset: only county-failed records will be retried"),
      bullet("Duplicate job submissions are safe: the checkpoint prevents double-processing"),

      // ══════════════════════════════════════════════════════
      // 3. FOUR-STAGE PIPELINE
      // ══════════════════════════════════════════════════════
      h1("3. Four-Stage Processing Pipeline"),

      h2("Stage 1: PDF to Image & Well Dot Detection"),
      para("Each PDF page is converted to a PNG image using the pdf2image library. A U-Net convolutional neural network (checkpoint: unet_best.pth, 638 MB container) detects the red/black well location dot marker on the PLSS grid section of the form. The dot’s pixel position relative to the grid is recorded as a quadrant (NW, NE, SW, SE) of the PLSS section."),
      bullet("Model: U-Net with custom well-dot training data"),
      bullet("Input: PDF page as 300 DPI PNG"),
      bullet("Output: dot pixel coordinates, confidence score, quadrant classification"),
      bullet("Failure mode: dot not found (recorded as model_tier=failed in output)"),
      nbsp(),

      h2("Stage 2: OCR Text Extraction"),
      para("Google Cloud Vision API (v1/images:annotate) performs full-text OCR on the well permit form. The pipeline extracts:"),
      bullet("Township (T) and Range (R): PLSS grid identifiers (e.g., T14N R8W)"),
      bullet("Section number (1-36): sub-division of the township"),
      bullet("Well name and API number: identifying information"),
      bullet("Date filed: used for year/month/decade classification"),
      para("OCR accuracy is the primary bottleneck for coordinate resolution. Many historical forms (pre-1950) use faded typewriter text or handwriting, causing the bounds_invalid failure mode (PLSS text extracted but outside valid Oklahoma ranges)."),
      nbsp(),

      h2("Stage 3: AI County Extraction"),
      para("Google Gemini Flash 2.5 (model: gemini-2.5-flash) reads the raw OCR text and extracts the Oklahoma county name. This is necessary because county information appears in variable positions and formats across 113 years of permit forms."),
      bullet("API key: read from GOOGLE_API_KEY environment variable (injected from AWS Secrets Manager)"),
      bullet("Quota: 10,000 requests/day on free tier — primary bottleneck during bulk processing"),
      bullet("Fallback: Gemini Pro (paid, no daily quota) — currently requires manual failover"),
      bullet("Result: county_name field in dot_coordinates.csv"),
      nbsp(),

      h2("Stage 4: Coordinate Resolution"),
      para("The pipeline attempts multiple methods in priority order to resolve PLSS grid coordinates to GPS lat/lon:"),
      bullet("quadrant_direct: Use Township, Range, Section, and Quadrant to compute center lat/lon from PLSS grid database (RDS PostgreSQL). Resolution: ~0.25 mile accuracy. Success rate: ~48-51%."),
      bullet("rds_lookup: Broader RDS table lookup when quadrant is unavailable. Success rate: ~3%."),
      bullet("Validation: All resolved coordinates are validated against Oklahoma bounding box (lat 33.5–37.1°N, lon -103.1 to -94.4°W). Out-of-bounds coordinates are rejected."),
      bullet("Failure modes: bounds_invalid (PLSS text unreadable, 95% of failures), rds_miss (PLSS valid but not in database, 3%)"),

      // ══════════════════════════════════════════════════════
      // 4. S3 vs D: DRIVE VERIFICATION
      // ══════════════════════════════════════════════════════
      h1("4. S3 vs. D: Drive PDF Verification"),
      para("A comprehensive PDF count verification was performed on May 22, 2026 using the script visualizer/s3_vs_local_verify.py. Every collection folder, year, and month was compared between the local D: drive and AWS S3."),

      h2("4.1 Collection Summary (Post-Fix)"),
      makeS3Table(),
      nbsp(),

      h2("4.2 Discrepancy Found and Fixed: Collection 13, November 2022"),
      para("The comparison identified one discrepancy: Collection 13, November 2022 had 587 files in S3 vs 94 on the local D: drive (493 extra files in S3). Investigation revealed the extra files had a completely different filename pattern — they were incorrectly uploaded to this S3 prefix from an unknown source."),
      bullet("Local D: drive naming: 35XXXXXXXXXXXX_WELL NAME_APINO.pdf (14-digit prefix with Oklahoma state code 35)"),
      bullet("S3 extras naming: XXXXXXXXXX_WELL NAME_APINO.pdf (10-digit prefix, no state code)"),
      bullet("All 94 local files were correctly present in S3 (no missing files)"),
      bullet("Action: batch-deleted all 493 extra files from S3 using s3.delete_objects() in batches of 1000"),
      bullet("Result: S3 count for Col13/Nov 2022 reduced from 587 to 94, matching local perfectly"),
      bullet("Grand total: D: drive = S3 = 576,384 PDFs — perfect synchronization achieved"),

      h2("4.3 Upload Resume Logic"),
      para("Because the original PDF upload was performed sequentially (one collection at a time), any interruption would leave a collection partially uploaded. To resume uploading from any point:"),
      bullet("Identify the last fully-uploaded month using the s3_vs_local_comparison.csv"),
      bullet("Find the first month with local_count > s3_count (S3 missing)"),
      bullet("Resume AWS S3 sync from that collection/year/month prefix:"),
      new Paragraph({
        spacing: { after: 100 },
        children: [new TextRun({
          text: "  aws s3 sync \"D:/ExportedFolderContents (N)/YYYY/MM - Month/\" s3://osu-well-records-225989338968/pdfs/ExportedFolderContents_N/YYYY/MM - Month/",
          font: "Courier New", size: 18, color: "555555"
        })]
      }),

      // ══════════════════════════════════════════════════════
      // 5. CHALLENGES
      // ══════════════════════════════════════════════════════
      h1("5. Challenges Faced"),
      para("The following table documents all significant challenges encountered during pipeline development and deployment, along with their severity and resolution status."),
      makeChallengesTable(),
      nbsp(),

      h2("5.1 Gemini Flash Quota Exhaustion — Detailed Analysis"),
      para("The most significant operational challenge was the exhaustion of the Gemini Flash daily quota (10,000 requests/day). With 30 Fargate tasks running simultaneously, each processing 200 PDFs, the system could theoretically consume 6,000 API calls per hour — exhausting the daily quota in under 2 hours."),
      bullet("Symptom: county_name field empty or blank in dot_coordinates.csv for slices processed after quota hit"),
      bullet("Affected slices: 18 slices (70, 74, 75, 100, 105, 115, 120, 125, 130, 135, 140, 145, 150, 160, 165, 175, 180, 190)"),
      bullet("County rates during quota exhaustion: ranged from 4% (slice 190) to 69% (slice 70)"),
      bullet("Diagnosis: google.api_core.exceptions.ResourceExhausted (429) in Fargate container logs"),
      bullet("Quota reset time: ~19:28 local (logged as 07:03 UTC next day cycle)"),
      bullet("Immediate fix: new Gemini API key from second Gmail account injected into .env + AWS Secrets Manager"),
      bullet("All 18 degraded slices re-submitted and re-processed; all now show 100% county extraction"),

      // ══════════════════════════════════════════════════════
      // 6. BOTTLENECKS
      // ══════════════════════════════════════════════════════
      h1("6. Bottlenecks & Known Limits"),

      h2("6.1 Processing Throughput"),
      bullet("AWS Batch concurrency: 30 simultaneous Fargate tasks (queue limit)"),
      bullet("Average slice processing time: ~40 minutes (200 PDFs, 4 stages each)"),
      bullet("Theoretical throughput: 30 tasks × (60/40 min) = ~45 slices/hour = ~1,080 slices/day"),
      bullet("Estimated completion time for 2,705 slices: ~63 hours (~2.6 days) from start"),
      bullet("Actual wall-clock time longer due to Batch scheduling overhead and ECR pull delays"),
      nbsp(),

      h2("6.2 Gemini API Quota vs. Throughput Conflict"),
      para("The core tension in the system: processing speed demands ~6,000 Gemini calls/hour (30 tasks × 200 PDFs/task ÷ 1 hr), but the daily limit is 10,000. At full concurrency, the daily quota is exhausted in ~1.67 hours, leaving the other 22+ hours with degraded county extraction."),
      bullet("Current mitigation: 2 API keys (2 Gmail accounts) = 20,000 calls/day ≈ 3.3 hours of full throughput"),
      bullet("Recommended future fix: add try/except ResourceExhausted in county_extractor.py; fall back to Gemini Pro (paid, no daily limit)"),
      bullet("Alternative: reduce Fargate concurrency to 10 tasks to stay under 10,000/day limit"),
      nbsp(),

      h2("6.3 PLSS Coordinate Resolution Rate"),
      para("Only 51% of processed wells receive GPS coordinates. The root cause breakdown:"),
      bullet("bounds_invalid (95% of failures): OCR extracts T/R/S text but values fall outside valid Oklahoma PLSS ranges. Caused by image quality, faded ink, handwriting, or misaligned OCR bounding boxes."),
      bullet("rds_miss (3% of failures): PLSS identifiers are valid but the exact T/R/S/Q combination is not in the PostgreSQL lookup table."),
      bullet("dot_not_found (2%): U-Net failed to detect the well location dot on the page."),
      para("Improving the OCR pre-processing stage (deskewing, contrast enhancement, image super-resolution) is expected to increase coordinate resolution to 70-80%."),

      // ══════════════════════════════════════════════════════
      // 7. API LIMITS & COSTS
      // ══════════════════════════════════════════════════════
      h1("7. API Limits & Cost Analysis"),
      makeApiTable(),
      nbsp(),
      para("Note: All costs are approximate estimates based on AWS/Google Cloud published rates as of May 2026. Actual costs depend on retry frequency, data transfer, and request volume.", { italic: true, color: DGRAY }),

      // ══════════════════════════════════════════════════════
      // 8. MONITORING
      // ══════════════════════════════════════════════════════
      h1("8. Monitoring Setup"),

      h2("8.1 Automated Loop Monitoring"),
      para("The pipeline uses an autonomous Claude Code monitoring loop (invoked with /loop) that runs every 20 minutes during active processing. Each iteration:"),
      bullet("Queries AWS Batch API for RUNNING, FAILED, SUCCEEDED, and RUNNABLE job counts"),
      bullet("Identifies any newly-FAILED jobs and checks failure reason (timeout vs. ECR error vs. task error)"),
      bullet("Re-submits ECR-timeout failures as osu-retry-slice-NNN jobs"),
      bullet("Checks monitor_state.json to prevent double-submission of already-resubmitted slices"),
      bullet("Samples 10 recently-completed slices from S3 and reports county extraction rates"),
      bullet("Alerts if county rate drops below 80% (indicates quota exhaustion or key rotation needed)"),
      bullet("Reports C: drive free space (alert if below 2 GB)"),
      nbsp(),

      h2("8.2 State & Log Files"),
      bullet("D:/project_modular/monitor_log.txt — timestamped log of each monitoring check"),
      bullet("D:/project_modular/monitor_state.json — set of all resubmitted slice indices (prevents double-submit)"),
      bullet("D:/project_modular/bulk_submit.log — output of the bulk submission script"),
      bullet("D:/project_modular/quota_issue_log.txt — documents Gemini quota events"),
      bullet("D:/project_modular/degraded_slices.txt — list of slices known to have low county rates"),
      nbsp(),

      h2("8.3 Health Check Commands"),
      new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: "# Check RUNNING/FAILED counts:", font: "Courier New", size: 18, color: "555555" })] }),
      new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: "aws batch list-jobs --job-queue osu-pipeline-queue --job-status RUNNING", font: "Courier New", size: 18, color: "555555" })] }),
      new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: "# Count completed S3 slices:", font: "Courier New", size: 18, color: "555555" })] }),
      new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: "aws s3 ls s3://osu-well-records-225989338968/results/ | wc -l", font: "Courier New", size: 18, color: "555555" })] }),
      new Paragraph({ spacing: { after: 160 }, children: [new TextRun({ text: "# Check county rates on recent slices: python visualizer/merge_well_locations.py", font: "Courier New", size: 18, color: "555555" })] }),

      // ══════════════════════════════════════════════════════
      // 9. DEPLOYMENT, RUN & MAINTENANCE
      // ══════════════════════════════════════════════════════
      h1("9. Deployment, Running & Maintenance"),

      h2("9.1 One-Time Deployment"),
      para("1. Build Docker image:"),
      new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: "  docker build -t osu-pipeline . -f Dockerfile", font: "Courier New", size: 18, color: "555555" })] }),
      para("2. Authenticate and push to AWS ECR:"),
      new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: "  aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 225989338968.dkr.ecr.us-east-1.amazonaws.com", font: "Courier New", size: 18, color: "555555" })] }),
      new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: "  docker push 225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline:latest", font: "Courier New", size: 18, color: "555555" })] }),
      para("3. Set credentials in .env (never commit to git):"),
      bullet("GOOGLE_API_KEY: Gemini Flash API key"),
      bullet("GOOGLE_APPLICATION_CREDENTIALS: path to GCP service account JSON"),
      bullet("RDS_HOST, RDS_USER, RDS_PASSWORD: PostgreSQL connection details"),
      para("4. Update AWS Secrets Manager (osu-pipeline/credentials) with current gemini_api_key and gcp_service_account JSON."),
      nbsp(),

      h2("9.2 Running a New Pipeline Batch"),
      bullet("Step 1: python aws/generate_index.py — scans S3 and writes dataset_index.csv"),
      bullet("Step 2: python aws/bulk_submit.py — submits one Batch job per slice (200 PDFs each)"),
      bullet("Step 3: Monitor with /loop command in Claude Code — auto-detects and re-submits failures"),
      bullet("Step 4: python aws/merge_results.py — after all slices complete, merges into master CSV"),
      bullet("Step 5: cd visualizer && python merge_well_locations.py — refresh GeoJSON map data"),
      bullet("Step 6: python deploy_viewer.py — upload updated map to S3"),
      nbsp(),

      h2("9.3 Routine Maintenance Tasks"),
      bullet("Monthly: Refresh well map data (merge_well_locations.py + deploy_viewer.py)"),
      bullet("Monthly: Check S3 vs local sync (python visualizer/s3_vs_local_verify.py)"),
      bullet("After pipeline run: Stop RDS instance to avoid idle charges ($12/month)"),
      bullet("After pipeline run: Clean stale ECR images (aws ecr batch-delete-image)"),
      bullet("Weekly: Check C: drive free space; clear %TEMP% if below 3 GB"),
      bullet("Quarterly: Compact Docker VHDX to reclaim C: drive space (requires admin elevation)"),
      bullet("Annually: Rotate Gemini API keys; verify GCP service account has not expired"),
      nbsp(),

      h2("9.4 Re-running Degraded Slices"),
      para("When county extraction rates drop (indicates quota exhaustion), submit degraded slices as retry jobs:"),
      new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: "  python aws/submit_retry.py --slices 70,74,75,100,105 --job-def osu-pipeline-job:5", font: "Courier New", size: 18, color: "555555" })] }),
      para("The checkpoint system ensures only failed/pending county records are retried — completed stages are not repeated."),

      // ══════════════════════════════════════════════════════
      // 10. GITHUB PAGES
      // ══════════════════════════════════════════════════════
      h1("10. GitHub Pages Deployment"),

      h2("10.1 Repository & Pages Setup"),
      kv("Repository", "https://github.com/Akshaykumarmundrathi/osu-well-pipeline"),
      kv("GitHub Pages URL", "https://akshaykumarmundrathi.github.io/osu-well-pipeline/"),
      kv("Branch", "master"),
      kv("Source folder", "docs/ (contains index.html = standalone map with embedded GeoJSON)"),
      kv("Enable Pages", "Settings > Pages > Source: Deploy from branch master/docs"),
      nbsp(),

      h2("10.2 How the Standalone Map Works"),
      para("The GitHub Pages version uses well_map_standalone.html — a self-contained HTML file with the GeoJSON data embedded directly as a JavaScript constant (const INLINE_GEOJSON = {...}). This eliminates the need for a backend or S3 data fetch, making it fully functional as a static page."),
      bullet("Built by: visualizer/build_standalone.py --upload"),
      bullet("Embeds: 2,449 well features as inline JSON (1.4 MB total file size)"),
      bullet("To update: run visualizer/refresh_map.ps1 (PowerShell), which merges S3 data, builds standalone, uploads to S3, and copies to docs/index.html"),
      nbsp(),

      h2("10.3 Update Workflow"),
      new Paragraph({ numbering: { reference: "numbers", level: 0 }, spacing: { after: 80 },
        children: [new TextRun({ text: "Merge new S3 data: python visualizer/merge_well_locations.py", size: 22, font: "Arial" })] }),
      new Paragraph({ numbering: { reference: "numbers", level: 0 }, spacing: { after: 80 },
        children: [new TextRun({ text: "Build standalone: python visualizer/build_standalone.py --upload", size: 22, font: "Arial" })] }),
      new Paragraph({ numbering: { reference: "numbers", level: 0 }, spacing: { after: 80 },
        children: [new TextRun({ text: "Copy to docs/: copy visualizer\\well_map_standalone.html docs\\index.html", size: 22, font: "Arial" })] }),
      new Paragraph({ numbering: { reference: "numbers", level: 0 }, spacing: { after: 80 },
        children: [new TextRun({ text: "Commit and push: git add docs/index.html && git commit -m \"chore: refresh well map\" && git push", size: 22, font: "Arial" })] }),
      new Paragraph({ numbering: { reference: "numbers", level: 0 }, spacing: { after: 160 },
        children: [new TextRun({ text: "GitHub Pages auto-deploys within 2-5 minutes", size: 22, font: "Arial" })] }),

      // ══════════════════════════════════════════════════════
      // 11. INTERACTIVE MAP
      // ══════════════════════════════════════════════════════
      h1("11. Interactive Well Location Map"),

      h2("11.1 Map Overview"),
      para("The interactive map provides a visual interface for exploring all 2,449 verified Oklahoma well locations extracted by the pipeline. It is built with Leaflet.js v1.9.4 and Leaflet.MarkerCluster, using satellite imagery tiles from Esri World Imagery (comparable to Google Maps Satellite) with roads and labels overlay."),

      h2("11.2 Tile Layers"),
      bullet("Satellite (Esri World Imagery): Very high-resolution imagery showing individual buildings, trees, roads, and terrain. Zoom level 18+ shows field-level detail."),
      bullet("Hybrid (default): Satellite imagery + Esri World Boundaries and Places + Esri World Transportation overlay. Shows roads, highways, place names, and county boundaries over satellite imagery. Most similar to Google Maps Hybrid view."),
      bullet("Street Map (OpenStreetMap): Detailed road/building map without satellite imagery."),
      bullet("Terrain/Topo (Esri World Topo Map): USGS topographic map showing elevation contours and land features."),
      bullet("Dark mode: CartoDB Dark Matter tiles for low-light viewing."),
      nbsp(),

      h2("11.3 Features"),
      bullet("Google Maps-style teardrop pin markers: colored by decade (1910s through 2020s), sized by AI confidence (90% = large pin, <70% = small pin)"),
      bullet("MarkerCluster grouping: clusters nearby wells at low zoom levels; expands at zoom 13+ with spiderfy"),
      bullet("Quick search bar: floating overlay search for wells, counties, API numbers (debounced 300ms)"),
      bullet("Sidebar filters: county name, well name/API, collection (1-13), decade, resolution type"),
      bullet("Popup details: well name, API number, collection, year/month, county, PLSS location (Township/Range/Section), coordinates, AI confidence, resolution method, PDF link, Google Maps link, copy-coordinates button"),
      bullet("County bar chart: top 15 counties by well count with visual bars"),
      bullet("Decade legend: click to toggle visibility of any decade"),
      bullet("Fits to data bounds on load"),
      bullet("Satellite-visible cluster icons: white-outlined dark blue circles visible on bright imagery"),
      nbsp(),

      h2("11.4 Deployment URLs"),
      kv("AWS S3 (requires public policy or presigned URL)",
         "https://osu-well-records-225989338968.s3.amazonaws.com/viewer/well_map.html"),
      kv("GitHub Pages (always public)",
         "https://akshaykumarmundrathi.github.io/osu-well-pipeline/"),
      kv("S3 Standalone (presigned, 7-day)", "viewer/well_map_standalone.html (presigned URL in viewer_url.txt)"),

      // ══════════════════════════════════════════════════════
      // 12. CURRENT STATUS
      // ══════════════════════════════════════════════════════
      h1("12. Current Pipeline Status"),
      para("As of May 22, 2026 at 00:10 UTC, the pipeline is actively processing with all systems healthy."),
      bullet("30 Fargate tasks currently RUNNING (at concurrency limit)"),
      bullet("2,702 jobs RUNNABLE in AWS Batch queue (2,522 rev5 bulk + 180 retry-slice)"),
      bullet("0 new FAILED jobs in the past 6 hours (37 historical failures, all old)"),
      bullet("160 slices fully completed in S3 with job_status.json"),
      bullet("4,773 records processed; 100% county extraction; 2,449 GPS-resolved wells"),
      bullet("New Gemini API key confirmed operational (slices completed at 23:47 and 23:58 UTC show 100%)"),
      bullet("S3 vs D: drive: 576,384 PDFs — perfectly synchronized after Col13 fix"),
      bullet("C: drive: 7.4 GB free (97% used) — adequate but monitor closely"),
      bullet("Estimated completion: ~63 additional hours at 30 concurrent tasks"),

      // ══════════════════════════════════════════════════════
      // 13. FUTURE WORK
      // ══════════════════════════════════════════════════════
      h1("13. Future Work & Recommendations"),
      para("The following improvements are recommended to increase pipeline quality, reduce costs, and expand capabilities."),
      makeFutureTable(),
      nbsp(),

      h2("13.1 Highest-Priority Recommendations"),
      para("The three most impactful improvements, in priority order:"),
      bullet("1. Add Gemini Pro fallback (catch ResourceExhausted, retry with Pro model) — eliminates quota-related degradation without requiring additional API keys or key rotation logic."),
      bullet("2. OCR pre-processing (deskew + contrast enhancement) — expected to raise coordinate resolution from 51% to 70-80%, the single biggest quality improvement available."),
      bullet("3. Stop RDS and clean ECR images after pipeline completes — reduces ongoing cloud costs by ~$15/month with minimal effort."),

      // ══════════════════════════════════════════════════════
      // APPENDIX: File Structure
      // ══════════════════════════════════════════════════════
      h1("Appendix: Project File Structure"),
      new Paragraph({
        spacing: { after: 160 },
        children: [new TextRun({
          font: "Courier New", size: 18, color: "444444",
          text: [
            "D:/project_modular/",
            "  Dockerfile                     # Container build definition",
            "  .env                           # Local credentials (NEVER commit)",
            "  monitor_state.json             # Resubmitted slice index registry",
            "  monitor_log.txt                # Timestamped monitoring log",
            "  unet_best.pth                  # U-Net model checkpoint (638 MB)",
            "  aws/",
            "    bulk_submit.py               # Submit all 2,705 slice jobs",
            "    generate_index.py            # Scan S3, write dataset_index.csv",
            "    merge_results.py             # Merge all slice CSVs after completion",
            "    monitor_jobs.py              # CLI pipeline monitor",
            "  project/",
            "    main.py                      # Fargate entry point per slice",
            "    ocr/vision_api.py            # Google Vision OCR wrapper",
            "    county/prompts.py            # Gemini Flash county extraction",
            "    grid/scoring.py              # U-Net dot scoring",
            "    latlong/latlong_extractor.py # PLSS -> GPS coordinate resolution",
            "    utils/processing_status.py   # Checkpoint read/write",
            "  visualizer/",
            "    well_map.html                # Interactive satellite map (CDN tiles)",
            "    well_map_standalone.html     # Standalone map (embedded GeoJSON)",
            "    merge_well_locations.py      # S3 -> GeoJSON aggregator",
            "    build_standalone.py          # Build standalone map with embedded data",
            "    deploy_viewer.py             # Upload map to S3",
            "    s3_vs_local_verify.py        # D: drive vs S3 PDF count comparison",
            "    refresh_map.ps1              # PowerShell: merge + build + upload",
            "  docs/",
            "    index.html                   # GitHub Pages entry (= standalone map)",
          ].join("\n")
        })]
      }),

      rule(BLUE),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { before: 200 },
        children: [new TextRun({
          text: "End of Document  —  OSU Well Records Pipeline Technical Report  —  May 2026",
          size: 20, italic: true, color: DGRAY, font: "Arial"
        })]
      }),

    ] // end children
  }] // end sections
});

Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync("D:/project_modular/OSU_Pipeline_Report.docx", buf);
  console.log("Written: D:/project_modular/OSU_Pipeline_Report.docx (" + Math.round(buf.length/1024) + " KB)");
});
