$ErrorActionPreference = 'Stop'
$renderRoot = $PSScriptRoot
$browserCandidates = @(
    'C:\Program Files\Google\Chrome\Application\chrome.exe',
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
)
$diagramBrowser = $browserCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $diagramBrowser) { throw 'Chrome or Edge is required to render PNG previews.' }
$renderProfilesRoot = Join-Path ([IO.Path]::GetTempPath()) ('ai-test-agent-diagrams-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $renderProfilesRoot
$specs = @(
    @{Name='AI-Test-Agent-Architecture'; Height=1370},
    @{Name='AI-Test-Agent-Deployment'; Height=1380}
)
foreach ($spec in $specs) {
    $htmlPath = Join-Path $renderRoot ($spec.Name + '.html')
    $pngPath = Join-Path $renderRoot ($spec.Name + '.png')
    $profilePath = Join-Path $renderProfilesRoot $spec.Name
    $fileUrl = ([uri]$htmlPath).AbsoluteUri
    & $diagramBrowser '--headless=new' '--disable-gpu' '--hide-scrollbars' '--no-first-run' '--no-default-browser-check' '--force-device-scale-factor=1' "--user-data-dir=$profilePath" "--window-size=1800,$($spec.Height)" "--screenshot=$pngPath" $fileUrl 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { throw "Browser rendering failed for $($spec.Name)" }
    if (-not (Test-Path -LiteralPath $pngPath)) { throw "Missing screenshot: $pngPath" }
}
# Temporary profiles are deliberately left in the OS temp directory. No user profile is touched.
Write-Output "PNG previews created in $renderRoot"
