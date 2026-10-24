// Image presets derived from known Docker images and their tag patterns
const IMAGE_PRESETS = [
    { image: 'budibase/budibase', regex: '^[0-9]+\\.[0-9]+\\.[0-9]+$', example: '3.20.12' },
    { image: 'crazymax/diun', regex: '^[0-9]+\\.[0-9]+\\.[0-9]+$', example: '4.30.0' },
    { image: 'homarr-labs/homarr', regex: '^v[0-9]+\\.[0-9]+\\.[0-9]+$', registry: 'ghcr.io', example: 'v1.46.0' },
    { image: 'mealie-recipes/mealie', regex: '^v[0-9]+\\.[0-9]+\\.[0-9]+$', registry: 'ghcr.io', example: 'v2.5.0' },
    { image: 'jellyfin/jellyfin', regex: '^[0-9]+\\.[0-9]+\\.[0-9]+$', example: '10.11.4' },
    { image: 'linuxserver/bazarr', regex: '^v[0-9]+\\.[0-9]+\\.[0-9]+-ls[0-9]+$', example: 'v1.5.3-ls328' },
    { image: 'linuxserver/calibre', regex: '^v[0-9]+\\.[0-9]+\\.[0-9]+-ls[0-9]+$', example: 'v8.16.2-ls374' },
    { image: 'linuxserver/calibre-web', regex: '^[0-9]+\\.[0-9]+\\.[0-9]+-ls[0-9]+$', example: '0.6.25-ls348' },
    { image: 'linuxserver/lidarr', regex: '^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+-ls[0-9]+$', example: '2.8.2.4493-ls22' },
    { image: 'linuxserver/prowlarr', regex: '^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+-ls[0-9]+$', example: '2.3.0.5236-ls134' },
    { image: 'linuxserver/qbittorrent', regex: '^[0-9]+\\.[0-9]+\\.[0-9]+-r[0-9]+-ls[0-9]+$', example: '5.1.2-r1-ls411' },
    { image: 'linuxserver/radarr', regex: '^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+-ls[0-9]+$', example: '6.0.4.10291-ls289' },
    { image: 'linuxserver/sabnzbd', regex: '^[0-9]+\\.[0-9]+\\.[0-9]+-ls[0-9]+$', example: '4.5.3-ls229' },
    { image: 'linuxserver/sonarr', regex: '^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+-ls[0-9]+$', example: '4.0.16.2944-ls299' },
    { image: 'linuxserver/tautulli', regex: '^v[0-9]+\\.[0-9]+\\.[0-9]+-ls[0-9]+$', example: 'v2.16.0-ls203' },
    { image: 'n8nio/n8n', regex: '^[0-9]+\\.[0-9]+\\.[0-9]+$', example: '2.0.3' },
    { image: 'pihole/pihole', regex: '^[0-9]+\\.[0-9]+\\.[0-9]+$', example: '2025.11.1' },
    { image: 'plexinc/pms-docker', regex: '^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+-[0-9a-f]+$', example: '1.42.2.10156-f737b826c' },
    { image: 'portainer/portainer-ce', regex: '^[0-9]+\\.[0-9]+\\.[0-9]+$', example: '2.33.1' },
];

// Global state
let socket;
let isDaemonRunning = false;
let imageConfigs = [];  // Array of image config objects
let logUnreadCount = 0;
let activeTab = 'updates'; // track current tab
let configDirty = false;
let notifDirty = false;

// ── Theme management ─────────────────────────────────────────────────────
const THEME_KEY = 'ium-theme'; // values: 'light', 'dark', 'system'

function getSystemPrefersDark() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function applyTheme(preference) {
    // preference: 'light' | 'dark' | 'system'
    const isDark = preference === 'dark' || (preference === 'system' && getSystemPrefersDark());
    document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
}

function initTheme() {
    const saved = localStorage.getItem(THEME_KEY) || 'system';
    applyTheme(saved);

    // Listen for OS preference changes when in system mode
    if (window.matchMedia) {
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
            const current = localStorage.getItem(THEME_KEY) || 'system';
            if (current === 'system') applyTheme('system');
        });
    }
}

function cycleTheme() {
    // Cycle: system → light → dark → system
    const current = localStorage.getItem(THEME_KEY) || 'system';
    const next = current === 'system' ? 'light' : current === 'light' ? 'dark' : 'system';
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
    updateThemeToggleLabel(next);
}

function updateThemeToggleLabel(preference) {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    const labels = { system: '◑', light: '☀', dark: '☽' };
    btn.textContent = labels[preference] || '◑';
    btn.title = `Theme: ${preference} — click to change`;
}

// Cached DOM elements (initialized in DOMContentLoaded)
const dom = {};

// Initialize Socket.IO connection
function initSocket() {
    socket = io();
    
    socket.on('connect', () => {
        console.log('Connected to server');
        updateConnectionStatus(true);
    });
    
    socket.on('disconnect', () => {
        console.log('Disconnected from server');
        updateConnectionStatus(false);
    });
    
    socket.on('status_update', updateStatus);
    socket.on('check_complete', handleCheckComplete);
    socket.on('check_progress', handleCheckProgress);

    socket.on('check_error', (data) => {
        addLog('Check failed: ' + data.error, 'error');
    });

    socket.on('connected', (data) => {
        addLog('Connected: ' + data.status, 'info');
    });
}

// Update connection status indicator
function updateConnectionStatus(connected) {
    if (connected) {
        dom.statusIndicator.classList.remove('checking');
        dom.statusIndicator.classList.add('connected');
        dom.statusText.textContent = 'Connected';
        loadStatus();
    } else {
        dom.statusIndicator.classList.remove('connected');
        dom.statusIndicator.classList.remove('checking');
        dom.statusText.textContent = 'Disconnected';
    }
}

