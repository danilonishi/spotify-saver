class UIManager {
    constructor(saveStateCallback = null) {
        this.recentLogs = new Set();
        this.logCooldownTime = 2000; // 2 seconds
        this.saveStateCallback = saveStateCallback;
    }

    updateUI(downloading, paused = false) {
        const downloadBtn = document.getElementById('download-btn');
        const stopDownloadBtn = document.getElementById('stop-download-btn');
        const progressContainer = document.getElementById('progress-container');

        downloadBtn.disabled = false;
        downloadBtn.textContent = downloading ? 'Enqueue Download' : '🎵 Start Download';
        if (downloading) {
            stopDownloadBtn.disabled = paused;
            stopDownloadBtn.textContent = paused ? 'Stopping...' : 'Stop Download';
            progressContainer.classList.remove('hidden');
        } else if (paused) {
            stopDownloadBtn.disabled = false;
            stopDownloadBtn.textContent = 'Resume Download';
            progressContainer.classList.add('hidden');
        } else {
            stopDownloadBtn.disabled = true;
            stopDownloadBtn.textContent = 'Stop Download';
            progressContainer.classList.add('hidden');
            this.updateProgress(0);
        }
    }

    renderDownloadQueue(queue) {
        const list = document.getElementById('download-queue');
        const clearButton = document.getElementById('clear-queue-btn');
        const expandedItems = new Set(
            Array.from(list.querySelectorAll('.queue-item-details[open]'), (item) => item.dataset.queueId)
        );
        list.replaceChildren();

        if (!queue.length) {
            const emptyMessage = document.createElement('li');
            emptyMessage.className = 'queue-empty';
            emptyMessage.textContent = 'Queue is empty';
            list.appendChild(emptyMessage);
        }

        queue.forEach((item) => {
            const entry = document.createElement('li');
            entry.className = `queue-item queue-item-${item.status}`;

            const details = document.createElement('details');
            details.className = 'queue-item-details';
            details.dataset.queueId = String(item.id);
            details.open = expandedItems.has(String(item.id));

            const summary = document.createElement('summary');
            summary.className = 'queue-item-summary';

            const name = document.createElement('span');
            name.className = 'queue-item-name';
            name.textContent = item.title;

            const status = document.createElement('span');
            status.className = 'queue-item-status';
            status.textContent = item.status === 'downloading' && item.progress != null
                ? `Downloading ${Math.round(item.progress)}%`
                : item.status;

            summary.append(name, status);
            details.appendChild(summary);

            const tracks = Array.isArray(item.trackData?.tracks)
                ? item.trackData.tracks
                : item.trackData?.name && item.trackData?.artists
                    ? [item.trackData]
                    : [];
            if (tracks.length) {
                const trackList = document.createElement('ul');
                trackList.className = 'queue-track-list';

                tracks.forEach((track, index) => {
                    const trackNumber = track.number || index + 1;
                    const trackState = item.trackStates?.get(index + 1) || 'waiting';
                    const trackEntry = document.createElement('li');
                    trackEntry.className = `track-state-${trackState}`;
                    trackEntry.dataset.trackNumber = String(index + 1);
                    trackEntry.dataset.trackState = trackState;

                    const icon = document.createElement('span');
                    icon.className = 'track-icon';
                    icon.textContent = this.getStateIcon(trackState);

                    const duration = track.duration || 0;
                    const minutes = Math.floor(duration / 60);
                    const seconds = String(duration % 60).padStart(2, '0');
                    const info = document.createElement('span');
                    info.className = 'queue-track-info';
                    info.textContent = `${trackNumber}. ${track.name} — ${(track.artists || []).join(', ')} [${minutes}:${seconds}]`;

                    trackEntry.append(icon, info);
                    trackList.appendChild(trackEntry);
                });
                details.appendChild(trackList);
            } else {
                const trackMessage = document.createElement('p');
                trackMessage.className = 'queue-track-empty';
                trackMessage.textContent = 'Track details appear when this download starts.';
                details.appendChild(trackMessage);
            }

            entry.appendChild(details);
            list.appendChild(entry);
        });

        clearButton.disabled = !queue.some((item) => item.status !== 'downloading');
    }

    updateQueuedTrackState(queueItemId, trackNumber, state) {
        const details = document.querySelector(`.queue-item-details[data-queue-id="${queueItemId}"]`);
        const track = details?.querySelector(`[data-track-number="${trackNumber}"]`);
        if (!track) return;

        track.className = track.className.replace(/track-state-\w+/g, '');
        track.classList.add(`track-state-${state}`);
        track.dataset.trackState = state;
        track.querySelector('.track-icon').textContent = this.getStateIcon(state);
    }

    updateStatus(message, type = 'info') {
        const statusMessage = document.getElementById('status-message');
        statusMessage.textContent = message;
        statusMessage.className = `status-${type}`;
    }

    updateProgress(percentage) {
        const progressFill = document.getElementById('progress-fill');
        const progressText = document.getElementById('progress-text');
        
        progressFill.style.width = `${percentage}%`;
        progressText.textContent = `${Math.round(percentage)}%`;
    }

    addLogEntry(message, type = 'info') {
        // Prevenir logs duplicados usando un identificador único
        const logId = `${type}:${message}`;
        
        // Verificar si este mensaje ya fue registrado recientemente
        if (this.recentLogs.has(logId)) {
            return; // No añadir logs duplicados
        }
        
        // Añadir al cache de logs recientes con cooldown
        this.recentLogs.add(logId);
        setTimeout(() => {
            this.recentLogs.delete(logId);
        }, this.logCooldownTime);
        
        const logContent = document.getElementById('log-content');
        const timestamp = new Date().toLocaleTimeString();
        const entry = document.createElement('div');
        entry.className = `log-entry ${type}`;
        entry.textContent = `[${timestamp}] ${message}`;
        
        // Insertar al principio para mostrar los más recientes arriba
        logContent.insertBefore(entry, logContent.firstChild);
        logContent.scrollTop = 0;
        
        // Guardar estado después de añadir log
        if (this.saveStateCallback) {
            this.saveStateCallback();
        }
    }

    clearLog() {
        const logContent = document.getElementById('log-content');
        logContent.innerHTML = '';
        
        // Limpiar también el estado de deduplicación
        this.recentLogs.clear();
        
        // Forzar limpieza de caché de estados
        console.log('🧹 Clearing all track states and cache');
    }

    getStateIcon(state) {
        const icons = {
            'waiting': '⏳',      // Reloj de arena - Esperando
            'downloading': '🔄', // Flechas azules - Descargando 
            'completed': '✅',   // Check verde - Completado
            'error': '❌'         // X roja - Error
        };
        const icon = icons[state] || '⏳';
        console.log(`📍 getStateIcon(${state}) -> ${icon}`);
        return icon;
    }

    setOutputDirectories(directories, rootPath) {
        const options = document.getElementById('output-dir-options');
        const outputDirInput = document.getElementById('output-dir');
        const mediaRootInput = document.getElementById('media-root-input');
        options.replaceChildren();
        if (rootPath) {
            mediaRootInput.value = rootPath;
        }

        if (!directories.length) {
            outputDirInput.value = '';
            const message = document.createElement('span');
            message.id = 'output-dir-message';
            message.textContent = 'No folders are available in the configured music directory.';
            options.appendChild(message);
            return;
        }

        const savedPath = localStorage.getItem('spotifysaver_selected_output_dir');
        let selectedButton = null;
        directories.forEach((directory, index) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'output-dir-button';
            button.dataset.outputDir = directory.path;
            button.textContent = directory.name;
            button.setAttribute('aria-pressed', 'false');
            options.appendChild(button);

            if (directory.path === savedPath || (!selectedButton && index === 0)) {
                selectedButton = button;
            }
        });
        this.selectOutputDirectory(selectedButton);
    }

    selectOutputDirectory(button) {
        const options = document.getElementById('output-dir-options');
        const outputDirInput = document.getElementById('output-dir');
        options.querySelectorAll('.output-dir-button').forEach((option) => {
            const isSelected = option === button;
            option.classList.toggle('selected', isSelected);
            option.setAttribute('aria-pressed', String(isSelected));
        });
        outputDirInput.value = button.dataset.outputDir;
        localStorage.setItem('spotifysaver_selected_output_dir', button.dataset.outputDir);
    }

    async setAppVersion(version) {
        const versionElement = document.getElementById('app-version');
        if (versionElement && version) {
            versionElement.textContent = `v${version}`;
        }
    }
}