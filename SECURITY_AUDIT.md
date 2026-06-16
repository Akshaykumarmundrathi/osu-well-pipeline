# Security Audit & Hardening (Jun-13)

Scope: GitHub repo + full history, the published site, and AWS exposure.
Principle followed: apply only changes that cannot break the live map / Action;
**flag** anything riskier for explicit approval.

## Clean (verified)
- **No secrets in the repo or git history** — scanned all commits for AWS keys
  (`AKIA…`), Google keys (`AIza…`), GitHub PATs (`ghp_/github_pat_`), private
  keys, and the GCP `smiling-breaker*.json`. Zero hits. The RDS/GOOGLE matches
  are variable names, log strings, and `test-host` fixtures — not values.
- **`.gitignore` is comprehensive** — `.env*`, `credentials/`, `*.pem`, `*.key`,
  `*.json` (with narrow allow-list), `*service*account*.json`, `*gcp*.json`,
  `smiling-breaker*.json`, AWS `.env.account*`.
- **GitHub Action least-privilege** — `permissions: contents: write, issues:
  write` only (nothing more). Secrets passed via `${{ secrets.* }}` (encrypted).
- **S3 policy is prefix-scoped** — public read (`s3:GetObject`) is limited to
  `pdfs/*`, `viewer/*`, `analysis/*`, `index`; **no public `ListBucket`**, so the
  571k object keys cannot be enumerated by outsiders.

## Fixed this pass (safe, no functional break)
- **Corrections are now authorization-gated.** `gh_apply_corrections.py` only
  applies issues from users with repo write access (`author_association` ∈
  OWNER/MEMBER/COLLABORATOR), plus an optional `ALLOWED_CORRECTORS` secret
  (comma-separated logins). Unauthorized correction issues are politely declined,
  closed, and labelled `needs-maintainer-review` — **never applied to the map or
  RDS**. This closes the "anyone can change a well location" hole while keeping
  the public "report a problem" flow intact (reports still get logged for a
  maintainer). Legitimate owner/maintainer corrections work unchanged.

## Flagged — needs your approval (AWS changes; not done autonomously to avoid breaking the site)
1. **Account ID in the public bucket name** (`osu-well-records-225989338968`).
   Inherent to direct-from-S3 loading. To remove it from public view, front the
   bucket with **CloudFront + Origin Access Control**, turn ON S3 "Block Public
   Access", and serve PDFs via a neutral domain. This both masks the account ID
   and removes all direct public bucket access. (Site keeps working via the CDN.)
2. **PDFs are world-readable.** Fine if these are public records; if not, the
   same CloudFront+OAC setup (signed URLs) restricts access.
3. **Rotate the GitHub PAT** that was shared earlier in chat — regenerate it in
   GitHub → Settings → Developer settings, and update the local `.env`. (It is
   NOT in git, but it was exposed in conversation.)
4. **Optional:** set repo secret `ALLOWED_CORRECTORS` if specific non-collaborator
   people should be allowed to submit applied corrections.

## Net
Public exposure now: the map JSON, the PDFs (prefix-scoped, no listing), and
read-only code. Corrections require authorization. No credentials anywhere
public. Remaining items (1-3) are AWS/account actions for you to approve.
