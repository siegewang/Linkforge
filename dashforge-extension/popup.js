document.addEventListener('DOMContentLoaded', async () => {
    const urlInput = document.getElementById('serverUrl');
    const showTimerInput = document.getElementById('showTimer');
    const pageDelayInput = document.getElementById('pageDelay');
    const ytDelayInput = document.getElementById('ytDelay');
    const saveBtn = document.getElementById('saveBtn');
    const status = document.getElementById('status');
    const deniedCountEl = document.getElementById('deniedCount');
    const openWebAdminBtn = document.getElementById('openWebAdminBtn');

    // Load stored settings with smart defaults
    const config = await chrome.storage.local.get({
        dashforgeUrl: "http://192.168.0.77:5006",
        showCountdownTimer: true,
        pageLogDelay: 30,
        youtubeLogDelay: 15,
        deniedUrls: []
    });

    urlInput.value = config.dashforgeUrl;
    showTimerInput.checked = (config.showCountdownTimer === true || config.showCountdownTimer === undefined);
    pageDelayInput.value = config.pageLogDelay;
    ytDelayInput.value = config.youtubeLogDelay;

    function updateWebAdminLink(url) {
        if (openWebAdminBtn) {
            const baseUrl = (url || "http://192.168.0.77:5006").replace(/\/$/, "");
            openWebAdminBtn.href = `${baseUrl}/admin/data#ignored-urls-section`;
        }
    }
    updateWebAdminLink(config.dashforgeUrl);

    if (deniedCountEl) {
        deniedCountEl.textContent = (config.deniedUrls || []).length;
    }

    // Sync latest from backend database and refresh counter
    try {
        chrome.runtime.sendMessage({ type: "SYNC_DENIED_URLS" }, (latestList) => {
            if (latestList && Array.isArray(latestList) && deniedCountEl) {
                deniedCountEl.textContent = latestList.length;
            }
        });
    } catch (e) {}

    async function saveAndBroadcast() {
        let valUrl = urlInput.value.trim();
        if (!valUrl) valUrl = "http://192.168.0.77:5006";

        let pageDelay = parseInt(pageDelayInput.value, 10);
        if (isNaN(pageDelay) || pageDelay < 1) pageDelay = 30;

        let ytDelay = parseInt(ytDelayInput.value, 10);
        if (isNaN(ytDelay) || ytDelay < 1) ytDelay = 15;

        const showTimer = showTimerInput.checked;

        const newCfg = {
            dashforgeUrl: valUrl,
            showCountdownTimer: showTimer,
            pageLogDelay: pageDelay,
            youtubeLogDelay: ytDelay
        };

        await chrome.storage.local.set(newCfg);
        updateWebAdminLink(valUrl);

        // Broadcast to all active tabs
        try {
            chrome.tabs.query({}, (tabs) => {
                if (tabs) {
                    for (const tab of tabs) {
                        if (tab.id) {
                            chrome.tabs.sendMessage(tab.id, {
                                type: "UPDATE_CONFIG",
                                payload: newCfg
                            }).catch(() => {});
                        }
                    }
                }
            });
        } catch (e) {}

        status.style.display = 'block';
        setTimeout(() => {
            status.style.display = 'none';
        }, 2000);
    }

    showTimerInput.addEventListener('change', saveAndBroadcast);
    saveBtn.addEventListener('click', saveAndBroadcast);
});