// Update status from server
function updateStatus(data) {
    if (data.checking) {
        dom.statusIndicator.classList.add('checking');
        dom.statusText.textContent = 'Checking...';
        dom.checkNow.disabled = true;
    } else {
        dom.statusIndicator.classList.remove('checking');
        dom.statusText.textContent = 'Connected';
        dom.checkNow.disabled = false;
    }

    if (data.daemon_running !== undefined) {
        isDaemonRunning = data.daemon_running;
        updateDaemonButton();
    }

    if (data.last_check) {
        updateLastCheck(data.last_check);
    }
}

// Load current status
async function loadStatus() {
    try {
        const response = await fetch('/api/status');
        const data = await response.json();

        // Update mode indicator
        if (data.dry_run) {
            dom.modeIndicator.textContent = 'DRY RUN';
            dom.modeIndicator.className = 'mode dry-run';
        } else {
            dom.modeIndicator.textContent = 'PRODUCTION';
            dom.modeIndicator.className = 'mode production';
        }

        isDaemonRunning = data.daemon_running;
        updateDaemonButton();

        if (data.last_check) {
            updateLastCheck(data.last_check);
        }

        // Load other data
        loadConfig();
        loadHistory();
        loadUpdates();

    } catch (error) {
        addLog('Failed to load status: ' + error, 'error');
    }
}

// Update last check time
function updateLastCheck(timestamp) {
    const date = new Date(timestamp);
    dom.lastCheck.textContent = date.toLocaleString();
}

// Update daemon button state
function updateDaemonButton() {
    if (isDaemonRunning) {
        dom.toggleDaemon.textContent = 'Stop Daemon';
        dom.toggleDaemon.classList.add('btn-danger');
        dom.toggleDaemon.classList.remove('btn-secondary');
        dom.daemonIndicator.classList.add('connected');
        dom.daemonStatusText.textContent = 'Running';
    } else {
        dom.toggleDaemon.textContent = 'Start Daemon';
        dom.toggleDaemon.classList.remove('btn-danger');
        dom.toggleDaemon.classList.add('btn-secondary');
        dom.daemonIndicator.classList.remove('connected');
        dom.daemonStatusText.textContent = 'Stopped';
    }
}

// Handle check progress event
function handleCheckProgress(data) {
    const { event, data: eventData } = data;

    switch (event) {
        case 'checking_image':
            addLog(`[${eventData.progress}/${eventData.total}] Checking ${eventData.image}:${eventData.base_tag}...`, 'info');
            break;
        case 'update_found':
            addLog(`  → Update available: ${eventData.old_tag} → ${eventData.new_tag}`, 'warning');
            break;
        case 'image_rebuilt':
            addLog(`  → Image rebuilt: ${eventData.tag} (new digest)`, 'warning');
            break;
        case 'no_update':
            addLog(`  - No update available`, 'info');
            break;
        case 'check_error':
            addLog(`  ✗ ${eventData.error}`, 'error');
            break;
    }
}

// Handle check complete event
function handleCheckComplete(data) {
    addLog('Check complete', 'info');
    updateLastCheck(data.timestamp);
    
    if (data.updates?.length > 0) {
        displayUpdates(data.updates);
        addLog(`Found ${data.updates.length} updates`, 'warning');
    } else {
        displayNoUpdates();
        addLog('No updates found', 'info');
    }
    
    loadHistory();
}

// Show a transient toast notification
function showToast(message, type = 'info', durationMs = 3000) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container';
        document.body.appendChild(container);
    }

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    // Trigger animation
    requestAnimationFrame(() => {
        requestAnimationFrame(() => toast.classList.add('toast-show'));
    });

    setTimeout(() => {
        toast.classList.remove('toast-show');
        toast.addEventListener('transitionend', () => toast.remove(), { once: true });
    }, durationMs);
}

// Display available updates
function displayUpdates(updates) {
    dom.updateList.innerHTML = '';

    updates.forEach(update => {
        const item = document.createElement('div');
        item.className = 'update-item';
        item.dataset.image = update.image;
        item.dataset.newTag = update.new_tag;

        const autoUpdateBadge = update.auto_update
            ? '<span class="badge badge-auto-update">Auto-update enabled</span>'
            : `<button class="btn btn-apply" data-image="${escapeHtml(update.image)}" data-new-tag="${escapeHtml(update.new_tag)}">Apply Update</button>`;
        // A pending downgrade is never auto-applied; offer to ignore it
        // until the registry resolves to a different digest.
        const downgradeBadge = update.downgrade
            ? ' <span class="badge badge-downgrade">Older than current</span>'
            : '';
        const dismissButton = update.downgrade
            ? `<button class="btn btn-secondary btn-dismiss" data-image="${escapeHtml(update.image)}">Ignore until registry changes</button>`
            : '';

        item.innerHTML = `
            <div class="update-item-header">
                <div>
                    <h3>${escapeHtml(update.image)}</h3>
                    <div class="update-details">
                        <span>Base tag: <strong>${escapeHtml(update.base_tag)}</strong></span>
                        <span class="tag-change">${escapeHtml(update.old_tag)} → ${escapeHtml(update.new_tag)}</span>${downgradeBadge}
                        <span>Digest: ${escapeHtml(update.digest.substring(0, 12))}...</span>
                    </div>
                </div>
                <div class="card-actions">${autoUpdateBadge}${dismissButton}</div>
            </div>
            <div class="update-error" style="display:none"></div>
        `;

        dom.updateList.appendChild(item);
    });

    // Wire up Apply buttons
    dom.updateList.querySelectorAll('.btn-apply').forEach(btn => {
        btn.addEventListener('click', () => applyUpdate(btn));
    });

    // Wire up Dismiss buttons (pending downgrades only)
    dom.updateList.querySelectorAll('.btn-dismiss').forEach(btn => {
        btn.addEventListener('click', () => dismissUpdate(btn));
    });
}

