<#
.SYNOPSIS
  Stage everything, commit with the given message, and push to main.
  One line instead of git add / git commit / git push separately.

.EXAMPLE
  .\commit.ps1 "fix: handle empty settlements gracefully"
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Message
)

$ErrorActionPreference = "Stop"

git add -A
if ($LASTEXITCODE -ne 0) { throw "git add failed" }

git status --short

git commit -m $Message
if ($LASTEXITCODE -ne 0) { throw "git commit failed" }

git push origin main
if ($LASTEXITCODE -ne 0) { throw "git push failed" }

Write-Host "Done. Pushed to origin/main." -ForegroundColor Green
