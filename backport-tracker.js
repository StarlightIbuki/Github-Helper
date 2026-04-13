// ==UserScript==
// @name         Backport Tracker
// @namespace    https://github.com/StarlightIbuki
// @version      1.0
// @description  Track backport PRs
// @match        https://github.com/*/*/pull/*
// @connect      github.com
// @run-at       document-start
// @icon         https://raw.githubusercontent.com/primer/octicons/main/icons/git-pull-request-24.svg
// @updateURL    https://raw.githubusercontent.com/StarlightIbuki/Github-Helper/main/backport-tracker.js
// @downloadURL  https://raw.githubusercontent.com/StarlightIbuki/Github-Helper/main/backport-tracker.js
// ==/UserScript==

(function() {
    'use strict';

    let backportData = [];
    let currentPrUrl = "";
    let isScanning = false;
    let refreshIntervalId = null;
    const MAX_RETRIES = 10;

    const OCTICONS = {
        check: '<svg class="octicon octicon-check color-fg-success" viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M13.78 4.22a.75.75 0 0 1 0 1.06l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 9.28a.751.751 0 0 1 .018-1.042.751.751 0 0 1 1.042-.018L6 10.94l6.72-6.72a.75.75 0 0 1 1.06 0Z"></path></svg>',
        x: '<svg class="octicon octicon-x color-fg-danger" viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M3.72 3.72a.75.75 0 0 1 1.06 0L8 6.94l3.22-3.22a.751.751 0 0 1 1.042.018.751.751 0 0 1 .018 1.042L9.06 8l3.22 3.22a.751.751 0 0 1-.018 1.042.751.751 0 0 1-1.042.018L8 9.06l-3.22 3.22a.751.751 0 0 1-1.042-.018.751.751 0 0 1-.018-1.042L6.94 8 3.72 4.78a.75.75 0 0 1 0-1.06Z"></path></svg>',
        dot: '<svg class="octicon octicon-dot-fill color-fg-attention" viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M8 4a4 4 0 1 1 0 8 4 4 0 0 1 0-8Z"></path></svg>',
        shield: '<svg class="octicon octicon-shield color-fg-attention" viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M7.467.133a1.748 1.748 0 0 1 1.066 0l5.25 1.68A1.75 1.75 0 0 1 15 3.48V7c0 1.566-.32 3.13-.935 4.423-.49 1.03-1.151 1.888-1.858 2.534l-.006.006-.007.005a10.339 10.339 0 0 1-4.201 2.031l-.001.001-.002.001a.752.752 0 0 1-.38 0l-.002-.001-.001-.001a10.339 10.339 0 0 1-4.201-2.03l-.007-.005-.006-.006c-.707-.646-1.368-1.503-1.858-2.534C.32 10.13 0 8.566 0 7V3.48c0-.712.428-1.353 1.083-1.566L6.333.234ZM8.457 1.61 3.207 3.29a.25.25 0 0 0-.154.226V7c0 1.189.24 2.453.758 3.543.376.792.903 1.488 1.516 2.048a9.138 9.138 0 0 0 2.673 1.583 9.138 9.138 0 0 0 2.673-1.583c.613-.56 1.14-1.256 1.516-2.048.518-1.09.758-2.354.758-3.543V3.516a.25.25 0 0 0-.154-.226L8.457 1.61Z"></path></svg>',
        sync: '<svg class="octicon octicon-sync" viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M1.705 8.005a.75.75 0 0 1 .834.656 5.5 5.5 0 0 0 9.592 2.97l-1.204-1.204a.25.25 0 0 1 .177-.427h3.646a.25.25 0 0 1 .25.25v3.646a.25.25 0 0 1-.427.177l-1.38-1.38A7.002 7.002 0 0 1 1.05 8.84a.75.75 0 0 1 .656-.834ZM8 2.5a5.487 5.487 0 0 0-4.131 1.869l1.204 1.204A.25.25 0 0 1 4.896 6H1.25A.25.25 0 0 1 1 5.75V2.104a.25.25 0 0 1 .427-.177l1.38 1.38A7.002 7.002 0 0 1 14.95 7.16a.75.75 0 0 1-1.49.178A5.5 5.5 0 0 0 8 2.5Z"></path></svg>',
        branch: '<svg aria-hidden="true" height="16" viewBox="0 0 16 16" width="16" fill="currentColor" class="octicon octicon-git-branch color-fg-muted"><path d="M5 3.25a.75.75 0 1 1-1.5 0 .75.75 0 0 1 1.5 0Zm0 2.122a2.25 2.25 0 1 0-1.5 0v.878A2.25 2.25 0 0 0 5.75 8.5h1.5v2.128a2.25 2.25 0 1 0 1.5 0V8.5h1.5a2.25 2.25 0 0 0 2.25-2.25v-.878a2.25 2.25 0 1 0-1.5 0v.878a.75.75 0 0 1-.75.75h-4.5A.75.75 0 0 1 5 6.25v-.878Zm3.75 7.378a.75.75 0 1 1-1.5 0 .75.75 0 0 1 1.5 0Zm3-8.25a.75.75 0 1 1-1.5 0 .75.75 0 0 1 1.5 0Z"></path></svg>',
        gear: '<svg aria-hidden="true" height="16" viewBox="0 0 16 16" version="1.1" width="16" fill="currentColor" class="octicon octicon-gear"><path d="M8 0a8.2 8.2 0 0 1 .701.031C9.444.095 9.99.645 10.16 1.29l.288 1.107c.018.066.079.158.212.224.231.114.454.243.668.386.123.082.233.09.299.071l1.103-.303c.644-.176 1.392.021 1.82.63.27.385.506.792.704 1.218.315.675.111 1.422-.364 1.891l-.814.806c-.049.048-.098.147-.088.294.016.257.016.515 0 .772-.01.147.038.246.088.294l.814.806c.475.469.679 1.216.364 1.891a7.977 7.977 0 0 1-.704 1.217c-.428.61-1.176.807-1.82.63l-1.102-.302c-.067-.019-.177-.011-.3.071a5.909 5.909 0 0 1-.668.386c-.133.066-.194.158-.211.224l-.29 1.106c-.168.646-.715 1.196-1.458 1.26a8.006 8.006 0 0 1-1.402 0c-.743-.064-1.289-.614-1.458-1.26l-.289-1.106c-.018-.066-.079-.158-.212-.224a5.738 5.738 0 0 1-.668-.386c-.123-.082-.233-.09-.299-.071l-1.103.303c-.644.176-1.392-.021-1.82-.63a8.12 8.12 0 0 1-.704-1.218c-.315-.675-.111-1.422.363-1.891l.815-.806c.05-.048.098-.147.088-.294a6.214 6.214 0 0 1 0-.772c.01-.147-.038-.246-.088-.294l-.815-.806C.635 6.045.431 5.298.746 4.623a7.92 7.92 0 0 1 .704-1.217c.428-.61 1.176-.807 1.82-.63l1.102.302c.067.019.177.011.3-.071.214-.143.437-.272.668-.386.133-.066.194-.158.211-.224l.29-1.106C6.009.645 6.556.095 7.299.03 7.53.01 7.764 0 8 0Zm-.571 1.525c-.036.003-.108.036-.137.146l-.289 1.105c-.147.561-.549.967-.998 1.189-.173.086-.34.183-.5.29-.417.278-.97.423-1.529.27l-1.103-.303c-.109-.03-.175.016-.195.045-.22.312-.412.644-.573.99-.014.031-.021.11.059.19l.815.806c.411.406.562.957.53 1.456a4.709 4.709 0 0 0 0 .582c.032.499-.119 1.05-.53 1.456l-.815.806c-.081.08-.073.159-.059.19.162.346.353.677.573.989.02.03.085.076.195.046l1.102-.303c.56-.153 1.113-.008 1.53.27.161.107.328.204.501.29.447.222.85.629.997 1.189l.289 1.105c.029.109.101.143.137.146a6.6 6.6 0 0 0 1.142 0c.036-.003.108-.036.137-.146l.289-1.105c.147-.561.549-.967.998-1.189.173-.086.34-.183.5-.29.417-.278.97-.423 1.529-.27l1.103.303c.109.029.175-.016.195-.045.22-.313.411-.644.573-.99.014-.031.021-.11-.059-.19l-.815-.806c-.411-.406-.562-.957-.53-1.456a4.709 4.709 0 0 0 0-.582c-.032-.499.119-1.05.53-1.456l.815-.806c.081-.08.073-.159.059-.19a6.464 6.464 0 0 0-.573-.989c-.02-.03-.085-.076-.195-.046l-1.102.303c-.56.153-1.113.008-1.53-.27a4.44 4.44 0 0 0-.501-.29c-.447-.222-.85-.629-.997-1.189l-.289-1.105c-.029-.11-.101-.143-.137-.146a6.6 6.6 0 0 0-1.142 0ZM11 8a3 3 0 1 1-6 0 3 3 0 0 1 6 0ZM9.5 8a1.5 1.5 0 1 0-3.001.001A1.5 1.5 0 0 0 9.5 8Z"></path></svg>',
        clock: '<svg class="octicon octicon-clock color-fg-attention" viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M8 0a8 8 0 1 1 0 16A8 8 0 0 1 8 0ZM1.5 8a6.5 6.5 0 1 0 13 0 6.5 6.5 0 0 0-13 0Zm7-3.25v2.992l2.028.812a.75.75 0 0 1-.557 1.392l-2.5-1A.751.751 0 0 1 7 8.25v-3.5a.75.75 0 0 1 1.5 0Z"></path></svg>',
        alert: '<svg class="octicon octicon-alert color-fg-muted" viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M6.457 1.047c.659-1.234 2.427-1.234 3.086 0l6.082 11.378A1.75 1.75 0 0 1 14.082 15H1.918a1.75 1.75 0 0 1-1.543-2.575Zm1.763.707a.25.25 0 0 0-.44 0L1.698 13.132a.25.25 0 0 0 .22.368h12.164a.25.25 0 0 0 .22-.368Zm.53 3.996v2.5a.75.75 0 0 1-1.5 0v-2.5a.75.75 0 0 1 1.5 0ZM9 11a1 1 0 1 1-2 0 1 1 0 0 1 2 0Z"></path></svg>'
    };

    // Built-in comment scan patterns (regex source strings). Pre-populated in the Advanced panel.
    const BUILTIN_PATTERN_STRS = [
        '(?:backport(?:ing)?(?:\\s+to|\\s+PR\\s+for)?)\\s*:?\\s*`?([a-zA-Z0-9._\\-\\/]+)`?',
        'cherry[\\-\\s]pick(?:\\s+to)?\\s*:?\\s*`?([a-zA-Z0-9._\\-\\/]+)`?',
        'port(?:ed)?\\s+to\\s+`?([a-zA-Z0-9._\\-\\/]+)`?',
    ];

    // Per-repo config persisted in localStorage.
    const CONFIG_KEY = 'bp_tracker_cfg_v1';

    function getRepoConfig(repo) {
        try {
            return { excludeCiJobs: [], requiredLabels: [], requiredReviews: 0, commentPatterns: [], refreshInterval: 30000,
                     ...JSON.parse(localStorage.getItem(CONFIG_KEY) || '{}')[repo] };
        } catch { return { excludeCiJobs: [], requiredLabels: [], requiredReviews: 0, commentPatterns: [], refreshInterval: 30000 }; }
    }

    function saveRepoConfig(repo, cfg) {
        try {
            const all = JSON.parse(localStorage.getItem(CONFIG_KEY) || '{}');
            localStorage.setItem(CONFIG_KEY, JSON.stringify({ ...all, [repo]: cfg }));
        } catch {}
    }

    function parseGithubUrl(url) {
        try {
            const u = new URL(url);
            const parts = u.pathname.split('/').filter(Boolean);
            if (parts.length >= 4 && parts[2] === 'pull') return { repo: `${parts[0]}/${parts[1]}`, prNumber: parts[3] };
        } catch (e) {}
        return null;
    }

    function getPrContext() {
        let baseBranchEl = document.querySelector('[data-testid="base-ref-name"], .base-ref, .commit-ref[title^="Base:"]');
        if (!baseBranchEl) {
            const refs = document.querySelectorAll('.commit-ref');
            if (refs.length > 0) baseBranchEl = refs[0];
        }
        if (!baseBranchEl) return null;
        let branchName = baseBranchEl.textContent.trim().replace(/^Base:\s*/i, '');
        if (!branchName) return null;
        return { isBackport: branchName !== 'master' && branchName !== 'main', baseBranch: branchName };
    }

    async function getPrStatus(repo, prNumber) {
        const prUrl = `https://github.com/${repo}/pull/${prNumber}`;
        const isCurrentPage = window.location.pathname.endsWith(`/${repo}/pull/${prNumber}`);

        let html;
        if (isCurrentPage) {
            html = document.documentElement.outerHTML;
        } else {
            try {
                const resp = await fetch(prUrl, { headers: { "Accept": "text/html" } });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                html = await resp.text();
            } catch(e) {
                return { state: 'ERROR', ciStatus: 'error', tooltip: `Fetch failed: ${e.message}`, jumpUrl: prUrl };
            }
        }

        let state = null;
        const doc = new DOMParser().parseFromString(html, 'text/html');

        const headerBadge = doc.querySelector('#partial-discussion-header .State, .gh-header-meta [data-testid="state-badge"]');
        if (headerBadge) {
            const text = headerBadge.textContent.toLowerCase();
            if (text.includes('merged')) state = 'MERGED';
            else if (text.includes('closed')) state = 'CLOSED';
            else if (text.includes('open')) state = 'OPEN';
        }

        if (!state) {
            const scripts = doc.querySelectorAll('script[type="application/json"]');
            for (const script of scripts) {
                if (!script.textContent.includes(prNumber)) continue;
                try {
                    const data = JSON.parse(script.textContent);
                    let found = null;
                    function search(obj) {
                        if (found || !obj || typeof obj !== 'object') return;
                        if (obj.pullRequest && String(obj.pullRequest.number) === String(prNumber) && obj.pullRequest.state) {
                            found = obj.pullRequest.state.toUpperCase(); return;
                        }
                        if (String(obj.number) === String(prNumber) && obj.state && typeof obj.state === 'string') {
                            if (obj.url && obj.url.includes(`/pull/${prNumber}`)) { found = obj.state.toUpperCase(); return; }
                        }
                        for (const key in obj) {
                            if (Object.prototype.hasOwnProperty.call(obj, key) && typeof obj[key] === 'object') search(obj[key]);
                        }
                    }
                    search(data);
                    if (found) { state = found; break; }
                } catch(e) {}
            }
        }

        if (!state && !isCurrentPage) {
            try {
                const resp = await fetch(`${prUrl}/hovercard`, { headers: { "X-Requested-With": "XMLHttpRequest" } });
                if (resp.ok) {
                    const hcHtml = await resp.text();
                    if (/Status:\s*Merged|State--merged|octicon-git-merge/i.test(hcHtml)) state = 'MERGED';
                    else if (/Status:\s*Closed|State--closed|octicon-issue-closed/i.test(hcHtml)) state = 'CLOSED';
                    else state = 'OPEN';
                }
            } catch(e) {}
        }

        if (!state) state = 'OPEN';

        let result = {
            state: state,
            merged: state === 'MERGED',
            closed: state === 'CLOSED',
            ciStatus: state === 'MERGED' ? 'success' : (state === 'CLOSED' ? 'closed' : 'pending'),
            jumpUrl: prUrl,
            tooltip: state.charAt(0) + state.slice(1).toLowerCase()
        };

        if (state !== 'OPEN') return result;

        const clientVersionMeta = doc.querySelector('meta[name="expected-client-version"]');
        const clientVersion = clientVersionMeta ? clientVersionMeta.content : "";

        const fetchHeaders = { "Accept": "application/json", "github-is-react": "true", "X-Requested-With": "XMLHttpRequest" };
        if (clientVersion) fetchHeaders["x-github-client-version"] = clientVersion;

        const cfg = getRepoConfig(repo);
        const tooltipParts = [];
        const condParts = [];

        // ── CI status checks ────────────────────────────────────────────────
        let foundChecks = [];
        try {
            const apiResp = await fetch(`${prUrl}/page_data/status_checks`, { headers: fetchHeaders });
            if (!apiResp.ok) throw new Error(`HTTP ${apiResp.status}`);
            const jsonData = await apiResp.json();
            if (jsonData && Array.isArray(jsonData.statusChecks)) {
                jsonData.statusChecks.forEach(check => {
                    const st = check.conclusion || check.state || "";
                    if (check.displayName && st) foundChecks.push({ name: check.displayName.toLowerCase(), state: st.toUpperCase(), url: check.targetUrl || null });
                });
            }
        } catch(e) {
            tooltipParts.push(`CI: fetch failed (${e.message})`);
        }

        if (foundChecks.length > 0) {
            const excludePatterns = cfg.excludeCiJobs.filter(j => j);
            let pass = 0, skip = 0, run = 0, fail = 0, ignored = 0, testFailUrl = null;

            foundChecks.forEach(c => {
                if (excludePatterns.some(p => c.name.includes(p.toLowerCase()))) { ignored++; return; }
                if (c.state === 'SUCCESS') pass++;
                else if (c.state === 'NEUTRAL' || c.state === 'SKIPPED') skip++;
                else if (['FAILURE', 'ERROR', 'TIMED_OUT', 'ACTION_REQUIRED'].includes(c.state)) {
                    fail++; if (!testFailUrl && c.url) testFailUrl = c.url;
                } else run++;
            });

            if (fail > 0) result.ciStatus = 'test_fail';
            else if (run > 0) result.ciStatus = 'pending';
            else result.ciStatus = 'success';

            result.jumpUrl = testFailUrl || prUrl;
            if (result.jumpUrl && result.jumpUrl.startsWith('/')) result.jumpUrl = `https://github.com${result.jumpUrl}`;

            const ciParts = [];
            if (pass > 0) ciParts.push(`${pass} passed`);
            if (skip > 0) ciParts.push(`${skip} skipped`);
            if (run > 0) ciParts.push(`${run} running`);
            if (fail > 0) ciParts.push(`${fail} failed`);
            if (ignored > 0) ciParts.push(`${ignored} ignored`);
            tooltipParts.push(ciParts.join(' '));
        } else if (tooltipParts.length === 0) {
            result.ciStatus = 'error';
            tooltipParts.push('No CI checks yet');
        }

        // ── Label check via PR page HTML ───────────────────────────────────
        if (cfg.requiredLabels.length > 0 && result.ciStatus !== 'test_fail') {
            const labelEls = doc.querySelectorAll(
                'a.IssueLabel, .hx_IssueLabel, ' +
                '[data-testid="labels-section-list"] a, ' +
                '[data-testid="label-list-item"], ' +
                '.js-issue-labels .IssueLabel'
            );
            const presentNames = Array.from(new Set(
                Array.from(labelEls).map(el => el.textContent.trim().toLowerCase()).filter(Boolean)
            ));
            const presentCount = cfg.requiredLabels.filter(req =>
                presentNames.some(n => n.includes(req.toLowerCase()))).length;
            if (presentCount < cfg.requiredLabels.length) result.ciStatus = 'mgr_pending';
            condParts.push(`${presentCount}/${cfg.requiredLabels.length} required label(s)`);
        }

        // ── Reviewer approvals via PR page HTML ────────────────────────────
        if (cfg.requiredReviews > 0 && result.ciStatus !== 'test_fail') {
            const approvedCount = doc.querySelectorAll(
                '.reviewers-status-icon .octicon-check'
            ).length;
            if (approvedCount < cfg.requiredReviews) result.ciStatus = 'mgr_pending';
            condParts.push(`${approvedCount}/${cfg.requiredReviews} required approval(s)`);
        }

        if (condParts.length) tooltipParts.push(condParts.join(' '));
        result.tooltip = tooltipParts.join('\n');

        return result;
    }

    async function triggerRefresh() {
        if (isScanning || backportData.length === 0) return;

        const btn = document.querySelector('#backport-refresh-btn');
        if (btn) btn.querySelector('svg').classList.add('anim-rotate');

        try {
            isScanning = true;
            for (const pr of backportData) {
                if (pr.skipStatusCheck || pr.merged || pr.closed || pr.ciStatus === 'success') {
                    continue;
                }

                const parsed = parseGithubUrl(pr.url);
                if (parsed) {
                    pr.ciStatus = 'fetching';
                    renderListUI();

                    const info = await getPrStatus(parsed.repo, parsed.prNumber);
                    Object.assign(pr, info);
                    renderListUI();
                }
            }
        } finally {
            isScanning = false;
            if (btn) btn.querySelector('svg').classList.remove('anim-rotate');
        }
    }

    function startAutoRefresh(repo) {
        if (refreshIntervalId) clearInterval(refreshIntervalId);
        const ms = getRepoConfig(repo).refreshInterval ?? 30000;
        refreshIntervalId = ms > 0 ? setInterval(() => triggerRefresh(), ms) : null;
    }

    // Finds backport PRs linked in comments. Uses custom regex patterns from repo config if set,
    // otherwise falls back to built-in patterns; finally falls back to all same-repo PR links.
    function findBackportPRs(comments, currentRepo, currentPrNumber) {
        const customPatterns = getRepoConfig(currentRepo).commentPatterns
            .filter(p => p)
            .map(p => { try { return new RegExp(p, 'i'); } catch { return null; } })
            .filter(Boolean);
        const PATTERNS = customPatterns.length ? customPatterns : BUILTIN_PATTERN_STRS.map(p => new RegExp(p, 'i'));
        const seenUrls = new Set();

        const collectLinks = (c, branch) => {
            const results = [];
            c.querySelectorAll('a[href*="/pull/"]').forEach(link => {
                if (seenUrls.has(link.href)) return;
                const parsed = parseGithubUrl(link.href);
                if (!parsed || parsed.prNumber === currentPrNumber) return;
                seenUrls.add(link.href);
                results.push({ branch, url: link.href });
            });
            return results;
        };

        // Primary: require both a text pattern match AND a PR link in the same comment element
        for (const pat of PATTERNS) {
            const found = [];
            comments.forEach(c => {
                const m = c.innerText.match(pat);
                if (m) found.push(...collectLinks(c, m[1]));
            });
            if (found.length) return found;
        }

        // Fallback: collect all same-repo PR links from any comment
        const found = [];
        comments.forEach(c => {
            c.querySelectorAll('a[href*="/pull/"]').forEach(link => {
                if (seenUrls.has(link.href)) return;
                const parsed = parseGithubUrl(link.href);
                if (!parsed || parsed.repo !== currentRepo || parsed.prNumber === currentPrNumber) return;
                seenUrls.add(link.href);
                found.push({ branch: '', url: link.href });
            });
        });
        return found;
    }

    async function attemptAutoScan(retryCount) {
        const prContext = getPrContext();
        if (!prContext && retryCount < MAX_RETRIES) {
            setTimeout(() => attemptAutoScan(retryCount + 1), 1000);
            return;
        }

        const currentRepoData = parseGithubUrl(window.location.href);
        if (!currentRepoData || !prContext) return;

        const root = document.getElementById('backport-ui-root');
        const comments = document.querySelectorAll('.comment-body, [data-testid="markdown-body"]');

        // Handle Backport PR context
        if (prContext.isBackport) {
            if (comments.length === 0) {
                if (retryCount < MAX_RETRIES) {
                    setTimeout(() => attemptAutoScan(retryCount + 1), 1000);
                } else if (root) {
                    root.innerHTML = `<div class="color-fg-muted f6">No backport PR</div>`;
                }
                return;
            }

            const link = comments[0]?.querySelector('a[href*="/pull/"]');
            if (link) {
                const origPrId = parseGithubUrl(link.href)?.prNumber || '?';
                backportData = [{
                    branch: 'Original PR',
                    url: link.href,
                    id: origPrId,
                    skipStatusCheck: true,
                    customText: `Backporting #${origPrId}`
                }];
                renderListUI();
            } else {
                if (root) root.innerHTML = `<div class="color-fg-muted f6">No backport PR</div>`;
            }
            return;
        }

        // Handle Standard Main PR context
        const mainPrInfo = await getPrStatus(currentRepoData.repo, currentRepoData.prNumber);
        if (mainPrInfo.state !== 'MERGED') {
            if (root) root.innerHTML = `<div class="color-fg-muted f6">PR is ${mainPrInfo.state}. Waiting for merge.</div>`;
            return;
        }

        const found = findBackportPRs(comments, currentRepoData.repo, currentRepoData.prNumber);

        if (found.length > 0) {
            backportData = found.map(pr => ({ ...pr, id: parseGithubUrl(pr.url)?.prNumber || '?', ciStatus: 'fetching' }));
            renderListUI();

            for (const pr of backportData) {
                const parsed = parseGithubUrl(pr.url);
                if (parsed) {
                    const info = await getPrStatus(parsed.repo, parsed.prNumber);
                    Object.assign(pr, info);
                    renderListUI();
                }
            }
        } else if (retryCount < MAX_RETRIES) {
            setTimeout(() => attemptAutoScan(retryCount + 1), 1000);
        } else {
            if (root) root.innerHTML = `<div class="color-fg-muted f6">No backport PR</div>`;
        }
    }

    function renderListUI() {
        const root = document.getElementById('backport-ui-root');
        if (!root) return;
        root.innerHTML = "";
        const list = document.createElement('div');
        list.className = "pb-1";

        backportData.forEach(pr => {
            const row = document.createElement('div');
            row.className = "d-flex flex-items-center mb-2 f6";
            row.setAttribute('title', pr.tooltip || '');

            let iconHtml = '';
            let badge = '';

            if (pr.skipStatusCheck) {
                iconHtml = OCTICONS.branch;
            } else {
                if (pr.merged || pr.ciStatus === 'success') iconHtml = OCTICONS.check;
                else if (pr.ciStatus === 'test_fail') iconHtml = OCTICONS.x;
                else if (pr.ciStatus === 'mgr_pending') iconHtml = OCTICONS.dot;
                else if (pr.ciStatus === 'error') iconHtml = OCTICONS.alert;
                else if (pr.ciStatus === 'fetching') iconHtml = OCTICONS.sync;
                else iconHtml = OCTICONS.dot;

                badge = pr.merged ? `<span class="Label Label--secondary mr-2" style="font-size: 10px; background-color: var(--color-done-subtle); color: var(--color-done-fg); padding: 0 4px;">Merged</span>` : '';
            }

            row.innerHTML = `
                <div class="flex-auto min-width-0">
                    ${pr.customText
                        ? `<a href="${pr.url}" class="Link--primary text-bold no-underline" target="_blank" style="font-size: 11px;">${pr.customText}</a>`
                        : `<a href="${pr.url}" class="Link--primary text-bold no-underline" target="_blank" style="font-size: 11px;">#${pr.id}</a>
                           <span class="color-fg-muted ml-1" style="font-size: 11px;">${pr.branch}</span>`
                    }
                </div>
                <div class="d-flex flex-items-center">
                    ${badge}
                    <a href="${pr.jumpUrl || pr.url}" target="_blank" class="d-flex no-underline color-fg-muted">${pr.ciStatus === 'fetching' ? iconHtml.replace('class="octicon', 'class="anim-rotate octicon') : iconHtml}</a>
                </div>`;
            list.appendChild(row);
        });

        root.appendChild(list);

        if (!backportData.some(pr => pr.skipStatusCheck)) {
            const btn = document.createElement('button');
            btn.className = "btn btn-sm btn-block mt-2";
            btn.style.fontSize = "11px";
            btn.textContent = "Copy summary";
            btn.onclick = () => {
                const text = backportData.map(pr => `[${pr.merged ? 'MERGED' : pr.ciStatus.toUpperCase()}] ${pr.branch}: ${pr.url}`).join('\n');
                navigator.clipboard.writeText(text);
            };
            root.appendChild(btn);
        }
    }

    function injectWidget(sidebar) {
        const section = document.createElement('div');
        section.id = 'backport-tracker-section';
        section.className = 'discussion-sidebar-item js-discussion-sidebar-item';
        section.style = "border-top: 1px solid var(--color-border-muted); padding-top: 16px; margin-top: 16px; position: relative;";

        const TA_STYLE = 'width:100%;box-sizing:border-box;resize:vertical;font-size:11px;';

        section.innerHTML = `
            <div class="discussion-sidebar-heading text-bold mb-2 d-flex flex-justify-between flex-items-center" style="font-size: 12px;">
                <span>${OCTICONS.branch} <span class="ml-1" id="backport-title">Backports</span></span>
                <div class="d-flex flex-items-center gap-2">
                    <button id="backport-refresh-btn" class="btn-link color-fg-muted" type="button" title="Refresh statuses">${OCTICONS.sync}</button>
                    <button id="backport-settings-btn" class="btn-link color-fg-muted" type="button">${OCTICONS.gear}</button>
                </div>
            </div>
            <div id="backport-settings-panel" class="select-menu-modal position-absolute right-0" role="dialog" style="display:none; z-index:99; width:260px; top:28px; overflow:visible;">
                <div class="select-menu-header rounded-top-2">
                    <span class="select-menu-title">Backport tracking settings</span>
                </div>
                <div style="padding:12px; max-height:420px; overflow-y:auto;">
                    <div class="mb-3">
                        <div class="text-small text-bold color-fg-muted mb-1">Auto-refresh</div>
                        <select id="bp-cfg-refresh" class="form-select select-sm" style="width:100%;">
                            <option value="0">Off</option>
                            <option value="15000">Every 15 s</option>
                            <option value="30000">Every 30 s</option>
                            <option value="60000">Every 1 min</option>
                            <option value="300000">Every 5 min</option>
                        </select>
                    </div>
                    <hr style="border-color:var(--color-border-muted);margin:0 -12px 8px;">
                    <div class="text-small text-bold color-fg-default mb-2" style="font-size:11px;text-transform:uppercase;letter-spacing:.5px;">Merge conditions</div>
                    <div class="mb-3">
                        <div class="text-small text-bold color-fg-muted mb-1">Exclude CI jobs</div>
                        <textarea id="bp-cfg-exclude-jobs" rows="2" class="form-control input-sm" placeholder="one per line, e.g. lint" autocomplete="off" style="${TA_STYLE}"></textarea>
                    </div>
                    <div class="mb-3">
                        <div class="text-small text-bold color-fg-muted mb-1">Required labels</div>
                        <textarea id="bp-cfg-labels" rows="2" class="form-control input-sm" placeholder="one per line, e.g. approved" autocomplete="off" style="${TA_STYLE}"></textarea>
                    </div>
                    <div class="mb-3">
                        <div class="text-small text-bold color-fg-muted mb-1">Required review approvals</div>
                        <input id="bp-cfg-reviews" type="number" class="form-control input-sm" placeholder="0" min="0" autocomplete="off" style="width:70px;">
                    </div>
                    <details style="margin-bottom:4px;">
                        <summary class="text-small text-bold color-fg-muted" style="cursor:pointer;user-select:none;">Advanced</summary>
                        <div style="margin-top:8px;">
                            <div class="text-small text-bold color-fg-muted mb-1">Comment scan patterns</div>
                            <div class="text-small color-fg-muted mb-1">One capture group = branch name. Defaults shown — edit to override.</div>
                            <textarea id="bp-cfg-patterns" rows="4" class="form-control input-sm" autocomplete="off" style="${TA_STYLE}"></textarea>
                        </div>
                    </details>
                </div>
            </div>
            <div id="backport-ui-root"><div class="color-fg-muted f6">Scraping references...</div></div>
        `;

        sidebar.prepend(section);
        document.getElementById('backport-refresh-btn').addEventListener('click', () => triggerRefresh());

        const settingsBtn = document.getElementById('backport-settings-btn');
        const settingsPanel = document.getElementById('backport-settings-panel');
        const splitLines = id => document.getElementById(id).value.split('\n').map(s => s.trim()).filter(Boolean);

        function updateSettingsBtnTitle(repo) {
            const cfg = getRepoConfig(repo);
            const parts = [];
            if (cfg.excludeCiJobs.length) parts.push(`Exclude: ${cfg.excludeCiJobs.map(j => `"${j}"`).join(', ')}`);
            if (cfg.requiredLabels.length) parts.push(`Labels: ${cfg.requiredLabels.map(l => `"${l}"`).join(', ')}`);
            if (cfg.requiredReviews > 0) parts.push(`Reviews: ${cfg.requiredReviews}`);
            if (cfg.commentPatterns.length) parts.push(`Custom scan patterns: ${cfg.commentPatterns.length}`);
            settingsBtn.title = parts.length
                ? `Required conditions:\n${parts.join('\n')}\n\nClick to configure`
                : 'No conditions configured — click to configure';
        }

        function doAutoSave() {
            const repo = parseGithubUrl(window.location.href)?.repo;
            if (!repo) return;
            const rawPatterns = splitLines('bp-cfg-patterns');
            // If the textarea still matches the built-in defaults exactly, store empty (so built-ins remain active)
            const isDefaultPatterns = rawPatterns.length === BUILTIN_PATTERN_STRS.length &&
                rawPatterns.every((p, i) => p === BUILTIN_PATTERN_STRS[i]);
            saveRepoConfig(repo, {
                excludeCiJobs: splitLines('bp-cfg-exclude-jobs'),
                requiredLabels: splitLines('bp-cfg-labels'),
                requiredReviews: parseInt(document.getElementById('bp-cfg-reviews').value, 10) || 0,
                commentPatterns: isDefaultPatterns ? [] : rawPatterns,
                refreshInterval: parseInt(document.getElementById('bp-cfg-refresh').value, 10) || 0,
            });
            updateSettingsBtnTitle(repo);
            startAutoRefresh(repo);
        }

        function closePanel() {
            settingsPanel.style.display = 'none';
            document.removeEventListener('click', outsideClickHandler);
        }

        function outsideClickHandler(e) {
            if (!settingsPanel.contains(e.target) && !settingsBtn.contains(e.target)) closePanel();
        }

        function openPanel() {
            const repo = parseGithubUrl(window.location.href)?.repo;
            if (repo) {
                const cfg = getRepoConfig(repo);
                document.getElementById('bp-cfg-refresh').value = String(cfg.refreshInterval ?? 30000);
                document.getElementById('bp-cfg-exclude-jobs').value = cfg.excludeCiJobs.join('\n');
                document.getElementById('bp-cfg-labels').value = cfg.requiredLabels.join('\n');
                document.getElementById('bp-cfg-reviews').value = cfg.requiredReviews || '';
                // Pre-populate patterns with built-in defaults when no custom patterns saved
                document.getElementById('bp-cfg-patterns').value = cfg.commentPatterns.length
                    ? cfg.commentPatterns.join('\n')
                    : BUILTIN_PATTERN_STRS.join('\n');
            }
            settingsPanel.style.display = 'block';
            setTimeout(() => document.addEventListener('click', outsideClickHandler), 0);
        }

        settingsBtn.addEventListener('click', () => {
            settingsPanel.style.display === 'none' ? openPanel() : closePanel();
        });

        // Auto-save on every field change (fires when focus leaves a modified field)
        ['bp-cfg-refresh', 'bp-cfg-exclude-jobs', 'bp-cfg-labels', 'bp-cfg-reviews', 'bp-cfg-patterns'].forEach(id => {
            document.getElementById(id).addEventListener('change', doAutoSave);
        });

        // Set initial tooltip and start auto-refresh based on saved config
        const repo = parseGithubUrl(window.location.href)?.repo;
        if (repo) { updateSettingsBtnTitle(repo); startAutoRefresh(repo); }

        setTimeout(() => {
            const context = getPrContext();
            const titleEl = document.getElementById('backport-title');
            if (context && titleEl) {
                titleEl.textContent = context.isBackport ? "Original PR" : "Backports";
            }
        }, 500);
    }

    const observer = new MutationObserver(() => {
        if (!window.location.pathname.includes('/pull/')) return;

        if (currentPrUrl !== window.location.href) {
            currentPrUrl = window.location.href;
            backportData = [];
            isScanning = false;
            if (refreshIntervalId) { clearInterval(refreshIntervalId); refreshIntervalId = null; }
            const oldWidget = document.getElementById('backport-tracker-section');
            if (oldWidget) oldWidget.remove();
        }

        const sidebar = document.querySelector('.Layout-sidebar, #partial-discussion-sidebar, [data-testid="sidebar"]');
        if (sidebar && !document.getElementById('backport-tracker-section')) {
            injectWidget(sidebar);
            attemptAutoScan(0);
        }
    });

    observer.observe(document.documentElement, { childList: true, subtree: true });
})();