[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$ProjectId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
  throw "Google Cloud CLI (gcloud) is not installed or is not on PATH."
}
if (-not (Get-Command Set-Clipboard -ErrorAction SilentlyContinue)) {
  throw "Set-Clipboard is not available in this PowerShell session."
}

$value = @(& gcloud secrets versions access latest `
  --secret steam-mcp-oauth-login-secret `
  --project $ProjectId) -join ""
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($value)) {
  throw "The Steam OAuth login secret could not be read."
}
$value.Trim() | Set-Clipboard
$value = $null
Write-Host "Steam MCP ChatGPT access key copied to the clipboard." -ForegroundColor Green
