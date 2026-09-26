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
        
        downloadBtn.addEventListener('click', () => this.downloadManager.startDownload());
        stopDownloadBtn.addEventListener('click', () => this.downloadManager.stopDownload());
        outputDirOptions.addEventListener('click', (event) => {
            const button = event.target.closest('button[data-output-dir]');
            if (button) {
                this.uiManager.selectOutputDirectory(button);
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
        try {
            const directories = await this.apiClient.getOutputDirectories();
            this.uiManager.setOutputDirectories(directories);
        } catch (error) {
            console.warn('Could not load download folders:', error);
            this.uiManager.setOutputDirectories([]);
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