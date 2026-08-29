// content.js
// Runs passively on all pages to track attention

let timeOnPage = 0;
let maxScrollDepth = 0;
let hasLogged = false;
let isCancelled = false;
function getCoreUrl() {
    try {
        let url = new URL(window.location.href);
        if (url.hostname.includes('youtube.com')) {
            // YouTube videos use the same pathname (/watch) but change the 'v' query parameter
            return url.pathname + (url.searchParams.get('v') || '');
        }
        // For other sites (like Daily Mail), ignore query parameters entirely to stop tracking scripts from resetting the timer
        return url.pathname;
    } catch(e) {
        return window.location.pathname;
    }
}

let currentUrl = getCoreUrl();

let dfUiContainer = null;
let dfCountdownText = null;

let userConfig = {
    showCountdownTimer: true,
    pageLogDelay: 30,
    youtubeLogDelay: 15
};

function applyTimerVisibility() {
    if (!dfCountdownText) return;
    if (userConfig.showCountdownTimer !== false) {
        dfCountdownText.style.display = 'block';
    } else {
        const txt = dfCountdownText.innerText || "";
        const isStatusMsg = txt.includes("ERR") || txt.includes("NO TXT") || txt.includes("SENT") || txt.includes("LOGGING");
        dfCountdownText.style.display = isStatusMsg ? 'block' : 'none';
    }
}

// Load initial config from storage
if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {
    chrome.storage.local.get({
        showCountdownTimer: true,
        showCountdownBadge: true,
        pageLogDelay: 30,
        youtubeLogDelay: 15
    }, (cfg) => {
        if (cfg) {
            userConfig.showCountdownTimer = cfg.showCountdownTimer !== undefined ? cfg.showCountdownTimer : (cfg.showCountdownBadge !== false);
            userConfig.pageLogDelay = cfg.pageLogDelay || 30;
            userConfig.youtubeLogDelay = cfg.youtubeLogDelay || 15;
            applyTimerVisibility();
        }
    });

    chrome.storage.onChanged.addListener((changes, area) => {
        if (area === 'local') {
            if (changes.showCountdownTimer !== undefined) userConfig.showCountdownTimer = changes.showCountdownTimer.newValue;
            else if (changes.showCountdownBadge !== undefined) userConfig.showCountdownTimer = changes.showCountdownBadge.newValue;
            if (changes.pageLogDelay !== undefined) userConfig.pageLogDelay = changes.pageLogDelay.newValue;
            if (changes.youtubeLogDelay !== undefined) userConfig.youtubeLogDelay = changes.youtubeLogDelay.newValue;
            applyTimerVisibility();
        }
    });
}

if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((message) => {
        if (message && message.type === "UPDATE_CONFIG" && message.payload) {
            userConfig = Object.assign(userConfig, message.payload);
            applyTimerVisibility();
        }
    });
}

let isUrlPermanentlyDenied = false;

function getExactCleanUrl() {
    return window.location.href.split('#')[0];
}

function checkDeniedStatus() {
    if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {
        chrome.storage.local.get("deniedUrls", (data) => {
            const list = data.deniedUrls || [];
            const clean = getExactCleanUrl();
            if (list.includes(clean)) {
                isUrlPermanentlyDenied = true;
                if (dfUiContainer) {
                    dfUiContainer.style.display = 'none';
                }
            }
        });
    }
}
checkDeniedStatus();

function checkValidPage() {
    if (isUrlPermanentlyDenied || isCancelled) {
        return false;
    }

    const url = window.location.href;
    const hostname = window.location.hostname;
    const pathname = window.location.pathname;

    if (hostname.includes('youtube.com') && pathname !== '/watch') {
        return false;
    }

    if (url.includes('google.com/search') || 
        url.includes('/login') || 
        url.includes('/auth') || 
        url.includes('facebook.com') ||
        url.includes('instagram.com') ||
        url.includes('twitter.com') ||
        url.includes('x.com') ||
        url.includes('192.168.0.77') || 
        url.includes('127.0.0.1') ||
        url.includes('localhost')) {
        return false;
    }
    return true;
}

function getPageMetadata() {
    const textContent = extractReadableText();
    const favicon = document.querySelector('link[rel~="icon"]')?.href || '';
    let image_url = document.querySelector('meta[property="og:image"]')?.content || document.querySelector('meta[name="twitter:image"]')?.content || '';
    
    if (window.location.hostname.includes('reddit.com')) {
        const redditRealImg = document.querySelector('shreddit-post img.preview, shreddit-post img, gallery-carousel ul li img, a[href*="preview.redd.it"] img, img[alt="Post image"]');
        if (redditRealImg && redditRealImg.src) {
            image_url = redditRealImg.src;
        } else {
            const redditVid = document.querySelector('shreddit-post shreddit-player-2');
            if (redditVid && redditVid.getAttribute('poster')) image_url = redditVid.getAttribute('poster');
        }
        if (image_url.includes('redditstatic.com') || image_url.includes('snoo') || image_url.includes('reddit-logo')) {
            image_url = redditRealImg ? redditRealImg.src : '';
        }
    }

    return {
        url: window.location.href,
        title: document.title,
        text: textContent,
        html: extractReadableHtml(), 
        favicon: favicon,
        image_url: image_url
    };
}

