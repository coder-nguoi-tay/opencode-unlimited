# OpenCode Unlimited - Status Dashboard
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectDir = (Get-Item $PSScriptRoot).Parent.Parent.FullName
Set-Location $ProjectDir

python main.py --status