async function dismissUpdate(btn) {
    const image = btn.dataset.image;
    const item = btn.closest('.update-item');
    const errorEl = item.querySelector('.update-error');

    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = 'Ignoring…';
    errorEl.style.display = 'none';

    try {
        const response = await fetch('/api/updates/dismiss', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ image })
        });

        const data = await response.json();

        if (response.ok && data.success) {
            item.remove();
            if (!dom.updateList.querySelector('.update-item')) {
                displayNoUpdates();
            }
            addLog(`Ignoring pending downgrade for ${image} until the registry changes`, 'info');
        } else {
            btn.textContent = originalText;
            btn.disabled = false;
            errorEl.textContent = data.error || 'Dismiss failed';
            errorEl.style.display = 'block';
            showToast(`Dismiss failed: ${data.error || 'unknown error'}`, 'error');
        }
    } catch (e) {
        btn.textContent = originalText;
        btn.disabled = false;
        errorEl.textContent = 'Network error: ' + e.message;
        errorEl.style.display = 'block';
        showToast(`Dismiss failed: ${e.message}`, 'error');
    }
}

async function applyUpdate(btn) {
    const image = btn.dataset.image;
    const newTag = btn.dataset.newTag;
    const item = btn.closest('.update-item');
    const errorEl = item.querySelector('.update-error');

    // Spinner state
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = 'Applying\u2026';
    errorEl.style.display = 'none';

    try {
        const response = await fetch('/api/apply-update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ image, new_tag: newTag })
        });

        const data = await response.json();

        if (response.ok && data.success) {
            btn.textContent = '\u2713 Applied';
            btn.classList.add('applied');
            item.classList.add('applied');
            showToast(`${image} updated to ${newTag}`, 'success');
            addLog(`Manually applied: ${image} \u2192 ${newTag}`, 'success');
        } else {
            btn.textContent = originalText;
            btn.disabled = false;
            errorEl.textContent = data.error || 'Update failed';
            errorEl.style.display = 'block';
            showToast(`Update failed: ${data.error || 'unknown error'}`, 'error');
        }
    } catch (e) {
        btn.textContent = originalText;
        btn.disabled = false;
        errorEl.textContent = 'Network error: ' + e.message;
        errorEl.style.display = 'block';
    }
}

// Display no updates message
function displayNoUpdates() {
    dom.updateList.innerHTML = '<p class="no-updates">All images are up to date!</p>';
}

// Load configuration and render cards
async function loadConfig() {
    try {
        const response = await fetch('/api/config');
        const config = await response.json();
        imageConfigs = config.images || [];
        renderImageCards();
        loadNotificationConfig(config.notifications || {});
    } catch (error) {
        addLog('Failed to load config: ' + error, 'error');
    }
}

// Populate notification fields from saved config
function loadNotificationConfig(notif) {
    const ntfy = notif.ntfy || {};
    const webhook = notif.webhook || {};
    dom.ntfyUrl.value = ntfy.url || '';
    dom.ntfyPriority.value = ntfy.priority || 'default';
    dom.webhookUrl.value = webhook.url || '';
    dom.webhookMethod.value = webhook.method || 'POST';
    dom.webhookBodyTemplate.value = webhook.body_template || '';
}

// Collect notification config from the UI fields
function collectNotificationConfig() {
    const notif = {};

    const ntfyUrl = dom.ntfyUrl.value.trim();
    if (ntfyUrl) {
        notif.ntfy = { url: ntfyUrl };
        const priority = dom.ntfyPriority.value;
        if (priority && priority !== 'default') {
            notif.ntfy.priority = priority;
        }
    }

    const webhookUrl = dom.webhookUrl.value.trim();
    if (webhookUrl) {
        notif.webhook = { url: webhookUrl };
        const method = dom.webhookMethod.value;
        if (method && method !== 'POST') {
            notif.webhook.method = method;
        }
        const bodyTemplate = dom.webhookBodyTemplate.value.trim();
        if (bodyTemplate) {
            notif.webhook.body_template = bodyTemplate;
        }
    }

    return notif;
}

// Send a test notification for a given channel type
async function testNotification(type) {
    const statusEl = type === 'ntfy' ? dom.ntfyTestStatus : dom.webhookTestStatus;
    statusEl.textContent = 'Sending…';
    statusEl.className = 'detect-status loading';

    try {
        const response = await fetch('/api/notifications/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ type })
        });
        const data = await response.json();
        if (response.ok) {
            statusEl.textContent = 'Sent!';
            statusEl.className = 'detect-status success';
        } else {
            statusEl.textContent = data.error || 'Failed';
            statusEl.className = 'detect-status error';
        }
    } catch (e) {
        statusEl.textContent = 'Network error';
        statusEl.className = 'detect-status error';
    }

    setTimeout(() => { statusEl.textContent = ''; statusEl.className = ''; }, 5000);
}

// Render all image cards
function renderImageCards() {
    dom.imageCards.innerHTML = '';

    if (imageConfigs.length === 0) {
        dom.imageCards.innerHTML = `
            <div class="config-empty-state">
                <p>No images configured yet.</p>
                <div class="empty-actions">
                    <button class="btn btn-primary" onclick="addNewImage()">+ Add Image</button>
                    <button class="btn btn-secondary" onclick="showPresetModal()">Add from Preset</button>
                </div>
            </div>
        `;
        return;
    }

    imageConfigs.forEach((config, index) => {
        const card = createImageCard(config, index);
        dom.imageCards.appendChild(card);
    });
}

