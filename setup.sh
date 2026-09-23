#!/usr/bin/env bash
# One-shot GitHub setup for internwatch: creates the repo, pushes, sets every secret, starts the first run.
# Usage: ./setup.sh [repo-name]      (default: internwatch)
set -euo pipefail
cd "$(dirname "$0")"

command -v gh >/dev/null || { echo "Install the GitHub CLI first:  brew install gh"; exit 1; }
gh auth status >/dev/null 2>&1 || gh auth login -w -s repo,workflow

REPO="${1:-internwatch}"
if git remote get-url origin >/dev/null 2>&1; then
  git push -u origin main
else
  # Public = free unlimited Actions minutes. Your resume never goes in git (it's .gitignored
  # and stored as a secret); tailored PDFs only arrive via ntfy/email.
  gh repo create "$REPO" --public --source . --push \
    --description "Internship + new-grad watcher with tailored resumes"
fi

echo "→ RESUME_YAML"
gh secret set RESUME_YAML < resume.yaml

key="${ANTHROPIC_API_KEY:-}"
if [ -z "$key" ]; then read -rsp "Anthropic API key (console.anthropic.com; Enter to skip tailoring): " key; echo; fi
[ -n "$key" ] && gh secret set ANTHROPIC_API_KEY --body "$key"

if [ -f .ntfy-topic ]; then TOPIC="$(cat .ntfy-topic)"; else TOPIC="kyle-iw-$(openssl rand -hex 8)"; echo "$TOPIC" > .ntfy-topic; fi
gh secret set NTFY_TOPIC --body "$TOPIC"

read -rp "Gmail address for alert emails (Enter to skip): " em
if [ -n "$em" ]; then
  echo "Needs a Gmail app password: https://myaccount.google.com/apppasswords (2FA must be on)"
  read -rsp "App password: " pw; echo
  gh secret set SMTP_HOST --body smtp.gmail.com
  gh secret set SMTP_PORT --body 465
  gh secret set SMTP_USER --body "$em"
  gh secret set SMTP_PASS --body "$pw"
  gh secret set EMAIL_TO  --body "$em"
fi

gh workflow enable watch.yml >/dev/null 2>&1 || true
gh workflow run watch.yml
echo
echo "Done. First run records current postings silently; alerts start on the next run (~10 min)."
echo "ntfy: install the app and subscribe to topic:  $TOPIC   (also saved in .ntfy-topic)"
echo "Watch runs:  gh run watch   or   gh run list --workflow watch.yml"
