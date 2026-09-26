class DownloadManager {
    constructor(apiClient, uiManager, saveStateCallback = null) {
        this.apiClient = apiClient;
        this.uiManager = uiManager;
        this.saveStateCallback = saveStateCallback;
        this.downloadInProgress = false;
        this.cancellationRequested = false;
        this.currentTaskId = null;
        this.downloadStartTime = null;
        this.lastLoggedTrack = null;
        this.lastLoggedTrackState = null; // Track the state of the last logged track
        this.trackStates = new Map();
        this.currentTrackData = null;
        this.trackUpdateCursor = 0;
    }

    getFormData() {
        const bitrateValue = document.getElementById('bitrate').value;
        const bitrate = bitrateValue === 'best' ? 256 : parseInt(bitrateValue);
        
        return {
            spotify_url: document.getElementById('spotify-url').value,
            output_dir: document.getElementById('output-dir').value,
            output_format: document.getElementById('format').value,
            bit_rate: bitrate,
            download_lyrics: document.getElementById('include-lyrics').checked,
            download_cover: true, // Always download cover
            generate_nfo: document.getElementById('create-nfo').checked,
            overwrite_existing: document.getElementById('overwrite-existing').checked
        };
    }

    validateForm() {
        const formData = this.getFormData();
        
        if (!formData.spotify_url) {
            this.uiManager.updateStatus('Please enter a valid Spotify URL', 'error');
            return false;
        }
        
        if (!formData.spotify_url.includes('spotify.com') &&
            !this.isYouTubeCollectionUrl(formData.spotify_url) &&
            !this.isYouTubeTrackUrl(formData.spotify_url)) {
            this.uiManager.updateStatus(
                'Enter a Spotify link or a YouTube track, album, or playlist link.',
                'error'
            );
            return false;
        }
        
        return true;
    }

    isYouTubeCollectionUrl(url) {
        try {
            const parsedUrl = new URL(url);
            const hosts = new Set([
                'music.youtube.com',
                'youtube.com',
                'www.youtube.com',
                'm.youtube.com'
            ]);
            return hosts.has(parsedUrl.hostname.toLowerCase()) &&
                ['/playlist', '/watch'].includes(parsedUrl.pathname.replace(/\/$/, '')) &&
                Boolean(parsedUrl.searchParams.get('list'));
        } catch (_error) {
            return false;
        }
    }

    isYouTubeTrackUrl(url) {
        try {
            const parsedUrl = new URL(url);
            const hosts = new Set([
                'music.youtube.com',
                'youtube.com',
                'www.youtube.com',
                'm.youtube.com',
                'youtu.be'
            ]);
            if (!hosts.has(parsedUrl.hostname.toLowerCase())) {
                return false;
            }
            if (parsedUrl.hostname.toLowerCase() === 'youtu.be') {
                return Boolean(parsedUrl.pathname.replace(/^\//, ''));
            }
            if (parsedUrl.pathname.replace(/\/$/, '') === '/watch') {
                return Boolean(parsedUrl.searchParams.get('v'));
            }
            return /^\/(shorts|embed)\/[^/]+/.test(parsedUrl.pathname);
        } catch (_error) {
            return false;
        }
    }

    async startDownload() {
        if (this.downloadInProgress) {
            return;
        }

        if (!this.validateForm()) {
            return;
        }

        const formData = this.getFormData();

        // Check API connectivity before starting
        this.uiManager.updateStatus('Checking API connection...', 'info');
        const apiAvailable = await this.apiClient.checkApiStatusWithRetry();
        
        if (!apiAvailable) {
            this.uiManager.updateStatus('Cannot connect to API. Please check if the server is running.', 'error');
            return;
        }

        this.downloadInProgress = true;
        this.cancellationRequested = false;
        this.uiManager.updateUI(true);
        this.uiManager.clearLog();
        this.uiManager.clearInspect();
        
        // Resetear estado de logging para nueva descarga
        this.lastLoggedTrack = null;
        this.lastLoggedTrackState = null;
        this.trackStates.clear();
        this.trackUpdateCursor = 0;

        try {
            const isYouTubeCollection = this.isYouTubeCollectionUrl(formData.spotify_url);
            const isYouTubeTrack = this.isYouTubeTrackUrl(formData.spotify_url);
            if (isYouTubeCollection || isYouTubeTrack) {
                this.currentTrackData = null;
                this.uiManager.updateStatus(
                    isYouTubeCollection
                        ? 'YouTube collection detected. Track metadata will come from YouTube.'
                        : 'YouTube track detected. Audio will be downloaded in the selected format.',
                    'info'
                );
                if (formData.download_lyrics || formData.generate_nfo) {
                    this.uiManager.addLogEntry(
                        'YouTube downloads use source metadata; lyrics and NFO options apply only to Spotify.',
                        'info'
                    );
                }
            } else {
                this.uiManager.updateStatus('Inspecting Spotify URL...', 'info');
                const inspectData = await this.apiClient.inspectSpotifyUrl(formData.spotify_url);
                this.uiManager.renderInspectData(inspectData, this.trackStates);
                this.currentTrackData = inspectData;

                await new Promise(resolve => setTimeout(resolve, 1500));
            }

            if (this.cancellationRequested) {
                this.handleDownloadCancelled();
                return;
            }

            // Paso 2: iniciar descarga
            this.uiManager.updateStatus('Starting download...', 'info');
            this.uiManager.addLogEntry('Sending download request...', 'info');

            const result = await this.apiClient.startDownload(formData);

            if (result.task_id) {
                this.currentTaskId = result.task_id;
                this.downloadStartTime = Date.now();
                this.uiManager.addLogEntry(`Download started with ID: ${result.task_id}`, 'success');
                if (this.saveStateCallback) {
                    this.saveStateCallback();
                }
                this.startProgressMonitoring(result.task_id);
                if (this.cancellationRequested) {
                    this.requestTaskCancellation(result.task_id);
                }
            } else {
                this.uiManager.updateStatus('Download completed successfully', 'success');
                this.uiManager.addLogEntry('Download complete', 'success');
                this.downloadInProgress = false;
                this.cancellationRequested = false;
                this.uiManager.updateUI(false);
            }

        } catch (error) {
            this.uiManager.updateStatus(`Error: ${error.message}`, 'error');
            this.uiManager.addLogEntry(`Error: ${error.message}`, 'error');
            this.downloadInProgress = false;
            this.cancellationRequested = false;
            this.currentTaskId = null;
            this.uiManager.updateUI(false);
        }
    }

    async stopDownload() {
        if (!this.downloadInProgress || this.cancellationRequested) {
            return;
        }

        this.cancellationRequested = true;
        this.uiManager.updateStatus('Stopping download...', 'info');
        if (this.currentTaskId) {
            await this.requestTaskCancellation(this.currentTaskId);
        }
    }

    async requestTaskCancellation(taskId) {
        try {
            await this.apiClient.cancelDownload(taskId);
        } catch (error) {
            this.cancellationRequested = false;
            this.uiManager.updateStatus(`Could not stop download: ${error.message}`, 'error');
            this.uiManager.addLogEntry(`Could not stop download: ${error.message}`, 'error');
        }
    }

    startProgressMonitoring(taskId) {
        // Monitorear progreso usando polling
        const pollInterval = 2000; // 2 segundos
        
        const checkProgress = async () => {
            try {
                const status = await this.apiClient.getDownloadStatus(taskId);
                if (status) {
                    console.log('📡 API Status received:', status);
                    
                    if (status.status === 'completed') {
                        this.handleDownloadCompleted(status);
                        return;
                    } else if (status.status === 'cancelled') {
                        this.handleDownloadCancelled(status);
                        return;
                    } else if (status.status === 'failed') {
                        this.handleDownloadFailed(
                            status.error_message || 'Download failed',
                            status.current_track_number,
                            status.failed_track_names || []
                        );
                        return;
                    } else if (status.status === 'cancelling') {
                        this.uiManager.updateStatus('Stopping download...', 'info');
                    } else if (status.status === 'processing') {
                        this.handleDownloadProgress(status);
                    }
                    
                    // Continuar monitoreando
                    setTimeout(checkProgress, pollInterval);
                } else {
                    // Si no hay endpoint de estado, usar simulación
                    this.simulateProgress();
                }
            } catch (error) {
                console.warn('Error checking progress, using simulation:', error);
                this.simulateProgress();
            }
        };
        
        // Iniciar monitoreo
        checkProgress();
    }

    handleDownloadCompleted(status = {}) {
        const completedTracks = status.completed_tracks || 0;
        const totalTracks = status.total_tracks || 0;
        const failedTrackNames = status.failed_track_names || [];
        const summary = totalTracks
            ? `Downloaded ${completedTracks}/${totalTracks} tracks`
            : 'Download completed successfully';

        this.uiManager.updateProgress(100);
        this.uiManager.updateStatus(
            failedTrackNames.length ? `${summary}. ${failedTrackNames.length} failed.` : summary,
            failedTrackNames.length ? 'error' : 'success'
        );
        this.uiManager.addLogEntry(summary, failedTrackNames.length ? 'error' : 'success');
        failedTrackNames.forEach(trackName => {
            this.uiManager.addLogEntry(`Failed: ${trackName}`, 'error');
        });
        
        // Marcar todas las canciones como completadas
        if (this.currentTrackData && this.currentTrackData.tracks) {
            this.currentTrackData.tracks.forEach((track, index) => {
                const trackKey = index + 1;
                if (failedTrackNames.includes(track.name)) {
                    this.updateTrackState(trackKey, 'error');
                } else {
                    this.updateTrackState(trackKey, 'completed');
                }
            });
        } else {
            // Fallback: marcar por índice
            this.trackStates.forEach((state, trackNumber) => {
                if (state !== 'error') {
                    this.updateTrackState(trackNumber, 'completed');
                }
            });
        }
        
        this.downloadInProgress = false;
        this.cancellationRequested = false;
        this.currentTaskId = null;
        this.uiManager.updateUI(false);
        if (this.saveStateCallback) {
            this.saveStateCallback();
        }
    }

    handleDownloadFailed(message, currentTrackNumber, failedTrackNames = []) {
        this.uiManager.updateStatus(`Error: ${message}`, 'error');
        this.uiManager.addLogEntry(`Error: ${message}`, 'error');

        failedTrackNames.forEach(trackName => {
            this.uiManager.addLogEntry(`Failed: ${trackName}`, 'error');
        });

        if (this.currentTrackData && this.currentTrackData.tracks) {
            this.currentTrackData.tracks.forEach((track, index) => {
                const trackKey = index + 1;
                if (failedTrackNames.includes(track.name)) {
                    this.updateTrackState(trackKey, 'error');
                } else {
                    this.updateTrackState(trackKey, 'completed');
                }
            });
        }
        
        // Marcar canción actual como error si está especificada
        if (currentTrackNumber) {
            this.updateTrackState(currentTrackNumber, 'error');
        }
        
        this.downloadInProgress = false;
        this.cancellationRequested = false;
        this.currentTaskId = null;
        this.uiManager.updateUI(false);
        if (this.saveStateCallback) {
            this.saveStateCallback();
        }
    }

    handleDownloadCancelled(status = {}) {
        const completedTracks = status.completed_tracks || 0;
        const message = completedTracks
            ? `Download stopped after ${completedTracks} track${completedTracks === 1 ? '' : 's'}.`
            : 'Download stopped.';
        this.uiManager.updateStatus(message, 'info');
        this.uiManager.addLogEntry(message, 'info');
        this.downloadInProgress = false;
        this.cancellationRequested = false;
        this.currentTaskId = null;
        this.uiManager.updateUI(false);
        if (this.saveStateCallback) {
            this.saveStateCallback();
        }
    }

    handleDownloadProgress(status) {
        const currentProgress = status.progress || 0;
        this.uiManager.updateProgress(currentProgress);
        this.uiManager.updateStatus(`Downloading... ${Math.round(currentProgress)}%`, 'info');

        // Consume every result event so fast failures cannot be overwritten by
        // the next track before the browser polls again.
        const trackUpdates = status.track_updates || [];
        trackUpdates.slice(this.trackUpdateCursor).forEach(update => {
            const resultState = update.status === 'error'
                ? 'error'
                : update.status === 'completed'
                    ? 'completed'
                    : null;
            if (resultState && update.track_number) {
                this.updateTrackState(update.track_number, resultState);
                if (resultState === 'error' && update.track_name) {
                    this.uiManager.addLogEntry(`Failed: ${update.track_name}`, 'error');
                }
            }
        });
        this.trackUpdateCursor = trackUpdates.length;
        
        // Actualizar estado de canción actual
        if (status.current_track && this.currentTrackData) {
            // Album numbers repeat in playlists; use the playlist position from the API.
            const currentTrackNumber = status.current_track_number ||
                this.findTrackNumberByName(status.current_track);
            
            if (currentTrackNumber) {
                console.log(`🟡 Real download: Track ${currentTrackNumber} (${status.current_track}) is downloading`);
                
                // Marcar canción actual como descargando
                if (status.current_track_status !== 'error' &&
                    status.current_track_status !== 'completed') {
                    this.updateTrackState(currentTrackNumber, 'downloading');
                }
                
                // Marcar canciones anteriores como completadas
                for (let i = 1; i < currentTrackNumber; i++) {
                    if (this.trackStates.has(i) && this.trackStates.get(i) !== 'error') {
                        this.updateTrackState(i, 'completed');
                    }
                }
            } else {
                console.warn(`⚠️ Could not find track number for: ${status.current_track}`);
            }
        } else if (status.current_track_number) {
            // Fallback: usar current_track_number si está disponible
            this.updateTrackState(status.current_track_number, 'downloading');
            
            for (let i = 1; i < status.current_track_number; i++) {
                if (this.trackStates.has(i) && this.trackStates.get(i) !== 'error') {
                    this.updateTrackState(i, 'completed');
                }
            }
        }
        // Registrar estado de la canción actual
        this.logTrackStatus(status);
    }
    
    logTrackStatus(status) {
        // Verificar si la última canción registrada cambió a completed o error
        if (this.lastLoggedTrack && this.lastLoggedTrackState) {
            const lastTrackNumber = this.findTrackNumberByName(this.lastLoggedTrack);
            if (lastTrackNumber) {
                const currentLastTrackState = this.trackStates.get(lastTrackNumber);
                if (currentLastTrackState !== this.lastLoggedTrackState && 
                    (currentLastTrackState === 'completed' || currentLastTrackState === 'error')) {
                    const statusMessage = currentLastTrackState === 'completed' ? 'Completed' : 'Failed';
                    this.uiManager.addLogEntry(`${statusMessage}: ${this.lastLoggedTrack}`, 
                        currentLastTrackState === 'completed' ? 'success' : 'error');
                    this.lastLoggedTrackState = currentLastTrackState;
                }
            }
        }
        
        // Solo registrar la nueva canción si es diferente a la última registrada
        if (status.current_track && status.current_track !== this.lastLoggedTrack) {
            this.uiManager.addLogEntry(`Downloading: ${status.current_track}`, 'info');
            this.lastLoggedTrack = status.current_track;
            // Actualizar el estado inicial de la nueva canción registrada
            const currentTrackNumber = this.findTrackNumberByName(status.current_track);
            if (currentTrackNumber) {
                this.lastLoggedTrackState = this.trackStates.get(currentTrackNumber) || 'downloading';
            }
        }
    }

    simulateProgress() {
        // Simulación de progreso para compatibilidad
        let progress = 0;
        let lastMessageIndex = -1;
        let simulatedTrackNumber = 1;
        const totalTracks = this.trackStates.size || 1;
        
        console.log('🎭 Starting simulation with real track data');
        
        const interval = setInterval(() => {
            if (this.cancellationRequested) {
                clearInterval(interval);
                this.handleDownloadCancelled();
                return;
            }

            progress += Math.random() * 8 + 2; // Progreso más consistente
            
            // Simular progreso por canción basado en datos reales
            const currentTrackByProgress = Math.ceil((progress / 100) * totalTracks);
            if (currentTrackByProgress > simulatedTrackNumber && simulatedTrackNumber <= totalTracks) {
                // Marcar canción anterior como completada
                if (simulatedTrackNumber > 1) {
                    this.updateTrackState(simulatedTrackNumber - 1, 'completed');
                }
                simulatedTrackNumber = currentTrackByProgress;
                
                // Simular log con nombre real de canción si está disponible
                if (this.currentTrackData && this.currentTrackData.tracks && this.currentTrackData.tracks[simulatedTrackNumber - 1]) {
                    const track = this.currentTrackData.tracks[simulatedTrackNumber - 1];
                    this.uiManager.addLogEntry(`Downloading: ${track.name}`, 'info');
                }
            }
            
            // Actualizar canción actual como descargando
            if (simulatedTrackNumber <= totalTracks && progress < 100) {
                this.updateTrackState(simulatedTrackNumber, 'downloading');
            }
            
            if (progress >= 100) {
                progress = 100;
                this.uiManager.updateProgress(progress);
                this.uiManager.updateStatus('Download completed successfully', 'success');
                this.uiManager.addLogEntry('Download complete', 'success');
                
                // Marcar todas las canciones como completadas
                this.trackStates.forEach((state, trackNumber) => {
                    if (state !== 'error') {
                        this.updateTrackState(trackNumber, 'completed');
                    }
                });
                
                this.downloadInProgress = false;
                this.uiManager.updateUI(false);
                clearInterval(interval);
            } else {
                this.uiManager.updateProgress(progress);
                this.uiManager.updateStatus(`Downloading... ${Math.round(progress)}%`, 'info');
                
                // Simular mensajes de progreso, evitando repetir el último mensaje
                if (Math.random() > 0.8) { // Reducir frecuencia de mensajes
                    const messages = [
                        'Searching for tracks...',
                        'Downloading track...',
                        'Setting metadata...',
                        'Generating thumbnail...',
                        'Saving file...',
                    ];
                    
                    let messageIndex;
                    do {
                        messageIndex = Math.floor(Math.random() * messages.length);
                    } while (messageIndex === lastMessageIndex && messages.length > 1);
                    
                    lastMessageIndex = messageIndex;
                    this.uiManager.addLogEntry(messages[messageIndex], 'info');
                }
            }
        }, 1000);
    }

    findTrackNumberByName(trackName) {
        if (!this.currentTrackData || !this.currentTrackData.tracks) {
            return null;
        }
        
        // Limpiar el nombre de la canción para comparación
        const cleanTrackName = trackName.toLowerCase().trim();
        
        // Buscar la canción por nombre
        for (const [index, track] of this.currentTrackData.tracks.entries()) {
            const cleanCurrentName = track.name.toLowerCase().trim();
            if (cleanCurrentName === cleanTrackName || cleanCurrentName.includes(cleanTrackName) || cleanTrackName.includes(cleanCurrentName)) {
                const trackKey = index + 1;
                console.log(`🎯 Found match: "${trackName}" -> Track ${trackKey}`);
                return trackKey;
            }
        }
        
        console.warn(`🔍 No match found for track: "${trackName}"`);
        console.log('Available tracks:', this.currentTrackData.tracks.map((t, index) => `${index + 1}: ${t.name}`));
        return null;
    }

    updateTrackState(trackNumber, state) {
        this.trackStates.set(trackNumber, state);        
        // Actualizar UI
        this.uiManager.updateSingleTrackIcon(trackNumber, state);
    }

    clearStates() {
        this.trackStates.clear();
        this.currentTrackData = null;
        this.lastLoggedTrack = null;
        this.lastLoggedTrackState = null;
    }

    // Getters for state access
    get isDownloadInProgress() {
        return this.downloadInProgress;
    }

    get taskId() {
        return this.currentTaskId;
    }

    get startTime() {
        return this.downloadStartTime;
    }

    // Methods to restore state
    restoreDownloadState(downloadInProgress, taskId, startTime) {
        this.downloadInProgress = downloadInProgress;
        this.currentTaskId = taskId;
        this.downloadStartTime = startTime;
        
        if (downloadInProgress && taskId) {
            this.startProgressMonitoring(taskId);
        }
    }
}