"use strict";
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, LevelFormat, ExternalHyperlink,
  BorderStyle, WidthType, ShadingType, HeadingLevel,
  PageNumber, PageBreak, VerticalAlign, TableOfContents
} = require("docx");
const fs = require("fs");

// ── Dimensions ────────────────────────────────────────────────────────────────
const W = 9360; // content width DXA (US Letter − 1" left/right margins)

// ── Colors ────────────────────────────────────────────────────────────────────
const NAVY  = "1B3A6B", BLUE  = "2E75B6", LBLUE = "D6E4F0";
const MGRAY = "F2F2F2", DGRAY = "595959";
const GREEN = "375623", LGREEN = "E2EFDA";
const RED   = "843C0C", LRED   = "FCE4D6";
const WHITE = "FFFFFF", BLACK  = "000000";
const AMBER = "7F6000", LAMBER = "FFF2CC";

// ── Borders ───────────────────────────────────────────────────────────────────
const b1  = (c="AAAAAA") => ({ style: BorderStyle.SINGLE, size: 4, color: c });
const bAll = (c="AAAAAA") => ({ top:b1(c), bottom:b1(c), left:b1(c), right:b1(c) });
const noBorder = () => {
  const n = { style: BorderStyle.NONE, size: 0, color: WHITE };
  return { top:n, bottom:n, left:n, right:n };
};

// ── Cell ──────────────────────────────────────────────────────────────────────
function cell(text, {
  width=1872, bold=false, shade=null, color=BLACK,
  colspan=1, rowspan=1, align=AlignmentType.LEFT, fontSize=20, italic=false,
  mono=false
}={}) {
  return new TableCell({
    width: { size:width, type:WidthType.DXA },
    columnSpan: colspan, rowSpan: rowspan,
    borders: bAll(), margins: { top:80, bottom:80, left:130, right:130 },
    shading: shade ? { fill:shade, type:ShadingType.CLEAR } : undefined,
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({
      alignment: align,
      children: [new TextRun({
        text: String(text), bold, italic, color, size: fontSize,
        font: mono ? "Courier New" : "Arial"
      })]
    })]
  });
}
function hCell(text, width, shade=LBLUE) {
  return cell(text, { width, bold:true, shade, color:NAVY, fontSize:20 });
}
function statusCell(text, level) {
  // level: "ok" | "warn" | "err" | "fixed"
  const map = { ok:   [GREEN, LGREEN], warn: [AMBER, LAMBER],
                err:  [RED,   LRED  ], fixed:[GREEN, LGREEN] };
  const [fg, bg] = map[level] || [DGRAY, MGRAY];
  return cell(text, { width:900, bold:true, color:fg, shade:bg, align:AlignmentType.CENTER });
}

// ── Paragraphs ────────────────────────────────────────────────────────────────
function h1(text) {
  return new Paragraph({ heading:HeadingLevel.HEADING_1, pageBreakBefore:true,
    children:[new TextRun({ text, font:"Arial" })] });
}
function h2(text) {
  return new Paragraph({ heading:HeadingLevel.HEADING_2,
    children:[new TextRun({ text, font:"Arial" })] });
}
function h3(text) {
  return new Paragraph({ heading:HeadingLevel.HEADING_3,
    children:[new TextRun({ text, font:"Arial" })] });
}
function para(text, { bold=false, italic=false, color=BLACK, size=22, after=160 }={}) {
  return new Paragraph({ spacing:{ after },
    children:[new TextRun({ text, bold, italic, color, size, font:"Arial" })] });
}
function bullet(text, { bold=false, ref="bullets" }={}) {
  return new Paragraph({ numbering:{ reference:ref, level:0 }, spacing:{ after:80 },
    children:[new TextRun({ text, bold, size:22, font:"Arial" })] });
}
function numbered(text) {
  return new Paragraph({ numbering:{ reference:"numbers", level:0 }, spacing:{ after:80 },
    children:[new TextRun({ text, size:22, font:"Arial" })] });
}
function code(text) {
  return new Paragraph({ spacing:{ after:60 },
    children:[new TextRun({ text, font:"Courier New", size:18, color:"444444" })] });
}
function nbsp() { return new Paragraph({ spacing:{ after:80 }, children:[new TextRun("")] }); }
function rule(color=BLUE) {
  return new Paragraph({ spacing:{ before:120, after:120 },
    border:{ bottom:{ style:BorderStyle.SINGLE, size:6, color, space:1 } }, children:[] });
}
function kv(label, value, url=null) {
  return new Paragraph({ spacing:{ after:100 }, children:[
    new TextRun({ text: label+": ", bold:true, size:22, font:"Arial" }),
    url ? new ExternalHyperlink({ children:[new TextRun({ text:value, style:"Hyperlink", size:22, font:"Arial" })], link:url })
         : new TextRun({ text: value, size:22, font:"Arial" })
  ]});
}
function callout(text, color=LBLUE, textColor=NAVY) {
  return new Table({
    width:{ size:W, type:WidthType.DXA }, columnWidths:[W],
    rows:[new TableRow({ children:[new TableCell({
      width:{ size:W, type:WidthType.DXA },
      borders: bAll(BLUE),
      shading:{ fill:color, type:ShadingType.CLEAR },
      margins:{ top:120, bottom:120, left:200, right:200 },
      children:[new Paragraph({ children:[
        new TextRun({ text, bold:true, size:22, color:textColor, font:"Arial" })
      ]})]
    })]})],
  });
}

// ── Tables ────────────────────────────────────────────────────────────────────
function makeLibsTable() {
  const rows = [
    ["boto3",               ">=1.35",    "AWS SDK: S3, Batch, Secrets Manager"],
    ["psycopg2-binary",     ">=2.9",     "PostgreSQL RDS driver for PLSS queries"],
    ["pymupdf",             ">=1.24",    "PDF page rendering to PIL images"],
    ["opencv-python-headless",">=4.10",  "Image preprocessing (threshold, denoise)"],
    ["pillow",              ">=10.4",    "PIL image handling and format conversion"],
    ["pytesseract",         ">=0.3.10",  "Tesseract OCR Python wrapper (free OCR)"],
    ["torch + torchvision", "2.4.1/0.19.1","U-Net dot detector model (CPU-only build)"],
    ["numpy",               ">=1.26",    "Array operations for image processing"],
    ["pandas",              ">=2.0",     "CSV and DataFrame operations"],
    ["scipy",               ">=1.11",    "Spatial algorithms for nearest-neighbor grid matching"],
    ["rapidfuzz",           ">=3.10",    "Fuzzy string matching for Oklahoma county names"],
    ["google-generativeai", "0.8.3",     "Gemini Flash county name extraction"],
    ["google-cloud-vision", "3.7.4",     "Optional Vision API (opt-in via USE_VISION_API=1)"],
    ["matplotlib",          ">=3.7",     "Debug visualizations for dot detection"],
  ];
  const cols = [2600, 1500, 5260];
  return new Table({ width:{ size:W, type:WidthType.DXA }, columnWidths:cols,
    rows:[
      new TableRow({ tableHeader:true, children:[
        hCell("Library",cols[0]), hCell("Version",cols[1]), hCell("Purpose",cols[2])
      ]}),
      ...rows.map(([l,v,p],i) => new TableRow({ children:[
        cell(l,{width:cols[0],bold:true,shade:i%2?WHITE:MGRAY,mono:true}),
        cell(v,{width:cols[1],shade:i%2?WHITE:MGRAY,mono:true}),
        cell(p,{width:cols[2],shade:i%2?WHITE:MGRAY}),
      ]}))
    ]
  });
}

