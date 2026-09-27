class UIManager {
    constructor(saveStateCallback = null) {
        this.recentLogs = new Set();
        this.logCooldownTime = 2000; // 2 seconds
        this.saveStateCallback = saveStateCallback;
    }

    updateUI(downloading) {
        const downloadBtn = document.getElementById('download-btn');
        const stopDownloadBtn = document.getElementById('stop-download-btn');
        const progressContainer = document.getElementById('progress-container');
        
        if (downloading) {
            downloadBtn.disabled = true;
            downloadBtn.textContent = '⏳ Downloading...';
            stopDownloadBtn.disabled = false;
            progressContainer.classList.remove('hidden');
        } else {
            downloadBtn.disabled = false;
            downloadBtn.textContent = '🎵 Start Download';
            stopDownloadBtn.disabled = true;
            progressContainer.classList.add('hidden');
            this.updateProgress(0);
        }
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

    updateSingleTrackIcon(trackNumber, state) {
        const container = document.getElementById('inspect-details');
        if (container.classList.contains('hidden')) {
            console.log('Container is hidden, not updating display');
            return;
        }
        
        const trackElement = container.querySelector(`[data-track-number="${trackNumber}"]`);
        if (trackElement) {
            const iconElement = trackElement.querySelector('.track-icon');
            if (iconElement) {
                const newIcon = this.getStateIcon(state);
                iconElement.textContent = newIcon;
                
                // Actualizar clases CSS
                trackElement.className = trackElement.className.replace(/track-state-\w+/g, '');
                trackElement.classList.add(`track-state-${state}`);
                trackElement.setAttribute('data-track-state', state);
                this.updateAlbumProgress(trackElement.closest('.album-details'));
                
                console.log(`🔄 Updated track ${trackNumber} icon to ${newIcon} (state: ${state})`);
            }
        } else {
            console.log(`❌ Could not find track element for track ${trackNumber}`);
        }
    }

    updateAlbumProgress(albumElement) {
        if (!albumElement) return;

        const tracks = Array.from(albumElement.querySelectorAll('[data-track-state]'));
        const completed = tracks.filter((track) => track.dataset.trackState === 'completed').length;
        const downloading = tracks.filter((track) => track.dataset.trackState === 'downloading').length;
        const errors = tracks.filter((track) => track.dataset.trackState === 'error').length;
        const progressText = albumElement.querySelector('.album-progress');
        const progressBar = albumElement.querySelector('.album-progress-bar');
        const status = [`${completed} / ${tracks.length} complete`];

        if (downloading) status.push(`${downloading} downloading`);
        if (errors) status.push(`${errors} failed`);
        progressText.textContent = status.join(' · ');
        progressBar.max = tracks.length || 1;
        progressBar.value = completed;
    }

    renderInspectData(data, trackStates) {
        const container = document.getElementById('inspect-details');
        const message = document.getElementById('inspect-message');
        container.innerHTML = '';
        
        console.log('📝 renderInspectData called, trackStates:', Array.from(trackStates.entries()));
        console.log('🔍 Checking for active intervals/timeouts...');
        
        // Limpiar cualquier timeout/interval que pueda estar corriendo
        for (let i = 1; i < 99999; i++) {
            window.clearTimeout(i);
            window.clearInterval(i);
        }
        console.log('🧹 Cleared all timeouts and intervals');

        if (data.tracks) {
            // Inicializar estados de todas las canciones como 'waiting'
            data.tracks.forEach((t, index) => {
                const trackKey = index + 1;
                if (!trackStates.has(trackKey)) {
                    trackStates.set(trackKey, 'waiting');
                }
            });
            
            const albumDetails = document.createElement('details');
            albumDetails.className = 'album-details';

            const summary = document.createElement('summary');
            summary.className = 'album-summary';

            const albumName = document.createElement('span');
            albumName.className = 'album-name';
            albumName.textContent = `${data.name} (${data.total_tracks} tracks)`;

            const albumProgress = document.createElement('span');
            albumProgress.className = 'album-progress';
            albumProgress.setAttribute('aria-live', 'polite');

            const progressBar = document.createElement('progress');
            progressBar.className = 'album-progress-bar';
            progressBar.setAttribute('aria-label', `${data.name} download progress`);

            summary.append(albumName, albumProgress, progressBar);

            const list = document.createElement('ul');
            list.className = 'track-list';
            list.style.listStyle = 'none';
            list.style.padding = '0';
            
            data.tracks.forEach((t, index) => {
                const trackKey = index + 1;
                const li = document.createElement('li');
                li.style.marginBottom = '8px';
                li.style.padding = '8px';
                li.style.borderRadius = '4px';
                li.style.backgroundColor = 'rgba(255,255,255,0.1)';
                li.style.transition = 'all 0.3s ease';
                
                const trackState = trackStates.get(trackKey) || 'waiting';
                const stateIcon = this.getStateIcon(trackState);
                
                console.log(`🎨 Rendering track ${t.number}: state="${trackState}", icon="${stateIcon}"`);
                
                li.innerHTML = `
                    <span class="track-icon" style="margin-right: 12px; font-size: 20px; display: inline-block; width: 30px; text-align: center; background-color: rgba(255,255,255,0.2); border-radius: 50%; padding: 2px;">${stateIcon}</span>
                    <span class="track-info">${t.number}. ${t.name} — ${t.artists.join(', ')} [${Math.floor(t.duration/60)}:${(t.duration%60).toString().padStart(2,'0')}]</span>
                `;
                
                // Agregar clase CSS para estado
                li.classList.add(`track-state-${trackState}`);
                li.setAttribute('data-track-number', trackKey);
                li.setAttribute('data-track-state', trackState);
                
                // Debug: Verificar qué estado tiene ahora
                console.log(`📝 Track ${t.number} initialized with state: ${trackState}`);
                
                list.appendChild(li);
            });
            albumDetails.append(summary, list);
            container.appendChild(albumDetails);
            this.updateAlbumProgress(albumDetails);
            
        } else if (data.name && data.artists) {
            // Inicializar estado para canción individual
            if (!trackStates.has(1)) {
                trackStates.set(1, 'waiting');
            }
            
            const trackState = trackStates.get(1) || 'waiting';
            const stateIcon = this.getStateIcon(trackState);
            
            container.innerHTML = `
                <div style="margin-bottom: 10px;">
                    <span class="track-icon" style="margin-right: 8px; font-size: 16px;">${stateIcon}</span>
                    <strong>${data.name}</strong> — ${data.artists.join(', ')}
                </div>
                <p>Álbum: ${data.album_name}</p>
                <p>Duración: ${Math.floor(data.duration/60)}:${(data.duration%60).toString().padStart(2,'0')}</p>
            `;
        }

        message.classList.add('hidden');
        container.classList.remove('hidden');
        
        // Guardar estado después de mostrar detalles
        if (this.saveStateCallback) {
            this.saveStateCallback();
        }
    }

    clearInspect() {
        const container = document.getElementById('inspect-details');
        const message = document.getElementById('inspect-message');
        container.innerHTML = '';
        message.textContent = 'Waiting for inspection...';
        message.classList.remove('hidden');
        container.classList.add('hidden');
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