// Create a single image card
function createImageCard(config, index, isNew = false) {
    const card = document.createElement('div');
    card.className = 'image-card' + (isNew ? ' is-new' : '');
    card.dataset.index = index;

    // Build badges HTML
    const badges = buildBadgeHtml(config.auto_update, config.base_tag);

    card.innerHTML = `
        <div class="card-header">
            <div class="card-title">
                <span class="card-image-name">${escapeHtml(config.image || 'New Image')}</span>
                <span class="card-status-badges">${badges}</span>
            </div>
            <div class="card-actions">
                <button class="btn-icon btn-expand" title="Expand/Collapse">
                    <span class="expand-icon">&#9660;</span>
                </button>
                <button class="btn-icon btn-delete" title="Delete Image">&#10005;</button>
            </div>
        </div>
        <div class="card-body">
            <div class="form-group">
                <label>Registry</label>
                <input type="text" class="form-input" name="registry"
                       value="${escapeHtml(config.registry || '')}"
                       placeholder="Docker Hub (default), ghcr.io, gcr.io, etc.">
            </div>

            <div class="form-group">
                <label>Image Name <span class="required">*</span></label>
                <input type="text" class="form-input" name="image"
                       value="${escapeHtml(config.image || '')}"
                       placeholder="e.g., linuxserver/calibre">
                <div class="field-error"></div>
            </div>

            <div class="form-group">
                <label>Regex Pattern <span class="required">*</span></label>
                <input type="text" class="form-input" name="regex"
                       value="${escapeHtml(config.regex || '')}"
                       placeholder="e.g., ^v[0-9]+\\.[0-9]+\\.[0-9]+-ls[0-9]+$">
                <div class="regex-validation">
                    <span class="regex-status"></span>
                </div>
                <div class="pattern-detect-row">
                    <button type="button" class="btn btn-secondary btn-sm btn-detect-patterns"
                            title="Fetch tags from registry and suggest a regex pattern">
                        Suggest Pattern
                    </button>
                    <span class="detect-status"></span>
                </div>
                <div class="pattern-dropdown-container" style="display: none;">
                    <select class="form-input pattern-select">
                        <option value="">-- Select a detected pattern --</option>
                    </select>
                </div>
                <div class="field-error"></div>
            </div>

            <div class="form-group">
                <label>Test Tag</label>
                <input type="text" class="form-input regex-test-input"
                       placeholder="e.g., v8.11.1-ls358">
                <div class="regex-test-hint">Enter a tag to test if it matches the regex pattern</div>
            </div>

            <div class="form-group">
                <label>Base Tag</label>
                <input type="text" class="form-input" name="base_tag"
                       value="${escapeHtml(config.base_tag || '')}"
                       placeholder="latest (default)">
                <div class="basetag-dropdown-container" style="display: none;">
                    <select class="form-input basetag-select">
                        <option value="">-- Select a detected base tag --</option>
                    </select>
                </div>
            </div>

            <div class="form-row checkbox-row">
                <label class="checkbox-label">
                    <input type="checkbox" name="auto_update" ${config.auto_update ? 'checked' : ''}>
                    <span>Auto-update enabled</span>
                </label>
                <label class="checkbox-label">
                    <input type="checkbox" name="cleanup_old_images" ${config.cleanup_old_images ? 'checked' : ''}>
                    <span>Cleanup old images</span>
                </label>
            </div>

            <div class="form-group keep-versions-group">
                <label>Keep Versions</label>
                <input type="number" class="form-input form-input-small" name="keep_versions"
                       value="${config.keep_versions || 3}"
                       min="1" max="99">
                <span class="field-hint">Number of image versions to keep when cleanup is enabled</span>
            </div>
        </div>
    `;

    // Attach event listeners
    attachCardEventListeners(card, index);

    // Validate regex on initial load
    const regexInput = card.querySelector('input[name="regex"]');
    if (regexInput.value) {
        validateRegex(regexInput);
    }

    // If new card, expand it
    if (isNew) {
        card.querySelector('.card-header').classList.add('expanded');
        card.querySelector('.card-body').classList.add('expanded');
    }

    return card;
}

// Escape HTML to prevent XSS
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Attach event listeners to a card
function attachCardEventListeners(card, index) {
    const header = card.querySelector('.card-header');
    const deleteBtn = card.querySelector('.btn-delete');
    const imageInput = card.querySelector('input[name="image"]');
    const regexInput = card.querySelector('input[name="regex"]');
    const testInput = card.querySelector('.regex-test-input');
    const autoUpdateCheckbox = card.querySelector('input[name="auto_update"]');
    const baseTagInput = card.querySelector('input[name="base_tag"]');

    // Toggle expand/collapse
    header.addEventListener('click', (e) => {
        if (e.target.closest('.btn-delete')) return;
        toggleCardExpand(card);
    });

    // Delete button
    deleteBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        showDeleteConfirmation(index);
    });

    // Update card title when image name changes
    imageInput.addEventListener('input', () => {
        markConfigDirty();
        const nameSpan = card.querySelector('.card-image-name');
        nameSpan.textContent = imageInput.value || 'New Image';
    });

    // Validate regex pattern
    regexInput.addEventListener('input', () => {
        markConfigDirty();
        validateRegex(regexInput);
        updateRegexTest(card);
    });

    // Test regex against input
    testInput.addEventListener('input', () => {
        updateRegexTest(card);
    });

    // Detect patterns button
    card.querySelector('.btn-detect-patterns').addEventListener('click', () => {
        detectPatterns(card);
    });

    // Pattern select dropdown
    card.querySelector('.pattern-select').addEventListener('change', (e) => {
        if (e.target.value) {
            regexInput.value = e.target.value;
            validateRegex(regexInput);
            updateRegexTest(card);
        }
    });

    // Base tag select dropdown
    card.querySelector('.basetag-select').addEventListener('change', (e) => {
        if (e.target.value) {
            baseTagInput.value = e.target.value;
            updateCardBadges(card);
        }
    });

    // Auto-populate container name and detect patterns when image name is entered
    imageInput.addEventListener('blur', () => {
        const imageName = imageInput.value.trim();
        if (imageName && !regexInput.value.trim()) {
            const status = card.querySelector('.detect-status');
            status.className = 'detect-status loading';
            status.textContent = 'Detecting\u2026';
            detectPatterns(card, 3);
        }
    });

    // Update badges when auto_update or base_tag changes
    autoUpdateCheckbox.addEventListener('change', () => {
        markConfigDirty();
        updateCardBadges(card);
    });
    baseTagInput.addEventListener('input', () => {
        markConfigDirty();
        updateCardBadges(card);
    });

    // Track dirty for cleanup checkbox and keep_versions
    const cleanupCheckbox = card.querySelector('input[name="cleanup_old_images"]');
    const keepVersionsInput = card.querySelector('input[name="keep_versions"]');
    if (cleanupCheckbox) cleanupCheckbox.addEventListener('change', markConfigDirty);
    if (keepVersionsInput) keepVersionsInput.addEventListener('input', markConfigDirty);
}

