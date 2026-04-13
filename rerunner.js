// ==UserScript==
// @name         GitHub Action Auto-Rerunner
// @namespace    https://github.com/StarlightIbuki
// @description  Stable Auto Rerunner
// @version      1.0
// @match        https://github.com/*/*/actions/runs/*
// @icon         https://github.githubassets.com/images/modules/site/features/actions-icon-actions.svg
// @grant        none
// @updateURL    https://raw.githubusercontent.com/StarlightIbuki/Github-Helper/main/rerunner.js
// @downloadURL  https://raw.githubusercontent.com/StarlightIbuki/Github-Helper/main/rerunner.js
// ==/UserScript==

(function() {
    'use strict';

    const STORAGE_KEY = 'gh_rerun_v13';
    const MONO_FONT = 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace';

    // --- Debug Helper ---
    const log = (...args) => {
        console.log('%c[GH-Auto-Rerun]', 'color: #0366d6; font-weight: bold; background: #f1f8ff; padding: 2px 5px; border-radius: 3px;', ...args);
    };

    // --- State Management ---
    const getRunId = () => window.location.pathname.match(/\/actions\/runs\/(\d+)/)?.[1];

    const loadState = () => {
        const id = getRunId();
        const all = JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
        const s = all[id] || { retryCount: 0, maxRetries: 3, isRunning: false, status: 'idle' };
        log('State Loaded:', s);
        return s;
    };

    const saveState = (s) => {
        const id = getRunId();
        if (!id) return;
        const all = JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
        all[id] = s;
        localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
    };

    let state = loadState();
    let isTransitioning = false;
    let pollInterval = null;

    const startPolling = () => {
        if (pollInterval) return;
        pollInterval = setInterval(() => {
            if (!state.isRunning) { stopPolling(); return; }
            performCheck();
        }, 3000);
    };

    const stopPolling = () => {
        if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
    };

    // --- UI Logic ---
    const injectStyles = () => {
        if (document.getElementById('tm-rerun-styles')) return;
        const style = document.createElement('style');
        style.id = 'tm-rerun-styles';
        style.innerHTML = `
            @keyframes tm-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
            @keyframes tm-pulse { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
            .tm-status-waiting { animation: tm-pulse 2s infinite; color: var(--color-success-fg); }
            .tm-status-triggering { animation: tm-spin 1s infinite linear; color: var(--color-attention-fg); }
            #tm-rerun-wrapper { white-space: nowrap !important; display: flex; align-items: center; flex-shrink: 0; border-right: 1px solid var(--color-border-default); }
            .tm-mono { font-family: ${MONO_FONT} !important; font-size: 11px !important; line-height: 20px !important; }
            .tm-counter-container { display: inline-flex; align-items: center; margin-left: 6px; color: var(--color-fg-muted); }
            .tm-input-slot { display: inline-grid; place-items: center start; min-width: 18px; }
            .tm-limit-edit { width: 24px !important; height: 18px !important; border: 1px solid var(--color-accent-emphasis) !important; text-align: center; background: var(--color-canvas-default) !important; color: var(--color-fg-default) !important; }
            .tm-clickable { cursor: pointer; padding: 0; }
            .tm-clickable:hover { color: var(--color-accent-fg) !important; text-decoration: underline; }
        `;
        document.head.appendChild(style);
    };

    const updateUIState = () => {
        const btn = document.getElementById('tm-toggle');
        const icon = document.getElementById('tm-icon');
        const currentCount = document.getElementById('tm-current-count');
        const maxDisplay = document.getElementById('tm-max-display');
        const wrapper = document.getElementById('tm-rerun-wrapper');

        if (!btn || !icon) return;

        btn.innerText = state.isRunning ? 'Stop' : 'Start auto-retry';
        btn.className = `btn btn-sm ml-2 ${state.isRunning ? 'btn-danger' : 'btn-primary'}`;

        const info = getStatusInfo();
        icon.className.baseVal = `octicon octicon-sync ${info.iconClass}`;
        wrapper.title = info.tip;

        currentCount.innerText = `${state.retryCount}/`;
        currentCount.style.display = (state.isRunning || state.retryCount > 0) ? 'inline' : 'none';
        maxDisplay.innerText = state.maxRetries;
    };

    const getStatusInfo = () => {
        if (isTransitioning) return { iconClass: 'tm-status-triggering', tip: 'Syncing: Waiting for GitHub UI to refresh...' };
        switch(state.status) {
            case 'waiting': return { iconClass: 'tm-status-waiting', tip: 'Watching: Waiting for failure...' };
            case 'triggering': return { iconClass: 'tm-status-triggering', tip: 'Action: Rerun requested...' };
            case 'finished': return { iconClass: 'tm-status-finished', tip: 'Done: Workflow succeeded.' };
            case 'limit': return { iconClass: 'tm-status-error', tip: 'Stopped: Max retries reached.' };
            default: return { iconClass: '', tip: 'Idle: Click to start.' };
        }
    };

    // --- Event-Driven Logic ---

    const performCheck = () => {
        if (!state.isRunning) return;
        if (isTransitioning) {
            log('Skipping check: Currently in transition/cooldown lock.');
            return;
        }

        const successBadge = document.querySelector('.State--success, [data-testid="workflow-run-status-badge-success"]');
        const failureBadge = document.querySelector('.State--failed, [data-testid="workflow-run-status-badge-failure"]');
        const inProgressBadge = document.querySelector('.State--pending, .State--queued, .State--in-progress, [data-testid*="status-badge-queued"], [data-testid*="status-badge-in-progress"]');

        log('Scanning status badges...', { success: !!successBadge, failure: !!failureBadge, progress: !!inProgressBadge });

        if (successBadge) {
            log('Success detected. Stopping.');
            state.isRunning = false; state.status = 'finished';
            saveState(state); updateUIState(); stopPolling(); return;
        }

        if (inProgressBadge) {
            if (state.status !== 'waiting') {
                log('Run in progress. Entering "waiting" mode.');
                state.status = 'waiting'; saveState(state); updateUIState();
            }
            return;
        }

        if (failureBadge) {
            log('Failure detected!');
            if (state.retryCount < state.maxRetries) {
                log(`Attempting rerun ${state.retryCount + 1}/${state.maxRetries}`);
                triggerRerun();
            } else {
                log('Retry limit reached. Stopping.');
                state.isRunning = false; state.status = 'limit';
                saveState(state); updateUIState(); stopPolling();
            }
        }
    };

    const triggerRerun = async () => {
        if (isTransitioning) return;
        isTransitioning = true;
        state.status = 'triggering';
        updateUIState();

        log('Searching for rerun buttons...');

        try {
            // Find the main "Re-run jobs" button or menu
            const buttons = Array.from(document.querySelectorAll('button, summary'));
            const mainBtn = buttons.find(el =>
                (el.textContent.includes('Re-run jobs') || el.getAttribute('data-testid') === 're-run-jobs-menu') &&
                !el.closest('#tm-rerun-wrapper')
            );

            if (!mainBtn) {
                throw new Error('CRITICAL: Could not find the main "Re-run jobs" button on the page.');
            }

            log('Found main button/menu:', mainBtn);

            if (mainBtn.tagName === 'SUMMARY' || mainBtn.getAttribute('aria-haspopup')) {
                log('Main button is a menu. Clicking to open...');
                mainBtn.click();
                await new Promise(r => setTimeout(r, 600)); // Wait for animation
            }

            // Look for "Re-run failed jobs"
            const failedItem = document.querySelector('button[data-show-dialog-id="rerun-dialog-failed"], .dropdown-item[data-show-dialog-id*="failed"]');

            if (failedItem) {
                log('Found "Re-run failed jobs" option. Clicking...', failedItem);
                failedItem.click();

                await new Promise(r => setTimeout(r, 800)); // Wait for dialog

                const confirmBtn = Array.from(document.querySelectorAll('button[type="submit"], button.Button--primary'))
                    .find(el => el.textContent.trim().toLowerCase().includes('re-run jobs'));

                if (confirmBtn) {
                    log('Found confirmation dialog button. Final click!');
                    state.retryCount++;
                    saveState(state);
                    confirmBtn.click();
                } else {
                    throw new Error('Could not find the final confirmation button in the dialog.');
                }
            } else {
                log('No "Failed jobs" specific option found. Triggering direct rerun...');
                mainBtn.click();
                state.retryCount++;
                saveState(state);
            }

            log('Rerun triggered. Entering transition lock to wait for UI update.');

            // Safety timeout: Unlock after 15s regardless if UI doesn't update
            const safetyTimeout = setTimeout(() => {
                if (isTransitioning) {
                    log('Transition lock safety timeout reached. Unlocking.');
                    isTransitioning = false;
                    updateUIState();
                }
            }, 15000);

            // Wait for badge to change to "Queued" or "Pending"
            let checks = 0;
            const waitForQueued = setInterval(() => {
                const isQueued = document.querySelector('.State--pending, .State--queued, [data-testid*="queued"]');
                checks++;
                if (isQueued) {
                    log('UI acknowledged rerun (Queued). Releasing lock.');
                    clearInterval(waitForQueued);
                    clearTimeout(safetyTimeout);
                    isTransitioning = false;
                    performCheck();
                } else if (checks > 20) {
                    log('UI update slow/missed. Releasing lock anyway.');
                    clearInterval(waitForQueued);
                    clearTimeout(safetyTimeout);
                    isTransitioning = false;
                }
            }, 500);

        } catch (e) {
            console.error('%c[GH-Auto-Rerun Error]', 'background: red; color: white;', e.message);
            isTransitioning = false;
            state.status = 'idle';
            updateUIState();
        }
    };

    // --- Observers ---
    let lastUrl = location.href;
    const navObserver = new MutationObserver(() => {
        if (location.href !== lastUrl) {
            log('Navigation detected:', location.href);
            lastUrl = location.href;
            const existing = document.getElementById('tm-rerun-wrapper');
            if (existing) existing.remove();
        }
        if (window.location.href.includes('/actions/runs/')) injectUI();
    });
    navObserver.observe(document.body, { childList: true, subtree: true });

    const statusObserver = new MutationObserver((mutations) => {
        // Debounce/filter mutations slightly to avoid noise
        if (mutations.some(m => m.addedNodes.length || m.removedNodes.length)) {
            performCheck();
        }
    });

    const injectUI = () => {
        const toolbar = document.querySelector('.js-check-run-search, .gh-header-actions');
        if (!toolbar || document.getElementById('tm-rerun-wrapper')) return;

        log('Injecting UI into toolbar...');
        injectStyles();
        state = loadState();

        const wrapper = document.createElement('div');
        wrapper.id = 'tm-rerun-wrapper';
        wrapper.className = 'mr-2 pr-2';
        wrapper.innerHTML = `
            <svg id="tm-icon" class="octicon octicon-sync" viewBox="0 0 16 16" width="14" height="14" fill="currentColor">
                <path d="M1.705 8.005a.75.75 0 0 1 .834.656 5.5 5.5 0 0 0 9.592 2.97l-1.204-1.204a.25.25 0 0 1 .177-.427h3.646a.25.25 0 0 1 .25.25v3.646a.25.25 0 0 1-.427.177l-1.38-1.38A7.002 7.002 0 0 1 1.05 8.84a.75.75 0 0 1 .656-.834ZM8 2.5a5.487 5.487 0 0 0-4.131 1.869l1.204 1.204A.25.25 0 0 1 4.896 6H1.25A.25.25 0 0 1 1 5.75V2.104a.25.25 0 0 1 .427-.177l1.38 1.38A7.002 7.002 0 0 1 14.95 7.16a.75.75 0 0 1-1.49.178A5.5 5.5 0 0 0 8 2.5Z"></path>
            </svg>
            <div class="tm-mono tm-counter-container">
                <span id="tm-current-count" class="tm-clickable" title="Reset counter"></span>
                <div class="tm-input-slot"><span id="tm-max-display" class="tm-clickable" title="Edit limit"></span></div>
            </div>
            <button type="button" id="tm-toggle" style="height: 24px; padding: 0 8px; font-size: 11px;"></button>
        `;

        toolbar.prepend(wrapper);

        document.getElementById('tm-toggle').onclick = () => {
            state.isRunning = !state.isRunning;
            state.status = state.isRunning ? 'waiting' : 'idle';
            log('Toggle clicked. Running:', state.isRunning);
            saveState(state);
            updateUIState();
            if (state.isRunning) {
                // Treat the click as an event: check immediately, then retry at 500ms/1.5s
                // in case the badge isn't in the DOM yet (async render on static pages).
                performCheck();
                setTimeout(performCheck, 500);
                setTimeout(performCheck, 1500);
                startPolling();
            } else {
                stopPolling();
            }
        };

        document.getElementById('tm-current-count').onclick = () => {
            if (confirm(`Reset retry count?`)) { state.retryCount = 0; saveState(state); updateUIState(); }
        };

        // Attach Observer to the main status area
        const statusArea = document.querySelector('.Layout-main') || document.body;
        statusObserver.disconnect();
        statusObserver.observe(statusArea, { childList: true, subtree: true });

        updateUIState();
        performCheck();
    };

    injectUI();
})();