"""Generate charts and diagrams for OSU Pipeline Report."""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.patches as FancyArrowPatch
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
import numpy as np
import os

OUT = r"D:\project_modular\report_images"
os.makedirs(OUT, exist_ok=True)

# ── 1. Pipeline Flow Diagram ─────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(14, 5))
ax.set_xlim(0, 14)
ax.set_ylim(0, 5)
ax.axis('off')
fig.patch.set_facecolor('#FAFAFA')

boxes = [
    (0.4,  2.0, '#1B4F8A', 'PDF\nSource\n(S3)'),
    (2.2,  2.0, '#1B4F8A', 'Stage 1\nLatLong\nExtraction'),
    (4.2,  2.0, '#1B6B3A', 'Stage 2\nGrid\nDetection'),
    (6.2,  2.0, '#1B6B3A', 'Stage 3\nLocation\nExtraction'),
    (8.2,  2.0, '#8A4B1B', 'Stage 4\nCounty\nExtraction'),
    (10.2, 2.0, '#8A4B1B', 'Dot\nCoordinate\nMapping'),
    (12.2, 2.0, '#4B1B8A', 'Results\nCSV\n(S3)'),
]
for x, y, color, label in boxes:
    rect = FancyBboxPatch((x, y), 1.6, 1.0, boxstyle="round,pad=0.05",
                           facecolor=color, edgecolor='white', linewidth=1.5)
    ax.add_patch(rect)
    ax.text(x+0.8, y+0.5, label, ha='center', va='center',
            color='white', fontsize=8, fontweight='bold', linespacing=1.4)

# Arrows between boxes
for i in range(len(boxes)-1):
    x1 = boxes[i][0] + 1.6
    x2 = boxes[i+1][0]
    y  = 2.5
    ax.annotate('', xy=(x2, y), xytext=(x1, y),
                arrowprops=dict(arrowstyle='->', color='#444', lw=1.5))

# Checkpoint label
ax.text(7.0, 1.4, 'Checkpoint resume (S3 processing_status.csv)', ha='center',
        fontsize=8, color='#666', style='italic')
ax.annotate('', xy=(7.0, 1.65), xytext=(7.0, 1.4),
            arrowprops=dict(arrowstyle='->', color='#999', lw=1))

ax.set_title('OSU Well Records — 4-Stage Extraction Pipeline', fontsize=13,
             fontweight='bold', pad=12, color='#1B1B1B')
plt.tight_layout()
plt.savefig(os.path.join(OUT, 'pipeline_flow.png'), dpi=150, bbox_inches='tight',
            facecolor=fig.get_facecolor())
plt.close()
print("pipeline_flow.png done")

# ── 2. Extraction Quality Bar Chart ──────────────────────────────────────────
fig, ax = plt.subplots(figsize=(9, 5))
fig.patch.set_facecolor('#FAFAFA')
ax.set_facecolor('#FAFAFA')

stages  = ['Grid\nDetection', 'County\nExtraction', 'Dot\nMapping', 'Location\nExtraction', 'LatLong\nExtraction']
rates   = [94.5, 97.5, 73.0, 68.0, 0.0]
colors  = ['#1B6B3A', '#1B4F8A', '#8A6B1B', '#8A4B1B', '#888888']

bars = ax.barh(stages, rates, color=colors, edgecolor='white', height=0.55)
for bar, rate in zip(bars, rates):
    label = f'{rate}%' if rate > 0 else 'N/A (RDS)'
    ax.text(bar.get_width() + 1.0, bar.get_y() + bar.get_height()/2,
            label, va='center', fontsize=10, color='#333')

ax.set_xlim(0, 115)
ax.set_xlabel('Detection Rate (%)', fontsize=11)
ax.set_title('Extraction Stage Performance (Sample: Slice 7, 200 Records)', fontsize=12,
             fontweight='bold', pad=10)
ax.axvline(x=90, color='#AAA', linestyle='--', linewidth=0.8, label='90% target')
ax.legend(fontsize=9)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.tick_params(labelsize=10)
plt.tight_layout()
plt.savefig(os.path.join(OUT, 'extraction_quality.png'), dpi=150, bbox_inches='tight',
            facecolor=fig.get_facecolor())
plt.close()
print("extraction_quality.png done")

# ── 3. AWS Architecture Diagram ───────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(13, 7))
ax.set_xlim(0, 13)
ax.set_ylim(0, 7)
ax.axis('off')
fig.patch.set_facecolor('#F0F4F8')

def box(ax, x, y, w, h, color, label, sub='', fontsize=9):
    rect = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.07",
                           facecolor=color, edgecolor='#CCC', linewidth=1)
    ax.add_patch(rect)
    ax.text(x+w/2, y+h/2 + (0.15 if sub else 0), label, ha='center', va='center',
            fontsize=fontsize, fontweight='bold', color='white')
    if sub:
        ax.text(x+w/2, y+h/2 - 0.2, sub, ha='center', va='center',
                fontsize=7.5, color='#EEE')

# S3 bucket
box(ax, 0.3, 4.5, 2.2, 1.8, '#3D7AB5', 'Amazon S3', 'osu-well-records\n86.9 GB PDFs\nResults CSVs')

# Secrets Manager
box(ax, 0.3, 2.2, 2.2, 1.8, '#D45B07', 'Secrets\nManager', 'GCP credentials\nDB password')