function makeAWSTable() {
  const rows = [
    ["S3",              "86.9 GB input PDFs + checkpoints + output CSVs", "~$2/mo storage"],
    ["AWS Batch (Fargate)","Array job execution: 2,705 tasks, 2 vCPU / 4 GB RAM each","~$8-15/run"],
    ["ECR",             "Docker image registry (638 MB osu-pipeline:latest)","~$0.10/GB/mo"],
    ["RDS PostgreSQL",  "PLSS coordinate lookup (t3.micro instance, Oklahomaplss DB)","~$12/mo *"],
    ["Secrets Manager", "DB credentials + GCP service account + Gemini API key","~$0.40/secret/mo"],
    ["CloudWatch Logs", "Container stdout/stderr for all 2,705 tasks","~$0.50/GB"],
    ["IAM",             "Task execution role with S3, Secrets, ECR permissions","Free"],
  ];
  const cols = [1800, 4500, 3060];
  return new Table({ width:{ size:W, type:WidthType.DXA }, columnWidths:cols,
    rows:[
      new TableRow({ tableHeader:true, children:[
        hCell("Service",cols[0]), hCell("Purpose",cols[1]), hCell("Est. Cost",cols[2])
      ]}),
      ...rows.map(([s,p,c],i) => new TableRow({ children:[
        cell(s,{width:cols[0],bold:true,shade:i%2?WHITE:MGRAY}),
        cell(p,{width:cols[1],shade:i%2?WHITE:MGRAY}),
        cell(c,{width:cols[2],shade:i%2?WHITE:MGRAY}),
      ]}))
    ]
  });
}

function makeCollectionTable() {
  const rows = [
    [1,"54,979","8.13 GB","1911-1925",15,180],
    [2,"46,492","8.32 GB","1926-1940",15,180],
    [3,"41,545","6.67 GB","1941-1950",10,120],
    [4,"53,988","6.58 GB","1941-1955", 7, 84],
    [5,"42,338","4.78 GB","1956-1960", 5, 60],
    [6,"53,851","5.88 GB","1961-1970",10,120],
    [7,"52,855","5.81 GB","1971-1979", 9,108],
    [8,"49,457","5.54 GB","1980-1982", 3, 36],
    [9,"49,578","6.35 GB","1983-1987", 5, 60],
    [10,"53,492","7.47 GB","1988-2000",13,156],
    [11,"50,991","8.60 GB","2001-2012",12,144],
    [12,"19,728","5.73 GB","2013-2018", 6, 72],
    [13, "7,090","7.04 GB","2019-2024", 6, 72],
  ];
  const cols = [600, 1500, 1500, 1800, 1200, 1200, 1560];
  return new Table({ width:{ size:W, type:WidthType.DXA }, columnWidths:cols,
    rows:[
      new TableRow({ tableHeader:true, children:[
        hCell("Col",cols[0]), hCell("PDF Count",cols[1]), hCell("Size on S3",cols[2]),
        hCell("Year Range",cols[3]), hCell("Years",cols[4]),
        hCell("Months",cols[5]), hCell("Status",cols[6])
      ]}),
      ...rows.map(([c,n,s,yr,y,m],i) => new TableRow({ children:[
        cell(c,  {width:cols[0],shade:i%2?WHITE:MGRAY,align:AlignmentType.CENTER}),
        cell(n,  {width:cols[1],shade:i%2?WHITE:MGRAY,bold:true,align:AlignmentType.RIGHT}),
        cell(s,  {width:cols[2],shade:i%2?WHITE:MGRAY,align:AlignmentType.RIGHT}),
        cell(yr, {width:cols[3],shade:i%2?WHITE:MGRAY,align:AlignmentType.CENTER}),
        cell(y,  {width:cols[4],shade:i%2?WHITE:MGRAY,align:AlignmentType.CENTER}),
        cell(m,  {width:cols[5],shade:i%2?WHITE:MGRAY,align:AlignmentType.CENTER}),
        statusCell("SYNCED","ok"),
      ]})),
      new TableRow({ children:[
        hCell("TOTAL",cols[0]),
        hCell("576,384",cols[1]),
        hCell("86.9 GB",cols[2]),
        hCell("1911-2024",cols[3]),
        hCell("",cols[4]), hCell("",cols[5]),
        hCell("VERIFIED",cols[6]),
      ]})
    ]
  });
}

function makeCostTable() {
  const rows = [
    ["AWS Fargate (2,705 tasks)","~$8-15","2 vCPU, 4 GB, ~40 min avg task duration"],
    ["S3 storage (86.9 GB input)","~$2/mo","Input PDFs — one-time upload"],
    ["S3 GET requests","~$3","576,384 PDF downloads per run"],
    ["RDS PostgreSQL t3.micro","~$0.50","Active only during pipeline run"],
    ["CloudWatch Logs","~$1","2,705 task log streams"],
    ["Gemini Flash API","~$5-20","Depends on free-tier exhaustion & key count"],
    ["Tesseract OCR","$0","Replaces $3,400 Google Vision API — runs in container"],
    ["Google Vision API (opt-in)","~$865/run","$1.50/1,000 pages × 576,384 PDFs — disabled by default"],
    ["TOTAL per run (Tesseract)","~$20-40","vs. ~$3,440 with Vision API"],
  ];
  const cols = [3000, 1500, 4860];
  return new Table({ width:{ size:W, type:WidthType.DXA }, columnWidths:cols,
    rows:[
      new TableRow({ tableHeader:true, children:[
        hCell("Component",cols[0]), hCell("Cost per Run",cols[1]), hCell("Notes",cols[2])
      ]}),
      ...rows.map(([c,co,n],i) => new TableRow({ children:[
        cell(c,{width:cols[0],bold:true,shade:i%2?WHITE:MGRAY}),
        cell(co,{width:cols[1],shade:i%2?WHITE:MGRAY,
                 bold:c.includes("TOTAL"),
                 color:co.includes("$0")?"375623":co.includes("3,440")?"C00000":BLACK}),
        cell(n,{width:cols[2],shade:i%2?WHITE:MGRAY}),
      ]}))
    ]
  });
}

