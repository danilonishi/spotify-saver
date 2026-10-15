class SpotifySaverUI {
    constructor() {
        this.apiClient = new ApiClient();
        this.stateManager = new StateManager();
        this.uiManager = new UIManager(() => this.saveState());
        this.downloadManager = new DownloadManager(this.apiClient, this.uiManager, () => this.saveState());
        this.uiManager.onRemoveQueueItem = (taskId, status) => this.downloadManager.cancelItem(taskId, status);
        this.uiManager.onRetryQueueItem = (taskId) => this.downloadManager.retryItem(taskId);
        this.uiManager.onOutputDirectorySelected = (kind, directory) => {
            this.persistOutputDirectoryPreference(kind, directory).catch((error) => {
                this.uiManager.updateStatus(error.message, 'error');
                this.uiManager.addLogEntry(error.message, 'error');
            });
        };
        this.outputDirectoryPreferenceWrites = new Map();
        
        this.currentRoots = {};
        this.settingsVisible = false;
        this.isInitialized = false;
        this.retryCount = 0;
        this.notificationSource = null;
        this.notificationConnectionWarningShown = false;
        this.channelWatcherManager = new ChannelWatcherManager(
            this.apiClient,
            this.uiManager,
            this
        );
        this.urlHandoff = this.captureUrlHandoff();

        this.initialize();
    }

    captureUrlHandoff() {
        const handoffKey = 'spotifysaver_url_handoff';
        const handoffErrorKey = 'spotifysaver_url_handoff_error';
        const handoffModeKey = 'spotifysaver_url_handoff_mode';
        const handoffMediaChoiceKey = 'spotifysaver_url_handoff_media_choice';
        const currentUrl = new URL(window.location.href);
        let handoffUrl = null;
        let handoffError = null;

        if (currentUrl.searchParams.has('url')) {
            const values = currentUrl.searchParams.getAll('url');
            currentUrl.searchParams.delete('url');
            window.history.replaceState(
                window.history.state,
                '',
                `${currentUrl.pathname}${currentUrl.search}${currentUrl.hash}`
            );

            if (values.length !== 1 || !values[0].trim()) {
                handoffError = 'The supplied source link is invalid.';
            } else if (this.isSupportedHandoffUrl(values[0].trim())) {
                handoffUrl = values[0].trim();
                window.sessionStorage.setItem(handoffKey, handoffUrl);
                const isChannelHandoff = this.isYouTubeChannelUrl(handoffUrl);
                if (isChannelHandoff) {
                    window.sessionStorage.setItem(handoffModeKey, 'true');
                    window.sessionStorage.removeItem(handoffMediaChoiceKey);
                } else {
                    window.sessionStorage.setItem(handoffModeKey, 'true');
                    if (this.isSpotifyHandoffUrl(handoffUrl)) {
                        window.sessionStorage.removeItem(handoffMediaChoiceKey);
                    } else {
                        window.sessionStorage.setItem(handoffMediaChoiceKey, 'true');
                    }
                }
            } else {
                handoffError = 'This link is not a supported Spotify or YouTube source.';
            }

            if (handoffError) {
                window.sessionStorage.removeItem(handoffKey);
                window.sessionStorage.removeItem(handoffModeKey);
                window.sessionStorage.removeItem(handoffMediaChoiceKey);
                window.sessionStorage.setItem(handoffErrorKey, handoffError);
            } else {
                window.sessionStorage.removeItem(handoffErrorKey);
            }
        } else {
            handoffUrl = window.sessionStorage.getItem(handoffKey);
            handoffError = window.sessionStorage.getItem(handoffErrorKey);
        }

        return {
            url: handoffUrl,
            error: handoffError,
            focused: window.sessionStorage.getItem(handoffModeKey) === 'true',
            requiresMediaChoice:
                window.sessionStorage.getItem(handoffMediaChoiceKey) === 'true'
        };
    }

    isSupportedHandoffUrl(sourceUrl) {
        try {
            const parsedUrl = new URL(sourceUrl);
            if (!['http:', 'https:'].includes(parsedUrl.protocol) ||
                parsedUrl.username || parsedUrl.password) {
                return false;
            }
            const hostname = parsedUrl.hostname.toLowerCase();
            if (
                hostname === 'open.spotify.com' &&
                /^\/(?:intl-[a-z]{2}(?:-[a-z]{2})?\/)?(?:track|album|playlist)\/[a-zA-Z0-9]+\/?$/.test(
                    parsedUrl.pathname
                )
            ) {
                return true;
            }
            return (
                this.isYouTubeChannelUrl(sourceUrl) ||
                this.downloadManager.isYouTubeTrackUrl(sourceUrl) ||
                this.downloadManager.isYouTubeCollectionUrl(sourceUrl)
            );
        } catch (_error) {
            return false;
        }
    }

    isYouTubeChannelUrl(sourceUrl) {
        try {
            const parsedUrl = new URL(sourceUrl);
            if (!['http:', 'https:'].includes(parsedUrl.protocol) ||
                parsedUrl.username || parsedUrl.password) {
                return false;
            }
            const hostname = parsedUrl.hostname.toLowerCase();
            if (!['youtube.com', 'www.youtube.com', 'm.youtube.com'].includes(hostname)) {
                return false;
            }
            const pathParts = parsedUrl.pathname.split('/').filter(Boolean);
            if (!pathParts.length) {
                return false;
            }
            if (pathParts[0].startsWith('@')) {
                return pathParts[0].length > 1;
            }
            return ['channel', 'c', 'user'].includes(pathParts[0]) &&
                Boolean(pathParts[1]);
        } catch (_error) {
            return false;
        }
    }

    isSpotifyHandoffUrl(sourceUrl) {
        try {
            return new URL(sourceUrl).hostname.toLowerCase() === 'open.spotify.com';
        } catch (_error) {
            return false;
        }
    }

    applyUrlHandoff() {
        const handoffKey = 'spotifysaver_url_handoff';
        const handoffErrorKey = 'spotifysaver_url_handoff_error';
        const urlInput = document.getElementById('spotify-url');

        if (this.urlHandoff.url) {
            if (this.isYouTubeChannelUrl(this.urlHandoff.url)) {
                const channelUrlInput = document.getElementById('watcher-url');
                channelUrlInput.value = this.urlHandoff.url;
                this.switchTab('watcher');
                this.uiManager.updateStatus(
                    'Channel URL ready. Review the watcher settings, then select Watch Channel to start watching.',
                    'info'
                );
                channelUrlInput.focus();
            } else {
                urlInput.value = this.urlHandoff.url;
                this.switchTab('audio');
                this.saveState();
                this.uiManager.updateStatus(
                    'Source link ready. Choose audio or video for YouTube, review your settings, then select Download to queue it.',
                    'info'
                );
            }
        } else if (this.urlHandoff.error) {
            this.uiManager.updateStatus(this.urlHandoff.error, 'error');
        }

        if (this.urlHandoff.focused) {
            this.enterUrlHandoffMode(this.urlHandoff.url || urlInput.value);
            if (this.urlHandoff.url) {
                const handoffInput = this.isYouTubeChannelUrl(this.urlHandoff.url)
                    ? document.getElementById('watcher-url')
                    : urlInput;
                handoffInput.focus();
            }
        }

        window.sessionStorage.removeItem(handoffKey);
        window.sessionStorage.removeItem(handoffErrorKey);
        window.sessionStorage.removeItem('spotifysaver_url_handoff_mode');
        this.urlHandoff = {
            url: null,
            error: null,
            focused: this.urlHandoff.focused,
            requiresMediaChoice: this.urlHandoff.requiresMediaChoice
        };
    }

    enterUrlHandoffMode(sourceUrl) {
        const dashboard = document.getElementById('download-dashboard');
        const isChannelHandoff = this.isYouTubeChannelUrl(sourceUrl);
        dashboard.classList.add('download-handoff-mode');
        dashboard.classList.toggle(
            'spotify-handoff-source',
            this.isSpotifyHandoffUrl(sourceUrl)
        );
        dashboard.classList.toggle('youtube-channel-handoff', isChannelHandoff);
        document.body.classList.add('download-handoff-mode');
        document.getElementById('download-settings-panel').open = true;
        document.getElementById('url-handoff-notice').classList.remove('hidden');
        const requiresMediaChoice = this.urlHandoff.requiresMediaChoice;
        this.uiManager.setDownloadButtonState('Download', requiresMediaChoice);
        document.querySelector('#url-handoff-notice h3').textContent = isChannelHandoff
            ? 'Review channel watcher'
            : 'Review your download';
        document.getElementById('url-handoff-help').textContent = isChannelHandoff
            ? 'Review the channel settings, then select Watch Channel to start watching.'
            : requiresMediaChoice
                ? 'Choose Audio or Video for this YouTube link before downloading.'
                : 'Review your folder and settings, then download when ready.';
        document.getElementById('settings-toggle-btn').classList.add('hidden');
        document.getElementById('logout-btn').classList.add('hidden');
    }

    exitUrlHandoffMode() {
        const dashboard = document.getElementById('download-dashboard');
        dashboard.classList.remove(
            'download-handoff-mode',
            'spotify-handoff-source',
            'youtube-channel-handoff'
        );
        document.body.classList.remove('download-handoff-mode');
        document.getElementById('url-handoff-notice').classList.add('hidden');
        this.uiManager.setDownloadButtonState();
        document.getElementById('settings-toggle-btn').classList.remove('hidden');
        document.getElementById('logout-btn').classList.remove('hidden');
        window.sessionStorage.removeItem('spotifysaver_url_handoff_mode');
        window.sessionStorage.removeItem('spotifysaver_url_handoff_media_choice');
        this.urlHandoff.focused = false;
        this.urlHandoff.requiresMediaChoice = false;
    }

    confirmUrlHandoffMediaType(mode) {
        if (!this.urlHandoff.focused || !this.urlHandoff.requiresMediaChoice ||
            !['audio', 'video'].includes(mode)) {
            return;
        }
        this.urlHandoff.requiresMediaChoice = false;
        window.sessionStorage.removeItem('spotifysaver_url_handoff_media_choice');
        this.uiManager.setDownloadButtonState('Download');
        document.getElementById('url-handoff-help').textContent =
            `You selected ${mode}. Review your folder and settings, then download when ready.`;
    }

    async startDownloadFromUi() {
        if (this.urlHandoff.focused && this.urlHandoff.requiresMediaChoice) {
            this.uiManager.updateStatus(
                'Choose Audio or Video for this YouTube link before downloading.',
                'warning'
            );
            return;
        }
        const queued = await this.downloadManager.startDownload();
        if (queued && this.urlHandoff.focused) {
            document.body.classList.add('download-handoff-complete');
            document.getElementById('url-handoff-help').textContent =
                'Download queued successfully.';
            this.uiManager.setDownloadButtonState('Queued', true);
            this.uiManager.updateStatus(
                'Download queued successfully. This window should close automatically.',
                'success'
            );
            window.postMessage(
                {
                    source: 'spotify-saver',
                    type: 'download-queued',
                    version: 1
                },
                window.location.origin
            );
        }
    }

    async initialize() {
        try {
            this.initializeEventListeners();
            this.loadPersistedState();

            const authentication = await this.apiClient.getAuthenticationStatus();
            if (!authentication.configured) {
                this.showAuthentication('setup');
                return;
            }
            if (!authentication.authenticated) {
                this.showAuthentication('login');
                return;
            }
            this.showAuthenticatedDashboard();
            
            // Check API status with retry mechanism
            const apiAvailable = await this.apiClient.checkApiStatusWithRetry();
            
            if (apiAvailable) {
                this.uiManager.updateStatus('API connected and ready', 'success');
                await this.loadSavedOutputDirectoryPreferences();
                await this.loadOutputDirectories();
                await this.loadAppVersion();
                this.connectNotifications();
                await this.channelWatcherManager.load();
                this.channelWatcherManager.startPolling();
                await this.loadJellyfinLibraryScanSettings();
                await this.loadSpotifySettings();
                await this.loadStorageStatus();
                window.setInterval(() => this.refreshJellyfinLibraryScanStatus(), 30000);
                window.setInterval(() => this.refreshStorageStatus(), 30000);
            } else {
                this.uiManager.updateStatus('API not available. Make sure it is running.', 'error');
            }
            
            this.isInitialized = true;

            // The server owns the download queue; pull its current state so
            // downloads that kept running while the page was closed show up.
            await this.downloadManager.hydrateFromServer();
            this.applyUrlHandoff();

        } catch (error) {
            console.error('Failed to initialize UI:', error);
            this.showAuthenticationError(
                `Could not initialize SpotifySaver: ${error.message}`
            );
        }
    }

    initializeEventListeners() {
        const downloadBtn = document.getElementById('download-btn');
        const stopDownloadBtn = document.getElementById('stop-download-btn');
        const spotifyUrl = document.getElementById('spotify-url');
        const clearUrlBtn = document.getElementById('clear-url-btn');
        const themeToggleBtn = document.getElementById('theme-toggle-btn');
        const clearLogsBtn = document.getElementById('clear-logs-btn');
        const clearQueueBtn = document.getElementById('clear-queue-btn');
        const clearCompletedBtn = document.getElementById('clear-completed-btn');
        const settingsToggleBtn = document.getElementById('settings-toggle-btn');
        
        downloadBtn.addEventListener('click', () => this.startDownloadFromUi());
        document.getElementById('url-handoff-return-btn').addEventListener(
            'click',
            () => this.exitUrlHandoffMode()
        );
        stopDownloadBtn.addEventListener('click', () => this.downloadManager.stopDownload());
        clearUrlBtn.addEventListener('click', () => {
            spotifyUrl.value = '';
            this.saveState();
            spotifyUrl.focus();
        });
        this.setDarkMode(localStorage.getItem('spotifysaver_theme') === 'dark');
        themeToggleBtn.addEventListener('click', () => {
            this.setDarkMode(!document.body.classList.contains('dark-mode'));
        });
        settingsToggleBtn.addEventListener('click', () => {
            this.setSettingsVisible(!this.settingsVisible);
        });
        document.getElementById('initial-setup-form').addEventListener('submit', (event) => {
            event.preventDefault();
            this.submitInitialSetup();
        });
        document.getElementById('login-form').addEventListener('submit', (event) => {
            event.preventDefault();
            this.submitLogin();
        });
        document.getElementById('logout-btn').addEventListener('click', () => {
            this.logout();
        });
        document.getElementById('spotify-settings-form').addEventListener('submit', (event) => {
            event.preventDefault();
            this.saveSpotifySettings();
        });
        document.getElementById('copy-spotify-redirect-uri').addEventListener(
            'click',
            () => this.copySpotifyRedirectUri()
        );
        document.getElementById('spotify-connect').addEventListener('click', () => {
            this.connectSpotify();
        });
        document.getElementById('spotify-remove-settings').addEventListener('click', () => {
            this.removeSpotifySettings();
        });
        document.getElementById('download-configuration-backup').addEventListener(
            'click',
            () => this.downloadConfigurationBackup()
        );
        const backupFileInput = document.getElementById('configuration-backup-file');
        const restoreBackupButton = document.getElementById('restore-configuration-backup');
        backupFileInput.addEventListener('change', () => {
            restoreBackupButton.disabled = backupFileInput.files.length === 0;
        });
        restoreBackupButton.addEventListener('click', () => {
            this.restoreConfigurationBackup().catch((error) => {
                this.setConfigurationBackupMessage(error.message, 'status-error');
            });
        });
        document.getElementById('enable-browser-notifications').addEventListener(
            'click',
            () => this.toggleBrowserNotifications()
        );
        this.setSettingsVisible(false);
        this.initializePanelCollapsePersistence();
        this.initializePanelVisibilityControls();
        this.initializeNotificationPreferences();
        const pickerIds = {
            audio: { editor: 'media-root-editor', trigger: 'set-media-root-btn', apply: 'apply-media-root-btn', addTrigger: 'add-folder-btn', addEditor: 'add-folder-editor', addInput: 'new-folder-input', create: 'create-folder-btn' },
            video: { editor: 'video-media-root-editor', trigger: 'video-set-media-root-btn', apply: 'video-apply-media-root-btn', addTrigger: 'video-add-folder-btn', addEditor: 'video-add-folder-editor', addInput: 'video-new-folder-input', create: 'video-create-folder-btn' },
            watcher: { editor: 'watcher-media-root-editor', trigger: 'watcher-set-media-root-btn', apply: 'watcher-apply-media-root-btn', addTrigger: 'watcher-add-folder-btn', addEditor: 'watcher-add-folder-editor', addInput: 'watcher-new-folder-input', create: 'watcher-create-folder-btn' }
        };
        Object.entries(pickerIds).forEach(([kind, ids]) => {
            const picker = UIManager.PICKERS[kind];
            const options = document.getElementById(picker.options);
            const editor = document.getElementById(ids.editor);
            const rootInput = document.getElementById(picker.rootInput);

            options.addEventListener('click', (event) => {
                const parentButton = event.target.closest('button[data-parent-dir]');
                if (parentButton) {
                    this.applyMediaRoot(kind, parentButton.dataset.parentDir);
                    return;
                }
                const button = event.target.closest('button[data-output-dir]');
                if (button) {
                    this.uiManager.selectOutputDirectory(button, kind);
                }
            });
            options.addEventListener('dblclick', (event) => {
                const button = event.target.closest('button[data-output-dir]');
                if (button) {
                    this.applyMediaRoot(kind, button.dataset.outputDir);
                }
            });
            document.getElementById(ids.trigger).addEventListener('click', () => {
                editor.classList.toggle('hidden');
                if (!editor.classList.contains('hidden')) {
                    rootInput.focus();
                }
            });
            document.getElementById(ids.apply).addEventListener('click', () => this.applyMediaRoot(kind));

            const addEditor = document.getElementById(ids.addEditor);
            const addInput = document.getElementById(ids.addInput);
            document.getElementById(ids.addTrigger).addEventListener('click', () => {
                addEditor.classList.toggle('hidden');
                if (!addEditor.classList.contains('hidden')) {
                    addInput.focus();
                }
            });
            document.getElementById(ids.create).addEventListener('click', () => this.createFolder(kind));
            addInput.addEventListener('keypress', (event) => {
                if (event.key === 'Enter') {
                    this.createFolder(kind);
                }
            });
            rootInput.addEventListener('keypress', (event) => {
                if (event.key === 'Enter') {
                    this.applyMediaRoot(kind);
                }
            });
        });

        document.querySelectorAll('.tab-button').forEach((tab) => {
            tab.addEventListener('click', () => {
                this.switchTab(tab.dataset.tab);
                this.confirmUrlHandoffMediaType(tab.dataset.tab);
            });
        });
        const savedTab = localStorage.getItem('spotifysaver_active_tab');
        this.switchTab(['audio', 'video', 'watcher'].includes(savedTab) ? savedTab : 'audio');

        // Persist checkbox state across reloads
        document.querySelectorAll('.tab-panel input[type="checkbox"]').forEach((box) => {
            const key = `spotifysaver_checkbox_${box.id}`;
            const saved = localStorage.getItem(key);
            if (saved !== null) {
                box.checked = saved === 'true';
            }
            box.addEventListener('change', () => localStorage.setItem(key, box.checked));
        });

        // Persist video format and resolution selections across reloads.
        document.querySelectorAll('#tab-video select, #tab-watcher select').forEach((select) => {
            const key = `spotifysaver_select_${select.id}`;
            const saved = localStorage.getItem(key);
            if (saved !== null && Array.from(select.options).some((option) => option.value === saved)) {
                select.value = saved;
            }
            select.addEventListener('change', () => localStorage.setItem(key, select.value));
        });

        clearQueueBtn.addEventListener('click', () => this.downloadManager.clearQueue());
        clearCompletedBtn.addEventListener('click', () => this.downloadManager.clearCompleted());
        document.getElementById('jellyfin-server-url').addEventListener('blur', (event) => {
            event.target.value = this.normalizeJellyfinServerUrl(event.target.value);
        });
        document.getElementById('jellyfin-settings-form').addEventListener('submit', (event) => {
            event.preventDefault();
            this.saveJellyfinLibraryScanSettings();
        });
        document.getElementById('jellyfin-remove-api-key').addEventListener('click', () => {
            this.removeJellyfinApiKey();
        });
        document.getElementById('jellyfin-scan-now').addEventListener('click', () => {
            this.requestJellyfinLibraryScan();
        });
        document.getElementById('storage-reserve-form').addEventListener('submit', (event) => {
            event.preventDefault();
            this.saveStorageSettings();
        });
        
        // Allow starting a download with Enter
        spotifyUrl.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                this.startDownloadFromUi();
            }
        });
        document.getElementById('watcher-url').addEventListener('keypress', (event) => {
            if (event.key === 'Enter') {
                this.channelWatcherManager.saveChannel();
            }
        });

        // Button to clear logs and state
        clearLogsBtn.addEventListener('click', () => {
            if (confirm('Are you sure you want to clear logs and state? This cannot be undone.')) {
                this.uiManager.clearLog();
                this.downloadManager.clearStates();
                this.stateManager.clearPersistedState();
                this.uiManager.updateStatus('API connected and ready', 'info');
                this.uiManager.addLogEntry('Logs and state manually cleared', 'info');
            }
        });
    }

    setDarkMode(enabled) {
        const themeToggleBtn = document.getElementById('theme-toggle-btn');
        document.body.classList.toggle('dark-mode', enabled);
        themeToggleBtn.setAttribute('aria-pressed', String(enabled));
        themeToggleBtn.textContent = enabled ? '☀️ Light mode' : '🌙 Dark mode';
        localStorage.setItem('spotifysaver_theme', enabled ? 'dark' : 'light');
    }

    getBackupPanelIds() {
        return [
            'download-status-panel',
            'download-settings-panel',
            'download-queue-panel',
            'channel-watcher-panel',
            'activity-log-panel'
        ];
    }

    getBackupBrowserPreferences() {
        const panelIds = this.getBackupPanelIds();
        const detailsIds = Array.from(
            document.querySelectorAll('details.panel-details[id]'),
            (panel) => panel.id
        );
        return {
            dark_mode: document.body.classList.contains('dark-mode'),
            browser_notifications_enabled: this.browserNotificationsEnabled,
            notification_preferences: Object.fromEntries(
                Array.from(
                    document.querySelectorAll('input[data-notification-preference]'),
                    (control) => [control.dataset.notificationPreference, control.checked]
                )
            ),
            panel_visibility: Object.fromEntries(
                panelIds.map((id) => [
                    id,
                    localStorage.getItem(`spotifysaver_panel_${id}`) !== 'hidden'
                ])
            ),
            panel_open: Object.fromEntries(
                detailsIds.map((id) => [
                    id,
                    document.getElementById(id).open
                ])
            ),
            panel_layout: this.panelLayout
        };
    }

    applyBackupBrowserPreferences(preferences) {
        if (typeof preferences.dark_mode === 'boolean') {
            this.setDarkMode(preferences.dark_mode);
        }

        if (typeof preferences.browser_notifications_enabled === 'boolean') {
            this.browserNotificationsEnabled = preferences.browser_notifications_enabled;
            localStorage.setItem(
                'spotifysaver_browser_notifications',
                preferences.browser_notifications_enabled ? 'enabled' : 'disabled'
            );
            this.updateBrowserNotificationControls();
        }

        const notificationPreferences = preferences.notification_preferences || {};
        document.querySelectorAll('input[data-notification-preference]').forEach((control) => {
            const category = control.dataset.notificationPreference;
            if (typeof notificationPreferences[category] !== 'boolean') return;
            control.checked = notificationPreferences[category];
            localStorage.setItem(
                `spotifysaver_notification_${category}`,
                control.checked ? 'enabled' : 'disabled'
            );
        });

        const panelIds = this.getBackupPanelIds();
        const visibility = preferences.panel_visibility || {};
        for (const panelId of panelIds) {
            if (typeof visibility[panelId] !== 'boolean') continue;
            const control = document.querySelector(
                `input[data-panel-visibility="${panelId}"]`
            );
            this.setPanelVisibility(panelId, control, visibility[panelId]);
            localStorage.setItem(
                `spotifysaver_panel_${panelId}`,
                visibility[panelId] ? 'visible' : 'hidden'
            );
        }

        const panelOpen = preferences.panel_open || {};
        for (const [panelId, isOpen] of Object.entries(panelOpen)) {
            const panel = document.getElementById(panelId);
            if (
                panel instanceof HTMLDetailsElement
                && panel.classList.contains('panel-details')
                && typeof isOpen === 'boolean'
            ) {
                panel.open = isOpen;
                localStorage.setItem(
                    `spotifysaver_panel_open_${panelId}`,
                    String(isOpen)
                );
            }
        }

        const knownPanels = new Set(panelIds);
        const importedLayout = preferences.panel_layout || {};
        const layout = { left: [], right: [] };
        for (const column of ['left', 'right']) {
            const values = Array.isArray(importedLayout[column])
                ? importedLayout[column]
                : [];
            for (const panelId of values) {
                if (
                    knownPanels.has(panelId)
                    && !layout.left.includes(panelId)
                    && !layout.right.includes(panelId)
                ) {
                    layout[column].push(panelId);
                }
            }
        }
        for (const panelId of panelIds) {
            if (!layout.left.includes(panelId) && !layout.right.includes(panelId)) {
                layout[layout.left.length < 2 ? 'left' : 'right'].push(panelId);
            }
        }
        this.panelLayout = layout;
        this.savePanelLayout();
        this.applyPanelLayout(document.querySelector('.panel-visibility-options'));
    }

    async downloadConfigurationBackup() {
        const status = document.getElementById('configuration-backup-status');
        try {
            const response = await fetch(
                `${this.apiClient.apiUrl}/configuration-backup`,
                { credentials: 'same-origin', cache: 'no-store' }
            );
            const result = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(result.detail || 'Could not create a configuration backup');
            }
            const backup = {
                ...result,
                browser_preferences: this.getBackupBrowserPreferences()
            };
            const blob = new Blob(
                [JSON.stringify(backup, null, 2)],
                { type: 'application/json' }
            );
            const downloadUrl = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = downloadUrl;
            link.download = `spotifysaver-config-${new Date().toISOString().slice(0, 10)}.json`;
            link.click();
            window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000);
            this.setConfigurationBackupMessage(
                'Configuration backup downloaded. Keep the file somewhere private; it contains channel URLs and server paths.',
                'watcher-help'
            );
        } catch (error) {
            this.setConfigurationBackupMessage(error.message, 'status-error');
        }
    }

    async restoreConfigurationBackup() {
        const input = document.getElementById('configuration-backup-file');
        const file = input.files[0];
        if (!file) {
            throw new Error('Choose a configuration backup file first');
        }
        if (file.size > 5 * 1024 * 1024) {
            throw new Error('The configuration backup file is larger than 5 MB');
        }
        const backup = JSON.parse(await file.text());
        if (
            backup?.schema_version !== 1
            || !backup.server
            || typeof backup.server !== 'object'
        ) {
            throw new Error('This is not a supported SpotifySaver configuration backup');
        }
        if (
            !confirm(
                'Restore this backup? It will replace watched channels and included settings on this instance. Existing API keys, passwords, Spotify secrets, and authorization tokens will not be imported.'
            )
        ) {
            return;
        }

        const response = await fetch(
            `${this.apiClient.apiUrl}/configuration-backup/restore`,
            {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(backup)
            }
        );
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(result.detail || 'Could not restore the configuration backup');
        }

        this.applyBackupBrowserPreferences(backup.browser_preferences || {});
        await this.loadSavedOutputDirectoryPreferences();
        await this.loadOutputDirectories();
        await this.channelWatcherManager.load();
        await this.loadJellyfinLibraryScanSettings();
        await this.loadSpotifySettings();
        this.setConfigurationBackupMessage(
            'Configuration restored. Re-enter any required secrets and reconnect Spotify if needed.',
            'watcher-help'
        );
    }

    setConfigurationBackupMessage(message, className) {
        const status = document.getElementById('configuration-backup-status');
        status.textContent = message;
        status.className = className;
    }

    loadPersistedState() {
        const state = this.stateManager.loadPersistedState();
        if (!state) return;

        // Only form data and logs are restored locally; the download queue
        // itself is always loaded fresh from the server.
        this.stateManager.restoreFormData(state);
    }

    saveState() {
        this.stateManager.saveState();
    }

    async loadSavedOutputDirectoryPreferences() {
        let preferences = { audio: null, video: null, watcher: null };
        try {
            preferences = await this.apiClient.getDownloadDirectoryPreferences();
        } catch (error) {
            console.warn('Could not load saved download folders:', error);
            this.uiManager.updateStatus(
                `Could not load saved download folders: ${error.message}`,
                'error'
            );
        }
        this.uiManager.setPreferredOutputDirectories(preferences);
    }

    showAuthentication(mode) {
        const setupMode = mode === 'setup';
        document.getElementById('authentication-panel').classList.remove('hidden');
        document.getElementById('download-dashboard').classList.add('hidden');
        document.getElementById('settings-toggle-btn').classList.add('hidden');
        document.getElementById('logout-btn').classList.add('hidden');
        document.getElementById('authentication-heading').textContent =
            setupMode ? 'Set up SpotifySaver' : 'Sign in to SpotifySaver';
        document.getElementById('authentication-help').textContent = setupMode
            ? 'Enter the one-time setup token from the server logs and create an administrator password.'
            : 'Sign in with the administrator password for this SpotifySaver server.';
        document.getElementById('initial-setup-form').classList.toggle('hidden', !setupMode);
        document.getElementById('login-form').classList.toggle('hidden', setupMode);
        document.getElementById('authentication-status').textContent = '';
    }

    showAuthenticatedDashboard() {
        document.getElementById('authentication-panel').classList.add('hidden');
        document.getElementById('download-dashboard').classList.remove('hidden');
        document.getElementById('settings-toggle-btn').classList.remove('hidden');
        document.getElementById('logout-btn').classList.remove('hidden');
    }

    showAuthenticationError(message) {
        document.getElementById('authentication-panel').classList.remove('hidden');
        document.getElementById('download-dashboard').classList.add('hidden');
        const status = document.getElementById('authentication-status');
        status.textContent = message;
        status.className = 'status-error';
    }

    async submitInitialSetup() {
        const form = document.getElementById('initial-setup-form');
        const status = document.getElementById('authentication-status');
        const submit = form.querySelector('button[type="submit"]');
        submit.disabled = true;
        status.textContent = 'Setting up administrator access...';
        status.className = 'watcher-help';
        try {
            await this.apiClient.setInitialPassword(
                document.getElementById('setup-token').value.trim(),
                document.getElementById('setup-password').value
            );
            window.location.reload();
        } catch (error) {
            status.textContent = error.message;
            status.className = 'status-error';
        } finally {
            submit.disabled = false;
        }
    }

    async submitLogin() {
        const form = document.getElementById('login-form');
        const status = document.getElementById('authentication-status');
        const submit = form.querySelector('button[type="submit"]');
        submit.disabled = true;
        status.textContent = 'Signing in...';
        status.className = 'watcher-help';
        try {
            await this.apiClient.login(document.getElementById('login-password').value);
            window.location.reload();
        } catch (error) {
            status.textContent = error.message;
            status.className = 'status-error';
        } finally {
            submit.disabled = false;
        }
    }

    async logout() {
        try {
            await this.apiClient.logout();
            window.location.reload();
        } catch (error) {
            this.uiManager.updateStatus(error.message, 'error');
        }
    }

    async loadSpotifySettings() {
        const redirectInput = document.getElementById('spotify-redirect-uri');
        redirectInput.value = `${window.location.origin}/api/v1/spotify/callback`;
        try {
            const settings = await this.apiClient.getSpotifySettings();
            document.getElementById('spotify-client-id').value = settings.client_id || '';
            document.getElementById('spotify-client-secret').value = '';
            this.renderSpotifySettings(settings);
        } catch (error) {
            this.setSpotifyConnectionIndicator('Spotify connection status unavailable', 'is-unknown');
            this.setSpotifySettingsMessage(error.message, 'status-error');
        }
    }

    async saveSpotifySettings() {
        const saveButton = document.getElementById('spotify-save-settings');
        saveButton.disabled = true;
        this.setSpotifySettingsMessage('Saving Spotify settings...', 'watcher-help');
        try {
            const settings = await this.apiClient.saveSpotifySettings({
                client_id: document.getElementById('spotify-client-id').value.trim(),
                client_secret: document.getElementById('spotify-client-secret').value,
                redirect_uri: document.getElementById('spotify-redirect-uri').value.trim()
            });
            document.getElementById('spotify-client-secret').value = '';
            this.renderSpotifySettings(settings);
        } catch (error) {
            this.setSpotifySettingsMessage(error.message, 'status-error');
        } finally {
            saveButton.disabled = false;
        }
    }

    async copySpotifyRedirectUri() {
        const input = document.getElementById('spotify-redirect-uri');
        const status = document.getElementById('spotify-redirect-copy-status');
        try {
            await navigator.clipboard.writeText(input.value);
            status.textContent = 'Redirect URL copied.';
            status.className = 'status-success';
        } catch {
            status.textContent = 'Could not copy automatically. Select and copy the URL above.';
            status.className = 'status-error';
        }
    }

    async connectSpotify() {
        const connectButton = document.getElementById('spotify-connect');
        connectButton.disabled = true;
        this.setSpotifySettingsMessage('Opening Spotify authorization...', 'watcher-help');
        try {
            const result = await this.apiClient.startSpotifyAuthorization();
            window.location.assign(result.authorization_url);
        } catch (error) {
            this.setSpotifySettingsMessage(error.message, 'status-error');
            connectButton.disabled = false;
        }
    }

    async removeSpotifySettings() {
        if (!confirm('Remove the saved Spotify credentials and account authorization?')) {
            return;
        }
        const removeButton = document.getElementById('spotify-remove-settings');
        removeButton.disabled = true;
        try {
            const result = await this.apiClient.removeSpotifySettings();
            document.getElementById('spotify-client-id').value = '';
            document.getElementById('spotify-client-secret').value = '';
            this.renderSpotifySettings(result);
        } catch (error) {
            this.setSpotifySettingsMessage(error.message, 'status-error');
        }
    }

    renderSpotifySettings(settings) {
        const redirectMatches = settings.redirect_uri ===
            document.getElementById('spotify-redirect-uri').value;
        document.getElementById('spotify-connect').disabled =
            !settings.credentials_configured || !redirectMatches;
        document.getElementById('spotify-remove-settings').disabled =
            !settings.credentials_configured;
        this.setSpotifyConnectionIndicator(
            settings.authorized ? 'Connected' : 'Not connected',
            settings.authorized ? 'is-connected' : 'is-disconnected'
        );
        const message = settings.credentials_configured && !redirectMatches
            ? 'Save Spotify Settings to apply this redirect URL before connecting to Spotify.'
            : settings.authorized
                ? 'Spotify account is connected. Spotify URLs and playlist downloads are ready.'
                : settings.credentials_configured
                    ? 'Credentials are saved. Connect your Spotify account to enable playlist access.'
                    : 'Spotify is not configured. Direct YouTube downloads do not require Spotify credentials.';
        this.setSpotifySettingsMessage(message, 'watcher-help');
    }

    setSpotifyConnectionIndicator(message, stateClass) {
        const indicator = document.getElementById('spotify-connection-indicator');
        indicator.classList.remove('is-connected', 'is-disconnected', 'is-unknown');
        indicator.classList.add(stateClass);
        indicator.querySelector('.spotify-connection-label').textContent = message;
    }

    setSpotifySettingsMessage(message, className) {
        const status = document.getElementById('spotify-settings-status');
        status.textContent = message;
        status.className = className;
    }

    async loadJellyfinLibraryScanSettings() {
        try {
            const settings = await this.apiClient.getJellyfinLibraryScanSettings();
            document.getElementById('jellyfin-server-url').value = settings.server_url || '';
            document.getElementById('jellyfin-scan-interval').value =
                settings.scan_interval_minutes || 5;
            document.getElementById('jellyfin-api-key').value = '';
            this.renderJellyfinLibraryScanStatus(settings);
        } catch (error) {
            this.setJellyfinLibraryScanMessage(error.message, 'status-error');
        }
    }

    async loadStorageStatus() {
        try {
            const status = await this.apiClient.getStorageStatus();
            this.renderStorageStatus(status, true);
        } catch (error) {
            this.setStorageSettingsMessage(error.message, 'status-error');
        }
    }

    async refreshStorageStatus() {
        try {
            const status = await this.apiClient.getStorageStatus();
            this.renderStorageStatus(status, false);
        } catch (error) {
            console.warn('Could not refresh storage status:', error);
        }
    }

    async saveStorageSettings() {
        const saveButton = document.getElementById('storage-save-settings');
        saveButton.disabled = true;
        this.setStorageSettingsMessage('Saving storage settings...', 'watcher-help');
        try {
            const status = await this.apiClient.saveStorageSettings({
                minimum_free_space_gb: Number(
                    document.getElementById('storage-minimum-free-gb').value
                )
            });
            this.renderStorageStatus(status, true);
            this.setStorageSettingsMessage('Storage settings saved.', 'watcher-help');
        } catch (error) {
            this.setStorageSettingsMessage(error.message, 'status-error');
        } finally {
            saveButton.disabled = false;
        }
    }

    renderStorageStatus(status, updateInput) {
        if (updateInput) {
            document.getElementById('storage-minimum-free-gb').value =
                status.minimum_free_space_gb ?? 0;
        }
        const filesystems = document.getElementById('storage-filesystem-list');
        filesystems.replaceChildren();
        (status.filesystems || []).forEach((filesystem) => {
            const item = document.createElement('li');
            const labels = Array.from(
                new Set((filesystem.paths || []).map((entry) => entry.label))
            ).join(', ');
            const paths = Array.from(
                new Set((filesystem.paths || []).map((entry) => entry.path))
            ).join(', ');
            item.className = filesystem.below_reserve
                ? 'storage-filesystem storage-filesystem-low'
                : 'storage-filesystem';
            item.textContent =
                `${filesystem.label}: ${this.formatStorageBytes(filesystem.free_bytes)} free` +
                ` of ${this.formatStorageBytes(filesystem.total_bytes)} (${labels}). ${paths}`;
            if (filesystem.below_reserve) {
                item.textContent +=
                    ` Below the ${status.minimum_free_space_gb} GB reserve; new downloads to this storage wait.`;
            }
            filesystems.appendChild(item);
        });
        (status.errors || []).forEach((error) => {
            const item = document.createElement('li');
            item.className = 'storage-filesystem storage-filesystem-low';
            item.textContent = `${error.label} (${error.path}): ${error.error}`;
            filesystems.appendChild(item);
        });
        if (!filesystems.childElementCount) {
            const empty = document.createElement('li');
            empty.className = 'queue-empty';
            empty.textContent = 'No download storage locations are configured.';
            filesystems.appendChild(empty);
        }
        this.setStorageSettingsMessage(
            status.protection_enabled
                ? `Protection enabled. Keep at least ${status.minimum_free_space_gb} GB free on each destination filesystem.`
                : 'Protection is disabled. Downloads will not be blocked for low free space.',
            'watcher-help'
        );
    }

    formatStorageBytes(bytes) {
        const gigabytes = Number(bytes || 0) / 1_000_000_000;
        return `${gigabytes.toFixed(2)} GB`;
    }

    setStorageSettingsMessage(message, className) {
        const status = document.getElementById('storage-settings-status');
        status.textContent = message;
        status.className = className;
    }

    async refreshJellyfinLibraryScanStatus() {
        try {
            const settings = await this.apiClient.getJellyfinLibraryScanSettings();
            this.renderJellyfinLibraryScanStatus(settings);
        } catch (error) {
            console.warn('Could not refresh Jellyfin library scan status:', error);
        }
    }

    async saveJellyfinLibraryScanSettings() {
        const saveButton = document.getElementById('jellyfin-save-settings');
        saveButton.disabled = true;
        this.setJellyfinLibraryScanMessage('Saving Jellyfin settings...', 'watcher-help');
        try {
            const serverUrlInput = document.getElementById('jellyfin-server-url');
            serverUrlInput.value = this.normalizeJellyfinServerUrl(serverUrlInput.value);
            const settings = await this.apiClient.saveJellyfinLibraryScanSettings({
                server_url: serverUrlInput.value,
                api_key: document.getElementById('jellyfin-api-key').value,
                clear_api_key: false,
                scan_interval_minutes: Number(
                    document.getElementById('jellyfin-scan-interval').value
                )
            });
            document.getElementById('jellyfin-api-key').value = '';
            this.renderJellyfinLibraryScanStatus(settings);
            this.uiManager.addLogEntry('Jellyfin library scan settings saved', 'success');
        } catch (error) {
            this.setJellyfinLibraryScanMessage(error.message, 'status-error');
        } finally {
            saveButton.disabled = false;
        }
    }

    normalizeJellyfinServerUrl(value) {
        const trimmed = value.trim();
        return trimmed && !/^[a-z][a-z\d+.-]*:\/\//i.test(trimmed)
            ? `http://${trimmed}`
            : trimmed;
    }

    async removeJellyfinApiKey() {
        if (!confirm('Remove the saved Jellyfin API key? Automatic library scans will stop until a new key is saved.')) {
            return;
        }

        const removeButton = document.getElementById('jellyfin-remove-api-key');
        let apiKeyConfigured = !removeButton.disabled;
        removeButton.disabled = true;
        this.setJellyfinLibraryScanMessage('Removing the saved Jellyfin API key...', 'watcher-help');
        try {
            const settings = await this.apiClient.removeJellyfinApiKey();
            apiKeyConfigured = settings.api_key_configured;
            document.getElementById('jellyfin-api-key').value = '';
            this.renderJellyfinLibraryScanStatus(settings);
            this.uiManager.addLogEntry('Jellyfin API key removed', 'info');
        } catch (error) {
            this.setJellyfinLibraryScanMessage(error.message, 'status-error');
        } finally {
            removeButton.disabled = !apiKeyConfigured;
        }
    }

    async requestJellyfinLibraryScan() {
        const scanButton = document.getElementById('jellyfin-scan-now');
        scanButton.disabled = true;
        try {
            const settings = await this.apiClient.requestJellyfinLibraryScan();
            this.renderJellyfinLibraryScanStatus(settings);
            this.uiManager.addLogEntry('Manual Jellyfin library scan requested', 'info');
        } catch (error) {
            this.setJellyfinLibraryScanMessage(error.message, 'status-error');
        } finally {
            scanButton.disabled = false;
        }
    }

    renderJellyfinLibraryScanStatus(settings) {
        document.getElementById('jellyfin-remove-api-key').disabled =
            !settings.api_key_configured;
        document.getElementById('jellyfin-scan-now').disabled =
            !settings.configured || settings.manual_scan_pending;
        if (!settings.configured) {
            this.setJellyfinLibraryScanMessage(
                settings.server_url
                    ? 'Jellyfin scans are disabled until an API key is configured.'
                    : 'Jellyfin scans are not configured.',
                'watcher-help'
            );
            return;
        }

        if (settings.last_error) {
            const retryAt = settings.next_scan_at
                ? new Date(settings.next_scan_at).toLocaleString()
                : null;
            this.setJellyfinLibraryScanMessage(
                `Last library scan request failed: ${settings.last_error}` +
                    (settings.scan_pending && retryAt
                        ? ` A retry is scheduled after ${retryAt}.`
                        : ''),
                'status-error'
            );
            return;
        }

        if (settings.scan_pending) {
            if (settings.manual_scan_pending) {
                const message = settings.queue_idle
                    ? 'Manual library scan requested; starting shortly.'
                    : 'Manual library scan requested; it will start when downloads finish.';
                this.setJellyfinLibraryScanMessage(message, 'watcher-help');
                return;
            }
            const nextScan = settings.next_scan_at
                ? new Date(settings.next_scan_at)
                : null;
            const waitingForCooldown = nextScan && nextScan.getTime() > Date.now();
            const queueMessage = settings.queue_idle
                ? waitingForCooldown
                    ? `A library scan is pending until ${nextScan.toLocaleString()}.`
                    : 'A library scan is pending and will start shortly.'
                : 'A library scan is pending until the download queue is idle.';
            this.setJellyfinLibraryScanMessage(
                queueMessage,
                'watcher-help'
            );
            return;
        }

        const lastScan = settings.last_scan_at
            ? `Last scan requested ${new Date(settings.last_scan_at).toLocaleString()}.`
            : 'No library scan has been requested yet.';
        this.setJellyfinLibraryScanMessage(lastScan, 'watcher-help');
    }

    setJellyfinLibraryScanMessage(message, className) {
        const status = document.getElementById('jellyfin-settings-status');
        status.textContent = message;
        status.className = className;
    }

    setSettingsVisible(visible) {
        this.settingsVisible = visible;
        const dashboard = document.getElementById('download-dashboard');
        dashboard.classList.toggle('settings-mode', visible);
        document.getElementById('download-settings-panel').classList.toggle('hidden', visible);
        document.getElementById('storage-settings-panel').classList.toggle('hidden', !visible);
        document.getElementById('jellyfin-settings-panel').classList.toggle('hidden', !visible);
        document.getElementById('spotify-settings-panel').classList.toggle('hidden', !visible);
        document.getElementById('configuration-backup-panel').classList.toggle('hidden', !visible);
        document.getElementById('panel-visibility-settings').classList.toggle('hidden', !visible);
        document.getElementById('notification-preferences-panel').classList.toggle('hidden', !visible);
        const button = document.getElementById('settings-toggle-btn');
        button.setAttribute('aria-pressed', String(visible));
        button.textContent = visible ? '⬅️ Back to downloads' : '⚙️ Settings';
    }

    initializePanelVisibilityControls() {
        const controls = document.querySelectorAll('input[data-panel-visibility]');
        controls.forEach((control) => {
            const panelId = control.dataset.panelVisibility;
            const saved = localStorage.getItem(`spotifysaver_panel_${panelId}`);
            const visible = saved !== 'hidden';
            this.setPanelVisibility(panelId, control, visible);
            control.addEventListener('change', () => {
                this.setPanelVisibility(panelId, control, control.checked);
                localStorage.setItem(
                    `spotifysaver_panel_${panelId}`,
                    control.checked ? 'visible' : 'hidden'
                );
            });
        });

        const panelOptions = document.querySelector('.panel-visibility-options');
        this.panelLayout = this.getSavedPanelLayout();
        this.applyPanelLayout(panelOptions);

        let draggedOption = null;
        panelOptions.addEventListener('dragstart', (event) => {
            draggedOption = event.target.closest('[data-panel-layout]');
            if (!draggedOption) return;
            event.dataTransfer.effectAllowed = 'move';
            event.dataTransfer.setData('text/plain', draggedOption.dataset.panelLayout);
            draggedOption.classList.add('is-dragging');
        });
        panelOptions.addEventListener('dragend', () => {
            draggedOption?.classList.remove('is-dragging');
            draggedOption = null;
        });
        panelOptions.addEventListener('dragover', (event) => {
            if (!draggedOption) return;
            event.preventDefault();
            event.dataTransfer.dropEffect = 'move';
        });
        panelOptions.addEventListener('drop', (event) => {
            const target = event.target.closest('[data-panel-layout]');
            if (!draggedOption) return;
            event.preventDefault();
            const destinationColumn = target
                ? target.dataset.panelColumn
                : this.getPanelColumnAtPosition(panelOptions, event.clientX);
            if (!destinationColumn) return;
            this.movePanelOption(draggedOption, target, destinationColumn, event);
            this.savePanelLayout();
            this.applyPanelLayout(panelOptions);
        });
    }

    initializePanelCollapsePersistence() {
        document.querySelectorAll('details.panel-details[id]').forEach((panel) => {
            const key = `spotifysaver_panel_open_${panel.id}`;
            const saved = localStorage.getItem(key);
            if (saved !== null) {
                panel.open = saved === 'true';
            }
            panel.addEventListener('toggle', () => {
                localStorage.setItem(key, String(panel.open));
            });
        });
    }

    initializeNotificationPreferences() {
        this.browserNotificationsEnabled =
            localStorage.getItem('spotifysaver_browser_notifications') === 'enabled';
        document.querySelectorAll('input[data-notification-preference]').forEach((control) => {
            const key = `spotifysaver_notification_${control.dataset.notificationPreference}`;
            const saved = localStorage.getItem(key);
            control.checked = saved === null ? true : saved === 'enabled';
            control.addEventListener('change', () => {
                localStorage.setItem(key, control.checked ? 'enabled' : 'disabled');
            });
        });
        this.updateBrowserNotificationControls();
    }

    async toggleBrowserNotifications() {
        const status = document.getElementById('browser-notification-status');
        if (!('Notification' in window)) {
            status.textContent = 'Browser notifications are not supported by this browser.';
            return;
        }
        if (!window.isSecureContext) {
            status.textContent = 'Browser notifications require HTTPS or localhost.';
            return;
        }

        if (this.browserNotificationsEnabled && Notification.permission === 'granted') {
            this.browserNotificationsEnabled = false;
            localStorage.setItem('spotifysaver_browser_notifications', 'disabled');
            this.updateBrowserNotificationControls();
            return;
        }

        let permission = Notification.permission;
        if (permission === 'default') {
            try {
                permission = await Notification.requestPermission();
            } catch (error) {
                console.error('Could not request browser notification permission:', error);
                status.textContent = 'Could not request browser notification permission.';
                return;
            }
        }
        this.browserNotificationsEnabled = permission === 'granted';
        localStorage.setItem(
            'spotifysaver_browser_notifications',
            this.browserNotificationsEnabled ? 'enabled' : 'disabled'
        );
        this.updateBrowserNotificationControls();
    }

    updateBrowserNotificationControls() {
        const button = document.getElementById('enable-browser-notifications');
        const status = document.getElementById('browser-notification-status');
        if (!('Notification' in window)) {
            button.disabled = true;
            status.textContent = 'Browser notifications are not supported by this browser.';
            return;
        }
        if (!window.isSecureContext) {
            button.disabled = true;
            status.textContent = 'Browser notifications require HTTPS or localhost.';
            return;
        }
        button.disabled = Notification.permission === 'denied';
        if (Notification.permission === 'denied') {
            status.textContent = 'Permission is blocked. Change it in your browser site settings.';
        } else if (Notification.permission === 'granted' && this.browserNotificationsEnabled) {
            status.textContent = 'Browser notifications are enabled.';
            button.textContent = 'Disable browser notifications';
            button.disabled = false;
        } else {
            status.textContent = 'Browser notifications are off.';
            button.textContent = 'Enable browser notifications';
        }
    }

    connectNotifications() {
        if (this.notificationSource || !window.EventSource) return;
        const lastEventId = localStorage.getItem('spotifysaver_notification_last_id') || '0';
        const url = new URL(`${this.apiClient.apiUrl}/notifications/events`);
        url.searchParams.set('after', lastEventId);
        this.notificationSource = new EventSource(url);
        this.notificationSource.addEventListener('notification', (event) => {
            localStorage.setItem('spotifysaver_notification_last_id', event.lastEventId);
            let notification;
            try {
                notification = JSON.parse(event.data);
            } catch (error) {
                console.warn('Could not parse a server notification:', error);
                return;
            }
            const preference = Array.from(
                document.querySelectorAll('input[data-notification-preference]')
            ).find((control) => control.dataset.notificationPreference === notification.category);
            if (preference?.checked) this.showNotification(notification);
        });
        this.notificationSource.onopen = () => {
            this.notificationConnectionWarningShown = false;
        };
        this.notificationSource.onerror = () => {
            if (this.notificationConnectionWarningShown) return;
            this.notificationConnectionWarningShown = true;
            console.warn('Live notifications disconnected; the browser will retry automatically.');
        };
    }

    showNotification(notification) {
        if (
            this.browserNotificationsEnabled &&
            'Notification' in window &&
            Notification.permission === 'granted'
        ) {
            const browserNotification = new Notification('SpotifySaver', {
                body: notification.message,
                tag: `spotify-saver-${notification.id}`
            });
            browserNotification.onclick = () => {
                window.focus();
                browserNotification.close();
            };
            return;
        }
        const container = document.getElementById('notification-container');
        const toast = document.createElement('div');
        toast.className = `notification-toast notification-${notification.category}`;
        toast.setAttribute('role', 'status');

        const message = document.createElement('span');
        message.textContent = notification.message;
        const dismiss = document.createElement('button');
        dismiss.type = 'button';
        dismiss.className = 'notification-dismiss';
        dismiss.setAttribute('aria-label', 'Dismiss notification');
        dismiss.textContent = '×';
        dismiss.addEventListener('click', () => toast.remove());

        toast.append(message, dismiss);
        container.appendChild(toast);
        window.setTimeout(() => toast.remove(), 10000);
    }

    getSavedPanelLayout() {
        const panelIds = Array.from(
            document.querySelectorAll('[data-panel-layout]'),
            (option) => option.dataset.panelLayout
        );
        const defaultLayout = {
            left: panelIds.slice(0, 2),
            right: panelIds.slice(2)
        };
        try {
            const saved = JSON.parse(
                localStorage.getItem('spotifysaver_panel_order') || 'null'
            );
            if (Array.isArray(saved)) {
                const ordered = Array.from(
                    new Set(saved.filter((panelId) => panelIds.includes(panelId)))
                );
                const completeOrder = [
                    ...ordered,
                    ...panelIds.filter((panelId) => !ordered.includes(panelId))
                ];
                return {
                    left: completeOrder.slice(0, 2),
                    right: completeOrder.slice(2)
                };
            }
            if (!saved || typeof saved !== 'object') return defaultLayout;
            const left = Array.isArray(saved.left) ? saved.left : [];
            const right = Array.isArray(saved.right) ? saved.right : [];
            const assigned = new Set();
            const layout = { left: [], right: [] };
            for (const column of ['left', 'right']) {
                for (const panelId of column === 'left' ? left : right) {
                    if (panelIds.includes(panelId) && !assigned.has(panelId)) {
                        layout[column].push(panelId);
                        assigned.add(panelId);
                    }
                }
            }
            for (const panelId of panelIds) {
                if (!assigned.has(panelId)) {
                    const column = layout.left.length < 2 ? 'left' : 'right';
                    layout[column].push(panelId);
                }
            }
            return layout;
        } catch (error) {
            console.warn('Could not load panel layout preference; using the default layout:', error);
            return defaultLayout;
        }
    }

    getPanelColumnAtPosition(panelOptions, clientX) {
        const bounds = panelOptions.getBoundingClientRect();
        return clientX < bounds.left + bounds.width / 2 ? 'left' : 'right';
    }

    movePanelOption(draggedOption, targetOption, destinationColumn, event) {
        const sourceColumn = draggedOption.dataset.panelColumn;
        const source = this.panelLayout[sourceColumn];
        const destination = this.panelLayout[destinationColumn];
        const sourceIndex = source.indexOf(draggedOption.dataset.panelLayout);
        if (sourceIndex === -1) return;
        source.splice(sourceIndex, 1);

        let destinationIndex = destination.length;
        if (targetOption && targetOption !== draggedOption) {
            const targetIndex = destination.indexOf(targetOption.dataset.panelLayout);
            if (targetIndex !== -1) {
                const bounds = targetOption.getBoundingClientRect();
                destinationIndex = targetIndex
                    + (event.clientY >= bounds.top + bounds.height / 2 ? 1 : 0);
            }
        }
        destination.splice(destinationIndex, 0, draggedOption.dataset.panelLayout);
    }

    savePanelLayout() {
        localStorage.setItem('spotifysaver_panel_order', JSON.stringify(this.panelLayout));
    }

    applyPanelLayout(panelOptions) {
        const leftColumn = document.querySelector('.left-column');
        const rightColumn = document.querySelector('.right-column');
        for (const [column, panelIds] of Object.entries(this.panelLayout)) {
            const targetColumn = column === 'left' ? leftColumn : rightColumn;
            panelIds.forEach((panelId, index) => {
                const option = panelOptions.querySelector(
                    `[data-panel-layout="${panelId}"]`
                );
                const panel = document.getElementById(panelId);
                if (!option || !panel) return;
                option.dataset.panelColumn = column;
                option.style.gridColumn = column === 'left' ? '1' : '2';
                option.style.gridRow = String(index + 1);
                targetColumn.appendChild(panel);
            });
        }
    }

    setPanelVisibility(panelId, control, visible) {
        document.getElementById(panelId).classList.toggle(
            'panel-hidden-by-preference',
            !visible
        );
        control.checked = visible;
    }

    async persistOutputDirectoryPreference(kind, directory) {
        const previousWrite =
            this.outputDirectoryPreferenceWrites.get(kind) || Promise.resolve();
        const write = previousWrite
            .catch(() => {})
            .then(() => this.apiClient.saveDownloadDirectoryPreference(kind, directory));
        this.outputDirectoryPreferenceWrites.set(kind, write);
        try {
            await write;
        } finally {
            if (this.outputDirectoryPreferenceWrites.get(kind) === write) {
                this.outputDirectoryPreferenceWrites.delete(kind);
            }
        }
    }

    switchTab(mode) {
        document.querySelectorAll('.tab-button').forEach((tab) => {
            const active = tab.dataset.tab === mode;
            tab.classList.toggle('active', active);
            tab.setAttribute('aria-selected', String(active));
        });
        document.querySelectorAll('.tab-panel').forEach((panel) => {
            panel.classList.toggle('hidden', panel.id !== `tab-${mode}`);
        });
        const downloadUrlGroup = document.getElementById('download-url-group');
        if (mode === 'audio' || mode === 'video') {
            const activePanel = document.getElementById(`tab-${mode}`);
            activePanel.insertBefore(downloadUrlGroup, activePanel.firstElementChild);
            downloadUrlGroup.classList.remove('hidden');
        } else {
            downloadUrlGroup.classList.add('hidden');
        }
        document.getElementById('download-btn').classList.toggle(
            'hidden',
            mode === 'watcher'
        );
        document.getElementById('watcher-form-actions').classList.toggle(
            'hidden',
            mode !== 'watcher'
        );
        localStorage.setItem('spotifysaver_active_tab', mode);
        this.downloadManager.mode = mode;
        this.uiManager.setActiveMode(mode);
    }

    async loadOutputDirectories() {
        for (const kind of Object.keys(UIManager.PICKERS)) {
            const picker = UIManager.PICKERS[kind];
            const preferredPath =
                this.uiManager.preferredOutputDirectories[kind] || '';
            const preferredParent = this.uiManager.getParentPath(preferredPath);
            const savedRoot =
                preferredParent ||
                (preferredPath ? preferredPath : null) ||
                localStorage.getItem(picker.rootKey);
            if (savedRoot) {
                document.getElementById(picker.rootInput).value = savedRoot;
            }

            try {
                const result = await this.apiClient.getOutputDirectories(
                    savedRoot,
                    picker.mediaType || kind
                );
                this.currentRoots[kind] = result.root;
                this.uiManager.setOutputDirectories(result.directories, result.root, kind);
            } catch (error) {
                console.warn(`Could not load ${kind} download folders:`, error);
                this.uiManager.setOutputDirectories([], '', kind);
            }
        }
    }

    async applyMediaRoot(kind = 'audio', path = null) {
        const picker = UIManager.PICKERS[kind];
        const rootInput = document.getElementById(picker.rootInput);
        const rootPath = (path || rootInput.value).trim();
        if (!rootPath) {
            this.uiManager.updateStatus('Enter a base media path', 'error');
            return;
        }

        try {
            const result = await this.apiClient.getOutputDirectories(
                rootPath,
                picker.mediaType || kind
            );
            localStorage.setItem(picker.rootKey, result.root);
            this.currentRoots[kind] = result.root;
            this.uiManager.setOutputDirectories(result.directories, result.root, kind);
            document.getElementById(
                {
                    audio: 'media-root-editor',
                    video: 'video-media-root-editor',
                    watcher: 'watcher-media-root-editor'
                }[kind]
            ).classList.add('hidden');
            this.uiManager.updateStatus('Base media path updated', 'success');
        } catch (error) {
            this.uiManager.updateStatus(error.message, 'error');
        }
    }

    async createFolder(kind) {
        const picker = UIManager.PICKERS[kind];
        const inputId = {
            audio: 'new-folder-input',
            video: 'video-new-folder-input',
            watcher: 'watcher-new-folder-input'
        }[kind];
        const input = document.getElementById(inputId);
        const name = input.value.trim();
        const root = this.currentRoots[kind];
        if (!name) {
            this.uiManager.updateStatus('Enter a folder name', 'error');
            return;
        }
        if (!root) {
            this.uiManager.updateStatus('Define a base media path first', 'error');
            return;
        }

        try {
            const created = await this.apiClient.createOutputDirectory(root, name);
            await this.persistOutputDirectoryPreference(kind, created.path);
            this.uiManager.preferredOutputDirectories[kind] = created.path;
            const result = await this.apiClient.getOutputDirectories(
                root,
                picker.mediaType || kind
            );
            this.uiManager.setOutputDirectories(result.directories, result.root, kind);
            input.value = '';
            document.getElementById(
                {
                    audio: 'add-folder-editor',
                    video: 'video-add-folder-editor',
                    watcher: 'watcher-add-folder-editor'
                }[kind]
            ).classList.add('hidden');
            this.uiManager.updateStatus(`Folder "${created.name}" created`, 'success');
        } catch (error) {
            this.uiManager.updateStatus(error.message, 'error');
        }

    }

    async selectWatcherOutputDirectory(outputDir) {
        const rootPath = this.uiManager.getParentPath(outputDir);
        if (!rootPath) {
            throw new Error('Could not determine the watched channel folder parent');
        }
        const result = await this.apiClient.getOutputDirectories(rootPath, 'video');
        this.currentRoots.watcher = result.root;
        this.uiManager.setOutputDirectories(result.directories, result.root, 'watcher');
        const button = Array.from(
            document.querySelectorAll('#watcher-output-dir-options [data-output-dir]')
        ).find((option) => option.dataset.outputDir === outputDir);
        if (!button) {
            throw new Error('The watched channel folder is no longer available');
        }
        this.uiManager.selectOutputDirectory(button, 'watcher');
    }

    async loadAppVersion() {
        try {
            const version = await this.apiClient.getAppVersion();
            if (version) {
                await this.uiManager.setAppVersion(version);
            }
        } catch (error) {
            console.warn('Could not load app version:', error);
        }
    }
}

// Initialize the application when the page loads
document.addEventListener('DOMContentLoaded', () => {
    new SpotifySaverUI();
});