// Toggle card expand/collapse state
function toggleCardExpand(card) {
    card.querySelector('.card-header').classList.toggle('expanded');
    card.querySelector('.card-body').classList.toggle('expanded');
}

// Validate regex pattern
function validateRegex(regexInput) {
    const card = regexInput.closest('.image-card');
    const statusSpan = card.querySelector('.regex-status');

    if (!regexInput.value) {
        statusSpan.className = 'regex-status';
        statusSpan.textContent = '';
        regexInput.classList.remove('invalid', 'valid');
        return true;
    }

    try {
        new RegExp(regexInput.value);
        statusSpan.className = 'regex-status valid';
        statusSpan.textContent = 'Valid regex pattern';
        regexInput.classList.remove('invalid');
        regexInput.classList.add('valid');
        return true;
    } catch (e) {
        statusSpan.className = 'regex-status invalid';
        statusSpan.textContent = 'Invalid regex pattern: ' + e.message;
        regexInput.classList.remove('valid');
        regexInput.classList.add('invalid');
        return false;
    }
}

// Update regex test result
function updateRegexTest(card) {
    const regexInput = card.querySelector('input[name="regex"]');
    const testInput = card.querySelector('.regex-test-input');
    const hintDiv = card.querySelector('.regex-test-hint');

    // Reset to default hint if no test input
    if (!testInput.value) {
        hintDiv.className = 'regex-test-hint';
        hintDiv.textContent = 'Enter a tag to test if it matches the regex pattern';
        return;
    }

    // Show error if test input but no meaningful regex pattern
    const pattern = regexInput.value.trim();
    if (!pattern || pattern === '^') {
        hintDiv.className = 'regex-test-hint no-match';
        hintDiv.textContent = 'Enter a regex pattern first';
        return;
    }

    try {
        const regex = new RegExp(pattern);
        if (regex.test(testInput.value)) {
            hintDiv.className = 'regex-test-hint match';
            hintDiv.textContent = `"${testInput.value}" matches the pattern`;
        } else {
            hintDiv.className = 'regex-test-hint no-match';
            hintDiv.textContent = `"${testInput.value}" does not match the pattern`;
        }
    } catch (e) {
        hintDiv.className = 'regex-test-hint';
        hintDiv.textContent = 'Enter a tag to test if it matches the regex pattern';
    }
}

// Detect tag patterns from registry (maxPatterns=0 means show all)
async function detectPatterns(card, maxPatterns = 0) {
    const imageInput = card.querySelector('input[name="image"]');
    const registryInput = card.querySelector('input[name="registry"]');
    const btn = card.querySelector('.btn-detect-patterns');
    const status = card.querySelector('.detect-status');
    const dropdown = card.querySelector('.pattern-dropdown-container');
    const select = card.querySelector('.pattern-select');

    const image = imageInput.value.trim();
    if (!image) {
        status.className = 'detect-status error';
        status.textContent = 'Enter an image name first';
        return;
    }

    // Show loading state
    btn.disabled = true;
    status.className = 'detect-status loading';
    status.textContent = 'Fetching tags...';
    dropdown.style.display = 'none';

    try {
        const response = await fetch('/api/detect-patterns', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({
                image: image,
                registry: registryInput.value.trim()
            })
        });

        const data = await response.json();

        if (!response.ok) {
            status.className = 'detect-status error';
            status.textContent = data.error || 'Detection failed';
            return;
        }

        if (!data.patterns || data.patterns.length === 0) {
            status.className = 'detect-status warning';
            status.textContent = `No patterns detected (${data.total_tags} tags scanned)`;
            return;
        }

        // Populate dropdown (cap to maxPatterns if set)
        const patterns = maxPatterns > 0 ? data.patterns.slice(0, maxPatterns) : data.patterns;
        select.innerHTML = '<option value="">-- Select a detected pattern --</option>';
        patterns.forEach(p => {
            const option = document.createElement('option');
            option.value = p.regex;
            option.textContent = `${p.label} (${p.match_count} tags, e.g. ${p.example_tags.join(', ')})`;
            select.appendChild(option);
        });

        dropdown.style.display = 'block';
        status.className = 'detect-status success';
        const shown = maxPatterns > 0 && data.patterns.length > maxPatterns
            ? `Top ${patterns.length} of ${data.patterns.length}`
            : `${data.patterns.length}`;
        status.textContent = `${shown} pattern(s) found from ${data.total_tags} tags`;

        // Auto-select if triggered automatically (maxPatterns > 0) and there's a dominant pattern
        if (maxPatterns > 0 && patterns.length === 1) {
            const regexInput = card.querySelector('input[name="regex"]');
            if (!regexInput.value.trim()) {
                regexInput.value = patterns[0].regex;
                validateRegex(regexInput);
                updateRegexTest(card);
                status.textContent += ' (auto-applied)';
                dropdown.style.display = 'none'; // hide the dropdown; it's already applied
            }
        }

        // Populate base tag dropdown if base_tag field is empty
        const baseTagInput = card.querySelector('input[name="base_tag"]');
        const baseTagDropdown = card.querySelector('.basetag-dropdown-container');
        const baseTagSelect = card.querySelector('.basetag-select');

        if (data.base_tags && data.base_tags.length > 0 && !baseTagInput.value.trim()) {
            const baseTags = maxPatterns > 0 ? data.base_tags.slice(0, maxPatterns) : data.base_tags;
            baseTagSelect.innerHTML = '<option value="">-- Select a detected base tag --</option>';
            baseTags.forEach(tag => {
                const option = document.createElement('option');
                option.value = tag;
                option.textContent = tag;
                baseTagSelect.appendChild(option);
            });
            baseTagDropdown.style.display = 'block';
        }

    } catch (error) {
        status.className = 'detect-status error';
        status.textContent = 'Network error: ' + error.message;
    } finally {
        btn.disabled = false;
    }
}

