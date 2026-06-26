# Bazaar bootstrap installer for Windows (Stage 1). The primary install is a git clone:
#
#   git clone https://github.com/jerryneoneo/bazaar-skills.git ~/bazaar-skills; cd ~/bazaar-skills; ./setup
#
# Or, if you self-host this script: iwr -useb https://<your-host>/install.ps1 | iex
# Already have a checkout? Skip this — just run ./setup in it. Override the repo with $env:BAZAAR_REPO.
#
# Mirrors install.sh: check prerequisites, clone the repo, then hand off to the agent (Claude Code
# or Codex) pointed at the in-repo runbook .claude/commands/bazaar-install.md (Stage 2).
#
# NOTE (scope: "Mac now, Windows designed-for"): the interactive flow works on Windows, but the
# always-on background supervisor is not yet implemented here (macOS uses launchd; Windows will use
# Task Scheduler via a future bin/platforms/windows.py). The channels supported today are Telegram
# and the console; iMessage + WhatsApp land later. Stage 2's preflight will flag anything missing.
#
# Override defaults with env vars: BAZAAR_REPO, BAZAAR_DIR, BAZAAR_AGENT.

$ErrorActionPreference = "Stop"

$Repo = if ($env:BAZAAR_REPO) { $env:BAZAAR_REPO } else { "https://github.com/jerryneoneo/bazaar-skills" }
$Dir  = if ($env:BAZAAR_DIR)  { $env:BAZAAR_DIR }  else { Join-Path $HOME "bazaar-skills" }
$Handoff = "follow .claude/commands/bazaar-install.md to set me up"

function Say  ($m) { Write-Host "Bazaar: $m" -ForegroundColor Cyan }
function Warn ($m) { Write-Host "Bazaar: $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host "Bazaar: $m" -ForegroundColor Red; exit 1 }
function Have ($c) { return [bool](Get-Command $c -ErrorAction SilentlyContinue) }

Say "Bootstrapping your personal P2P seller agent (Windows)."

# 1. Prerequisites.
if (-not (Have git))     { Die "git is required. Install it and re-run." }
if (-not (Have python))  { if (-not (Have python3)) { Die "python is required. Install it and re-run." } }
foreach ($opt in @("node","npx")) {
  if (-not (Have $opt)) { Warn "$opt not found — needed later for the Playwright browser tool (Stage 2 will flag it)." }
}

# 2. Clone (or update).
if (Test-Path (Join-Path $Dir ".git")) {
  Say "Updating existing install at $Dir"
  git -C $Dir pull --ff-only
} elseif ((Test-Path $Dir) -and (Get-ChildItem $Dir -Force | Select-Object -First 1)) {
  Die "$Dir exists and is not empty. Move it or set BAZAAR_DIR to a fresh path."
} else {
  Say "Cloning $Repo -> $Dir"
  git clone --depth 1 $Repo $Dir
}
Set-Location $Dir

# Python launcher (python on PATH, else python3).
$Py = if (Have python) { "python" } else { "python3" }
function CliToHarness ($a) { if ($a -eq "codex") { "codex" } else { "claude-code" } }
function SigninCmd    ($a) { if ($a -eq "codex") { "codex login" } else { "claude  (then complete the login, or run /login)" } }
function PresentLabel ($c) { if (Have $c) { "[installed]" } else { "[not installed]" } }

# 3. SELECT the agent runtime (always a menu, unless BAZAAR_AGENT overrides it).
$Agent = $env:BAZAAR_AGENT
if ($Agent) {
  if ($Agent -notin @("claude","codex")) { Die "BAZAAR_AGENT must be 'claude' or 'codex' (got '$Agent')." }
  if (-not (Have $Agent)) { Die "BAZAAR_AGENT=$Agent but '$Agent' is not on PATH. Install it and re-run." }
} else {
  while (-not $Agent) {
    Write-Host ""
    Say "Which agent runtime do you want to use?"
    Write-Host ("  1) Claude Code   {0}" -f (PresentLabel claude))
    Write-Host ("  2) Codex         {0}" -f (PresentLabel codex))
    $choice = Read-Host "Bazaar: Enter 1 or 2 (q to quit)"
    switch ($choice) {
      "1" { $Agent = "claude" }
      "2" { $Agent = "codex" }
      { $_ -in @("q","Q") } { Die "Aborted. Re-run install.ps1 when you're ready." }
      default { Warn "Pick 1, 2, or q."; continue }
    }
    if (-not (Have $Agent)) {
      Warn "$Agent is not installed."
      if ($Agent -eq "claude") { Warn "Install Claude Code: https://claude.com/claude-code" } else { Warn "Install Codex, then re-run." }
      Warn "Install it (in another terminal) and pick again, or q to quit."
      $Agent = $null
    }
  }
}
$Harness = CliToHarness $Agent

# 4. SIGN IN — gate the handoff on the chosen runtime being authenticated (instruct & wait).
while ($true) {
  & $Py bin/install.py harness --name $Harness *> $null
  if ($LASTEXITCODE -eq 0) { break }
  Warn "$Agent is not signed in yet."
  Say ("Sign in: run `{0}` in another terminal, then press Enter to re-check (q to quit)." -f (SigninCmd $Agent))
  $ans = Read-Host "Bazaar"
  if ($ans -in @("q","Q")) { Die "Aborted. Sign in, then re-run install.ps1." }
}
Say "$Agent is signed in."

# 5. CONTINUE — hand off to Stage 2 with the selected runtime so the runbook trusts the choice.
$env:BAZAAR_HARNESS = $Harness
Say "Handing off to $Agent for guided onboarding..."
& $Agent $Handoff