function makeChallengesTable() {
  const rows = [
    ["Docker Disk Exhaustion","MED","Full PyTorch re-download on every code change (~500 MB); Docker VM disk filled, corrupted build cache","Split into Dockerfile.base (heavy deps, monthly) + Dockerfile (code only, 10-sec rebuild). Rebuild time: ~20 min → ~10 sec."],
    ["Wrong S3 Index Path","LOW","INDEX_KEY=dataset_index.csv instead of index/dataset_index.csv — all tasks failed immediately with NoSuchKey","Corrected INDEX_KEY in job definition rev 2. Lesson: validate S3 paths before submitting large array jobs."],
    ["Fargate Network Config Stripped","HIGH","networkConfiguration block accidentally removed. Containers had no public IP, could not pull ECR image, no CloudWatch logs — completely silent failure","Reinstated assignPublicIp: ENABLED in rev 3. Always diff new job definition JSON against last working revision."],
    ["Fargate vCPU Quota (30 default)","MED","Array job of 2,832 tasks sat RUNNABLE indefinitely. Default limit: 30 Fargate vCPUs total. At 4 vCPU/task, only 7 ran concurrently.","Reduced to 2 vCPU / 4 GB per task (30 concurrent). Raised maxvCpus to 4,096. Submitted quota increase request."],
    ["JOB_INDEX_OFFSET Missing","MED","Re-submitting slices 7-13 as array size 7 mapped indices 0-6 to wrong dataset slices, risking overwriting clean output","Added JOB_INDEX_OFFSET env var. JOB_INDEX = AWS_BATCH_JOB_ARRAY_INDEX + offset. Enables safe partial re-submission."],
    ["Gemini Flash Rate Limiting","HIGH","4 parallel workers per container fired Gemini calls simultaneously, exhausting 5 RPM free tier. County extraction fell to empty string.","Added threading.Lock() rate limiter in prompts.py with GEMINI_MIN_CALL_GAP_S (default 6.0s). Switched to Gemini Flash free tier."],
    ["Gemini Flash Daily Quota","HIGH","10,000 req/day quota exhausted in ~1.7 hrs with 30 concurrent tasks. 18 slices showed 4-69% county extraction rate.","Obtained 2nd Gmail API key (AIzaSyDc2k...). Re-ran all 18 degraded slices. All now 100%. Added to Secrets Manager."],
    ["pandas / scipy Missing","LOW","dot_extractor.py imported pandas and scipy. Both absent from requirements.txt. 180/200 test PDFs failed with ModuleNotFoundError.","Added pandas>=2.0 and scipy>=1.11 to requirements.txt. Triggered base image rebuild."],
    ["RDS Secret Malformed JSON","MED","Secrets Manager stored raw connection string instead of JSON object. Parser failed silently — PLSS resolution returned None for every record.","Re-uploaded secret as proper JSON: {host, port, dbname, user, password}. Resolution immediately began working."],
    ["Vision API Cost ($3,400/run)","HIGH","Original pipeline called Google Vision API 4-5× per PDF. At $1.50/1,000 calls, 576,384 PDFs = ~$865-3,440/run.","Replaced with Tesseract OCR (free, runs in container). Vision API retained as opt-in (USE_VISION_API=1). Savings: ~$3,400/run."],
    ["ECR Container Pull Timeouts","MED","CannotPullContainerError during ECR network congestion. Random task failures with no useful error log.","Checkpoint system allows safe re-submission. Re-submitted failed slices which succeeded on retry."],
    ["Windows Encoding (cp1252)","LOW","Python scripts printing Unicode (arrows, checkmarks) crash on Windows terminal with UnicodeEncodeError.","Set PYTHONIOENCODING=utf-8 in shell. Replace non-ASCII chars in print statements."],
    ["S3 ACL Disabled","LOW","BucketOwnerEnforced bucket — ACL=public-read fails with AccessControlListNotSupported.","Removed ACL from all put_object calls. Use 7-day presigned URLs for viewer access."],
    ["C: Drive Space Crisis","MED","C: reached 97% full (0.4 GB free), threatening Docker and pipeline ops. Docker VHDX at 8 GB.","Cleared 4.87 GB from %TEMP%. Docker VHDX compaction pending (requires admin elevation)."],
    ["S3 Extra Files (Col 13)","MED","Col13/Nov 2022 had 493 extra S3 files with different naming pattern (not on D: drive).","Batch-deleted 493 files via s3.delete_objects(). S3 now matches D: drive: 576,384 PDFs."],
    ["bounds_invalid Rate (95%)","HIGH","OCR extracts T/R/S text but values fall outside valid Oklahoma PLSS ranges — 95% of non-resolved records.","Future fix: OCR pre-processing (deskew, contrast enhancement). County-centroid fallback for coarse resolution."],
  ];
  const cols = [2100, 650, 3300, 3310];
  return new Table({ width:{ size:W, type:WidthType.DXA }, columnWidths:cols,
    rows:[
      new TableRow({ tableHeader:true, children:[
        hCell("Challenge",cols[0]), hCell("Sev.",cols[1]),
        hCell("Description",cols[2]), hCell("Resolution / Status",cols[3])
      ]}),
      ...rows.map(([ch,s,d,r],i) => new TableRow({ children:[
        cell(ch,{width:cols[0],bold:true,shade:i%2?WHITE:MGRAY}),
        statusCell(s, s==="HIGH"?"err":s==="MED"?"warn":"ok"),
        cell(d, {width:cols[2],shade:i%2?WHITE:MGRAY}),
        cell(r, {width:cols[3],shade:i%2?WHITE:MGRAY}),
      ]}))
    ]
  });
}

function makeStatusTable() {
  const rows = [
    ["Total PDFs in Collection","576,384","13 collections, 1911-2024"],
    ["Total Slices","2,705","SLICE_SIZE = 200 records/slice"],
    ["Slices Completed (S3)","160 / 391 started (41%)","job_status.json present"],
    ["Records Processed","4,773","Full 4-stage pipeline completed"],
    ["County Extraction Rate","100% (4,773/4,773)","After new Gemini API key injection"],
    ["Wells with GPS Coords","2,449 (51% of processed)","Validated Oklahoma bounding box"],
    ["Coordinate Resolution Rate","51%","bounds_invalid main cause (95% of failures)"],
    ["RUNNING Batch Jobs","30","At concurrency limit"],
    ["RUNNABLE (Queued)","2,702","2,522 rev5 bulk + 180 retry-slice"],
    ["New Gemini API Key Active","May 21, 2026","Second Gmail account, fresh 10k/day quota"],
    ["D: Drive vs S3 Sync","PERFECT MATCH","576,384 / 576,384 after Col13 cleanup"],
    ["C: Drive Free Space","7.4 GB (97% used)","Monitor; compact Docker VHDX when able"],
  ];
  const cols = [2800, 2400, 4160];
  return new Table({ width:{ size:W, type:WidthType.DXA }, columnWidths:cols,
    rows:[
      new TableRow({ tableHeader:true, children:[
        hCell("Metric",cols[0]), hCell("Value",cols[1]), hCell("Notes",cols[2])
      ]}),
      ...rows.map(([m,v,n],i) => new TableRow({ children:[
        cell(m,{width:cols[0],bold:true}),
        cell(v,{width:cols[1],shade:i%2?WHITE:MGRAY,
                bold:v.includes("100%")||v.includes("PERFECT"),
                color:v.includes("100%")||v.includes("PERFECT")?GREEN:BLACK}),
        cell(n,{width:cols[2],shade:i%2?WHITE:MGRAY}),
      ]}))
    ]
  });
}