// Build badge HTML for auto-update and base tag indicators
function buildBadgeHtml(autoUpdate, baseTag) {
    let badges = '';
    if (autoUpdate) {
        badges += '<span class="badge badge-auto-update">Auto-update</span>';
    }
    if (baseTag && baseTag !== 'latest') {
        badges += `<span class="badge badge-base-tag">${escapeHtml(baseTag)}</span>`;
    }
    return badges;
}

// Update card badges based on current values
function updateCardBadges(card) {
    const badgesSpan = card.querySelector('.card-status-badges');
    const autoUpdate = card.querySelector('input[name="auto_update"]').checked;
    const baseTag = card.querySelector('input[name="base_tag"]').value;
    badgesSpan.innerHTML = buildBadgeHtml(autoUpdate, baseTag);
}

// Add new image
function addNewImage() {
    const newConfig = {
        image: '',
        regex: '',
        base_tag: '',
        auto_update: false,
        cleanup_old_images: false,
        keep_versions: 3,
        registry: ''
    };

    markConfigDirty();
    imageConfigs.push(newConfig);
    const card = createImageCard(newConfig, imageConfigs.length - 1, true);
    dom.imageCards.appendChild(card);

    // Scroll to new card and focus image input
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setTimeout(() => {
        card.querySelector('input[name="image"]').focus();
    }, 100);
}

