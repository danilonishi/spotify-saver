class DownloadManager {
    constructor(apiClient, uiManager, saveStateCallback = null) {
        this.apiClient = apiClient;
        this.uiManager = uiManager;
        this.saveStateCallback = saveStateCallback;
        // The queue lives on the server; this array only mirrors it for
        // rendering, so downloads survive the page being closed.
        this.queue = [];
        this.localIdCounter = 0;
        this.queuePaused = false;
        this.pollTimer = null;
        this.pollInterval = 2000;
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

        if (!formData.output_dir) {
            this.uiManager.updateStatus('Choose a download folder', 'error');
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

    getQueueKey(url) {
        try {
            const parsedUrl = new URL(url);
            const hostname = parsedUrl.hostname.toLowerCase().replace(/^www\./, '');
            const spotifyMatch = parsedUrl.pathname.match(/\/(album|playlist|track)\/([^/]+)/i);
            if (hostname.endsWith('spotify.com') && spotifyMatch) {
                return `spotify:${spotifyMatch[1].toLowerCase()}:${spotifyMatch[2]}`;
            }

            const playlistId = parsedUrl.searchParams.get('list');
            if (playlistId) {
                return `youtube:playlist:${playlistId}`;
            }

            const videoId = parsedUrl.searchParams.get('v') ||
                (hostname === 'youtu.be' ? parsedUrl.pathname.replace(/^\//, '') : null);
            if (videoId) {
                return `youtube:track:${videoId}`;
            }

            for (const parameter of Array.from(parsedUrl.searchParams.keys())) {
                if (parameter === 'si' || parameter === 'feature' || parameter.startsWith('utm_')) {
                    parsedUrl.searchParams.delete(parameter);
                }
            }
            parsedUrl.searchParams.sort();
            return `${hostname}${parsedUrl.pathname.replace(/\/$/, '')}?${parsedUrl.searchParams}`;
        } catch (_error) {
            return url.trim();
        }
    }

    getQueueTitle(url) {
        try {
            const parsedUrl = new URL(url);
            const spotifyMatch = parsedUrl.pathname.match(/\/(album|playlist|track)\/([^/]+)/i);
            if (spotifyMatch) {
                return `${spotifyMatch[1][0].toUpperCase()}${spotifyMatch[1].slice(1)} ${spotifyMatch[2]}`;
            }
            const playlistId = parsedUrl.searchParams.get('list');
            if (playlistId) {
                return `YouTube collection ${playlistId}`;
            }
            return `YouTube track ${parsedUrl.searchParams.get('v') || parsedUrl.pathname.replace(/^\//, '')}`;
        } catch (_error) {
            return url;
        }
    }

    loadQueueItemDetails(queueItem) {
        if (this.isYouTubeCollectionUrl(queueItem.url) || this.isYouTubeTrackUrl(queueItem.url)) {
            return;
        }

        queueItem.inspectPromise = this.apiClient.inspectSpotifyUrl(queueItem.url);
        queueItem.inspectPromise.then((data) => {
            queueItem.trackData = data;
            if (data.name) queueItem.title = data.name;
            this.uiManager.renderDownloadQueue(this.queue);
        }, (error) => {
            queueItem.inspectionError = error.message;
            this.uiManager.renderDownloadQueue(this.queue);
        });
    }

    // The server owns the queue: adding an item just hands it a download
    // request, and the server keeps running it even if this page is closed.
    startDownload() {
        if (!this.validateForm()) {
            return;
        }

        const formData = this.getFormData();
        const queueKey = this.getQueueKey(formData.spotify_url);
        const duplicate = this.queue.find((item) => item.key === queueKey &&
            ['queued', 'downloading', 'paused'].includes(item.status));
        if (duplicate) {
            this.uiManager.updateStatus(`${duplicate.title} is already in the queue.`, 'info');
            return;
        }

        const queueItem = {
            id: `local-${++this.localIdCounter}`,
            key: queueKey,
            url: formData.spotify_url,
            title: this.getQueueTitle(formData.spotify_url),
            formData,
            status: 'queued',
            progress: 0,
            trackData: null,
            trackStates: new Map(),
            trackUpdateCursor: 0,
            lastLoggedTrack: null,
            lastLoggedTrackState: null,
            loggedTerminal: false
        };
        this.queue.push(queueItem);
        this.uiManager.renderDownloadQueue(this.queue);
        this.uiManager.addLogEntry(`${queueItem.title} added to queue`, 'info');
        this.loadQueueItemDetails(queueItem);
        this.submitToServer(queueItem);
        this.ensurePolling();
    }

    async submitToServer(queueItem) {
        try {
            const result = await this.apiClient.startDownload(queueItem.formData);
            queueItem.id = result.task_id;
            queueItem.status = 'queued';
            this.uiManager.addLogEntry(`Download queued with ID: ${result.task_id}`, 'success');
            this.uiManager.renderDownloadQueue(this.queue);
        } catch (error) {
            queueItem.status = 'failed';
            queueItem.error_message = error.message;
            this.uiManager.updateStatus(`Error: ${error.message}`, 'error');
            this.uiManager.addLogEntry(`Error: ${error.message}`, 'error');
            this.uiManager.renderDownloadQueue(this.queue);
        }
    }

    ensurePolling() {
        if (this.pollTimer) return;
        this.pollOnce();
        this.pollTimer = setInterval(() => this.pollOnce(), this.pollInterval);
    }

    stopPolling() {
        if (!this.pollTimer) return;
        clearInterval(this.pollTimer);
        this.pollTimer = null;
    }

    async pollOnce() {
        try {
            const data = await this.apiClient.getQueue();
            this.queuePaused = Boolean(data.paused);
            this.reconcileQueue(data.tasks || []);
        } catch (error) {
            console.warn('Error refreshing download queue:', error);
        }
    }

    mapServerStatus(task) {
        switch (task.status) {
            case 'processing':
            case 'cancelling':
                return 'downloading';
            case 'queued':
                return this.queuePaused && task.error_message === 'Paused by user' ? 'paused' : 'queued';
            case 'cancelled':
                return 'failed';
            default:
                return task.status; // completed, failed
        }
    }

    // Rebuilds the local queue from the server's queue snapshot, keeping any
    // client-only fields (track metadata, log dedupe state) tied to each id.
    reconcileQueue(serverTasks) {
        const existingById = new Map(this.queue.map((item) => [item.id, item]));
        const localOnly = this.queue.filter((item) => String(item.id).startsWith('local-'));

        const merged = serverTasks.map((task) => {
            const existing = existingById.get(task.task_id);
            const item = existing || {
                id: task.task_id,
                key: this.getQueueKey(task.spotify_url || ''),
                url: task.spotify_url || '',
                title: this.getQueueTitle(task.spotify_url || ''),
                formData: {
                    spotify_url: task.spotify_url,
                    output_dir: task.output_dir,
                    output_format: task.output_format,
                    bit_rate: task.bit_rate
                },
                trackData: null,
                trackStates: new Map(),
                trackUpdateCursor: 0,
                lastLoggedTrack: null,
                lastLoggedTrackState: null,
                loggedTerminal: false
            };
            this.applyTaskToItem(item, task);
            return item;
        });

        // Keep locally created items the server hasn't acknowledged yet.
        this.queue = merged.concat(localOnly.filter((item) => !item.error_message));
        this.uiManager.renderDownloadQueue(this.queue);
        this.updateGlobalStatus();
    }

    applyTaskToItem(item, task) {
        item.progress = task.progress || 0;
        item.error_message = task.error_message;
        item.failed_track_names = task.failed_track_names || [];
        item.output_directory = task.output_directory;
        item.queue_position = task.queue_position;
        if (task.spotify_url) item.url = task.spotify_url;
        // Prefer the server-resolved display name (e.g. real album title)
        // over the URL-derived placeholder used before it's known.
        if (task.title) item.title = task.title;
        // The server resolves the full track listing once, so every client
        // (not just the one that started the download) can render it.
        if (!item.trackData && Array.isArray(task.tracks) && task.tracks.length) {
            item.trackData = { tracks: task.tracks };
        }
        item.status = this.mapServerStatus(task);

        // Consume every result event so fast failures cannot be overwritten
        // by the next track before the browser polls again.
        const trackUpdates = task.track_updates || [];
        trackUpdates.slice(item.trackUpdateCursor).forEach((update) => {
            const resultState = update.status === 'error'
                ? 'error'
                : update.status === 'completed'
                    ? 'completed'
                    : null;
            if (resultState && update.track_number) {
                this.updateTrackState(item, update.track_number, resultState);
                if (resultState === 'error' && update.track_name) {
                    this.uiManager.addLogEntry(`Failed: ${update.track_name}`, 'error');
                }
            }
        });
        item.trackUpdateCursor = trackUpdates.length;

        if (task.status === 'processing' && task.current_track_number) {
            if (task.current_track_status !== 'error' && task.current_track_status !== 'completed') {
                this.updateTrackState(item, task.current_track_number, 'downloading');
            }
            for (let i = 1; i < task.current_track_number; i += 1) {
                if (item.trackStates.has(i) && item.trackStates.get(i) !== 'error') {
                    this.updateTrackState(item, i, 'completed');
                }
            }
        }

        this.logTrackStatus(item, task);
        this.logTerminalStateOnce(item, task);
    }

    logTrackStatus(item, task) {
        if (item.lastLoggedTrack && item.lastLoggedTrackState) {
            const lastTrackNumber = this.findTrackNumberByName(item, item.lastLoggedTrack);
            if (lastTrackNumber) {
                const currentState = item.trackStates.get(lastTrackNumber);
                if (currentState !== item.lastLoggedTrackState &&
                    (currentState === 'completed' || currentState === 'error')) {
                    const message = currentState === 'completed' ? 'Completed' : 'Failed';
                    this.uiManager.addLogEntry(`${message}: ${item.lastLoggedTrack}`,
                        currentState === 'completed' ? 'success' : 'error');
                    item.lastLoggedTrackState = currentState;
                }
            }
        }

        if (task.current_track && task.current_track !== item.lastLoggedTrack) {
            this.uiManager.addLogEntry(`Downloading: ${task.current_track}`, 'info');
            item.lastLoggedTrack = task.current_track;
            const trackNumber = this.findTrackNumberByName(item, task.current_track);
            item.lastLoggedTrackState = trackNumber ? (item.trackStates.get(trackNumber) || 'downloading') : 'downloading';
        }
    }

    logTerminalStateOnce(item, task) {
        if (item.loggedTerminal) return;
        if (!['completed', 'failed', 'cancelled'].includes(task.status)) return;
        item.loggedTerminal = true;

        if (task.status === 'completed') {
            const summary = task.total_tracks
                ? `Downloaded ${task.completed_tracks}/${task.total_tracks} tracks`
                : 'Download completed successfully';
            const hasFailures = (task.failed_track_names || []).length > 0;
            this.uiManager.addLogEntry(summary, hasFailures ? 'error' : 'success');
            if (item.trackData && item.trackData.tracks) {
                item.trackData.tracks.forEach((track, index) => {
                    const trackKey = index + 1;
                    const state = (task.failed_track_names || []).includes(track.name) ? 'error' : 'completed';
                    this.updateTrackState(item, trackKey, state);
                });
            } else {
                item.trackStates.forEach((state, trackNumber) => {
                    if (state !== 'error') this.updateTrackState(item, trackNumber, 'completed');
                });
            }
        } else if (task.status === 'failed') {
            this.uiManager.addLogEntry(`Error: ${task.error_message || 'Download failed'}`, 'error');
        } else if (task.status === 'cancelled') {
            this.uiManager.addLogEntry(`${item.title}: download cancelled`, 'info');
        }
    }

    updateGlobalStatus() {
        const active = this.queue.find((item) => item.status === 'downloading');
        const hasQueuedWork = this.queue.some((item) => ['queued', 'paused'].includes(item.status));
        const hasWork = Boolean(active) || hasQueuedWork;

        // The "downloading" flag only stays true while something is actually
        // running (or in the middle of being stopped); once the pause is
        // fully applied it drops so the button flips to "Resume Download".
        const downloadingFlag = this.queuePaused ? Boolean(active) : hasWork;
        this.uiManager.updateUI(downloadingFlag, this.queuePaused);

        if (active) {
            this.uiManager.updateProgress(active.progress || 0);
            this.uiManager.updateStatus(`Downloading ${active.title}... ${Math.round(active.progress || 0)}%`, 'info');
        } else if (this.queuePaused && hasWork) {
            this.uiManager.updateStatus('Queue paused. Resume to continue.', 'info');
        } else if (hasWork) {
            this.uiManager.updateStatus('Waiting for a free download slot...', 'info');
        } else if (!this.queue.length) {
            this.uiManager.updateProgress(0);
        }

        // Nothing left to track: stop polling instead of spamming the API
        // forever; startDownload()/stopDownload() restart it on demand.
        if (!hasWork) {
            this.stopPolling();
        }

        if (this.saveStateCallback) this.saveStateCallback();
    }

    async stopDownload() {
        if (this.queuePaused) {
            await this.resumeDownload();
            return;
        }
        try {
            await this.apiClient.pauseQueue();
            this.queuePaused = true;
            this.uiManager.updateStatus('Pausing queue...', 'info');
            await this.pollOnce();
            this.ensurePolling();
        } catch (error) {
            this.uiManager.updateStatus(`Could not pause queue: ${error.message}`, 'error');
            this.uiManager.addLogEntry(`Could not pause queue: ${error.message}`, 'error');
        }
    }

    async resumeDownload() {
        try {
            await this.apiClient.resumeQueue();
            this.queuePaused = false;
            this.uiManager.updateStatus('Resuming download queue...', 'info');
            await this.pollOnce();
            this.ensurePolling();
        } catch (error) {
            this.uiManager.updateStatus(`Could not resume queue: ${error.message}`, 'error');
            this.uiManager.addLogEntry(`Could not resume queue: ${error.message}`, 'error');
        }
    }

    async clearQueue() {
        try {
            await this.apiClient.clearQueuedDownloads();
            this.uiManager.updateStatus('Queued downloads cleared.', 'info');
            await this.pollOnce();
        } catch (error) {
            this.uiManager.updateStatus(`Could not clear queue: ${error.message}`, 'error');
        }
    }

    async clearCompleted() {
        try {
            const result = await this.apiClient.clearCompletedDownloads();
            this.uiManager.updateStatus(
                result.removed ? `Removed ${result.removed} finished download${result.removed === 1 ? '' : 's'}.` : 'No completed downloads to clear.',
                'info'
            );
            await this.pollOnce();
        } catch (error) {
            this.uiManager.updateStatus(`Could not clear completed downloads: ${error.message}`, 'error');
        }
    }

    async removeItem(taskId) {
        try {
            await this.apiClient.removeDownload(taskId);
            await this.pollOnce();
        } catch (error) {
            this.uiManager.updateStatus(`Could not remove download: ${error.message}`, 'error');
        }
    }

    // Removes a single queue item, whether it's still queued, actively
    // downloading (stopping the current track and any remaining tracks for
    // it), or already finished.
    async cancelItem(taskId, status = null) {
        // Items the server hasn't acknowledged yet (or that already failed
        // client-side, e.g. submitToServer errors) only exist locally.
        if (String(taskId).startsWith('local-')) {
            this.queue = this.queue.filter((item) => item.id !== taskId);
            this.uiManager.renderDownloadQueue(this.queue);
            return;
        }
        try {
            if (['completed', 'failed', 'cancelled'].includes(status)) {
                await this.apiClient.removeDownload(taskId);
            } else {
                await this.apiClient.cancelDownload(taskId);
            }
            await this.pollOnce();
        } catch (error) {
            this.uiManager.updateStatus(`Could not remove download: ${error.message}`, 'error');
        }
    }

    findTrackNumberByName(item, trackName) {
        if (!item.trackData || !item.trackData.tracks) {
            return null;
        }

        const cleanTrackName = trackName.toLowerCase().trim();
        for (const [index, track] of item.trackData.tracks.entries()) {
            const cleanCurrentName = track.name.toLowerCase().trim();
            if (cleanCurrentName === cleanTrackName ||
                cleanCurrentName.includes(cleanTrackName) ||
                cleanTrackName.includes(cleanCurrentName)) {
                return index + 1;
            }
        }
        return null;
    }

    updateTrackState(item, trackNumber, state) {
        item.trackStates.set(trackNumber, state);
        this.uiManager.updateQueuedTrackState(item.id, trackNumber, state);
    }

    clearStates() {
        this.queue = [];
        this.uiManager.renderDownloadQueue(this.queue);
    }

    // Loads whatever the server already knows about (e.g. after a page
    // reload) so downloads that kept running while the page was closed
    // show up immediately, then keeps polling for live updates.
    async hydrateFromServer() {
        await this.pollOnce();
        this.ensurePolling();
    }
}