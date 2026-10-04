# Bambu Studio Desktop URI Handler
# Parses: bambustudio://open?file=http://...

param (
    [Parameter(Mandatory=$true)]
    [string]$Uri
)

try {
    # Extract file URL from URI
    # bambustudio://open?file=http%3A%2F%2F... or bambustudio://open?file=http://...
    $FileUrl = ""
    if ($Uri -match "[?&]file=([^&]+)") {
        $FileUrl = [System.Uri]::UnescapeDataString($Matches[1])
    } else {
        # Fallback: strip scheme
        $FileUrl = $Uri -replace "^bambustudio://(open\?file=)?", ""
        $FileUrl = [System.Uri]::UnescapeDataString($FileUrl)
    }

    if (-not $FileUrl.StartsWith("http://") -and -not $FileUrl.StartsWith("https://")) {
        [System.Windows.Forms.MessageBox]::Show("Invalid download URL: $FileUrl", "Bambu Studio Launcher")
        exit 1
    }

    # Prepare temp download directory
    $TempDir = Join-Path $env:TEMP "3DPrintLib"
    if (-not (Test-Path $TempDir)) {
        New-Item -ItemType Directory -Path $TempDir -Force | Out-Null
    }

    # Retrieve response headers or default filename
    $UriObj = [System.Uri]$FileUrl
    $DefaultName = [System.IO.Path]::GetFileName($UriObj.LocalPath)
    if (-not $DefaultName -or $DefaultName -eq "download") {
        $DefaultName = "model_$([guid]::NewGuid().ToString().Substring(0,8)).3mf"
    }

    $DestFile = Join-Path $TempDir $DefaultName

    # Download file using WebClient / Invoke-WebRequest
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
    $WebClient = New-Object System.Net.WebClient
    $WebClient.DownloadFile($FileUrl, $DestFile)

    # Locate Bambu Studio Executable
    $BambuPaths = @(
        "$env:ProgramFiles\Bambu Studio\bin\bambu-studio.exe",
        "$env:ProgramFiles\Bambu Studio\bambu-studio.exe",
        "$env:LOCALAPPDATA\Programs\BambuStudio\bambu-studio.exe",
        "${env:ProgramFiles(x86)}\Bambu Studio\bambu-studio.exe"
    )

    $BambuExe = $null
    foreach ($path in $BambuPaths) {
        if (Test-Path $path) {
            $BambuExe = $path
            break
        }
    }

    # Check PATH if not found in default paths
    if (-not $BambuExe) {
        $BambuCmd = Get-Command "bambu-studio.exe" -ErrorAction SilentlyContinue
        if ($BambuCmd) {
            $BambuExe = $BambuCmd.Source
        }
    }

    if (-not $BambuExe) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show("Bambu Studio executable not found in default locations.`nPlease install Bambu Studio or associate .3mf/.stl with it.", "Bambu Studio Launcher", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning)
        # Fallback to default shell association
        Start-Process $DestFile
        exit 0
    }

    # Launch Bambu Studio with downloaded model
    Start-Process -FilePath $BambuExe -ArgumentList "`"$DestFile`""
}
catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show("Error opening model in Bambu Studio: $($_.Exception.Message)", "Launcher Error", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)
    exit 1
}
