-- AppleScript protocol handler for macOS (bambustudio://open?file=http://...)
on open location this_URL
    set file_url to do shell script "python3 -c \"import urllib.parse, sys; print(urllib.parse.unquote(sys.argv[1].split('file=')[-1]))\" " & quoted form of this_URL
    set temp_path to "/tmp/bambu_download_" & (do shell script "date +%s") & ".3mf"
    do shell script "curl -sSL -o " & quoted form of temp_path & " " & quoted form of file_url
    tell application "BambuStudio"
        activate
        open POSIX file temp_path
    end tell
end open location