function initDashForgeUI() {
    if (document.getElementById('dashforge-memory-ui')) return;
    
    dfUiContainer = document.createElement('div');
    dfUiContainer.id = 'dashforge-memory-ui';
    dfUiContainer.style.cssText = `
        position: fixed;
        bottom: 20px;
        right: 20px;
        background: rgba(9, 9, 11, 0.85);
        backdrop-filter: blur(10px);
        -webkit-backdrop-filter: blur(10px);
        border: 1px solid rgba(6, 182, 212, 0.4);
        border-radius: 12px;
        padding: 6px 12px;
        display: none;
        align-items: center;
        gap: 10px;
        z-index: 2147483647;
        font-family: monospace;
        color: #fff;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5), 0 0 15px rgba(6, 182, 212, 0.2);
        transition: all 0.3s ease;
        pointer-events: auto;
    `;
    
    dfCountdownText = document.createElement('div');
    dfCountdownText.style.cssText = 'font-size: 14px; color: #06b6d4; font-weight: bold; min-width: 32px; text-align: center;';
    dfCountdownText.innerText = '30s';
    applyTimerVisibility();
    
    // Green tick button to immediately log the page
    const logNowButton = document.createElement('button');
    logNowButton.innerHTML = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>';
    logNowButton.style.cssText = `
        background: transparent;
        border: none;
        color: #22c55e; /* bright green */
        padding: 4px;
        border-radius: 6px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: all 0.2s ease;
    `;
    logNowButton.title = "Log Page Immediately";
    logNowButton.onmouseover = () => { logNowButton.style.background = 'rgba(34, 197, 94, 0.15)'; };
    logNowButton.onmouseout = () => { logNowButton.style.background = 'transparent'; };
    logNowButton.onclick = () => {
        if (hasLogged || isCancelled || !checkValidPage()) return;
        hasLogged = true;
        if (dfCountdownText) {
            dfCountdownText.style.display = 'block';
            dfCountdownText.style.color = '#eab308';
            dfCountdownText.innerText = 'LOGGING...';
        }
        logPageToDashForge();
    };

    const cancelButton = document.createElement('button');
    cancelButton.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
    cancelButton.style.cssText = `
        background: transparent;
        border: none;
        color: #ef4444; /* bright red */
        padding: 4px;
        border-radius: 6px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: all 0.2s ease;
    `;
    cancelButton.title = "Abort Link Logging (Never log this page again)";
    cancelButton.onmouseover = () => { cancelButton.style.background = 'rgba(239, 68, 68, 0.15)'; };
    cancelButton.onmouseout = () => { cancelButton.style.background = 'transparent'; };
    cancelButton.onclick = async () => {
        isCancelled = true;
        isUrlPermanentlyDenied = true;
        const cleanUrl = getExactCleanUrl();

        // 1. Add to permanent denied URLs list in extension storage
        try {
            const { deniedUrls = [] } = await chrome.storage.local.get("deniedUrls");
            if (!deniedUrls.includes(cleanUrl)) {
                deniedUrls.push(cleanUrl);
                await chrome.storage.local.set({ deniedUrls });
            }
        } catch(e) {}

        // 2. Notify background script to register denial with backend
        try {
            chrome.runtime.sendMessage({
                type: "DENY_URL",
                payload: { url: cleanUrl }
            });
        } catch(e) {}

        // 3. Immediately hide floating capsule
        dfUiContainer.style.opacity = '0';
        setTimeout(() => {
            if (dfUiContainer) dfUiContainer.style.display = 'none';
        }, 300);
    };

    const homeBtn = document.createElement('button');
    homeBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path><polyline points="9 22 9 12 15 12 15 22"></polyline></svg>';
    homeBtn.style.cssText = `
        background: rgba(39, 39, 42, 0.8);
        color: #a1a1aa;
        border: 1px solid rgba(82, 82, 91, 0.4);
        padding: 4px;
        border-radius: 6px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: all 0.2s ease;
    `;
    homeBtn.title = "Pin to Homepage";
    homeBtn.onclick = () => {
        chrome.runtime.sendMessage({
            type: "PIN_PAGE",
            payload: getPageMetadata()
        }, (response) => {
            if (response && response.success) {
                homeBtn.style.color = '#34d399';
                homeBtn.style.borderColor = 'rgba(52, 211, 153, 0.4)';
            }
        });
    };
    
    dfUiContainer.appendChild(dfCountdownText);
    dfUiContainer.appendChild(logNowButton);
    dfUiContainer.appendChild(cancelButton);
    dfUiContainer.appendChild(homeBtn);
    
    document.body.appendChild(dfUiContainer);
}

