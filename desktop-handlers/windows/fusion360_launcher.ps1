# Autodesk Fusion 360 Desktop URI Handler
# Parses: fusion360://open?file=http://...

param (
    [Parameter(Mandatory=$true)]
    [string]$Uri
)

try {
    $FileUrl = ""
    if ($Uri -match "[?&]file=([^&]+)") {
        $FileUrl = [System.Uri]::UnescapeDataString($Matches[1])
    } else {
        $FileUrl = $Uri -replace "^fusion360://(open\?file=)?", ""
        $FileUrl = [System.Uri]::UnescapeDataString($FileUrl)
    }

    if (-not $FileUrl.StartsWith("http://") -and -not $FileUrl.StartsWith("https://")) {
        [System.Windows.Forms.MessageBox]::Show("Invalid download URL: $FileUrl", "Fusion 360 Launcher")
        exit 1
    }

    $TempDir = Join-Path $env:TEMP "3DPrintLib"
    if (-not (Test-Path $TempDir)) {
        New-Item -ItemType Directory -Path $TempDir -Force | Out-Null
    }

    $UriObj = [System.Uri]$FileUrl
    $DefaultName = [System.IO.Path]::GetFileName($UriObj.LocalPath)
    if (-not $DefaultName -or $DefaultName -eq "download") {
        $DefaultName = "model_$([guid]::NewGuid().ToString().Substring(0,8)).step"
    }

    $DestFile = Join-Path $TempDir $DefaultName

    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
    $WebClient = New-Object System.Net.WebClient
    $WebClient.DownloadFile($FileUrl, $DestFile)

    # Locate Fusion 360 Executable in Autodesk WebDeploy directory
    $AutodeskDir = Join-Path $env:LOCALAPPDATA "Autodesk\webdeploy\production"
    $FusionExe = $null

    if (Test-Path $AutodeskDir) {
        $FoundExes = Get-ChildItem -Path $AutodeskDir -Filter "Fusion360.exe" -Recurse -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending
        if ($FoundExes.Count -gt 0) {
            $FusionExe = $FoundExes[0].FullName
        }
    }

    if (-not $FusionExe) {
        # Check PATH or default associations
        $FusionCmd = Get-Command "Fusion360.exe" -ErrorAction SilentlyContinue
        if ($FusionCmd) {
            $FusionExe = $FusionCmd.Source
        }
    }

    if ($FusionExe) {
        Start-Process -FilePath $FusionExe -ArgumentList "`"$DestFile`""
    } else {
        # Fallback to system file handler
        Start-Process $DestFile
    }
}
catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show("Error opening model in Fusion 360: $($_.Exception.Message)", "Launcher Error", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)
    exit 1
}
