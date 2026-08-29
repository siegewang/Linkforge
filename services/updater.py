import os
import subprocess
import threading
import time
import logging
import requests

logger = logging.getLogger(__name__)

def get_version_info():
    """Returns local git commit, branch, and checks remote GitHub repository for available updates."""
    version_info = {
        "version_tag": "2.1.0",
        "current_commit": "unknown",
        "current_commit_date": "",
        "current_commit_msg": "",
        "branch": "main",
        "remote_url": "https://github.com/siegewang/Linkforge",
        "update_available": False,
        "remote_commit": "",
        "remote_commit_msg": "",
        "pending_commits": [],
        "last_checked": time.strftime("%Y-%m-%d %H:%M:%S")
    }

    try:
        # Get local commit hash
        local_hash = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'], 
            stderr=subprocess.DEVNULL, 
            text=True
        ).strip()
        version_info["current_commit"] = local_hash

        # Get local commit date
        local_date = subprocess.check_output(
            ['git', 'log', '-1', '--format=%cd', '--date=relative'], 
            stderr=subprocess.DEVNULL, 
            text=True
        ).strip()
        version_info["current_commit_date"] = local_date

        # Get local commit message
        local_msg = subprocess.check_output(
            ['git', 'log', '-1', '--format=%s'], 
            stderr=subprocess.DEVNULL, 
            text=True
        ).strip()
        version_info["current_commit_msg"] = local_msg

        # Get current branch
        branch = subprocess.check_output(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'], 
            stderr=subprocess.DEVNULL, 
            text=True
        ).strip()
        version_info["branch"] = branch or "main"

        # Check remote commit via git ls-remote
        try:
            remote_out = subprocess.check_output(
                ['git', 'ls-remote', 'origin', f'refs/heads/{version_info["branch"]}'], 
                stderr=subprocess.DEVNULL, 
                text=True, 
                timeout=6
            ).strip()
            
            if remote_out:
                remote_hash = remote_out.split()[0][:7]
                version_info["remote_commit"] = remote_hash
                if remote_hash != local_hash:
                    version_info["update_available"] = True
                    
                    # Fetch changelog summaries if possible
                    try:
                        subprocess.run(['git', 'fetch', 'origin', version_info["branch"]], capture_output=True, timeout=8)
                        pending_log = subprocess.check_output(
                            ['git', 'log', f'HEAD..origin/{version_info["branch"]}', '--oneline', '-n', '5'],
                            stderr=subprocess.DEVNULL,
                            text=True
                        ).strip()
                        if pending_log:
                            version_info["pending_commits"] = pending_log.split('\n')
                    except Exception:
                        pass
        except Exception as ls_err:
            logger.debug(f"ls-remote check skipped: {ls_err}")
            
            # Fallback to GitHub Public API
            try:
                api_url = "https://api.github.com/repos/siegewang/Linkforge/commits/main"
                resp = requests.get(api_url, timeout=4, headers={"User-Agent": "LinkForge-Updater"})
                if resp.status_code == 200:
                    data = resp.json()
                    r_sha = data.get("sha", "")[:7]
                    version_info["remote_commit"] = r_sha
                    version_info["remote_commit_msg"] = data.get("commit", {}).get("message", "").split('\n')[0]
                    if r_sha and r_sha != local_hash:
                        version_info["update_available"] = True
            except Exception as api_err:
                logger.debug(f"GitHub API check error: {api_err}")

    except Exception as e:
        logger.error(f"Error reading version info: {e}")

    return version_info

def apply_git_update():
    """Pulls latest updates from GitHub, repackages extensions, and triggers graceful restart."""
    try:
        # 1. Pull latest code from GitHub
        pull_res = subprocess.run(
            ['git', 'pull', 'origin', 'main'], 
            capture_output=True, 
            text=True, 
            timeout=30
        )
        
        if pull_res.returncode != 0:
            # Try reset --hard in case of minor local file changes
            reset_res = subprocess.run(
                ['git', 'reset', '--hard', 'origin/main'],
                capture_output=True,
                text=True,
                timeout=15
            )
            if reset_res.returncode != 0:
                return {
                    "status": "error", 
                    "message": f"Update failed: {pull_res.stderr or reset_res.stderr}"
                }

        # 2. Repackage browser extensions
        try:
            if os.path.exists("package_extensions.py"):
                subprocess.run(['python', 'package_extensions.py'], timeout=10)
        except Exception as ext_e:
            logger.warning(f"Extension repackaging after update skipped: {ext_e}")

        # 3. Schedule graceful restart
        def _graceful_restart():
            time.sleep(2)
            logger.info("Restarting LinkForge to apply update...")
            os._exit(0)

        threading.Thread(target=_graceful_restart, daemon=True, name="AppRestartWorker").start()

        return {
            "status": "success",
            "message": "LinkForge has been updated successfully! Restarting the server now..."
        }
    except Exception as e:
        logger.error(f"Error applying update: {e}")
        return {"status": "error", "message": str(e)}
