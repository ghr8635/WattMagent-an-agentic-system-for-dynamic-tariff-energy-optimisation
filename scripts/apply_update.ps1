<#
Apply a changes zip to this repository and run the checks.

Usage (from the repository root, on your feature branch):
    .\scripts\apply_update.ps1 .\wattmagent_update10.zip

It extracts straight into the repo (files are overwritten in place), deletes nothing,
runs ruff, mypy and pytest, and shows `git status`. Zip files are git-ignored, so they
can never be committed. Commit and push yourself once the checks are green.
#>
param([Parameter(Mandatory = $true)][string]$Zip)

$ErrorActionPreference = "Stop"
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -eq "main") { throw "You are on main. Create a feature branch first: git checkout -b feat/<name>" }

Expand-Archive -Path $Zip -DestinationPath . -Force
python -m ruff format .
python -m ruff check .
python -m mypy
python -m pytest
git status --short
Write-Host "`nBranch: $branch. If everything above is green: git add -A; git commit -m '...'; git push"
