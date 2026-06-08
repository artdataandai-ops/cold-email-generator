param(
    [string]$Pptx,
    [string]$OutDir
)
$Pptx = (Resolve-Path $Pptx).Path
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }
$OutDir = (Resolve-Path $OutDir).Path
$ppt = New-Object -ComObject PowerPoint.Application
try {
    $pres = $ppt.Presentations.Open($Pptx, $true, $false, $false)  # ReadOnly, no untitled, no withWindow
    for ($i = 1; $i -le $pres.Slides.Count; $i++) {
        $out = Join-Path $OutDir ("slide_{0:D2}.png" -f $i)
        # SaveAs as PNG isn't per-slide; use Slides.Export.
        $pres.Slides.Item($i).Export($out, 'PNG', 1920, 1080)
        Write-Output $out
    }
    $pres.Close()
} finally {
    $ppt.Quit()
    [System.Runtime.Interopservices.Marshal]::ReleaseComObject($ppt) | Out-Null
}
