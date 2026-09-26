class SpotifySaverUI {
    constructor() {
        this.apiClient = new ApiClient();
        this.stateManager = new StateManager();
        this.uiManager = new UIManager(() => this.saveState());
        this.downloadManager = new DownloadManager(this.apiClient, this.uiManager, () => this.saveState());
        
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
            
            // If there was a download in progress, try to reconnect
            if (this.downloadManager.isDownloadInProgress && this.downloadManager.taskId) {
                this.downloadManager.startProgressMonitoring(this.downloadManager.taskId);
            }
            
        } catch (error) {
            console.error('Failed to initialize UI:', error);
            this.uiManager.updateStatus('Failed to initialize. Please refresh the page.', 'error');
        }
    }

    initializeEventListeners() {
        const downloadBtn = document.getElementById('download-btn');
        const stopDownloadBtn = document.getElementById('stop-download-btn');
        const spotifyUrl = document.getElementById('spotify-url');
        const clearLogsBtn = document.getElementById('clear-logs-btn');
        const outputDirOptions = document.getElementById('output-dir-options');
        const setMediaRootBtn = document.getElementById('set-media-root-btn');
        const mediaRootEditor = document.getElementById('media-root-editor');
        const mediaRootInput = document.getElementById('media-root-input');
        const applyMediaRootBtn = document.getElementById('apply-media-root-btn');
        
        downloadBtn.addEventListener('click', () => this.downloadManager.startDownload());
        stopDownloadBtn.addEventListener('click', () => this.downloadManager.stopDownload());
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
        mediaRootInput.addEventListener('keypress', (event) => {
            if (event.key === 'Enter') {
                this.applyMediaRoot();
            }
        });
        
        // Permitir iniciar descarga con Enter
        spotifyUrl.addEventListener('keypress', (e) => {
            if (e.key === 'Enter' && !this.downloadManager.isDownloadInProgress) {
                this.downloadManager.startDownload();
            }
        });

        // Botón para limpiar logs y estado
        clearLogsBtn.addEventListener('click', () => {
            if (confirm('Are you sure you want to clear logs and state? This cannot be undone.')) {
                this.uiManager.clearLog();
                this.uiManager.clearInspect();
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

        // Restaurar únicamente los datos del formulario; nunca reanudar descargas
        // automáticamente al cargar la página.
        this.stateManager.restoreFormData(state);

        if (state.downloadInProgress && state.currentTaskId) {
            this.stateManager.clearPersistedState();
            this.uiManager.updateStatus('Previous download state cleared. Press Start Download to begin a new one.', 'info');
        }
    }

    saveState() {
        const appState = {
            downloadInProgress: this.downloadManager.isDownloadInProgress,
            currentTaskId: this.downloadManager.taskId,
            downloadStartTime: this.downloadManager.startTime
        };
        this.stateManager.saveState(appState);
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

// Inicializar la aplicación cuando se carga la página
document.addEventListener('DOMContentLoaded', () => {
    new SpotifySaverUI();
});