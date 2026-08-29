async function syncDeniedUrls() {
    try {
        const { dashforgeUrl = "http://192.168.0.77:5006" } = await chrome.storage.local.get("dashforgeUrl");
        const baseUrl = dashforgeUrl.replace(/\/$/, "");
        const res = await fetch(`${baseUrl}/api/links/denied`);
        if (res.ok) {
            const data = await res.json();
            if (data && Array.isArray(data.denied_urls)) {
                await chrome.storage.local.set({ deniedUrls: data.denied_urls });
                return data.denied_urls;
            }
        }
    } catch (e) {
        console.warn("Could not sync denied URLs from backend:", e);
    }
    return null;
}

chrome.runtime.onInstalled.addListener(() => syncDeniedUrls());
chrome.runtime.onStartup.addListener(() => syncDeniedUrls());
syncDeniedUrls();

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === "SYNC_DENIED_URLS") {
        syncDeniedUrls().then(res => sendResponse(res));
        return true;
    }
    if (message.type === "LOG_PAGE") {
        handleLogPage(message.payload).then(result => sendResponse(result));
        return true; 
    }
    if (message.type === "PIN_PAGE") {
        handlePinPage(message.payload).then(result => sendResponse(result));
        return true; 
    }
    if (message.type === "DENY_URL") {
        handleDenyUrl(message.payload).then(result => sendResponse(result));
        return true;
    }
    if (message.type === "UNDENY_URL") {
        handleUndenyUrl(message.payload).then(result => sendResponse(result));
        return true;
    }
});

async function handleDenyUrl(payload) {
    try {
        const { dashforgeUrl = "http://192.168.0.77:5006" } = await chrome.storage.local.get("dashforgeUrl");
        const baseUrl = dashforgeUrl.replace(/\/$/, "");
        await fetch(`${baseUrl}/api/links/deny`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        return { success: true };
    } catch (e) {
        return { success: false, error: e.message };
    }
}

async function handleUndenyUrl(payload) {
    try {
        const { dashforgeUrl = "http://192.168.0.77:5006" } = await chrome.storage.local.get("dashforgeUrl");
        const baseUrl = dashforgeUrl.replace(/\/$/, "");
        await fetch(`${baseUrl}/api/links/undeny`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        return { success: true };
    } catch (e) {
        return { success: false, error: e.message };
    }
}

async function handlePinPage(payload) {
    console.log("[DashForge] Background pinning payload for:", payload.url);
    try {
        const { dashforgeUrl = "http://192.168.0.77:5006" } = await chrome.storage.local.get("dashforgeUrl");
        const baseUrl = dashforgeUrl.replace(/\/$/, "");
        
        const response = await fetch(`${baseUrl}/api/links/pin`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(payload)
        });
        
        if (response.ok) {
            return { success: true };
        } else {
            return { success: false, error: "Backend rejected pin" };
        }
    } catch (e) {
        console.error("Error pinning page:", e);
        return { success: false, error: e.message };
    }
}

async function handleLogPage(payload) {
    console.log("[DashForge] Background received payload for:", payload.url);
    try {
        const { dashforgeUrl = "http://192.168.0.77:5006" } = await chrome.storage.local.get("dashforgeUrl");
        
        // Remove trailing slash if user added it
        const baseUrl = dashforgeUrl.replace(/\/$/, "");
        
        const response = await fetch(`${baseUrl}/api/links/auto-log`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(payload)
        });
        
        if (response.ok) {
            console.log("Successfully logged to DashForge:", payload.url);
            return { success: true };
        } else {
            console.error("Failed to log to DashForge:", await response.text());
            return { success: false, error: "Backend rejected" };
        }
    } catch (e) {
        console.error("Error connecting to DashForge Server:", e);
        return { success: false, error: e.message };
    }
}