// Show preset selection modal
function showPresetModal() {
    // Determine which images are already configured
    const configuredImages = new Set(imageConfigs.map(c => c.image));

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';

    let listHtml = IMAGE_PRESETS.map(preset => {
        const alreadyAdded = configuredImages.has(preset.image);
        const displayName = preset.registry
            ? `${preset.registry}/${preset.image}`
            : preset.image;
        return `
            <div class="preset-item${alreadyAdded ? ' preset-disabled' : ''}"
                 data-image="${escapeHtml(preset.image)}"
                 data-filter="${escapeHtml(displayName.toLowerCase())}">
                <div class="preset-name">${escapeHtml(displayName)}</div>
                <div class="preset-details">
                    <code class="preset-regex">${escapeHtml(preset.regex)}</code>
                    <span class="preset-example">e.g. ${escapeHtml(preset.example)}</span>
                </div>
                ${alreadyAdded ? '<span class="preset-badge">Already added</span>' : ''}
            </div>
        `;
    }).join('');

    overlay.innerHTML = `
        <div class="modal-content modal-presets">
            <div class="modal-title">Add from Preset</div>
            <input type="text" class="form-input preset-filter" placeholder="Filter images...">
            <div class="preset-list">${listHtml}</div>
            <div class="modal-actions">
                <button class="btn btn-secondary modal-cancel">Cancel</button>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);

    // Filter functionality
    const filterInput = overlay.querySelector('.preset-filter');
    filterInput.addEventListener('input', () => {
        const query = filterInput.value.toLowerCase();
        overlay.querySelectorAll('.preset-item').forEach(item => {
            const match = item.dataset.filter.includes(query);
            item.style.display = match ? '' : 'none';
        });
    });

    // Click on preset item to add it
    overlay.querySelectorAll('.preset-item:not(.preset-disabled)').forEach(item => {
        item.addEventListener('click', () => {
            const preset = IMAGE_PRESETS.find(p => p.image === item.dataset.image);
            if (preset) {
                addFromPreset(preset);
                overlay.remove();
            }
        });
    });

    // Close handlers
    overlay.querySelector('.modal-cancel').addEventListener('click', () => overlay.remove());
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) overlay.remove();
    });

    // Focus filter input
    setTimeout(() => filterInput.focus(), 100);
}

// Add a new image card from a preset
function addFromPreset(preset) {
    const newConfig = {
        image: preset.image,
        regex: preset.regex,
        base_tag: '',
        auto_update: false,
        cleanup_old_images: false,
        keep_versions: 3,
        registry: preset.registry || ''
    };

    markConfigDirty();
    imageConfigs.push(newConfig);
    const card = createImageCard(newConfig, imageConfigs.length - 1, true);
    dom.imageCards.appendChild(card);

    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    addLog(`Added preset: ${preset.registry ? preset.registry + '/' : ''}${preset.image}`, 'info');
}

// Show delete confirmation modal
function showDeleteConfirmation(index) {
    const imageName = imageConfigs[index]?.image || 'this image';

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-content">
            <div class="modal-title">Delete Image Configuration</div>
            <div class="modal-body">
                Are you sure you want to delete the configuration for <strong>${escapeHtml(imageName)}</strong>?
                This action cannot be undone until you save.
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary modal-cancel">Cancel</button>
                <button class="btn btn-danger modal-confirm">Delete</button>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);

    overlay.querySelector('.modal-cancel').addEventListener('click', () => {
        overlay.remove();
    });

    overlay.querySelector('.modal-confirm').addEventListener('click', () => {
        deleteImage(index);
        overlay.remove();
    });

    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) {
            overlay.remove();
        }
    });
}

// Delete image from list
function deleteImage(index) {
    markConfigDirty();
    imageConfigs.splice(index, 1);
    renderImageCards();
    addLog('Image removed (save to apply changes)', 'warning');
}

// Collect form data from all cards
function collectFormData() {
    const configs = [];
    const cards = dom.imageCards.querySelectorAll('.image-card');
    let hasErrors = false;

    cards.forEach((card) => {
        const imageInput = card.querySelector('input[name="image"]');
        const regexInput = card.querySelector('input[name="regex"]');

        // Clear previous errors
        card.querySelectorAll('.field-error').forEach(el => el.textContent = '');
        card.classList.remove('has-error');

        // Validate required fields
        let cardHasError = false;

        if (!imageInput.value.trim()) {
            imageInput.nextElementSibling.textContent = 'Image name is required';
            imageInput.classList.add('invalid');
            cardHasError = true;
        } else {
            imageInput.classList.remove('invalid');
        }

        if (!regexInput.value.trim()) {
            const errorDiv = regexInput.closest('.form-group').querySelector('.field-error');
            errorDiv.textContent = 'Regex pattern is required';
            regexInput.classList.add('invalid');
            cardHasError = true;
        } else if (!validateRegex(regexInput)) {
            const errorDiv = regexInput.closest('.form-group').querySelector('.field-error');
            errorDiv.textContent = 'Invalid regex pattern';
            cardHasError = true;
        }

        if (cardHasError) {
            card.classList.add('has-error');
            // Expand card to show errors
            card.querySelector('.card-header').classList.add('expanded');
            card.querySelector('.card-body').classList.add('expanded');
            hasErrors = true;
        }

        // Build config object
        const config = {
            image: imageInput.value.trim(),
            regex: regexInput.value.trim()
        };

        // Add optional fields only if they have values
        const baseTag = card.querySelector('input[name="base_tag"]').value.trim();
        const registry = card.querySelector('input[name="registry"]').value.trim();
        const autoUpdate = card.querySelector('input[name="auto_update"]').checked;
        const cleanup = card.querySelector('input[name="cleanup_old_images"]').checked;
        const keepVersions = parseInt(card.querySelector('input[name="keep_versions"]').value, 10) || 3;

        if (baseTag) config.base_tag = baseTag;
        if (registry) config.registry = registry;
        config.auto_update = autoUpdate;
        config.cleanup_old_images = cleanup;
        if (keepVersions !== 3) config.keep_versions = keepVersions;

        configs.push(config);
    });

    return hasErrors ? null : configs;
}

// Save configuration
async function saveConfig() {
    const configs = collectFormData();

    if (!configs) {
        addLog('Please fix validation errors before saving', 'error');
        return;
    }

    const payload = { images: configs };
    const notifications = collectNotificationConfig();
    if (Object.keys(notifications).length > 0) {
        payload.notifications = notifications;
    }

    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify(payload)
        });

        if (response.ok) {
            addLog('Configuration saved successfully', 'info');
            imageConfigs = configs;
            markConfigClean();
            showToast('Configuration saved', 'success');

            // Remove "is-new" class from all cards
            dom.imageCards.querySelectorAll('.image-card.is-new').forEach(card => {
                card.classList.remove('is-new');
            });

            loadStatus();
        } else {
            const error = await response.json();
            addLog('Failed to save config: ' + error.error, 'error');
        }
    } catch (error) {
        addLog('Error saving configuration: ' + error, 'error');
    }
}

// Load update history
async function loadHistory() {
    try {
        const response = await fetch('/api/history');
        const history = await response.json();

        if (history.length === 0) {
            dom.historyList.innerHTML = '<p class="no-updates">No update history yet.</p>';
            return;
        }

        dom.historyList.innerHTML = '';
        history.reverse().forEach(item => {
            const elem = document.createElement('div');
            elem.className = 'history-item';
            const date = new Date(item.timestamp);
            elem.innerHTML = `
                <span class="history-time">${date.toLocaleString()}</span>
                <span>${item.image}</span>
                <span class="tag-change">${item.old_tag} → ${item.new_tag}</span>
                <span>${item.applied ? '✅ Applied' : '⚠️ Dry run'}</span>
            `;
            dom.historyList.appendChild(elem);
        });
    } catch (error) {
        addLog('Failed to load history: ' + error, 'error');
    }
}

// Load last updates
async function loadUpdates() {
    try {
        const response = await fetch('/api/updates');
        const data = await response.json();

        if (data.updates?.length > 0) {
            displayUpdates(data.updates);
        } else if (data.last_check) {
            // Check was performed but no updates found
            displayNoUpdates();
        } else {
            // No check has been performed yet
            dom.updateList.innerHTML =
                '<p class="no-updates">No check performed yet. Click "Check Now" to scan for updates.</p>';
        }
    } catch (error) {
        addLog('Failed to load updates: ' + error, 'error');
    }
}

// Trigger manual check
async function checkNow() {
    try {
        addLog('Starting manual check...', 'info');
        const response = await fetch('/api/check', {
            method: 'POST',
            headers: { 'X-Requested-With': 'XMLHttpRequest' }
        });
        
        if (!response.ok) {
            const error = await response.json();
            addLog('Check failed: ' + error.error, 'error');
        }
    } catch (error) {
        addLog('Failed to start check: ' + error, 'error');
    }
}

// Toggle daemon mode
async function toggleDaemon() {
    try {
        const action = isDaemonRunning ? 'stop' : 'start';
        const interval = dom.daemonInterval.value;

        const response = await fetch('/api/daemon', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ action, interval: parseInt(interval) })
        });

        if (response.ok) {
            const result = await response.json();
            addLog(`Daemon ${result.status}`, 'info');
            isDaemonRunning = action === 'start';
            updateDaemonButton();
        } else {
            const error = await response.json();
            addLog('Daemon control failed: ' + error.error, 'error');
        }
    } catch (error) {
        addLog('Failed to control daemon: ' + error, 'error');
    }
}

// Add log entry
function addLog(message, level = 'info') {
    const entry = document.createElement('div');
    entry.className = `log-line ${level}`;
    const timestamp = new Date().toLocaleTimeString();
    entry.textContent = `[${timestamp}] ${message}`;
    dom.logOutput.appendChild(entry);

    if (activeTab !== 'log') {
        logUnreadCount++;
        updateLogBadge();
    }

    dom.logOutput.scrollTop = dom.logOutput.scrollHeight;

    // Keep only last 100 lines
    while (dom.logOutput.children.length > 100) {
        dom.logOutput.removeChild(dom.logOutput.firstChild);
    }
}

// Tab switching
function switchTab(tabName) {
    activeTab = tabName;

    document.querySelectorAll('.tab-button').forEach(btn => {
        btn.classList.remove('active');
        if (btn.dataset.tab === tabName) btn.classList.add('active');
    });

    document.querySelectorAll('.tab-pane').forEach(pane => {
        pane.classList.remove('active');
        if (pane.id === tabName) pane.classList.add('active');
    });

    if (tabName === 'log') {
        logUnreadCount = 0;
        updateLogBadge();
        // Scroll to bottom when opening
        if (dom.logOutput) {
            dom.logOutput.scrollTop = dom.logOutput.scrollHeight;
        }
    }
}

function updateLogBadge() {
    if (!dom.logBadge) return;
    if (logUnreadCount > 0) {
        dom.logBadge.textContent = logUnreadCount > 99 ? '99+' : logUnreadCount;
        dom.logBadge.style.display = '';
    } else {
        dom.logBadge.style.display = 'none';
    }
}

function setBannerDirty(bannerEl, isDirty) {
    if (bannerEl) bannerEl.style.display = isDirty ? '' : 'none';
}

function markConfigDirty() {
    if (configDirty) return;
    configDirty = true;
    setBannerDirty(dom.unsavedBanner, true);
}

function markConfigClean() {
    configDirty = false;
    setBannerDirty(dom.unsavedBanner, false);
    markNotifClean();
}

function markNotifDirty() {
    if (notifDirty) return;
    notifDirty = true;
    setBannerDirty(dom.unsavedBannerNotifications, true);
}

function markNotifClean() {
    notifDirty = false;
    setBannerDirty(dom.unsavedBannerNotifications, false);
}

// Initialize everything when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    updateThemeToggleLabel(localStorage.getItem(THEME_KEY) || 'system');

    // Cache frequently accessed DOM elements
    dom.statusIndicator = document.getElementById('status-indicator');
    dom.statusText = document.getElementById('status-text');
    dom.checkNow = document.getElementById('check-now');
    dom.updateList = document.getElementById('update-list');
    dom.logOutput = document.getElementById('log-output');
    dom.toggleDaemon = document.getElementById('toggle-daemon');
    dom.daemonIndicator = document.getElementById('daemon-indicator');
    dom.daemonStatusText = document.getElementById('daemon-status-text');
    dom.lastCheck = document.getElementById('last-check');
    dom.modeIndicator = document.getElementById('mode-indicator');
    dom.imageCards = document.getElementById('image-cards');
    dom.addImageBtn = document.getElementById('add-image');
    dom.saveConfigBtn = document.getElementById('save-config');
    dom.historyList = document.getElementById('history-list');
    dom.daemonInterval = document.getElementById('daemon-interval');
    dom.appVersion = document.getElementById('app-version');
    dom.unsavedBanner = document.getElementById('unsaved-banner');
    dom.unsavedBannerNotifications = document.getElementById('unsaved-banner-notifications');
    dom.logBadge = document.getElementById('log-badge');
    document.getElementById('theme-toggle').addEventListener('click', cycleTheme);
    dom.ntfyUrl = document.getElementById('ntfy-url');
    dom.ntfyPriority = document.getElementById('ntfy-priority');
    dom.ntfyTestStatus = document.getElementById('ntfy-test-status');
    dom.webhookUrl = document.getElementById('webhook-url');
    dom.webhookMethod = document.getElementById('webhook-method');
    dom.webhookBodyTemplate = document.getElementById('webhook-body-template');
    dom.webhookTestStatus = document.getElementById('webhook-test-status');
    dom.saveNotifBtn = document.getElementById('save-notifications-config');
    // Load version into footer
    fetch('/api/version').then(r => r.json()).then(data => {
        dom.appVersion.textContent = `ium v${data.version}`;
    }).catch(() => {});

    initSocket();

    // Event listeners
    dom.checkNow.addEventListener('click', checkNow);
    dom.toggleDaemon.addEventListener('click', toggleDaemon);
    dom.saveConfigBtn.addEventListener('click', saveConfig);
    dom.saveNotifBtn.addEventListener('click', saveConfig);
    dom.addImageBtn.addEventListener('click', addNewImage);
    document.getElementById('add-preset').addEventListener('click', showPresetModal);

    dom.ntfyUrl.addEventListener('input', markNotifDirty);
    dom.ntfyPriority.addEventListener('change', markNotifDirty);
    dom.webhookUrl.addEventListener('input', markNotifDirty);
    dom.webhookMethod.addEventListener('change', markNotifDirty);
    dom.webhookBodyTemplate.addEventListener('input', markNotifDirty);
    document.getElementById('test-ntfy').addEventListener('click', () => testNotification('ntfy'));
    document.getElementById('test-webhook').addEventListener('click', () => testNotification('webhook'));

    // Tab switching
    document.querySelectorAll('.tab-button').forEach(btn => {
        btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    addLog('Web UI initialized', 'info');
});