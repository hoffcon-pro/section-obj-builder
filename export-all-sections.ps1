$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$inputDir = Join-Path $repoRoot "section data"
$outputDir = Join-Path $repoRoot "output"

if (-not (Test-Path $inputDir)) {
    throw "Input directory not found: $inputDir"
}

New-Item -ItemType Directory -Path $outputDir -Force | Out-Null

$csvFiles = Get-ChildItem -Path $inputDir -Filter *.csv | Sort-Object Name

if (-not $csvFiles) {
    throw "No CSV files found in $inputDir"
}

foreach ($csv in $csvFiles) {
    Write-Host "Exporting $($csv.Name)..."
    uv run python main.py $csv.FullName --output-dir $outputDir
}

Write-Host "Finished exporting $($csvFiles.Count) CSV file(s) to $outputDir"