if (document.body) {
    initDashForgeUI();
} else {
    document.addEventListener('DOMContentLoaded', initDashForgeUI);
}

setInterval(() => {
    if (!document.getElementById('dashforge-memory-ui') && document.body) {
        initDashForgeUI();
    }

    const newUrl = getCoreUrl();
    if (newUrl !== currentUrl) {
        currentUrl = newUrl;
        timeOnPage = 0;
        maxScrollDepth = 0;
        hasLogged = false;
        isCancelled = false;
        isUrlPermanentlyDenied = false;
        checkDeniedStatus();
        if (dfUiContainer) dfUiContainer.style.opacity = '1';
        if (dfCountdownText) dfCountdownText.style.color = '#a1a1aa';
    }

    if (document.hidden) return;
    timeOnPage++;
    
    const isYouTube = window.location.hostname.includes('youtube.com') || window.location.hostname.includes('youtu.be');
    const threshold = isYouTube ? (userConfig.youtubeLogDelay || 15) : (userConfig.pageLogDelay || 30);
    const timeLeft = Math.max(0, threshold - timeOnPage);
    const valid = checkValidPage();

    if (dfUiContainer && dfCountdownText) {
        if (!hasLogged && !isCancelled && valid) {
            dfCountdownText.innerText = `${timeLeft}s`;
            applyTimerVisibility();

            if (dfUiContainer.style.display === 'none' || dfUiContainer.style.display === '') {
                 dfUiContainer.style.display = 'flex';
                 dfUiContainer.style.opacity = '1';
            }
        } else if (hasLogged || isCancelled || !valid) {
            applyTimerVisibility();
            const hasStatusMsg = dfCountdownText.innerText.includes("ERR") || 
                                 dfCountdownText.innerText.includes("NO TXT") || 
                                 dfCountdownText.innerText.includes("SENT") || 
                                 dfCountdownText.innerText.includes("LOGGING");
            
            if (!hasStatusMsg) {
                dfUiContainer.style.opacity = '0';
                setTimeout(() => { if (dfUiContainer) dfUiContainer.style.display = 'none'; }, 300);
            }
        }
    }

    checkAttentionThreshold(threshold);
}, 1000);

function checkAttentionThreshold(threshold) {
    if (hasLogged || isCancelled || !checkValidPage()) return;

    if (timeOnPage >= threshold) {
        hasLogged = true;
        
        if (dfCountdownText) {
            dfCountdownText.style.display = 'block';
            dfCountdownText.style.color = '#eab308';
            dfCountdownText.innerText = 'LOGGING...';
        }
        
        logPageToDashForge();
    }
}

function extractReadableText() {
    const textNodes = document.querySelectorAll('p, h1, h2, h3');
    let text = Array.from(textNodes).map(el => el.innerText).join(' ');
    
    if (text.length < 200) {
        text = document.body.innerText || "";
    }
    
    text = text.replace(/\\s+/g, ' ').trim();
    return text.substring(0, 4000);
}

function extractReadableHtml() {
    // Try to find the main article container
    let container = document.querySelector('article') || document.querySelector('main');
    if (container) return container.innerHTML;
    
    // Fallback: piece together paragraphs and images for a low-fi archive
    const nodes = document.querySelectorAll('p, h1, h2, h3, img, figure');
    return Array.from(nodes).map(el => el.outerHTML).join('\n<br>\n');
}

function logPageToDashForge() {
    const payload = getPageMetadata();
    const isYouTube = window.location.hostname.includes('youtube.com') || window.location.hostname.includes('youtu.be');
    
    if (!isYouTube && payload.text.length < 200) {
        if (dfCountdownText) {
            dfCountdownText.style.display = 'block';
            dfCountdownText.style.color = '#ef4444';
            dfCountdownText.innerText = 'ERR: NO TXT';
        }
        return;
    }

    chrome.runtime.sendMessage({
        type: "LOG_PAGE",
        payload: payload
    }, (response) => {
        if (chrome.runtime.lastError) {
            if (dfCountdownText) {
                dfCountdownText.style.display = 'block';
                dfCountdownText.style.color = '#ef4444';
                dfCountdownText.innerText = 'ERR: DISCONNECTED';
            }
        } else if (!response || !response.success) {
            if (dfCountdownText) {
                dfCountdownText.style.display = 'block';
                dfCountdownText.style.color = '#ef4444';
                // Show the exact error returned by background.js, truncated to fit
                const errMsg = response && response.error ? response.error : 'UNKNOWN';
                dfCountdownText.innerText = 'ERR: ' + errMsg.substring(0, 20);
            }
        } else {
            if (dfCountdownText) {
                dfCountdownText.style.display = 'block';
                dfCountdownText.style.color = '#4ade80';
                dfCountdownText.innerText = 'SENT!';
            }
            setTimeout(() => {
                if (dfUiContainer) dfUiContainer.style.opacity = '0';
            }, 3000);
        }
    });
}
