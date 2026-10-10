class SpotifySaverUI {
    constructor() {
        this.apiClient = new ApiClient();
        this.stateManager = new StateManager();
        this.uiManager = new UIManager(() => this.saveState());
        this.downloadManager = new DownloadManager(this.apiClient, this.uiManager, () => this.saveState());
        this.uiManager.onRemoveQueueItem = (taskId, status) => this.downloadManager.cancelItem(taskId, status);
        
        this.currentRoots = {};
        this.isInitialized = false;
        this.retryCount = 0;

        this.initialize();
    }

    async initialize() {
        try {
            this.initializeEventListeners();
            this.loadPersistedState();
            
            // Check API status with retry mechanism
            const apiAvailable = await this.apiClient.checkApiStatusWithRetry();
            
            if (apiAvailable) {
                this.uiManager.updateStatus('API connected and ready', 'success');
                await this.loadOutputDirectories();
                await this.loadAppVersion();
            } else {
                this.uiManager.updateStatus('API not available. Make sure it is running.', 'error');
            }
            
            this.isInitialized = true;

            // The server owns the download queue; pull its current state so
            // downloads that kept running while the page was closed show up.
            await this.downloadManager.hydrateFromServer();

        } catch (error) {
            console.error('Failed to initialize UI:', error);
            this.uiManager.updateStatus('Failed to initialize. Please refresh the page.', 'error');
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
        
        downloadBtn.addEventListener('click', () => this.downloadManager.startDownload());
        stopDownloadBtn.addEventListener('click', () => this.downloadManager.stopDownload());
        clearUrlBtn.addEventListener('click', () => {
            spotifyUrl.value = '';
            this.saveState();
            spotifyUrl.focus();
        });
        const setDarkMode = (enabled) => {
            document.body.classList.toggle('dark-mode', enabled);
            themeToggleBtn.setAttribute('aria-pressed', String(enabled));
            themeToggleBtn.textContent = enabled ? '☀️ Light mode' : '🌙 Dark mode';
            localStorage.setItem('spotifysaver_theme', enabled ? 'dark' : 'light');
        };
        setDarkMode(localStorage.getItem('spotifysaver_theme') === 'dark');
        themeToggleBtn.addEventListener('click', () => {
            setDarkMode(!document.body.classList.contains('dark-mode'));
        });
        const pickerIds = {
            audio: { editor: 'media-root-editor', trigger: 'set-media-root-btn', apply: 'apply-media-root-btn', addTrigger: 'add-folder-btn', addEditor: 'add-folder-editor', addInput: 'new-folder-input', create: 'create-folder-btn' },
            video: { editor: 'video-media-root-editor', trigger: 'video-set-media-root-btn', apply: 'video-apply-media-root-btn', addTrigger: 'video-add-folder-btn', addEditor: 'video-add-folder-editor', addInput: 'video-new-folder-input', create: 'video-create-folder-btn' }
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
            tab.addEventListener('click', () => this.switchTab(tab.dataset.tab));
        });
        this.switchTab(localStorage.getItem('spotifysaver_active_tab') === 'video' ? 'video' : 'audio');

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
        document.querySelectorAll('#tab-video select').forEach((select) => {
            const key = `spotifysaver_select_${select.id}`;
            const saved = localStorage.getItem(key);
            if (saved !== null && Array.from(select.options).some((option) => option.value === saved)) {
                select.value = saved;
            }
            select.addEventListener('change', () => localStorage.setItem(key, select.value));
        });

        clearQueueBtn.addEventListener('click', () => this.downloadManager.clearQueue());
        clearCompletedBtn.addEventListener('click', () => this.downloadManager.clearCompleted());
        
        // Allow starting a download with Enter
        spotifyUrl.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                this.downloadManager.startDownload();
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

    switchTab(mode) {
        document.querySelectorAll('.tab-button').forEach((tab) => {
            const active = tab.dataset.tab === mode;
            tab.classList.toggle('active', active);
            tab.setAttribute('aria-selected', String(active));
        });
        document.querySelectorAll('.tab-panel').forEach((panel) => {
            panel.classList.toggle('hidden', panel.id !== `tab-${mode}`);
        });
        localStorage.setItem('spotifysaver_active_tab', mode);
        this.downloadManager.mode = mode;
        this.uiManager.setActiveMode(mode);
    }

    async loadOutputDirectories() {
        for (const kind of Object.keys(UIManager.PICKERS)) {
            const picker = UIManager.PICKERS[kind];
            const savedRoot = localStorage.getItem(picker.rootKey);
            if (savedRoot) {
                document.getElementById(picker.rootInput).value = savedRoot;
            }

            try {
                const result = await this.apiClient.getOutputDirectories(savedRoot, kind);
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
            const result = await this.apiClient.getOutputDirectories(rootPath, kind);
            localStorage.setItem(picker.rootKey, result.root);
            this.currentRoots[kind] = result.root;
            this.uiManager.setOutputDirectories(result.directories, result.root, kind);
            document.getElementById(
                kind === 'video' ? 'video-media-root-editor' : 'media-root-editor'
            ).classList.add('hidden');
            this.uiManager.updateStatus('Base media path updated', 'success');
        } catch (error) {
            this.uiManager.updateStatus(error.message, 'error');
        }
    }

    async createFolder(kind) {
        const picker = UIManager.PICKERS[kind];
        const input = document.getElementById(kind === 'video' ? 'video-new-folder-input' : 'new-folder-input');
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
            localStorage.setItem(picker.selectedKey, created.path);
            const result = await this.apiClient.getOutputDirectories(root, kind);
            this.uiManager.setOutputDirectories(result.directories, result.root, kind);
            input.value = '';
            document.getElementById(
                kind === 'video' ? 'video-add-folder-editor' : 'add-folder-editor'
            ).classList.add('hidden');
            this.uiManager.updateStatus(`Folder "${created.name}" created`, 'success');
        } catch (error) {
            this.uiManager.updateStatus(error.message, 'error');
        }
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