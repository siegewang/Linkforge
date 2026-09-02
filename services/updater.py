import os
import subprocess
import threading
import time
import json
import logging
import requests

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def get_git_commit_pure_python(repo_dir):
    """Directly parses .git folder using pure Python to get commit hash without requiring git binary."""
    try:
        git_dir = os.path.join(repo_dir, '.git')
        if not os.path.exists(git_dir):
            return None
        
        head_file = os.path.join(git_dir, 'HEAD')
        if not os.path.exists(head_file):
            return None
            
        with open(head_file, 'r', encoding='utf-8', errors='ignore') as f:
            head_content = f.read().strip()
            
        if head_content.startswith('ref:'):
            ref_path = head_content.split(' ', 1)[1].strip()
            ref_file = os.path.join(git_dir, ref_path.replace('/', os.sep))
            if os.path.exists(ref_file):
                with open(ref_file, 'r', encoding='utf-8', errors='ignore') as rf:
                    return rf.read().strip()[:7]
            
            # Check packed-refs
            packed_file = os.path.join(git_dir, 'packed-refs')
            if os.path.exists(packed_file):
                with open(packed_file, 'r', encoding='utf-8', errors='ignore') as pf:
                    for line in pf:
                        line = line.strip()
                        if line and not line.startswith('#') and ref_path in line:
                            return line.split(' ')[0][:7]
        else:
            return head_content[:7]
    except Exception as e:
        logger.debug(f"Pure python git read error: {e}")
    return None

def get_github_token():
    """Retrieve GitHub token from environment variable or SQLite settings table for private repositories."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        from services.db import get_db
        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key = 'github_token'").fetchone()
        if row and row[0]:
            return row[0].strip()
    except Exception:
        pass
    return None

def get_version_info():
    """Returns local version, commit hash, and checks remote GitHub repository for updates."""
    # 1. Read static fallback from version.json if available
    v_data = {}
    v_path = os.path.join(BASE_DIR, "version.json")
    if os.path.exists(v_path):
        try:
            with open(v_path, 'r', encoding='utf-8') as f:
                v_data = json.load(f)
        except Exception:
            pass

    version_info = {
        "version_tag": v_data.get("version", "2.1.0"),
        "current_commit": v_data.get("commit", "2b20c6a"),
        "current_commit_date": v_data.get("commit_date", "Today"),
        "current_commit_msg": "LinkForge Release",
        "branch": v_data.get("branch", "main"),
        "remote_url": "https://github.com/siegewang/Linkforge",
        "update_available": False,
        "remote_commit": "",
        "remote_commit_msg": "",
        "pending_commits": [],
        "last_checked": time.strftime("%Y-%m-%d %H:%M:%S")
    }

    # 2. Try pure Python .git parser
    py_commit = get_git_commit_pure_python(BASE_DIR)
    if py_commit:
        version_info["current_commit"] = py_commit

    # 3. Try git CLI if available for richer details (dates, messages)
    try:
        local_hash = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'], 
            cwd=BASE_DIR,
            stderr=subprocess.DEVNULL, 
            text=True
        ).strip()
        if local_hash:
            version_info["current_commit"] = local_hash

        local_date = subprocess.check_output(
            ['git', 'log', '-1', '--format=%cd', '--date=relative'], 
            cwd=BASE_DIR,
            stderr=subprocess.DEVNULL, 
            text=True
        ).strip()
        if local_date:
            version_info["current_commit_date"] = local_date

        local_msg = subprocess.check_output(
            ['git', 'log', '-1', '--format=%s'], 
            cwd=BASE_DIR,
            stderr=subprocess.DEVNULL, 
            text=True
        ).strip()
        if local_msg:
            version_info["current_commit_msg"] = local_msg
    except Exception:
        pass

    # 4. Check Remote via GitHub REST API (Works for both public and private repositories)
    try:
        api_url = "https://api.github.com/repos/siegewang/Linkforge/commits/main"
        headers = {"User-Agent": "LinkForge-App"}
        gh_token = get_github_token()
        if gh_token:
            headers["Authorization"] = f"token {gh_token}"
            
        resp = requests.get(api_url, timeout=5, headers=headers)
        if resp.status_code == 200:
            gh_data = resp.json()
            r_sha = gh_data.get("sha", "")[:7]
            version_info["remote_commit"] = r_sha
            version_info["remote_commit_msg"] = gh_data.get("commit", {}).get("message", "").split('\n')[0]
            
            # An update is available when GitHub has a newer commit hash that doesn't match local
            if r_sha and r_sha != version_info["current_commit"]:
                version_info["update_available"] = True
    except Exception as api_err:
        logger.debug(f"GitHub API check error: {api_err}")
        
        # Fallback to git ls-remote if GitHub API was blocked or offline
        try:
            remote_out = subprocess.check_output(
                ['git', 'ls-remote', 'origin', f'refs/heads/{version_info["branch"]}'], 
                cwd=BASE_DIR,
                stderr=subprocess.DEVNULL, 
                text=True, 
                timeout=5
            ).strip()
            if remote_out:
                r_sha = remote_out.split()[0][:7]
                version_info["remote_commit"] = r_sha
                if r_sha != version_info["current_commit"]:
                    version_info["update_available"] = True
        except Exception:
            pass

    return version_info

def apply_git_update():
    """Pulls latest updates from GitHub, repackages extensions, and triggers graceful restart."""
    try:
        # 1. Pull latest code from GitHub (Supports private repos with token auth)
        gh_token = get_github_token()
        if gh_token:
            import base64
            auth_header = f"Authorization: Basic {base64.b64encode(f'token:{gh_token}'.encode()).decode()}"
            pull_cmd = ['git', '-c', f'http.extraHeader={auth_header}', 'pull', 'origin', 'main']
            reset_cmd = ['git', '-c', f'http.extraHeader={auth_header}', 'reset', '--hard', 'origin/main']
        else:
            pull_cmd = ['git', 'pull', 'origin', 'main']
            reset_cmd = ['git', 'reset', '--hard', 'origin/main']

        pull_res = subprocess.run(
            pull_cmd, 
            cwd=BASE_DIR,
            capture_output=True, 
            text=True, 
            timeout=30
        )
        
        if pull_res.returncode != 0:
            reset_res = subprocess.run(
                reset_cmd,
                cwd=BASE_DIR,
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
            pkg_script = os.path.join(BASE_DIR, "package_extensions.py")
            if os.path.exists(pkg_script):
                subprocess.run(['python', pkg_script], cwd=BASE_DIR, timeout=10)
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
