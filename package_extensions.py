import os
import zipfile
import json
import shutil

ext_dir = "dashforge-extension"
chrome_zip = "static/downloads/dashforge-chrome.zip"
firefox_zip = "static/downloads/dashforge-firefox.zip"

with open(os.path.join(ext_dir, "manifest.json"), "r") as f:
    manifest = json.load(f)

def create_zip(zip_path, is_chrome):
    with zipfile.ZipFile(zip_path, 'w') as zf:
        for root, _, files in os.walk(ext_dir):
            for file in files:
                filepath = os.path.join(root, file)
                arcname = os.path.relpath(filepath, ext_dir)
                
                if file == "manifest.json":
                    # Modify manifest depending on browser
                    m = manifest.copy()
                    if is_chrome:
                        m["background"] = {"service_worker": "background.js"}
                        if "content_security_policy" in m:
                            # Chrome MV3 csp format is different, or we can just drop the Firefox specific one
                            del m["content_security_policy"]
                    else:
                        m["background"] = {"scripts": ["background.js"]}
                        # Add gecko configuration required by Mozilla AMO validation
                        m["browser_specific_settings"] = {
                            "gecko": {
                                "id": "passive-memory@dashforge.local",
                                "strict_min_version": "140.0",
                                "data_collection_permissions": {
                                    "required": [
                                        "none"
                                    ]
                                }
                            }
                        }
                    
                    zf.writestr(arcname, json.dumps(m, indent=2))
                else:
                    zf.write(filepath, arcname)

create_zip(chrome_zip, True)
create_zip(firefox_zip, False)
shutil.copy2(firefox_zip, "static/downloads/dashforge-firefox.xpi")
print("Extensions packaged successfully.")