function makeFutureTable() {
  const rows = [
    ["Add Gemini Pro Fallback","HIGH","Catch ResourceExhausted in county_extractor.py, retry with Pro model. Eliminates quota-related degradation without key rotation."],
    ["Multi-Key Gemini Rotation","HIGH","Cycle through multiple API keys to keep county extraction running through daily quota resets."],
    ["OCR Pre-processing","HIGH","Deskewing, contrast enhancement, image super-resolution to reduce bounds_invalid from 95% to ~50%. Expected coordinate resolution: 51% → 70-80%."],
    ["County Centroid Fallback","MED","When PLSS fails, assign county centroid lat/lon (coarser but valid). Would recover ~40% of currently unresolved records."],
    ["Stop RDS When Idle","MED","aws rds stop-db-instance after pipeline completes — saves ~$12/month."],
    ["Clean ECR Stale Images","LOW","12 untagged ECR images (~4.5 GB); aws ecr batch-delete-image to reclaim storage."],
    ["Docker VHDX Compaction","LOW","Compact WSL disk to reclaim C: drive space (requires admin elevation)."],
    ["Confidence Scoring","MED","Add per-field confidence scores (county, section, township, range) to output CSV for downstream quality filtering."],
    ["Automatic Retry Logic","MED","Implement re-queue logic for failed tasks with exponential backoff on S3 throttling and RDS timeouts."],
    ["Batch Gemini Calls","MED","Use Gemini batch API to reduce per-call latency and lower costs by grouping county extraction requests."],
    ["Multi-Page Scanning","LOW","Extend location extraction beyond pages 0-1; some multi-page records have PLSS data on page 3+."],
    ["Well Type Classification","LOW","Classify oil vs gas vs water wells from permit text using Gemini."],
    ["Public REST API / GIS Dashboard","LOW","Allow OSU researchers and state agencies to query the extracted dataset interactively."],
  ];
  const cols = [2700, 800, 5860];
  return new Table({ width:{ size:W, type:WidthType.DXA }, columnWidths:cols,
    rows:[
      new TableRow({ tableHeader:true, children:[
        hCell("Task",cols[0]), hCell("Priority",cols[1]), hCell("Description",cols[2])
      ]}),
      ...rows.map(([t,p,d],i) => new TableRow({ children:[
        cell(t,{width:cols[0],bold:true,shade:i%2?WHITE:MGRAY}),
        statusCell(p, p==="HIGH"?"err":p==="MED"?"warn":"ok"),
        cell(d,{width:cols[2],shade:i%2?WHITE:MGRAY}),
      ]}))
    ]
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// DOCUMENT
// ─────────────────────────────────────────────────────────────────────────────
const doc = new Document({
  numbering: { config: [
    { reference:"bullets",
      levels:[{ level:0, format:LevelFormat.BULLET, text:"•", alignment:AlignmentType.LEFT,
                style:{ paragraph:{ indent:{ left:720, hanging:360 } } } }] },
    { reference:"sub-bullets",
      levels:[{ level:0, format:LevelFormat.BULLET, text:"-", alignment:AlignmentType.LEFT,
                style:{ paragraph:{ indent:{ left:1080, hanging:360 } } } }] },
    { reference:"numbers",
      levels:[{ level:0, format:LevelFormat.DECIMAL, text:"%1.", alignment:AlignmentType.LEFT,
                style:{ paragraph:{ indent:{ left:720, hanging:360 } } } }] },
  ]},
  styles: {
    default: { document: { run:{ font:"Arial", size:22 } } },
    paragraphStyles: [
      { id:"Heading1", name:"Heading 1", basedOn:"Normal", next:"Normal", quickFormat:true,
        run:{ size:32, bold:true, color:NAVY, font:"Arial" },
        paragraph:{ spacing:{ before:360, after:120 }, outlineLevel:0 } },
      { id:"Heading2", name:"Heading 2", basedOn:"Normal", next:"Normal", quickFormat:true,
        run:{ size:26, bold:true, color:BLUE, font:"Arial" },
        paragraph:{ spacing:{ before:240, after:100 }, outlineLevel:1 } },
      { id:"Heading3", name:"Heading 3", basedOn:"Normal", next:"Normal", quickFormat:true,
        run:{ size:23, bold:true, color:DGRAY, font:"Arial" },
        paragraph:{ spacing:{ before:160, after:80 }, outlineLevel:2 } },
    ]
  },
  sections: [{ properties:{
    page:{ size:{ width:12240, height:15840 },
           margin:{ top:1440, right:1260, bottom:1440, left:1260 } }
  },
  headers:{ default: new Header({ children:[
    new Paragraph({ alignment:AlignmentType.RIGHT,
      border:{ bottom:{ style:BorderStyle.SINGLE, size:4, color:BLUE } },
      spacing:{ after:60 },
      children:[new TextRun({ text:"OSU Oklahoma Well Records Pipeline  |  Technical Report  |  May 2026",
        size:18, color:DGRAY, font:"Arial" })]
    })
  ]})},
  footers:{ default: new Footer({ children:[
    new Paragraph({ alignment:AlignmentType.CENTER,
      border:{ top:{ style:BorderStyle.SINGLE, size:4, color:BLUE } },
      spacing:{ before:60 },
      children:[
        new TextRun({ text:"OSU Well Records Pipeline  —  Confidential", size:18, color:DGRAY, font:"Arial" }),
        new TextRun({ text:"\t", size:18, font:"Arial" }),
        new TextRun({ text:"Page ", size:18, color:DGRAY, font:"Arial" }),
        new TextRun({ children:[PageNumber.CURRENT], size:18, color:DGRAY, font:"Arial" }),
        new TextRun({ text:" of ", size:18, color:DGRAY, font:"Arial" }),
        new TextRun({ children:[PageNumber.TOTAL_PAGES], size:18, color:DGRAY, font:"Arial" }),
      ]
    })
  ]})},
  children: [

    // ══ TITLE PAGE ══════════════════════════════════════════
    new Paragraph({ spacing:{ after:600 }, children:[] }),
    new Paragraph({ alignment:AlignmentType.CENTER, spacing:{ after:80 },
      children:[new TextRun({ text:"OSU OKLAHOMA WELL RECORDS PIPELINE",
        bold:true, size:56, color:NAVY, font:"Arial" })] }),
    new Paragraph({ alignment:AlignmentType.CENTER, spacing:{ after:80 },
      border:{ bottom:{ style:BorderStyle.SINGLE, size:8, color:BLUE } },
      children:[new TextRun({ text:"Comprehensive Technical Report",
        size:36, italic:true, color:BLUE, font:"Arial" })] }),
    new Paragraph({ alignment:AlignmentType.CENTER, spacing:{ after:80 },
      children:[new TextRun({ text:"Design, Implementation, Challenges & Analysis",
        size:28, color:DGRAY, font:"Arial" })] }),
    new Paragraph({ spacing:{ after:300 }, children:[] }),
    new Paragraph({ alignment:AlignmentType.CENTER, spacing:{ after:60 },
      children:[new TextRun({ text:"Prepared by:", size:22, color:DGRAY, font:"Arial" })] }),
    new Paragraph({ alignment:AlignmentType.CENTER, spacing:{ after:60 },
      children:[new TextRun({ text:"Akshay Kumar Mundrathi", bold:true, size:32, color:NAVY, font:"Arial" })] }),
    new Paragraph({ alignment:AlignmentType.CENTER, spacing:{ after:40 },
      children:[new TextRun({ text:"Oklahoma State University", size:26, color:DGRAY, font:"Arial" })] }),
    new Paragraph({ alignment:AlignmentType.CENTER, spacing:{ after:300 },
      children:[new TextRun({ text:"May 2026", bold:true, size:26, color:BLUE, font:"Arial" })] }),
    rule(BLUE),
    new Paragraph({ alignment:AlignmentType.CENTER, spacing:{ after:40 },
      children:[new TextRun({ text:"576,384 PDFs  |  86.9 GB  |  1911-2024  |  13 Collections",
        bold:true, size:24, color:NAVY, font:"Arial" })] }),
    new Paragraph({ alignment:AlignmentType.CENTER, spacing:{ after:60 },
      children:[new TextRun({ text:"AWS Batch  ·  Google Vision  ·  Gemini Flash  ·  U-Net CNN  ·  PostgreSQL PLSS",
        size:20, color:DGRAY, font:"Arial" })] }),
    rule(BLUE),
    new Paragraph({ alignment:AlignmentType.CENTER, spacing:{ after:40 },
      children:[new TextRun({ text:"Repository: ", size:20, color:DGRAY, font:"Arial" }),
        new ExternalHyperlink({ children:[new TextRun({ text:"github.com/Akshaykumarmundrathi/osu-well-pipeline", style:"Hyperlink", size:20, font:"Arial" })],
          link:"https://github.com/Akshaykumarmundrathi/osu-well-pipeline" })] }),
    new Paragraph({ alignment:AlignmentType.CENTER, spacing:{ after:40 },
      children:[new TextRun({ text:"Live Map: ", size:20, color:DGRAY, font:"Arial" }),
        new ExternalHyperlink({ children:[new TextRun({ text:"akshaykumarmundrathi.github.io/osu-well-pipeline", style:"Hyperlink", size:20, font:"Arial" })],
          link:"https://akshaykumarmundrathi.github.io/osu-well-pipeline/" })] }),

    // ══ TABLE OF CONTENTS ════════════════════════════════════
    new Paragraph({ children:[new PageBreak()] }),
    new Paragraph({ alignment:AlignmentType.CENTER, spacing:{ after:200 },
      children:[new TextRun({ text:"TABLE OF CONTENTS", bold:true, size:32, color:NAVY, font:"Arial" })] }),
    new TableOfContents("", { hyperlink:true, headingStyleRange:"1-2" }),

    // ══ 1. SETUP & REQUIREMENTS ══════════════════════════════
    h1("1. Setup & Requirements"),

    h2("1.1 Project Overview"),
    para("Oklahoma State University (OSU) digitized historical well records spanning 1911–2024. The pipeline ingests 576,384 PDFs (86.9 GB) organized across 13 archival collections, extracts structured fields via OCR and machine learning, and resolves geographic coordinates for each well location using the Public Land Survey System (PLSS) database."),
    para("The goal is to convert unstructured scanned documents — many handwritten or typewritten — into a fully queryable dataset with GPS coordinates, county assignments, and PLSS location data for every well record. Results are published to an interactive satellite map accessible via GitHub Pages and AWS S3."),
    nbsp(),

    h2("1.2 AWS Services Used"),
    makeAWSTable(),
    para("* Stop RDS when the pipeline is not running to avoid unnecessary charges.", { italic:true, color:DGRAY, after:80 }),
    nbsp(),

    h2("1.3 Python Libraries"),
    makeLibsTable(),
    nbsp(),

    h2("1.4 System Requirements"),
    bullet("Docker Desktop — build base and app images locally"),
    bullet("AWS CLI v2 — configured with ECR push permissions"),
    bullet("Python 3.11 — local development and testing"),
    bullet("Git — version control (GitHub: Akshaykumarmundrathi/osu-well-pipeline)"),
    bullet("Local disk: 10+ GB free for Docker layer cache"),
    bullet("Tesseract OCR binary — installed via apt in the Docker image (tesseract-ocr-eng)"),
    bullet("Node.js 22 — required to regenerate this DOCX report via make_report.js"),
    nbsp(),

    // ══ 2. DESIGN & DATA FLOW ════════════════════════════════
    h1("2. Design & Data Flow"),

    h2("2.1 High-Level Architecture"),
    para("The pipeline is structured as an AWS Batch array job with up to 2,705 tasks. Each task processes a contiguous slice of 200 records from the 576,384-record master index. Results are written to per-task CSV files on S3, checkpointed continuously, and aggregated into a GeoJSON FeatureCollection for map visualization."),
    nbsp(),

    h2("2.2 Input Data Structure"),
    para("Each of the 13 S3 collections mirrors the D: drive folder hierarchy:"),
    code("D:/ExportedFolderContents (N)/YYYY/MM - Month/*.pdf"),
    code("s3://osu-well-records-225989338968/pdfs/ExportedFolderContents_N/YYYY/MM - Month/*.pdf"),
    nbsp(),
    para("A master index CSV maps every PDF to its collection, year, month, and S3 path:"),
    code("s3://osu-well-records-225989338968/index/dataset_index.csv  (576,384 rows)"),
    nbsp(),
    makeCollectionTable(),
    nbsp(),

    h2("2.3 Docker Image Strategy — Two-Stage Build"),
    para("Heavy Python dependencies are isolated in a base image that changes rarely, dramatically reducing iterative rebuild times:"),
    nbsp(),
    h3("Dockerfile.base (rebuilt ~monthly, ~2 GB)"),
    code("  Base:  python:3.11-slim"),
    code("  APT:   libgl1, libglib2.0-0, tesseract-ocr, tesseract-ocr-eng"),
    code("  pip:   all requirements.txt dependencies"),
    code("  pip:   torch 2.4.1 + torchvision 0.19.1 (CPU-only index URL)"),
    nbsp(),
    h3("Dockerfile (app image, ~10 second rebuild)"),
    code("  FROM:  225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline-base:latest"),
    code("  COPY:  project/ source code, run_batch_job.py, unet_best.pth"),
    code("  ENV:   UNET_CHECKPOINT, OUTPUT_ROOT"),
    nbsp(),
    para("This split reduced iterative rebuild time from ~20 minutes (with PyTorch re-download) to ~10 seconds. The final app image pushed to ECR as osu-pipeline:latest is 638 MB.", { color:GREEN }),
    nbsp(),

    h2("2.4 Per-PDF Extraction Pipeline — Four Stages"),
    h3("Stage 1 — County Extraction (Gemini Flash 2.5)"),
    para("Gemini Flash 2.5 reads the page 0 image and returns a county name as free text. The result is fuzzy-matched against the 77 Oklahoma county names using rapidfuzz (threshold >= 85) to normalize variations like 'Washington Co.' or misspellings."),
    bullet("API key: read from GOOGLE_API_KEY environment variable (injected from AWS Secrets Manager at task startup)"),
    bullet("Quota: 10,000 requests/day on free tier — primary operational bottleneck at scale"),
    bullet("Rate limiter: threading.Lock() with GEMINI_MIN_CALL_GAP_S (default 6.0s) prevents concurrent calls"),
    nbsp(),
    h3("Stage 2 — Location Extraction (OCR / Regex)"),
    para("Tesseract OCR scans page 0 (and page 1 as fallback) for Public Land Survey System (PLSS) keywords: township, range, and section numbers. Regex patterns and token search extract T/R/S values."),
    bullet("Tesseract OCR: free, runs in container — replaces Google Vision API (see Challenge #10)"),
    bullet("Vision API retained as opt-in via USE_VISION_API=1 environment variable for quality comparison"),
    nbsp(),
    h3("Stage 3 — Grid / Dot Detection (U-Net CNN)"),
    para("A trained U-Net model (unet_best.pth, baked into Docker image) identifies dot positions on PLSS plat map grids. scipy spatial nearest-neighbor matching maps each dot to its grid cell coordinates, producing a quadrant assignment (NW, NE, SW, SE of the PLSS section)."),
    nbsp(),
    h3("Stage 4 — LatLong Resolution (PostgreSQL RDS)"),
    para("Township, range, section, and quadrant values query the PLSS coordinate table in the Oklahomaplss PostgreSQL database. Results are validated against the Oklahoma bounding box (lat 33.5-37.1°N, lon -103.1 to -94.4°W)."),
    bullet("quadrant_direct: T/R/S/Q → center lat/lon. ~48-51% success rate."),
    bullet("rds_lookup: broader RDS lookup when quadrant unavailable. ~3% success rate."),
    bullet("bounds_invalid: PLSS text extracted but falls outside Oklahoma range. 95% of failures."),
    nbsp(),

    h2("2.5 Checkpoint & Resume Design"),
    para("run_batch_job.py implements three independent resilience layers:"),
    bullet("Checkpoint download: fetches processing_status.csv from S3 before processing begins. Rows marked done are skipped."),
    bullet("Periodic upload thread: background thread uploads results to S3 every 300 seconds — at most 5 minutes of work lost on crash."),
    bullet("SIGTERM handler: catches Fargate's pre-kill signal (30 seconds before SIGKILL), terminates subprocess, performs final S3 upload."),
    para("This means any failed or timed-out task can be safely re-submitted — it resumes from where it left off without double-processing."),
    nbsp(),

    h2("2.6 Array Job Offset Pattern"),
    para("AWS Batch array jobs always start their index at 0. When re-submitting only a subset of slices, a naive re-submission would process the wrong data. The solution is the JOB_INDEX_OFFSET environment variable:"),
    code("  JOB_INDEX = int(AWS_BATCH_JOB_ARRAY_INDEX) + int(JOB_INDEX_OFFSET)"),
    para("Current deployment: JOB_INDEX_OFFSET=7 (all slices start from index 7 in the dataset index). For re-submitting slices 15-20, set offset=15 and array size=6."),
    nbsp(),

    // ══ 3. S3 VS D: DRIVE VERIFICATION ══════════════════════
    h1("3. S3 vs. D: Drive PDF Verification"),
    para("A comprehensive PDF count verification was performed on May 22, 2026 using visualizer/s3_vs_local_verify.py. Every collection folder, year, and month was compared between D: drive and S3."),
    nbsp(),
    makeCollectionTable(),
    nbsp(),

    h2("3.1 Discrepancy Found & Fixed: Collection 13, November 2022"),
    callout("ISSUE RESOLVED: 493 extra S3 files in Col13/2022/November — deleted May 22, 2026. D: drive and S3 now in perfect sync: 576,384 PDFs each."),
    nbsp(),
    para("Investigation findings:"),
    bullet("Local D: drive (94 files): naming pattern 35XXXXXXXXXXXX_WELL NAME_APINO.pdf (14-digit prefix with Oklahoma state code 35)"),
    bullet("S3 extra files (493): naming pattern XXXXXXXXXX_WELL NAME_APINO.pdf (10-digit prefix, no state code) — clearly a different upload source"),
    bullet("All 94 local files were correctly present in S3; no legitimate files were lost"),
    bullet("Action: batch-deleted 493 files via s3.delete_objects() in groups of 1,000"),
    nbsp(),

    h2("3.2 Upload Resume Logic"),
    para("Because the original PDF upload was performed sequentially, any interruption leaves a collection partially uploaded. To resume:"),
    numbered("Identify the first month with local_count > s3_count in s3_vs_local_comparison.csv"),
    numbered("Resume AWS S3 sync from that collection/year/month prefix:"),
    code("  aws s3 sync \"D:/ExportedFolderContents (N)/YYYY/MM - Month/\" \\"),
    code("    s3://osu-well-records-225989338968/pdfs/ExportedFolderContents_N/YYYY/MM%20-%20Month/"),
    numbered("Run python visualizer/s3_vs_local_verify.py to confirm sync is complete"),
    nbsp(),

    // ══ 4. CHALLENGES & SOLUTIONS ════════════════════════════
    h1("4. Challenges, Bottlenecks & Solutions"),
    para("The following table consolidates all 16 significant challenges encountered across development, deployment, and operations. Issues from both the original pipeline build and the current bulk-processing run are included."),
    nbsp(),
    makeChallengesTable(),
    nbsp(),

    h2("4.1 Gemini Flash Quota — Detailed Timeline"),
    bullet("Slices 70-190 ran concurrently with 30 Fargate tasks, each calling Gemini for every PDF"),
    bullet("Daily quota of 10,000 requests exhausted in ~1.7 hours (30 tasks × 200 PDFs × ~1 call/PDF = 6,000/hr)"),
    bullet("Symptom: county_name blank/empty in dot_coordinates.csv; slices showed 4-69% extraction rate"),
    bullet("Diagnosis: google.api_core.exceptions.ResourceExhausted (HTTP 429) in Fargate container CloudWatch logs"),
    bullet("Fix: new API key from second Gmail account injected into .env + Secrets Manager osu-pipeline/credentials"),
    bullet("All 18 affected slices re-submitted and re-processed; all now show 100% county extraction"),
    nbsp(),

    h2("4.2 Tesseract vs. Google Vision API — Key Decision"),
    callout("Replacing Google Vision API with Tesseract OCR saved ~$3,400 per pipeline run. Vision API is retained as opt-in for quality testing.", LGREEN, GREEN),
    nbsp(),
    bullet("Original: Google Vision API called 4-5× per PDF. At $1.50/1,000 calls × ~2.27M calls = ~$3,400/run"),
    bullet("Replacement: Tesseract OCR runs inside the Docker container at zero marginal cost"),
    bullet("Interface preserved: _FakeAnnotation / _FakePoly / _FakeVertex duck-typing means all downstream extractors required zero code changes"),
    bullet("SHA-256 disk cache eliminates repeat OCR on identical images within a task"),
    bullet("Quality trade-off: Tesseract accuracy lower on very faded or handwritten text; Vision API still available via USE_VISION_API=1 for quality benchmarking"),
    nbsp(),

    // ══ 5. MONITORING ════════════════════════════════════════
    h1("5. Monitoring Setup"),

    h2("5.1 Automated Loop Monitoring"),
    para("An autonomous Claude Code monitoring loop (triggered with /loop) runs every 20 minutes during active processing. Each iteration performs:"),
    bullet("Query AWS Batch API for RUNNING, FAILED, SUCCEEDED, RUNNABLE job counts"),
    bullet("Identify newly-FAILED jobs and classify failure reason (ECR timeout vs. task error vs. quota)"),
    bullet("Re-submit ECR-timeout failures as osu-retry-slice-NNN jobs"),
    bullet("Check monitor_state.json to prevent double-submission of already-resubmitted slices"),
    bullet("Sample 10 recently-completed slices, report county extraction rates (alert if < 80%)"),
    bullet("Report C: drive free space (alert if < 2 GB)"),
    nbsp(),

    h2("5.2 State & Log Files"),
    bullet("D:/project_modular/monitor_log.txt — timestamped log of each monitoring check result"),
    bullet("D:/project_modular/monitor_state.json — set of all resubmitted slice indices (prevents double-submit)"),
    bullet("D:/project_modular/bulk_submit.log — bulk submission script output (2,495 new + 209 existing submitted)"),
    bullet("D:/project_modular/quota_issue_log.txt — documents Gemini quota events and reset times"),
    bullet("D:/project_modular/degraded_slices.txt — list of slices with low county rates (all now fixed)"),
    nbsp(),

    h2("5.3 Health Check Commands"),
    code("# Count RUNNING jobs:"),
    code("aws batch list-jobs --job-queue osu-pipeline-queue --job-status RUNNING --query length(jobSummaryList)"),
    nbsp(),
    code("# Count completed S3 slices (have job_status.json):"),
    code("python -c \"import boto3,sys; s3=boto3.client('s3'); [print(p) for p in ...]\""),
    nbsp(),
    code("# Check county rates on last 10 completed slices:"),
    code("python visualizer/merge_well_locations.py"),
    nbsp(),
    code("# Full S3 vs D: drive verification:"),
    code("python visualizer/s3_vs_local_verify.py"),
    nbsp(),

    // ══ 6. DEPLOYMENT & MAINTENANCE ═════════════════════════
    h1("6. Deployment, Running & Maintenance"),

    h2("6.1 First-Time Deployment"),
    numbered("Build base image (heavy deps, ~20 min, do once):"),
    code("  docker build -t osu-pipeline-base -f Dockerfile.base ."),
    code("  docker push 225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline-base:latest"),
    numbered("Build app image (~10 sec per code change):"),
    code("  docker build -t osu-pipeline ."),
    code("  aws ecr get-login-password | docker login --username AWS --password-stdin 225989338968.dkr.ecr.us-east-1.amazonaws.com"),
    code("  docker push 225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline:latest"),
    numbered("Populate D:/project_modular/.env with credentials (NEVER commit to git):"),
    bullet("GOOGLE_API_KEY, GOOGLE_APPLICATION_CREDENTIALS, RDS_HOST/USER/PASSWORD/DBNAME", { ref:"sub-bullets" }),
    numbered("Update AWS Secrets Manager osu-pipeline/credentials with current gemini_api_key and gcp_service_account JSON"),
    nbsp(),

    h2("6.2 Running a New Pipeline Batch"),
    numbered("Generate index: python aws/generate_index.py  (scans S3, writes dataset_index.csv)"),
    numbered("Submit jobs: python aws/bulk_submit.py  (one Batch job per slice, JOB_INDEX_OFFSET=7)"),
    numbered("Monitor: /loop command in Claude Code — auto-detects and re-submits failures every 20 min"),
    numbered("Merge results: python aws/merge_results.py  (after all slices complete, creates master CSV)"),
    numbered("Refresh map: cd visualizer && python merge_well_locations.py && python deploy_viewer.py"),
    nbsp(),

    h2("6.3 Routine Maintenance"),
    bullet("Monthly: refresh well map (merge_well_locations.py + deploy_viewer.py + copy to docs/)"),
    bullet("Monthly: verify S3 vs D: drive sync (s3_vs_local_verify.py)"),
    bullet("After pipeline run: stop RDS (saves ~$12/month)"),
    bullet("After pipeline run: clean stale ECR images (aws ecr batch-delete-image)"),
    bullet("Weekly during active run: check C: drive free space; clear %TEMP% if < 3 GB"),
    bullet("As needed: compact Docker VHDX (requires admin elevation — WSL disk management)"),
    bullet("Annually: rotate Gemini API keys, verify GCP service account has not expired"),
    nbsp(),

    h2("6.4 Re-running Degraded Slices"),
    para("When county extraction rates drop below 80% (Gemini quota hit), identify the affected slice indices and re-submit after the quota resets (~24 hours or after injecting a new API key):"),
    code("  aws batch submit-job --job-name osu-retry-slice-NNN \\"),
    code("    --job-queue osu-pipeline-queue --job-definition osu-pipeline-job:5 \\"),
    code("    --container-overrides '{\"environment\":[{\"name\":\"SLICE_INDEX\",\"value\":\"NNN\"}]}'"),
    para("The checkpoint system ensures only failed/pending county records are retried — completed stages are not re-run."),
    nbsp(),

    h2("6.5 Pitfalls for New Operators"),
    callout("Read this section before running the pipeline for the first time — these caused the most lost time.", LRED, RED),
    nbsp(),
    bullet("NEVER omit networkConfiguration.assignPublicIp: ENABLED in Fargate job definitions. Containers without a public IP cannot pull ECR images and produce no logs — the failure is completely silent."),
    bullet("Always set JOB_INDEX_OFFSET when re-submitting a partial array job. Without it, tasks process the wrong dataset slices and may overwrite clean output."),
    bullet("Diff the job definition JSON against the last working revision before registering a new version. One missing block can silently break all tasks."),
    bullet("Stop the RDS instance when the pipeline is not running. An idle t3.micro costs ~$12/month."),
    bullet("PyTorch CPU-only wheels must be installed via --index-url https://download.pytorch.org/whl/cpu. Without this, pip pulls 2+ GB CUDA wheels that exhaust disk space."),
    bullet("The index CSV must be at index/dataset_index.csv, not the bucket root."),
    bullet("Tesseract requires tesseract-ocr-eng as an apt package. It cannot be installed via pip."),
    bullet("Monitor Gemini quota continuously during bulk runs. With 30 concurrent tasks, 10,000/day is exhausted in ~1.7 hours. Rotate keys or reduce concurrency."),
    nbsp(),

    // ══ 7. INTERACTIVE MAP ══════════════════════════════════
    h1("7. Interactive Well Location Map"),

    h2("7.1 Map Overview & Satellite Quality"),
    para("The interactive map visualizes 2,449 verified Oklahoma well locations using Leaflet.js v1.9.4 with Esri World Imagery satellite tiles — the same source used by ArcGIS Online and comparable in resolution to Google Maps Satellite. At zoom level 17-18, individual roads, buildings, trees, and terrain features are clearly visible, allowing precise field-level well location verification."),
    nbsp(),

    h2("7.2 Tile Layers Available"),
    bullet("Hybrid (default): Esri World Imagery satellite + Esri World Boundaries/Places + Esri World Transportation. Shows roads, highways, county boundaries, and place names over high-resolution satellite imagery. Closest to Google Maps Hybrid view."),
    bullet("Satellite: Esri World Imagery only. Zoom 18+ shows individual buildings, trees, field boundaries. No label clutter."),
    bullet("Street Map: OpenStreetMap. Detailed roads, building outlines, addresses."),
    bullet("Terrain / Topo: Esri World Topo Map (USGS). Topographic contour lines, elevation data, terrain features."),
    bullet("Dark: CartoDB Dark Matter. Low-light viewing; good for decade-color pattern analysis."),
    nbsp(),

    h2("7.3 Map Features"),
    bullet("Google Maps-style teardrop pins: decade-colored (1910s-2020s), sized by AI confidence (90%+ = large pin)"),
    bullet("Satellite-visible cluster icons: white-outlined dark blue circles with well count"),
    bullet("Clusters expand at zoom 13; spiderfy at max zoom for densely packed wells"),
    bullet("Floating quick-search bar: real-time filter by well name, county, or API number (300ms debounce)"),
    bullet("Sidebar filters: county, well name, collection (1-13), decade, coordinate resolution method"),
    bullet("Popup: well name, API number, collection, year/month filed, county, PLSS (T/R/S), coordinates, AI confidence, PDF link, Google Maps link, copy-coordinates button"),
    bullet("Top-15 county bar chart in sidebar"),
    bullet("Decade legend with click-to-toggle visibility"),
    nbsp(),

    h2("7.4 Deployment URLs"),
    kv("AWS S3 (online map, live data)", "https://osu-well-records-225989338968.s3.amazonaws.com/viewer/well_map.html",
       "https://osu-well-records-225989338968.s3.amazonaws.com/viewer/well_map.html"),
    kv("GitHub Pages (always public, embedded data)", "https://akshaykumarmundrathi.github.io/osu-well-pipeline/",
       "https://akshaykumarmundrathi.github.io/osu-well-pipeline/"),
    nbsp(),

    h2("7.5 Updating the Map"),
    numbered("Run: python visualizer/merge_well_locations.py  (aggregates all S3 results into GeoJSON)"),
    numbered("Run: python visualizer/build_standalone.py --upload  (embeds GeoJSON in HTML, uploads to S3)"),
    numbered("Copy: copy visualizer\\well_map_standalone.html docs\\index.html"),
    numbered("Commit: git add docs/index.html && git commit -m \"chore: refresh well map\" && git push"),
    para("GitHub Pages auto-deploys within 2-5 minutes of the push.", { italic:true, color:DGRAY }),
    nbsp(),

    // ══ 8. CURRENT STATUS ════════════════════════════════════
    h1("8. Current Pipeline Status"),
    para("Status as of May 22, 2026 at 00:10 UTC. All systems operational."),
    nbsp(),
    makeStatusTable(),
    nbsp(),
    callout("New Gemini API key (second Gmail account) injected May 21, 2026. County extraction confirmed at 100% on all slices completed after key injection. All 18 previously-degraded slices re-processed successfully.", LGREEN, GREEN),
    nbsp(),

    // ══ 9. ANALYSIS & VALUE ══════════════════════════════════
    h1("9. Analysis, Value & Cost Summary"),

    h2("9.1 Pipeline Value"),
    para("The pipeline transforms 576,384 handwritten and typewritten Oklahoma oil and gas well records (1911-2024, 86.9 GB) into a structured, geographically-referenced dataset. Key downstream applications:"),
    bullet("GIS mapping of historical well locations across all 77 Oklahoma counties"),
    bullet("Environmental impact analysis — proximity to water bodies, fault lines, and populated areas"),
    bullet("Oil and gas production trend research spanning 113 years"),
    bullet("Regulatory compliance audits for historical well permits"),
    bullet("Academic research on land use and resource extraction patterns"),
    bullet("Cross-reference integration with USGS and EPA databases for environmental risk scoring"),
    nbsp(),

    h2("9.2 Cost Summary"),
    makeCostTable(),
    nbsp(),

    // ══ 10. FUTURE WORK ══════════════════════════════════════
    h1("10. Future Work & Recommendations"),
    makeFutureTable(),
    nbsp(),

    h2("10.1 Top Priority Actions"),
    bullet("1. Add Gemini Pro fallback (catch ResourceExhausted, retry with Pro) — eliminates quota-related degradation, requires only one code change in county_extractor.py."),
    bullet("2. OCR pre-processing (deskew + contrast enhancement) — expected to raise coordinate resolution from 51% to 70-80%, the single biggest quality improvement available."),
    bullet("3. Stop RDS and clean ECR images immediately after pipeline completes — reduces ongoing cloud costs by ~$15/month with minimal effort."),
    nbsp(),

    h2("10.2 Research Directions"),
    bullet("Vector embeddings of well descriptions for semantic similarity search across 113 years of records"),
    bullet("Fine-tuned vision model to replace Gemini for county/location extraction — eliminates API dependency and quota constraints"),
    bullet("Incremental ingestion pipeline for newly-digitized records as OSU continues the digitization effort"),
    bullet("Public REST API or GIS dashboard for OSU researchers and state agencies to query the dataset interactively"),
    bullet("Automated anomaly detection to flag records where extracted coordinates fall outside Oklahoma state boundaries"),
    nbsp(),

    // ══ APPENDIX ═════════════════════════════════════════════
    h1("Appendix: Project File Structure"),
    code("D:/project_modular/"),
    code("  Dockerfile                     # App image (code only, 10-sec rebuild)"),
    code("  Dockerfile.base                # Base image (heavy deps, monthly rebuild)"),
    code("  requirements.txt               # Python dependencies"),
    code("  .env                           # Local credentials (NEVER commit to git)"),
    code("  make_report.js                 # Regenerate this DOCX report (node make_report.js)"),
    code("  OSU_Pipeline_Report.docx       # This report"),
    code("  monitor_state.json             # Resubmitted slice index registry"),
    code("  monitor_log.txt                # Timestamped monitoring log"),
    code("  unet_best.pth                  # U-Net model checkpoint"),
    code("  aws/"),
    code("    bulk_submit.py               # Submit all 2,705 slice jobs to Batch"),
    code("    generate_index.py            # Scan S3, write dataset_index.csv"),
    code("    merge_results.py             # Merge all slice CSVs after completion"),
    code("    monitor_jobs.py              # CLI pipeline monitor"),
    code("  project/"),
    code("    main.py                      # Fargate task entry point"),
    code("    ocr/vision_api.py            # Google Vision / Tesseract OCR wrapper"),
    code("    county/prompts.py            # Gemini Flash county extraction"),
    code("    grid/scoring.py              # U-Net dot scoring"),
    code("    latlong/latlong_extractor.py # PLSS -> GPS coordinate resolution"),
    code("    utils/processing_status.py   # Checkpoint read/write"),
    code("  visualizer/"),
    code("    well_map.html                # Interactive satellite map (Leaflet + Esri tiles)"),
    code("    well_map_standalone.html     # Standalone map (GeoJSON embedded inline)"),
    code("    merge_well_locations.py      # S3 results -> GeoJSON aggregator"),
    code("    build_standalone.py          # Embed GeoJSON in HTML for offline/Pages use"),
    code("    deploy_viewer.py             # Upload map to S3"),
    code("    s3_vs_local_verify.py        # D: drive vs S3 PDF count comparison"),
    code("    refresh_map.ps1              # PowerShell: merge + build + upload + open"),
    code("  docs/"),
    code("    index.html                   # GitHub Pages entry (= standalone map)"),
    nbsp(),

    rule(BLUE),
    new Paragraph({ alignment:AlignmentType.CENTER, spacing:{ before:200 },
      children:[new TextRun({
        text:"End of Document  —  OSU Well Records Pipeline Technical Report  —  May 2026",
        size:20, italic:true, color:DGRAY, font:"Arial"
      })]
    }),

  ] // children
  }] // sections
});

Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync("D:/project_modular/OSU_Pipeline_Report.docx", buf);
  const kb = Math.round(buf.length / 1024);
  console.log(`Written: D:/project_modular/OSU_Pipeline_Report.docx (${kb} KB)`);
}).catch(e => { console.error(e); process.exit(1); });