# ECR
box(ax, 0.3, 0.2, 2.2, 1.6, '#6B48A6', 'ECR', 'osu-pipeline:latest\nosu-pipeline-base:latest')

# AWS Batch
box(ax, 3.5, 2.5, 3.0, 2.8, '#1A6B3A', 'AWS Batch', 'Fargate\n2832 tasks\n1 vCPU / 2 GB RAM')

# Fargate containers
box(ax, 7.5, 4.2, 2.5, 1.5, '#1B4F8A', 'Fargate Task', 'Tesseract + PyMuPDF\nGemini Flash/Pro')
box(ax, 7.5, 2.5, 2.5, 1.5, '#1B4F8A', 'Fargate Task', 'Tesseract + PyMuPDF\nGemini Flash/Pro')
box(ax, 7.5, 0.8, 2.5, 1.5, '#555',    '+ 28 more tasks', 'Running concurrently')

# RDS
box(ax, 10.5, 3.2, 2.2, 1.5, '#A63B1B', 'RDS\nPostgreSQL', 'PLSS coordinate\nresolution')

# Google Cloud
box(ax, 10.5, 0.8, 2.2, 2.0, '#1557B0', 'Google Cloud', 'Gemini Flash 2.5\nGemini Pro 2.5\nCloud Vision API')

# Arrows
arrow_kw = dict(arrowstyle='->', color='#555', lw=1.3)
ax.annotate('', xy=(3.5, 4.0), xytext=(2.5, 4.8), arrowprops=arrow_kw)  # S3 → Batch
ax.annotate('', xy=(3.5, 3.2), xytext=(2.5, 3.0), arrowprops=arrow_kw)  # Secrets → Batch
ax.annotate('', xy=(3.5, 2.8), xytext=(2.5, 1.0), arrowprops=arrow_kw)  # ECR → Batch
ax.annotate('', xy=(7.5, 5.0), xytext=(6.5, 4.5), arrowprops=arrow_kw)  # Batch → Task
ax.annotate('', xy=(7.5, 3.2), xytext=(6.5, 3.5), arrowprops=arrow_kw)
ax.annotate('', xy=(10.5, 3.9), xytext=(10.0, 4.5), arrowprops=arrow_kw) # Task → RDS
ax.annotate('', xy=(10.5, 2.0), xytext=(10.0, 3.2), arrowprops=arrow_kw) # Task → GCloud

ax.set_title('OSU Pipeline — AWS Architecture Overview', fontsize=13,
             fontweight='bold', pad=12, color='#1B1B1B')
plt.tight_layout()
plt.savefig(os.path.join(OUT, 'aws_architecture.png'), dpi=150, bbox_inches='tight',
            facecolor=fig.get_facecolor())
plt.close()
print("aws_architecture.png done")

# ── 4. Cost Breakdown Pie ─────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(8, 6))
fig.patch.set_facecolor('#FAFAFA')

labels  = ['AWS Fargate\n(2,832 tasks × 4hr)', 'Google Gemini API\n(Flash + Pro)',
           'Amazon S3\n(Storage + Transfer)', 'RDS PostgreSQL\n(t3.micro)',
           'ECR + Misc']
sizes   = [225, 95, 12, 13, 5]
colors  = ['#1B4F8A', '#1B6B3A', '#8A6B1B', '#A63B1B', '#6B48A6']
explode = (0.05, 0.05, 0, 0, 0)

wedges, texts, autotexts = ax.pie(sizes, labels=labels, colors=colors,
                                   explode=explode, autopct='%1.1f%%',
                                   startangle=140, pctdistance=0.78)
for t in texts:   t.set_fontsize(9)
for a in autotexts: a.set_fontsize(9); a.set_color('white'); a.set_fontweight('bold')

ax.set_title(f'Estimated Pipeline Cost Breakdown\nTotal ≈ $350 full run (2,832 tasks)',
             fontsize=12, fontweight='bold', pad=12)
plt.tight_layout()
plt.savefig(os.path.join(OUT, 'cost_breakdown.png'), dpi=150, bbox_inches='tight',
            facecolor=fig.get_facecolor())
plt.close()
print("cost_breakdown.png done")

# ── 5. County Method Distribution ────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(7, 4))
fig.patch.set_facecolor('#FAFAFA')
ax.set_facecolor('#FAFAFA')

methods = ['OCR Anchor\n(Fuzzy Match)', 'Gemini Flash\n(LLM Fast)', 'Gemini Pro\n(LLM Accurate)']
counts  = [41, 151, 3]
colors  = ['#1B4F8A', '#1B6B3A', '#8A4B1B']
bars    = ax.bar(methods, counts, color=colors, edgecolor='white', width=0.5)
for bar, count in zip(bars, counts):
    pct = count / sum(counts) * 100
    ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 2,
            f'{count}\n({pct:.1f}%)', ha='center', va='bottom', fontsize=10)

ax.set_ylabel('Records', fontsize=11)
ax.set_title('County Extraction Method Distribution (200 records)', fontsize=11, fontweight='bold')
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.set_ylim(0, 185)
plt.tight_layout()
plt.savefig(os.path.join(OUT, 'county_methods.png'), dpi=150, bbox_inches='tight',
            facecolor=fig.get_facecolor())
plt.close()
print("county_methods.png done")

print("\nAll charts saved to", OUT)
