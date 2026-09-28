class SpotifySaverUI {
    constructor() {
        this.apiClient = new ApiClient();
        this.stateManager = new StateManager();
        this.uiManager = new UIManager(() => this.saveState());
        this.downloadManager = new DownloadManager(this.apiClient, this.uiManager, () => this.saveState());
        this.uiManager.onRemoveQueueItem = (taskId, status) => this.downloadManager.cancelItem(taskId, status);
        
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
        const outputDirOptions = document.getElementById('output-dir-options');
        const setMediaRootBtn = document.getElementById('set-media-root-btn');
        const mediaRootEditor = document.getElementById('media-root-editor');
        const mediaRootInput = document.getElementById('media-root-input');
        const applyMediaRootBtn = document.getElementById('apply-media-root-btn');
        
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
        outputDirOptions.addEventListener('click', (event) => {
            const button = event.target.closest('button[data-output-dir]');
            if (button) {
                this.uiManager.selectOutputDirectory(button);
            }
        });
        setMediaRootBtn.addEventListener('click', () => {
            mediaRootEditor.classList.toggle('hidden');
            if (!mediaRootEditor.classList.contains('hidden')) {
                mediaRootInput.focus();
            }
        });
        applyMediaRootBtn.addEventListener('click', () => this.applyMediaRoot());
        clearQueueBtn.addEventListener('click', () => this.downloadManager.clearQueue());
        clearCompletedBtn.addEventListener('click', () => this.downloadManager.clearCompleted());
        mediaRootInput.addEventListener('keypress', (event) => {
            if (event.key === 'Enter') {
                this.applyMediaRoot();
            }
        });
        
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

    async loadOutputDirectories() {
        const savedRoot = localStorage.getItem('spotifysaver_media_root');
        if (savedRoot) {
            document.getElementById('media-root-input').value = savedRoot;
        }

        try {
            const result = await this.apiClient.getOutputDirectories(savedRoot);
            this.uiManager.setOutputDirectories(result.directories, result.root);
        } catch (error) {
            console.warn('Could not load download folders:', error);
            this.uiManager.setOutputDirectories([], '');
        }
    }

    async applyMediaRoot() {
        const rootInput = document.getElementById('media-root-input');
        const rootPath = rootInput.value.trim();
        if (!rootPath) {
            this.uiManager.updateStatus('Enter a base media path', 'error');
            return;
        }

        try {
            const result = await this.apiClient.getOutputDirectories(rootPath);
            localStorage.setItem('spotifysaver_media_root', result.root);
            this.uiManager.setOutputDirectories(result.directories, result.root);
            document.getElementById('media-root-editor').classList.add('hidden');
            this.uiManager.updateStatus('Base media path updated', 'success');
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